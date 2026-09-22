"""Windfall Transfer's entry point."""

import os
import sys
import traceback

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from windfall import winapp

try:
    from windfall.gui import main

    main()
except Exception:
    winapp.message_box("Windfall Transfer couldn't start:\n\n" + traceback.format_exc(limit=4), error=True)
