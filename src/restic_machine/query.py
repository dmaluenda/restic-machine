# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 David Maluenda

"""Read queries on the index, shared by the command line and the web UI."""

import re
import sqlite3

from .db import DIR


def snapshots(con: sqlite3.Connection, include_forgotten: bool = False) -> list[sqlite3.Row]:
    """Snapshots in time order, indexed or still pending (seq is NULL)."""
    where = "" if include_forgotten else "WHERE s.forgotten_at IS NULL"
    return con.execute(
        "SELECT s.*, se.hostname, se.paths FROM snapshot s JOIN series se ON se.id = s.series"
        f" {where} ORDER BY s.time_us, s.restic_id").fetchall()


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


def resolve_path(con: sqlite3.Connection, path: str) -> int:
    """restic-style path ("/C/Users/me", "/home/me") -> path id; the root is 0."""
    path_id = 0
    for name in (part for part in path.split("/") if part):
        row = con.execute("SELECT id FROM path WHERE parent = ? AND name = ?", (path_id, name)).fetchone()
        if row is None:
            raise LookupError(f"{path}: not in the index")
        path_id = row["id"]
    return path_id


def list_dir(con: sqlite3.Connection, snap: sqlite3.Row, path: str = "/") -> list[sqlite3.Row]:
    """The entries of a directory as they were in a snapshot: name, type, size, mtime_us.
    Directories first, then by name."""
    alive = {"series": snap["series"], "seq": snap["seq"]}
    dir_id = resolve_path(con, path)
    if dir_id:
        row = con.execute(
            "SELECT type FROM version WHERE path = :path AND series = :series"
            " AND last_seq >= :seq AND first_seq <= :seq", {**alive, "path": dir_id}).fetchone()
        if row is None:
            raise LookupError(f"{path}: not in snapshot {snap['restic_id'][:8]}")
        if row["type"] != DIR:
            raise LookupError(f"{path}: not a directory")
    return con.execute(
        "SELECT p.name, v.type, v.size, v.mtime_us FROM path p JOIN version v ON v.path = p.id"
        " WHERE p.parent = :dir AND v.series = :series AND v.last_seq >= :seq AND v.first_seq <= :seq"
        " ORDER BY v.type != :dir_type, p.name COLLATE NOCASE",
        {**alive, "dir": dir_id, "dir_type": DIR}).fetchall()
