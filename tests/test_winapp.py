import os
import tempfile
import unittest
from unittest import mock

from windfall import winapp


class UnpackedFolderTests(unittest.TestCase):
    def unpacked(self, folder, program_files=r"C:\Program Files"):
        with mock.patch.dict(os.environ, {"ProgramW6432": program_files}):
            os.environ.pop("WINDFALL_UNPACKED", None)
            if folder is not None:
                os.environ["WINDFALL_UNPACKED"] = folder
            return winapp.unpacked_folder()

    def test_only_ever_program_files_windfall(self):
        self.assertEqual(
            self.unpacked(r"C:\Program Files\Windfall-Transfer\app-0123456789ab"), r"C:\Program Files\Windfall-Transfer"
        )
        self.assertEqual(
            self.unpacked(r"c:\program files\windfall-transfer\app-0123456789ab\\"),
            r"c:\program files\windfall-transfer",
        )
        self.assertIsNone(self.unpacked(r"C:\Users\me\AppData\Local\Temp\Windfall-Transfer\app-0123456789ab"))
        self.assertIsNone(self.unpacked(r"C:\Program Files\Windfall Transfer\app-0123456789ab"))  # v1.0.0's
        self.assertIsNone(self.unpacked(r"C:\Program Files\Other\app-0123456789ab"))
        self.assertIsNone(self.unpacked(r"C:\Program Files\app-0123456789ab"))
        self.assertIsNone(self.unpacked(None))

    def test_earlier_unpacked_copies_are_only_ours_if_they_hold_nothing_else(self):
        with (
            tempfile.TemporaryDirectory() as program_files,
            mock.patch.dict(os.environ, {"ProgramW6432": program_files}),
        ):
            self.assertEqual(winapp.legacy_unpacked_folders(), [])  # not there
            folders = [os.path.join(program_files, name) for name in ("Windfall Transfer", "Connect App")]
            for folder in folders:
                os.makedirs(os.path.join(folder, "app-0123456789ab"))
            self.assertEqual(winapp.legacy_unpacked_folders(), folders)
            os.makedirs(os.path.join(folders[1], "Plugins"))  # someone else's "Connect App": leave it alone
            self.assertEqual(winapp.legacy_unpacked_folders(), folders[:1])


if __name__ == "__main__":
    unittest.main()
