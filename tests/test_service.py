"""Plug / unplug / replug / stuck write / stop, against BridgeService with fake USB and Wintun objects."""

import struct
import threading
import time
import unittest
from unittest import mock

from connect_app import packets as pk
from connect_app import service as service_module
from connect_app.service import BridgeService
from connect_app.winusb import WinUsbError

MAC_HW = bytes.fromhex("362520ea6f78")
WIN, MAC = bytes([10, 77, 0, 1]), bytes([10, 77, 0, 2])


def dhcp(options):
    bootp = struct.pack("!BBBB4sH2s4s4s4s4s16s64s128s", 1, 1, 6, 0, b"\x01\x02\x03\x04", 0, b"\x00\x00",
                        bytes(4), bytes(4), bytes(4), bytes(4), MAC_HW + bytes(10), b"", b"")
    bootp += b"\x63\x82\x53\x63" + options + b"\xff"
    everyone = b"\xff" * 4
    return pk.ethernet(pk.BROADCAST_MAC, MAC_HW, pk.ETH_IPV4,
                       pk.ipv4(bytes(4), everyone, pk.PROTO_UDP, pk.udp4(bytes(4), everyone, 68, 67, bootp)))


class Scenario:
    def __init__(self):
        self.connection = 0
        self.events = []
        self.acked = threading.Event()


class FakeFunction:
    """Connection 1: the cable is pulled. Connection 2: the Mac stops accepting data."""

    scenario = None

    def __init__(self, device, control, data, name):
        self.n = device.n
        self.host_mac = bytes.fromhex("362520ea6f87")
        self.max_datagram = 1514
        self.notes = []
        self.aborted = threading.Event()
        self.script = [[dhcp(bytes([53, 1, 1]))], [dhcp(bytes([53, 1, 3, 50, 4]) + MAC + bytes([54, 4]) + WIN)]]

    def start(self, read_timeout_ms=500, write_timeout_ms=0):
        self.scenario.events.append(f"{self.n}: start read={read_timeout_ms} write={write_timeout_ms}")

    def receive(self):
        if self.script:
            return self.script.pop(0)
        if self.n == 1:
            time.sleep(0.3)
            raise OSError(22, "The device does not recognize the command")
        self.aborted.wait()
        raise OSError(995, "The I/O operation has been aborted")

    def send(self, frames):
        for frame in frames:
            if pk.inspect(frame).get("dhcp") == "ACK":
                self.scenario.acked.set()
            if self.n == 2 and frame[12:14] == b"\x08\x00" and frame[23] == pk.PROTO_ICMP:
                raise OSError(121, "The semaphore timeout period has expired")

    def abort_receive(self):
        self.aborted.set()

    def stop(self):
        self.scenario.events.append(f"{self.n}: stop")


class FakeDevice:
    def __init__(self, scenario, n):
        self.scenario, self.n = scenario, n

    def disable_selective_suspend(self):
        pass

    def close(self):
        self.scenario.events.append(f"{self.n}: device closed")


class FakeSession:
    def __init__(self, scenario):
        self.scenario = scenario
        self.n = scenario.connection
        self.closed = False
        self.ping = [pk.ipv4(WIN, MAC, pk.PROTO_ICMP, bytes(8))] if self.n == 2 else []

    def receive(self):
        assert not self.closed, "session used after close"
        if self.ping and self.scenario.acked.is_set():  # Windows pings once the Mac has its address
            return self.ping.pop()
        return None

    def wait(self, ms):
        time.sleep(min(ms, 50) / 1000)

    def send(self, packet):
        assert not self.closed, "session used after close"
        return True

    def close(self):
        self.closed = True
        self.scenario.events.append(f"{self.n}: session closed")


class FakeAdapter:
    def __init__(self, scenario):
        self.scenario = scenario

    def start_session(self):
        return FakeSession(self.scenario)


class ServiceTests(unittest.TestCase):
    def test_reconnects_after_unplug_and_stuck_write_then_stops(self):
        scenario = Scenario()
        service = BridgeService()

        def fake_open(vid, pid):
            if scenario.connection == 2:
                service._quit.set()  # "Stop bridge" while waiting for the third plug-in
                raise WinUsbError(2, "no USB device 05ac:1905 is connected")
            scenario.connection += 1
            scenario.acked.clear()
            scenario.events.append(f"{scenario.connection}: plugged in")
            return FakeDevice(scenario, scenario.connection)

        FakeFunction.scenario = scenario
        with mock.patch.object(service_module, "open_device", fake_open), \
                mock.patch.object(service_module, "NcmFunction", FakeFunction), \
                mock.patch.object(service_module, "find_functions", lambda device: [(0, 1)]), \
                self.assertLogs("bridge", "INFO") as logs:
            started = time.monotonic()
            service._serve(FakeAdapter(scenario))
        self.assertLess(time.monotonic() - started, 10)
        self.assertEqual(scenario.events, [
            "1: plugged in", "1: start read=0 write=5000", "1: stop", "1: session closed", "1: device closed",
            "2: plugged in", "2: start read=0 write=5000", "2: stop", "2: session closed", "2: device closed"])
        text = "\n".join(logs.output)
        self.assertEqual(text.count("the Mac took address 10.77.0.2"), 2)
        self.assertEqual(text.count("the Mac disconnected"), 2)
        self.assertIn("semaphore timeout", text)
        self.assertFalse([t for t in threading.enumerate() if t.name in ("usb", "tun")], "threads left running")

    def test_rejects_addresses_in_different_subnets(self):
        with self.assertRaises(ValueError):
            BridgeService("10.77.0.1", "10.78.0.2", 24)
        with self.assertRaises(ValueError):
            BridgeService("10.77.0.1", "10.77.0.1", 24)


if __name__ == "__main__":
    unittest.main()
