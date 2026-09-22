"""Set up and remove the Mac's USB driver on this PC, without Zadig.

"Set up" gives the Mac's USB device Microsoft's own WinUSB driver (winusb.inf, which ships with Windows and is
signed by Microsoft): nothing is added to the driver store and no certificate is installed. "Remove" undoes that,
and also cleans up what Zadig adds when it was used instead: its driver package and its self-made certificate.
Installing and removing need administrator rights; listing doesn't.
"""

import ctypes
import ctypes.wintypes as wt
import glob
import os
import re
import uuid
from dataclasses import dataclass

from .winusb import GUID

APPLE_VID = 0x05AC
KNOWN_MAC_PIDS = {0x1905}  # what an Apple silicon Mac reports when plugged into a PC; others are recognised by name
INTERFACE_GUID = "{8F5C2E1A-6B3D-4F7E-9C21-5A0DFEEDC0DE}"  # registered on the Mac's device so the app can open it
WINDIR = os.environ.get("WINDIR", r"C:\Windows")

GUID_DEVCLASS_USBDEVICE = "{88BAE032-5A81-49F0-BC3D-A4FF138216D6}"  # winusb.inf's device class

DIGCF_ALLCLASSES = 0x04
SPDRP_SERVICE = 0x04
SPDRP_CLASSGUID = 0x08
DI_ENUMSINGLEINF = 0x00010000
DI_FLAGSEX_ALLOWEXCLUDEDDRVS = 0x00000800
SPDIT_CLASSDRIVER = 1
DIIDFLAG_NOFINISHINSTALLUI = 0x02
DICS_FLAG_GLOBAL, DIREG_DEV = 1, 1
KEY_QUERY_VALUE, KEY_SET_VALUE = 0x0001, 0x0002
REG_MULTI_SZ = 7
SUOI_FORCEDELETE = 0x0001
CR_SUCCESS = 0
CERT_STORE_PROV_SYSTEM_W = 10
CERT_SYSTEM_STORE_LOCAL_MACHINE = 0x00020000
CERT_STORE_OPEN_EXISTING_FLAG = 0x00004000
CERT_STORE_READONLY_FLAG = 0x00008000
CERT_FIND_SUBJECT_STR_W = 0x00080007
CERT_NAME_SIMPLE_DISPLAY_TYPE = 4
ENCODING = 0x00010001  # X509_ASN_ENCODING | PKCS_7_ASN_ENCODING
INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value


class SP_DEVINFO_DATA(ctypes.Structure):
    _fields_ = [("cbSize", wt.DWORD), ("ClassGuid", GUID), ("DevInst", wt.DWORD), ("Reserved", ctypes.c_size_t)]


class SP_DEVINSTALL_PARAMS_W(ctypes.Structure):
    _fields_ = [("cbSize", wt.DWORD), ("Flags", wt.DWORD), ("FlagsEx", wt.DWORD), ("hwndParent", wt.HWND),
                ("InstallMsgHandler", ctypes.c_void_p), ("InstallMsgHandlerContext", ctypes.c_void_p),
                ("FileQueue", ctypes.c_void_p), ("ClassInstallReserved", ctypes.c_size_t), ("Reserved", wt.DWORD),
                ("DriverPath", ctypes.c_wchar * 260)]


class SP_DRVINFO_DATA_V2_W(ctypes.Structure):
    _fields_ = [("cbSize", wt.DWORD), ("DriverType", wt.DWORD), ("Reserved", ctypes.c_size_t),
                ("Description", ctypes.c_wchar * 256), ("MfgName", ctypes.c_wchar * 256),
                ("ProviderName", ctypes.c_wchar * 256), ("DriverDate", wt.FILETIME), ("DriverVersion", ctypes.c_uint64)]


class DEVPROPKEY(ctypes.Structure):
    _fields_ = [("fmtid", GUID), ("pid", wt.ULONG)]


DEVPKEY_Device_BusReportedDeviceDesc = DEVPROPKEY(GUID.parse("{540b947e-8b40-45bc-a8a2-6a0b894cbda2}"), 4)

