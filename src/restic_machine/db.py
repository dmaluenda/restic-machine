# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 David Maluenda

"""The index: one SQLite file per restic repository, kept in the user's data directory.

It holds every file name of every indexed snapshot, so it never goes inside the package or
a git checkout.

Layout. Snapshots are grouped in series (same host and paths, like
`restic --group-by host,paths`) and numbered by `seq`, their position in time within the
series. A `version` row is one state (type, size, mtime) of one path in one series, alive
from snapshot `first_seq` to `last_seq`; `last_seq` is OPEN while the version is still
alive in the latest indexed snapshot. A file that doesn't change across 500 snapshots is a
single row, so the index grows with the changes, not with snapshots x files.
"""

import hashlib
import json
import re
import sqlite3
from pathlib import Path

SCHEMA_VERSION = 1

NODE_TYPES = ("file", "dir", "symlink", "dev", "chardev", "fifo", "socket", "irregular")
TYPE_CODE = {name: code for code, name in enumerate(NODE_TYPES)}
DIR = TYPE_CODE["dir"]
IRREGULAR = TYPE_CODE["irregular"]

OPEN = 2**31 - 1  # last_seq of a version alive in the latest indexed snapshot

_SCHEMA = f"""
BEGIN IMMEDIATE;
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS series(
    id INTEGER PRIMARY KEY,
    hostname TEXT NOT NULL,
    paths TEXT NOT NULL,                        -- JSON list, sorted
    UNIQUE (hostname, paths));
CREATE TABLE IF NOT EXISTS snapshot(
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
    UNIQUE (series, seq));
CREATE TABLE IF NOT EXISTS path(
    id INTEGER PRIMARY KEY,
    parent INTEGER NOT NULL,                    -- 0 is the root
    name TEXT NOT NULL,
    UNIQUE (parent, name));
CREATE TABLE IF NOT EXISTS version(
    id INTEGER PRIMARY KEY,
    series INTEGER NOT NULL,
    path INTEGER NOT NULL,
    type INTEGER NOT NULL,                      -- index into NODE_TYPES
    size INTEGER,
    mtime_us INTEGER,
    first_seq INTEGER NOT NULL,
    last_seq INTEGER NOT NULL);                 -- {OPEN} = OPEN
CREATE INDEX IF NOT EXISTS version_by_path ON version(path, series, last_seq);
CREATE INDEX IF NOT EXISTS version_by_last ON version(series, last_seq);
CREATE INDEX IF NOT EXISTS version_by_first ON version(series, first_seq);
PRAGMA user_version = {SCHEMA_VERSION};
COMMIT;
"""


class IndexSchemaError(Exception):
    pass


def connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path, isolation_level=None)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA synchronous=NORMAL")
    con.execute("PRAGMA busy_timeout=5000")
    con.execute("PRAGMA temp_store=MEMORY")
    con.execute("PRAGMA cache_size=-65536")
    version = con.execute("PRAGMA user_version").fetchone()[0]
    if version == 0:
        con.executescript(_SCHEMA)
        version = con.execute("PRAGMA user_version").fetchone()[0]
    if version != SCHEMA_VERSION:
        con.close()
        raise IndexSchemaError(
            f"{path} was written by another version of restic-machine (schema {version});"
            " delete it to rebuild the index")
    return con


def open_index(data_dir: Path, repository_id: str) -> sqlite3.Connection:
    if not re.fullmatch(r"[0-9a-f]{8,128}", repository_id):
        raise ValueError(f"unexpected repository id: {repository_id!r}")
    con = connect(data_dir / repository_id / "index.sqlite")
    con.execute("INSERT OR IGNORE INTO meta(key, value) VALUES('repository_id', ?)", (repository_id,))
    return con


# The repository location may embed credentials (e.g. rest:https://user:pass@host/), so the
# catalog that maps a location to its index only stores a hash of it.

def _catalog(data_dir: Path) -> Path:
    return data_dir / "repositories.json"


def _key(label: str) -> str:
    return hashlib.sha256(label.encode()).hexdigest()


def remember_repository(data_dir: Path, label: str, repository_id: str) -> None:
    catalog = _catalog(data_dir)
    try:
        known = json.loads(catalog.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        known = {}
    if known.get(_key(label)) == repository_id:
        return
    known[_key(label)] = repository_id
    data_dir.mkdir(parents=True, exist_ok=True)
    tmp = catalog.with_suffix(".tmp")
    tmp.write_text(json.dumps(known, indent=2), encoding="utf-8")
    tmp.replace(catalog)


def known_repository(data_dir: Path, label: str) -> str | None:
    try:
        return json.loads(_catalog(data_dir).read_text(encoding="utf-8")).get(_key(label))
    except (OSError, ValueError):
        return None
