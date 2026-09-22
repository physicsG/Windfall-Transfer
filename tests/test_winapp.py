import os
import unittest
from unittest import mock

from connect_app import winapp


class UnpackedFolderTests(unittest.TestCase):
    def unpacked(self, folder, program_files=r"C:\Program Files"):
        with mock.patch.dict(os.environ, {"ProgramW6432": program_files}):
            os.environ.pop("CONNECTAPP_UNPACKED", None)
            if folder is not None:
                os.environ["CONNECTAPP_UNPACKED"] = folder
            return winapp.unpacked_folder()

    def test_only_ever_program_files_connect_app(self):
        self.assertEqual(self.unpacked(r"C:\Program Files\Connect App\app-0123456789ab"),
                         r"C:\Program Files\Connect App")
        self.assertEqual(self.unpacked(r"c:\program files\connect app\app-0123456789ab\\"),
                         r"c:\program files\connect app")
        self.assertIsNone(self.unpacked(r"C:\Users\me\AppData\Local\Temp\Connect App\app-0123456789ab"))
        self.assertIsNone(self.unpacked(r"C:\Program Files\Other\app-0123456789ab"))
        self.assertIsNone(self.unpacked(r"C:\Program Files\app-0123456789ab"))
        self.assertIsNone(self.unpacked(None))


if __name__ == "__main__":
    unittest.main()