_setupapi = ctypes.WinDLL("setupapi", use_last_error=True)
_newdev = ctypes.WinDLL("newdev", use_last_error=True)
_cfgmgr32 = ctypes.WinDLL("cfgmgr32", use_last_error=True)
_advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
_crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
_DEV = ctypes.POINTER(SP_DEVINFO_DATA)


def _fn(dll, name, restype, *argtypes):
    function = getattr(dll, name)
    function.restype = restype
    function.argtypes = list(argtypes)
    return function


_GetClassDevs = _fn(_setupapi, "SetupDiGetClassDevsW", ctypes.c_void_p, ctypes.c_void_p, wt.LPCWSTR, wt.HWND, wt.DWORD)
_CreateList = _fn(_setupapi, "SetupDiCreateDeviceInfoList", ctypes.c_void_p, ctypes.c_void_p, wt.HWND)
_DestroyList = _fn(_setupapi, "SetupDiDestroyDeviceInfoList", wt.BOOL, ctypes.c_void_p)
_EnumDeviceInfo = _fn(_setupapi, "SetupDiEnumDeviceInfo", wt.BOOL, ctypes.c_void_p, wt.DWORD, _DEV)
_OpenDeviceInfo = _fn(_setupapi, "SetupDiOpenDeviceInfoW", wt.BOOL, ctypes.c_void_p, wt.LPCWSTR, wt.HWND, wt.DWORD,
                      _DEV)
_GetInstanceId = _fn(_setupapi, "SetupDiGetDeviceInstanceIdW", wt.BOOL, ctypes.c_void_p, _DEV, wt.LPWSTR, wt.DWORD,
                     ctypes.POINTER(wt.DWORD))
_GetRegistryProperty = _fn(_setupapi, "SetupDiGetDeviceRegistryPropertyW", wt.BOOL, ctypes.c_void_p, _DEV, wt.DWORD,
                           ctypes.POINTER(wt.DWORD), ctypes.c_void_p, wt.DWORD, ctypes.POINTER(wt.DWORD))
_GetDeviceProperty = _fn(_setupapi, "SetupDiGetDevicePropertyW", wt.BOOL, ctypes.c_void_p, _DEV,
                         ctypes.POINTER(DEVPROPKEY), ctypes.POINTER(wt.ULONG), ctypes.c_void_p, wt.DWORD,
                         ctypes.POINTER(wt.DWORD), wt.DWORD)
_GetInstallParams = _fn(_setupapi, "SetupDiGetDeviceInstallParamsW", wt.BOOL, ctypes.c_void_p, _DEV,
                        ctypes.POINTER(SP_DEVINSTALL_PARAMS_W))
_SetInstallParams = _fn(_setupapi, "SetupDiSetDeviceInstallParamsW", wt.BOOL, ctypes.c_void_p, _DEV,
                        ctypes.POINTER(SP_DEVINSTALL_PARAMS_W))
_BuildDriverList = _fn(_setupapi, "SetupDiBuildDriverInfoList", wt.BOOL, ctypes.c_void_p, _DEV, wt.DWORD)
_EnumDriverInfo = _fn(_setupapi, "SetupDiEnumDriverInfoW", wt.BOOL, ctypes.c_void_p, _DEV, wt.DWORD, wt.DWORD,
                      ctypes.POINTER(SP_DRVINFO_DATA_V2_W))
_SetSelectedDriver = _fn(_setupapi, "SetupDiSetSelectedDriverW", wt.BOOL, ctypes.c_void_p, _DEV,
                         ctypes.POINTER(SP_DRVINFO_DATA_V2_W))
_SetRegistryProperty = _fn(_setupapi, "SetupDiSetDeviceRegistryPropertyW", wt.BOOL, ctypes.c_void_p, _DEV, wt.DWORD,
                           ctypes.c_void_p, wt.DWORD)
_OpenDevRegKey = _fn(_setupapi, "SetupDiOpenDevRegKey", wt.HKEY, ctypes.c_void_p, _DEV, wt.DWORD, wt.DWORD, wt.DWORD,
                     wt.DWORD)
