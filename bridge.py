"""connect-app bridge: gives Windows a network adapter that is wired to the Mac over the USB-C cable.

Start it with "Start bridge.cmd" (or `python bridge.py`). It asks for administrator rights because it creates
a network adapter. While it runs, this PC is 10.77.0.1 and the Mac gets 10.77.0.2 over DHCP.
"""

import argparse
import ctypes
import ipaddress
import logging
import os
import struct
import subprocess
import sys
import threading
import time

from connect_app import packets as pk
from connect_app.dhcp import DhcpServer
from connect_app.ncm import NcmFunction, find_functions
from connect_app.winusb import WinUsbError, open_device
from connect_app.wintun import Wintun, add_ipv4_address, interface_index

HERE = os.path.dirname(os.path.abspath(__file__))
WINTUN_DLL = os.path.join(HERE, "vendor", "wintun", "wintun.dll")
LOG_FILE = os.path.join(HERE, "logs", "bridge.log")
ADAPTER_NAME = "Mac USB Link"
ADAPTER_GUID = "{6F6D2C1A-3B7E-4C55-9E1D-2A5C0FFEE0A1}"  # fixed, so Windows recognises the same network each time
APPLE_VID, MAC_PID = 0x05AC, 0x1905

log = logging.getLogger("bridge")


