"""App settings, kept per user in %APPDATA%\\Windfall Transfer\\settings.json (passwords never go here)."""

import json
import os
from typing import TypedDict, cast


class Settings(TypedDict):
    windows_ip: str
    mac_ip: str
    prefix: int
    mac_user: str
    start_on_launch: bool
    usb4_mac_ip: str  # the Mac's address on Thunderbolt/USB4, only if it isn't found automatically


DEFAULTS: Settings = {
    "windows_ip": "10.77.0.1",
    "mac_ip": "10.77.0.2",
    "prefix": 24,
    "mac_user": "",
    "start_on_launch": True,
    "usb4_mac_ip": "",
}
APPDATA = os.environ.get("APPDATA") or os.path.expanduser("~")
PATH = os.path.join(APPDATA, "Windfall Transfer", "settings.json")
LEGACY_DIR = os.path.join(APPDATA, "ConnectApp")  # where the app kept settings when it was called Connect App
LEGACY_PATH = os.path.join(LEGACY_DIR, "settings.json")


def load(path: str = PATH, legacy: str | None = LEGACY_PATH) -> Settings:
    """The saved settings; until there are any, the ones saved under the app's earlier name. Values of the wrong type
    keep their defaults."""
    settings: dict[str, object] = dict(DEFAULTS)
    for candidate in (path, legacy):
        if not candidate:
            continue
        try:
            with open(candidate, encoding="utf-8") as f:
                stored = json.load(f)
            settings.update(
                {key: value for key, value in stored.items() if key in settings and type(value) is type(settings[key])}
            )
            break
        except (OSError, ValueError, AttributeError):
            continue
    return cast(Settings, settings)


def save(settings: Settings, path: str = PATH) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump({key: value for key, value in settings.items() if key in DEFAULTS}, f, indent=2)
