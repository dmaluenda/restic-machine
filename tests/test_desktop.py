# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 David Maluenda

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from restic_machine import desktop


class DesktopTest(unittest.TestCase):
    def test_output_goes_to_a_log_and_failures_to_a_dialog(self):
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.dict(os.environ, {"RESTIC_MACHINE_DATA": tmp}), \
                mock.patch.object(sys, "stdout"), mock.patch.object(sys, "stderr"), \
                mock.patch.object(desktop, "_alert") as alert, \
                mock.patch.object(desktop.cli, "main", side_effect=lambda argv: print("no repository") or 78) as cli_main:
            code = desktop.main([])
            log = sys.stdout
            log.close()
            self.assertEqual(code, 78)
            self.assertEqual(cli_main.call_args.args[0][:3], ["serve", "--idle-timeout", "10"])
            self.assertIn("no repository", (Path(tmp) / "restic-machine.log").read_text(encoding="utf-8"))
            self.assertIn("no repository", alert.call_args.args[0])


if __name__ == "__main__":
    unittest.main()
