"""Minimal ctypes bindings for SetupAPI + WinUSB, using only DLLs that ship with Windows."""

import ctypes
import ctypes.wintypes as wt
import uuid
import winreg
from collections.abc import Callable
from typing import Any, Self

_setupapi = ctypes.WinDLL("setupapi", use_last_error=True)
_winusb = ctypes.WinDLL("winusb", use_last_error=True)
_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

GUID_DEVINTERFACE_USB_DEVICE = "{A5DCBF10-6530-11D2-901F-00C04FB951ED}"

INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
ERROR_SEM_TIMEOUT = 121
ERROR_NO_MORE_ITEMS = 259

# Pipe policy types (winusbio.h)
SHORT_PACKET_TERMINATE = 0x01
AUTO_CLEAR_STALL = 0x02
PIPE_TRANSFER_TIMEOUT = 0x03
RAW_IO = 0x07
# Power policy types
AUTO_SUSPEND = 0x81

PIPE_BULK = 2


class WinUsbError(OSError):
    pass


def _check(ok: object, what: str) -> None:
    if not ok:
        err = ctypes.get_last_error()
        raise WinUsbError(err, f"{what}: {ctypes.FormatError(err).strip()} (error {err})")


class GUID(ctypes.Structure):
    _fields_ = [("Data1", wt.DWORD), ("Data2", wt.WORD), ("Data3", wt.WORD), ("Data4", ctypes.c_ubyte * 8)]

    @classmethod
    def parse(cls, text: str) -> Self:
        u = uuid.UUID(text)
        guid = cls(u.time_low, u.time_mid, u.time_hi_version)
        guid.Data4[:] = list(u.bytes[8:])
        return guid


class SP_DEVICE_INTERFACE_DATA(ctypes.Structure):
    _fields_ = [("cbSize", wt.DWORD), ("InterfaceClassGuid", GUID), ("Flags", wt.DWORD), ("Reserved", ctypes.c_size_t)]


class WINUSB_SETUP_PACKET(ctypes.Structure):
    _pack_ = 1
    _fields_ = [
        ("RequestType", ctypes.c_ubyte),
        ("Request", ctypes.c_ubyte),
        ("Value", ctypes.c_ushort),
        ("Index", ctypes.c_ushort),
        ("Length", ctypes.c_ushort),
    ]


class USB_INTERFACE_DESCRIPTOR(ctypes.Structure):
    _pack_ = 1
    _fields_ = [
        ("bLength", ctypes.c_ubyte),
        ("bDescriptorType", ctypes.c_ubyte),
        ("bInterfaceNumber", ctypes.c_ubyte),
        ("bAlternateSetting", ctypes.c_ubyte),
        ("bNumEndpoints", ctypes.c_ubyte),
        ("bInterfaceClass", ctypes.c_ubyte),
        ("bInterfaceSubClass", ctypes.c_ubyte),
        ("bInterfaceProtocol", ctypes.c_ubyte),
        ("iInterface", ctypes.c_ubyte),
    ]


class WINUSB_PIPE_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("PipeType", ctypes.c_int),
        ("PipeId", ctypes.c_ubyte),
        ("MaximumPacketSize", ctypes.c_ushort),
        ("Interval", ctypes.c_ubyte),
    ]


_HANDLE = ctypes.c_void_p
_ULONG_P = ctypes.POINTER(wt.ULONG)


def _proto(dll: ctypes.WinDLL, name: str, restype: Any, *argtypes: Any) -> Callable[..., Any]:
    fn = dll[name]
    fn.restype = restype
    fn.argtypes = list(argtypes)
    return fn


