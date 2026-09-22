import socket
import struct
import unittest

from windfall import mdns

INSTANCE = "Alex's MacBook Pro"


def smb_response():
    """A compressed mDNS answer: PTR to the SMB instance, SRV to the host, A record for the host."""
    msg = struct.pack("!HHHHHH", 0x1234, 0x8400, 0, 1, 0, 2)
    service_at = len(msg)
    msg += mdns.encode_name("_smb._tcp.local")
    ptr_rdata = bytes([len(INSTANCE)]) + INSTANCE.encode() + struct.pack("!H", 0xC000 | service_at)
    msg += struct.pack("!HHIH", mdns.TYPE_PTR, 1, 10, len(ptr_rdata))
    instance_at = len(msg)
    msg += ptr_rdata
    srv_rdata = struct.pack("!HHH", 0, 0, 445)
    host_at = len(msg) + 2 + 10 + len(srv_rdata)
    srv_rdata += mdns.encode_name("Alexs-MacBook-Pro.local")
    msg += struct.pack("!H", 0xC000 | instance_at) + struct.pack("!HHIH", mdns.TYPE_SRV, 0x8001, 10, len(srv_rdata))
    msg += srv_rdata
    msg += struct.pack("!H", 0xC000 | host_at) + struct.pack("!HHIH", mdns.TYPE_A, 0x8001, 10, 4)
    msg += socket.inet_aton("169.254.21.56")
    return msg


class MdnsTests(unittest.TestCase):
    def test_query_asks_the_questions(self):
        query = mdns.build_query(mdns.QUESTIONS, 77)
        query_id, flags, questions = struct.unpack_from("!HHH", query)
        self.assertEqual((query_id, flags, questions), (77, 0, len(mdns.QUESTIONS)))
        offset, names = 12, []
        for _ in range(questions):
            name, offset = mdns.read_name(query, offset)
            self.assertEqual(struct.unpack_from("!HH", query, offset), (mdns.TYPE_PTR, 1))
            names.append(name)
            offset += 4
        self.assertEqual(tuple(names), mdns.QUESTIONS)

    def test_parses_compressed_records(self):
        self.assertEqual(
            mdns.parse_response(smb_response()),
            [
                ("_smb._tcp.local", mdns.TYPE_PTR, f"{INSTANCE}._smb._tcp.local"),
                (f"{INSTANCE}._smb._tcp.local", mdns.TYPE_SRV, "Alexs-MacBook-Pro.local"),
                ("Alexs-MacBook-Pro.local", mdns.TYPE_A, "169.254.21.56"),
            ],
        )

    def test_friendly_name_prefers_the_file_sharing_name(self):
        records = mdns.parse_response(smb_response())
        self.assertEqual(mdns.friendly_name(records), INSTANCE)
        self.assertEqual(mdns.friendly_name(records[1:]), "Alexs-MacBook-Pro")
        self.assertIsNone(mdns.friendly_name([]))

    def test_ignores_queries_and_rejects_garbage(self):
        self.assertEqual(mdns.parse_response(mdns.build_query(["_smb._tcp.local"], 1)), [])
        loop = struct.pack("!HHHHHH", 0, 0x8400, 0, 1, 0, 0) + b"\xc0\x0c"  # name pointing at itself
        with self.assertRaises(ValueError):
            mdns.parse_response(loop)
        with self.assertRaises(ValueError):
            mdns.parse_response(b"\x00" * 5)


if __name__ == "__main__":
    unittest.main()
