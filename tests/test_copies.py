# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 David Maluenda

"""A copy of the repository (a mirror of its files), checked with a real restic (when there is one)."""

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest import mock

from restic_machine import config, copies, db, server
from restic_machine.indexer import Indexer
from restic_machine.restic import Restic
from test_server import Served, restic_path

RESTIC = os.environ.get("RESTIC_MACHINE_TEST_RESTIC") or shutil.which("restic")


@unittest.skipUnless(RESTIC, "restic not found (put it on PATH or set RESTIC_MACHINE_TEST_RESTIC)")
class CheckTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        (self.tmp / "src").mkdir()
        self.env = {"RESTIC_REPOSITORY": str(self.tmp / "repo"), "RESTIC_PASSWORD": "test-only",
                    "RESTIC_CACHE_DIR": str(self.tmp / "cache"), "RESTIC_REPOSITORY_FILE": None,
                    "RESTIC_PASSWORD_FILE": None, "RESTIC_PASSWORD_COMMAND": None}
        # Never the user's own repository or password: drop restic's variables, then set ours.
        self.clean = {k: v for k, v in os.environ.items() if not k.startswith("RESTIC_")}
        self.clean.update({k: v for k, v in self.env.items() if v})
        self.restic("init")
        self.data = mock.patch.dict(os.environ, {"RESTIC_MACHINE_DATA": str(self.tmp / "data")})
        self.data.start()
        self.addCleanup(self.data.stop)

    def restic(self, *args, repository=None):
        env = {**self.clean, **({"RESTIC_REPOSITORY": str(repository)} if repository else {})}
        return subprocess.run([RESTIC, *args], env=env, check=True, capture_output=True).stdout

    def backup(self, text):
        (self.tmp / "src" / "a.txt").write_text(text)
        self.restic("backup", str(self.tmp / "src"))

    def config(self, location):
        cfg = config.Config(restic_binary=RESTIC, restic_env=self.env, data_dir=self.tmp / "data")
        cfg.copies = config.normalize_copies([{"name": "mirror", "location": str(location)}])
        return cfg

    def test_the_copy_is_checked_and_what_it_holds_recorded(self):
        self.backup("one")
        self.backup("two")
        shutil.copytree(self.tmp / "repo", self.tmp / "mirror")  # the copy, made now
        self.backup("three")
        self.restic("forget", "--keep-last", "2", "--prune")  # the repository moves on
        cfg = self.config(self.tmp / "mirror")
        restic = Restic(RESTIC, self.env)
        repository_id = restic.repository_id()
        db.remember_repository(cfg.data_dir, cfg.repository_label(), repository_id)
        with closing(db.open_index(cfg.data_dir, repository_id)) as con:
            Indexer(con, restic).run()

        self.assertEqual(copies.check(cfg, "mirror", "1/2"), 0)
        seen = copies.seen(cfg, repository_id)["mirror"]
        on_mirror = sorted(s["id"] for s in Restic(RESTIC, {**self.env, "RESTIC_REPOSITORY": str(self.tmp / "mirror")}).snapshots())
        self.assertEqual((seen["snapshots"], len(seen["snapshots"])), (on_mirror, 2))
        self.assertEqual((seen["check"]["ok"], seen["check"]["read_data_subset"]), (True, "1/2"))
        log = (self.tmp / "data" / "check.log").read_text(encoding="utf-8")
        self.assertIn("2 snapshots on mirror", log)
        self.assertIn(" OK (", log)

    def test_a_file_forgotten_here_is_restored_from_the_copy(self):
        self.backup("one")
        first = json.loads(self.restic("snapshots", "--json"))[0]
        self.backup("two")
        shutil.copytree(self.tmp / "repo", self.tmp / "mirror")
        self.backup("three")
        cfg = self.config(self.tmp / "mirror")
        app = server.App(cfg)  # the real restic, on the repository and on its copy
        app.start_indexing()  # as each backup does
        app.wait_for_indexing()
        self.restic("forget", "--keep-last", "1", "--prune")  # "one" is now only on the mirror
        app.start_indexing()
        app.wait_for_indexing()
        self.assertEqual(copies.check(cfg, "mirror"), 0)
        served = Served()
        served.addCleanup, served.assertEqual = self.addCleanup, self.assertEqual
        served.start(app)
        target = served.post_json("/api/restore", {"snapshot": first["id"], "path": restic_path(self.tmp / "src" / "a.txt")})
        self.assertEqual(Path(target["target"]).read_text(), "one")
        shutil.rmtree(self.tmp / "mirror")  # unplugged
        refused = served.post_json("/api/restore", {"snapshot": first["id"], "path": restic_path(self.tmp / "src" / "a.txt")}, status=409)
        self.assertIn("not there now", refused["error"])

    def test_what_it_answers_when_it_cannot(self):
        self.backup("one")
        cfg = self.config(self.tmp / "unplugged")
        db.remember_repository(cfg.data_dir, cfg.repository_label(), Restic(RESTIC, self.env).repository_id())
        self.assertEqual(copies.check(cfg, "mirror"), copies.EX_TEMPFAIL)  # not there now
        self.assertEqual(copies.check(cfg, "nowhere"), copies.EX_CONFIG)  # no such copy
        self.restic("init", repository=self.tmp / "other")
        self.assertEqual(copies.check(self.config(self.tmp / "other"), "mirror"), copies.EX_CONFIG)  # another repository
        self.assertIn("holds another repository", (self.tmp / "data" / "check.log").read_text(encoding="utf-8"))

    def test_the_share_login_is_the_backups_on_the_same_server(self):
        cfg = config.Config(backup=config.normalize_backup(
            {"share": {"path": r"\\nas\backups", "user": "backup", "password_file": "pw.txt"}}))
        cfg.copies = config.normalize_copies([
            {"name": "same", "location": r"\\NAS\copy\repo"}, {"name": "other", "location": r"\\elsewhere\copy"},
            {"name": "own", "location": r"\\nas\copy", "share_user": "reader", "share_password_file": "r.txt"}])
        self.assertEqual([copies.login(cfg, c) for c in cfg.copies],
                         [("backup", "pw.txt"), ("", ""), ("reader", "r.txt")])


if __name__ == "__main__":
    unittest.main()
