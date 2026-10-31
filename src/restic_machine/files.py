# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 David Maluenda

"""A file of a snapshot, from the Browse page: restored next to the original, or read as text
to compare two versions of it (or one with the file on disk now) line by line.

Restoring never overwrites anything: the file goes next to the original, named after the
snapshot's date and time (`notes_20261024-1600.txt`), and only if that name is free.
"""

import difflib
import os
import tempfile
from datetime import datetime
from pathlib import Path

from . import excludes
from .restic import Restic

TEXT_LIMIT = 4 * 1024 * 1024  # bytes: bigger files aren't compared


class FileError(Exception):
    """What was asked can't be done with this file (not a file, not text, too big, name taken)."""


def snapshot_time(snap) -> datetime:
    """The snapshot's time, local."""
    return datetime.fromtimestamp(snap["time_us"] / 1_000_000)


def restored_path(native: str, when: datetime) -> Path:
    """Where a restored file goes: next to the original, as `name_YYYYMMDD-HHMM.ext`."""
    original = Path(native)
    return original.with_name(f"{original.stem}_{when:%Y%m%d-%H%M}{original.suffix}")


def restore(restic: Restic, snap, path: str, info: dict) -> Path:
    """Write the file `path` as it was in `snap` next to the original (see restored_path),
    with its modification time then. `info` is its index entry."""
    if info["type"] != "file":
        raise FileError("only files can be restored so far")
    target = restored_path(excludes.native_path(path), snapshot_time(snap))
    target.parent.mkdir(parents=True, exist_ok=True)  # it may be in a folder removed since
    try:
        out = open(target, "xb")  # never over an existing file
    except FileExistsError:
        raise FileError(f"{target} already exists: rename or move it first") from None
    try:
        with out:
            restic.dump(snap["restic_id"], path, out)
    except BaseException:
        target.unlink(missing_ok=True)
        raise
    if info["mtime_us"] is not None:
        os.utime(target, ns=(info["mtime_us"] * 1000, info["mtime_us"] * 1000))
    return target


# -- comparing text --------------------------------------------------------------------------

def text_lines(data: bytes) -> list[str]:
    """The lines of a text file: UTF-8, or else the Windows code page, or else Latin-1."""
    if b"\0" in data[:8192]:
        raise FileError("not a text file")
    for encoding in ("utf-8-sig", "cp1252"):
        try:
            return data.decode(encoding).splitlines()
        except UnicodeDecodeError:
            continue
    return data.decode("latin-1").splitlines()


def _check_size(size: int | None) -> None:
    if size is not None and size > TEXT_LIMIT:
        raise FileError(f"too big to compare here ({size / 1024 / 1024:.1f} MiB; up to {TEXT_LIMIT // 1024 // 1024} MiB)")


def snapshot_text(restic: Restic, snap, path: str, info: dict) -> list[str]:
    if info["type"] != "file":
        raise FileError("only files can be compared")
    _check_size(info["size"])
    with tempfile.TemporaryFile() as out:
        restic.dump(snap["restic_id"], path, out)
        out.seek(0)
        return text_lines(out.read())


def disk_text(native: str) -> list[str] | None:
    """The file as it is on disk now; None if it isn't there."""
    try:
        if not os.path.isfile(native):
            return None
        _check_size(os.path.getsize(native))
        return text_lines(Path(native).read_bytes())
    except OSError as e:
        raise FileError(f"can't read {native}: {e.strerror or e}") from None


def side_by_side(left: list[str], right: list[str]) -> tuple[list[list], int]:
    """Aligned rows `[kind, left number, left line, right number, right line]` (kind: same,
    changed, removed, added; a missing side is None) and the number of changed blocks."""
    rows, changes = [], 0
    big = len(left) + len(right) > 20000  # the junk heuristic keeps long files quick
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, left, right, autojunk=big).get_opcodes():
        if tag == "equal":
            rows += [["same", i + 1, left[i], j + 1, right[j]] for i, j in zip(range(i1, i2), range(j1, j2))]
            continue
        changes += 1
        for k in range(max(i2 - i1, j2 - j1)):
            i, j = i1 + k, j1 + k
            kind = "changed" if i < i2 and j < j2 else "removed" if i < i2 else "added"
            rows.append([kind, i + 1 if i < i2 else None, left[i] if i < i2 else None,
                         j + 1 if j < j2 else None, right[j] if j < j2 else None])
    return rows, changes
