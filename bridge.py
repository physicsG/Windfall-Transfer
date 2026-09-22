"""The bridge in a console, without the window. Ctrl+C stops it."""

import argparse
import logging
import os
import sys
import time

from windfall import winapp
from windfall.service import LOG_FILE, BridgeService

log = logging.getLogger("bridge")


def main():
    parser = argparse.ArgumentParser(description="Network bridge to a Mac over a USB-C cable.")
    parser.add_argument("--windows-ip", default="10.77.0.1", help="this PC's address on the link")
    parser.add_argument("--mac-ip", default="10.77.0.2", help="address handed to the Mac")
    parser.add_argument("--prefix", type=int, default=24, help="subnet prefix length")
    args = parser.parse_args()
    try:
        service = BridgeService(args.windows_ip, args.mac_ip, args.prefix)
    except ValueError as e:
        parser.error(str(e))

    if not winapp.is_admin():
        print("The bridge creates a network adapter, which needs administrator rights. Asking Windows...")
        return 0 if winapp.relaunch_as_admin(os.path.abspath(__file__), sys.argv[1:], console=True) else 1
    instance = winapp.single_instance()  # held until exit
    if instance is None:
        print("The bridge is already running (in Windfall Transfer or another window).")
        return 1

    os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(message)s",
        datefmt="%H:%M:%S",
        handlers=[logging.StreamHandler(sys.stdout), logging.FileHandler(LOG_FILE, encoding="utf-8")],
    )
    log.info("press Ctrl+C to stop; with File Sharing on the Mac, open \\\\%s in Explorer", service.mac_ip)
    service.start()
    try:
        while service.running:
            time.sleep(0.5)  # short sleeps keep Ctrl+C responsive
    except KeyboardInterrupt:
        log.info("stopping...")
        service.stop()
    return 1 if service.state == BridgeService.FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