class Bridge:
    """Moves traffic between the Mac's USB network function (Ethernet frames) and the adapter (IP packets)."""

    def __init__(self, fn, session, windows_ip, mac_ip, netmask):
        self.fn = fn
        self.session = session
        self.our_mac = fn.host_mac or b"\x02\x00\x5e\xc0\xde\x01"
        self.windows_ip = windows_ip
        self.broadcast = bytes(a | (~m & 0xFF) for a, m in zip(windows_ip, netmask))
        self.dhcp = DhcpServer(windows_ip, mac_ip, netmask)
        self.max_packet = (fn.max_datagram or 1514) - 14
        self.peer_mac = None
        self.peer_addresses = set()  # the Mac's own IPs; we never answer ARP/NDP for those
        self.stop = threading.Event()
        self.counters = {"to_mac": 0, "to_mac_bytes": 0, "to_windows": 0, "to_windows_bytes": 0, "dropped": 0}
        self._oversize_warned = False

    # ---- Mac -> Windows ----

    def usb_loop(self):
        while not self.stop.is_set():
            try:
                frames = self.fn.receive()
            except ValueError as e:
                log.warning("bad transfer block from the Mac: %s", e)
                continue
            except OSError as e:
                self._lost(e)
                break
            for frame in frames:
                try:
                    self.from_mac(frame)
                except EOFError:
                    self.stop.set()
                    break
                except OSError as e:  # a reply to the Mac could not be sent
                    self._lost(e)
                    break
                except Exception:
                    log.exception("error handling a frame from the Mac")

    def _lost(self, error):
        if not self.stop.is_set():
            log.info("USB link to the Mac ended (%s)", error.strerror or error)
        self.stop.set()

    def from_mac(self, frame):
        if len(frame) < 14:
            return
        src_mac = frame[6:12]
        if not src_mac[0] & 1 and src_mac != self.peer_mac:
            self.peer_mac = src_mac
            log.info("Mac's link address: %s", pk.mac_str(src_mac))
        ethertype = frame[12] << 8 | frame[13]
        payload = frame[14:]
        if ethertype == pk.ETH_ARP:
            self._arp(frame)
            return
        if ethertype == pk.ETH_IPV4 and len(payload) >= 20:
            packet = payload[:(payload[2] << 8) | payload[3]]  # drop Ethernet padding
            if any(packet[12:16]):
                self.peer_addresses.add(packet[12:16])
            if packet[9] == pk.PROTO_UDP and self._dhcp(packet):
                return
        elif ethertype == pk.ETH_IPV6 and len(payload) >= 40:
            packet = payload[:40 + ((payload[4] << 8) | payload[5])]
            if any(packet[8:24]):
                self.peer_addresses.add(packet[8:24])
            if packet[6] == pk.PROTO_ICMPV6 and self._neighbor_solicitation(packet, src_mac):
                return
        else:
            return
        if self.session.send(packet):
            self.counters["to_windows"] += 1
            self.counters["to_windows_bytes"] += len(packet)
        else:
            self.counters["dropped"] += 1

    def _arp(self, frame):
        info = pk.inspect(frame)
        if info["type"] != "arp":
            return
        spa, tpa = info["arp_spa"], info["arp_tpa"]
        if any(spa):
            self.peer_addresses.add(spa)
        # Answer "who has 10.77.0.1?" on behalf of Windows; ignore probes (sender 0.0.0.0) and announcements.
        if info["arp_op"] == 1 and any(spa) and spa != tpa and tpa == self.windows_ip:
            self.fn.send([pk.arp(2, self.our_mac, self.windows_ip, info["arp_sha"], spa, dst_mac=info["src_mac"])])

    def _dhcp(self, packet):
        ihl = (packet[0] & 0x0F) * 4
        udp = packet[ihl:]
        if len(udp) < 8 or struct.unpack_from("!HH", udp) != (68, 67):
            return False
        result = self.dhcp.handle(udp[8:])
        if result:
            reply, kind = result
            everyone = b"\xff\xff\xff\xff"
            ip = pk.ipv4(self.windows_ip, everyone, pk.PROTO_UDP, pk.udp4(self.windows_ip, everyone, 67, 68, reply))
            self.fn.send([pk.ethernet(pk.BROADCAST_MAC, self.our_mac, pk.ETH_IPV4, ip)])
            if kind == "ACK":
                log.info("the Mac took address %s", pk.ip_str(self.dhcp.client_ip))
            else:
                log.info("DHCP %s sent to the Mac", kind)
        return True  # DHCP client traffic is for us, not for Windows

    def _neighbor_solicitation(self, packet, src_mac):
        """IPv6 'who has X?' from the Mac: answer for anything that isn't the Mac's own address (i.e. Windows)."""
        icmp = packet[40:]
        if len(icmp) < 24 or icmp[0] != 135:
            return False
        src_ip, target = packet[8:24], bytes(icmp[8:24])
        if any(src_ip) and target not in self.peer_addresses:
            self.fn.send([pk.neighbor_advert(self.our_mac, target, src_mac, src_ip, solicited=True)])
        return True

    # ---- Windows -> Mac ----

    def tun_loop(self):
        batch, size = [], 0
        while not self.stop.is_set():
            try:
                packet = self.session.receive()
            except EOFError:
                self.stop.set()
                break
            if packet is None:
                if batch:
                    self._flush(batch)
                    batch, size = [], 0
                self.session.wait(250)
                continue
            frame = self.to_ethernet(packet)
            if frame is None:
                self.counters["dropped"] += 1
                continue
            batch.append(frame)
            size += len(frame)
            if len(batch) >= 256 or size >= 24000:
                self._flush(batch)
                batch, size = [], 0

    def _flush(self, batch):
        try:
            self.fn.send(batch)
        except OSError as e:
            self._lost(e)
            return
        self.counters["to_mac"] += len(batch)
        self.counters["to_mac_bytes"] += sum(len(f) - 14 for f in batch)

    def to_ethernet(self, packet):
        if len(packet) > self.max_packet:
            if not self._oversize_warned:
                log.warning("dropping %d-byte packets: the link carries at most %d", len(packet), self.max_packet)
                self._oversize_warned = True
            return None
        version = packet[0] >> 4
        if version == 4 and len(packet) >= 20:
            dst = packet[16:20]
            if dst == b"\xff\xff\xff\xff" or dst == self.broadcast:
                mac = pk.BROADCAST_MAC
            elif 224 <= dst[0] <= 239:
                mac = b"\x01\x00\x5e" + bytes([dst[1] & 0x7F]) + dst[2:4]
            else:
                mac = self.peer_mac
            ethertype = pk.ETH_IPV4
        elif version == 6 and len(packet) >= 40:
            dst = packet[24:40]
            mac = pk.ipv6_multicast_mac(dst) if dst[0] == 0xFF else self.peer_mac
            ethertype = pk.ETH_IPV6
        else:
            return None
        if mac is None:
            return None  # haven't heard from the Mac yet
        return mac + self.our_mac + struct.pack("!H", ethertype) + packet


def is_admin():
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except OSError:
        return False


def relaunch_as_admin():
    command = subprocess.list2cmdline([sys.executable, os.path.abspath(__file__)] + sys.argv[1:])
    # cmd /k keeps the window open afterwards, so any error stays readable.
    return ctypes.windll.shell32.ShellExecuteW(None, "runas", "cmd.exe", f'/k "{command}"', HERE, 1) > 32


def wait_for_mac(poll_seconds=1.0, report_after=3.0):
    """Block until the Mac's USB device can be opened. Explains why only if the wait drags on."""
    reason, since, reported = None, time.monotonic(), None
    while True:
        try:
            return open_device(APPLE_VID, MAC_PID)
        except WinUsbError as e:
            if e.strerror != reason:
                reason, since = e.strerror, time.monotonic()
            if reason != reported and time.monotonic() - since >= report_after:
                log.info("waiting for the Mac: %s. If the cable is plugged in, unplug and replug it.", reason)
                reported = reason
            time.sleep(poll_seconds)


