"""A one-client DHCPv4 server: hands the Mac a fixed address on the cable link (no router, no DNS)."""

import struct

MAGIC = b"\x63\x82\x53\x63"
DISCOVER, OFFER, REQUEST, DECLINE, ACK, NAK, RELEASE, INFORM = range(1, 9)
_NAMES = {OFFER: "OFFER", ACK: "ACK", NAK: "NAK"}


def parse_options(data):
    options, i = {}, 0
    while i < len(data):
        code = data[i]
        if code == 255:
            break
        if code == 0:
            i += 1
            continue
        if i + 1 >= len(data):
            break
        length = data[i + 1]
        options[code] = bytes(data[i + 2 : i + 2 + length])
        i += 2 + length
    return options


class DhcpServer:
    def __init__(self, server_ip, client_ip, netmask, lease_seconds=86400):
        self.server_ip = bytes(server_ip)
        self.client_ip = bytes(client_ip)
        self.netmask = bytes(netmask)
        self.lease = lease_seconds

    def handle(self, bootp):
        """Answer one client message. Returns (reply BOOTP payload, 'OFFER'|'ACK'|'NAK'), or None to stay silent."""
        if len(bootp) < 240 or bootp[0] != 1 or bytes(bootp[236:240]) != MAGIC:
            return None
        options = parse_options(bootp[240:])
        kind = options.get(53, b"\0")[0]
        if kind == DISCOVER:
            return self._reply(bootp, OFFER), _NAMES[OFFER]
        if kind == REQUEST:
            server_id = options.get(54)
            if server_id is not None and server_id != self.server_ip:
                return None  # the client chose a different server
            requested = options.get(50) or bytes(bootp[12:16])  # option 50, or ciaddr when renewing
            reply = ACK if requested == self.client_ip else NAK
            return self._reply(bootp, reply), _NAMES[reply]
        return None

    def _reply(self, request, kind):
        xid, flags, chaddr = bytes(request[4:8]), bytes(request[10:12]), bytes(request[28:44])
        nak = kind == NAK
        header = struct.pack(
            "!BBBB4sH2s4s4s4s4s16s64s128s",
            2,
            1,
            6,
            0,
            xid,
            0,
            flags,
            bytes(4),
            bytes(4) if nak else self.client_ip,
            bytes(4) if nak else self.server_ip,
            bytes(4),
            chaddr,
            b"",
            b"",
        )
        options = MAGIC + bytes([53, 1, kind, 54, 4]) + self.server_ip
        if not nak:
            options += bytes([51, 4]) + struct.pack("!I", self.lease)
            options += bytes([58, 4]) + struct.pack("!I", self.lease // 2)
            options += bytes([59, 4]) + struct.pack("!I", self.lease * 7 // 8)
            options += bytes([1, 4]) + self.netmask
        reply = header + options + b"\xff"
        return reply + bytes(max(0, 300 - len(reply)))  # some clients expect at least 300 bytes
