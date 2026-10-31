# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 David Maluenda

"""Copies of the repository: mirrors of its files elsewhere (a backup of the backup, on a disk
kept offline). They are only read, never written to.

A copy holds what the repository held when it was made, so the index (one per repository id,
keeping the snapshots forgotten since) already knows what is in it. When a copy can be reached,
`restic-machine check --source NAME` records which snapshots it really holds (better than
deducing it from the time of the copy) and runs `restic check` on it; the job that makes the
copy can call it right after copying, and use its exit code:

    0   the copy is sound
    1   restic check found problems (restic's own exit code, kept)
    75  the copy can't be reached now (try again when it is)
    78  not set up right: no such copy, no location, or it isn't a copy of this repository

What was seen is kept in copies.json next to the index, per repository.
"""

import json
import os
import re
import socket
import time
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from . import backup, config, db
from .restic import RepositoryUnavailable, Restic, ResticError

EX_TEMPFAIL, EX_CONFIG = 75, 78


class CopyError(Exception):
    def __init__(self, message: str, code: int = EX_CONFIG):
        super().__init__(message)
        self.code = code


def find(cfg: config.Config, name: str) -> dict:
    copy = next((c for c in cfg.copies if c["name"].lower() == name.lower()), None)
    if copy is None:
        known = ", ".join(c["name"] for c in cfg.copies) or "none"
        raise CopyError(f"no copy called {name} in {cfg.path} (copies: {known})")
    if not copy["location"]:
        raise CopyError(f"{copy['name']}: no location set, so it can't be read")
    return copy


def login(cfg: config.Config, copy: dict) -> tuple[str, str]:
    """(user, password file) to log into the copy's Windows share, if it needs one: its own, or
    else the backup's when the copy is on the same server (one login per server is the rule)."""
    if copy.get("share_user"):
        return copy["share_user"], copy.get("share_password_file", "")
    share = cfg.backup["share"]
    location = copy["location"].replace("/", "\\")
    if share["user"] and share["path"] and location.startswith("\\\\"):
        same_server = backup._split_unc(location)[0].lower() == backup._split_unc(share["path"])[0].lower()
        if same_server:
            return share["user"], share["password_file"]
    return "", ""


@contextmanager
def reached(cfg: config.Config, copy: dict):
    """A Restic for the copy. On a Windows share that needs a login, it logs in first, by IP
    as the backup does (a session started over SSH or by the scheduler has no network login
    of its own), and logs out after."""
    location = copy["location"]
    user, password_file = login(cfg, copy)
    unc = None
    if user and location.replace("/", "\\").startswith("\\\\"):
        host, rest = backup._split_unc(location)
        try:
            address = socket.gethostbyname(host)
        except OSError as e:
            raise CopyError(f"{copy['name']}: cannot resolve {host}: {e}", EX_TEMPFAIL) from None
        unc = f"\\\\{address}\\{rest.split(chr(92))[0]}"
        location = f"\\\\{address}\\{rest}"
        try:
            backup.connect_share(unc, user, password_file)
        except (backup.BackupError, OSError) as e:
            raise CopyError(f"{copy['name']}: can't be reached now: {e}", EX_TEMPFAIL) from None
    try:
        env = {**cfg.restic_env, "RESTIC_REPOSITORY": location, "RESTIC_REPOSITORY_FILE": None}
        yield Restic(cfg.restic_binary, env)
    finally:
        if unc:
            backup.disconnect_share(unc)


# -- what was seen -----------------------------------------------------------------------

def _seen_file(cfg: config.Config) -> Path:
    return cfg.data_dir / "copies.json"


def seen(cfg: config.Config, repository_id: str) -> dict:
    """copy name -> {"seen_at", "snapshots": [ids], "check": {...}} for this repository."""
    try:
        return json.loads(_seen_file(cfg).read_text(encoding="utf-8")).get(repository_id, {})
    except (OSError, ValueError):
        return {}


def _remember(cfg: config.Config, repository_id: str, name: str, **values) -> None:
    path = _seen_file(cfg)
    try:
        everything = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        everything = {}
    everything.setdefault(repository_id, {}).setdefault(name, {}).update(values)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(everything, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def _on_a_filesystem(location: str) -> bool:
    """A local or shared folder, not one of restic's backends (sftp:, s3:, rest:...)."""
    return not re.match(r"[a-z][a-z0-9]+:", location) or bool(re.match(r"[A-Za-z]:[\\/]", location))


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


# -- check ---------------------------------------------------------------------------------

def check(cfg: config.Config, name: str, read_data_subset: str | None = None) -> int:
    """Record which snapshots the copy holds, then `restic check` it (reading `read_data_subset`
    of its data too, e.g. "11/12", if given). Logged to check.log next to the index; returns
    the exit code described above."""
    log = backup.Log(config.default_data_dir() / "check.log")
    started = time.monotonic()
    try:
        copy = find(cfg, name)
        repository_id = db.known_repository(cfg.data_dir, cfg.repository_label())
        if repository_id is None:
            raise CopyError("the repository itself has never been read here, so its copies can't be told apart")
        cfg.check_password_source()
        log.line(f"START check of {copy['name']} ({copy['location']})" +
                 (f", reading {read_data_subset} of its data" if read_data_subset else ""))
        with reached(cfg, copy) as restic:
            location = restic.env["RESTIC_REPOSITORY"]
            if _on_a_filesystem(location) and not Path(location, "config").exists():
                raise CopyError(f"{copy['name']}: not there now ({location})", EX_TEMPFAIL)  # a disk unplugged, say
            try:
                found = restic.repository_id()
            except RepositoryUnavailable as e:
                raise CopyError(f"{copy['name']}: can't be read now: {e}", EX_TEMPFAIL) from None
            if found != repository_id:
                raise CopyError(f"{copy['name']} holds another repository ({found[:8]}), not a copy of {repository_id[:8]}")
            snapshots = restic.snapshots()
            ids = sorted(s["id"] for s in snapshots)
            newest = max((s["time"] for s in snapshots), default=None)
            _remember(cfg, repository_id, copy["name"], seen_at=_now(), snapshots=ids, newest=newest)
            log.line(f"{len(ids)} snapshots on {copy['name']}" + (f", the newest from {newest[:16]}" if newest else ""))
            extra = ["--read-data-subset", read_data_subset] if read_data_subset else []
            log.line("restic check " + " ".join(extra))
            code = restic.check(extra, log.file)
        ok = code == 0
        _remember(cfg, repository_id, copy["name"], check={
            "at": _now(), "ok": ok, "exit_code": code, "read_data_subset": read_data_subset})
        log.line(f"{'OK' if ok else f'ERROR: restic check failed ({code})'} ({time.monotonic() - started:.0f} s)")
        return code
    except CopyError as e:
        log.line(f"ERROR: {e}")
        return e.code
    except ResticError as e:
        log.line(f"ERROR: {e}")
        return EX_TEMPFAIL if isinstance(e, RepositoryUnavailable) else EX_CONFIG
    except (config.ConfigError, backup.BackupError) as e:
        log.line(f"ERROR: {e}")
        return EX_CONFIG
    except OSError as e:
        log.line(f"ERROR: {e}")
        return EX_TEMPFAIL
    finally:
        log.close()
