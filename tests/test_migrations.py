# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 David Maluenda

import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from restic_machine import db, migrations
from restic_machine.indexer import Indexer
from test_indexer import FakeRestic

TOTALS = "SELECT series, path, size, files, first_seq, last_seq FROM dir_total ORDER BY series, path, first_seq"


class MigrationTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.path = db.index_file(self.dir, "f" * 64)

    def build_index(self):
        restic = FakeRestic()
        restic.add(1, "2026-01-01T10:00:00Z", {"/data/a": 1, "/data/sub/x": 10})
        restic.add(2, "2026-01-02T10:00:00Z", {"/data/a": 4, "/data/sub/x": 10, "/data/sub/y": 3})
        restic.add(3, "2026-01-03T10:00:00Z", {"/data/a": 4}, host="other")
        restic.add(4, "2026-01-04T10:00:00Z", {"/data/sub/y": 3})
        with closing(db.open_index(self.dir, "f" * 64)) as con:
            Indexer(con, restic).run()
            return [tuple(row) for row in con.execute(TOTALS)]

    def test_new_index_is_created_at_the_current_version(self):
        with closing(db.connect(self.path)) as con:
            self.assertEqual(con.execute("PRAGMA user_version").fetchone()[0], migrations.SCHEMA_VERSION)

    def test_upgrade_fills_in_directory_totals_for_the_whole_history(self):
        expected = self.build_index()
        with closing(sqlite3.connect(self.path)) as con:  # turn it back into a version 1 index
            con.execute("DROP TABLE dir_total")
            con.execute("PRAGMA user_version = 1")
            con.commit()
        with closing(db.connect(self.path)) as con:
            self.assertEqual(con.execute("PRAGMA user_version").fetchone()[0], migrations.SCHEMA_VERSION)
            self.assertEqual([tuple(row) for row in con.execute(TOTALS)], expected)
        backup = self.path.with_name("index.v1.bak")
        with closing(sqlite3.connect(backup)) as copy:
            self.assertEqual(copy.execute("PRAGMA user_version").fetchone()[0], 1)

    def test_index_from_a_newer_version_is_refused(self):
        self.build_index()
        with closing(sqlite3.connect(self.path)) as con:
            con.execute("PRAGMA user_version = 99")
        with self.assertRaises(db.IndexSchemaError):
            db.connect(self.path)


if __name__ == "__main__":
    unittest.main()