_UninstallOEMInf = _fn(_setupapi, "SetupUninstallOEMInfW", wt.BOOL, wt.LPCWSTR, wt.DWORD, ctypes.c_void_p)
_DiInstallDevice = _fn(_newdev, "DiInstallDevice", wt.BOOL, wt.HWND, ctypes.c_void_p, _DEV,
                       ctypes.POINTER(SP_DRVINFO_DATA_V2_W), wt.DWORD, ctypes.POINTER(wt.BOOL))
_DiUninstallDevice = _fn(_newdev, "DiUninstallDevice", wt.BOOL, wt.HWND, ctypes.c_void_p, _DEV, wt.DWORD,
                         ctypes.POINTER(wt.BOOL))
_GetDevNodeStatus = _fn(_cfgmgr32, "CM_Get_DevNode_Status", wt.DWORD, ctypes.POINTER(wt.ULONG),
                        ctypes.POINTER(wt.ULONG), wt.DWORD, wt.ULONG)
_LocateDevNode = _fn(_cfgmgr32, "CM_Locate_DevNodeW", wt.DWORD, ctypes.POINTER(wt.DWORD), wt.LPCWSTR, wt.ULONG)
_ReenumerateDevNode = _fn(_cfgmgr32, "CM_Reenumerate_DevNode", wt.DWORD, wt.DWORD, wt.ULONG)
_RegQueryValueEx = _fn(_advapi32, "RegQueryValueExW", wt.LONG, wt.HKEY, wt.LPCWSTR, ctypes.POINTER(wt.DWORD),
                       ctypes.POINTER(wt.DWORD), ctypes.c_void_p, ctypes.POINTER(wt.DWORD))
_RegSetValueEx = _fn(_advapi32, "RegSetValueExW", wt.LONG, wt.HKEY, wt.LPCWSTR, wt.DWORD, wt.DWORD, ctypes.c_void_p,
                     wt.DWORD)
_RegCloseKey = _fn(_advapi32, "RegCloseKey", wt.LONG, wt.HKEY)
_CertOpenStore = _fn(_crypt32, "CertOpenStore", ctypes.c_void_p, ctypes.c_void_p, wt.DWORD, ctypes.c_void_p,
                     wt.DWORD, ctypes.c_void_p)
_CertCloseStore = _fn(_crypt32, "CertCloseStore", wt.BOOL, ctypes.c_void_p, wt.DWORD)
_CertFind = _fn(_crypt32, "CertFindCertificateInStore", ctypes.c_void_p, ctypes.c_void_p, wt.DWORD, wt.DWORD,
                wt.DWORD, ctypes.c_void_p, ctypes.c_void_p)
_CertGetNameString = _fn(_crypt32, "CertGetNameStringW", wt.DWORD, ctypes.c_void_p, wt.DWORD, wt.DWORD,
                         ctypes.c_void_p, wt.LPWSTR, wt.DWORD)
_CertDuplicate = _fn(_crypt32, "CertDuplicateCertificateContext", ctypes.c_void_p, ctypes.c_void_p)
_CertDelete = _fn(_crypt32, "CertDeleteCertificateFromStore", wt.BOOL, ctypes.c_void_p)
_CertFree = _fn(_crypt32, "CertFreeCertificateContext", wt.BOOL, ctypes.c_void_p)


def _error(what, err=None):
    err = ctypes.get_last_error() if err is None else err
    return OSError(err, f"{what}: {ctypes.FormatError(err).strip()} (error {err})")


def _check(ok, what):
    if not ok:
        raise _error(what)


@dataclass
class MacDevice:
    instance_id: str
    pid: int
    present: bool  # plugged in right now
    service: str   # driver in use: "WinUSB" once set up, "usbccgp" before
    name: str      # what the device calls itself ("Mac")

    @property
    def ready(self):
        return self.service.lower() == "winusb"


def is_mac(pid, name):
    return pid in KNOWN_MAC_PIDS or name == "Mac"


def _string(buf):
    return ctypes.wstring_at(ctypes.addressof(buf))


def _instance_id(hset, dev):
    buf = ctypes.create_unicode_buffer(512)
    return buf.value if _GetInstanceId(hset, ctypes.byref(dev), buf, 512, None) else ""


def _registry_string(hset, dev, prop):
    buf = ctypes.create_unicode_buffer(512)
    ok = _GetRegistryProperty(hset, ctypes.byref(dev), prop, None, buf, ctypes.sizeof(buf), None)
    return buf.value if ok else ""


