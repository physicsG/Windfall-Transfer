"""The window's logic with fake bridge and Thunderbolt/USB4 states (the window stays hidden)."""

import os
import tempfile
import tkinter as tk
import unittest
from unittest import mock

from connect_app import driver, gui, smb
from connect_app import settings as app_settings
from connect_app.service import BridgeService
from connect_app.usb4 import Usb4Monitor

USB4_MAC = "169.254.21.56"


class FakeService:
    def __init__(self, state, mac_configured=False):
        self.state = state
        self.mac_configured = mac_configured
        self.mac_ip = "10.77.0.2"
        self.rates = (12.3, 0.4)
        self.error = None
        self.waiting_reason = None
        self.running = state != BridgeService.STOPPED


class FakeUsb4:
    def __init__(self):
        self.set(Usb4Monitor.ABSENT)
        self.manual = None

    def set(self, state, mac_ip=None):
        self.state, self.mac_ip, self.mac_name = state, mac_ip, None
        self.link_speed = 20_000_000_000 if state in (Usb4Monitor.SEARCHING, Usb4Monitor.FOUND) else 0
        self.rates, self.adapter, self.local_ip = (512.0, 1.5), None, None

    def set_manual_address(self, ip):
        self.manual = ip

    def stop(self, timeout=None):
        pass


class GuiTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.saved = {}  # address -> saved user
        patches = [mock.patch.object(app_settings, "load", return_value=dict(app_settings.DEFAULTS)),
                   mock.patch.object(app_settings, "save"),
                   mock.patch.object(smb, "saved_user", side_effect=lambda ip: self.saved.get(ip)),
                   mock.patch.object(gui, "LOG_FILE", os.path.join(self.dir.name, "bridge.log"))]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        try:
            self.root = tk.Tk()
        except tk.TclError as e:
            self.skipTest(f"no display: {e}")
        self.root.withdraw()
        self.usb4 = FakeUsb4()
        self.app = gui.App(self.root, start_bridge=False, usb4=self.usb4)

    def tearDown(self):
        if not self.app.destroyed:
            self.app._destroy()
        self.dir.cleanup()

    def show(self, service=None):
        self.app.service = service
        self.app._refresh()
        return self.app.status_var.get(), self.app.detail_var.get()

    def test_status_follows_the_bridge(self):
        self.assertEqual(self.show()[0], "Bridge stopped")
        self.assertEqual(self.show(FakeService(BridgeService.WAITING))[0], "Waiting for the Mac")
        self.assertEqual(self.show(FakeService(BridgeService.CONNECTED))[0], "Mac connected")
        status, detail = self.show(FakeService(BridgeService.CONNECTED, mac_configured=True))
        self.assertEqual(status, "Connected over USB to the Mac at 10.77.0.2")
        self.assertIn("12.3 MB/s", detail)
        self.assertEqual(str(self.app.open_button["state"]), "normal")
        self.assertEqual(self.app.step_marks["address"]["text"], gui.DONE)

    def test_sign_in_flow(self):
        self.show(FakeService(BridgeService.CONNECTED, mac_configured=True))
        self.app._port_checked("10.77.0.2", True)
        self.assertIn("Enter your Mac account name", self.app.folders_var.get())
        self.app._listed(smb.SmbError(1326, "Wrong user name or password."), "10.77.0.2", "gordian", True, True)
        self.assertEqual(self.app.account_var.get(), "Wrong user name or password.")
        self.app._refresh()
        self.assertEqual(self.app.step_marks["account"]["text"], gui.FAIL)

        self.app.password_var.set("secret")
        self.app._listed([("Public", ""), ("gordian", "Home folder")], "10.77.0.2", "gordian", True, True)
        self.assertEqual(self.app.tree.get_children(), ("Public", "gordian"))
        self.assertEqual(self.app.password_var.get(), "", "password field is cleared after signing in")
        self.assertIn("saved in Windows Credential Manager", self.app.account_var.get())
        self.app._refresh()
        self.assertEqual(self.app.step_marks["folders"]["text"], gui.DONE)
        self.assertEqual(self.app.settings["mac_user"], "gordian")

    def test_disconnect_clears_the_folder_list(self):
        self.show(FakeService(BridgeService.CONNECTED, mac_configured=True))
        self.app._listed([("Public", "")], "10.77.0.2", "gordian", True, False)
        self.show(FakeService(BridgeService.WAITING))
        self.assertEqual(self.app.tree.get_children(), ())
        self.assertEqual(str(self.app.open_button["state"]), "disabled")

    def test_thunderbolt_link_is_preferred_over_the_bridge(self):
        usb = FakeService(BridgeService.CONNECTED, mac_configured=True)
        self.show(usb)
        self.app._listed([("Public", "")], "10.77.0.2", "gordian", True, False)
        self.saved[USB4_MAC] = "gordian"
        self.usb4.set(Usb4Monitor.FOUND, USB4_MAC)
        status, detail = self.show(usb)
        self.assertEqual(status, f"Connected over Thunderbolt/USB4 to the Mac at {USB4_MAC}")
        self.assertIn("20 Gbps link", detail)
        self.assertIn("512.0 MB/s", detail)
        self.assertEqual(self.app.target_ip, USB4_MAC)
        self.assertEqual(self.app.tree.get_children(), (), "folders are listed again for the new connection")
        self.assertEqual(self.app.account_var.get(), "Password saved for gordian.")
        self.assertIn("Thunderbolt/USB4 cable", self.app.step_notes["cable"]["text"])
        self.assertEqual(self.app._mac_addresses(), [USB4_MAC, "10.77.0.2"])

        self.usb4.set(Usb4Monitor.ABSENT)
        self.assertEqual(self.show(usb)[0], "Connected over USB to the Mac at 10.77.0.2")
        self.assertEqual(self.app.target_ip, "10.77.0.2")

    def test_stale_answers_from_the_previous_connection_are_ignored(self):
        self.show(FakeService(BridgeService.CONNECTED, mac_configured=True))
        self.usb4.set(Usb4Monitor.FOUND, USB4_MAC)
        self.show(FakeService(BridgeService.CONNECTED, mac_configured=True))
        self.app._listed([("Old", "")], "10.77.0.2", "gordian", True, False)
        self.assertEqual(self.app.tree.get_children(), ())

    def test_looking_for_the_mac_on_thunderbolt(self):
        self.usb4.set(Usb4Monitor.SEARCHING)
        self.assertEqual(self.show()[0], "Thunderbolt/USB4 link is up; looking for the Mac...")
        self.assertEqual(self.app.step_marks["address"]["text"], gui.BUSY)
        self.assertIn("looking for the Mac", self.app.usb4_var.get())

    def finish_background_work(self):
        callback, result = self.app.results.get(timeout=5)
        callback(result)

    def test_setting_up_a_mac(self):
        mac = r"USB\VID_05AC&PID_1905\X"
        self.app._setup_checked([driver.MacDevice(mac, 0x1905, True, "usbccgp", "Mac")])
        self.assertEqual(self.show(FakeService(BridgeService.WAITING))[0], "The Mac needs a one-time setup on this PC")
        self.assertEqual(self.app.setup_button.winfo_manager(), "pack")
        self.assertEqual(str(self.app.setup_pc_button["state"]), "normal")
        self.assertIn("needs a one-time setup", self.app.setup_var.get())
        self.app.service = FakeService(BridgeService.STOPPED)  # a stopped bridge is started after setting up
        with mock.patch.object(gui.driver, "install_winusb", return_value=False) as install, \
                mock.patch.object(self.app, "start_bridge") as start:
            self.app.set_up()
            self.finish_background_work()
        install.assert_called_once_with(mac)
        start.assert_called_once()
        self.app._setup_checked([driver.MacDevice(mac, 0x1905, True, "WinUSB", "Mac")])
        self.assertEqual(self.show(FakeService(BridgeService.WAITING))[0], "Waiting for the Mac")
        self.assertEqual(self.app.setup_button.winfo_manager(), "")

    def test_remove_lists_what_it_undoes_and_can_be_cancelled(self):
        left = driver.Leftovers([driver.MacDevice("id", 0x1905, True, "WinUSB", "Mac")], ["oem140.inf"],
                                [("Root", r"USB\VID_05AC&PID_1905 (libwdi autogenerated)")])
        self.app.removing = True
        with mock.patch.object(gui.messagebox, "askyesno", return_value=False) as ask, \
                mock.patch.object(gui.driver, "remove_all") as remove_all:
            self.app._confirm_remove(left)
        text = ask.call_args[0][1]
        for expected in ("standard driver", "oem140.inf", "certificate", "Wintun", "passwords", "settings and logs"):
            self.assertIn(expected, text)
        remove_all.assert_not_called()
        self.assertFalse(self.app.removing)

    def test_settings_are_validated(self):
        self.app.mac_ip_var.set("10.78.0.2")
        self.app.save_settings()
        self.assertTrue(self.app.settings_var.get().startswith("Not saved"))
        self.app.mac_ip_var.set("10.77.0.9")
        self.app.usb4_ip_var.set("not an address")
        self.app.save_settings()
        self.assertTrue(self.app.settings_var.get().startswith("Not saved"))
        self.app.usb4_ip_var.set("169.254.1.2")
        self.app.save_settings()
        self.assertEqual((self.app.settings["mac_ip"], self.app.settings["usb4_mac_ip"]), ("10.77.0.9", "169.254.1.2"))
        self.assertEqual(self.usb4.manual, "169.254.1.2")


if __name__ == "__main__":
    unittest.main()
