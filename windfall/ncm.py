"""Host side of USB CDC-NCM (Network Control Model): class requests and NTB framing."""

import struct
import threading
from dataclasses import dataclass

from .winusb import PIPE_BULK, PIPE_TRANSFER_TIMEOUT, SHORT_PACKET_TERMINATE

NTH16 = 0x484D434E  # "NCMH"
NDP16 = 0x304D434E  # "NCM0" (no CRC)
NDP16_CRC = 0x314D434E  # "NCM1"
NTH32 = 0x686D636E  # "ncmh"
NDP32 = 0x306D636E  # "ncm0"
NDP32_CRC = 0x316D636E  # "ncm1"

SET_ETHERNET_PACKET_FILTER = 0x43
GET_NTB_PARAMETERS = 0x80
GET_NET_ADDRESS = 0x81
GET_MAX_DATAGRAM_SIZE = 0x87

FILTER_ALL_MULTICAST = 0x02
FILTER_DIRECTED = 0x04
FILTER_BROADCAST = 0x08

CLASS_IN = 0xA1  # device-to-host | class | interface
CLASS_OUT = 0x21  # host-to-device | class | interface


@dataclass
class NtbParameters:
    formats: int = 0x01
    in_max: int = 2048
    in_divisor: int = 4
    in_remainder: int = 0
    in_alignment: int = 4
    out_max: int = 2048
    out_divisor: int = 4
    out_remainder: int = 0
    out_alignment: int = 4
    out_max_datagrams: int = 0

    @classmethod
    def parse(cls, data):
        (_, formats, in_max, in_div, in_rem, in_align, _, out_max, out_div, out_rem, out_align, out_max_dg) = (
            struct.unpack_from("<HHIHHHHIHHHH", data)
        )
        return cls(formats, in_max, in_div, in_rem, in_align, out_max, out_div, out_rem, out_align, out_max_dg)


def parse_ntb(buf):
    """Split one received NTB (16- or 32-bit) into Ethernet frames."""
    if len(buf) < 12:
        raise ValueError(f"NTB too short ({len(buf)} bytes)")
    sig = struct.unpack_from("<I", buf)[0]
    frames = []
    if sig == NTH16:
        _, _, block_len, ndp = struct.unpack_from("<HHHH", buf, 4)
        end = min(block_len or len(buf), len(buf))
        for _ in range(32):  # guards against NDP loops
            if not ndp:
                break
            if ndp + 8 > end:
                raise ValueError("NDP16 outside NTB")
            ndp_sig, ndp_len, next_ndp = struct.unpack_from("<IHH", buf, ndp)
            if ndp_sig not in (NDP16, NDP16_CRC):
                raise ValueError(f"bad NDP16 signature {ndp_sig:#x}")
            crc = 4 if ndp_sig == NDP16_CRC else 0
            for pos in range(ndp + 8, min(ndp + ndp_len, end) - 3, 4):
                index, length = struct.unpack_from("<HH", buf, pos)
                if not index or not length:
                    break
                if index + length > end:
                    raise ValueError("datagram outside NTB")
                frames.append(bytes(buf[index : index + length - crc]))
            ndp = next_ndp
    elif sig == NTH32:
        _, _, block_len, ndp = struct.unpack_from("<HHII", buf, 4)
        end = min(block_len or len(buf), len(buf))
        for _ in range(32):
            if not ndp:
                break
            if ndp + 16 > end:
                raise ValueError("NDP32 outside NTB")
            ndp_sig, ndp_len, _, next_ndp = struct.unpack_from("<IHHI", buf, ndp)
            if ndp_sig not in (NDP32, NDP32_CRC):
                raise ValueError(f"bad NDP32 signature {ndp_sig:#x}")
            crc = 4 if ndp_sig == NDP32_CRC else 0
            for pos in range(ndp + 16, min(ndp + ndp_len, end) - 7, 8):
                index, length = struct.unpack_from("<II", buf, pos)
                if not index or not length:
                    break
                if index + length > end:
                    raise ValueError("datagram outside NTB")
                frames.append(bytes(buf[index : index + length - crc]))
            ndp = next_ndp
    else:
        raise ValueError(f"bad NTB signature {sig:#x}")
    return frames


def build_ntb16(frames, sequence, params):
    """Pack Ethernet frames into one NTB-16, with the NDP right after the header."""
    ndp_align = max(params.out_alignment, 4)
    ndp_off = (12 + ndp_align - 1) // ndp_align * ndp_align
    ndp_len = 8 + 4 * (len(frames) + 1)
    divisor = max(params.out_divisor, 1)
    remainder = params.out_remainder % divisor
    offset = ndp_off + ndp_len
    entries = []
    for frame in frames:
        offset += (remainder - offset) % divisor
        entries.append((offset, len(frame)))
        offset += len(frame)
    ntb = bytearray(offset)
    struct.pack_into("<IHHHH", ntb, 0, NTH16, 12, sequence & 0xFFFF, offset, ndp_off)
    struct.pack_into("<IHH", ntb, ndp_off, NDP16, ndp_len, 0)
    for i, ((start, length), frame) in enumerate(zip(entries, frames)):
        struct.pack_into("<HH", ntb, ndp_off + 8 + 4 * i, start, length)
        ntb[start : start + length] = frame
    return bytes(ntb)


