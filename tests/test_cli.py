# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 David Maluenda

import contextlib
import io
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from restic_machine import cli


class CliTest(unittest.TestCase):
    def test_every_command_has_help(self):
        for command in ("index", "snapshots", "ls", "info", "serve", "forget"):
            with self.assertRaises(SystemExit) as caught, contextlib.redirect_stdout(io.StringIO()) as out:
                cli.main([command, "--help"])
            self.assertEqual(caught.exception.code, 0)
            self.assertIn("usage: restic-machine", out.getvalue())


class ForgetTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.file = Path(tmp.name) / "config.toml"

    def run_forget(self, toml, *extra):
        self.file.write_text('[restic]\nrepository = "fake:repo"\npassword_file = "pw.txt"\n' + toml, encoding="utf-8")
        with mock.patch.object(cli, "Restic") as restic, contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(io.StringIO()) as err:
            restic.return_value.forget.return_value = 0
            code = cli.main(["--config", str(self.file), "forget", *extra])
        return code, restic.return_value, err.getvalue()

    def test_applies_the_saved_policy(self):
        code, restic, _ = self.run_forget("[retention]\nkeep_last = 32\nkeep_daily = 7\nkeep_tag = [\"keep\"]\n")
        self.assertEqual(code, 0)
        restic.forget.assert_called_once_with(
            ["--keep-last", "32", "--keep-daily", "7", "--keep-tag", "keep"], True)

    def test_refuses_without_a_policy(self):
        code, restic, err = self.run_forget("")
        self.assertEqual(code, cli.EX_CONFIG)
        self.assertIn("no retention policy", err)
        restic.forget.assert_not_called()

    def test_dry_run_only_reads(self):
        code, restic, _ = self.run_forget("[retention]\nkeep_last = 1\n", "--dry-run")
        self.assertEqual(code, 0)
        restic.forget_preview.assert_called_once_with(["--keep-last", "1"])
        restic.forget.assert_not_called()


if __name__ == "__main__":
    unittest.main()
