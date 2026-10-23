# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 David Maluenda

"""Total size and file count of every directory, kept like file versions.

A `dir_total` row is one state of a directory's totals in one series, alive from snapshot
`first_seq` to `last_seq` (OPEN while current). A directory gets a new row whenever
anything below it changes, even when the totals stay the same, so "did anything change in
here since snapshot X?" is a single lookup. The root of a snapshot is path 0.
"""

import sqlite3
from collections import defaultdict

from .db import OPEN, TYPE_CODE

FILE = TYPE_CODE["file"]

_CHANGES_BY_PARENT = """
    SELECT p.parent AS dir, sum(CASE WHEN v.type = :file THEN coalesce(v.size, 0) ELSE 0 END) AS size,
           sum(v.type = :file) AS files
    FROM version v JOIN path p ON p.id = v.path
    WHERE v.series = :series AND v.{column} = :seq
    GROUP BY p.parent"""


def update_dir_totals(con: sqlite3.Connection, series: int, seq: int) -> None:
    """Carry the totals of snapshot seq - 1 over to `seq`, from the versions that start at
    `seq` and the ones that ended at seq - 1. Runs inside the caller's transaction."""
    parents: dict[int, int] = {}
    delta: dict[int, list[int]] = defaultdict(lambda: [0, 0])

    def bubble(directory: int, size: int, files: int) -> None:
        while True:
            entry = delta[directory]
            entry[0] += size
            entry[1] += files
            if directory == 0:
                return
            if directory not in parents:
                parents[directory] = con.execute(
                    "SELECT parent FROM path WHERE id = ?", (directory,)).fetchone()[0]
            directory = parents[directory]

    args = {"series": series, "file": FILE}
    for row in con.execute(_CHANGES_BY_PARENT.format(column="first_seq"), {**args, "seq": seq}).fetchall():
        bubble(row["dir"], row["size"], row["files"])
    if seq > 1:
        for row in con.execute(_CHANGES_BY_PARENT.format(column="last_seq"), {**args, "seq": seq - 1}).fetchall():
            bubble(row["dir"], -row["size"], -row["files"])
    if not delta:
        return

    con.execute("CREATE TEMP TABLE IF NOT EXISTS total_delta(path INTEGER PRIMARY KEY, size INTEGER, files INTEGER)")
    con.execute("DELETE FROM total_delta")
    con.executemany("INSERT INTO total_delta VALUES(?, ?, ?)",
                    [(path, size, files) for path, (size, files) in delta.items()])
    args = {"series": series, "seq": seq, "prev": seq - 1, "open": OPEN}
    current = ("(SELECT t.{} FROM dir_total t WHERE t.path = total_delta.path"
               " AND t.series = :series AND t.last_seq = :open)")
    con.execute(f"UPDATE total_delta SET size = size + coalesce({current.format('size')}, 0),"
                f" files = files + coalesce({current.format('files')}, 0)", args)
    con.execute("UPDATE dir_total SET last_seq = :prev WHERE series = :series AND last_seq = :open"
                " AND path IN (SELECT path FROM total_delta)", args)
    con.execute("INSERT INTO dir_total(series, path, size, files, first_seq, last_seq)"
                " SELECT :series, path, size, files, :seq, :open FROM total_delta", args)
    con.execute("DELETE FROM total_delta")
