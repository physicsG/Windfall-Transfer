"""ctypes bindings for wintun.dll (https://www.wintun.net) plus the IP Helper calls that configure the adapter."""

import ctypes
import ctypes.wintypes as wt
import os

from .winusb import GUID

ERROR_HANDLE_EOF = 38
ERROR_BUFFER_OVERFLOW = 111
ERROR_NO_MORE_ITEMS = 259
ERROR_OBJECT_ALREADY_EXISTS = 5010
WAIT_OBJECT_0 = 0

_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
_kernel32.WaitForSingleObject.restype = wt.DWORD
_kernel32.WaitForSingleObject.argtypes = [wt.HANDLE, wt.DWORD]

_iphlpapi = ctypes.WinDLL("iphlpapi", use_last_error=True)

_LOGGER = ctypes.WINFUNCTYPE(None, ctypes.c_int, ctypes.c_uint64, ctypes.c_wchar_p)


def _error(what, err=None):
    err = ctypes.get_last_error() if err is None else err
    return OSError(err, f"{what}: {ctypes.FormatError(err).strip()} (error {err})")


class Wintun:
    """The loaded wintun.dll."""

    def __init__(self, dll_path, log=None):
        if not os.path.exists(dll_path):
            raise FileNotFoundError(f"wintun.dll not found at {dll_path}")
        d = self._dll = ctypes.WinDLL(dll_path, use_last_error=True)
        for name, restype, argtypes in [
            ("WintunCreateAdapter", ctypes.c_void_p, [wt.LPCWSTR, wt.LPCWSTR, ctypes.POINTER(GUID)]),
            ("WintunCloseAdapter", None, [ctypes.c_void_p]),
            ("WintunDeleteDriver", wt.BOOL, []),
            ("WintunGetAdapterLUID", None, [ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint64)]),
            ("WintunGetRunningDriverVersion", wt.DWORD, []),
            ("WintunSetLogger", None, [_LOGGER]),
            ("WintunStartSession", ctypes.c_void_p, [ctypes.c_void_p, wt.DWORD]),
            ("WintunEndSession", None, [ctypes.c_void_p]),
            ("WintunGetReadWaitEvent", wt.HANDLE, [ctypes.c_void_p]),
            ("WintunReceivePacket", ctypes.c_void_p, [ctypes.c_void_p, ctypes.POINTER(wt.DWORD)]),
            ("WintunReleaseReceivePacket", None, [ctypes.c_void_p, ctypes.c_void_p]),
            ("WintunAllocateSendPacket", ctypes.c_void_p, [ctypes.c_void_p, wt.DWORD]),
            ("WintunSendPacket", None, [ctypes.c_void_p, ctypes.c_void_p]),
        ]:
            fn = getattr(d, name)
            fn.restype = restype
            fn.argtypes = argtypes
        self._logger = None
        if log:  # log(level, message); level 0 = info, 1 = warning, 2 = error
            self._logger = _LOGGER(lambda level, timestamp, message: log(level, message))  # keep a reference
            d.WintunSetLogger(self._logger)

    def driver_version(self):
        version = self._dll.WintunGetRunningDriverVersion()
        return f"{version >> 16}.{version & 0xFFFF}" if version else None

    def delete_driver(self):
        """Remove the Wintun driver from Windows, unless another app's adapter (Tailscale, WireGuard...) uses it."""
        return bool(self._dll.WintunDeleteDriver())

    def create_adapter(self, name, tunnel_type, guid_text):
        guid = GUID.parse(guid_text)
        handle = self._dll.WintunCreateAdapter(name, tunnel_type, ctypes.byref(guid))
        if not handle:
            raise _error("WintunCreateAdapter")
        return Adapter(self._dll, handle, name)


class Adapter:
    def __init__(self, dll, handle, name):
        self._dll = dll
        self.handle = handle
        self.name = name
        luid = ctypes.c_uint64()
        dll.WintunGetAdapterLUID(handle, ctypes.byref(luid))
        self.luid = luid.value

    def start_session(self, capacity=0x400000):
        handle = self._dll.WintunStartSession(self.handle, capacity)
        if not handle:
            raise _error("WintunStartSession")
        return Session(self._dll, handle)

    def close(self):
        """Removes the adapter (it was created by this process)."""
        if self.handle:
            self._dll.WintunCloseAdapter(self.handle)
            self.handle = None


