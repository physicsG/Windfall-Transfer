import unittest

from windfall.netinfo import Interface
from windfall.usb4 import Usb4Monitor, speed_text

LOCAL, MAC = "169.254.77.10", "169.254.21.56"


def adapter(connected=True, description="USB4(TM) P2P Network Adapter", out_octets=0, in_octets=0):
    return Interface(
        index=42,
        alias="Ethernet 7",
        description=description,
        connected=connected,
        tx_speed=20_000_000_000,
        rx_speed=20_000_000_000,
        in_octets=in_octets,
        out_octets=out_octets,
        mtu=62000,
    )


class FakeNet:
    def __init__(self):
        self.ifaces = [
            Interface(1, "Wi-Fi", "Intel(R) Wi-Fi 6E AX211 160MHz", True, 144_400_000, 144_400_000, 0, 0, 1500)
        ]
        self.addresses = [("127.0.0.1", 8, 1)]
        self.seen = []

    def interfaces(self):
        return list(self.ifaces)

    def ipv4_addresses(self, index=None):
        return [a for a in self.addresses if index is None or a[2] == index]

    def neighbors(self, index):
        return list(self.seen) if index == 42 else []


class Usb4MonitorTests(unittest.TestCase):
    def setUp(self):
        self.net = FakeNet()
        self.answers = {}
        self.queries = []
        self.monitor = Usb4Monitor(net=self.net, discover=self.discover)

    def discover(self, local_ip, exclude=()):
        self.queries.append((local_ip, set(exclude)))
        return dict(self.answers)

    def test_no_adapter_or_unplugged(self):
        self.monitor.check(now=0)
        self.assertEqual(self.monitor.state, Usb4Monitor.ABSENT)
        self.net.ifaces.append(adapter(connected=False))
        self.monitor.check(now=1)
        self.assertEqual(self.monitor.state, Usb4Monitor.DOWN)

    def test_finds_the_mac_with_bonjour(self):
        self.net.ifaces.append(adapter())
        with self.assertLogs("bridge", "INFO") as logs:
            self.monitor.check(now=0)  # link up, but Windows hasn't picked its address yet
            self.assertEqual(self.monitor.state, Usb4Monitor.SEARCHING)
            self.assertEqual(self.queries, [])
            self.net.addresses.append((LOCAL, 16, 42))
            self.answers = {MAC: "Alex's MacBook Pro"}
            self.monitor.check(now=2)  # too soon to search again
            self.assertEqual(self.monitor.state, Usb4Monitor.SEARCHING)
            self.monitor.check(now=6)
        self.assertEqual(
            (self.monitor.state, self.monitor.mac_ip, self.monitor.mac_name),
            (Usb4Monitor.FOUND, MAC, "Alex's MacBook Pro"),
        )
        self.assertEqual(self.queries, [(LOCAL, {"127.0.0.1", LOCAL})])
        self.assertIn("found the Mac on Thunderbolt/USB4 at 169.254.21.56", "\n".join(logs.output))
        self.assertEqual(speed_text(self.monitor.link_speed), "20 Gbps")

    def test_falls_back_to_the_neighbor_table_and_keeps_the_mac_on_a_missed_recheck(self):
        self.net.ifaces.append(adapter())
        self.net.addresses.append((LOCAL, 16, 42))
        self.net.seen = [LOCAL, MAC]
        self.monitor.check(now=0)
        self.assertEqual((self.monitor.state, self.monitor.mac_ip), (Usb4Monitor.FOUND, MAC))
        self.net.seen = []
        self.monitor.check(now=100)  # re-check finds nothing: keep the address
        self.assertEqual((self.monitor.state, self.monitor.mac_ip), (Usb4Monitor.FOUND, MAC))

    def test_manual_address_wins(self):
        self.net.ifaces.append(adapter(description="Thunderbolt(TM) Networking"))
        self.monitor.set_manual_address(" 169.254.1.2 ")
        self.monitor.check(now=0)
        self.assertEqual((self.monitor.state, self.monitor.mac_ip), (Usb4Monitor.FOUND, "169.254.1.2"))

    def test_measures_traffic_and_forgets_the_mac_when_unplugged(self):
        self.net.ifaces.append(adapter())
        self.net.addresses.append((LOCAL, 16, 42))
        self.answers = {MAC: None}
        self.monitor.check(now=0)
        self.net.ifaces[-1] = adapter(out_octets=100_000_000, in_octets=2_000_000)
        self.monitor.check(now=2)
        self.assertEqual(self.monitor.rates, (50.0, 1.0))
        self.net.ifaces[-1] = adapter(connected=False)
        with self.assertLogs("bridge", "INFO") as logs:
            self.monitor.check(now=3)
        self.assertEqual(
            (self.monitor.state, self.monitor.mac_ip, self.monitor.rates), (Usb4Monitor.DOWN, None, (0.0, 0.0))
        )
        self.assertIn("Thunderbolt/USB4 link is down", "\n".join(logs.output))


if __name__ == "__main__":
    unittest.main()