def _device_property_string(hset, dev, key):
    buf = ctypes.create_unicode_buffer(512)
    kind = wt.ULONG()
    ok = _GetDeviceProperty(hset, ctypes.byref(dev), ctypes.byref(key), ctypes.byref(kind), buf, ctypes.sizeof(buf),
                            None, 0)
    return buf.value if ok else ""


def _present(devinst):
    status, problem = wt.ULONG(), wt.ULONG()
    return _GetDevNodeStatus(ctypes.byref(status), ctypes.byref(problem), devinst, 0) == CR_SUCCESS


def find_macs():
    """Macs this PC knows as USB devices, plugged in now or before."""
    hset = _GetClassDevs(None, "USB", None, DIGCF_ALLCLASSES)
    if not hset or hset == INVALID_HANDLE_VALUE:
        raise _error("list USB devices")
    macs = []
    try:
        index = 0
        while True:
            dev = SP_DEVINFO_DATA(cbSize=ctypes.sizeof(SP_DEVINFO_DATA))
            if not _EnumDeviceInfo(hset, index, ctypes.byref(dev)):
                break
            index += 1
            instance = _instance_id(hset, dev)
            match = re.match(r"USB\\VID_05AC&PID_([0-9A-F]{4})\\", instance, re.IGNORECASE)  # whole device, not &MI_
            if not match:
                continue
            pid = int(match.group(1), 16)
            name = _device_property_string(hset, dev, DEVPKEY_Device_BusReportedDeviceDesc)
            if is_mac(pid, name):
                macs.append(MacDevice(instance, pid, _present(dev.DevInst),
                                      _registry_string(hset, dev, SPDRP_SERVICE), name))
    finally:
        _DestroyList(hset)
    return macs


def _open(hset, instance_id):
    dev = SP_DEVINFO_DATA(cbSize=ctypes.sizeof(SP_DEVINFO_DATA))
    _check(_OpenDeviceInfo(hset, instance_id, None, 0, ctypes.byref(dev)), f"find device {instance_id}")
    return dev


def _read_multi_sz(key, name):
    kind, size = wt.DWORD(), wt.DWORD()
    if _RegQueryValueEx(key, name, None, ctypes.byref(kind), None, ctypes.byref(size)) or kind.value != REG_MULTI_SZ:
        return []
    buf = ctypes.create_string_buffer(size.value)
    if _RegQueryValueEx(key, name, None, None, buf, ctypes.byref(size)):
        return []
    return [part for part in buf.raw[:size.value].decode("utf-16-le").split("\0") if part]


def _register_interface_guid(hset, dev):
    key = _OpenDevRegKey(hset, ctypes.byref(dev), DICS_FLAG_GLOBAL, 0, DIREG_DEV, KEY_QUERY_VALUE | KEY_SET_VALUE)
    if not key or key == INVALID_HANDLE_VALUE:
        raise _error("open the device's settings")
    try:
        guids = _read_multi_sz(key, "DeviceInterfaceGUIDs")
        if INTERFACE_GUID.lower() in (guid.lower() for guid in guids):
            return
        data = ("\0".join(guids + [INTERFACE_GUID]) + "\0\0").encode("utf-16-le")
        status = _RegSetValueEx(key, "DeviceInterfaceGUIDs", 0, REG_MULTI_SZ, data, len(data))
        if status:
            raise _error("save the device's interface GUID", status)
    finally:
        _RegCloseKey(key)


