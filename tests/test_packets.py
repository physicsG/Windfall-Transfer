import unittest

from connect_app import packets as pk

MAC = bytes.fromhex("020000000001")
LL = pk.link_local(MAC)
V4_A, V4_B = bytes([169, 254, 77, 10]), bytes([169, 254, 1, 2])


class PacketTests(unittest.TestCase):
    def test_link_local_uses_modified_eui64(self):
        self.assertEqual(pk.ip_str(LL), "fe80::ff:fe00:1")

    def test_icmpv6_checksum_verifies(self):
        frame = pk.echo6(MAC, LL, pk.ipv6_multicast_mac(pk.ALL_NODES), pk.ALL_NODES, 1, 2)
        icmp = frame[14 + 40:]
        self.assertEqual(pk.checksum(pk._pseudo6(LL, pk.ALL_NODES, len(icmp), pk.PROTO_ICMPV6) + icmp), 0)

    def test_neighbor_advert_has_hop_limit_255_and_valid_checksum(self):
        frame = pk.neighbor_advert(MAC, LL, MAC, LL, solicited=True)
        self.assertEqual(frame[14 + 7], 255)
        icmp = frame[14 + 40:]
        self.assertEqual(pk.checksum(pk._pseudo6(LL, LL, len(icmp), pk.PROTO_ICMPV6) + icmp), 0)

    def test_tcp_checksum_verifies(self):
        segment = pk.tcp6(MAC, LL, MAC, LL, 40000, 22, 12345, 0, pk.TCP_SYN)[14 + 40:]
        self.assertEqual(pk.checksum(pk._pseudo6(LL, LL, len(segment), pk.PROTO_TCP) + segment), 0)

    def test_ipv4_icmp_and_udp_checksums_verify(self):
        frame = pk.echo4(MAC, V4_A, MAC, V4_B, 1, 1)
        self.assertEqual(pk.checksum(frame[14:34]), 0)
        self.assertEqual(pk.checksum(frame[34:]), 0)
        udp = pk.udp4(V4_A, V4_B, 67, 68, b"hello")
        pseudo = V4_A + V4_B + bytes([0, pk.PROTO_UDP]) + len(udp).to_bytes(2, "big")
        self.assertEqual(pk.checksum(pseudo + udp), 0)

    def test_inspect_describes_frames(self):
        self.assertEqual(pk.inspect(pk.arp(1, MAC, bytes(4), bytes(6), V4_B))["text"], "ARP probe for 169.254.1.2")
        self.assertEqual(pk.inspect(pk.echo4(MAC, V4_A, MAC, V4_B, 1, 1))["text"],
                         "IPv4 169.254.77.10 -> 169.254.1.2 ICMP echo request")
        syn = pk.inspect(pk.tcp6(MAC, LL, MAC, LL, 40000, 22, 1, 0, pk.TCP_SYN))
        self.assertEqual((syn["sport"], syn["dport"], syn["tcp_flags"]), (40000, 22, pk.TCP_SYN))


if __name__ == "__main__":
    unittest.main()
