# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 David Maluenda

"""Read queries on the index, shared by the command line and the web UI.

Entries come back as plain dicts (ready for JSON): `type` is a name from db.NODE_TYPES and,
for a directory, `size` and `files` are the totals of everything below it.
"""

import re
import sqlite3
from bisect import bisect_right
from datetime import datetime, timezone

from .db import DIR, NODE_TYPES

# Rows of `version` / `dir_total` alive in snapshot :seq of :series, for a table alias.
# The unary + keeps SQLite from walking the whole table by first_seq: these lookups must go
# through the (path, series, last_seq) indexes, one path at a time.
_ALIVE = "{t}.series = :series AND +{t}.first_seq <= :{seq} AND {t}.last_seq >= :{seq}"


def snapshots(con: sqlite3.Connection, include_forgotten: bool = False,
              series: int | None = None) -> list[sqlite3.Row]:
    """Snapshots in time order, indexed or still pending (seq is NULL), with the total size
    (`total_size`, `total_files`) of the indexed ones."""
    where, args = [], []
    if not include_forgotten:
        where.append("s.forgotten_at IS NULL")
    if series is not None:
        where.append("s.series = ?")
        args.append(series)
    return con.execute(
        "SELECT s.*, se.hostname, se.paths, t.size AS total_size, t.files AS total_files"
        " FROM snapshot s JOIN series se ON se.id = s.series"
        " LEFT JOIN dir_total t ON t.path = 0 AND t.series = s.series"
        "  AND t.first_seq <= s.seq AND t.last_seq >= s.seq"
        f" {'WHERE ' + ' AND '.join(where) if where else ''}"
        " ORDER BY s.time_us, s.restic_id", args).fetchall()


def find_snapshot(con: sqlite3.Connection, ref: str) -> sqlite3.Row:
    """`latest`, or a restic snapshot id or a unique prefix of it. Forgotten snapshots can
    still be opened by id, to investigate."""
    if ref == "latest":
        rows = con.execute(
            "SELECT * FROM snapshot WHERE seq IS NOT NULL AND forgotten_at IS NULL"
            " ORDER BY time_us DESC LIMIT 1").fetchall()
    elif re.fullmatch(r"[0-9a-f]{1,64}", ref):
        rows = con.execute("SELECT * FROM snapshot WHERE restic_id LIKE ? || '%' LIMIT 2", (ref,)).fetchall()
    else:
        rows = []
    if not rows:
        raise LookupError(f"no snapshot matches {ref!r}")
    if len(rows) > 1:
        raise LookupError(f"{ref!r} matches more than one snapshot; give more characters")
    if rows[0]["seq"] is None:
        raise LookupError(f"snapshot {rows[0]['restic_id'][:8]} is not indexed")
    return rows[0]


def previous_snapshot(con: sqlite3.Connection, snap: sqlite3.Row, shown=None) -> sqlite3.Row | None:
    """The visible snapshot before `snap` in its series: what its changes are measured against.
    `shown` (a test on snapshot rows) says which are visible; by default, those restic still has."""
    if shown is None:
        return con.execute(
            "SELECT * FROM snapshot WHERE series = ? AND seq < ? AND forgotten_at IS NULL"
            " ORDER BY seq DESC LIMIT 1", (snap["series"], snap["seq"])).fetchone()
    rows = con.execute("SELECT * FROM snapshot WHERE series = ? AND seq < ? ORDER BY seq DESC",
                       (snap["series"], snap["seq"]))
    return next((row for row in rows if shown(row)), None)


# -- copies of the repository ---------------------------------------------------------------
# A copy (a mirror of the repository's files, say on a disk kept offline) holds what the
# repository held when it was made: the snapshots made before then and not yet forgotten.

def copy_cutoff(copied_at: str) -> tuple[int, str]:
    """A copy's time, local "YYYY-MM-DDTHH:MM", as the index compares it: (time_us, and the
    UTC text that forgotten_at is written in)."""
    moment = datetime.fromisoformat(copied_at).astimezone()
    return int(moment.timestamp() * 1_000_000), moment.astimezone(timezone.utc).isoformat(timespec="seconds")


def on_copy(row: sqlite3.Row, cutoff: tuple[int, str]) -> bool:
    """Does a copy made at `cutoff` hold this (indexed) snapshot?"""
    time_us, utc = cutoff
    return (row["seq"] is not None and row["replaced_by"] is None and row["time_us"] <= time_us
            and (row["forgotten_at"] is None or row["forgotten_at"] > utc))