def _winusb_driver(hset, dev=None):
    """Find the 'WinUsb Device' driver in Windows' own winusb.inf, for a device (or for the whole set's class)."""
    target = ctypes.byref(dev) if dev is not None else None
    params = SP_DEVINSTALL_PARAMS_W(cbSize=ctypes.sizeof(SP_DEVINSTALL_PARAMS_W))
    _check(_GetInstallParams(hset, target, ctypes.byref(params)), "read install settings")
    params.Flags |= DI_ENUMSINGLEINF
    params.FlagsEx |= DI_FLAGSEX_ALLOWEXCLUDEDDRVS
    params.DriverPath = os.path.join(WINDIR, "INF", "winusb.inf")
    _check(_SetInstallParams(hset, target, ctypes.byref(params)), "use winusb.inf")
    _check(_BuildDriverList(hset, target, SPDIT_CLASSDRIVER), "read winusb.inf")
    index = 0
    while True:
        info = SP_DRVINFO_DATA_V2_W(cbSize=ctypes.sizeof(SP_DRVINFO_DATA_V2_W))
        if not _EnumDriverInfo(hset, target, SPDIT_CLASSDRIVER, index, ctypes.byref(info)):
            raise OSError(2, "Windows' winusb.inf doesn't offer the 'WinUsb Device' driver")
        if info.Description == "WinUsb Device" and info.ProviderName == "Microsoft":
            return info
        index += 1


def winusb_available():
    """Read-only check that this Windows has Microsoft's WinUSB driver to offer (what install_winusb uses)."""
    class_guid = GUID.parse(GUID_DEVCLASS_USBDEVICE)
    hset = _CreateList(ctypes.byref(class_guid), None)
    if not hset or hset == INVALID_HANDLE_VALUE:
        raise _error("prepare a driver search")
    try:
        return _winusb_driver(hset).Description == "WinUsb Device"
    finally:
        _DestroyList(hset)


def _class_of(instance_id):
    hset = _CreateList(None, None)
    try:
        return "{%s}" % str(uuid.UUID(bytes_le=bytes(_open(hset, instance_id).ClassGuid))).upper()
    finally:
        _DestroyList(hset)


def _set_class(instance_id, class_guid):
    hset = _CreateList(None, None)
    try:
        dev = _open(hset, instance_id)
        data = (class_guid + "\0").encode("utf-16-le")
        _check(_SetRegistryProperty(hset, ctypes.byref(dev), SPDRP_CLASSGUID, data, len(data)), "set the device type")
    finally:
        _DestroyList(hset)


def install_winusb(instance_id):
    """Give one Mac Microsoft's WinUSB driver. Needs administrator rights. Returns True if Windows wants a restart.

    winusb.inf only offers its driver to devices of its own class, so the device moves to that class first, just as
    when picking "Universal Serial Bus devices > WinUsb Device" by hand in Device Manager. On failure it moves back.
    """
    original_class = _class_of(instance_id)
    moved = original_class != GUID_DEVCLASS_USBDEVICE
    if moved:
        _set_class(instance_id, GUID_DEVCLASS_USBDEVICE)
    try:
        hset = _CreateList(None, None)
        try:
            dev = _open(hset, instance_id)  # opened after the move, so it carries the new class
            _register_interface_guid(hset, dev)
            driver = _winusb_driver(hset, dev)
            _check(_SetSelectedDriver(hset, ctypes.byref(dev), ctypes.byref(driver)), "select WinUSB")
            reboot = wt.BOOL()
            _check(_DiInstallDevice(None, hset, ctypes.byref(dev), ctypes.byref(driver), DIIDFLAG_NOFINISHINSTALLUI,
                                    ctypes.byref(reboot)), "install WinUSB")
            return bool(reboot.value)
        finally:
            _DestroyList(hset)
    except Exception:
        if moved:
            try:
                _set_class(instance_id, original_class)
            except OSError:
                pass
        raise


def uninstall_device(instance_id):
    """Remove the device's driver setup (like Device Manager's Uninstall device). Returns True if a restart is due."""
    hset = _CreateList(None, None)
    try:
        dev = _open(hset, instance_id)
        reboot = wt.BOOL()
        _check(_DiUninstallDevice(None, hset, ctypes.byref(dev), 0, ctypes.byref(reboot)), "uninstall the Mac's driver")
        return bool(reboot.value)
    finally:
        _DestroyList(hset)


def _read_inf(path):
    with open(path, "rb") as f:
        data = f.read()
    return data.decode("utf-16") if data[:2] in (b"\xff\xfe", b"\xfe\xff") else data.decode("latin-1")


