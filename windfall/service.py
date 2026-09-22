"""The bridge between the Mac's USB network function and a Wintun adapter, and the service that keeps it running."""

import ipaddress
import logging
import os
import struct
import subprocess
import threading
import time

from . import packets as pk
from .dhcp import DhcpServer
from .driver import APPLE_VID, KNOWN_MAC_PIDS
from .ncm import NcmFunction, find_functions
from .winusb import WinUsbError, open_device
from .wintun import Wintun, add_ipv4_address, interface_index

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WINTUN_DLL = os.path.join(ROOT, "vendor", "wintun", "wintun.dll")
DATA_DIR = os.path.join(os.environ.get("LOCALAPPDATA") or os.path.expanduser("~"), "Windfall Transfer")
LOG_FILE = os.path.join(DATA_DIR, "logs", "bridge.log")  # outside the app folder, which may be read-only
LEGACY_DATA_DIR = os.path.join(os.path.dirname(DATA_DIR), "ConnectApp")  # logs under the earlier name, Connect App
ADAPTER_NAME = "Mac USB Link"
ADAPTER_GUID = "{6F6D2C1A-3B7E-4C55-9E1D-2A5C0FFEE0A1}"  # fixed, so Windows recognises the same network each time

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
        self.mac_configured = threading.Event()  # set once the Mac accepted its address
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
                self.mac_configured.set()
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


def _rates(before, after, seconds):
    return ((after["to_mac_bytes"] - before["to_mac_bytes"]) / seconds / 1e6,
            (after["to_windows_bytes"] - before["to_windows_bytes"]) / seconds / 1e6)


