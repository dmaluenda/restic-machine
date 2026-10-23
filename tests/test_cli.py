# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 David Maluenda

import contextlib
import io
import unittest

from restic_machine import cli


class CliTest(unittest.TestCase):
    def test_every_command_has_help(self):
        for command in ("index", "snapshots", "ls", "info", "serve"):
            with self.assertRaises(SystemExit) as caught, contextlib.redirect_stdout(io.StringIO()) as out:
                cli.main([command, "--help"])
            self.assertEqual(caught.exception.code, 0)
            self.assertIn("usage: restic-machine", out.getvalue())


if __name__ == "__main__":
    unittest.main()
