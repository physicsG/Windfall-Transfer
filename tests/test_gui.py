"""The window's logic with fake bridge states (the window stays hidden)."""

import os
import tempfile
import tkinter as tk
import unittest
from unittest import mock

from connect_app import gui, smb
from connect_app import settings as app_settings
from connect_app.service import BridgeService


class FakeService:
    def __init__(self, state, mac_configured=False):
        self.state = state
        self.mac_configured = mac_configured
        self.mac_ip = "10.77.0.2"
        self.rates = (12.3, 0.4)
        self.error = None
        self.waiting_reason = None
        self.running = state != BridgeService.STOPPED


class GuiTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        patches = [mock.patch.object(app_settings, "load", return_value=dict(app_settings.DEFAULTS)),
                   mock.patch.object(app_settings, "save"),
                   mock.patch.object(smb, "saved_user", return_value=None),
                   mock.patch.object(gui, "LOG_FILE", os.path.join(self.dir.name, "bridge.log"))]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        try:
            self.root = tk.Tk()
        except tk.TclError as e:
            self.skipTest(f"no display: {e}")
        self.root.withdraw()
        self.app = gui.App(self.root, start_bridge=False)

    def tearDown(self):
        if not self.app.destroyed:
            self.app._destroy()
        self.dir.cleanup()

    def show(self, service):
        self.app.service = service
        self.app._refresh()
        return self.app.status_var.get(), self.app.detail_var.get()

    def test_status_follows_the_bridge(self):
        self.assertEqual(self.show(None)[0], "Bridge stopped")
        self.assertEqual(self.show(FakeService(BridgeService.WAITING))[0], "Waiting for the Mac")
        self.assertEqual(self.show(FakeService(BridgeService.CONNECTED))[0], "Mac connected")
        status, detail = self.show(FakeService(BridgeService.CONNECTED, mac_configured=True))
        self.assertEqual(status, "Connected to the Mac at 10.77.0.2")
        self.assertIn("12.3 MB/s", detail)
        self.assertEqual(str(self.app.open_button["state"]), "normal")
        self.assertEqual(self.app.step_marks["address"]["text"], gui.DONE)

    def test_sign_in_flow(self):
        self.show(FakeService(BridgeService.CONNECTED, mac_configured=True))
        self.app._port_checked(True)
        self.assertIn("Enter your Mac account name", self.app.folders_var.get())
        self.app._listed(smb.SmbError(1326, "Wrong user name or password."), "gordian", True, True)
        self.assertEqual(self.app.account_var.get(), "Wrong user name or password.")
        self.app._refresh()
        self.assertEqual(self.app.step_marks["account"]["text"], gui.FAIL)

        self.app.password_var.set("secret")
        self.app._listed([("Public", ""), ("gordian", "Home folder")], "gordian", True, True)
        self.assertEqual(self.app.tree.get_children(), ("Public", "gordian"))
        self.assertEqual(self.app.password_var.get(), "", "password field is cleared after signing in")
        self.assertIn("saved in Windows Credential Manager", self.app.account_var.get())
        self.app._refresh()
        self.assertEqual(self.app.step_marks["folders"]["text"], gui.DONE)
        self.assertEqual(self.app.settings["mac_user"], "gordian")

    def test_disconnect_clears_the_folder_list(self):
        self.show(FakeService(BridgeService.CONNECTED, mac_configured=True))
        self.app._listed([("Public", "")], "gordian", True, False)
        self.show(FakeService(BridgeService.WAITING))
        self.assertEqual(self.app.tree.get_children(), ())
        self.assertEqual(str(self.app.open_button["state"]), "disabled")

    def test_settings_are_validated(self):
        self.app.mac_ip_var.set("10.78.0.2")
        self.app.save_settings()
        self.assertTrue(self.app.settings_var.get().startswith("Not saved"))
        self.app.mac_ip_var.set("10.77.0.9")
        self.app.save_settings()
        self.assertEqual(self.app.settings["mac_ip"], "10.77.0.9")


if __name__ == "__main__":
    unittest.main()