def targets_mac_with_winusb(inf_text):
    """True for a driver package that puts WinUSB on a Mac's whole USB device (as Zadig's does).

    Zadig writes the ID through a string variable (DeviceID = "VID_05AC&PID_1905"), so match the pair itself.
    """
    pids = re.findall(r"VID_05AC&PID_([0-9A-F]{4})(?![0-9A-F])(?!&MI_)", inf_text, re.IGNORECASE)
    return any(int(pid, 16) in KNOWN_MAC_PIDS for pid in pids) and re.search("winusb", inf_text, re.IGNORECASE)


def mac_driver_packages():
    """Driver packages in Windows' driver store that put WinUSB on a Mac (e.g. added by Zadig)."""
    found = []
    for path in sorted(glob.glob(os.path.join(WINDIR, "INF", "oem*.inf"))):
        try:
            if targets_mac_with_winusb(_read_inf(path)):
                found.append(os.path.basename(path))
        except OSError:
            pass
    return found


def delete_driver_package(name):
    _check(_UninstallOEMInf(name, SUOI_FORCEDELETE, None), f"delete driver package {name}")


def is_zadig_mac_certificate(subject):
    match = re.search(r"USB\\VID_05AC&PID_([0-9A-F]{4})", subject, re.IGNORECASE)
    return bool(match) and int(match.group(1), 16) in KNOWN_MAC_PIDS and "libwdi autogenerated" in subject


def _zadig_certificates(store_name, delete):
    flags = CERT_SYSTEM_STORE_LOCAL_MACHINE | CERT_STORE_OPEN_EXISTING_FLAG | (0 if delete else CERT_STORE_READONLY_FLAG)
    store = _CertOpenStore(ctypes.c_void_p(CERT_STORE_PROV_SYSTEM_W), 0, None, flags, ctypes.c_wchar_p(store_name))
    if not store:
        raise _error(f"open the {store_name} certificate store")
    matched, context = [], None
    try:
        while True:
            context = _CertFind(store, ENCODING, 0, CERT_FIND_SUBJECT_STR_W, ctypes.c_wchar_p("libwdi autogenerated"),
                                context)
            if not context:
                break
            name = ctypes.create_unicode_buffer(512)
            _CertGetNameString(context, CERT_NAME_SIMPLE_DISPLAY_TYPE, 0, None, name, 512)
            if is_zadig_mac_certificate(name.value):
                matched.append(name.value)
                if delete:
                    _check(_CertDelete(_CertDuplicate(context)), f"delete certificate '{name.value}'")
    finally:
        if context:
            _CertFree(context)
        _CertCloseStore(store, 0)
    return matched


def zadig_certificates():
    """Zadig's self-made certificates for a Mac, as [(store, subject)]."""
    return [(store, subject) for store in ("Root", "TrustedPublisher")
            for subject in _zadig_certificates(store, delete=False)]


def rescan():
    """Ask Windows to look for devices again (so a plugged-in Mac gets its standard driver back right away)."""
    root = wt.DWORD()
    if _LocateDevNode(ctypes.byref(root), None, 0) == CR_SUCCESS:
        _ReenumerateDevNode(root.value, 0)


@dataclass
class Leftovers:
    devices: list       # MacDevice using WinUSB (set up by this app or Zadig)
    packages: list      # driver packages for a Mac, e.g. Zadig's
    certificates: list  # (store, subject) of Zadig's certificates for a Mac

    def __bool__(self):
        return bool(self.devices or self.packages or self.certificates)


def leftovers():
    """What remove_all() would undo on this PC."""
    return Leftovers([mac for mac in find_macs() if mac.ready], mac_driver_packages(), zadig_certificates())


def remove_all():
    """Undo the Mac's driver setup (this app's and Zadig's). Needs administrator rights. Returns what was done."""
    done = []
    for name in mac_driver_packages():  # first, so Windows can't pick Zadig's package again below
        delete_driver_package(name)
        done.append(f"deleted the driver package {name}")
    for mac in find_macs():
        if mac.ready:
            uninstall_device(mac.instance_id)
            done.append(f"gave the Mac's USB device back to Windows' standard driver ({mac.instance_id})")
    for store in ("Root", "TrustedPublisher"):
        for subject in _zadig_certificates(store, delete=True):
            done.append(f"deleted the certificate '{subject}' from {store}")
    rescan()
    return done
