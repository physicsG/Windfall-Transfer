"""App settings, kept per user in %APPDATA%\\ConnectApp\\settings.json (passwords never go here)."""

import json
import os

DEFAULTS = {
    "windows_ip": "10.77.0.1",
    "mac_ip": "10.77.0.2",
    "prefix": 24,
    "mac_user": "",
    "start_on_launch": True,
}
PATH = os.path.join(os.environ.get("APPDATA") or os.path.expanduser("~"), "ConnectApp", "settings.json")


def load(path=PATH):
    settings = dict(DEFAULTS)
    try:
        with open(path, encoding="utf-8") as f:
            stored = json.load(f)
        settings.update({key: value for key, value in stored.items() if key in DEFAULTS})
    except (OSError, ValueError, AttributeError):
        pass
    return settings


def save(settings, path=PATH):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump({key: settings[key] for key in DEFAULTS if key in settings}, f, indent=2)
