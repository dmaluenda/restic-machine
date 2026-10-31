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

    def test_retention_policy(self):
        policy = config.normalize_policy({"keep_last": 32, "keep_daily": 7, "keep_hourly": 0,
                                          "keep_within": "1y6m", "keep_tag": "keep, important"})
        self.assertEqual(policy, {"keep_last": 32, "keep_daily": 7, "keep_within": "1y6m",
                                  "keep_tag": ["keep", "important"], "prune": True})
        self.assertEqual(config.forget_args(policy), [
            "--keep-last", "32", "--keep-daily", "7", "--keep-within", "1y6m",
            "--keep-tag", "keep", "--keep-tag", "important"])
        self.assertFalse(config.has_rules(config.normalize_policy({"prune": False})))
        for wrong in ({"keep_daily": -1}, {"keep_daily": "7"}, {"keep_within": "6 months"},
                      {"group_by": "hostname"}, {"prune": "yes"}):
            with self.assertRaises(config.ConfigError, msg=wrong):
                config.normalize_policy(wrong)

    def test_saved_file_reads_back_the_same(self):
        cfg = config.Config(source=self.dir / "config.toml", restic_binary="C:/tools/restic.exe",
                            restic_env={"RESTIC_REPOSITORY": "sftp:nas:/srv/restic", "AWS_PROFILE": "backup",
                                        "RESTIC_PASSWORD_FILE": None},
                            data_dir=self.dir / "index", forgotten_retention_days=10,
                            retention={"keep_last": 3, "keep_tag": ["keep"], "prune": True})
        config.save(cfg)
        again = config.load(self.dir / "config.toml")
        self.assertEqual((again.restic_binary, again.restic_env, again.data_dir,
                          again.forgotten_retention_days, again.retention),
                         ("C:/tools/restic.exe", {"RESTIC_REPOSITORY": "sftp:nas:/srv/restic", "AWS_PROFILE": "backup"},
                          self.dir / "index", 10, {"keep_last": 3, "keep_tag": ["keep"], "prune": True}))

    def test_settings_from_the_web_page(self):
        cfg = config.Config(restic_env={"RESTIC_REPOSITORY": "old"})
        new = cfg.with_settings({"repository": "", "password_file": "~/pw.txt", "restic_binary": "",
                                 "forgotten_retention_days": 5})
        self.assertNotIn("RESTIC_REPOSITORY", new.restic_env)  # empty: back to the environment variable
        self.assertEqual(new.restic_env["RESTIC_PASSWORD_FILE"], os.path.expanduser("~/pw.txt"))
        self.assertEqual((new.restic_binary, new.forgotten_retention_days), ("restic", 5))
        self.assertEqual(cfg.restic_env, {"RESTIC_REPOSITORY": "old"})  # the original is untouched
        with self.assertRaises(config.ConfigError):
            cfg.with_settings({"forgotten_retention_days": "a week"})

    def test_initial_path_as_restic_names_it(self):
        for given, expected in (("", "/"), ("/", "/"), ("C:\\Users\\me\\My Docs\\", "/C/Users/me/My Docs"),
                                ("C:/Users/me", "/C/Users/me"), ("c:", "/c"), ("/home/me//x", "/home/me/x")):
            self.assertEqual(config.browse_path(given), expected, given)
        cfg = config.Config(source=self.dir / "config.toml").with_settings({"initial_path": "C:\\Users\\me\\My Docs"})
        config.save(cfg)
        self.assertIn('initial_path = "/C/Users/me/My Docs"', cfg.path.read_text(encoding="utf-8"))
        self.assertEqual(config.load(cfg.path).initial_path, "/C/Users/me/My Docs")
        config.save(cfg.with_settings({"initial_path": ""}))
        self.assertNotIn("[browse]", cfg.path.read_text(encoding="utf-8"))  # the root is the default

    def test_copies_of_the_repository(self):
        cfg = config.Config(source=self.dir / "config.toml").with_settings({"copies": [
            {"name": "offline-disk", "location": "", "copied_at": "2026-10-24T12:14"},
            {"name": "other", "location": "/mnt/other/repo", "copied_at": ""}]})
        config.save(cfg)
        nobody = {"share_user": "", "share_password_file": ""}
        self.assertEqual(config.load(cfg.path).copies, [
            {"name": "offline-disk", "location": "", "copied_at": "2026-10-24T12:14", **nobody},
            {"name": "other", "location": "/mnt/other/repo", "copied_at": "", **nobody}])
        for wrong in ([{"name": ""}], [{"name": "live"}], [{"name": "a"}, {"name": "A"}],
                      [{"name": "a", "copied_at": "yesterday"}], {"name": "a"}):
            with self.assertRaises(config.ConfigError, msg=wrong):
                cfg.with_settings({"copies": wrong})


if __name__ == "__main__":
    unittest.main()