_SetupDiGetClassDevsW = _proto(
    _setupapi, "SetupDiGetClassDevsW", ctypes.c_void_p, ctypes.POINTER(GUID), wt.LPCWSTR, wt.HWND, wt.DWORD
)
_SetupDiEnumDeviceInterfaces = _proto(
    _setupapi,
    "SetupDiEnumDeviceInterfaces",
    wt.BOOL,
    ctypes.c_void_p,
    ctypes.c_void_p,
    ctypes.POINTER(GUID),
    wt.DWORD,
    ctypes.POINTER(SP_DEVICE_INTERFACE_DATA),
)
_SetupDiGetDeviceInterfaceDetailW = _proto(
    _setupapi,
    "SetupDiGetDeviceInterfaceDetailW",
    wt.BOOL,
    ctypes.c_void_p,
    ctypes.POINTER(SP_DEVICE_INTERFACE_DATA),
    ctypes.c_void_p,
    wt.DWORD,
    ctypes.POINTER(wt.DWORD),
    ctypes.c_void_p,
)
_SetupDiDestroyDeviceInfoList = _proto(_setupapi, "SetupDiDestroyDeviceInfoList", wt.BOOL, ctypes.c_void_p)
_CreateFileW = _proto(
    _kernel32, "CreateFileW", _HANDLE, wt.LPCWSTR, wt.DWORD, wt.DWORD, ctypes.c_void_p, wt.DWORD, wt.DWORD, _HANDLE
)
_CloseHandle = _proto(_kernel32, "CloseHandle", wt.BOOL, _HANDLE)
_WinUsb_Initialize = _proto(_winusb, "WinUsb_Initialize", wt.BOOL, _HANDLE, ctypes.POINTER(_HANDLE))
_WinUsb_Free = _proto(_winusb, "WinUsb_Free", wt.BOOL, _HANDLE)
_WinUsb_GetAssociatedInterface = _proto(
    _winusb, "WinUsb_GetAssociatedInterface", wt.BOOL, _HANDLE, ctypes.c_ubyte, ctypes.POINTER(_HANDLE)
)
_WinUsb_QueryInterfaceSettings = _proto(
    _winusb, "WinUsb_QueryInterfaceSettings", wt.BOOL, _HANDLE, ctypes.c_ubyte, ctypes.POINTER(USB_INTERFACE_DESCRIPTOR)
)
_WinUsb_QueryPipe = _proto(
    _winusb,
    "WinUsb_QueryPipe",
    wt.BOOL,
    _HANDLE,
    ctypes.c_ubyte,
    ctypes.c_ubyte,
    ctypes.POINTER(WINUSB_PIPE_INFORMATION),
)
_WinUsb_SetCurrentAlternateSetting = _proto(
    _winusb, "WinUsb_SetCurrentAlternateSetting", wt.BOOL, _HANDLE, ctypes.c_ubyte
)
_WinUsb_ControlTransfer = _proto(
    _winusb,
    "WinUsb_ControlTransfer",
    wt.BOOL,
    _HANDLE,
    WINUSB_SETUP_PACKET,
    ctypes.c_void_p,
    wt.ULONG,
    _ULONG_P,
    ctypes.c_void_p,
)
_WinUsb_ReadPipe = _proto(
    _winusb, "WinUsb_ReadPipe", wt.BOOL, _HANDLE, ctypes.c_ubyte, ctypes.c_void_p, wt.ULONG, _ULONG_P, ctypes.c_void_p
)
_WinUsb_WritePipe = _proto(
    _winusb, "WinUsb_WritePipe", wt.BOOL, _HANDLE, ctypes.c_ubyte, ctypes.c_void_p, wt.ULONG, _ULONG_P, ctypes.c_void_p
)
_WinUsb_SetPipePolicy = _proto(
    _winusb, "WinUsb_SetPipePolicy", wt.BOOL, _HANDLE, ctypes.c_ubyte, wt.ULONG, wt.ULONG, ctypes.c_void_p
)
_WinUsb_SetPowerPolicy = _proto(_winusb, "WinUsb_SetPowerPolicy", wt.BOOL, _HANDLE, wt.ULONG, wt.ULONG, ctypes.c_void_p)
_WinUsb_AbortPipe = _proto(_winusb, "WinUsb_AbortPipe", wt.BOOL, _HANDLE, ctypes.c_ubyte)


