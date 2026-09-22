"""Build a self-contained Windfall Transfer that runs on any 64-bit Windows 10/11 PC without Python installed:

    dist/Windfall Transfer.exe    the one file to distribute: it carries the folder below and unpacks it on first start
    build/Windfall Transfer/      the same app as a folder (Windfall Transfer.exe + runtime/ + app/), built first

    python tools/build.py

Nothing is downloaded. The Python runtime is a trimmed copy of the one running this script (a python.org 3.x,
64-bit install): only the standard-library modules the app imports, compiled into a zip. The launcher is compiled
with the C# compiler that ships with Windows (.NET Framework 4), and the icon is drawn here.
"""

import hashlib
import modulefinder
import os
import py_compile
import shutil
import struct
import subprocess
import sys
import tempfile
import zipfile
import zlib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TOOLS = ROOT / "tools"
STAGE = ROOT / "build" / "Windfall Transfer"
SINGLE = ROOT / "dist" / "Windfall Transfer.exe"
OLD_OUTPUTS = [ROOT / "dist" / "Connect App.exe", ROOT / "dist" / "Connect App", ROOT / "dist" / "Connect App.zip",
               ROOT / "build" / "Connect App"]  # what builds made before the app was renamed to Windfall Transfer
FIXED_TIME = (2020, 1, 1, 0, 0, 0)  # zip entry dates, so unchanged builds get the same payload ID
RUNTIME = STAGE / "runtime"
APP = STAGE / "app"
ENTRY = ROOT / "Windfall Transfer.pyw"
BASE = Path(sys.base_prefix)
STDLIB = BASE / "Lib"
DLLS = BASE / "DLLs"
TAG = f"python{sys.version_info.major}{sys.version_info.minor}"
CSC = Path(os.environ.get("WINDIR", r"C:\Windows")) / "Microsoft.NET" / "Framework64" / "v4.0.30319" / "csc.exe"

WHOLE_PACKAGES = ["encodings", "tkinter"]  # codecs are looked up by name at run time; tkinter's parts load lazily
EXCLUDES = ["unittest", "doctest", "pydoc", "pdb", "test", "idlelib", "tkinter.test", "lib2to3", "ensurepip", "venv",
            "turtle", "turtledemo", "bz2", "lzma", "_bz2", "_lzma", "ssl", "_ssl", "http", "urllib", "email", "xml",
            "xmlrpc", "sqlite3", "asyncio", "multiprocessing", "concurrent"]
if "_sha2" in sys.builtin_module_names:
    EXCLUDES += ["hashlib", "_hashlib"]  # random falls back to hashlib only without the built-in _sha2
RUNTIME_FILES = ["python.exe", "pythonw.exe", f"{TAG}.dll", "python3.dll", "vcruntime140.dll", "vcruntime140_1.dll"]
EXTENSION_DLLS = {"_ctypes": ["libffi-*.dll"], "_tkinter": ["tcl*.dll", "tk*.dll", "zlib1.dll"]}
TCL_FOLDERS = ["tcl8.6", "tk8.6", "tcl8"]
TCL_SKIP = shutil.ignore_patterns("demos", "tzdata")

README = """Windfall Transfer: your Mac and this PC over a USB-C cable
==========================================================

1. Double-click "Windfall Transfer.exe" and allow the administrator prompt.
2. Plug in the Mac (keep it awake and unlocked).
   - Regular USB-C cable: click "Set up this PC" once when Windfall Transfer asks for it.
   - Thunderbolt/USB4 cable in this PC's Thunderbolt port: nothing to set up.
3. On the Mac, turn on File Sharing (the "Mac setup" tab shows how).
4. Sign in on the "Shared folders" tab and open the Mac's folders in Explorer.

To undo everything: "This PC" tab > "Remove from this PC...", then delete this folder.

Needs 64-bit Windows 10 or 11. Includes Python (runtime\\LICENSE.txt), Tcl/Tk (runtime\\tcl\\*\\license.terms)
and Wintun (app\\vendor\\wintun\\LICENSE.txt).
"""


def find_modules():
    """Standard-library sources and extension modules the app needs (plus whole packages in WHOLE_PACKAGES)."""
    finder = modulefinder.ModuleFinder(path=[str(ROOT), str(STDLIB), str(DLLS)], excludes=EXCLUDES)
    finder.run_script(str(ENTRY))
    sources, extensions = {}, set()
    for module in finder.modules.values():
        path = Path(module.__file__) if module.__file__ else None
        if path is None:
            continue  # built into the Python DLL
        if path.suffix == ".pyd":
            extensions.add(path)
        elif path.is_relative_to(STDLIB):
            sources[path.relative_to(STDLIB)] = path
    for package in WHOLE_PACKAGES:
        for path in (STDLIB / package).rglob("*.py"):
            relative = path.relative_to(STDLIB)
            if "test" not in relative.parts and "tests" not in relative.parts:
                sources[relative] = path
    return sources, extensions


