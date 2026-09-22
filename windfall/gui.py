"""Windfall Transfer's window: runs the bridge, watches for a Thunderbolt/USB4 link, shows the Mac's shared folders."""

import ipaddress
import logging
import os
import queue
import shutil
import sys
import threading
import time
import tkinter as tk
import traceback
from tkinter import messagebox, ttk

from . import driver, smb, winapp
from . import settings as app_settings
from .service import ADAPTER_NAME, DATA_DIR, LEGACY_DATA_DIR, LOG_FILE, ROOT, WINTUN_DLL, BridgeService
from .usb4 import Usb4Monitor, speed_text
from .wintun import Wintun

log = logging.getLogger("bridge")

GREEN, AMBER, RED, GREY = "#2e7d32", "#b26a00", "#c62828", "#6b6b6b"
DONE, TODO, FAIL, BUSY = "✓", "–", "✗", "…"
MARK_COLORS = {DONE: GREEN, TODO: GREY, FAIL: RED, BUSY: AMBER}

MAC_STEPS = [
    ("cable", "Connect the Mac",
     "Plug the Mac into this PC with a USB-C cable, and keep it awake and unlocked. A Thunderbolt or USB4 cable in "
     "this PC's Thunderbolt port gives the fastest link; any other USB-C cable works through the bridge."),
    ("address", "The Mac gets its address",
     "Happens by itself: through the bridge the Mac gets {mac_ip}; on Thunderbolt/USB4 it picks its own address "
     "and Windfall Transfer finds it."),
    ("sharing", "Turn on File Sharing",
     "On the Mac: System Settings > General > Sharing > File Sharing."),
    ("account", "Allow your account for Windows",
     "Click the (i) next to File Sharing > Options..., and tick your account under \"Windows File Sharing\" "
     "(the Mac asks for your password). Then sign in on the Shared folders tab with your Mac account name "
     "(Terminal: whoami) and password."),
    ("folders", "Choose folders to share (optional)",
     "In the same (i) panel, add folders under Shared Folders with +. Your home folder is always available "
     "when you sign in with your own account."),
]
USB4_TIP = ("Thunderbolt/USB4 cable, optional: transfers to the Mac may get faster with the MTU of the Mac's "
            "Thunderbolt Bridge set to 9000 (System Settings > Network > Thunderbolt Bridge > Details > Hardware).")


class _QueueHandler(logging.Handler):
    def __init__(self, target):
        super().__init__()
        self.target = target

    def emit(self, record):
        try:
            self.target.put(self.format(record))
        except Exception:
            self.handleError(record)