def report_traffic(bridge, interval=5.0):
    previous, last = dict(bridge.counters), time.monotonic()
    while not bridge.stop.is_set():
        time.sleep(0.5)  # short sleeps keep Ctrl+C responsive
        now = time.monotonic()
        if now - last < interval:
            continue
        current = dict(bridge.counters)
        to_mac = (current["to_mac_bytes"] - previous["to_mac_bytes"]) / (now - last) / 1e6
        to_windows = (current["to_windows_bytes"] - previous["to_windows_bytes"]) / (now - last) / 1e6
        if to_mac >= 0.05 or to_windows >= 0.05:
            log.info("traffic: to Mac %.1f MB/s, to Windows %.1f MB/s", to_mac, to_windows)
        previous, last = current, now


def main():
    parser = argparse.ArgumentParser(description="Network bridge to a Mac over a USB-C cable.")
    parser.add_argument("--windows-ip", default="10.77.0.1", help="this PC's address on the link")
    parser.add_argument("--mac-ip", default="10.77.0.2", help="address handed to the Mac")
    parser.add_argument("--prefix", type=int, default=24, help="subnet prefix length")
    args = parser.parse_args()

    windows_ip = ipaddress.IPv4Address(args.windows_ip)
    mac_ip = ipaddress.IPv4Address(args.mac_ip)
    network = ipaddress.IPv4Network(f"{windows_ip}/{args.prefix}", strict=False)
    if mac_ip not in network or mac_ip == windows_ip:
        parser.error("--mac-ip must be a different address in the same subnet as --windows-ip")

    if not is_admin():
        print("The bridge creates a network adapter, which needs administrator rights. Asking Windows...")
        return 0 if relaunch_as_admin() else 1

    os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(message)s", datefmt="%H:%M:%S",
                        handlers=[logging.StreamHandler(sys.stdout), logging.FileHandler(LOG_FILE, encoding="utf-8")])

    wintun = Wintun(WINTUN_DLL, log=lambda level, m: log.log(logging.WARNING if level else logging.DEBUG,
                                                              "wintun: %s", m))
    log.info("creating network adapter '%s'...", ADAPTER_NAME)
    adapter = wintun.create_adapter(ADAPTER_NAME, "ConnectApp", ADAPTER_GUID)
    try:
        add_ipv4_address(adapter.luid, windows_ip.packed, args.prefix)
        index = interface_index(adapter.luid)
        for family in ("ipv4", "ipv6"):
            subprocess.run(["netsh", "interface", family, "set", "subinterface", str(index), "mtu=1500",
                            "store=active"], capture_output=True)
        log.info("this PC is %s on the cable; the Mac gets %s. With File Sharing on the Mac, open \\\\%s "
                 "in Explorer. Press Ctrl+C to stop.", windows_ip, mac_ip, mac_ip)
        serve(adapter, windows_ip, mac_ip, network)
    except KeyboardInterrupt:
        pass
    finally:
        log.info("stopping...")
        adapter.close()
    log.info("stopped.")
    return 0


def serve(adapter, windows_ip, mac_ip, network):
    """Bridge every time the Mac is plugged in, until Ctrl+C. The adapter stays; only its link goes up and down."""
    while True:
        device = wait_for_mac()
        session = None
        try:
            session = adapter.start_session()  # the adapter shows as connected while a session is open
            log.info("the Mac is connected")
            run_connection(device, session, windows_ip, mac_ip, network)
            log.info("the Mac disconnected; plug the cable back in and the bridge reconnects by itself")
        except OSError as e:
            log.warning("could not use the Mac's USB connection (%s); trying again", e.strerror or e)
            time.sleep(2)
        finally:
            if session:
                session.close()
            device.close()


def run_connection(device, session, windows_ip, mac_ip, network):
    """Bridge one USB connection to the Mac until it drops."""
    functions = find_functions(device)
    if not functions:
        raise OSError(0, "the Mac's USB device has no network function")
    fn = NcmFunction(device, *functions[0], "A")
    try:
        device.disable_selective_suspend()  # the Mac does not wake up reliably from USB suspend
    except OSError as e:
        log.warning("could not disable USB power saving: %s", e)
    fn.start(read_timeout_ms=0, write_timeout_ms=5000)  # a stuck write means the Mac stopped responding
    for note in fn.notes:
        log.info("note: %s", note)

    bridge = Bridge(fn, session, windows_ip.packed, mac_ip.packed, network.netmask.packed)
    threads = [threading.Thread(target=bridge.usb_loop, daemon=True),
               threading.Thread(target=bridge.tun_loop, daemon=True)]
    for t in threads:
        t.start()
    try:
        report_traffic(bridge)
    finally:
        bridge.stop.set()
        fn.abort_receive()
        for t in threads:
            t.join(timeout=6)  # longer than the write timeout, so neither thread outlives the session
        fn.stop()


if __name__ == "__main__":
    sys.exit(main())