def _add(archive, name, data):
    info = zipfile.ZipInfo(name, date_time=FIXED_TIME)
    info.compress_type = zipfile.ZIP_DEFLATED
    archive.writestr(info, data, compresslevel=9)


def write_stdlib_zip(sources):
    with tempfile.TemporaryDirectory() as tmp, zipfile.ZipFile(RUNTIME / f"{TAG}.zip", "w") as archive:
        for relative, path in sorted(sources.items()):
            compiled = Path(tmp) / "module.pyc"
            py_compile.compile(str(path), cfile=str(compiled), dfile=str(relative), doraise=True)
            _add(archive, relative.with_suffix(".pyc").as_posix(), compiled.read_bytes())


def copy_runtime(extensions):
    for name in RUNTIME_FILES:
        shutil.copy2(BASE / name, RUNTIME / name)
    shutil.copy2(BASE / "LICENSE.txt", RUNTIME / "LICENSE.txt")
    for path in sorted(extensions):
        shutil.copy2(path, RUNTIME / path.name)
        for pattern in EXTENSION_DLLS.get(path.stem, []):
            for dll in DLLS.glob(pattern):
                shutil.copy2(dll, RUNTIME / dll.name)
    if any(path.stem == "_tkinter" for path in extensions):
        for folder in TCL_FOLDERS:
            shutil.copytree(BASE / "tcl" / folder, RUNTIME / "tcl" / folder, ignore=TCL_SKIP)
    # Isolated: only these paths, no site-packages, and PYTHONPATH/PYTHONHOME are ignored.
    (RUNTIME / f"{TAG}._pth").write_text(f"{TAG}.zip\n.\n..\\app\n", encoding="utf-8")


def copy_app():
    shutil.copytree(ROOT / "windfall", APP / "windfall", ignore=shutil.ignore_patterns("__pycache__"))
    shutil.copytree(ROOT / "vendor", APP / "vendor")
    shutil.copy2(ENTRY, APP / ENTRY.name)


# ---- icon: two arrows (Mac <-> PC) on a rounded blue square ----

def _draw(size):
    """RGBA rows of the icon at one size, 4x4 supersampled."""
    blue, white, samples = (37, 99, 235), (255, 255, 255), 4

    def in_square(u, v, margin=0.03, radius=0.22):
        x = min(max(u, margin + radius), 1 - margin - radius)
        y = min(max(v, margin + radius), 1 - margin - radius)
        return (u - x) ** 2 + (v - y) ** 2 <= radius ** 2

    def in_arrow(u, v, y0, tail, tip, head=0.19, half_head=0.13, half_shaft=0.05):
        direction = 1 if tip > tail else -1
        base = tip - direction * head
        if min(tail, base) <= u <= max(tail, base) and abs(v - y0) <= half_shaft:
            return True
        t = (u - base) * direction
        return 0 <= t <= head and abs(v - y0) <= half_head * (1 - t / head)

    rows = []
    for y in range(size):
        row = bytearray()
        for x in range(size):
            background = foreground = 0
            for sy in range(samples):
                for sx in range(samples):
                    u, v = (x + (sx + 0.5) / samples) / size, (y + (sy + 0.5) / samples) / size
                    if in_square(u, v):
                        background += 1
                        foreground += in_arrow(u, v, 0.37, 0.20, 0.80) or in_arrow(u, v, 0.63, 0.80, 0.20)
            mix = foreground / background if background else 0
            row += bytes([round(b + (w - b) * mix) for b, w in zip(blue, white)])
            row.append(round(255 * background / samples ** 2))
        rows.append(bytes(row))
    return rows


def _png(size, rows):
    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(b"".join(b"\0" + row for row in rows), 9)) + chunk(b"IEND", b""))


