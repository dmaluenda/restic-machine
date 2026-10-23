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
single row, so the index grows with the changes, not with snapshots x files. `dir_total`
keeps the size of every directory the same way (see totals.py). The tables themselves are
created and upgraded in migrations.py.
"""

import hashlib
import json
import re
import sqlite3
from pathlib import Path

NODE_TYPES = ("file", "dir", "symlink", "dev", "chardev", "fifo", "socket", "irregular")
TYPE_CODE = {name: code for code, name in enumerate(NODE_TYPES)}
DIR = TYPE_CODE["dir"]
IRREGULAR = TYPE_CODE["irregular"]

OPEN = 2**31 - 1  # last_seq of a version alive in the latest indexed snapshot


class IndexSchemaError(Exception):
    pass


def connect(path: Path) -> sqlite3.Connection:
    """Open (creating or upgrading it if needed) the index at `path`."""
    from . import migrations  # imported here: migrations needs this module's names

    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path, isolation_level=None)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA synchronous=NORMAL")
    con.execute("PRAGMA busy_timeout=5000")
    con.execute("PRAGMA temp_store=MEMORY")
    con.execute("PRAGMA cache_size=-65536")
    try:
        migrations.upgrade(con, path)
    except BaseException:
        con.close()
        raise
    return con


def index_file(data_dir: Path, repository_id: str) -> Path:
    if not re.fullmatch(r"[0-9a-f]{8,128}", repository_id):
        raise ValueError(f"unexpected repository id: {repository_id!r}")
    return data_dir / repository_id / "index.sqlite"


def open_index(data_dir: Path, repository_id: str) -> sqlite3.Connection:
    con = connect(index_file(data_dir, repository_id))
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
