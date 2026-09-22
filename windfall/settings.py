"""App settings, kept per user in %APPDATA%\\Windfall Transfer\\settings.json (passwords never go here)."""

import json
import os

DEFAULTS = {
    "windows_ip": "10.77.0.1",
    "mac_ip": "10.77.0.2",
    "prefix": 24,
    "mac_user": "",
    "start_on_launch": True,
    "usb4_mac_ip": "",  # the Mac's address on Thunderbolt/USB4, only if it isn't found automatically
}
APPDATA = os.environ.get("APPDATA") or os.path.expanduser("~")
PATH = os.path.join(APPDATA, "Windfall Transfer", "settings.json")
LEGACY_DIR = os.path.join(APPDATA, "ConnectApp")  # where the app kept settings when it was called Connect App
LEGACY_PATH = os.path.join(LEGACY_DIR, "settings.json")


def load(path=PATH, legacy=LEGACY_PATH):
    """The saved settings; until there are any, the ones saved under the app's earlier name."""
    settings = dict(DEFAULTS)
    for candidate in (path, legacy):
        if not candidate:
            continue
        try:
            with open(candidate, encoding="utf-8") as f:
                stored = json.load(f)
            settings.update({key: value for key, value in stored.items() if key in DEFAULTS})
            break
        except (OSError, ValueError, AttributeError):
            continue
    return settings


def save(settings, path=PATH):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump({key: settings[key] for key in DEFAULTS if key in settings}, f, indent=2)
