# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 David Maluenda

"""Backup settings, the schedule, and one backup run against a real restic (when there is one)."""

import json
import os
import shutil
import subprocess
import tempfile
import threading
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

from restic_machine import backup, config, schedule

RESTIC = os.environ.get("RESTIC_MACHINE_TEST_RESTIC") or shutil.which("restic")


class SettingsTest(unittest.TestCase):
    def test_defaults_and_checks(self):
        settings = config.normalize_backup({})
        self.assertEqual(settings["schedule"], {"every_hours": 1, "from": "08:00", "to": "23:00",
                                                "also_at": [], "prune_at": "04:00"})
        settings = config.normalize_backup({"sources": "C:/a\nC:/b\n", "schedule": {"also_at": "4:00, 13:30"}})
        self.assertEqual((settings["sources"], settings["schedule"]["also_at"]), (["C:/a", "C:/b"], ["04:00", "13:30"]))
        for wrong in ({"sources": [""]}, {"schedule": {"from": "8h"}}, {"schedule": {"every_hours": 30}},
                      {"use_fs_snapshot": "yes"}):
            with self.assertRaises(config.ConfigError, msg=wrong):
                config.normalize_backup(wrong)

    def test_schedule_in_words_and_times(self):
        plan = config.normalize_backup({"schedule": {"every_hours": 3, "from": "08:00", "to": "20:00",
                                                     "also_at": ["04:00"]}})["schedule"]
        self.assertEqual(schedule.describe(plan), "every 3 hours from 08:00 to 20:00, and at 04:00")
        self.assertEqual(schedule.run_times(plan), ["04:00", "08:00", "11:00", "14:00", "17:00", "20:00"])
        with self.assertRaises(schedule.ScheduleError):
            schedule.check({**plan, "from": "20:00", "to": "08:00"})
        with self.assertRaises(schedule.ScheduleError):
            schedule.check({**plan, "every_hours": 0, "also_at": []})

    def test_the_task_is_registered_without_a_password(self):
        cfg = config.Config(source=Path("C:/cfg/config.toml"), backup=config.normalize_backup(
            {"schedule": {"every_hours": 1, "from": "08:00", "to": "23:00", "also_at": ["04:00"]}}))
        script = schedule.install_script(cfg, ["old backup"], Path("C:/data/result.json"))
        self.assertIn("-LogonType S4U -RunLevel Highest", script)
        self.assertIn("-RepetitionInterval (New-TimeSpan -Hours 1) -RepetitionDuration (New-TimeSpan -Minutes 900)", script)
        self.assertIn("New-ScheduledTaskTrigger -Daily -At '04:00'", script)
        self.assertIn("-m restic_machine --config \"C:", script)
        self.assertIn("Disable-ScheduledTask -TaskName 'old backup'", script)
        self.assertNotIn("Password", script.replace("-LogonType S4U", ""))

    def test_share_paths(self):
        cfg = config.Config(backup=config.normalize_backup({"share": {"path": r"\\nas\backups", "by_ip": False}}))
        self.assertEqual(backup.share_paths(cfg, r"\\nas\backups\repo"), (r"\\nas\backups", r"\\nas\backups\repo"))
        with mock.patch.object(backup.socket, "gethostbyname", return_value="10.0.0.5"):
            cfg.backup["share"]["by_ip"] = True
            self.assertEqual(backup.share_paths(cfg, r"\\NAS\backups\repo"), (r"\\10.0.0.5\backups", r"\\10.0.0.5\backups\repo"))


class SingleRunTest(unittest.TestCase):
    def test_a_second_run_is_skipped(self):
        name = f"Local\\restic-machine-test-{os.getpid()}"
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {"RESTIC_MACHINE_DATA": tmp}):
            first = backup.SingleRun(name)
            self.assertTrue(first.acquire())
            other = []
            thread = threading.Thread(target=lambda: other.append(backup.SingleRun(name).acquire()))
            thread.start()
            thread.join()
            self.assertEqual(other, [False])
            first.release()
            again = backup.SingleRun(name)
            self.assertTrue(again.acquire())
            again.release()


@unittest.skipUnless(RESTIC, "restic not found (put it on PATH or set RESTIC_MACHINE_TEST_RESTIC)")
class RunTest(unittest.TestCase):
    def test_backup_then_prune(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            tmp = Path(tmp)
            (tmp / "src").mkdir()
            (tmp / "src" / "a.txt").write_text("hello")
            (tmp / "src" / "skip.tmp").write_text("left out")
            (tmp / "excludes.txt").write_text("*.tmp\n")
            env = {"RESTIC_REPOSITORY": str(tmp / "repo"), "RESTIC_PASSWORD": "test-only",
                   "RESTIC_CACHE_DIR": str(tmp / "cache"), "RESTIC_REPOSITORY_FILE": None,
                   "RESTIC_PASSWORD_FILE": None, "RESTIC_PASSWORD_COMMAND": None}
            # Never the user's own repository or password: drop restic's variables, then set ours.
            clean = {k: v for k, v in os.environ.items() if not k.startswith("RESTIC_")}
            clean.update({k: v for k, v in env.items() if v})
            subprocess.run([RESTIC, "init"], env=clean, check=True, capture_output=True)
            cfg = config.Config(restic_binary=RESTIC, restic_env=env, excludes_file=tmp / "excludes.txt",
                                retention={"keep_last": 1, "prune": True},
                                backup=config.normalize_backup({"sources": [str(tmp / "src")], "use_fs_snapshot": False,
                                                                "lock_name": f"Local\\restic-machine-run-{os.getpid()}"}))
            with mock.patch.dict(os.environ, {"RESTIC_MACHINE_DATA": str(tmp / "data")}):
                self.assertEqual(backup.run(cfg, prune="never"), 0)
                (tmp / "src" / "a.txt").write_text("hello again")
                self.assertEqual(backup.run(cfg, prune="always"), 0)
                log = (tmp / "data" / "backup.log").read_text(encoding="utf-8")
            listing = subprocess.run([RESTIC, "snapshots", "--json"], capture_output=True, check=True,
                                     env=clean).stdout
            self.assertEqual(len(json.loads(listing)), 1)  # keep_last 1 after the prune
            self.assertEqual(log.count(" OK"), 2)
            self.assertIn("restic forget --keep-last 1 --prune", log)
            files = subprocess.run([RESTIC, "ls", "latest", "--json"], capture_output=True, check=True,
                                   env=clean).stdout.decode()
            self.assertIn("a.txt", files)
            self.assertNotIn("skip.tmp", files)

    def test_prune_waits_for_its_time_on_a_first_run(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {"RESTIC_MACHINE_DATA": tmp}):
            cfg = config.Config(backup=config.normalize_backup({"schedule": {"prune_at": "04:00"}}))
            self.assertFalse(backup.prune_due(cfg, datetime(2026, 10, 24, 16, 0)))  # no record yet: now
            self.assertTrue(backup.prune_due(cfg, datetime(2026, 10, 25, 4, 5)))   # the prune hour
            self.assertTrue(backup.prune_due(cfg, datetime(2026, 10, 25, 23, 0)))  # 31 h later: catch up


if __name__ == "__main__":
    unittest.main()