def interface_paths(guid_text: str) -> list[str]:
    """Device paths of all present devices exposing the given device interface class."""
    guid = GUID.parse(guid_text)
    hdev = _SetupDiGetClassDevsW(ctypes.byref(guid), None, None, 0x02 | 0x10)  # DIGCF_PRESENT | DIGCF_DEVICEINTERFACE
    if not hdev or hdev == INVALID_HANDLE_VALUE:
        return []
    paths: list[str] = []
    try:
        index = 0
        while True:
            data = SP_DEVICE_INTERFACE_DATA(cbSize=ctypes.sizeof(SP_DEVICE_INTERFACE_DATA))
            if not _SetupDiEnumDeviceInterfaces(hdev, None, ctypes.byref(guid), index, ctypes.byref(data)):
                break
            index += 1
            needed = wt.DWORD()
            _SetupDiGetDeviceInterfaceDetailW(hdev, ctypes.byref(data), None, 0, ctypes.byref(needed), None)
            buf = ctypes.create_string_buffer(needed.value)
            # SP_DEVICE_INTERFACE_DETAIL_DATA_W.cbSize is 8 on 64-bit Windows, 6 on 32-bit.
            ctypes.c_uint32.from_buffer(buf).value = 8 if ctypes.sizeof(ctypes.c_void_p) == 8 else 6
            if _SetupDiGetDeviceInterfaceDetailW(hdev, ctypes.byref(data), buf, needed, None, None):
                paths.append(ctypes.wstring_at(ctypes.addressof(buf) + 4))
    finally:
        _SetupDiDestroyDeviceInfoList(hdev)
    return paths


def _registered_interface_guids(vid: int, pid: int) -> list[str]:
    """Interface GUIDs a driver installer (e.g. Zadig) registered for this VID/PID."""
    guids: list[str] = []
    base = rf"SYSTEM\CurrentControlSet\Enum\USB\VID_{vid:04X}&PID_{pid:04X}"
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, base) as key:
            i = 0
            while True:
                try:
                    instance = winreg.EnumKey(key, i)
                except OSError:
                    break
                i += 1
                try:
                    with winreg.OpenKey(key, instance + r"\Device Parameters") as params:
                        for name in ("DeviceInterfaceGUIDs", "DeviceInterfaceGUID"):
                            try:
                                value, _ = winreg.QueryValueEx(params, name)
                            except OSError:
                                continue
                            guids.extend(value if isinstance(value, list) else [value])
                except OSError:
                    pass
    except OSError:
        pass
    return [g for g in guids if g]


def find_device_paths(vid: int, pid: int) -> list[str]:
    needle = f"vid_{vid:04x}&pid_{pid:04x}"
    paths: list[str] = []
    for guid in [*_registered_interface_guids(vid, pid), GUID_DEVINTERFACE_USB_DEVICE]:
        for path in interface_paths(guid):
            low = path.lower()
            if needle in low and "&mi_" not in low and path not in paths:
                paths.append(path)
    return paths


class Interface:
    """One USB interface of a WinUSB-bound device."""

    def __init__(self, handle: ctypes.c_void_p, descriptor: USB_INTERFACE_DESCRIPTOR) -> None:
        self.handle = handle
        self.number: int = descriptor.bInterfaceNumber
        self.cls: int = descriptor.bInterfaceClass
        self.subclass: int = descriptor.bInterfaceSubClass
        self.protocol: int = descriptor.bInterfaceProtocol

    def pipes(self, alt: int) -> list[WINUSB_PIPE_INFORMATION]:
        desc = USB_INTERFACE_DESCRIPTOR()
        _check(
            _WinUsb_QueryInterfaceSettings(self.handle, alt, ctypes.byref(desc)),
            f"query interface {self.number} alt {alt}",
        )
        pipes: list[WINUSB_PIPE_INFORMATION] = []
        for i in range(desc.bNumEndpoints):
            info = WINUSB_PIPE_INFORMATION()
            _check(_WinUsb_QueryPipe(self.handle, alt, i, ctypes.byref(info)), f"query pipe {i}")
            pipes.append(info)
        return pipes

    def set_alt(self, alt: int) -> None:
        _check(_WinUsb_SetCurrentAlternateSetting(self.handle, alt), f"select interface {self.number} alt {alt}")

    def control_in(self, request_type: int, request: int, value: int, index: int, length: int) -> bytes:
        buf = ctypes.create_string_buffer(length)
        done = wt.ULONG()
        setup = WINUSB_SETUP_PACKET(request_type, request, value, index, length)
        _check(
            _WinUsb_ControlTransfer(self.handle, setup, buf, length, ctypes.byref(done), None),
            f"control request 0x{request:02x}",
        )
        return buf.raw[: done.value]

    def control_out(self, request_type: int, request: int, value: int, index: int, data: bytes = b"") -> None:
        buf = ctypes.create_string_buffer(data, len(data)) if data else None
        done = wt.ULONG()
        setup = WINUSB_SETUP_PACKET(request_type, request, value, index, len(data))
        _check(
            _WinUsb_ControlTransfer(self.handle, setup, buf, len(data), ctypes.byref(done), None),
            f"control request 0x{request:02x}",
        )

    def set_pipe_policy(self, pipe: int, policy: int, value: int) -> None:
        v = wt.ULONG(value) if policy == PIPE_TRANSFER_TIMEOUT else ctypes.c_ubyte(value)
        _check(
            _WinUsb_SetPipePolicy(self.handle, pipe, policy, ctypes.sizeof(v), ctypes.byref(v)),
            f"set pipe policy {policy} on 0x{pipe:02x}",
        )

    def read_into(self, pipe: int, buffer: bytearray) -> int | None:
        """Read one transfer into a bytearray. Returns the byte count, or None on timeout."""
        view = (ctypes.c_char * len(buffer)).from_buffer(buffer)
        done = wt.ULONG()
        if not _WinUsb_ReadPipe(self.handle, pipe, view, len(buffer), ctypes.byref(done), None):
            err = ctypes.get_last_error()
            if err == ERROR_SEM_TIMEOUT:
                return None
            raise WinUsbError(err, f"read 0x{pipe:02x}: {ctypes.FormatError(err).strip()} (error {err})")
        return done.value

    def write(self, pipe: int, data: bytes) -> int:
        done = wt.ULONG()
        _check(_WinUsb_WritePipe(self.handle, pipe, data, len(data), ctypes.byref(done), None), f"write 0x{pipe:02x}")
        return done.value

    def abort(self, pipe: int) -> None:
        _WinUsb_AbortPipe(self.handle, pipe)