def normalize(path: str) -> str:
    return "/" + "/".join(part for part in path.split("/") if part)


def resolve_path(con: sqlite3.Connection, path: str) -> int:
    """restic-style path ("/C/Users/me", "/home/me") -> path id; the root is 0."""
    path_id = 0
    for name in (part for part in path.split("/") if part):
        row = con.execute("SELECT id FROM path WHERE parent = ? AND name = ?", (path_id, name)).fetchone()
        if row is None:
            raise LookupError(f"{normalize(path)}: not in the index")
        path_id = row["id"]
    return path_id


def _directory(con: sqlite3.Connection, snap: sqlite3.Row, path: str) -> int:
    dir_id = resolve_path(con, path)
    if dir_id:
        row = con.execute(
            f"SELECT type FROM version v WHERE v.path = :path AND {_ALIVE.format(t='v', seq='seq')}",
            {"path": dir_id, "series": snap["series"], "seq": snap["seq"]}).fetchone()
        if row is None:
            raise LookupError(f"{normalize(path)}: not in snapshot {snap['restic_id'][:8]}")
        if row["type"] != DIR:
            raise LookupError(f"{normalize(path)}: not a directory")
    return dir_id


def _entry(name, type_code, size, mtime_us, total_size, total_files) -> dict:
    is_dir = type_code == DIR
    return {"name": name, "type": NODE_TYPES[type_code],
            "size": (total_size or 0) if is_dir else size,
            "files": (total_files or 0) if is_dir else None,
            "mtime_us": mtime_us, "status": None}


def _sorted(entries: list[dict]) -> list[dict]:
    return sorted(entries, key=lambda e: (e["type"] != "dir", e["name"].casefold(), e["name"]))


def list_dir(con: sqlite3.Connection, snap: sqlite3.Row, path: str = "/",
             previous: sqlite3.Row | None = None) -> list[dict]:
    """A directory as it was in `snap`, directories first. Against `previous` (any earlier
    snapshot of the same series), each entry gets a status: "added", "modified" (a file, or a
    type change), "changed" (a directory with something changed below it) or None, and its
    size and file count then (`prev_size`, `prev_files`; None if it wasn't there)."""
    args = {"dir": _directory(con, snap, path), "series": snap["series"], "seq": snap["seq"],
            "prev": previous["seq"] if previous is not None else None}
    rows = con.execute(
        "SELECT p.name, v.type, v.size, v.mtime_us, t.id AS total_id, t.size AS total_size,"
        " t.files AS total_files, pv.type AS prev_type, pv.size AS prev_size,"
        " pv.mtime_us AS prev_mtime, pt.id AS prev_total_id, pt.size AS prev_total_size,"
        " pt.files AS prev_total_files"
        " FROM path p"  # the folder's children first (CROSS JOIN keeps that order): left to
        # itself, the planner may read every version alive in the snapshot instead (0.1 s a folder)
        f" CROSS JOIN version v ON v.path = p.id AND {_ALIVE.format(t='v', seq='seq')}"
        f" LEFT JOIN dir_total t ON t.path = p.id AND {_ALIVE.format(t='t', seq='seq')}"
        f" LEFT JOIN version pv ON pv.path = p.id AND {_ALIVE.format(t='pv', seq='prev')}"
        f" LEFT JOIN dir_total pt ON pt.path = p.id AND {_ALIVE.format(t='pt', seq='prev')}"
        " WHERE p.parent = :dir", args).fetchall()
    entries = []
    for row in rows:
        entry = _entry(row["name"], row["type"], row["size"], row["mtime_us"],
                       row["total_size"], row["total_files"])
        if previous is not None:
            was_dir = row["prev_type"] == DIR
            entry["prev_size"] = (row["prev_total_size"] or 0) if was_dir else row["prev_size"]
            entry["prev_files"] = (row["prev_total_files"] or 0) if was_dir else None
            if row["prev_type"] is None:
                entry["status"] = "added"
            elif row["prev_type"] != row["type"]:
                entry["status"] = "modified"
            elif row["type"] == DIR:
                entry["status"] = "changed" if row["total_id"] != row["prev_total_id"] else None
            elif (row["size"], row["mtime_us"]) != (row["prev_size"], row["prev_mtime"]):
                entry["status"] = "modified"
        entries.append(entry)
    return _sorted(entries)


