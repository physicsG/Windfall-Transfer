"""Just enough multicast DNS (Bonjour) to find the Mac on a link. The query goes out from an ordinary port, which makes
it a "legacy unicast" query answered straight back to that port (RFC 6762, 6.7): nothing listens on 5353, so Windows'
own mDNS service is left alone."""

import random
import socket
import struct
import time

GROUP, PORT = "224.0.0.251", 5353
TYPE_A, TYPE_PTR, TYPE_SRV = 1, 12, 33
# Service types a Mac usually advertises: any answer reveals its address on the link.
QUESTIONS = ("_services._dns-sd._udp.local", "_smb._tcp.local", "_device-info._tcp.local")


def encode_name(name):
    out = b""
    for label in name.rstrip(".").split("."):
        raw = label.encode("utf-8")
        out += bytes([len(raw)]) + raw
    return out + b"\0"


def build_query(names, query_id):
    header = struct.pack("!HHHHHH", query_id, 0, len(names), 0, 0, 0)
    return header + b"".join(encode_name(name) + struct.pack("!HH", TYPE_PTR, 1) for name in names)


def read_name(msg, offset):
    """Decode a (possibly compressed) DNS name; returns (name, offset just past it)."""
    labels, end, jumps = [], None, 0
    while True:
        if offset >= len(msg):
            raise ValueError("name runs past the end of the message")
        length = msg[offset]
        if length & 0xC0 == 0xC0:
            if offset + 1 >= len(msg) or jumps > 32:
                raise ValueError("bad name pointer")
            if end is None:
                end = offset + 2
            offset = ((length & 0x3F) << 8) | msg[offset + 1]
            jumps += 1
            continue
        offset += 1
        if length == 0:
            return ".".join(labels), (offset if end is None else end)
        labels.append(msg[offset : offset + length].decode("utf-8", "replace"))
        offset += length


def parse_response(msg):
    """Records of a DNS response as [(name, type, data)]: PTR and SRV data are names, A data is an IP."""
    if len(msg) < 12:
        raise ValueError("message too short")
    _, flags, questions, answers, authorities, additionals = struct.unpack_from("!HHHHHH", msg)
    if not flags & 0x8000:
        return []  # a query, not a response
    offset = 12
    for _ in range(questions):
        offset = read_name(msg, offset)[1] + 4
    records = []
    for _ in range(answers + authorities + additionals):
        name, offset = read_name(msg, offset)
        rtype, _, _, length = struct.unpack_from("!HHIH", msg, offset)
        start = offset + 10
        offset = start + length
        if offset > len(msg):
            raise ValueError("record runs past the end of the message")
        if rtype == TYPE_PTR:
            data = read_name(msg, start)[0]
        elif rtype == TYPE_SRV and length > 6:
            data = read_name(msg, start + 6)[0]
        elif rtype == TYPE_A and length == 4:
            data = socket.inet_ntoa(msg[start : start + 4])
        else:
            data = None
        records.append((name, rtype, data))
    return records


def friendly_name(records):
    """The responder's name: its file-sharing name ("Alex's MacBook Pro") or else its host name."""
    for _, rtype, data in records:
        if rtype == TYPE_PTR and data and data.endswith("._smb._tcp.local"):
            return data[: -len("._smb._tcp.local")]
    for name, rtype, data in records:
        host = data if rtype == TYPE_SRV else name if rtype == TYPE_A else None
        if host and host.endswith(".local"):
            return host[: -len(".local")]
    return None


def discover(local_ip, timeout=1.5, exclude=()):
    """Ask the link that local_ip is on who is there. Returns {ip: name or None} for everyone who answered."""
    found = {}
    ignore = set(exclude) | {local_ip}
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP) as sock:
        sock.bind((local_ip, 0))
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(local_ip))
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 255)
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_LOOP, 0)
        sock.sendto(build_query(QUESTIONS, random.randrange(1, 0x10000)), (GROUP, PORT))
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            sock.settimeout(remaining)
            try:
                msg, (ip, _) = sock.recvfrom(9000)
            except socket.timeout:
                break
            except ConnectionResetError:  # Windows reports ICMP "port unreachable" this way; keep listening
                continue
            if ip in ignore:
                continue
            try:
                name = friendly_name(parse_response(msg))
            except (ValueError, struct.error, IndexError):
                continue
            if found.get(ip) is None:
                found[ip] = name
    return found