def _dib(size, rows):
    header = struct.pack("<IiiHHIIiiII", 40, size, size * 2, 1, 32, 0, 0, 0, 0, 0, 0)
    pixels = b"".join(bytes(value for i in range(0, len(row), 4)
                            for value in (row[i + 2], row[i + 1], row[i], row[i + 3]))  # RGBA -> BGRA
                      for row in reversed(rows))
    return header + pixels + bytes(((size + 31) // 32) * 4 * size)  # AND mask unused: alpha decides


def write_icon(path, sizes=(16, 20, 24, 32, 40, 48, 64, 256)):
    images = [(_png if size >= 256 else _dib)(size, _draw(size)) for size in sizes]
    offset = 6 + 16 * len(images)
    entries = b""
    for size, data in zip(sizes, images):
        entries += struct.pack("<BBBBHHII", size % 256, size % 256, 0, 0, 1, 32, len(data), offset)
        offset += len(data)
    path.write_bytes(struct.pack("<HHH", 0, 1, len(images)) + entries + b"".join(images))


def compile_launcher(out, icon, manifest, payload=None, payload_id=None):
    """The folder launcher, or with a payload the single-file one (ONEFILE: the payload is embedded)."""
    if not CSC.exists():
        raise SystemExit(f"C# compiler not found at {CSC} (part of Windows' .NET Framework 4)")
    command = [str(CSC), "/nologo", "/target:winexe", "/optimize+", "/platform:x64", f"/win32manifest:{manifest}",
               f"/win32icon:{icon}", f"/out:{out}", "/reference:System.Windows.Forms.dll"]
    sources = [str(TOOLS / "launcher.cs")]
    if payload:
        generated = payload.with_name("payload.cs")
        generated.write_text(f'static class Payload {{ public const string Id = "{payload_id}"; }}\n',
                             encoding="utf-8")
        command += ["/define:ONEFILE", f"/resource:{payload},payload.zip", "/reference:System.IO.Compression.dll",
                    "/reference:System.IO.Compression.FileSystem.dll"]
        sources.append(str(generated))
    subprocess.run(command + sources, check=True)


def smoke_test(folder, label):
    """Import the app with a bundled runtime only (no window), to catch anything missing from the build."""
    code = ("import tkinter, windfall.gui, windfall.driver, windfall.usb4, windfall.service; "
            "print('Python', __import__('sys').version.split()[0], 'Tcl', tkinter.Tcl().eval('info patchlevel'))")
    env = {key: value for key, value in os.environ.items() if not key.startswith(("PYTHON", "TCL_", "TK_"))}
    result = subprocess.run([str(folder / "runtime" / "python.exe"), "-c", code], cwd=folder, env=env,
                            capture_output=True, text=True)
    if result.returncode:
        raise SystemExit(f"{label}: the bundled runtime couldn't load the app\n{result.stderr.strip()}")
    print(f"{label}: OK ({result.stdout.strip()})")


def build_single_exe(icon):
    """Embed runtime/ and app/ in one Windfall Transfer.exe, then check it unpacks and runs like on a fresh PC."""
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        payload = tmp / "payload.zip"
        with zipfile.ZipFile(payload, "w") as archive:
            for folder in (RUNTIME, APP):
                for path in sorted(folder.rglob("*")):
                    if path.is_file():
                        _add(archive, path.relative_to(STAGE).as_posix(), path.read_bytes())
        payload_id = hashlib.sha256(payload.read_bytes()).hexdigest()[:12]
        built = tmp / "Windfall Transfer.exe"
        compile_launcher(built, icon, TOOLS / "launcher.manifest", payload, payload_id)
        # Same launcher without the admin requirement, so the check needs no prompt; it unpacks to a temp folder.
        manifest = tmp / "check.manifest"
        manifest.write_text((TOOLS / "launcher.manifest").read_text(encoding="utf-8")
                            .replace("requireAdministrator", "asInvoker"), encoding="utf-8")
        checker = tmp / "check.exe"
        compile_launcher(checker, icon, manifest, payload, payload_id)
        unpacked = tmp / "unpacked"
        subprocess.run([str(checker), "--extract-only", str(unpacked)], check=True)
        smoke_test(unpacked, "single-file exe, unpacked")
        SINGLE.parent.mkdir(exist_ok=True)
        staged = SINGLE.with_name(SINGLE.name + ".new")  # same drive as the target, so the swap is a rename
        shutil.copy2(built, staged)
        try:
            os.replace(staged, SINGLE)
        except PermissionError:
            staged.unlink()
            raise SystemExit(f"{SINGLE} is in use: close Windfall Transfer and build again") from None
    return payload_id


def remove(path):
    """Delete a file or folder. False if it's in use (for example, Windfall Transfer is running from it)."""
    try:
        if path.is_dir():
            shutil.rmtree(path)
        elif path.exists():
            path.unlink()
        return True
    except PermissionError:
        return False


def main():
    if sys.maxsize <= 2 ** 32 or not (BASE / f"{TAG}.dll").exists():
        raise SystemExit("run this with a 64-bit python.org Python installation")
    if not remove(STAGE):
        raise SystemExit(f"{STAGE} is in use: close Windfall Transfer if it runs from there, then build again")
    RUNTIME.mkdir(parents=True)
    sources, extensions = find_modules()
    write_stdlib_zip(sources)
    copy_runtime(extensions)
    copy_app()
    write_icon(APP / "app.ico")
    compile_launcher(STAGE / "Windfall Transfer.exe", APP / "app.ico", TOOLS / "launcher.manifest")
    (STAGE / "READ ME.txt").write_text(README, encoding="utf-8")
    smoke_test(STAGE, "folder build")
    payload_id = build_single_exe(APP / "app.ico")
    for old in OLD_OUTPUTS:
        if not remove(old):
            print(f"note: {old} (from an earlier build) is in use; delete it once Windfall Transfer is closed")
    size = sum(path.stat().st_size for path in STAGE.rglob("*") if path.is_file())
    print(f"{len(sources)} standard-library modules, {len(extensions)} extension modules: "
          f"{', '.join(sorted(path.name for path in extensions))}")
    print(f"built {STAGE} ({size / 1e6:.1f} MB)")
    print(f"built {SINGLE} ({SINGLE.stat().st_size / 1e6:.1f} MB, version {payload_id}): the file to distribute")


if __name__ == "__main__":
    main()
