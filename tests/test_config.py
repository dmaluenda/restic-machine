# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 David Maluenda

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from restic_machine import config


class ConfigTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)

    def write(self, text):
        path = self.dir / "config.toml"
        path.write_text(text, encoding="utf-8")
        return path

    def test_restic_settings_become_environment_variables(self):
        path = self.write(
            '[restic]\nbinary = "/opt/restic"\nrepository = "sftp:nas:/srv/restic"\n'
            'password_file = "~/pw.txt"\n[restic.env]\nAWS_PROFILE = "backup"\n'
            '[index]\ndata_dir = "~/idx"\nforgotten_retention_days = 7\n')
        cfg = config.load(path)
        self.assertEqual(cfg.restic_binary, "/opt/restic")
        self.assertEqual(cfg.restic_env["RESTIC_REPOSITORY"], "sftp:nas:/srv/restic")
        self.assertEqual(cfg.restic_env["RESTIC_PASSWORD_FILE"], os.path.expanduser("~/pw.txt"))
        self.assertEqual(cfg.restic_env["AWS_PROFILE"], "backup")
        self.assertEqual(cfg.data_dir, Path("~/idx").expanduser())
        self.assertEqual(cfg.forgotten_retention_days, 7)
        self.assertEqual(cfg.repository_label(), "sftp:nas:/srv/restic")

    def test_missing_default_file_means_defaults(self):
        with mock.patch.dict(os.environ, {"RESTIC_MACHINE_CONFIG": str(self.dir / "nope.toml")}):
            self.assertEqual(config.load().restic_binary, "restic")

    def test_missing_explicit_file_is_an_error(self):
        with self.assertRaises(config.ConfigError):
            config.load(self.dir / "nope.toml")

    def test_user_directories(self):
        env = {"XDG_CONFIG_HOME": str(self.dir / "cfg"), "XDG_CACHE_HOME": str(self.dir / "cache")}
        with mock.patch.dict(os.environ, env):
            os.environ.pop("RESTIC_MACHINE_CONFIG", None)
            os.environ.pop("RESTIC_MACHINE_DATA", None)
            self.assertEqual(config.config_file(), self.dir / "cfg" / "restic-machine" / "config.toml")
            self.assertEqual(config.default_data_dir(), self.dir / "cache" / "restic-machine")

    def test_repository_and_password_are_required(self):
        cleared = {var: "" for var in ("RESTIC_REPOSITORY", "RESTIC_REPOSITORY_FILE", "RESTIC_PASSWORD",
                                       "RESTIC_PASSWORD_FILE", "RESTIC_PASSWORD_COMMAND")}
        with mock.patch.dict(os.environ, cleared):
            cfg = config.Config()
            with self.assertRaises(config.ConfigError):
                cfg.repository_label()
            with self.assertRaises(config.ConfigError):
                cfg.check_password_source()


if __name__ == "__main__":
    unittest.main()
