import json
import os
import tempfile
import unittest

from connect_app import settings


class SettingsTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.dir.name, "ConnectApp", "settings.json")

    def tearDown(self):
        self.dir.cleanup()

    def test_defaults_when_missing(self):
        self.assertEqual(settings.load(self.path), settings.DEFAULTS)

    def test_round_trip_keeps_only_known_keys(self):
        values = dict(settings.DEFAULTS, mac_ip="10.77.0.9", mac_user="gordian", password="never stored")
        settings.save(values, self.path)
        with open(self.path, encoding="utf-8") as f:
            self.assertNotIn("password", json.load(f))
        self.assertEqual(settings.load(self.path)["mac_ip"], "10.77.0.9")

    def test_corrupt_file_gives_defaults(self):
        os.makedirs(os.path.dirname(self.path))
        with open(self.path, "w", encoding="utf-8") as f:
            f.write("{not json")
        self.assertEqual(settings.load(self.path), settings.DEFAULTS)


if __name__ == "__main__":
    unittest.main()
