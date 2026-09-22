import struct
import unittest

from windfall import packets as pk
from windfall.service import Bridge

WIN, MAC, MASK = bytes([10, 77, 0, 1]), bytes([10, 77, 0, 2]), bytes([255, 255, 255, 0])
MAC_HW = bytes.fromhex("02000000000a")
HOST_HW = bytes.fromhex("02000000000b")


class FakeFunction:
    host_mac = HOST_HW
    max_datagram = 1514

    def __init__(self):
        self.sent = []

    def send(self, frames):
        self.sent.extend(frames)


class FakeSession:
    def __init__(self):
        self.got = []

    def send(self, packet):
        self.got.append(bytes(packet))
        return True


def dhcp_frame(options):
    bootp = struct.pack(
        "!BBBB4sH2s4s4s4s4s16s64s128s",
        1,
        1,
        6,
        0,
        b"\x12\x34\x56\x78",
        0,
        b"\x80\x00",
        bytes(4),
        bytes(4),
        bytes(4),
        bytes(4),
        MAC_HW + bytes(10),
        b"",
        b"",
    )
    bootp += b"\x63\x82\x53\x63" + options + b"\xff"
    everyone = b"\xff" * 4
    return pk.ethernet(
        pk.BROADCAST_MAC,
        MAC_HW,
        pk.ETH_IPV4,
        pk.ipv4(bytes(4), everyone, pk.PROTO_UDP, pk.udp4(bytes(4), everyone, 68, 67, bootp)),
    )


def neighbor_solicitation(src, target):
    body = bytes(4) + target + b"\x01\x01" + MAC_HW
    dst = b"\xff\x02" + bytes(9) + b"\x01\xff" + target[13:]
    return pk.ethernet(
        pk.ipv6_multicast_mac(dst),
        MAC_HW,
        pk.ETH_IPV6,
        pk.ipv6(src, dst, pk.PROTO_ICMPV6, pk.icmpv6(src, dst, 135, 0, body), hop_limit=255),
    )


class BridgeTests(unittest.TestCase):
    def setUp(self):
        self.fn, self.session = FakeFunction(), FakeSession()
        self.bridge = Bridge(self.fn, self.session, WIN, MAC, MASK)

    def test_dhcp_offers_then_acks_the_mac_address(self):
        self.bridge.from_mac(dhcp_frame(bytes([53, 1, 1])))
        offer = self.fn.sent[-1]
        self.assertEqual(pk.inspect(offer).get("dhcp"), "OFFER")
        self.assertEqual(offer[14 + 20 + 8 + 16 : 14 + 20 + 8 + 20], MAC)  # yiaddr
        self.assertEqual(pk.checksum(offer[14:34]), 0)
        self.assertFalse(self.bridge.mac_configured.is_set())
        self.bridge.from_mac(dhcp_frame(bytes([53, 1, 3, 50, 4]) + MAC + bytes([54, 4]) + WIN))
        self.assertEqual(pk.inspect(self.fn.sent[-1]).get("dhcp"), "ACK")
        self.assertTrue(self.bridge.mac_configured.is_set())
        self.assertEqual(self.session.got, [], "DHCP must not reach Windows")

    def test_dhcp_naks_other_addresses(self):
        self.bridge.from_mac(dhcp_frame(bytes([53, 1, 3, 50, 4]) + bytes([169, 254, 21, 56])))
        self.assertEqual(pk.inspect(self.fn.sent[-1]).get("dhcp"), "NAK")
        self.assertFalse(self.bridge.mac_configured.is_set())

    def test_arp_answers_only_for_windows(self):
        self.bridge.from_mac(pk.arp(1, MAC_HW, MAC, bytes(6), WIN))
        self.assertEqual(pk.inspect(self.fn.sent[-1])["text"], "ARP 10.77.0.1 is-at 02:00:00:00:00:0b")
        self.bridge.from_mac(pk.arp(1, MAC_HW, bytes(4), bytes(6), MAC))  # probe
        self.bridge.from_mac(pk.arp(1, MAC_HW, MAC, bytes(6), bytes([10, 77, 0, 9])))  # someone else
        self.assertEqual(len(self.fn.sent), 1)

    def test_ipv4_padding_is_stripped(self):
        echo = pk.echo4(MAC_HW, MAC, HOST_HW, WIN, 1, 1, data=b"")
        self.bridge.from_mac(echo + bytes(60 - len(echo)))
        self.assertEqual(self.session.got[-1], echo[14:])

    def test_neighbor_solicitations(self):
        mac_ll, win_ll = pk.link_local(MAC_HW), pk.link_local(bytes.fromhex("020000000099"))
        self.bridge.from_mac(neighbor_solicitation(mac_ll, win_ll))
        self.assertTrue(
            pk.inspect(self.fn.sent[-1])["text"].endswith(f"neighbor advertisement for {pk.ip_str(win_ll)}")
        )
        self.bridge.from_mac(neighbor_solicitation(mac_ll, mac_ll))  # the Mac's own address
        self.bridge.from_mac(neighbor_solicitation(bytes(16), win_ll))  # duplicate address detection
        self.assertEqual(len(self.fn.sent), 1)

    def test_windows_packets_get_the_right_ethernet_destination(self):
        self.bridge.peer_mac = MAC_HW
        b = self.bridge
        self.assertEqual(b.to_ethernet(pk.ipv4(WIN, MAC, 17, bytes(8)))[:6], MAC_HW)
        self.assertEqual(b.to_ethernet(pk.ipv4(WIN, bytes([10, 77, 0, 255]), 17, bytes(8)))[:6], pk.BROADCAST_MAC)
        self.assertEqual(
            b.to_ethernet(pk.ipv4(WIN, bytes([224, 0, 0, 251]), 17, bytes(8)))[:6], bytes.fromhex("01005e0000fb")
        )
        mdns6 = bytes.fromhex("ff0200000000000000000000000000fb")
        self.assertEqual(
            b.to_ethernet(pk.ipv6(pk.link_local(HOST_HW), mdns6, 17, bytes(8)))[:6], bytes.fromhex("3333000000fb")
        )
        with self.assertLogs("bridge", "WARNING"):
            self.assertIsNone(b.to_ethernet(pk.ipv4(WIN, MAC, 17, bytes(2000))))

    def test_unicast_waits_until_the_mac_is_known(self):
        self.assertIsNone(self.bridge.to_ethernet(pk.ipv4(WIN, MAC, 17, bytes(8))))


if __name__ == "__main__":
    unittest.main()
