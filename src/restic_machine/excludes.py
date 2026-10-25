# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 David Maluenda

"""The backup's exclude file (what restic gets with --exclude-file), as Browse shows and edits it.

restic-machine doesn't run the backup itself yet: excluding something adds its full path to
the file the backup job passes to restic, so it takes effect from the next backup. The
snapshots already made keep it; taking it out of them is `restic rewrite`, a separate and
destructive step.

To show what is excluded, patterns are matched the way restic does: `*`, `?`, `[...]` within a
path component and `**` across components; a pattern that doesn't start at the root (or a
drive) matches anywhere in the path; matching a directory excludes everything under it; `!`
lines bring back what earlier lines excluded; $VARIABLES are expanded; case matters.
"""

import os
import re
import shutil
from datetime import date
from fnmatch import fnmatchcase
from functools import lru_cache
from pathlib import Path

MARK = "# added by restic-machine"
_DRIVE = re.compile(r"[A-Za-z]:")


def native_path(restic_path: str) -> str:
    """restic's name for a path in a snapshot -> the path on this computer (/C/x -> C:\\x on Windows)."""
    parts = [part for part in restic_path.split("/") if part]
    if os.name == "nt":
        if parts and len(parts[0]) == 1:
            return parts[0] + ":\\" + "\\".join(parts[1:])
        return "\\" + "\\".join(parts)
    return "/" + "/".join(parts)


def read(file: Path) -> list[str]:
    """The pattern lines of an exclude file (comments and blank lines left out)."""
    try:
        lines = file.read_text(encoding="utf-8-sig", errors="replace").splitlines()
    except FileNotFoundError:
        return []
    return [line.strip() for line in lines if line.strip() and not line.strip().startswith("#")]


def _components(text: str) -> tuple[bool, tuple[str, ...]]:
    """(anchored at the root?, components). Both separators are accepted, as on Windows."""
    text = text.replace("\\", "/")
    anchored = text.startswith("/") or bool(_DRIVE.match(text))
    return anchored, tuple(part for part in text.split("/") if part)


@lru_cache(maxsize=4096)
def _matches(pattern: tuple[str, ...], path: tuple[str, ...]) -> bool:
    if not pattern:
        return not path
    if pattern[0] == "**":
        return any(_matches(pattern[1:], path[i:]) for i in range(len(path) + 1))
    return bool(path) and fnmatchcase(path[0], pattern[0]) and _matches(pattern[1:], path[1:])


@lru_cache(maxsize=1024)
def _compiled(pattern: str) -> tuple[bool, tuple[str, ...], tuple[bool, ...]]:
    """(anchored?, components, which components are plain names, without wildcards)."""
    anchored, parts = _components(os.path.expandvars(pattern))
    return anchored, parts, tuple(not any(c in part for c in "*?[") for part in parts)


def _covers(pattern: str, path: tuple[str, ...], names: frozenset[str] = frozenset()) -> bool:
    """Does the pattern match the path or one of its parent directories? `names`, the path's
    components as a set, if given, rules most patterns out at once: a plain name of the
    pattern that the path doesn't have."""
    anchored, parts, plain = _compiled(pattern)
    if not parts:
        return False
    if names and any(is_plain and part not in names for part, is_plain in zip(parts, plain)):
        return False
    if "**" in parts:  # the general way, slower
        if not anchored:
            parts = ("**", *parts)
        return any(_matches(parts, path[:end]) for end in range(1, len(path) + 1))
    # Without **, the pattern is k components in a row: at the start if anchored, else anywhere.
    k = len(parts)
    if len(path) < k:
        return False
    starts = range(1 if anchored else len(path) - k + 1)
    return any(all(path[i + j] == parts[j] if plain[j] else fnmatchcase(path[i + j], parts[j])
                   for j in range(k)) for i in starts)


def excluded_by(path: str, patterns: list[str]) -> str | None:
    """The pattern that excludes `path` (a native path), or None. The last matching line wins,
    so a later `!pattern` brings it back."""
    _, parts = _components(path)
    names = frozenset(parts)
    verdict = None
    for line in patterns:
        negated = line.startswith("!")
        if _covers(line[1:] if negated else line, parts, names):
            verdict = None if negated else line
    return verdict


def add(file: Path, path: str) -> None:
    """Exclude `path` (native) from the next backups. The previous file is kept as .bak."""
    if path in read(file):
        return
    lines, style = _load(file)
    while lines and not lines[-1].strip():
        lines.pop()
    lines += ["", f"{MARK} on {date.today().isoformat()}", path]
    _save(file, lines, style)


def remove(file: Path, pattern: str) -> bool:
    """Take a pattern line out (and the restic-machine comment just above it, if any)."""
    if pattern not in read(file):
        return False
    lines, style = _load(file)
    kept = []
    for line in lines:
        if line.strip() == pattern:
            if kept and kept[-1].startswith(MARK):
                kept.pop()
                if kept and not kept[-1].strip():
                    kept.pop()
            continue
        kept.append(line)
    _save(file, kept, style)
    return True


# The file is the user's: keep its byte order mark and line endings as they are.

def _load(file: Path) -> tuple[list[str], tuple[bool, str]]:
    if not file.exists():
        return [], (False, os.linesep)
    raw = file.read_bytes()
    text = raw.decode("utf-8-sig")
    return text.splitlines(), (raw.startswith(b"\xef\xbb\xbf"), "\r\n" if "\r\n" in text else "\n")


def _save(file: Path, lines: list[str], style: tuple[bool, str]) -> None:
    bom, eol = style
    if file.exists():
        shutil.copy2(file, file.with_name(file.name + ".bak"))
    tmp = file.with_name(file.name + ".tmp")
    tmp.write_bytes((b"\xef\xbb\xbf" if bom else b"") + (eol.join(lines) + eol).encode("utf-8"))
    os.replace(tmp, file)
