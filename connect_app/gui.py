"""Connect App window: runs the bridge and shows the connection, the Mac's shared folders and the Mac-side setup."""

import ipaddress
import logging
import os
import queue
import sys
import threading
import time
import tkinter as tk
import traceback
from tkinter import messagebox, ttk

from . import settings as app_settings
from . import smb, winapp
from .service import ADAPTER_NAME, LOG_FILE, BridgeService

log = logging.getLogger("bridge")

GREEN, AMBER, RED, GREY = "#2e7d32", "#b26a00", "#c62828", "#6b6b6b"
DONE, TODO, FAIL, BUSY = "✓", "–", "✗", "…"
MARK_COLORS = {DONE: GREEN, TODO: GREY, FAIL: RED, BUSY: AMBER}

MAC_STEPS = [
    ("cable", "Connect the Mac",
     "Plug the Mac into this PC with the USB-C cable, and keep it awake and unlocked. "
     "Nothing needs to be installed on the Mac."),
    ("address", "The Mac gets its address",
     "Happens by itself: the bridge hands the Mac {mac_ip}."),
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

    def __init__(self, root, start_bridge=None):
        self.root = root
        self.settings = app_settings.load()
        self.service = None
        self.stopping = False
        self.closing = False
        self.destroyed = False
        self._after_stop = []
        self.log_lines = queue.Queue()
        self.results = queue.Queue()
        self.connected = False
        self.port_open = None  # the Mac's File Sharing answers (None = not checked yet)
        self.port_checking = False
        self.last_port_check = 0.0
        self.shares = None
        self.shares_busy = False
        self.auto_listed = False
        self.sign_in_error = None
        self.signed_in_user = None
        self.saved_user = smb.saved_user(self.mac_ip)
        self._handlers = self._setup_logging()
        self._build()
        root.protocol("WM_DELETE_WINDOW", self.close)
        root.report_callback_exception = self._report_exception
        root.after(self.POLL_MS, self.poll)
        if self.settings["start_on_launch"] if start_bridge is None else start_bridge:
            self.start_bridge()

    @property
    def mac_ip(self):
        if self.service and self.service.running:
            return str(self.service.mac_ip)  # new addresses only apply after a restart
        return self.settings["mac_ip"]

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
        root.title("Connect App - Mac over USB-C")
        self.scale = scale = root.winfo_fpixels("1i") / 96.0
        root.geometry(f"{int(820 * scale)}x{int(620 * scale)}")
        root.minsize(int(680 * scale), int(520 * scale))
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

        notebook = ttk.Notebook(root)
        notebook.pack(fill="both", expand=True, padx=14, pady=(6, 14))
        notebook.add(self._build_shares(notebook), text="Shared folders")
        notebook.add(self._build_steps(notebook), text="Mac setup")
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
        self.folders_var = tk.StringVar(value="Start the bridge and connect the Mac to see its shared folders.")
        ttk.Label(folders, textvariable=self.folders_var, style="Hint.TLabel",
                  wraplength=int(700 * self.scale)).pack(anchor="w", pady=(8, 0))
        return frame

    def _build_steps(self, parent):
        frame = ttk.Frame(parent, padding=12)
        ttk.Label(frame, text="One-time setup on the Mac. The marks update by themselves while the bridge runs.",
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
            description = ttk.Label(body, text=text.format(mac_ip=self.mac_ip), wraplength=int(700 * self.scale))
            description.pack(anchor="w")
            note = ttk.Label(body, style="Hint.TLabel")
            note.pack(anchor="w")
            self.step_marks[key], self.step_notes[key], self.step_texts[key] = mark, note, description
        return frame

    def _build_settings(self, parent):
        frame = ttk.Frame(parent, padding=12)
        form = ttk.LabelFrame(frame, text="Addresses on the cable", padding=10)
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
        self.autostart_var = tk.BooleanVar(value=self.settings["start_on_launch"])
        ttk.Checkbutton(frame, text="Start the bridge when Connect App opens",
                        variable=self.autostart_var).pack(anchor="w", pady=(12, 0))
        row = ttk.Frame(frame)
        row.pack(fill="x", pady=(10, 0))
        ttk.Button(row, text="Save settings", command=self.save_settings).pack(side="left")
        self.settings_var = tk.StringVar()
        ttk.Label(row, textvariable=self.settings_var, style="Hint.TLabel").pack(side="left", padx=(12, 0))

        about = ttk.LabelFrame(frame, text="About this connection", padding=10)
        about.pack(fill="x", pady=(16, 0))
        for text in (f"Network adapter: '{ADAPTER_NAME}', which exists while the bridge runs.",
                     "Speed depends on the cable: a USB 2.0 cable reaches about 35-40 MB/s.",
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

    def _refresh(self):
        service = self.service
        running = bool(service and service.running)
        connected = bool(service and service.state == BridgeService.CONNECTED and service.mac_configured)
        if connected != self.connected:
            self.connected = connected
            self._connection_changed()
        text, detail, color = self._status(service)
        self.status_var.set(text)
        self.detail_var.set(detail)
        self.dot.itemconfigure(self.dot_item, fill=color)
        self.start_button.configure(text="Stop bridge" if running else "Start bridge",
                                    state="disabled" if self.stopping else "normal")
        ready = "normal" if connected else "disabled"
        self.open_button.configure(state=ready)
        self.refresh_button.configure(state=ready)
        self.sign_in_button.configure(state=ready)
        self.open_folder_button.configure(state=ready if self.shares else "disabled")
        self.forget_button.configure(state="normal" if self.saved_user else "disabled")
        self._refresh_steps(service)

    def _status(self, service):
        if self.stopping:
            return "Stopping the bridge...", "", AMBER
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
        if not service.mac_configured:
            return "Mac connected", f"Handing it the address {service.mac_ip}...", AMBER
        to_mac, to_pc = service.rates
        return (f"Connected to the Mac at {service.mac_ip}",
                f"To the Mac {to_mac:.1f} MB/s    To this PC {to_pc:.1f} MB/s", GREEN)

    def _set_step(self, key, mark, note=""):
        self.step_marks[key].configure(text=mark, foreground=MARK_COLORS[mark])
        self.step_notes[key].configure(text=note)

    def _refresh_steps(self, service):
        state = service.state if service else BridgeService.STOPPED
        if state == BridgeService.CONNECTED:
            self._set_step("cable", DONE, "The Mac is connected.")
        elif state == BridgeService.WAITING:
            self._set_step("cable", BUSY, "Waiting for the Mac...")
        else:
            self._set_step("cable", TODO, "Start the bridge first." if state != BridgeService.FAILED else "")
        if self.connected:
            self._set_step("address", DONE, f"The Mac has {service.mac_ip}.")
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
            who = self.signed_in_user or self.saved_user
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
        self._show_shares([])
        if self.connected:
            self.folders_var.set("Checking the Mac's File Sharing...")
        else:
            self.folders_var.set("Connect the Mac to see its shared folders.")

    def _maybe_check_port(self):
        if not self.connected or self.port_checking:
            return
        if time.monotonic() - self.last_port_check < self.PORT_CHECK_SECONDS:
            return
        self.port_checking = True
        self.last_port_check = time.monotonic()
        ip = self.mac_ip
        self._in_background(lambda: smb.port_open(ip), self._port_checked)

    def _port_checked(self, is_open):
        self.port_checking = False
        if not self.connected:
            return
        is_open = is_open is True
        changed = is_open != self.port_open
        self.port_open = is_open
        known_user = self.saved_user or self.signed_in_user
        if is_open and not self.auto_listed and known_user:
            self.auto_listed = True
            self.list_folders()
        elif changed and is_open and self.shares is None:
            self.folders_var.set("File Sharing is on. Enter your Mac account name and password above, "
                                 "then click Sign in.")
        elif changed and not is_open:
            self.folders_var.set("The Mac's File Sharing doesn't answer. See the Mac setup tab.")

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
            messagebox.showerror("Connect App", f"Check the connection settings: {e}")
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
        if not password and user not in (self.saved_user, self.signed_in_user):
            self.account_var.set("Enter the password of your Mac account.")
            return
        self.list_folders(user, password or None, self.save_var.get())

    def list_folders(self, user=None, password=None, save=False):
        if self.shares_busy:
            return
        if not self.connected:
            self.folders_var.set("Connect the Mac first: start the bridge and plug in the cable.")
            return
        self.shares_busy = True
        self.folders_var.set("Asking the Mac for its shared folders...")
        ip = self.mac_ip

        def work():
            if password is not None:
                smb.sign_in(ip, user, password)
                if save:
                    smb.save_credentials(ip, user, password)
            return smb.list_shares(ip)
        self._in_background(work, lambda result: self._listed(result, user, password is not None, save))

    def _listed(self, result, user, used_password, saved):
        self.shares_busy = False
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
            self.signed_in_user = user
            self.password_var.set("")
            self.settings["mac_user"] = user
            app_settings.save(self.settings)
            if saved:
                self.saved_user = user
        who = self.signed_in_user or self.saved_user
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
        path = f"\\\\{self.mac_ip}" + (f"\\{name}" if name else "")
        try:
            os.startfile(path)
        except OSError as e:
            messagebox.showerror("Connect App", f"Couldn't open {path}: {e.strerror or e}")

    def forget(self):
        ip = self.mac_ip
        try:
            smb.forget_credentials(ip)
        except OSError as e:
            self.account_var.set(f"Couldn't remove the saved password: {e.strerror or e}")
            return
        smb.sign_out(ip)
        self.saved_user = self.signed_in_user = None
        self.account_var.set("Removed the saved password. Explorer will ask for it next time.")
        log.info("removed the saved password for \\\\%s", ip)

    def save_settings(self):
        try:
            windows_ip = str(ipaddress.IPv4Address(self.windows_ip_var.get().strip()))
            mac_ip = str(ipaddress.IPv4Address(self.mac_ip_var.get().strip()))
            prefix = int(self.prefix_var.get().strip())
            BridgeService(windows_ip, mac_ip, prefix)  # validates the combination
        except ValueError as e:
            self.settings_var.set(f"Not saved: {e}")
            return
        new = {"windows_ip": windows_ip, "mac_ip": mac_ip, "prefix": prefix}
        changed = any(self.settings[key] != value for key, value in new.items())
        self.settings.update(new, start_on_launch=self.autostart_var.get())
        app_settings.save(self.settings)
        running = bool(self.service and self.service.running)
        self.settings_var.set("Saved." + (" Stop and start the bridge to use the new addresses."
                                          if changed and running else ""))
        if not running:
            self.saved_user = smb.saved_user(self.mac_ip)
            self.step_texts["address"].configure(text=MAC_STEPS[1][2].format(mac_ip=self.mac_ip))

    # ---- closing ----

    def close(self):
        if self.closing:
            return
        self.closing = True
        self.stop_bridge(then=self._destroy)

    def _destroy(self):
        self.destroyed = True
        root_logger = logging.getLogger()
        for handler in self._handlers:
            root_logger.removeHandler(handler)
            handler.close()
        self.root.destroy()

    def _report_exception(self, exc_type, value, tb):
        log.error("unexpected error in the window:\n%s", "".join(traceback.format_exception(exc_type, value, tb)))
        messagebox.showerror("Connect App", f"Something went wrong: {value}\n\nDetails are in the log.")


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    preview = "--preview" in argv  # show the window without admin rights or the bridge (for trying out the UI)
    if not preview and not winapp.is_admin():
        if not winapp.relaunch_as_admin(os.path.abspath(sys.argv[0]), argv, console=False):
            winapp.message_box("Connect App needs administrator rights to create its network adapter.", error=True)
        return
    winapp.enable_dpi_awareness()
    instance = None if preview else winapp.single_instance()  # held until exit
    if not preview and instance is None:
        winapp.message_box("Connect App (or the command-line bridge) is already running.")
        return
    root = tk.Tk()
    App(root, start_bridge=False if preview else None)
    root.mainloop()
