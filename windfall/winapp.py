"""Small Windows helpers for the app: elevation, single instance, DPI awareness, message boxes."""

import contextlib
import ctypes
import ctypes.wintypes as wt
import os
import re
import subprocess
import sys
from collections.abc import Sequence

ERROR_ALREADY_EXISTS = 183

_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
_kernel32.CreateMutexW.restype = wt.HANDLE
_kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, wt.BOOL, wt.LPCWSTR]
_kernel32.CloseHandle.argtypes = [wt.HANDLE]
_shell32 = ctypes.WinDLL("shell32", use_last_error=True)
_shell32.ShellExecuteW.restype = ctypes.c_void_p
_shell32.ShellExecuteW.argtypes = [wt.HWND, wt.LPCWSTR, wt.LPCWSTR, wt.LPCWSTR, wt.LPCWSTR, ctypes.c_int]
_user32 = ctypes.WinDLL("user32", use_last_error=True)
_user32.MessageBoxW.argtypes = [wt.HWND, wt.LPCWSTR, wt.LPCWSTR, wt.UINT]


def is_admin() -> bool:
    try:
        return bool(_shell32.IsUserAnAdmin())
    except OSError:
        return False


def relaunch_as_admin(script: str, args: Sequence[str] = (), console: bool = True) -> bool:
    """Start `script` again with administrator rights, in a console that stays open or else with pythonw.exe.
    False if that was refused."""
    exe = sys.executable
    if console:
        command = subprocess.list2cmdline([exe, script, *args])
        program, params = "cmd.exe", f'/k "{command}"'
    else:
        pythonw = os.path.join(os.path.dirname(exe), "pythonw.exe")
        program, params = (pythonw if os.path.exists(pythonw) else exe), subprocess.list2cmdline([script, *args])
    result: int | None = _shell32.ShellExecuteW(None, "runas", program, params, os.path.dirname(script), 1)
    return (result or 0) > 32


def single_instance(name: str = "Local\\WindfallTransfer") -> int | None:
    """A mutex handle if no other bridge is running in this session, else None. Hold it while running."""
    handle: int | None = _kernel32.CreateMutexW(None, False, name)
    if not handle:
        return None  # typically access denied: another (elevated) instance owns it
    if ctypes.get_last_error() == ERROR_ALREADY_EXISTS:
        _kernel32.CloseHandle(handle)
        return None
    return handle


def enable_dpi_awareness() -> None:
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)  # system DPI aware: crisp text when scaled
    except (AttributeError, OSError):
        with contextlib.suppress(AttributeError, OSError):
            _user32.SetProcessDPIAware()


def _program_files() -> str:
    return os.environ.get("PROGRAMW6432") or os.environ.get("PROGRAMFILES") or r"C:\Program Files"


def unpacked_folder() -> str | None:
    """Program Files\\Windfall-Transfer, if the single-file Windfall-Transfer.exe unpacked this copy there."""
    folder = os.environ.get("WINDFALL_UNPACKED")  # set by the launcher
    if not folder:
        return None
    home = os.path.dirname(os.path.normpath(folder))
    expected = os.path.join(_program_files(), "Windfall-Transfer")
    # "Remove from this PC" deletes this folder, so accept nothing else.
    return home if os.path.normcase(home) == os.path.normcase(expected) else None


def legacy_unpacked_folders() -> list[str]:
    """The Program Files folders earlier versions unpacked themselves into, if they hold nothing else."""
    ours = re.compile(r"app-[0-9a-f]{12}(\.unpacking-[0-9a-f]{32})?")
    found = []
    for name in ("Windfall Transfer", "Connect App"):
        folder = os.path.join(_program_files(), name)
        try:
            entries = os.listdir(folder)
        except OSError:
            continue
        if entries and all(ours.fullmatch(entry) for entry in entries):
            found.append(folder)
    return found


def delete_after_exit(folder: str, seconds: int = 5) -> None:
    """Delete a folder shortly after this process exits (its own files stay in use until then)."""
    subprocess.Popen(
        f'cmd.exe /d /c ping -n {seconds + 1} 127.0.0.1 >nul & rmdir /s /q "{folder}"',
        creationflags=subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP,
        close_fds=True,
    )


def set_app_id(app_id: str) -> None:
    with contextlib.suppress(AttributeError, OSError):
        _shell32.SetCurrentProcessExplicitAppUserModelID(ctypes.c_wchar_p(app_id))


def message_box(text: str, title: str = "Windfall Transfer", error: bool = False) -> None:
    _user32.MessageBoxW(None, text, title, 0x10 if error else 0x40)  # MB_ICONERROR / MB_ICONINFORMATION
