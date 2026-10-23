# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 David Maluenda

"""Incremental indexing of restic snapshots (see db.py for the layout of the index).

Snapshots are ingested in time order within each series. When one shows up out of order
(e.g. copied in with `restic copy`), the series is rewound to that point and the later
snapshots are read again. A snapshot that reappears under a new id with the same time and
tree (`restic tag` rewrites snapshots) just takes the place of the old one.
"""

import json
import sqlite3
import time
from collections.abc import Callable, Iterable
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

from .db import IRREGULAR, OPEN, TYPE_CODE

_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
_US = timedelta(microseconds=1)

_TEMP_TABLES = (
    "CREATE TEMP TABLE IF NOT EXISTS staged("
    " parent INTEGER, name TEXT, type INTEGER, size INTEGER, mtime_us INTEGER)",
    "CREATE TEMP TABLE IF NOT EXISTS incoming("
    " path INTEGER PRIMARY KEY, type INTEGER, size INTEGER, mtime_us INTEGER)",
)


def timestamp_us(text: str | None) -> int | None:
    """restic's RFC 3339 timestamps (any offset, nanoseconds) -> UTC microseconds.

    Comparing instants rather than strings matters: the same mtime is written with another
    offset after a daylight-saving change, and must not count as a modification.
    """
    if not text:
        return None
    moment = datetime.fromisoformat(text)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return (moment - _EPOCH) // _US


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class IndexBusy(Exception):
    """Another process is updating the index."""