def removed_entries(con: sqlite3.Connection, snap: sqlite3.Row, previous: sqlite3.Row,
                    path: str = "/") -> list[dict]:
    """What the directory had in `previous` and no longer has in `snap`, as it was then. The
    directory may be gone from `snap` itself: then that is everything it had."""
    try:
        dir_id = _directory(con, snap, path)
    except LookupError:
        dir_id = _directory(con, previous, path)
    args = {"dir": dir_id, "series": snap["series"], "seq": snap["seq"], "prev": previous["seq"]}
    rows = con.execute(
        "SELECT p.name, v.type, v.size, v.mtime_us, t.size AS total_size, t.files AS total_files"
        " FROM path p"  # children first, as in list_dir
        f" CROSS JOIN version v ON v.path = p.id AND {_ALIVE.format(t='v', seq='prev')}"
        f" LEFT JOIN dir_total t ON t.path = p.id AND {_ALIVE.format(t='t', seq='prev')}"
        " WHERE p.parent = :dir AND NOT EXISTS (SELECT 1 FROM version x"
        f"  WHERE x.path = p.id AND {_ALIVE.format(t='x', seq='seq')})", args).fetchall()
    entries = []
    for row in rows:
        entry = _entry(row["name"], row["type"], row["size"], row["mtime_us"],
                       row["total_size"], row["total_files"])
        entry["status"] = "removed"
        entries.append(entry)
    return _sorted(entries)


def entry(con: sqlite3.Connection, snap: sqlite3.Row, path: str) -> dict:
    """One path as it was in `snap`: its type, size and mtime (LookupError if it wasn't there)."""
    path_id = resolve_path(con, path)
    row = con.execute(
        f"SELECT type, size, mtime_us FROM version v WHERE v.path = :path AND {_ALIVE.format(t='v', seq='seq')}",
        {"path": path_id, "series": snap["series"], "seq": snap["seq"]}).fetchone()
    if not path_id or row is None:
        raise LookupError(f"{normalize(path)}: not in snapshot {snap['restic_id'][:8]}")
    return {"type": NODE_TYPES[row["type"]], "size": row["size"], "mtime_us": row["mtime_us"]}


def history(con: sqlite3.Connection, snap: sqlite3.Row, path: str, shown=None) -> list[dict]:
    """A path across the visible snapshots of `snap`'s series (`shown`: as previous_snapshot):
    one point per snapshot with whether it existed and its size (directories: totals below it)
    and mtime."""
    path_id = resolve_path(con, path)
    key = {"path": path_id, "series": snap["series"]}
    versions = con.execute(
        "SELECT type, size, mtime_us, first_seq, last_seq FROM version"
        " WHERE path = :path AND series = :series ORDER BY first_seq", key).fetchall()
    totals = con.execute(
        "SELECT size, files, first_seq, last_seq FROM dir_total"
        " WHERE path = :path AND series = :series ORDER BY first_seq", key).fetchall()
    snaps = con.execute(
        "SELECT * FROM snapshot WHERE series = ? AND seq IS NOT NULL ORDER BY seq", (snap["series"],)).fetchall()
    snaps = [s for s in snaps if (shown(s) if shown else s["forgotten_at"] is None)]

    def alive_at(rows):
        firsts = [row["first_seq"] for row in rows]

        def lookup(seq):
            i = bisect_right(firsts, seq) - 1
            return rows[i] if i >= 0 and rows[i]["last_seq"] >= seq else None
        return lookup

    version_at, total_at = alive_at(versions), alive_at(totals)
    points = []
    for s in snaps:
        point = {"snapshot": s["restic_id"], "time": s["time"], "live": s["forgotten_at"] is None,
                 "present": False, "type": None, "size": None, "files": None, "mtime_us": None}
        version = version_at(s["seq"]) if path_id else None
        if path_id == 0 or version is not None:  # the root is always there
            type_code = version["type"] if version else DIR
            total = total_at(s["seq"]) if type_code == DIR else None
            entry = _entry(None, type_code, version["size"] if version else None,
                           version["mtime_us"] if version else None,
                           total["size"] if total else None, total["files"] if total else None)
            point.update(present=True, type=entry["type"], size=entry["size"], files=entry["files"],
                         mtime_us=entry["mtime_us"])
        points.append(point)
    return points
