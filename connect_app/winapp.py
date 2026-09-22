"""Small Windows helpers for the app: elevation, single instance, DPI awareness, message boxes."""

import ctypes
import ctypes.wintypes as wt
import os
import subprocess
import sys

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


def is_admin():
    try:
        return bool(_shell32.IsUserAnAdmin())
    except OSError:
        return False


def relaunch_as_admin(script, args=(), console=True):
    """Start `script` again with administrator rights (Windows asks the user). False if that was refused.

    console=True runs it in a console that stays open afterwards (for the command-line bridge);
    console=False uses pythonw.exe, for the windowed app.
    """
    exe = sys.executable
    if console:
        command = subprocess.list2cmdline([exe, script, *args])
        program, params = "cmd.exe", f'/k "{command}"'
    else:
        pythonw = os.path.join(os.path.dirname(exe), "pythonw.exe")
        program, params = (pythonw if os.path.exists(pythonw) else exe), subprocess.list2cmdline([script, *args])
    result = _shell32.ShellExecuteW(None, "runas", program, params, os.path.dirname(script), 1)
    return (result or 0) > 32


def single_instance(name="Local\\ConnectAppBridge"):
    """A mutex handle if no other bridge is running in this session, else None. Hold it while running."""
    handle = _kernel32.CreateMutexW(None, False, name)
    if not handle:
        return None  # typically access denied: another (elevated) instance owns it
    if ctypes.get_last_error() == ERROR_ALREADY_EXISTS:
        _kernel32.CloseHandle(handle)
        return None
    return handle


def enable_dpi_awareness():
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)  # system DPI aware: crisp text at 150% scaling etc.
    except (AttributeError, OSError):
        try:
            _user32.SetProcessDPIAware()
        except (AttributeError, OSError):
            pass


def unpacked_folder():
    """Program Files\\Connect App, if this copy was unpacked there by the single-file Connect App.exe."""
    folder = os.environ.get("CONNECTAPP_UNPACKED")  # set by the launcher
    if not folder:
        return None
    home = os.path.dirname(os.path.normpath(folder))
    program_files = os.environ.get("ProgramW6432") or os.environ.get("ProgramFiles") or r"C:\Program Files"
    expected = os.path.join(program_files, "Connect App")
    return home if os.path.normcase(home) == os.path.normcase(expected) else None  # never anything else


def delete_after_exit(folder, seconds=5):
    """Delete a folder shortly after this process exits (its own files stay in use until then)."""
    subprocess.Popen(f'cmd.exe /d /c ping -n {seconds + 1} 127.0.0.1 >nul & rmdir /s /q "{folder}"',
                     creationflags=subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP,
                     close_fds=True)


def set_app_id(app_id):
    try:
        _shell32.SetCurrentProcessExplicitAppUserModelID(ctypes.c_wchar_p(app_id))
    except (AttributeError, OSError):
        pass


def message_box(text, title="Connect App", error=False):
    _user32.MessageBoxW(None, text, title, 0x10 if error else 0x40)  # MB_ICONERROR / MB_ICONINFORMATION
