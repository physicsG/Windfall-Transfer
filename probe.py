"""Diagnostic probe: bring up the Mac's USB network functions over WinUSB and report what the Mac sends and which
ports answer."""

import argparse
import collections
import random
import struct
import sys
import threading
import time

from windfall import packets as pk
from windfall.ncm import NcmFunction, find_functions
from windfall.winusb import WinUsbError, open_device

APPLE_VID, MAC_PID = 0x05AC, 0x1905
TCP_PORTS = {22: "SSH/Remote Login", 445: "SMB/File Sharing", 548: "AFP", 5000: "AirPlay", 7000: "AirPlay"}
PING_ID = 0xC0DE


class Probe:
    """Talks to the Mac over one NCM function and records what happens."""

    def __init__(self, fn, index, log):
        self.fn = fn
        self.log = log
        self.our_mac = fn.host_mac or bytes([0x02, 0x00, 0x5E, 0xC0, 0xDE, index])
        self.our_ll = pk.link_local(self.our_mac)
        self.our_v4 = bytes([169, 254, 77, 10 + index])
        self.peer_mac = None
        self.peer_ll = None
        self.peer_v4 = []
        self.rx = self.tx = 0
        self.kinds = collections.Counter()
        self.ping6 = set()
        self.ping4 = set()
        self.pinged_by_mac = 0
        self.dhcp = collections.Counter()
        self.tcp = {}
        self.tcp_attempts = 0
        self.tcp_seq = random.randrange(1 << 32)
        self.errors = collections.Counter()

    @property
    def tag(self):
        return f"[{self.fn.name}]"

    def send(self, frame):
        self.fn.send([frame])
        self.tx += 1

    def handle(self, frame):
        self.rx += 1
        info = pk.inspect(frame)
        self.kinds[info["text"]] += 1
        if self.kinds[info["text"]] <= 2:
            self.log(f"{self.tag} <- {info['text']}")
        src_mac = info["src_mac"]
        if src_mac == self.our_mac:
            return
        if info["type"] != "other" and self.peer_mac is None and not src_mac[0] & 1:
            self.peer_mac = src_mac
            self.log(f"{self.tag} Mac's MAC on this function: {pk.mac_str(src_mac)}")
        if info["type"] == "ipv6":
            self._handle_ipv6(info)
        elif info["type"] == "arp":
            self._handle_arp(info)
        elif info["type"] == "ipv4":
            self._handle_ipv4(info)

    def _handle_ipv6(self, info):
        src, dst, proto = info["src"], info["dst"], info["proto"]
        if src[:2] == b"\xfe\x80" and src != self.our_ll and self.peer_ll is None:
            self.peer_ll = src
            self.log(f"{self.tag} Mac's IPv6 link-local address: {pk.ip_str(src)}")
        if proto == pk.PROTO_ICMPV6:
            kind = info.get("icmp_type")
            if kind == 135 and info.get("target") == self.our_ll and any(src):
                self.send(pk.neighbor_advert(self.our_mac, self.our_ll, info["src_mac"], src, solicited=True))
            elif kind == 128 and dst == self.our_ll:
                body = info["icmp_body"]
                ident, seq = struct.unpack_from("!HH", body)
                self.pinged_by_mac += 1
                self.send(pk.echo6(self.our_mac, self.our_ll, info["src_mac"], src, ident, seq,
                                   reply=True, data=body[4:]))
            elif kind == 129 and dst == self.our_ll:
                self.ping6.add(pk.ip_str(src))
        elif proto == pk.PROTO_TCP and dst == self.our_ll and info.get("sport") in TCP_PORTS:
            port, flags = info["sport"], info["tcp_flags"]
            if flags & (pk.TCP_SYN | pk.TCP_ACK) == pk.TCP_SYN | pk.TCP_ACK:
                self.tcp[port] = "OPEN - accepts connections over this cable"
                self.send(pk.tcp6(self.our_mac, self.our_ll, info["src_mac"], src, info["dport"], port,
                                  info["tcp_ack"], 0, pk.TCP_RST))
            elif flags & pk.TCP_RST:
                self.tcp.setdefault(port, "closed - reachable, but nothing listening")
        elif proto == pk.PROTO_UDP and info.get("dport") == 547:
            self.dhcp["DHCPv6"] += 1

    def _note_v4(self, addr):
        if any(addr) and addr != self.our_v4 and addr not in self.peer_v4:
            self.peer_v4.append(addr)
            self.log(f"{self.tag} Mac's IPv4 address: {pk.ip_str(addr)}")

    def _handle_arp(self, info):
        spa = info["arp_spa"]
        self._note_v4(spa)
        if info["arp_op"] == 1 and info["arp_tpa"] == self.our_v4 and any(spa):
            self.send(pk.arp(2, self.our_mac, self.our_v4, info["arp_sha"], spa, dst_mac=info["src_mac"]))

    def _handle_ipv4(self, info):
        src, dst = info["src"], info["dst"]
        self._note_v4(src)
        if info.get("dhcp"):
            self.dhcp["DHCP " + info["dhcp"]] += 1
        if info["proto"] == pk.PROTO_ICMP and dst == self.our_v4:
            if info["icmp_type"] == 0:
                self.ping4.add(pk.ip_str(src))
            elif info["icmp_type"] == 8:
                body = info["icmp_body"]
                ident, seq = struct.unpack_from("!HH", body)
                self.pinged_by_mac += 1
                self.send(pk.echo4(self.our_mac, self.our_v4, info["src_mac"], src, ident, seq,
                                   reply=True, data=body[4:]))

    def tick(self, n):
        # All-nodes ping: the Mac answers from its link-local address even before we know it.
        self.send(pk.echo6(self.our_mac, self.our_ll, pk.ipv6_multicast_mac(pk.ALL_NODES), pk.ALL_NODES,
                           PING_ID, n))
        if self.peer_ll and self.peer_mac:
            self.send(pk.echo6(self.our_mac, self.our_ll, self.peer_mac, self.peer_ll, PING_ID, 1000 + n))
            if self.tcp_attempts < 3 and len(self.tcp) < len(TCP_PORTS):
                self.tcp_attempts += 1
                for i, port in enumerate(TCP_PORTS):
                    if port not in self.tcp:
                        self.send(pk.tcp6(self.our_mac, self.our_ll, self.peer_mac, self.peer_ll,
                                          40000 + i, port, self.tcp_seq, 0, pk.TCP_SYN))
        if self.peer_mac:
            for addr in self.peer_v4:
                self.send(pk.echo4(self.our_mac, self.our_v4, self.peer_mac, addr, PING_ID, n))

    def summary(self, out):
        fn, p = self.fn, self.fn.params
        out(f"\n=== Function {fn.name} (USB interfaces {fn.control.number}+{fn.data.number}) ===")
        out(f"  NTB limits: in {p.in_max} B, out {p.out_max} B, out divisor {p.out_divisor} rem {p.out_remainder}, "
            f"align {p.out_alignment}, max datagrams {p.out_max_datagrams or 'any'}, formats 0x{p.formats:x}")
        out(f"  our MAC {pk.mac_str(self.our_mac)} ({'from the Mac' if fn.host_mac else 'made up'}), "
            f"max datagram {fn.max_datagram}")
        for note in fn.notes:
            out(f"  note: {note}")
        out(f"  frames received {self.rx}, sent {self.tx}")
        out(f"  Mac: MAC {pk.mac_str(self.peer_mac) if self.peer_mac else '-'}, "
            f"IPv6 {pk.ip_str(self.peer_ll) if self.peer_ll else '-'}, "
            f"IPv4 {', '.join(pk.ip_str(a) for a in self.peer_v4) or '-'}")
        if self.kinds:
            out("  what the Mac sent (most common first):")
            for text, count in self.kinds.most_common(12):
                out(f"    {count:4d} x {text}")
        out(f"  ping over IPv6: {'answered by ' + ', '.join(sorted(self.ping6)) if self.ping6 else 'no answer'}")
        if self.peer_v4:
            out(f"  ping over IPv4: {'answered by ' + ', '.join(sorted(self.ping4)) if self.ping4 else 'no answer'}")
        if self.pinged_by_mac:
            out(f"  the Mac pinged us {self.pinged_by_mac} times (we answered)")
        if self.dhcp:
            out("  address requests from the Mac: " + ", ".join(f"{k} x{v}" for k, v in self.dhcp.items()))
        if self.peer_ll:
            for port, name in TCP_PORTS.items():
                out(f"  TCP {port:<5} {name:<17} {self.tcp.get(port, 'no answer')}")
        for err, count in self.errors.items():
            out(f"  error x{count}: {err}")

    def works(self):
        return bool(self.ping6 or self.ping4 or any(v.startswith("OPEN") for v in self.tcp.values()))


