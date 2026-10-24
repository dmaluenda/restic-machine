# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 David Maluenda

"""End to end against a real restic binary and a throwaway repository made by the test.

Skipped unless restic is on PATH or RESTIC_MACHINE_TEST_RESTIC points at it.
"""

import os
import shutil
import subprocess
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from restic_machine import config, db, query, retention
from restic_machine.indexer import Indexer
from restic_machine.restic import RepositoryUnavailable, Restic, ResticError

RESTIC = os.environ.get("RESTIC_MACHINE_TEST_RESTIC") or shutil.which("restic")


def restic_path(path: Path) -> str:
    """How restic names a local path in a snapshot (C:\\x -> /C/x on Windows)."""
    path = path.resolve()
    if os.name == "nt":
        return "/" + path.drive.rstrip(":") + path.as_posix()[len(path.drive):]
    return path.as_posix()


@unittest.skipUnless(RESTIC, "restic not found (put it on PATH or set RESTIC_MACHINE_TEST_RESTIC)")
class RealResticTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.src = self.tmp / "src"
        (self.src / "sub").mkdir(parents=True)
        # Never inherit the user's own repository or password.
        self.env = {"RESTIC_REPOSITORY": str(self.tmp / "repo"), "RESTIC_PASSWORD": "test-only",
                    "RESTIC_CACHE_DIR": str(self.tmp / "cache"), "RESTIC_REPOSITORY_FILE": None,
                    "RESTIC_PASSWORD_FILE": None, "RESTIC_PASSWORD_COMMAND": None}
        self.restic = Restic(RESTIC, self.env)
        self.write_to_repo("init")

    def write_to_repo(self, *args):
        # Only the test writes, to build its synthetic repository; restic-machine never does.
        subprocess.run([RESTIC, *args], env=self.restic.env, check=True, capture_output=True,
                       stdin=subprocess.DEVNULL)

    def put(self, name, content, mtime):
        path = self.src / name
        path.write_bytes(content)
        os.utime(path, (mtime, mtime))

    def backup(self, when):
        self.write_to_repo("backup", "--host", "synthetic", "--time", when, str(self.src))

    def test_index_and_browse(self):
        self.put("a.txt", b"hello", 1_700_000_000)
        self.put("sub/b.txt", b"bee", 1_700_000_000)
        self.backup("2026-01-01 10:00:00")
        self.put("a.txt", b"hello world", 1_700_000_500)
        self.backup("2026-01-01 11:00:00")

        con = db.open_index(self.tmp / "data", self.restic.repository_id())
        self.addCleanup(con.close)
        self.assertEqual(Indexer(con, self.restic).run(), 2)
        first, second = query.snapshots(con)
        src = restic_path(self.src)

        def files(snap):
            return {r["name"]: r["size"] for r in query.list_dir(con, snap, src) if r["type"] != "dir"}

        self.assertEqual({r["name"] for r in query.list_dir(con, first, src)}, {"a.txt", "sub"})
        self.assertEqual(files(first), {"a.txt": 5})
        self.assertEqual(files(second), {"a.txt": 11})
        a_txt = query.resolve_path(con, src + "/a.txt")
        self.assertEqual(con.execute("SELECT count(*) FROM version WHERE path = ?", (a_txt,)).fetchone()[0], 2)
        self.assertGreaterEqual(second["n_modified"], 1)
        # Reading left no lock behind in the repository.
        self.assertEqual(list((self.tmp / "repo" / "locks").iterdir()), [])

    def test_retention_rules_match_restic(self):
        """The calendar's copy of restic's rules gives restic's own answer, reasons included."""
        self.put("a.txt", b"x", 1_700_000_000)
        times = ["2024-12-31 23:30:00", "2025-06-15 12:00:00", "2026-01-01 00:30:00", "2026-03-30 10:00:00",
                 "2026-09-28 09:00:00", "2026-10-05 09:00:00", "2026-10-19 09:00:00", "2026-10-22 08:00:00",
                 "2026-10-23 08:00:00", "2026-10-23 09:00:00", "2026-10-24 08:00:00", "2026-10-24 09:00:00"]
        for when in times:
            self.backup(when)
        snapshots = self.restic.snapshots()
        self.write_to_repo("tag", "--add", "keep", snapshots[1]["id"])
        snaps = [retention.Snapshot(datetime.fromisoformat(s["time"]), s["id"], s.get("tags") or [])
                 for s in self.restic.snapshots()]
        for policy in ({"keep_last": 3, "keep_daily": 4, "keep_weekly": 2, "keep_monthly": 3, "keep_yearly": 9},
                       {"keep_hourly": 3, "keep_tag": ["keep"]},
                       {"keep_within": "2d", "keep_monthly": 2},
                       {"keep_within_daily": "1m", "keep_within_yearly": "5y"}):
            ours = {s.id: (keep, reasons) for s, keep, reasons in retention.apply(snaps, policy)}
            theirs = {}
            for group in self.restic.forget_preview(config.forget_args(policy)):
                why = {r["snapshot"]["id"]: r["matches"] for r in group.get("reasons") or []}
                theirs.update({s["id"]: (True, why.get(s["id"], [])) for s in group.get("keep") or []})
                theirs.update({s["id"]: (False, []) for s in group.get("remove") or []})
            self.assertEqual(ours, theirs, policy)

    def test_errors(self):
        wrong = Restic(RESTIC, {**self.env, "RESTIC_PASSWORD": "wrong"})
        with self.assertRaises(ResticError) as caught:
            wrong.snapshots()
        self.assertNotIsInstance(caught.exception, RepositoryUnavailable)
        missing = Restic(RESTIC, {**self.env, "RESTIC_REPOSITORY": str(self.tmp / "nothing")})
        with self.assertRaises(ResticError):
            missing.snapshots()
        with self.assertRaises(ResticError):
            Restic(str(self.tmp / "no-such-restic"), self.env).snapshots()


if __name__ == "__main__":
    unittest.main()
