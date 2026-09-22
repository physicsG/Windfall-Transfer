"""Connect App: double-click to connect this PC to the Mac over the USB-C cable (asks for administrator rights)."""

import os
import sys
import traceback

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from connect_app import winapp  # noqa: E402

try:
    from connect_app.gui import main

    main()
except Exception:
    winapp.message_box("Connect App couldn't start:\n\n" + traceback.format_exc(limit=4), error=True)