def receive_loop(probe, stop, log):
    while not stop.is_set():
        try:
            frames = probe.fn.receive()
        except ValueError as e:  # malformed NTB
            probe.errors[f"bad NTB: {e}"] += 1
            continue
        except OSError as e:
            probe.errors[str(e)] += 1
            if probe.errors[str(e)] == 1:
                log(f"{probe.tag} receive error: {e}")
            time.sleep(0.5)
            continue
        for frame in frames:
            try:
                probe.handle(frame)
            except Exception as e:  # keep listening; report at the end
                probe.errors[f"handler: {e!r}"] += 1


def main():
    parser = argparse.ArgumentParser(description="Probe the Mac's USB network link.")
    parser.add_argument("--seconds", type=float, default=40, help="how long to keep the link up (default 40)")
    args = parser.parse_args()

    start = time.monotonic()
    print_lock = threading.Lock()

    def log(message):
        with print_lock:
            print(f"{time.monotonic() - start:6.1f}s  {message}", flush=True)

    try:
        device = open_device(APPLE_VID, MAC_PID)
    except WinUsbError as e:
        print(f"Could not open the Mac's USB device: {e}")
        print("Click 'Set up this PC' in Windfall Transfer first, then close it: only one program can use the device.")
        return 1

    functions, probes = [], []
    try:
        log(f"opened {device.path}")
        for number, itf in sorted(device.interfaces.items()):
            log(f"interface {number}: class {itf.cls:02x}/{itf.subclass:02x}/{itf.protocol:02x}")
        try:
            device.disable_selective_suspend()
        except OSError as e:
            log(f"note: {e}")

        for i, (control, data) in enumerate(find_functions(device)):
            fn = NcmFunction(device, control, data, "AB"[i] if i < 2 else str(i))
            try:
                fn.start()
            except (OSError, RuntimeError) as e:
                log(f"[{fn.name}] could not start: {e}")
                continue
            functions.append(fn)
            log(f"[{fn.name}] link up on interfaces {control}+{data} "
                f"(bulk IN 0x{fn.pipe_in:02x}, OUT 0x{fn.pipe_out:02x})")
        if not functions:
            print("No NCM function could be started.")
            return 1

        probes = [Probe(fn, i, log) for i, fn in enumerate(functions)]
        stop = threading.Event()
        threads = [threading.Thread(target=receive_loop, args=(p, stop, log), daemon=True) for p in probes]
        for t in threads:
            t.start()

        time.sleep(1.5)  # give macOS a moment to notice the link
        n = 0
        try:
            while time.monotonic() - start < args.seconds:
                n += 1
                for p in probes:
                    try:
                        p.tick(n)
                    except OSError as e:
                        p.errors[f"send: {e}"] += 1
                time.sleep(2)
        except KeyboardInterrupt:
            log("interrupted")
        stop.set()
        for t in threads:
            t.join(timeout=2)
    finally:
        for fn in functions:
            fn.stop()
        device.close()

    for p in probes:
        p.summary(print)
    working = [p.fn.name for p in probes if p.works()]
    if working:
        print(f"\nRESULT: the Mac answers over the cable (function {', '.join(working)}).")
    elif any(p.rx for p in probes):
        print("\nRESULT: the Mac sends traffic, but did not answer pings or connections.")
    else:
        print("\nRESULT: no traffic from the Mac on any function.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
