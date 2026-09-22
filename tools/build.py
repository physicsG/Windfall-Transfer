"""Build a self-contained Connect App: dist/Connect App/ and dist/Connect App.zip, which run on any 64-bit
Windows 10/11 PC without Python installed.

    python tools/build.py

Nothing is downloaded. The Python runtime is a trimmed copy of the one running this script (a python.org 3.x,
64-bit install): only the standard-library modules the app imports, compiled into a zip. The launcher is compiled
with the C# compiler that ships with Windows (.NET Framework 4), and the icon is drawn here.
"""

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
DIST_PARENT = ROOT / "dist"
DIST = DIST_PARENT / "Connect App"
RUNTIME = DIST / "runtime"
APP = DIST / "app"
ENTRY = ROOT / "Connect App.pyw"
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

README = """Connect App: your Mac and this PC over a USB-C cable
=====================================================

1. Double-click "Connect App.exe" and allow the administrator prompt.
2. Plug in the Mac (keep it awake and unlocked).
   - Regular USB-C cable: click "Set up this PC" once when Connect App asks for it.
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


def write_stdlib_zip(sources):
    with tempfile.TemporaryDirectory() as tmp, zipfile.ZipFile(RUNTIME / f"{TAG}.zip", "w",
                                                                zipfile.ZIP_DEFLATED) as archive:
        for relative, path in sorted(sources.items()):
            compiled = Path(tmp) / "module.pyc"
            py_compile.compile(str(path), cfile=str(compiled), dfile=str(relative), doraise=True)
            archive.write(compiled, relative.with_suffix(".pyc").as_posix())


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
    shutil.copytree(ROOT / "connect_app", APP / "connect_app", ignore=shutil.ignore_patterns("__pycache__"))
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
    pixels = b"".join(bytes(value for i in range(0, len(row), 4) for value in (row[i + 2], row[i + 1], row[i], row[i + 3]))
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


def build_launcher(icon):
    if not CSC.exists():
        raise SystemExit(f"C# compiler not found at {CSC} (part of Windows' .NET Framework 4)")
    subprocess.run([str(CSC), "/nologo", "/target:winexe", "/optimize+",
                    f"/win32manifest:{TOOLS / 'launcher.manifest'}", f"/win32icon:{icon}",
                    f"/out:{DIST / 'Connect App.exe'}", "/reference:System.Windows.Forms.dll",
                    str(TOOLS / "launcher.cs")], check=True)


def smoke_test():
    """Import the app with the bundled runtime only (no window), to catch anything missing from the build."""
    code = ("import tkinter, connect_app.gui, connect_app.driver, connect_app.usb4, connect_app.service; "
            "print('bundled runtime OK: Python', __import__('sys').version.split()[0], "
            "'Tcl', tkinter.Tcl().eval('info patchlevel'))")
    env = {key: value for key, value in os.environ.items() if not key.startswith(("PYTHON", "TCL_", "TK_"))}
    result = subprocess.run([str(RUNTIME / "python.exe"), "-c", code], cwd=DIST, env=env, capture_output=True,
                            text=True)
    print(result.stdout.strip() or result.stderr.strip())
    if result.returncode:
        raise SystemExit("the bundled runtime couldn't load the app")


def main():
    if sys.maxsize <= 2 ** 32 or not (BASE / f"{TAG}.dll").exists():
        raise SystemExit("run this with a 64-bit python.org Python installation")
    if DIST.exists():
        shutil.rmtree(DIST)
    RUNTIME.mkdir(parents=True)
    sources, extensions = find_modules()
    write_stdlib_zip(sources)
    copy_runtime(extensions)
    copy_app()
    write_icon(APP / "app.ico")
    build_launcher(APP / "app.ico")
    (DIST / "READ ME.txt").write_text(README, encoding="utf-8")
    smoke_test()
    archive = shutil.make_archive(str(DIST_PARENT / "Connect App"), "zip", root_dir=DIST_PARENT, base_dir=DIST.name)
    size = sum(path.stat().st_size for path in DIST.rglob("*") if path.is_file())
    print(f"{len(sources)} standard-library modules, {len(extensions)} extension modules: "
          f"{', '.join(sorted(path.name for path in extensions))}")
    print(f"built {DIST} ({size / 1e6:.1f} MB) and {archive} ({Path(archive).stat().st_size / 1e6:.1f} MB)")


if __name__ == "__main__":
    main()