class Indexer:
    def __init__(self, con: sqlite3.Connection, restic, progress: Callable[..., None] | None = None):
        self.con = con
        self.restic = restic
        self.progress = progress or (lambda event, **info: None)
        self._dir_ids: dict[str, int] = {}
        for statement in _TEMP_TABLES:
            con.execute(statement)

    def run(self, limit: int | None = None) -> int:
        """Bring the index up to date; returns how many snapshots were read."""
        self.sync_snapshots()
        pending = self._pending()
        if limit is not None:
            pending = pending[:limit]
        self.progress("start", total=len(pending))
        done = 0
        for n, snap in enumerate(pending, 1):
            started = time.monotonic()
            stats = self.ingest(snap["id"], self.restic.ls(snap["restic_id"]))
            if stats is not None:
                done += 1
                self.progress("snapshot", n=n, total=len(pending), snapshot=snap,
                              seconds=time.monotonic() - started, **stats)
        return done

    def sync_snapshots(self) -> None:
        """Record new snapshots; flag the ones restic no longer lists (hidden, kept a while)."""
        live = self.restic.snapshots()
        live_ids = {info["id"] for info in live}
        now = _now()
        with self._write():
            for info in live:
                known = self.con.execute(
                    "SELECT id FROM snapshot WHERE restic_id = ?", (info["id"],)).fetchone()
                if known:
                    self.con.execute("UPDATE snapshot SET forgotten_at = NULL WHERE id = ?", (known["id"],))
                    continue
                series = self._series_id(info.get("hostname") or "", info.get("paths") or [])
                time_us = timestamp_us(info["time"])
                self.con.execute(
                    "INSERT INTO snapshot(restic_id, series, time, time_us, tree, info)"
                    " VALUES(?, ?, ?, ?, ?, ?)",
                    (info["id"], series, info["time"], time_us, info["tree"], json.dumps(info)))
                self._adopt_twin(info["id"], series, time_us, info["tree"], live_ids, now)
            gone = [row["id"] for row in self.con.execute(
                        "SELECT id, restic_id FROM snapshot WHERE forgotten_at IS NULL")
                    if row["restic_id"] not in live_ids]
            self.con.executemany("UPDATE snapshot SET forgotten_at = ? WHERE id = ?",
                                 [(now, snapshot_id) for snapshot_id in gone])
        if gone:
            self.progress("forgotten", count=len(gone))

    def _adopt_twin(self, restic_id, series, time_us, tree, live_ids, now) -> None:
        """A new id with the time and tree of an indexed snapshot that restic no longer lists
        is the same snapshot rewritten: hand its place over instead of reading it again."""
        for twin in self.con.execute(
                "SELECT * FROM snapshot WHERE series = ? AND time_us = ? AND tree = ? AND seq IS NOT NULL",
                (series, time_us, tree)).fetchall():
            if twin["restic_id"] in live_ids:
                continue
            new_id = self.con.execute(
                "SELECT id FROM snapshot WHERE restic_id = ?", (restic_id,)).fetchone()["id"]
            self.con.execute(
                "UPDATE snapshot SET seq = NULL, forgotten_at = coalesce(forgotten_at, ?), replaced_by = ?"
                " WHERE id = ?", (now, new_id, twin["id"]))
            self.con.execute(
                "UPDATE snapshot SET seq = ?, indexed_at = ?, n_entries = ?, n_added = ?,"
                " n_modified = ?, n_removed = ? WHERE id = ?",
                (twin["seq"], now, twin["n_entries"], twin["n_added"], twin["n_modified"],
                 twin["n_removed"], new_id))
            self.progress("adopted", snapshot=restic_id, replaces=twin["restic_id"])
            return

    def _pending(self) -> list[sqlite3.Row]:
        with self._write():
            for row in self.con.execute(
                    "SELECT series, min(time_us) AS earliest FROM snapshot"
                    " WHERE seq IS NULL AND forgotten_at IS NULL GROUP BY series").fetchall():
                (later,) = self.con.execute(
                    "SELECT min(seq) FROM snapshot WHERE series = ? AND seq IS NOT NULL AND time_us > ?",
                    (row["series"], row["earliest"])).fetchone()
                if later is not None:
                    self._rewind(row["series"], later)
        return self.con.execute(
            "SELECT * FROM snapshot WHERE seq IS NULL AND forgotten_at IS NULL"
            " ORDER BY time_us, restic_id").fetchall()

    def _rewind(self, series: int, seq: int) -> None:
        """Forget what the index knows from position `seq` on. Snapshots already removed from
        the repository can't be read again: they keep their record but lose their content."""
        c = self.con
        c.execute("DELETE FROM version WHERE series = ? AND first_seq >= ?", (series, seq))
        # Whatever was alive at seq - 1 is alive in the latest indexed snapshot again.
        c.execute("UPDATE version SET last_seq = ? WHERE series = ? AND last_seq >= ?",
                  (OPEN, series, seq - 1))
        lost = c.execute(
            "SELECT count(*) FROM snapshot WHERE series = ? AND seq >= ? AND forgotten_at IS NOT NULL",
            (series, seq)).fetchone()[0]
        rewound = c.execute(
            "UPDATE snapshot SET seq = NULL, indexed_at = NULL, n_entries = NULL, n_added = NULL,"
            " n_modified = NULL, n_removed = NULL WHERE series = ? AND seq >= ?", (series, seq)).rowcount
        self.progress("rewind", series=series, seq=seq, snapshots=rewound, lost=lost)

    def ingest(self, snapshot_id: int, nodes: Iterable[dict]) -> dict | None:
        """Add one snapshot at the end of its series. Returns change counts, or None when
        another process indexed it first. All or nothing: a failed read changes nothing."""
        try:
            with self._write():
                snap = self.con.execute("SELECT * FROM snapshot WHERE id = ?", (snapshot_id,)).fetchone()
                if snap["seq"] is not None:
                    return None
                latest, latest_time = self.con.execute(
                    "SELECT max(seq), max(time_us) FROM snapshot WHERE series = ? AND seq IS NOT NULL",
                    (snap["series"],)).fetchone()
                if latest_time is not None and snap["time_us"] < latest_time:
                    raise RuntimeError(f"snapshot {snap['restic_id'][:8]} is out of order; index again")
                prev = latest or 0
                self._stage(nodes)
                stats = self._apply(snap["series"], prev + 1, prev)
                self.con.execute(
                    "UPDATE snapshot SET seq = ?, indexed_at = ?, n_entries = ?, n_added = ?,"
                    " n_modified = ?, n_removed = ? WHERE id = ?",
                    (prev + 1, _now(), stats["entries"], stats["added"], stats["modified"],
                     stats["removed"], snapshot_id))
                return stats
        finally:
            close = getattr(nodes, "close", None)
            if close:
                close()  # stops restic if we bailed out half way

    def _stage(self, nodes: Iterable[dict]) -> None:
        c = self.con
        c.execute("DELETE FROM staged")
        c.execute("DELETE FROM incoming")
        insert = "INSERT INTO staged VALUES(?, ?, ?, ?, ?)"
        batch = []
        for node in nodes:
            parent, _, name = node["path"].rpartition("/")
            if not name:
                continue
            batch.append((self._dir_id(parent), name, TYPE_CODE.get(node.get("type"), IRREGULAR),
                          node.get("size"), timestamp_us(node.get("mtime"))))
            if len(batch) >= 10_000:
                c.executemany(insert, batch)
                batch.clear()
        c.executemany(insert, batch)
        c.execute("INSERT OR IGNORE INTO path(parent, name) SELECT parent, name FROM staged")
        c.execute(
            "INSERT OR REPLACE INTO incoming SELECT p.id, s.type, s.size, s.mtime_us"
            " FROM staged s JOIN path p ON p.parent = s.parent AND p.name = s.name")
        c.execute("DELETE FROM staged")

    def _apply(self, series: int, seq: int, prev: int) -> dict:
        c = self.con
        args = {"series": series, "seq": seq, "prev": prev, "open": OPEN}
        # Close the versions that are gone or changed; unchanged ones stay open, untouched.
        removed_or_modified = c.execute(
            "UPDATE version SET last_seq = :prev WHERE series = :series AND last_seq = :open"
            " AND NOT EXISTS (SELECT 1 FROM incoming i WHERE i.path = version.path"
            "  AND i.type = version.type AND i.size IS version.size AND i.mtime_us IS version.mtime_us)",
            args).rowcount
        added_or_modified = c.execute(
            "INSERT INTO version(series, path, type, size, mtime_us, first_seq, last_seq)"
            " SELECT :series, i.path, i.type, i.size, i.mtime_us, :seq, :open FROM incoming i"
            " WHERE NOT EXISTS (SELECT 1 FROM version v"
            "  WHERE v.path = i.path AND v.series = :series AND v.last_seq = :open)",
            args).rowcount
        entries = c.execute("SELECT count(*) FROM incoming").fetchone()[0]
        c.execute("DELETE FROM incoming")
        if prev == 0:
            return {"entries": entries, "added": None, "modified": None, "removed": None}
        modified = c.execute(
            "SELECT count(*) FROM version n WHERE n.series = :series AND n.first_seq = :seq"
            " AND EXISTS (SELECT 1 FROM version o"
            "  WHERE o.path = n.path AND o.series = :series AND o.last_seq = :prev)",
            args).fetchone()[0]
        return {"entries": entries, "added": added_or_modified - modified, "modified": modified,
                "removed": removed_or_modified - modified}

    def _dir_id(self, path: str) -> int:
        if not path:
            return 0
        cached = self._dir_ids.get(path)
        if cached is not None:
            return cached
        parent, _, name = path.rpartition("/")
        parent_id = self._dir_id(parent)
        self.con.execute("INSERT OR IGNORE INTO path(parent, name) VALUES(?, ?)", (parent_id, name))
        (path_id,) = self.con.execute(
            "SELECT id FROM path WHERE parent = ? AND name = ?", (parent_id, name)).fetchone()
        self._dir_ids[path] = path_id
        return path_id

    def _series_id(self, hostname: str, paths: list[str]) -> int:
        key = (hostname, json.dumps(sorted(paths)))
        self.con.execute("INSERT OR IGNORE INTO series(hostname, paths) VALUES(?, ?)", key)
        return self.con.execute(
            "SELECT id FROM series WHERE hostname = ? AND paths = ?", key).fetchone()["id"]

    @contextmanager
    def _write(self):
        try:
            self.con.execute("BEGIN IMMEDIATE")
        except sqlite3.OperationalError as e:
            if "locked" in str(e) or "busy" in str(e):
                raise IndexBusy("another process is updating the index") from None
            raise
        try:
            yield
        except BaseException:
            self.con.execute("ROLLBACK")
            self._dir_ids.clear()  # may hold ids of rows that were just rolled back
            raise
        self.con.execute("COMMIT")
