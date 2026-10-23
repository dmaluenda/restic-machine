# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 David Maluenda

"""Upgrades of the index format, so an existing index never has to be rebuilt.

The index records its format version (PRAGMA user_version). Opening it with a newer
restic-machine applies the missing steps in order, each in its own transaction, after
saving a copy of the file next to it. A new index starts at version 0 and goes through the
very same steps, so every index is built the same way.

Rules for new steps: never change a released step, append a new one. What can be derived
from the index is filled in for the whole history. What would have to be read from restic
again is left NULL, meaning "unknown", for the snapshots already indexed: restic can't
give back the ones it has forgotten.
"""

import logging
import sqlite3
from contextlib import closing
from pathlib import Path

from . import db
from .totals import update_dir_totals

log = logging.getLogger("restic_machine")


def _v1_initial_schema(con: sqlite3.Connection) -> None:
    for statement in (
        "CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT NOT NULL)",
        """CREATE TABLE series(
            id INTEGER PRIMARY KEY,
            hostname TEXT NOT NULL,
            paths TEXT NOT NULL,                        -- JSON list, sorted
            UNIQUE (hostname, paths))""",
        """CREATE TABLE snapshot(
            id INTEGER PRIMARY KEY,
            restic_id TEXT NOT NULL UNIQUE,
            series INTEGER NOT NULL REFERENCES series(id),
            seq INTEGER,                                -- position within the series, once indexed
            time TEXT NOT NULL,                         -- as restic reports it (offset of the backup host)
            time_us INTEGER NOT NULL,                   -- UTC microseconds, for ordering
            tree TEXT NOT NULL,
            info TEXT NOT NULL,                         -- the whole `restic snapshots --json` record
            indexed_at TEXT,
            forgotten_at TEXT,                          -- gone from the repository: hidden, kept a while
            replaced_by INTEGER REFERENCES snapshot(id), -- same content under a new id (e.g. retagged)
            n_entries INTEGER,
            n_added INTEGER,
            n_modified INTEGER,
            n_removed INTEGER,
            UNIQUE (series, seq))""",
        """CREATE TABLE path(
            id INTEGER PRIMARY KEY,
            parent INTEGER NOT NULL,                    -- 0 is the root
            name TEXT NOT NULL,
            UNIQUE (parent, name))""",
        """CREATE TABLE version(
            id INTEGER PRIMARY KEY,
            series INTEGER NOT NULL,
            path INTEGER NOT NULL,
            type INTEGER NOT NULL,                      -- index into db.NODE_TYPES
            size INTEGER,
            mtime_us INTEGER,
            first_seq INTEGER NOT NULL,
            last_seq INTEGER NOT NULL)                  -- db.OPEN while alive in the latest snapshot""",
        "CREATE INDEX version_by_path ON version(path, series, last_seq)",
        "CREATE INDEX version_by_last ON version(series, last_seq)",
        "CREATE INDEX version_by_first ON version(series, first_seq)",
    ):
        con.execute(statement)


def _v2_directory_totals(con: sqlite3.Connection) -> None:
    con.execute("""CREATE TABLE dir_total(
        id INTEGER PRIMARY KEY,
        series INTEGER NOT NULL,
        path INTEGER NOT NULL,                          -- 0 is the root: the whole snapshot
        size INTEGER NOT NULL,                          -- bytes in the files below it
        files INTEGER NOT NULL,
        first_seq INTEGER NOT NULL,
        last_seq INTEGER NOT NULL)""")
    con.execute("CREATE INDEX dir_total_by_path ON dir_total(path, series, last_seq)")
    con.execute("CREATE INDEX dir_total_by_last ON dir_total(series, last_seq)")
    for series, last in con.execute(
            "SELECT series, max(seq) FROM snapshot WHERE seq IS NOT NULL GROUP BY series").fetchall():
        for seq in range(1, last + 1):
            update_dir_totals(con, series, seq)


# _STEPS[n] takes the index from version n to version n + 1.
_STEPS = (_v1_initial_schema, _v2_directory_totals)
SCHEMA_VERSION = len(_STEPS)


def _version(con: sqlite3.Connection) -> int:
    return con.execute("PRAGMA user_version").fetchone()[0]


def upgrade(con: sqlite3.Connection, path: Path) -> None:
    current = _version(con)
    if current > SCHEMA_VERSION:
        raise db.IndexSchemaError(
            f"{path} was written by a newer restic-machine (index format {current}); upgrade restic-machine")
    if current == SCHEMA_VERSION:
        return
    if current > 0:
        backup = path.with_name(f"{path.stem}.v{current}.bak")
        log.info("upgrading the index from format %d to %d (copy of the old one: %s)",
                 current, SCHEMA_VERSION, backup)
        with closing(sqlite3.connect(backup)) as copy:
            con.backup(copy)
    for step in range(current, SCHEMA_VERSION):
        con.execute("BEGIN IMMEDIATE")
        try:
            if _version(con) == step:  # another process may have upgraded it meanwhile
                _STEPS[step](con)
                con.execute(f"PRAGMA user_version = {step + 1}")
        except BaseException:
            con.execute("ROLLBACK")
            raise
        con.execute("COMMIT")
