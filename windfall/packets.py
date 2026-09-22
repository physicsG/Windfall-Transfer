"""Just enough Ethernet/ARP/IPv4/IPv6/ICMP/UDP/TCP to probe a point-to-point link."""

import ipaddress
import struct
from typing import TypedDict

ETH_IPV4, ETH_ARP, ETH_IPV6 = 0x0800, 0x0806, 0x86DD
PROTO_ICMP, PROTO_TCP, PROTO_UDP, PROTO_ICMPV6 = 1, 6, 17, 58
BROADCAST_MAC = b"\xff" * 6
ALL_NODES = ipaddress.IPv6Address("ff02::1").packed

TCP_SYN, TCP_RST, TCP_ACK = 0x02, 0x04, 0x10
_TCP_FLAG_NAMES = [(0x02, "SYN"), (0x10, "ACK"), (0x04, "RST"), (0x01, "FIN"), (0x08, "PSH")]
_ICMP6_NAMES = {
    1: "dest unreachable",
    2: "packet too big",
    3: "time exceeded",
    128: "echo request",
    129: "echo reply",
    130: "MLD query",
    131: "MLD report",
    133: "router solicitation",
    134: "router advertisement",
    135: "neighbor solicitation",
    136: "neighbor advertisement",
    143: "MLDv2 report",
}
_UDP_NAMES = {
    53: "DNS",
    67: "DHCP",
    68: "DHCP",
    123: "NTP",
    137: "NetBIOS",
    138: "NetBIOS",
    546: "DHCPv6",
    547: "DHCPv6",
    1900: "SSDP",
    5353: "mDNS",
    5355: "LLMNR",
}
_DHCP_TYPES = {1: "DISCOVER", 2: "OFFER", 3: "REQUEST", 4: "DECLINE", 5: "ACK", 6: "NAK", 7: "RELEASE", 8: "INFORM"}


class FrameInfo(TypedDict, total=False):
    text: str
    type: str  # "arp", "ipv4", "ipv6" or "other"
    dst_mac: bytes
    src_mac: bytes
    arp_op: int
    arp_sha: bytes
    arp_spa: bytes
    arp_tpa: bytes
    proto: int
    src: bytes
    dst: bytes
    sport: int
    dport: int
    tcp_seq: int
    tcp_ack: int
    tcp_flags: int
    icmp_type: int
    icmp_body: bytes
    target: bytes
    dhcp: str


def mac_str(mac: bytes) -> str:
    return ":".join(f"{b:02x}" for b in mac)


def ip_str(raw: bytes | memoryview) -> str:
    return str(ipaddress.ip_address(bytes(raw)))


def checksum(data: bytes) -> int:
    if len(data) % 2:
        data += b"\0"
    total: int = sum(struct.unpack(f"!{len(data) // 2}H", data))
    while total >> 16:
        total = (total & 0xFFFF) + (total >> 16)
    return ~total & 0xFFFF


def ethernet(dst: bytes, src: bytes, ethertype: int, payload: bytes) -> bytes:
    return dst + src + struct.pack("!H", ethertype) + payload


# ---- IPv6 ----


def link_local(mac: bytes) -> bytes:
    """fe80::/64 address derived from a MAC (modified EUI-64)."""
    return b"\xfe\x80" + bytes(6) + bytes([mac[0] ^ 0x02]) + mac[1:3] + b"\xff\xfe" + mac[3:6]


def ipv6_multicast_mac(addr: bytes) -> bytes:
    return b"\x33\x33" + addr[12:16]


def ipv6(src: bytes, dst: bytes, next_header: int, payload: bytes, hop_limit: int = 64) -> bytes:
    return struct.pack("!IHBB", 0x60000000, len(payload), next_header, hop_limit) + src + dst + payload


def _pseudo6(src: bytes, dst: bytes, length: int, proto: int) -> bytes:
    return src + dst + struct.pack("!I3xB", length, proto)


def icmpv6(src: bytes, dst: bytes, icmp_type: int, code: int, body: bytes) -> bytes:
    csum = checksum(_pseudo6(src, dst, 4 + len(body), PROTO_ICMPV6) + struct.pack("!BBH", icmp_type, code, 0) + body)
    return struct.pack("!BBH", icmp_type, code, csum) + body