class BridgeService:
    """Runs the bridge in a background thread: creates the adapter, then bridges every time the Mac is plugged in,
    until stop(). The adapter stays for the whole run; only its link goes up and down with the cable."""

    STOPPED, STARTING, WAITING, CONNECTED, FAILED = "stopped", "starting", "waiting", "connected", "failed"

    def __init__(self, windows_ip="10.77.0.1", mac_ip="10.77.0.2", prefix=24, wintun_dll=WINTUN_DLL):
        self.windows_ip = ipaddress.IPv4Address(windows_ip)
        self.mac_ip = ipaddress.IPv4Address(mac_ip)
        self.prefix = int(prefix)
        if not 8 <= self.prefix <= 30:
            raise ValueError("the prefix length must be between 8 and 30")
        self.network = ipaddress.IPv4Network(f"{self.windows_ip}/{self.prefix}", strict=False)
        if self.mac_ip not in self.network or self.mac_ip == self.windows_ip:
            raise ValueError("the Mac's address must be a different address in the same subnet as this PC's")
        self.wintun_dll = wintun_dll
        self.state = self.STOPPED
        self.error = None
        self.waiting_reason = None  # why the Mac can't be used yet, once the wait drags on
        self.rates = (0.0, 0.0)  # MB/s to the Mac, to Windows
        self._bridge = None
        self._quit = threading.Event()
        self._thread = None

    @property
    def running(self):
        return self._thread is not None and self._thread.is_alive()

    @property
    def mac_configured(self):
        bridge = self._bridge
        return bool(bridge and bridge.mac_configured.is_set())

    def start(self):
        if self.running:
            return
        self._quit.clear()
        self.error = None
        self.state = self.STARTING
        self._thread = threading.Thread(target=self._run, name="bridge", daemon=True)
        self._thread.start()

    def stop(self, timeout=10):
        self._quit.set()
        if self._thread:
            self._thread.join(timeout)

    def _run(self):
        try:
            wintun = Wintun(self.wintun_dll, log=lambda level, message: log.log(
                logging.WARNING if level else logging.DEBUG, "wintun: %s", message))
            log.info("creating network adapter '%s'...", ADAPTER_NAME)
            adapter = wintun.create_adapter(ADAPTER_NAME, "WindfallTransfer", ADAPTER_GUID)
            try:
                add_ipv4_address(adapter.luid, self.windows_ip.packed, self.prefix)
                index = interface_index(adapter.luid)
                for family in ("ipv4", "ipv6"):
                    subprocess.run(["netsh", "interface", family, "set", "subinterface", str(index), "mtu=1500",
                                    "store=active"], capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW)
                log.info("this PC is %s on the cable; the Mac gets %s", self.windows_ip, self.mac_ip)
                self._serve(adapter)
            finally:
                adapter.close()
        except Exception as e:
            self.error = str(e)
            self.state = self.FAILED
            log.error("the bridge stopped with an error: %s", e)
        else:
            self.state = self.STOPPED
            log.info("bridge stopped")

    def _serve(self, adapter):
        while not self._quit.is_set():
            self.state = self.WAITING
            device = self._wait_for_mac()
            if device is None:
                return
            session = None
            try:
                session = adapter.start_session()  # the adapter shows as connected while a session is open
                self.state = self.CONNECTED
                log.info("the Mac is connected")
                self._run_connection(device, session)
                if not self._quit.is_set():
                    log.info("the Mac disconnected; plug the cable back in and the bridge reconnects by itself")
            except OSError as e:
                log.warning("could not use the Mac's USB connection (%s); trying again", e.strerror or e)
                self._quit.wait(2)
            finally:
                self._bridge = None
                self.rates = (0.0, 0.0)
                if session:
                    session.close()
                device.close()

    def _wait_for_mac(self, poll_seconds=1.0, report_after=3.0):
        """The Mac's USB device once it can be opened, or None when stopping. Explains only if the wait drags on."""
        reason, since, reported = None, time.monotonic(), None
        self.waiting_reason = None
        while not self._quit.is_set():
            try:
                return self._open_mac()
            except WinUsbError as e:
                if e.strerror != reason:
                    reason, since = e.strerror, time.monotonic()
                if reason != reported and time.monotonic() - since >= report_after:
                    log.info("waiting for the Mac: %s. If the cable is plugged in, unplug and replug it.", reason)
                    reported = self.waiting_reason = reason
            self._quit.wait(poll_seconds)
        return None

    @staticmethod
    def _open_mac():
        error = None
        for pid in sorted(KNOWN_MAC_PIDS):
            try:
                return open_device(APPLE_VID, pid)
            except WinUsbError as e:
                error = e
        raise error

    def _run_connection(self, device, session):
        """Bridge one USB connection to the Mac until it drops or the service stops."""
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

        bridge = Bridge(fn, session, self.windows_ip.packed, self.mac_ip.packed, self.network.netmask.packed)
        threads = [threading.Thread(target=bridge.usb_loop, name="usb", daemon=True),
                   threading.Thread(target=bridge.tun_loop, name="tun", daemon=True)]
        for t in threads:
            t.start()
        self._bridge = bridge
        try:
            self._monitor(bridge)
        finally:
            bridge.stop.set()
            fn.abort_receive()
            for t in threads:
                t.join(timeout=6)  # longer than the write timeout, so neither thread outlives the session
            fn.stop()

    def _monitor(self, bridge, sample=1.0, report_every=5.0):
        """Keep self.rates current (and log busy periods) until the connection drops or the service stops."""
        last_sample = last_report = time.monotonic()
        sampled = reported = dict(bridge.counters)
        while not bridge.stop.is_set() and not self._quit.wait(0.25):
            now = time.monotonic()
            if now - last_sample >= sample:
                current = dict(bridge.counters)
                self.rates = _rates(sampled, current, now - last_sample)
                sampled, last_sample = current, now
            if now - last_report >= report_every:
                current = dict(bridge.counters)
                to_mac, to_windows = _rates(reported, current, now - last_report)
                if to_mac >= 0.05 or to_windows >= 0.05:
                    log.info("traffic: to Mac %.1f MB/s, to Windows %.1f MB/s", to_mac, to_windows)
                reported, last_report = current, now
