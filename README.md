# Windfall Transfer

Connect a Mac to a Windows PC with a USB-C cable and copy files with Explorer and Finder.

- **Regular USB-C cable**: an Apple silicon Mac shows up as a USB network adapter (CDC-NCM, VID `05AC` PID `1905`)
  that Windows' own driver can't start. Windfall Transfer drives it through WinUSB and connects it to a
  [Wintun](https://www.wintun.net) adapter: this PC is `10.77.0.1`, the Mac gets `10.77.0.2`. A USB 2.0 cable
  reaches about 35–40 MB/s.
- **Thunderbolt / USB4 cable** in the PC's Thunderbolt port: Windows and macOS set up the link themselves
  (10–20+ Gbps). Windfall Transfer finds the Mac on it and prefers it when both links are up.

## Use it

1. Run `Windfall Transfer.exe` (from `dist\`, see below) and allow the administrator prompt. It isn't code-signed:
   if SmartScreen warns, choose *More info* → *Run anyway*.
2. Plug in the Mac, awake and unlocked. With a regular USB-C cable, click **Set up this PC** once when asked: it
   gives the Mac's USB device Windows' built-in WinUSB driver.
3. On the Mac, turn on System Settings → General → Sharing → **File Sharing**. If Windows rejects your password,
   open File Sharing's ⓘ → *Options…* and tick your account under *Windows File Sharing*.
4. Sign in on the **Shared folders** tab with your Mac account, then double-click a folder to open it in Explorer.

To undo everything: **This PC** tab → **Remove from this PC…**, then delete the exe.

Needs 64-bit Windows 10/11, and an Apple silicon Mac for USB cables (any Thunderbolt Mac for Thunderbolt/USB4).

## Build it

With a 64-bit [python.org](https://www.python.org) Python 3.13:

```
python tools\build.py
```

This writes `dist\Windfall Transfer.exe`, the one file to distribute: a launcher with the app, Wintun and a trimmed
private copy of that Python embedded, unpacked to `C:\Program Files\Windfall Transfer` on first start.

## Develop

- Run from source: `python "Windfall Transfer.pyw"`, or `pythonw "Windfall Transfer.pyw" --preview` for the window
  without the bridge.
- Tests: `python -m unittest discover -s tests` (no Mac or admin rights needed).
- `python probe.py` brings up the Mac's USB network functions for 40 seconds and reports what the Mac sends. Close
  the app first: only one program can use the Mac's USB device at a time.
- `python bridge.py` (or `Start bridge.cmd`) runs the bridge in a console.

`vendor/wintun/` holds Wintun 0.14.1's `wintun.dll` (amd64, signed by WireGuard LLC) and its license.