def neighbor_advert(our_mac: bytes, our_ip: bytes, dst_mac: bytes, dst_ip: bytes, solicited: bool) -> bytes:
    flags = 0x20000000 | (0x40000000 if solicited else 0)  # Override (+ Solicited)
    body = struct.pack("!I", flags) + our_ip + b"\x02\x01" + our_mac  # target link-layer address option
    packet = ipv6(our_ip, dst_ip, PROTO_ICMPV6, icmpv6(our_ip, dst_ip, 136, 0, body), hop_limit=255)
    return ethernet(dst_mac, our_mac, ETH_IPV6, packet)


def echo6(
    our_mac: bytes,
    our_ip: bytes,
    dst_mac: bytes,
    dst_ip: bytes,
    ident: int,
    seq: int,
    reply: bool = False,
    data: bytes = b"windfall-transfer probe",
) -> bytes:
    body = struct.pack("!HH", ident, seq) + data
    packet = ipv6(our_ip, dst_ip, PROTO_ICMPV6, icmpv6(our_ip, dst_ip, 129 if reply else 128, 0, body))
    return ethernet(dst_mac, our_mac, ETH_IPV6, packet)


def tcp6(
    our_mac: bytes, our_ip: bytes, dst_mac: bytes, dst_ip: bytes, sport: int, dport: int, seq: int, ack: int, flags: int
) -> bytes:
    options = struct.pack("!BBH", 2, 4, 1440) if flags & TCP_SYN else b""  # MSS
    offset = (20 + len(options)) // 4
    header = struct.pack("!HHIIBBHHH", sport, dport, seq, ack, offset << 4, flags, 65535, 0, 0) + options
    csum = checksum(_pseudo6(our_ip, dst_ip, len(header), PROTO_TCP) + header)
    header = header[:16] + struct.pack("!H", csum) + header[18:]
    return ethernet(dst_mac, our_mac, ETH_IPV6, ipv6(our_ip, dst_ip, PROTO_TCP, header))


# ---- IPv4 ----


def arp(
    op: int, our_mac: bytes, our_ip: bytes, target_mac: bytes, target_ip: bytes, dst_mac: bytes = BROADCAST_MAC
) -> bytes:
    body = struct.pack("!HHBBH", 1, ETH_IPV4, 6, 4, op) + our_mac + our_ip + target_mac + target_ip
    return ethernet(dst_mac, our_mac, ETH_ARP, body)


def ipv4(src: bytes, dst: bytes, proto: int, payload: bytes, ttl: int = 64) -> bytes:
    header = struct.pack("!BBHHHBBH4s4s", 0x45, 0, 20 + len(payload), 0, 0x4000, ttl, proto, 0, src, dst)
    header = header[:10] + struct.pack("!H", checksum(header)) + header[12:]
    return header + payload


def udp4(src: bytes, dst: bytes, sport: int, dport: int, payload: bytes) -> bytes:
    length = 8 + len(payload)
    csum = checksum(
        src + dst + struct.pack("!BBH", 0, PROTO_UDP, length) + struct.pack("!HHHH", sport, dport, length, 0) + payload
    )
    return struct.pack("!HHHH", sport, dport, length, csum or 0xFFFF) + payload


def echo4(
    our_mac: bytes,
    our_ip: bytes,
    dst_mac: bytes,
    dst_ip: bytes,
    ident: int,
    seq: int,
    reply: bool = False,
    data: bytes = b"windfall-transfer probe",
) -> bytes:
    body = struct.pack("!BBHHH", 0 if reply else 8, 0, 0, ident, seq) + data
    body = body[:2] + struct.pack("!H", checksum(body)) + body[4:]
    return ethernet(dst_mac, our_mac, ETH_IPV4, ipv4(our_ip, dst_ip, PROTO_ICMP, body))


# ---- parsing ----


def _dhcp_type(bootp: memoryview) -> str | None:
    if bytes(bootp[236:240]) != b"\x63\x82\x53\x63":
        return None
    i = 240
    while i + 1 < len(bootp) and bootp[i] != 255:
        if bootp[i] == 0:
            i += 1
            continue
        code, length = bootp[i], bootp[i + 1]
        if code == 53 and length >= 1 and i + 2 < len(bootp):
            return _DHCP_TYPES.get(bootp[i + 2], str(bootp[i + 2]))
        i += 2 + length
    return None


