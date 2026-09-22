# connect-app

A network link between a Mac and this Windows PC over a plain USB-C cable (no Thunderbolt needed), so you
can copy files with Explorer and Finder.

When an Apple Silicon Mac is plugged into a PC's USB-C port, it shows up as a USB network adapter
(CDC-NCM, VID `05AC` PID `1905`). Windows' built-in driver can't start it, because the Mac doesn't group its
interfaces the way Windows expects. This app drives the device directly through WinUSB and connects it to a
virtual network adapter ([Wintun](https://www.wintun.net)).

## One-time setup

1. **Python 3.10+** on Windows (only the standard library is used).
2. **WinUSB for the Mac's USB device**: with the Mac plugged in, run [Zadig](https://zadig.akeo.ie) →
   *Options* → tick *List All Devices*, untick *Ignore Hubs or Composite Parents* → pick
   **Mac (Composite Parent)** (USB ID `05AC 1905`) → **WinUSB** → *Replace Driver*.
   If the Mac then shows as "Unknown USB Device", unplug and replug the cable.
3. **On the Mac**: System Settings → General → Sharing → turn on **File Sharing**. If Windows rejects your
   password later, open File Sharing's ⓘ → *Options…* and tick your account under *Windows File Sharing*.

## Use

1. Plug in the cable and double-click **`Connect App.pyw`** (approve the admin prompt; the bridge creates
   a network adapter called *Mac USB Link*). The bridge runs while the window is open.
2. The Mac gets `10.77.0.2` automatically; this PC is `10.77.0.1`.
3. On the **Shared folders** tab, sign in with your Mac account name and password. Tick *Remember* to store
   them in Windows Credential Manager, so Explorer opens the Mac without asking. Double-click a folder to open
   it in Explorer and copy files either way.

The **Mac setup** tab lists the one-time steps on the Mac and ticks them off as they work; **Connection
settings** changes the addresses. Prefer a console? `Start bridge.cmd` runs the same bridge without a window
(Ctrl+C stops it); then open **`\\10.77.0.2`** in Explorer yourself. Only one of the two can run at a time.

Unplugging is fine: *Mac USB Link* shows as disconnected, and the bridge reconnects by itself when you plug
the cable back in (the Mac gets the same address again). If the Mac stops responding without being unplugged,
the bridge notices within 5 seconds of trying to send and reconnects.

Speed is limited by the cable: USB 2.0 cables top out around 35–40 MB/s. The Mac supports 10 Gbps in this
mode, so a USB 3.x cable should be much faster.

Options: `python bridge.py --windows-ip 10.77.0.1 --mac-ip 10.77.0.2 --prefix 24`. Log: `logs\bridge.log`.

## Diagnostics and tests

`python probe.py` brings up both of the Mac's USB network functions for 40 seconds and reports what the Mac
sends, whether it answers pings, and which TCP ports are reachable. It doesn't need admin rights (stop the
bridge first; only one program can use the Mac's USB device at a time).

`python -m unittest discover -s tests` runs the tests (no Mac or admin rights needed).
`pythonw "Connect App.pyw" --preview` shows the window without starting the bridge.

## Undo

Device Manager → *Universal Serial Bus devices* → **Mac** → *Uninstall device*, tick *Attempt to remove the
driver for this device*, then replug the cable. The Wintun adapter only exists while the bridge runs.

## Layout

| Path | What it is |
|---|---|
| `Connect App.pyw` | The app (window) |
| `bridge.py`, `Start bridge.cmd` | The same bridge from a console |
| `probe.py` | Diagnostic probe |
| `connect_app/gui.py` | The window (Tkinter) |
| `connect_app/service.py` | The bridge (USB network function ⇄ Wintun adapter, DHCP/ARP/NDP) and the service that reconnects it |
| `connect_app/smb.py` | Listing the Mac's shared folders, signing in, Credential Manager |
| `connect_app/settings.py`, `winapp.py` | Settings file; elevation, single instance, DPI |
| `connect_app/winusb.py` | SetupAPI + WinUSB bindings (ctypes) |
| `connect_app/ncm.py` | CDC-NCM class requests and NTB framing |
| `connect_app/wintun.py` | Wintun + IP Helper bindings |
| `connect_app/dhcp.py` | One-client DHCP server for the Mac |
| `connect_app/packets.py` | Packet building and parsing helpers |
| `vendor/wintun/` | Wintun 0.14.1 `wintun.dll` (amd64, signed by WireGuard LLC) and its license |
| `tests/` | Unit tests |
