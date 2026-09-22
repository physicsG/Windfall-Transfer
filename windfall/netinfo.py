"""Network interface facts from the IP Helper API: interfaces (with traffic counters), addresses and neighbors."""

import ctypes
import ctypes.wintypes as wt
import socket
from dataclasses import dataclass

from .wintun import MIB_UNICASTIPADDRESS_ROW, SOCKADDR_INET
from .winusb import GUID

AF_INET = 2
IF_OPER_STATUS_UP = 1
MEDIA_CONNECT_STATE_CONNECTED = 1
NEIGHBOR_RESOLVED = {2, 3, 4, 5}  # probe, delay, stale, reachable

_iphlpapi = ctypes.WinDLL("iphlpapi", use_last_error=True)


class MIB_IF_ROW2(ctypes.Structure):
    _fields_ = [
        ("InterfaceLuid", ctypes.c_uint64), ("InterfaceIndex", ctypes.c_ulong), ("InterfaceGuid", GUID),
        ("Alias", ctypes.c_wchar * 257), ("Description", ctypes.c_wchar * 257),
        ("PhysicalAddressLength", ctypes.c_ulong), ("PhysicalAddress", ctypes.c_ubyte * 32),
        ("PermanentPhysicalAddress", ctypes.c_ubyte * 32), ("Mtu", ctypes.c_ulong), ("Type", ctypes.c_ulong),
        ("TunnelType", ctypes.c_int), ("MediaType", ctypes.c_int), ("PhysicalMediumType", ctypes.c_int),
        ("AccessType", ctypes.c_int), ("DirectionType", ctypes.c_int), ("InterfaceAndOperStatusFlags", ctypes.c_ubyte),
        ("OperStatus", ctypes.c_int), ("AdminStatus", ctypes.c_int), ("MediaConnectState", ctypes.c_int),
        ("NetworkGuid", GUID), ("ConnectionType", ctypes.c_int),
        ("TransmitLinkSpeed", ctypes.c_uint64), ("ReceiveLinkSpeed", ctypes.c_uint64),
        ("InOctets", ctypes.c_uint64), ("InUcastPkts", ctypes.c_uint64), ("InNUcastPkts", ctypes.c_uint64),
        ("InDiscards", ctypes.c_uint64), ("InErrors", ctypes.c_uint64), ("InUnknownProtos", ctypes.c_uint64),
        ("InUcastOctets", ctypes.c_uint64), ("InMulticastOctets", ctypes.c_uint64),
        ("InBroadcastOctets", ctypes.c_uint64), ("OutOctets", ctypes.c_uint64), ("OutUcastPkts", ctypes.c_uint64),
        ("OutNUcastPkts", ctypes.c_uint64), ("OutDiscards", ctypes.c_uint64), ("OutErrors", ctypes.c_uint64),
        ("OutUcastOctets", ctypes.c_uint64), ("OutMulticastOctets", ctypes.c_uint64),
        ("OutBroadcastOctets", ctypes.c_uint64), ("OutQLen", ctypes.c_uint64),
    ]


class MIB_IPNET_ROW2(ctypes.Structure):
    _fields_ = [("Address", SOCKADDR_INET), ("InterfaceIndex", ctypes.c_ulong), ("InterfaceLuid", ctypes.c_uint64),
                ("PhysicalAddress", ctypes.c_ubyte * 32), ("PhysicalAddressLength", ctypes.c_ulong),
                ("State", ctypes.c_int), ("Flags", ctypes.c_ubyte), ("ReachabilityTime", ctypes.c_ulong)]


assert ctypes.sizeof(MIB_IF_ROW2) == 1352 and ctypes.sizeof(MIB_IPNET_ROW2) == 88

_iphlpapi.GetIfTable2.restype = wt.DWORD
_iphlpapi.GetIfTable2.argtypes = [ctypes.POINTER(ctypes.c_void_p)]
_iphlpapi.GetUnicastIpAddressTable.restype = wt.DWORD
_iphlpapi.GetUnicastIpAddressTable.argtypes = [ctypes.c_ushort, ctypes.POINTER(ctypes.c_void_p)]
_iphlpapi.GetIpNetTable2.restype = wt.DWORD
_iphlpapi.GetIpNetTable2.argtypes = [ctypes.c_ushort, ctypes.POINTER(ctypes.c_void_p)]
_iphlpapi.FreeMibTable.restype = None
_iphlpapi.FreeMibTable.argtypes = [ctypes.c_void_p]


@dataclass
class Interface:
    index: int
    alias: str
    description: str
    connected: bool
    tx_speed: int  # bits per second
    rx_speed: int
    in_octets: int
    out_octets: int
    mtu: int


def _rows(getter, row_type, *args):
    """Call a GetXxxTable2-style function and return its rows (copied), freeing the table."""
    table = ctypes.c_void_p()
    err = getter(*args, ctypes.byref(table))
    if err:
        raise OSError(err, f"{getter.__name__}: {ctypes.FormatError(err).strip()} (error {err})")
    try:
        count = ctypes.c_ulong.from_address(table.value).value
        rows = (row_type * count).from_address(table.value + 8)  # NumEntries, padded to the rows' alignment
        return [row_type.from_buffer_copy(row) for row in rows]
    finally:
        _iphlpapi.FreeMibTable(table)


def _ipv4(sockaddr):
    return socket.inet_ntoa(bytes(sockaddr.data[2:6]))  # sockaddr_in: family, port, address


def interfaces():
    """Network interfaces, without the per-driver filter layers Windows also lists as interfaces."""
    return [Interface(index=row.InterfaceIndex, alias=row.Alias, description=row.Description,
                      connected=row.OperStatus == IF_OPER_STATUS_UP
                      and row.MediaConnectState == MEDIA_CONNECT_STATE_CONNECTED,
                      tx_speed=row.TransmitLinkSpeed, rx_speed=row.ReceiveLinkSpeed,
                      in_octets=row.InOctets, out_octets=row.OutOctets, mtu=row.Mtu)
            for row in _rows(_iphlpapi.GetIfTable2, MIB_IF_ROW2)
            if not row.InterfaceAndOperStatusFlags & 0x02]  # FilterInterface


def ipv4_addresses(index=None):
    """This PC's IPv4 addresses as [(ip, prefix_length, interface_index)], optionally for one interface."""
    return [(_ipv4(row.Address), row.OnLinkPrefixLength, row.InterfaceIndex)
            for row in _rows(_iphlpapi.GetUnicastIpAddressTable, MIB_UNICASTIPADDRESS_ROW, AF_INET)
            if index is None or row.InterfaceIndex == index]


def neighbors(index):
    """IPv4 neighbors this PC has resolved on one interface (unicast only), as a list of IPs."""
    found = []
    for row in _rows(_iphlpapi.GetIpNetTable2, MIB_IPNET_ROW2, AF_INET):
        mac = bytes(row.PhysicalAddress[:row.PhysicalAddressLength])
        if (row.InterfaceIndex == index and row.State in NEIGHBOR_RESOLVED and len(mac) == 6
                and any(mac) and not mac[0] & 1):
            found.append(_ipv4(row.Address))
    return found
