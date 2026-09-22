import struct
import unittest

from windfall.ncm import NDP32, NTH32, NtbParameters, build_ntb16, parse_ntb

FRAMES = [bytes(range(60)), b"x" * 1514, b"y" * 101, b"z" * 16014]


class NtbTests(unittest.TestCase):
    def test_ntb16_round_trip_respects_alignment(self):
        for params in (NtbParameters(out_max=32764, out_divisor=4, out_remainder=2, out_alignment=4),
                       NtbParameters(out_max=32764, out_divisor=1, out_remainder=0, out_alignment=8)):
            ntb = build_ntb16(FRAMES, 7, params)
            self.assertEqual(parse_ntb(ntb), FRAMES)
            ndp = struct.unpack_from("<H", ntb, 10)[0]
            self.assertEqual(ndp % max(params.out_alignment, 4), 0)
            for i in range(len(FRAMES)):
                start = struct.unpack_from("<H", ntb, ndp + 8 + 4 * i)[0]
                self.assertEqual(start % params.out_divisor, params.out_remainder % params.out_divisor)

    def test_parses_ntb32(self):
        frames = [b"a" * 64, b"b" * 1500]
        ndp_len = 16 + 8 * (len(frames) + 1)
        offset, entries, body = 16 + ndp_len, [], b""
        for frame in frames:
            entries.append((offset, len(frame)))
            body += frame
            offset += len(frame)
        ntb = struct.pack("<IHHII", NTH32, 16, 0, offset, 16)
        ntb += struct.pack("<IHHII", NDP32, ndp_len, 0, 0, 0)
        ntb += b"".join(struct.pack("<II", *entry) for entry in entries) + bytes(8) + body
        self.assertEqual(parse_ntb(ntb), frames)

    def test_rejects_garbage(self):
        with self.assertRaises(ValueError):
            parse_ntb(b"not an NTB at all!")


if __name__ == "__main__":
    unittest.main()
