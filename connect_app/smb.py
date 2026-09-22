"""Windows file-sharing helpers: list a server's shared folders, sign in, and keep the password in Credential Manager.

Credentials saved here are ordinary Windows credentials for the server's address, so Explorer uses them too and
opens \\\\server without asking. Windows keeps them encrypted; this module never writes passwords anywhere else.
"""

import ctypes
import ctypes.wintypes as wt
import socket

_netapi32 = ctypes.WinDLL("netapi32", use_last_error=True)
_mpr = ctypes.WinDLL("mpr", use_last_error=True)
_advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)

STYPE_DISKTREE = 0
STYPE_SPECIAL = 0x80000000
MAX_PREFERRED_LENGTH = 0xFFFFFFFF
ERROR_MORE_DATA = 234
ERROR_NOT_FOUND = 1168
ERROR_SESSION_CREDENTIAL_CONFLICT = 1219
CRED_TYPE_DOMAIN_PASSWORD = 2
CRED_PERSIST_LOCAL_MACHINE = 2

AUTH_ERRORS = {5, 86, 1326, 1331, 2221}

_HINTS = {
    5: "Access denied. Sign in with your Mac account.",
    53: "The Mac's file sharing can't be reached. Is File Sharing turned on?",
    64: "The connection to the Mac dropped.",
    67: "The Mac's file sharing can't be reached. Is File Sharing turned on?",
    86: "Wrong user name or password.",
    1203: "The Mac's file sharing can't be reached. Is File Sharing turned on?",
    1231: "The Mac can't be reached. Is the bridge connected?",
    1326: "Wrong user name or password, or your account isn't ticked under Windows File Sharing on the Mac.",
    1331: "This Mac account is disabled.",
    2221: "The Mac doesn't know that user name.",
}


class SmbError(OSError):
    pass


def _error(code):
    return SmbError(code, _HINTS.get(code) or f"{ctypes.FormatError(code).strip()} (error {code})")


class SHARE_INFO_1(ctypes.Structure):
    _fields_ = [("netname", wt.LPWSTR), ("type", wt.DWORD), ("remark", wt.LPWSTR)]


class NETRESOURCEW(ctypes.Structure):
    _fields_ = [("dwScope", wt.DWORD), ("dwType", wt.DWORD), ("dwDisplayType", wt.DWORD), ("dwUsage", wt.DWORD),
                ("lpLocalName", wt.LPWSTR), ("lpRemoteName", wt.LPWSTR), ("lpComment", wt.LPWSTR),
                ("lpProvider", wt.LPWSTR)]


class CREDENTIALW(ctypes.Structure):
    _fields_ = [("Flags", wt.DWORD), ("Type", wt.DWORD), ("TargetName", wt.LPWSTR), ("Comment", wt.LPWSTR),
                ("LastWritten", wt.FILETIME), ("CredentialBlobSize", wt.DWORD),
                ("CredentialBlob", ctypes.POINTER(ctypes.c_ubyte)), ("Persist", wt.DWORD),
                ("AttributeCount", wt.DWORD), ("Attributes", ctypes.c_void_p), ("TargetAlias", wt.LPWSTR),
                ("UserName", wt.LPWSTR)]


_netapi32.NetShareEnum.restype = wt.DWORD
_netapi32.NetShareEnum.argtypes = [wt.LPWSTR, wt.DWORD, ctypes.POINTER(ctypes.c_void_p), wt.DWORD,
                                   ctypes.POINTER(wt.DWORD), ctypes.POINTER(wt.DWORD), ctypes.POINTER(wt.DWORD)]