class App:
    POLL_MS = 400
    PORT_CHECK_SECONDS = 5.0
    SETUP_CHECK_SECONDS = 3.0

    def __init__(self, root, start_bridge=None, usb4=None):
        self.root = root
        self.settings = app_settings.load()
        self.service = None
        self.usb4 = usb4 or Usb4Monitor(self.settings["usb4_mac_ip"])
        self.stopping = False
        self.closing = False
        self.destroyed = False
        self._after_stop = []
        self.log_lines = queue.Queue()
        self.results = queue.Queue()
        self.link = None  # ("usb4" or "usb", the Mac's IP) for the connection in use
        self.port_open = None  # the Mac's File Sharing answers (None = not checked yet)
        self.port_checking = False
        self.last_port_check = 0.0
        self.shares = None
        self.shares_busy = False
        self.auto_listed = False
        self.sign_in_error = None
        self.signed_in = None  # (Mac IP, user) of the session this app opened
        self.saved_user = smb.saved_user(self.settings["mac_ip"])
        self.macs = []  # the Mac's USB device entries on this PC (driver.find_macs)
        self.setup_checking = False
        self.last_setup_check = 0.0
        self.setup_busy = False
        self.removing = False
        self.delete_data_on_exit = False
        self._handlers = self._setup_logging()
        self._build()
        root.protocol("WM_DELETE_WINDOW", self.close)
        root.report_callback_exception = self._report_exception
        root.after(self.POLL_MS, self.poll)
        if usb4 is None:
            self.usb4.start()
        if self.settings["start_on_launch"] if start_bridge is None else start_bridge:
            self.start_bridge()

    @property
    def connected(self):
        return self.link is not None

    @property
    def needs_setup(self):
        """The Mac is plugged in by USB, but its USB device doesn't have the WinUSB driver yet."""
        return any(mac.present and not mac.ready for mac in self.macs)

    @property
    def target_ip(self):
        """Where the Mac is reached: over the connection in use, else at the bridge's address for it."""
        if self.link:
            return self.link[1]
        if self.service and self.service.running:
            return str(self.service.mac_ip)  # new addresses only apply after a restart
        return self.settings["mac_ip"]

    def _known_user(self):
        """The Mac account this app can use right now: a session opened here, or a saved password."""
        if self.signed_in and self.signed_in[0] == self.target_ip:
            return self.signed_in[1]
        return self.saved_user

    def _mac_addresses(self):
        """Every address the Mac is known by (current one first), so a saved password works with either cable."""
        return list(dict.fromkeys(ip for ip in (self.target_ip, self.settings["mac_ip"], self.usb4.mac_ip) if ip))

    # ---- layout ----

    def _setup_logging(self):
        os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)
        formatter = logging.Formatter("%(asctime)s  %(message)s", "%H:%M:%S")
        handlers = [logging.FileHandler(LOG_FILE, encoding="utf-8"), _QueueHandler(self.log_lines)]
        root_logger = logging.getLogger()
        root_logger.setLevel(logging.INFO)
        for handler in handlers:
            handler.setFormatter(formatter)
            root_logger.addHandler(handler)
        return handlers

    def _build(self):
        root = self.root
        root.title("Windfall Transfer - Mac over USB-C")
        icon = os.path.join(ROOT, "app.ico")  # drawn by tools/build.py for the packaged app
        if os.path.exists(icon):
            try:
                root.iconbitmap(default=icon)
            except tk.TclError:
                pass
        self.scale = scale = root.winfo_fpixels("1i") / 96.0
        root.geometry(f"{int(820 * scale)}x{int(640 * scale)}")
        root.minsize(int(680 * scale), int(540 * scale))
        style = ttk.Style(root)
        if "vista" in style.theme_names():
            style.theme_use("vista")
        style.configure("Treeview", rowheight=int(22 * scale))
        style.configure("Status.TLabel", font=("Segoe UI", 12, "bold"))
        style.configure("Step.TLabel", font=("Segoe UI", 10, "bold"))
        style.configure("Hint.TLabel", foreground=GREY)

        header = ttk.Frame(root, padding=(14, 12, 14, 6))
        header.pack(fill="x")
        size = int(14 * scale)
        self.dot = tk.Canvas(header, width=size, height=size, highlightthickness=0, bg=root.cget("bg"))
        self.dot_item = self.dot.create_oval(1, 1, size - 1, size - 1, fill=GREY, outline="")
        self.dot.pack(side="left", padx=(0, 10))
        text = ttk.Frame(header)
        text.pack(side="left", fill="x", expand=True)
        self.status_var = tk.StringVar(value="Bridge stopped")
        ttk.Label(text, textvariable=self.status_var, style="Status.TLabel").pack(anchor="w")
        self.detail_var = tk.StringVar()
        ttk.Label(text, textvariable=self.detail_var, style="Hint.TLabel").pack(anchor="w")
        self.open_button = ttk.Button(header, text="Open Mac in Explorer", command=lambda: self.open_share(None))
        self.open_button.pack(side="right")
        self.start_button = ttk.Button(header, text="Start bridge", command=self.toggle_bridge)
        self.start_button.pack(side="right", padx=(0, 8))
        self.setup_button = ttk.Button(header, text="Set up this PC", command=self.set_up)  # shown when needed

        notebook = ttk.Notebook(root)
        notebook.pack(fill="both", expand=True, padx=14, pady=(6, 14))
        notebook.add(self._build_shares(notebook), text="Shared folders")
        notebook.add(self._build_steps(notebook), text="Mac setup")
        notebook.add(self._build_pc(notebook), text="This PC")
        notebook.add(self._build_settings(notebook), text="Connection settings")
        notebook.add(self._build_log(notebook), text="Log")
        self.notebook = notebook

    def _build_shares(self, parent):
        frame = ttk.Frame(parent, padding=12)
        account = ttk.LabelFrame(frame, text="Mac account", padding=10)
        account.pack(fill="x")
        ttk.Label(account, text="Account name").grid(row=0, column=0, sticky="w")
        self.user_var = tk.StringVar(value=self.saved_user or self.settings["mac_user"])
        ttk.Entry(account, textvariable=self.user_var, width=22).grid(row=0, column=1, sticky="w", padx=(6, 16))
        ttk.Label(account, text="Password").grid(row=0, column=2, sticky="w")
        self.password_var = tk.StringVar()
        self.password_entry = ttk.Entry(account, textvariable=self.password_var, show="•", width=22)
        self.password_entry.grid(row=0, column=3, sticky="w", padx=(6, 0))
        self.password_entry.bind("<Return>", lambda event: self.sign_in())
        self.save_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(account, variable=self.save_var,
                        text="Remember in Windows Credential Manager, so Explorer opens the Mac without asking"
                        ).grid(row=1, column=0, columnspan=4, sticky="w", pady=(8, 0))
        buttons = ttk.Frame(account)
        buttons.grid(row=2, column=0, columnspan=4, sticky="w", pady=(8, 0))
        self.sign_in_button = ttk.Button(buttons, text="Sign in and show folders", command=self.sign_in)
        self.sign_in_button.pack(side="left")
        self.forget_button = ttk.Button(buttons, text="Forget saved password", command=self.forget)
        self.forget_button.pack(side="left", padx=(8, 0))
        self.account_var = tk.StringVar(value=f"Password saved for {self.saved_user}." if self.saved_user else "")
        ttk.Label(account, textvariable=self.account_var, style="Hint.TLabel",
                  wraplength=int(700 * self.scale)).grid(row=3, column=0, columnspan=4, sticky="w", pady=(8, 0))

        folders = ttk.LabelFrame(frame, text="Folders the Mac shares", padding=10)
        folders.pack(fill="both", expand=True, pady=(12, 0))
        tree_frame = ttk.Frame(folders)
        tree_frame.pack(fill="both", expand=True)
        self.tree = ttk.Treeview(tree_frame, columns=("name", "comment"), show="headings", height=6,
                                 selectmode="browse")
        self.tree.heading("name", text="Folder", anchor="w")
        self.tree.heading("comment", text="Description", anchor="w")
        self.tree.column("name", width=int(260 * self.scale), anchor="w")
        self.tree.column("comment", width=int(380 * self.scale), anchor="w")
        scrollbar = ttk.Scrollbar(tree_frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=scrollbar.set)
        self.tree.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")
        self.tree.bind("<Double-1>", lambda event: self.open_selected())
        self.tree.bind("<Return>", lambda event: self.open_selected())
        row = ttk.Frame(folders)
        row.pack(fill="x", pady=(8, 0))
        self.open_folder_button = ttk.Button(row, text="Open in Explorer", command=self.open_selected)
        self.open_folder_button.pack(side="left")
        self.refresh_button = ttk.Button(row, text="Refresh", command=self.list_folders)
        self.refresh_button.pack(side="left", padx=(8, 0))
        self.folders_var = tk.StringVar(value="Connect the Mac to see its shared folders.")
        ttk.Label(folders, textvariable=self.folders_var, style="Hint.TLabel",
                  wraplength=int(700 * self.scale)).pack(anchor="w", pady=(8, 0))
        return frame

    def _build_steps(self, parent):
        frame = ttk.Frame(parent, padding=12)
        ttk.Label(frame, text="One-time setup on the Mac. The marks update by themselves while the Mac is connected.",
                  style="Hint.TLabel").pack(anchor="w", pady=(0, 8))
        self.step_marks, self.step_notes, self.step_texts = {}, {}, {}
        for number, (key, title, text) in enumerate(MAC_STEPS, 1):
            row = ttk.Frame(frame)
            row.pack(fill="x", pady=5)
            mark = ttk.Label(row, text=TODO, width=2, font=("Segoe UI", 12, "bold"), foreground=GREY)
            mark.pack(side="left", anchor="n")
            body = ttk.Frame(row)
            body.pack(side="left", fill="x", expand=True, padx=(6, 0))
            ttk.Label(body, text=f"{number}. {title}", style="Step.TLabel").pack(anchor="w")
            description = ttk.Label(body, text=text.format(mac_ip=self.settings["mac_ip"]),
                                    wraplength=int(700 * self.scale))
            description.pack(anchor="w")
            note = ttk.Label(body, style="Hint.TLabel")
            note.pack(anchor="w")
            self.step_marks[key], self.step_notes[key], self.step_texts[key] = mark, note, description
        ttk.Label(frame, text=USB4_TIP, style="Hint.TLabel",
                  wraplength=int(730 * self.scale)).pack(anchor="w", pady=(12, 0))
        return frame

    def _build_pc(self, parent):
        frame = ttk.Frame(parent, padding=12)
        usb = ttk.LabelFrame(frame, text="USB cable: the Mac's USB driver", padding=10)
        usb.pack(fill="x")
        self.setup_var = tk.StringVar(value="Checking...")
        ttk.Label(usb, textvariable=self.setup_var, wraplength=int(700 * self.scale)).pack(anchor="w")
        self.setup_pc_button = ttk.Button(usb, text="Set up this PC", command=self.set_up)
        self.setup_pc_button.pack(anchor="w", pady=(8, 0))
        ttk.Label(usb, style="Hint.TLabel", wraplength=int(700 * self.scale),
                  text="Setting up gives the Mac's USB device Microsoft's WinUSB driver, which is part of Windows. "
                       "Nothing is downloaded and no certificate is added. Needed once per Mac, and only for "
                       "USB cables: Thunderbolt/USB4 cables work without it.").pack(anchor="w", pady=(8, 0))

        remove = ttk.LabelFrame(frame, text="Remove Windfall Transfer from this PC", padding=10)
        remove.pack(fill="x", pady=(12, 0))
        ttk.Label(remove, wraplength=int(700 * self.scale),
                  text="Undoes everything Windfall Transfer changed on this PC, and what Zadig added if you used it: "
                       "the Mac's USB driver, Zadig's driver package and certificate, the Wintun network driver "
                       "(unless another app such as Tailscale uses it), saved Mac passwords, and Windfall Transfer's "
                       "settings and logs. Then you can simply delete Windfall Transfer.").pack(anchor="w")
        self.remove_button = ttk.Button(remove, text="Remove from this PC...", command=self.remove_from_pc)
        self.remove_button.pack(anchor="w", pady=(8, 0))
        return frame

    def _build_settings(self, parent):
        frame = ttk.Frame(parent, padding=12)
        form = ttk.LabelFrame(frame, text="USB cable (through the bridge)", padding=10)
        form.pack(fill="x")
        self.windows_ip_var = tk.StringVar(value=self.settings["windows_ip"])
        self.mac_ip_var = tk.StringVar(value=self.settings["mac_ip"])
        self.prefix_var = tk.StringVar(value=str(self.settings["prefix"]))
        rows = [("This PC", self.windows_ip_var, f"address of the '{ADAPTER_NAME}' adapter"),
                ("Mac", self.mac_ip_var, "handed to the Mac automatically"),
                ("Prefix length", self.prefix_var, "24 means 255.255.255.0")]
        for row, (label, var, hint) in enumerate(rows):
            ttk.Label(form, text=label).grid(row=row, column=0, sticky="w", pady=3)
            ttk.Entry(form, textvariable=var, width=18).grid(row=row, column=1, sticky="w", padx=8, pady=3)
            ttk.Label(form, text=hint, style="Hint.TLabel").grid(row=row, column=2, sticky="w", pady=3)
        ttk.Label(form, text="Use addresses that none of your other networks use.",
                  style="Hint.TLabel").grid(row=3, column=0, columnspan=3, sticky="w", pady=(6, 0))

        usb4 = ttk.LabelFrame(frame, text="Thunderbolt / USB4 cable (built into Windows and macOS)", padding=10)
        usb4.pack(fill="x", pady=(12, 0))
        ttk.Label(usb4, text="Windfall Transfer finds the Mac on this link by itself.").grid(
            row=0, column=0, columnspan=3, sticky="w")
        ttk.Label(usb4, text="Mac's address").grid(row=1, column=0, sticky="w", pady=(6, 0))
        self.usb4_ip_var = tk.StringVar(value=self.settings["usb4_mac_ip"])
        ttk.Entry(usb4, textvariable=self.usb4_ip_var, width=18).grid(row=1, column=1, sticky="w", padx=8,
                                                                      pady=(6, 0))
        ttk.Label(usb4, text="only if it isn't found; the Mac shows it under Network > Thunderbolt Bridge",
                  style="Hint.TLabel").grid(row=1, column=2, sticky="w", pady=(6, 0))

        self.autostart_var = tk.BooleanVar(value=self.settings["start_on_launch"])
        ttk.Checkbutton(frame, text="Start the bridge when Windfall Transfer opens",
                        variable=self.autostart_var).pack(anchor="w", pady=(12, 0))
        row = ttk.Frame(frame)
        row.pack(fill="x", pady=(10, 0))
        ttk.Button(row, text="Save settings", command=self.save_settings).pack(side="left")
        self.settings_var = tk.StringVar()
        ttk.Label(row, textvariable=self.settings_var, style="Hint.TLabel").pack(side="left", padx=(12, 0))

        about = ttk.LabelFrame(frame, text="About this connection", padding=10)
        about.pack(fill="x", pady=(16, 0))
        self.usb4_var = tk.StringVar()
        ttk.Label(about, textvariable=self.usb4_var, wraplength=int(700 * self.scale)).pack(anchor="w", pady=1)
        for text in (f"USB bridge adapter: '{ADAPTER_NAME}', which exists while the bridge runs. A USB 2.0 cable "
                     "reaches about 35-40 MB/s.",
                     f"Log file: {LOG_FILE}",
                     f"Settings file: {app_settings.PATH}"):
            ttk.Label(about, text=text, wraplength=int(700 * self.scale)).pack(anchor="w", pady=1)
        return frame

    def _build_log(self, parent):
        frame = ttk.Frame(parent, padding=12)
        self.log_text = tk.Text(frame, height=10, wrap="word", state="disabled", font=("Consolas", 9),
                                relief="flat", borderwidth=0)
        scrollbar = ttk.Scrollbar(frame, orient="vertical", command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=scrollbar.set)
        self.log_text.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")
        return frame

    # ---- periodic refresh ----

    def poll(self):
        while True:
            try:
                callback, result = self.results.get_nowait()
            except queue.Empty:
                break
            callback(result)
            if self.destroyed:
                return
        self._drain_log()
        self._refresh()
        self._maybe_check_port()
        self._maybe_check_setup()
        self.root.after(self.POLL_MS, self.poll)

    def _drain_log(self):
        lines = []
        while True:
            try:
                lines.append(self.log_lines.get_nowait())
            except queue.Empty:
                break
        if not lines:
            return
        self.log_text.configure(state="normal")
        self.log_text.insert("end", "\n".join(lines) + "\n")
        excess = int(self.log_text.index("end-1c").split(".")[0]) - 1000
        if excess > 0:
            self.log_text.delete("1.0", f"{excess + 1}.0")
        self.log_text.configure(state="disabled")
        self.log_text.see("end")

    def _current_link(self):
        """The best connection to the Mac right now: Thunderbolt/USB4 first, then the USB bridge."""
        if self.usb4.state == Usb4Monitor.FOUND and self.usb4.mac_ip:
            return ("usb4", self.usb4.mac_ip)
        service = self.service
        if service and service.state == BridgeService.CONNECTED and service.mac_configured:
            return ("usb", str(service.mac_ip))
        return None

    def _refresh(self):
        service = self.service
        running = bool(service and service.running)
        link = self._current_link()
        if link != self.link:
            self.link = link
            self._connection_changed()
        text, detail, color = self._status(service)
        self.status_var.set(text)
        self.detail_var.set(detail)
        self.dot.itemconfigure(self.dot_item, fill=color)
        self.start_button.configure(text="Stop bridge" if running else "Start bridge",
                                    state="disabled" if self.stopping else "normal")
        ready = "normal" if self.connected else "disabled"
        self.open_button.configure(state=ready)
        self.refresh_button.configure(state=ready)
        self.sign_in_button.configure(state=ready)
        self.open_folder_button.configure(state=ready if self.shares else "disabled")
        self.forget_button.configure(state="normal" if self.saved_user else "disabled")
        self.usb4_var.set(self._usb4_summary())
        busy = self.setup_busy or self.removing
        show_setup = self.needs_setup and not self.connected
        if show_setup != bool(self.setup_button.winfo_manager()):
            if show_setup:
                self.setup_button.pack(side="right", padx=(0, 8))
            else:
                self.setup_button.pack_forget()
        self.setup_button.configure(state="disabled" if busy else "normal")
        self.setup_pc_button.configure(state="normal" if self.needs_setup and not busy else "disabled")
        self.remove_button.configure(state="disabled" if busy or self.stopping else "normal")
        if not busy:
            self.setup_var.set(self._setup_summary())
        self._refresh_steps(service)

    def _setup_summary(self):
        present = [mac for mac in self.macs if mac.present]
        if any(not mac.ready for mac in present):
            return "The Mac is plugged in and needs a one-time setup: click Set up this PC."
        if present:
            hint = "" if self.connected else (" If it doesn't connect within a few seconds, unplug the cable and "
                                              "plug it back in.")
            return "Set up: the Mac's USB connection uses Windows' WinUSB driver." + hint
        if any(mac.ready for mac in self.macs):
            return "Set up. Plug the Mac in with a USB cable to connect."
        return "Plug the Mac in with a USB cable. If it needs the one-time setup, the button below turns on."

    def _status(self, service):
        usb4 = self.usb4
        if self.stopping:
            return "Stopping the bridge...", "", AMBER
        if self.link and self.link[0] == "usb4":
            to_mac, to_pc = usb4.rates
            name = f"{usb4.mac_name}    " if usb4.mac_name else ""
            return (f"Connected over Thunderbolt/USB4 to the Mac at {usb4.mac_ip}",
                    f"{name}{speed_text(usb4.link_speed)} link    To the Mac {to_mac:.1f} MB/s    "
                    f"To this PC {to_pc:.1f} MB/s", GREEN)
        if self.link:
            to_mac, to_pc = service.rates
            return (f"Connected over USB to the Mac at {service.mac_ip}",
                    f"To the Mac {to_mac:.1f} MB/s    To this PC {to_pc:.1f} MB/s", GREEN)
        if usb4.state == Usb4Monitor.SEARCHING:
            return ("Thunderbolt/USB4 link is up; looking for the Mac...",
                    "If it isn't found, enter the Mac's address under Connection settings.", AMBER)
        if self.needs_setup:
            return ("The Mac needs a one-time setup on this PC",
                    "Click Set up this PC: Windows' own WinUSB driver then handles the Mac's USB connection.", AMBER)
        if service is None or service.state == BridgeService.STOPPED:
            return "Bridge stopped", "Click Start bridge to connect to the Mac.", GREY
        if service.state == BridgeService.FAILED:
            return "The bridge stopped with an error", service.error or "", RED
        if service.state == BridgeService.STARTING:
            return "Starting the bridge...", "", AMBER
        if service.state == BridgeService.WAITING:
            if service.waiting_reason and "WinUSB" in service.waiting_reason:
                return "The Mac isn't answering", "Unplug the USB-C cable and plug it back in.", AMBER
            return "Waiting for the Mac", "Plug in the USB-C cable, and keep the Mac awake and unlocked.", AMBER
        return "Mac connected", f"Handing it the address {service.mac_ip}...", AMBER

    def _usb4_summary(self):
        usb4 = self.usb4
        if usb4.state == Usb4Monitor.ABSENT:
            return ("Thunderbolt/USB4 link: none. It appears when a Thunderbolt or USB4 cable connects the Mac to "
                    "this PC's Thunderbolt port.")
        if usb4.state == Usb4Monitor.DOWN:
            return "Thunderbolt/USB4 link: the adapter is there but not connected."
        parts = [f"Thunderbolt/USB4 link: {usb4.adapter.description if usb4.adapter else 'up'}",
                 speed_text(usb4.link_speed)]
        if usb4.local_ip:
            parts.append(f"this PC {usb4.local_ip}")
        parts.append(f"Mac {usb4.mac_ip}" if usb4.mac_ip else "looking for the Mac")
        return ", ".join(parts) + "."

    def _set_step(self, key, mark, note=""):
        self.step_marks[key].configure(text=mark, foreground=MARK_COLORS[mark])
        self.step_notes[key].configure(text=note)

    def _refresh_steps(self, service):
        usb4 = self.usb4
        state = service.state if service else BridgeService.STOPPED
        over_usb4 = bool(self.link and self.link[0] == "usb4")
        if over_usb4:
            self._set_step("cable", DONE, f"Connected with a Thunderbolt/USB4 cable ({speed_text(usb4.link_speed)} "
                                          "link).")
        elif state == BridgeService.CONNECTED:
            self._set_step("cable", DONE, "Connected over USB, through the bridge.")
        elif usb4.state == Usb4Monitor.SEARCHING:
            self._set_step("cable", DONE, "Thunderbolt/USB4 cable connected.")
        elif self.needs_setup:
            self._set_step("cable", BUSY, "The Mac is plugged in; this PC needs its one-time setup (This PC tab).")
        elif state == BridgeService.WAITING:
            self._set_step("cable", BUSY, "Waiting for the Mac...")
        else:
            self._set_step("cable", TODO, "" if state == BridgeService.FAILED else
                           "Start the bridge, or connect a Thunderbolt/USB4 cable.")
        if over_usb4:
            self._set_step("address", DONE, f"The Mac is at {usb4.mac_ip} on Thunderbolt/USB4.")
        elif self.connected:
            self._set_step("address", DONE, f"The Mac has {service.mac_ip}.")
        elif usb4.state == Usb4Monitor.SEARCHING:
            self._set_step("address", BUSY, "Looking for the Mac on Thunderbolt/USB4...")
        elif state == BridgeService.CONNECTED:
            self._set_step("address", BUSY, "Handing out the address...")
        else:
            self._set_step("address", TODO)
        if not self.connected:
            self._set_step("sharing", TODO)
        elif self.port_open:
            self._set_step("sharing", DONE, "File Sharing answers.")
        elif self.port_open is False:
            self._set_step("sharing", FAIL, "File Sharing doesn't answer yet.")
        else:
            self._set_step("sharing", BUSY, "Checking...")
        if self.shares is not None:
            who = self._known_user()
            self._set_step("account", DONE, f"Signed in as {who}." if who else "Signed in.")
        elif self.sign_in_error in smb.AUTH_ERRORS:
            self._set_step("account", FAIL, "Signing in failed; see the Shared folders tab.")
        else:
            self._set_step("account", TODO)
        if self.shares:
            self._set_step("folders", DONE, f"{len(self.shares)} shared folder(s).")
        else:
            self._set_step("folders", TODO, "No shared folders found yet." if self.shares is not None else "")

    def _connection_changed(self):
        self.port_open = None
        self.last_port_check = 0.0
        self.auto_listed = False
        self.shares = None
        self.sign_in_error = None
        self._show_shares([])
        if not self.connected:
            self.folders_var.set("Connect the Mac to see its shared folders.")
            return
        self.saved_user = smb.saved_user(self.target_ip)  # passwords are saved per address
        if self.saved_user:
            self.user_var.set(self.saved_user)
            self.account_var.set(f"Password saved for {self.saved_user}.")
        self.folders_var.set("Checking the Mac's File Sharing...")

    def _maybe_check_port(self):
        if not self.connected or self.port_checking:
            return
        if time.monotonic() - self.last_port_check < self.PORT_CHECK_SECONDS:
            return
        self.port_checking = True
        self.last_port_check = time.monotonic()
        ip = self.target_ip
        self._in_background(lambda: smb.port_open(ip), lambda is_open: self._port_checked(ip, is_open))

    def _port_checked(self, ip, is_open):
        self.port_checking = False
        if not self.connected or ip != self.target_ip:
            return
        is_open = is_open is True
        changed = is_open != self.port_open
        self.port_open = is_open
        if is_open and not self.auto_listed and self._known_user():
            self.auto_listed = True
            self.list_folders()
        elif changed and is_open and self.shares is None:
            self.folders_var.set("File Sharing is on. Enter your Mac account name and password above, "
                                 "then click Sign in.")
        elif changed and not is_open:
            self.folders_var.set("The Mac's File Sharing doesn't answer. See the Mac setup tab.")

    def _maybe_check_setup(self):
        if self.setup_checking or self.setup_busy or self.removing:
            return
        if time.monotonic() - self.last_setup_check < self.SETUP_CHECK_SECONDS:
            return
        self.setup_checking = True
        self.last_setup_check = time.monotonic()
        self._in_background(driver.find_macs, self._setup_checked)

    def _setup_checked(self, macs):
        self.setup_checking = False
        if isinstance(macs, Exception):
            log.debug("checking the Mac's USB driver failed: %s", macs)
            return
        self.macs = macs

    # ---- set up / remove ----

    def set_up(self):
        targets = [mac.instance_id for mac in self.macs if mac.present and not mac.ready]
        if not targets or self.setup_busy or self.removing:
            return
        self.setup_busy = True
        self.setup_var.set("Setting up the Mac's USB driver...")
        log.info("setting up the Mac's USB driver (Microsoft WinUSB) for %s", ", ".join(targets))
        self._in_background(lambda: [driver.install_winusb(target) for target in targets], self._set_up_done)

    def _set_up_done(self, result):
        self.setup_busy = False
        self.last_setup_check = 0.0  # look again right away
        if isinstance(result, Exception):
            log.error("setting up the Mac's USB driver failed: %s", result)
            messagebox.showerror("Windfall Transfer", f"Setting up the Mac's USB driver failed:\n\n{result}")
            return
        log.info("the Mac's USB driver is set up")
        if any(result):
            messagebox.showinfo("Windfall Transfer", "The Mac's USB driver is set up. Windows asks for a restart to "
                                               "finish it.")
        if not (self.service and self.service.running):
            self.start_bridge()

    def remove_from_pc(self):
        if self.removing or self.setup_busy:
            return
        self.removing = True  # blocks set up/remove until the user has answered
        self._in_background(driver.leftovers, self._confirm_remove)

    def _confirm_remove(self, left):
        if isinstance(left, Exception):
            self.removing = False
            messagebox.showerror("Windfall Transfer", f"Couldn't check what to remove:\n\n{left}")
            return
        items = []
        if left.devices:
            items.append("give the Mac's USB connection back to Windows' standard driver")
        if left.packages:
            items.append(f"delete the driver package Zadig added ({', '.join(left.packages)})")
        if left.certificates:
            items.append("delete Zadig's certificate for the Mac from Windows' trusted certificates")
        items += ["delete the Wintun network driver, unless another app (such as Tailscale or WireGuard) uses it",
                  "forget the Mac passwords saved in Windows Credential Manager",
                  "delete Windfall Transfer's settings and logs"]
        unpacked = winapp.unpacked_folder()
        if unpacked:
            items.append(f"delete the copy of Windfall Transfer unpacked in {unpacked}")
        if self._legacy_leftovers():
            items.append("delete what the app left behind under its earlier name, Connect App")
        what = "Windfall Transfer.exe" if unpacked else "its folder"
        bullets = "\n".join(f"• {item}" for item in items)
        text = (f"This undoes what Windfall Transfer changed on this PC:\n\n{bullets}\n\n"
                f"Windfall Transfer closes afterwards, and then you can delete {what}. Continue?")
        if not messagebox.askyesno("Remove Windfall Transfer from this PC", text, icon="warning"):
            self.removing = False
            return
        self.stop_bridge(then=self._remove_now)

    @staticmethod
    def _legacy_leftovers():
        """Folders the app left under its earlier name, Connect App (settings, logs, unpacked copy)."""
        folders = (app_settings.LEGACY_DIR, LEGACY_DATA_DIR, winapp.legacy_unpacked_folder())
        return [folder for folder in folders if folder and os.path.isdir(folder)]

    def _remove_now(self):
        addresses = self._mac_addresses()

        def work():
            done = driver.remove_all()
            try:
                if Wintun(WINTUN_DLL).delete_driver():
                    done.append("deleted the Wintun network driver")
                else:
                    done.append("left the Wintun network driver in place (another app still uses it)")
            except OSError as e:
                done.append(f"left the Wintun network driver in place ({e})")
            for address in addresses:
                smb.forget_credentials(address)
                smb.sign_out(address)
            done.append("forgot the saved Mac passwords")
            return done
        self._in_background(work, self._removed)

    def _removed(self, result):
        self.removing = False
        if isinstance(result, Exception):
            log.error("removing from this PC failed: %s", result)
            messagebox.showerror("Windfall Transfer", f"Removing didn't finish:\n\n{result}\n\nYou can try again.")
            return
        for line in result:
            log.info("removed: %s", line)
        what = "Windfall Transfer.exe" if winapp.unpacked_folder() else "its folder"
        bullets = "\n".join(f"• {line}" for line in result)
        messagebox.showinfo("Windfall Transfer", f"Removed from this PC:\n\n{bullets}\n\n"
                                                 f"Windfall Transfer closes now; you can delete {what}.")
        self.delete_data_on_exit = True
        self.close()

    # ---- actions ----

    def _in_background(self, work, done):
        def run():
            try:
                result = work()
            except Exception as e:  # handed to `done` on the UI thread
                result = e
            self.results.put((done, result))
        threading.Thread(target=run, daemon=True).start()

    def toggle_bridge(self):
        if self.service and self.service.running:
            self.stop_bridge()
        else:
            self.start_bridge()

    def start_bridge(self):
        if self.stopping or (self.service and self.service.running):
            return
        try:
            self.service = BridgeService(self.settings["windows_ip"], self.settings["mac_ip"],
                                         self.settings["prefix"])
        except ValueError as e:
            messagebox.showerror("Windfall Transfer", f"Check the connection settings: {e}")
            return
        self.service.start()

    def stop_bridge(self, then=None):
        if then:
            self._after_stop.append(then)
        service = self.service
        if self.stopping:
            return
        if not service or not service.running:
            self._run_after_stop()
            return
        self.stopping = True

        def stopped(_):
            self.stopping = False
            self._run_after_stop()
        self._in_background(service.stop, stopped)

    def _run_after_stop(self):
        callbacks, self._after_stop = self._after_stop, []
        for callback in callbacks:
            callback()

    def sign_in(self):
        user = self.user_var.get().strip()
        password = self.password_var.get()
        if not user:
            self.account_var.set("Enter your Mac account name (on the Mac, Terminal: whoami).")
            return
        if not password and user != self._known_user():
            self.account_var.set("Enter the password of your Mac account.")
            return
        self.list_folders(user, password or None, self.save_var.get())

    def list_folders(self, user=None, password=None, save=False):
        if self.shares_busy:
            return
        if not self.connected:
            self.folders_var.set("Connect the Mac first: plug in the cable (and start the bridge for a USB cable).")
            return
        self.shares_busy = True
        self.folders_var.set("Asking the Mac for its shared folders...")
        ip = self.target_ip
        save_for = self._mac_addresses() if save else []

        def work():
            if password is not None:
                smb.sign_in(ip, user, password)
                for address in save_for:
                    smb.save_credentials(address, user, password)
            return smb.list_shares(ip)
        self._in_background(work, lambda result: self._listed(result, ip, user, password is not None, save))

    def _listed(self, result, ip, user, used_password, saved):
        self.shares_busy = False
        if ip != self.target_ip:
            return  # the connection changed while the Mac was answering
        if isinstance(result, Exception):
            code = getattr(result, "errno", None)
            message = getattr(result, "strerror", None) or str(result)
            self.sign_in_error = code
            if code in smb.AUTH_ERRORS:
                self.account_var.set(message)
                self.folders_var.set("Sign in with your Mac account to see the folders.")
                if used_password:
                    self.password_entry.focus_set()
            else:
                self.folders_var.set(message)
            log.info("could not list the Mac's shared folders: %s", message)
            return
        self.sign_in_error = None
        if used_password:
            self.signed_in = (ip, user)
            self.password_var.set("")
            self.settings["mac_user"] = user
            app_settings.save(self.settings)
            if saved:
                self.saved_user = user
        who = self._known_user()
        remembered = bool(who and who == self.saved_user)
        self.account_var.set((f"Signed in as {who}." if who else "Signed in.") +
                             (" The password is saved in Windows Credential Manager." if remembered else ""))
        self.shares = result
        self._show_shares(result)
        if result:
            self.folders_var.set(f"{len(result)} shared folder(s). Double-click one to open it in Explorer.")
        else:
            self.folders_var.set("The Mac isn't sharing any folders yet (step 5 on the Mac setup tab).")
        log.info("the Mac shares %d folder(s)", len(result))

    def _show_shares(self, shares):
        self.tree.delete(*self.tree.get_children())
        for name, comment in shares:
            self.tree.insert("", "end", iid=name, values=(name, comment))

    def open_selected(self):
        selection = self.tree.selection()
        if selection:
            self.open_share(selection[0])
        elif self.shares:
            self.folders_var.set("Select a folder first.")

    def open_share(self, name):
        path = f"\\\\{self.target_ip}" + (f"\\{name}" if name else "")
        try:
            os.startfile(path)
        except OSError as e:
            messagebox.showerror("Windfall Transfer", f"Couldn't open {path}: {e.strerror or e}")

    def forget(self):
        addresses = self._mac_addresses()
        try:
            for address in addresses:
                smb.forget_credentials(address)
        except OSError as e:
            self.account_var.set(f"Couldn't remove the saved password: {e.strerror or e}")
            return
        for address in addresses:
            smb.sign_out(address)
        self.saved_user = self.signed_in = None
        self.account_var.set("Removed the saved password. Explorer will ask for it next time.")
        log.info("removed the saved password for %s", ", ".join(addresses))

    def save_settings(self):
        try:
            windows_ip = str(ipaddress.IPv4Address(self.windows_ip_var.get().strip()))
            mac_ip = str(ipaddress.IPv4Address(self.mac_ip_var.get().strip()))
            prefix = int(self.prefix_var.get().strip())
            BridgeService(windows_ip, mac_ip, prefix)  # validates the combination
            usb4_ip = self.usb4_ip_var.get().strip()
            if usb4_ip:
                usb4_ip = str(ipaddress.IPv4Address(usb4_ip))
        except ValueError as e:
            self.settings_var.set(f"Not saved: {e}")
            return
        new = {"windows_ip": windows_ip, "mac_ip": mac_ip, "prefix": prefix}
        changed = any(self.settings[key] != value for key, value in new.items())
        if usb4_ip != self.settings["usb4_mac_ip"]:
            self.usb4.set_manual_address(usb4_ip)
        self.settings.update(new, usb4_mac_ip=usb4_ip, start_on_launch=self.autostart_var.get())
        app_settings.save(self.settings)
        running = bool(self.service and self.service.running)
        self.settings_var.set("Saved." + (" Stop and start the bridge to use the new addresses."
                                          if changed and running else ""))
        self.step_texts["address"].configure(text=MAC_STEPS[1][2].format(mac_ip=mac_ip))

    # ---- closing ----

    def close(self):
        if self.closing:
            return
        self.closing = True
        self.stop_bridge(then=self._destroy)

    def _destroy(self):
        self.destroyed = True
        self.usb4.stop(timeout=1)
        root_logger = logging.getLogger()
        for handler in self._handlers:
            root_logger.removeHandler(handler)
            handler.close()
        if self.delete_data_on_exit:  # after "Remove from this PC", once the log file is closed
            for folder in (os.path.dirname(app_settings.PATH), DATA_DIR, *self._legacy_leftovers()):
                shutil.rmtree(folder, ignore_errors=True)
            unpacked = winapp.unpacked_folder()
            if unpacked:
                winapp.delete_after_exit(unpacked)  # this process runs from there until it exits
        self.root.destroy()

    def _report_exception(self, exc_type, value, tb):
        log.error("unexpected error in the window:\n%s", "".join(traceback.format_exception(exc_type, value, tb)))
        messagebox.showerror("Windfall Transfer", f"Something went wrong: {value}\n\nDetails are in the log.")


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    preview = "--preview" in argv  # show the window without admin rights or the bridge (for trying out the UI)
    if not preview and not winapp.is_admin():
        if not winapp.relaunch_as_admin(os.path.abspath(sys.argv[0]), argv, console=False):
            winapp.message_box("Windfall Transfer needs administrator rights to create its network adapter.",
                               error=True)
        return
    winapp.enable_dpi_awareness()
    winapp.set_app_id("WindfallTransfer.App")  # own taskbar button and icon, not Python's
    instance = None if preview else winapp.single_instance()  # held until exit
    if not preview and instance is None:
        winapp.message_box("Windfall Transfer (or the command-line bridge) is already running.")
        return
    root = tk.Tk()
    App(root, start_bridge=False if preview else None)
    root.mainloop()