class WinUsbDevice:
    """A whole USB device bound to WinUSB, with a handle per interface."""

    def __init__(self, path: str) -> None:
        self.path = path
        self.interfaces: dict[int, Interface] = {}
        self._handles: list[ctypes.c_void_p] = []
        # R/W, shared, overlapped
        self._file: int | None = _CreateFileW(path, 0xC0000000, 0x3, None, 3, 0x40000080, None)
        if not self._file or self._file == INVALID_HANDLE_VALUE:
            err = ctypes.get_last_error()
            raise WinUsbError(err, f"open device: {ctypes.FormatError(err).strip()} (error {err})")
        try:
            first = _HANDLE()
            _check(_WinUsb_Initialize(self._file, ctypes.byref(first)), "WinUsb_Initialize")
            self._handles.append(first)
            index = 0
            while True:
                handle = _HANDLE()
                if not _WinUsb_GetAssociatedInterface(first, index, ctypes.byref(handle)):
                    if ctypes.get_last_error() == ERROR_NO_MORE_ITEMS:
                        break
                    _check(False, f"WinUsb_GetAssociatedInterface({index})")
                self._handles.append(handle)
                index += 1
            for handle in self._handles:
                desc = USB_INTERFACE_DESCRIPTOR()
                _check(_WinUsb_QueryInterfaceSettings(handle, 0, ctypes.byref(desc)), "WinUsb_QueryInterfaceSettings")
                self.interfaces[desc.bInterfaceNumber] = Interface(handle, desc)
        except Exception:
            self.close()
            raise

    def disable_selective_suspend(self) -> None:
        off = ctypes.c_ubyte(0)
        _check(
            _WinUsb_SetPowerPolicy(self._handles[0], AUTO_SUSPEND, 1, ctypes.byref(off)),
            "disable USB selective suspend",
        )

    def close(self) -> None:
        for handle in reversed(self._handles):
            _WinUsb_Free(handle)
        self._handles = []
        if self._file and self._file != INVALID_HANDLE_VALUE:
            _CloseHandle(self._file)
        self._file = None


def open_device(vid: int, pid: int) -> WinUsbDevice:
    paths = find_device_paths(vid, pid)
    if not paths:
        raise WinUsbError(2, f"no USB device {vid:04x}:{pid:04x} is connected")
    errors: list[str] = []
    for path in paths:
        try:
            return WinUsbDevice(path)
        except OSError as e:
            errors.append(str(e))
    raise WinUsbError(
        1, f"device {vid:04x}:{pid:04x} is connected but could not be opened with WinUSB ({'; '.join(errors)})"
    )