def _transport(info: FrameInfo, proto: int, seg: memoryview) -> str:
    """Fill in UDP/TCP/ICMP fields; returns a short description."""
    if proto == PROTO_UDP and len(seg) >= 8:
        sport, dport = struct.unpack_from("!HH", seg)
        info.update({"sport": sport, "dport": dport})
        name = _UDP_NAMES.get(dport) or _UDP_NAMES.get(sport) or ""
        if dport in (67, 68) and len(seg) >= 8 + 240:
            dhcp = _dhcp_type(seg[8:])
            if dhcp:
                info["dhcp"] = dhcp
                name = f"DHCP {dhcp}"
        return f"UDP {sport}->{dport} {name}".rstrip()
    if proto == PROTO_TCP and len(seg) >= 20:
        sport, dport, seq, ack, _, flags = struct.unpack_from("!HHIIBB", seg)
        info.update({"sport": sport, "dport": dport, "tcp_seq": seq, "tcp_ack": ack, "tcp_flags": flags})
        return f"TCP {sport}->{dport} [{','.join(n for bit, n in _TCP_FLAG_NAMES if flags & bit)}]"
    if proto in (PROTO_ICMP, PROTO_ICMPV6) and len(seg) >= 4:
        info.update({"icmp_type": seg[0], "icmp_body": bytes(seg[4:])})
        if proto == PROTO_ICMPV6:
            name = _ICMP6_NAMES.get(seg[0], f"type {seg[0]}")
            if seg[0] in (135, 136) and len(seg) >= 24:
                info["target"] = bytes(seg[8:24])
                name += f" for {ip_str(seg[8:24])}"
            return f"ICMPv6 {name}"
        return "ICMP " + {0: "echo reply", 8: "echo request", 3: "dest unreachable"}.get(seg[0], f"type {seg[0]}")
    return f"protocol {proto}"


def inspect(frame: bytes) -> FrameInfo:
    """Parse an Ethernet frame into the fields the probe cares about, plus a one-line 'text'."""
    info: FrameInfo = {"dst_mac": bytes(frame[0:6]), "src_mac": bytes(frame[6:12]), "type": "other"}
    if len(frame) < 14:
        info["text"] = f"runt frame ({len(frame)} bytes)"
        return info
    ethertype = struct.unpack_from("!H", frame, 12)[0]
    p = memoryview(frame)[14:]
    if ethertype == ETH_ARP and len(p) >= 28:
        op = struct.unpack_from("!H", p, 6)[0]
        sha, spa, tpa = bytes(p[8:14]), bytes(p[14:18]), bytes(p[24:28])
        info.update({"type": "arp", "arp_op": op, "arp_sha": sha, "arp_spa": spa, "arp_tpa": tpa})
        if op != 1:
            info["text"] = f"ARP {ip_str(spa)} is-at {mac_str(sha)}"
        elif spa == bytes(4):
            info["text"] = f"ARP probe for {ip_str(tpa)}"
        elif spa == tpa:
            info["text"] = f"ARP announce {ip_str(spa)}"
        else:
            info["text"] = f"ARP who-has {ip_str(tpa)} tell {ip_str(spa)}"
    elif ethertype == ETH_IPV4 and len(p) >= 20:
        ihl = (p[0] & 0x0F) * 4
        proto, src, dst = p[9], bytes(p[12:16]), bytes(p[16:20])
        info.update({"type": "ipv4", "proto": proto, "src": src, "dst": dst})
        info["text"] = f"IPv4 {ip_str(src)} -> {ip_str(dst)} " + _transport(info, proto, p[ihl:])
    elif ethertype == ETH_IPV6 and len(p) >= 40:
        nh, src, dst = p[6], bytes(p[8:24]), bytes(p[24:40])
        off = 40
        while nh in (0, 43, 60) and off + 8 <= len(p):  # hop-by-hop, routing, destination options
            nh, off = p[off], off + (p[off + 1] + 1) * 8
        if nh == 44 and off + 8 <= len(p):  # fragment header
            nh, off = p[off], off + 8
        info.update({"type": "ipv6", "proto": nh, "src": src, "dst": dst})
        info["text"] = f"IPv6 {ip_str(src)} -> {ip_str(dst)} " + _transport(info, nh, p[off:])
    else:
        info["text"] = f"ethertype 0x{ethertype:04x} ({len(frame)} bytes)"
    return info