_netapi32.NetApiBufferFree.restype = wt.DWORD
_netapi32.NetApiBufferFree.argtypes = [ctypes.c_void_p]
_mpr.WNetAddConnection2W.restype = wt.DWORD
_mpr.WNetAddConnection2W.argtypes = [ctypes.POINTER(NETRESOURCEW), wt.LPCWSTR, wt.LPCWSTR, wt.DWORD]
_mpr.WNetCancelConnection2W.restype = wt.DWORD
_mpr.WNetCancelConnection2W.argtypes = [wt.LPCWSTR, wt.DWORD, wt.BOOL]
_advapi32.CredWriteW.restype = wt.BOOL
_advapi32.CredWriteW.argtypes = [ctypes.POINTER(CREDENTIALW), wt.DWORD]
_advapi32.CredReadW.restype = wt.BOOL
_advapi32.CredReadW.argtypes = [wt.LPCWSTR, wt.DWORD, wt.DWORD, ctypes.POINTER(ctypes.POINTER(CREDENTIALW))]
_advapi32.CredDeleteW.restype = wt.BOOL
_advapi32.CredDeleteW.argtypes = [wt.LPCWSTR, wt.DWORD, wt.DWORD]
_advapi32.CredFree.restype = None
_advapi32.CredFree.argtypes = [ctypes.c_void_p]


def list_shares(server):
    """Shared folders on \\\\server as a sorted list of (name, description), without hidden/admin shares."""
    buf = ctypes.c_void_p()
    read, total, resume = wt.DWORD(), wt.DWORD(), wt.DWORD()
    status = _netapi32.NetShareEnum(f"\\\\{server}", 1, ctypes.byref(buf), MAX_PREFERRED_LENGTH,
                                    ctypes.byref(read), ctypes.byref(total), ctypes.byref(resume))
    try:
        if status not in (0, ERROR_MORE_DATA):
            raise _error(status)
        shares = []
        if read.value:
            for entry in ctypes.cast(buf, ctypes.POINTER(SHARE_INFO_1 * read.value)).contents:
                name = entry.netname or ""
                if entry.type & 0xFF == STYPE_DISKTREE and not entry.type & STYPE_SPECIAL and not name.endswith("$"):
                    shares.append((name, entry.remark or ""))
        return sorted(shares, key=lambda share: share[0].lower())
    finally:
        if buf:
            _netapi32.NetApiBufferFree(buf)


def sign_in(server, user, password):
    """Open a signed-in session to \\\\server for this process, replacing any earlier one."""
    resource = NETRESOURCEW(dwType=0, lpRemoteName=f"\\\\{server}\\IPC$")
    status = _mpr.WNetAddConnection2W(ctypes.byref(resource), password, user, 0)
    if status == ERROR_SESSION_CREDENTIAL_CONFLICT:
        sign_out(server)
        status = _mpr.WNetAddConnection2W(ctypes.byref(resource), password, user, 0)
    if status:
        raise _error(status)


def sign_out(server):
    _mpr.WNetCancelConnection2W(f"\\\\{server}\\IPC$", 0, True)


def save_credentials(server, user, password):
    """Store the sign-in for \\\\server in Windows Credential Manager (used by Explorer as well)."""
    blob = password.encode("utf-16-le")
    buffer = (ctypes.c_ubyte * max(len(blob), 1)).from_buffer_copy(blob or b"\0")
    cred = CREDENTIALW()
    cred.Type = CRED_TYPE_DOMAIN_PASSWORD
    cred.TargetName = server
    cred.Comment = "Mac over USB-C (Connect App)"
    cred.CredentialBlobSize = len(blob)
    cred.CredentialBlob = ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte))
    cred.Persist = CRED_PERSIST_LOCAL_MACHINE
    cred.UserName = user
    if not _advapi32.CredWriteW(ctypes.byref(cred), 0):
        raise _error(ctypes.get_last_error())


def saved_user(server):
    """The user name saved for \\\\server in Credential Manager, or None."""
    cred = ctypes.POINTER(CREDENTIALW)()
    if not _advapi32.CredReadW(server, CRED_TYPE_DOMAIN_PASSWORD, 0, ctypes.byref(cred)):
        return None
    try:
        return cred.contents.UserName
    finally:
        _advapi32.CredFree(cred)


def forget_credentials(server):
    if not _advapi32.CredDeleteW(server, CRED_TYPE_DOMAIN_PASSWORD, 0):
        err = ctypes.get_last_error()
        if err != ERROR_NOT_FOUND:
            raise _error(err)


def port_open(host, port=445, timeout=1.5):
    try:
        with socket.create_connection((str(host), port), timeout=timeout):
            return True
    except OSError:
        return False