def find_functions(device):
    """(control, data) interface numbers of each CDC-NCM function on a WinUSB device."""
    pairs = []
    for number, itf in sorted(device.interfaces.items()):
        data = device.interfaces.get(number + 1)
        if itf.cls == 0x02 and itf.subclass == 0x0D and data and data.cls == 0x0A:
            pairs.append((number, number + 1))
    return pairs


class NcmFunction:
    """One CDC-NCM function: a control interface plus its data interface."""

    def __init__(self, device, control_number, data_number, name):
        self.name = name
        self.control = device.interfaces[control_number]
        self.data = device.interfaces[data_number]
        self.params = NtbParameters()
        self.host_mac = None
        self.max_datagram = None
        self.notes = []
        self.pipe_in = self.pipe_out = None
        self._rx = None
        self._sequence = 0
        self._tx_lock = threading.Lock()

    def _request_in(self, request, length):
        return self.control.control_in(CLASS_IN, request, 0, self.control.number, length)

    def start(self, read_timeout_ms=500, write_timeout_ms=0):
        """Configure the function and bring the link up. A timeout of 0 means wait indefinitely."""
        self.data.set_alt(0)  # resets the function
        try:
            self.params = NtbParameters.parse(self._request_in(GET_NTB_PARAMETERS, 28))
        except (OSError, struct.error) as e:
            self.notes.append(f"GET_NTB_PARAMETERS failed ({e}); using defaults")
        try:
            mac = self._request_in(GET_NET_ADDRESS, 6)
            if len(mac) == 6 and any(mac) and not mac[0] & 1:
                self.host_mac = mac
        except OSError as e:
            self.notes.append(f"GET_NET_ADDRESS failed ({e})")
        try:
            self.max_datagram = struct.unpack("<H", self._request_in(GET_MAX_DATAGRAM_SIZE, 2))[0]
        except (OSError, struct.error) as e:
            self.notes.append(f"GET_MAX_DATAGRAM_SIZE failed ({e})")
        try:
            self.control.control_out(
                CLASS_OUT,
                SET_ETHERNET_PACKET_FILTER,
                FILTER_DIRECTED | FILTER_BROADCAST | FILTER_ALL_MULTICAST,
                self.control.number,
            )
        except OSError as e:
            self.notes.append(f"SET_ETHERNET_PACKET_FILTER failed ({e})")

        self.data.set_alt(1)  # link up
        for pipe in self.data.pipes(1):
            if pipe.PipeType == PIPE_BULK:
                if pipe.PipeId & 0x80:
                    self.pipe_in = pipe.PipeId
                else:
                    self.pipe_out = pipe.PipeId
        if self.pipe_in is None or self.pipe_out is None:
            raise RuntimeError(f"function {self.name}: data interface has no bulk IN/OUT pipes")
        self.data.set_pipe_policy(self.pipe_out, SHORT_PACKET_TERMINATE, 1)
        if write_timeout_ms:
            self.data.set_pipe_policy(self.pipe_out, PIPE_TRANSFER_TIMEOUT, write_timeout_ms)
        if read_timeout_ms:
            self.data.set_pipe_policy(self.pipe_in, PIPE_TRANSFER_TIMEOUT, read_timeout_ms)
        self._rx = bytearray((max(self.params.in_max, 2048) + 511) // 512 * 512)

    def abort_receive(self):
        """Make a pending receive() return (with an error)."""
        if self.pipe_in is not None:
            self.data.abort(self.pipe_in)

    def receive(self):
        """Wait (up to the read timeout, if any) for one NTB; returns its Ethernet frames (maybe none)."""
        count = self.data.read_into(self.pipe_in, self._rx)
        if not count:
            return []
        return parse_ntb(memoryview(self._rx)[:count])

    def send(self, frames):
        """Send Ethernet frames, packing as many per NTB as the device allows."""
        p = self.params
        max_count = p.out_max_datagrams or 64
        max_size = min(p.out_max, 0xFFFF)
        overhead = 12 + max(p.out_alignment, 4) + 8 + 4  # NTH16, NDP alignment, NDP header, terminator
        batch, estimate = [], 0
        for frame in frames:
            cost = len(frame) + 4 + max(p.out_divisor, 1)  # payload, NDP entry, worst-case padding
            if batch and (len(batch) >= max_count or overhead + estimate + cost > max_size):
                self._send_ntb(batch)
                batch, estimate = [], 0
            batch.append(frame)
            estimate += cost
        if batch:
            self._send_ntb(batch)

    def _send_ntb(self, frames):
        with self._tx_lock:
            ntb = build_ntb16(frames, self._sequence, self.params)
            self._sequence += 1
            self.data.write(self.pipe_out, ntb)

    def stop(self):
        try:
            self.data.set_alt(0)
        except OSError:
            pass