class Session:
    """Packet rings between this process and the Windows network stack."""

    def __init__(self, dll, handle):
        self._dll = dll
        self.handle = handle
        self.read_event = dll.WintunGetReadWaitEvent(handle)
        self._size = wt.DWORD()

    def receive(self):
        """Next IP packet Windows wants to send, or None if there is none. EOFError when the adapter goes away."""
        ptr = self._dll.WintunReceivePacket(self.handle, ctypes.byref(self._size))
        if not ptr:
            err = ctypes.get_last_error()
            if err == ERROR_NO_MORE_ITEMS:
                return None
            if err == ERROR_HANDLE_EOF:
                raise EOFError("the Wintun adapter is shutting down")
            raise _error("WintunReceivePacket", err)
        try:
            return ctypes.string_at(ptr, self._size.value)
        finally:
            self._dll.WintunReleaseReceivePacket(self.handle, ptr)

    def wait(self, timeout_ms):
        """Wait until Windows has packets for us (or the timeout passes)."""
        return _kernel32.WaitForSingleObject(self.read_event, timeout_ms) == WAIT_OBJECT_0

    def send(self, packet):
        """Hand an IP packet to Windows. Returns False when the ring is full and the packet was dropped."""
        ptr = self._dll.WintunAllocateSendPacket(self.handle, len(packet))
        if not ptr:
            err = ctypes.get_last_error()
            if err == ERROR_BUFFER_OVERFLOW:
                return False
            if err == ERROR_HANDLE_EOF:
                raise EOFError("the Wintun adapter is shutting down")
            raise _error("WintunAllocateSendPacket", err)
        ctypes.memmove(ptr, packet, len(packet))
        self._dll.WintunSendPacket(self.handle, ptr)
        return True

    def close(self):
        if self.handle:
            self._dll.WintunEndSession(self.handle)
            self.handle = None


# ---- IP Helper: address and index of the adapter ----


class SOCKADDR_INET(ctypes.Structure):
    _fields_ = [("si_family", ctypes.c_ushort), ("data", ctypes.c_ubyte * 26)]


class MIB_UNICASTIPADDRESS_ROW(ctypes.Structure):
    _fields_ = [
        ("Address", SOCKADDR_INET),
        ("InterfaceLuid", ctypes.c_uint64),
        ("InterfaceIndex", ctypes.c_ulong),
        ("PrefixOrigin", ctypes.c_int),
        ("SuffixOrigin", ctypes.c_int),
        ("ValidLifetime", ctypes.c_ulong),
        ("PreferredLifetime", ctypes.c_ulong),
        ("OnLinkPrefixLength", ctypes.c_ubyte),
        ("SkipAsSource", ctypes.c_ubyte),
        ("DadState", ctypes.c_int),
        ("ScopeId", ctypes.c_ulong),
        ("CreationTimeStamp", ctypes.c_int64),
    ]


assert ctypes.sizeof(MIB_UNICASTIPADDRESS_ROW) == 80

_iphlpapi.InitializeUnicastIpAddressEntry.restype = None
_iphlpapi.InitializeUnicastIpAddressEntry.argtypes = [ctypes.POINTER(MIB_UNICASTIPADDRESS_ROW)]
_iphlpapi.CreateUnicastIpAddressEntry.restype = wt.DWORD
_iphlpapi.CreateUnicastIpAddressEntry.argtypes = [ctypes.POINTER(MIB_UNICASTIPADDRESS_ROW)]
_iphlpapi.ConvertInterfaceLuidToIndex.restype = wt.DWORD
_iphlpapi.ConvertInterfaceLuidToIndex.argtypes = [ctypes.POINTER(ctypes.c_uint64), ctypes.POINTER(wt.ULONG)]


def add_ipv4_address(luid, address, prefix_length):
    """Assign a static IPv4 address (4 raw bytes) to the interface. Needs administrator rights."""
    row = MIB_UNICASTIPADDRESS_ROW()
    _iphlpapi.InitializeUnicastIpAddressEntry(ctypes.byref(row))
    row.InterfaceLuid = luid
    row.Address.si_family = 2  # AF_INET; sockaddr_in = family, port (2 bytes), address (4 bytes)
    row.Address.data[2:6] = list(address)
    row.OnLinkPrefixLength = prefix_length
    row.DadState = 4  # IpDadStatePreferred
    err = _iphlpapi.CreateUnicastIpAddressEntry(ctypes.byref(row))
    if err not in (0, ERROR_OBJECT_ALREADY_EXISTS):
        raise _error("CreateUnicastIpAddressEntry", err)


def interface_index(luid):
    index = wt.ULONG()
    err = _iphlpapi.ConvertInterfaceLuidToIndex(ctypes.byref(ctypes.c_uint64(luid)), ctypes.byref(index))
    if err:
        raise _error("ConvertInterfaceLuidToIndex", err)
    return index.value
