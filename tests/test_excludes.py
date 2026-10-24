# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 David Maluenda

import os
import tempfile
import unittest
from pathlib import Path

from restic_machine import excludes


class MatchTest(unittest.TestCase):
    def check(self, patterns, path):
        return excludes.excluded_by(path, patterns)

    def test_absolute_paths_and_what_is_under_them(self):
        patterns = [r"C:\Users\me\Big"]
        self.assertEqual(self.check(patterns, r"C:\Users\me\Big"), r"C:\Users\me\Big")
        self.assertEqual(self.check(patterns, r"C:\Users\me\Big\inside\file.iso"), r"C:\Users\me\Big")
        self.assertIsNone(self.check(patterns, r"C:\Users\me\Bigger"))
        self.assertIsNone(self.check(patterns, r"C:\Users\me\big"))  # case matters, as for restic
        self.assertEqual(self.check(["C:/Users/me/Big"], r"C:\Users\me\Big"), "C:/Users/me/Big")

    def test_relative_patterns_match_anywhere(self):
        self.assertTrue(self.check([r"AppData\Local\Temp"], r"C:\Users\me\AppData\Local\Temp\x.dat"))
        self.assertTrue(self.check(["node_modules"], r"C:\code\app\node_modules\left-pad\index.js"))
        self.assertTrue(self.check(["*.tmp"], r"C:\Users\me\Documents\draft.tmp"))
        self.assertIsNone(self.check(["*.tmp"], r"C:\Users\me\Documents\draft.tmpl"))
        self.assertTrue(self.check([r"AppData\Local\Packages\*\LocalState\ext4.vhdx"],
                                   r"C:\Users\me\AppData\Local\Packages\Ubuntu_1\LocalState\ext4.vhdx"))

    def test_double_star_and_negation(self):
        self.assertTrue(self.check([r"C:\Users\**\cache"], r"C:\Users\me\x\y\cache\f"))
        patterns = ["*.log", "!important.log"]
        self.assertIsNone(self.check(patterns, r"C:\logs\important.log"))
        self.assertEqual(self.check(patterns, r"C:\logs\other.log"), "*.log")

    def test_native_paths(self):
        if os.name == "nt":
            self.assertEqual(excludes.native_path("/C/Users/me/a b"), r"C:\Users\me\a b")
            self.assertEqual(excludes.native_path("/C"), "C:\\")
        else:
            self.assertEqual(excludes.native_path("/home/me/a b"), "/home/me/a b")


class FileTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.file = Path(tmp.name) / "excludes.txt"

    def test_adding_and_removing_keeps_the_file_as_it_was(self):
        original = b"# caches\r\nAppData\\Local\\Temp\r\n*.tmp"  # CRLF, no BOM, no final newline
        self.file.write_bytes(original)
        excludes.add(self.file, r"C:\Users\me\Big")
        text = self.file.read_bytes()
        self.assertTrue(text.startswith(original + b"\r\n\r\n# added by restic-machine on "))
        self.assertTrue(text.endswith(b"\r\nC:\\Users\\me\\Big\r\n"))
        self.assertNotIn(b"\n\n", text.replace(b"\r\n", b"\r"))  # no bare LF anywhere
        self.assertEqual(excludes.read(self.file), [r"AppData\Local\Temp", "*.tmp", r"C:\Users\me\Big"])
        self.assertEqual(self.file.with_name("excludes.txt.bak").read_bytes(), original)
        excludes.add(self.file, r"C:\Users\me\Big")  # already there: nothing changes
        self.assertEqual(excludes.read(self.file).count(r"C:\Users\me\Big"), 1)
        self.assertTrue(excludes.remove(self.file, r"C:\Users\me\Big"))
        self.assertEqual(self.file.read_bytes(), original + b"\r\n")
        self.assertFalse(excludes.remove(self.file, "not there"))

    def test_a_new_file(self):
        excludes.add(self.file, "/home/me/big")
        self.assertEqual(excludes.read(self.file), ["/home/me/big"])


if __name__ == "__main__":
    unittest.main()
