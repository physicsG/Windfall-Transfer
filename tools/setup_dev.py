"""Create .venv with the dev tools, installed only from requirements-dev.txt: exact versions, wheels only, and every
file checked against its hash. --lock rewrites requirements-dev.txt from pyproject.toml's dev dependency group.

python tools/setup_dev.py [--lock]
"""

import argparse
import json
import re
import subprocess
import sys
import tempfile
import tomllib
import urllib.request
import venv
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
VENV = ROOT / ".venv"
LOCK = ROOT / "requirements-dev.txt"
HEADER = "# Written by `python tools/setup_dev.py --lock` from pyproject.toml's dev group. Don't edit by hand.\n"


def wanted() -> list[str]:
    with open(ROOT / "pyproject.toml", "rb") as f:
        requirements: list[str] = tomllib.load(f)["dependency-groups"]["dev"]
    return requirements


def normalize(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def locked() -> dict[str, str]:
    """{name: version} of what requirements-dev.txt pins."""
    pins = re.findall(r"^([A-Za-z0-9._-]+)==(\S+)", LOCK.read_text(encoding="utf-8"), re.MULTILINE)
    return {normalize(name): version for name, version in pins}


def lock() -> None:
    """Resolve the dev group like pip would here, then pin each package with the hashes PyPI lists for its wheels."""
    with tempfile.TemporaryDirectory() as tmp:
        report = Path(tmp) / "report.json"
        pip = [sys.executable, "-m", "pip", "install", "--dry-run", "--ignore-installed", "--only-binary", ":all:"]
        subprocess.run([*pip, "--disable-pip-version-check", "--quiet", "--report", str(report), *wanted()], check=True)
        resolved = json.loads(report.read_text(encoding="utf-8"))["install"]
    pins = sorted((normalize(item["metadata"]["name"]), item["metadata"]["version"]) for item in resolved)
    entries = []
    for name, version in pins:
        with urllib.request.urlopen(f"https://pypi.org/pypi/{name}/{version}/json", timeout=30) as response:
            files = json.load(response)["urls"]
        hashes = sorted(file["digests"]["sha256"] for file in files if file["packagetype"] == "bdist_wheel")
        entries.append(f"{name}=={version} \\\n" + " \\\n".join(f"    --hash=sha256:{digest}" for digest in hashes))
    LOCK.write_text(HEADER + "\n".join(entries) + "\n", encoding="utf-8", newline="\n")
    print(f"wrote {LOCK}: {', '.join(f'{name} {version}' for name, version in pins)}")


def install() -> None:
    pins = locked()
    stale = [req for req in wanted() if pins.get(normalize(req.split("==")[0])) != req.split("==")[-1]]
    if stale:
        raise SystemExit(f"requirements-dev.txt doesn't match pyproject.toml ({', '.join(stale)}): run with --lock")
    python = VENV / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")
    if not python.exists():
        venv.EnvBuilder(with_pip=True).create(VENV)  # pip comes from the Python installation, not the network
    pip = [str(python), "-m", "pip", "install", "--require-hashes", "--only-binary", ":all:", "--no-input"]
    subprocess.run([*pip, "--disable-pip-version-check", "--quiet", "-r", str(LOCK)], check=True)
    print(f"{VENV} is ready: {', '.join(f'{name} {version}' for name, version in sorted(pins.items()))}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Set up .venv with the dev tools.")
    parser.add_argument("--lock", action="store_true", help="rewrite requirements-dev.txt instead")
    if parser.parse_args().lock:
        lock()
    else:
        install()


if __name__ == "__main__":
    main()
