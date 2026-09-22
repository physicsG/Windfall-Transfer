"""Create .venv with the development tools from pyproject.toml's dev dependency group.

    python tools/setup_dev.py
"""

import subprocess
import sys
import tomllib
import venv
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
VENV = ROOT / ".venv"


def main() -> None:
    if sys.version_info < (3, 13):
        raise SystemExit("run this with Python 3.13 or newer")
    with open(ROOT / "pyproject.toml", "rb") as f:
        requirements: list[str] = tomllib.load(f)["dependency-groups"]["dev"]
    python = VENV / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")
    if not python.exists():
        venv.EnvBuilder(with_pip=True).create(VENV)
    subprocess.run([python, "-m", "pip", "install", "--disable-pip-version-check", "--quiet", *requirements], check=True)
    print(f"{VENV} is ready: {', '.join(requirements)}")


if __name__ == "__main__":
    main()
