# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 David Maluenda

"""Indexer logic against a fake restic: no binary or repository needed."""

import tempfile
import unittest
from pathlib import Path

from restic_machine import db, query
from restic_machine.indexer import Indexer, timestamp_us
from restic_machine.restic import RepositoryUnavailable

MTIME = "2026-01-01T00:00:00Z"


def snapshot_id(n: int) -> str:
    return f"{n:064x}"


def nodes(files: dict) -> list[dict]:
    """{"/data/a": size or (size, mtime)} -> restic `ls --json` nodes, parent dirs included."""
    out = {}
    for path, spec in files.items():
        size, mtime = spec if isinstance(spec, tuple) else (spec, MTIME)
        parts = path.strip("/").split("/")
        for depth in range(1, len(parts)):
            directory = "/" + "/".join(parts[:depth])
            out.setdefault(directory, {"struct_type": "node", "path": directory, "type": "dir", "mtime": MTIME})
        out[path] = {"struct_type": "node", "path": path, "type": "file", "size": size, "mtime": mtime}
    return [out[path] for path in sorted(out)]


class FakeRestic:
    def __init__(self):
        self.repo = {}
        self.ls_calls = []
        self.fail_at = None  # (snapshot id, node position) where ls breaks off

    def add(self, n, time, files, host="host", paths=("/data",), tree=None):
        info = {"id": snapshot_id(n), "time": time, "hostname": host, "paths": list(paths),
                "tree": tree or f"tree-{n}"}
        self.repo[info["id"]] = (info, nodes(files))
        return info["id"]

    def forget(self, n):
        del self.repo[snapshot_id(n)]

    def snapshots(self):
        return [info for info, _ in self.repo.values()]

    def ls(self, restic_id):
        self.ls_calls.append(restic_id)
        for position, node in enumerate(self.repo[restic_id][1]):
            if self.fail_at == (restic_id, position):
                raise RepositoryUnavailable("pack not found")
            yield node


class IndexerTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.con = db.open_index(Path(tmp.name), "f" * 64)
        self.addCleanup(self.con.close)
        self.restic = FakeRestic()
        self.events = []
        self.indexer = Indexer(self.con, self.restic, progress=lambda event, **info: self.events.append(event))

    def listing(self, n, path="/data"):
        snap = query.find_snapshot(self.con, snapshot_id(n))
        return {row["name"]: row["size"] for row in query.list_dir(self.con, snap, path)}

    def versions_of(self, path):
        return self.con.execute("SELECT count(*) FROM version WHERE path = ?",
                                (query.resolve_path(self.con, path),)).fetchone()[0]

    def snapshot_row(self, n):
        return self.con.execute("SELECT * FROM snapshot WHERE restic_id = ?", (snapshot_id(n),)).fetchone()

    def test_unchanged_file_is_a_single_row(self):
        for n in range(1, 6):
            self.restic.add(n, f"2026-01-0{n}T10:00:00Z", {"/data/a": 1})
        self.assertEqual(self.indexer.run(), 5)
        self.assertEqual(self.versions_of("/data/a"), 1)
        for n in range(1, 6):
            self.assertEqual(self.listing(n), {"a": 1})

    def test_added_modified_removed(self):
        self.restic.add(1, "2026-01-01T10:00:00Z", {"/data/a": 1, "/data/b": 2})
        self.restic.add(2, "2026-01-01T11:00:00Z", {"/data/a": 5, "/data/c": 3})
        self.indexer.run()
        self.assertEqual(self.listing(1), {"a": 1, "b": 2})
        self.assertEqual(self.listing(2), {"a": 5, "c": 3})
        row = self.snapshot_row(2)
        self.assertEqual((row["n_entries"], row["n_added"], row["n_modified"], row["n_removed"]), (3, 1, 1, 1))
        self.assertIsNone(self.snapshot_row(1)["n_added"])

    def test_mtime_change_is_a_new_version_but_offset_change_is_not(self):
        self.restic.add(1, "2026-03-28T10:00:00Z", {"/data/a": (1, "2026-03-29T00:30:00Z")})
        self.restic.add(2, "2026-03-29T10:00:00Z", {"/data/a": (1, "2026-03-29T01:30:00+01:00")})  # same instant
        self.restic.add(3, "2026-03-30T10:00:00Z", {"/data/a": (1, "2026-03-30T08:00:00+02:00")})
        self.indexer.run()
        self.assertEqual(self.versions_of("/data/a"), 2)

    def test_runs_are_incremental(self):
        self.restic.add(1, "2026-01-01T10:00:00Z", {"/data/a": 1})
        self.assertEqual(self.indexer.run(), 1)
        self.assertEqual(self.indexer.run(), 0)
        self.restic.add(2, "2026-01-01T11:00:00Z", {"/data/a": 2})
        self.assertEqual(self.indexer.run(), 1)
        self.assertEqual(len(self.restic.ls_calls), 2)
        self.assertEqual(self.listing(1), {"a": 1})
        self.assertEqual(self.listing(2), {"a": 2})

    def test_forgotten_snapshot_is_hidden_but_kept(self):
        for n in (1, 2, 3):
            self.restic.add(n, f"2026-01-0{n}T10:00:00Z", {"/data/a": n})
        self.indexer.run()
        self.restic.forget(2)
        self.indexer.run()
        self.assertIn("forgotten", self.events)
        visible = [row["restic_id"] for row in query.snapshots(self.con)]
        self.assertEqual(visible, [snapshot_id(1), snapshot_id(3)])
        self.assertEqual(len(query.snapshots(self.con, include_forgotten=True)), 3)
        self.assertEqual(self.listing(2), {"a": 2})

    def test_snapshot_out_of_order_rewinds_the_series(self):
        self.restic.add(1, "2026-01-01T10:00:00Z", {"/data/a": 1})
        self.restic.add(3, "2026-01-03T10:00:00Z", {"/data/a": 3, "/data/b": 3})
        self.indexer.run()
        self.restic.add(2, "2026-01-02T10:00:00Z", {"/data/a": 2})
        self.assertEqual(self.indexer.run(), 2)
        self.assertIn("rewind", self.events)
        self.assertEqual([self.snapshot_row(n)["seq"] for n in (1, 2, 3)], [1, 2, 3])
        self.assertEqual(self.listing(1), {"a": 1})
        self.assertEqual(self.listing(2), {"a": 2})
        self.assertEqual(self.listing(3), {"a": 3, "b": 3})
        self.assertEqual(self.versions_of("/data/a"), 3)

    def test_rewritten_snapshot_takes_the_place_of_the_old_one(self):
        # `restic tag` gives a snapshot a new id, keeping its time and tree.
        self.restic.add(1, "2026-01-01T10:00:00Z", {"/data/a": 1}, tree="t1")
        self.restic.add(2, "2026-01-02T10:00:00Z", {"/data/a": 2}, tree="t2")
        self.indexer.run()
        self.restic.forget(1)
        self.restic.add(9, "2026-01-01T10:00:00Z", {"/data/a": 1}, tree="t1")
        self.assertEqual(self.indexer.run(), 0)
        self.assertNotIn("rewind", self.events)
        self.assertEqual(len(self.restic.ls_calls), 2)
        self.assertEqual(self.snapshot_row(9)["seq"], 1)
        old = self.snapshot_row(1)
        self.assertIsNone(old["seq"])
        self.assertIsNotNone(old["forgotten_at"])
        self.assertEqual(old["replaced_by"], self.snapshot_row(9)["id"])
        self.assertEqual(self.listing(9), {"a": 1})

    def test_failed_read_changes_nothing(self):
        self.restic.add(1, "2026-01-01T10:00:00Z", {"/data/a": 1})
        self.indexer.run()
        sid = self.restic.add(2, "2026-01-02T10:00:00Z", {"/data/a": 1, "/data/new/deep/x": 7})
        self.restic.fail_at = (sid, 4)  # after the new directories were created
        before = self.con.execute("SELECT count(*) FROM path").fetchone()[0]
        with self.assertRaises(RepositoryUnavailable):
            self.indexer.run()
        self.assertIsNone(self.snapshot_row(2)["seq"])
        self.assertEqual(self.con.execute("SELECT count(*) FROM path").fetchone()[0], before)
        self.restic.fail_at = None
        self.assertEqual(self.indexer.run(), 1)
        self.assertEqual(self.listing(2, "/data/new/deep"), {"x": 7})

    def test_series_are_kept_apart(self):
        self.restic.add(1, "2026-01-01T10:00:00Z", {"/data/a": 1}, host="alpha")
        self.restic.add(2, "2026-01-01T11:00:00Z", {"/data/a": 2}, host="beta")
        self.restic.add(3, "2026-01-01T12:00:00Z", {"/data/a": 1}, host="alpha")
        self.indexer.run()
        self.assertEqual([self.snapshot_row(n)["seq"] for n in (1, 2, 3)], [1, 1, 2])
        self.assertEqual(self.listing(2), {"a": 2})
        self.assertEqual(self.listing(3), {"a": 1})
        self.assertEqual(self.versions_of("/data/a"), 2)

    def test_listing_errors(self):
        self.restic.add(1, "2026-01-01T10:00:00Z", {"/data/a": 1})
        self.restic.add(2, "2026-01-02T10:00:00Z", {"/data/b": 1})
        self.indexer.run()
        snap1 = query.find_snapshot(self.con, snapshot_id(1))
        with self.assertRaises(LookupError):
            query.list_dir(self.con, snap1, "/data/a")  # a file
        with self.assertRaises(LookupError):
            query.list_dir(self.con, snap1, "/nowhere")
        self.assertEqual(query.find_snapshot(self.con, "latest")["restic_id"], snapshot_id(2))
        with self.assertRaises(LookupError):
            query.find_snapshot(self.con, "0")  # ambiguous prefix


class TimestampTest(unittest.TestCase):
    def test_nanoseconds_and_offsets(self):
        self.assertEqual(timestamp_us("1970-01-01T00:00:01.123456789Z"), 1_123_456)
        self.assertEqual(timestamp_us("1970-01-01T01:00:00+01:00"), 0)
        self.assertIsNone(timestamp_us(None))


if __name__ == "__main__":
    unittest.main()
