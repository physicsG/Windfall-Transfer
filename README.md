# connect-app

Connect a Mac to a Windows PC with a USB-C cable and copy files with Explorer and Finder.

- **Regular USB-C cable** (no Thunderbolt needed): when an Apple silicon Mac is plugged into a PC, it shows up as
  a USB network adapter (CDC-NCM, VID `05AC` PID `1905`). Windows' built-in driver can't start it, because the Mac
  doesn't group its interfaces the way Windows expects. Connect App drives the device itself through WinUSB and
  connects it to a virtual network adapter ([Wintun](https://www.wintun.net)): this PC is `10.77.0.1`, the Mac
  gets `10.77.0.2`. A USB 2.0 cable reaches about 35–40 MB/s.
- **Thunderbolt / USB4 cable** in the PC's Thunderbolt port: Windows and macOS build that network link themselves
  ("USB4(TM) P2P Network Adapter" ⇄ "Thunderbolt Bridge"), at 10–20+ Gbps. Connect App finds the Mac on it and
  prefers it when both links are up.

## Use it (no Python needed)

Copy **`Connect App.exe`** (one file, from `dist\`) to the PC, then:

1. Double-click **`Connect App.exe`** and allow the administrator prompt. The first start of each version unpacks
   the app into `C:\Program Files\Connect App` (a few seconds; only administrators can write there, so nothing
   can tamper with the files the app runs with admin rights). If Windows SmartScreen warns about an unknown app
   (the exe isn't code-signed), choose *More info* → *Run anyway*.
2. Plug in the Mac (awake and unlocked). With a regular USB-C cable, click **Set up this PC** once when the app
   asks: it gives the Mac's USB device Microsoft's own WinUSB driver, which is part of Windows (nothing is
   downloaded, no certificate is added). Thunderbolt/USB4 cables need no setup.
3. On the Mac: System Settings → General → Sharing → **File Sharing**. If Windows rejects your password later,
   open File Sharing's ⓘ → *Options…* and tick your account under *Windows File Sharing*. The **Mac setup** tab
   ticks these steps off as they work.
4. On the **Shared folders** tab, sign in with your Mac account name and password. Tick *Remember* to store them
   in Windows Credential Manager, so Explorer opens the Mac without asking. Double-click a folder to open it.

Unplugging is fine: the bridge reconnects when the cable is back. **Connection settings** changes the addresses
(and takes the Mac's Thunderbolt address if it isn't found automatically).

**To undo everything**: **This PC** tab → **Remove from this PC…**. That restores the Mac's standard USB driver,
removes what Zadig added if it was used earlier (its driver package and certificate), deletes the Wintun driver
unless another app such as Tailscale uses it, forgets saved Mac passwords, deletes the app's settings
(`%APPDATA%\ConnectApp`), logs (`%LOCALAPPDATA%\ConnectApp`) and its unpacked copy in
`C:\Program Files\Connect App`. Then delete `Connect App.exe`.

Needs 64-bit Windows 10/11. Apple silicon Macs for USB cables; any Thunderbolt Mac for Thunderbolt/USB4 cables.

## Build it

On a PC with a 64-bit [python.org](https://www.python.org) Python 3.13:

```
python tools\build.py
```

This writes **`dist\Connect App.exe`** (about 8 MB), the one file to distribute. It first builds the app as a
folder in `build\Connect App\` (about 18 MB: a trimmed private copy of that Python with only the standard-library
modules the app imports, isolated from any other Python on the PC; the app; Wintun; and a small launcher compiled
with the C# compiler that ships with Windows, which asks for administrator rights through its manifest). The
single exe embeds that folder and unpacks it on first start. The build checks both: it loads the app with the
folder's runtime, and unpacks the single exe into a temporary folder and loads the app from there. Nothing is
downloaded, and unchanged sources give the same version, so users don't unpack again.

## Develop

- Run from source: `python "Connect App.pyw"` (asks for admin rights), or `pythonw "Connect App.pyw" --preview`
  to see the window without the bridge.
- Tests: `python -m unittest discover -s tests` (no Mac or admin rights needed).
- `python probe.py` brings up the Mac's USB network functions for 40 seconds and reports what the Mac sends and
  which ports answer. Stop the app first: only one program can use the Mac's USB device at a time.
- `bridge.py` / `Start bridge.cmd` run the same bridge in a console (Ctrl+C stops it).

## Layout

| Path | What it is |
|---|---|
| `Connect App.pyw` | The app (window) |
| `connect_app/gui.py` | The window (Tkinter) |
| `connect_app/service.py` | The bridge (USB network function ⇄ Wintun adapter, DHCP/ARP/NDP) and the service that reconnects it |
| `connect_app/driver.py` | Set up / remove the Mac's USB driver (Microsoft's WinUSB via SetupAPI), and Zadig's leftovers |
| `connect_app/usb4.py` | Thunderbolt/USB4 mode: watches for Windows' USB4 network adapter and finds the Mac on it |
| `connect_app/netinfo.py`, `mdns.py` | Interface/address/neighbor tables (IP Helper); a one-shot Bonjour query |
| `connect_app/smb.py` | Listing the Mac's shared folders, signing in, Credential Manager |
| `connect_app/settings.py`, `winapp.py` | Settings file; elevation, single instance, DPI, taskbar identity |
| `connect_app/winusb.py` | SetupAPI + WinUSB bindings (ctypes) |
| `connect_app/ncm.py` | CDC-NCM class requests and NTB framing |
| `connect_app/wintun.py` | Wintun + IP Helper bindings |
| `connect_app/dhcp.py` | One-client DHCP server for the Mac |
| `connect_app/packets.py` | Packet building and parsing helpers |
| `tools/build.py`, `launcher.cs`, `launcher.manifest` | The self-contained build: folder, then the single exe |
| `bridge.py`, `Start bridge.cmd`, `probe.py` | Console bridge and diagnostic probe |
| `vendor/wintun/` | Wintun 0.14.1 `wintun.dll` (amd64, signed by WireGuard LLC) and its license |
| `tests/` | Unit tests |
