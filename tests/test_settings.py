import json
import os
import tempfile
import unittest

from windfall import settings


class SettingsTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.dir.name, "Windfall-Transfer", "settings.json")
        self.legacy = [
            os.path.join(self.dir.name, name, "settings.json") for name in ("Windfall Transfer", "ConnectApp")
        ]

    def tearDown(self):
        self.dir.cleanup()

    def load(self):
        return settings.load(self.path, self.legacy)

    def test_defaults_when_missing(self):
        self.assertEqual(self.load(), settings.DEFAULTS)

    def test_round_trip_keeps_only_known_keys(self):
        values = dict(settings.DEFAULTS, mac_ip="10.77.0.9", mac_user="alex", password="never stored")
        settings.save(values, self.path)
        with open(self.path, encoding="utf-8") as f:
            self.assertNotIn("password", json.load(f))
        self.assertEqual(self.load()["mac_ip"], "10.77.0.9")

    def test_corrupt_file_gives_defaults(self):
        os.makedirs(os.path.dirname(self.path))
        with open(self.path, "w", encoding="utf-8") as f:
            f.write("{not json")
        self.assertEqual(self.load(), settings.DEFAULTS)

    def test_settings_from_earlier_names_carry_over_until_saved_again(self):
        settings.save(dict(settings.DEFAULTS, mac_user="alex"), self.legacy[1])
        self.assertEqual(self.load()["mac_user"], "alex")
        settings.save(dict(settings.DEFAULTS, mac_user="kim"), self.legacy[0])
        self.assertEqual(self.load()["mac_user"], "kim")
        settings.save(dict(settings.DEFAULTS, mac_user="sam"), self.path)
        self.assertEqual(self.load()["mac_user"], "sam")


if __name__ == "__main__":
    unittest.main()
