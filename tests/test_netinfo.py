"""Smoke tests against this PC's real network stack (read-only)."""

import unittest

from connect_app import netinfo

LOOPBACK = "Software Loopback Interface 1"


class NetinfoTests(unittest.TestCase):
    def test_lists_interfaces_without_filter_layers(self):
        interfaces = netinfo.interfaces()
        self.assertTrue(any(i.description == LOOPBACK for i in interfaces))
        self.assertFalse([i for i in interfaces if "-WFP " in i.description or "-QoS Packet" in i.description])

    def test_addresses_include_loopback(self):
        self.assertIn("127.0.0.1", [ip for ip, _, _ in netinfo.ipv4_addresses()])

    def test_neighbors_are_a_list_of_ips(self):
        loopback = next(i for i in netinfo.interfaces() if i.description == LOOPBACK)
        self.assertIsInstance(netinfo.neighbors(loopback.index), list)


if __name__ == "__main__":
    unittest.main()
