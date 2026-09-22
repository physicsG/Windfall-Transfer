"""Thunderbolt/USB4 mode: watch for the network link Windows and macOS set up by themselves over a Thunderbolt or
USB4 cable (Windows' "USB4(TM) P2P Network Adapter" <-> the Mac's "Thunderbolt Bridge"), and find the Mac on it.

That link needs no bridge. This only finds the Mac's address (an address entered by the user, else a Bonjour
answer on the link, else Windows' neighbor table) and measures the traffic.
"""

import logging
import re
import threading
import time

from . import mdns, netinfo

log = logging.getLogger("bridge")

ADAPTER = re.compile(r"USB4.*P2P|Thunderbolt.*Network", re.IGNORECASE)


def speed_text(bits_per_second):
    if bits_per_second >= 1e9:
        return f"{bits_per_second / 1e9:g} Gbps"
    return f"{bits_per_second / 1e6:g} Mbps"


class Usb4Monitor:
    ABSENT, DOWN, SEARCHING, FOUND = "absent", "down", "searching", "found"
    SEARCH_EVERY = 5.0     # seconds between attempts while looking for the Mac
    RECHECK_EVERY = 60.0   # re-confirm the Mac's address this often once found

    def __init__(self, manual_mac_ip="", net=netinfo, discover=mdns.discover, poll_seconds=2.0):
        self.manual_mac_ip = (manual_mac_ip or "").strip()
        self.net = net
        self.discover = discover
        self.poll_seconds = poll_seconds
        self.state = self.ABSENT
        self.adapter = None
        self.local_ip = None
        self.mac_ip = None
        self.mac_name = None
        self.link_speed = 0
        self.rates = (0.0, 0.0)  # MB/s to the Mac, to this PC
        self._last_search = None  # None: search on the next check
        self._last_counters = None
        self._quit = threading.Event()
        self._thread = None

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._quit.clear()
        self._thread = threading.Thread(target=self._run, name="usb4", daemon=True)
        self._thread.start()

    def stop(self, timeout=5):
        self._quit.set()
        if self._thread:
            self._thread.join(timeout)

    def set_manual_address(self, ip):
        """Use this address for the Mac instead of looking for it (empty: look for it again)."""
        self.manual_mac_ip = (ip or "").strip()
        self._last_search = None
        if self.state == self.FOUND:
            self.state = self.SEARCHING  # decided again on the next check

    def _run(self):
        while not self._quit.is_set():
            try:
                self.check()
            except Exception:
                log.exception("checking for a Thunderbolt/USB4 link failed")
            self._quit.wait(self.poll_seconds)

    def check(self, now=None):
        """One polling round."""
        now = time.monotonic() if now is None else now
        adapter = next((i for i in self.net.interfaces() if ADAPTER.search(i.description)), None)
        if adapter is None or not adapter.connected:
            self._lost(self.ABSENT if adapter is None else self.DOWN, adapter)
            return
        if self.state in (self.ABSENT, self.DOWN):
            log.info("Thunderbolt/USB4 link is up (%s, %s)", adapter.description, speed_text(adapter.tx_speed))
            self.state, self._last_search, self._last_counters = self.SEARCHING, None, None
        self.adapter = adapter
        self.link_speed = max(adapter.tx_speed, adapter.rx_speed)
        self._measure(adapter, now)
        local = [ip for ip, _, _ in self.net.ipv4_addresses(adapter.index)]
        self.local_ip = local[0] if local else None
        due = self.SEARCH_EVERY if self.state == self.SEARCHING else self.RECHECK_EVERY
        if self._last_search is None or now - self._last_search >= due:
            self._last_search = now
            self._search(adapter)

    def _lost(self, state, adapter):
        if self.state in (self.SEARCHING, self.FOUND):
            log.info("Thunderbolt/USB4 link is down")
        self.state, self.adapter = state, adapter
        self.mac_ip = self.mac_name = self.local_ip = None
        self.link_speed, self.rates, self._last_counters = 0, (0.0, 0.0), None

    def _measure(self, adapter, now):
        counters = (now, adapter.out_octets, adapter.in_octets)
        previous, self._last_counters = self._last_counters, counters
        if previous and now > previous[0]:
            seconds = now - previous[0]
            self.rates = ((counters[1] - previous[1]) / seconds / 1e6, (counters[2] - previous[2]) / seconds / 1e6)

    def _search(self, adapter):
        ip, name = self._find_mac(adapter)
        if ip is None:
            if self.state != self.FOUND:  # once found, one missed re-check doesn't mean the Mac moved
                self.state = self.SEARCHING
            return
        if ip != self.mac_ip or self.state != self.FOUND:
            log.info("found the Mac on Thunderbolt/USB4 at %s%s", ip, f" ({name})" if name else "")
        if ip != self.mac_ip:
            self.mac_name = None
        self.mac_ip, self.state = ip, self.FOUND
        self.mac_name = name or self.mac_name

    def _find_mac(self, adapter):
        if self.manual_mac_ip:
            return self.manual_mac_ip, None
        if self.local_ip:
            mine = {ip for ip, _, _ in self.net.ipv4_addresses()}
            try:
                answers = self.discover(self.local_ip, exclude=mine)
            except OSError as e:
                log.debug("Bonjour query on the Thunderbolt/USB4 link failed: %s", e)
                answers = {}
            if answers:
                ip = self.mac_ip if self.mac_ip in answers else sorted(answers)[0]
                return ip, answers[ip]
        seen = [ip for ip in self.net.neighbors(adapter.index) if ip != self.local_ip]
        if seen:
            return (self.mac_ip if self.mac_ip in seen else seen[0]), None
        return None, None
