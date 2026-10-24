# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 David Maluenda

"""One backup run, as the schedule starts it: `restic-machine backup`.

1. Only one run at a time (a named lock, shared with an older backup script if it uses the same
   name, so the two never overlap while moving from one to the other).
2. If the repository is on a Windows share that needs its own login, connect it first, by IP
   when asked (Windows allows one login per server name, and the user may be browsing the same
   server under another account), with the user and the password file given: the password is
   read from the file, never stored.
3. `restic backup` of the sources, with the exclude file, and a VSS snapshot on Windows (that
   needs administrator rights, which the scheduled task has).
4. On the run at the prune time, or when the last prune is more than 30 hours old (the computer
   was off), the retention policy: `restic forget` + prune.

Everything, restic's output included, goes to backup.log next to the index.
"""

import ctypes
import os
import socket
import subprocess
from datetime import datetime, timedelta
from pathlib import Path

from . import config

PRUNE_CATCH_UP = timedelta(hours=30)
PRUNE_SPACING = timedelta(hours=12)
_NO_WINDOW = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}


class BackupError(Exception):
    pass


def log_file(cfg: config.Config) -> Path:
    return config.default_data_dir() / "backup.log"


def _last_prune_file() -> Path:
    return config.default_data_dir() / "last_prune.txt"


class Log:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.file = open(path, "ab")

    def line(self, text: str) -> None:
        line = f"{datetime.now():%Y-%m-%d %H:%M:%S} {text}"
        print(line, flush=True)
        self.file.write((line + "\n").encode("utf-8"))
        self.file.flush()

    def close(self) -> None:
        self.file.close()


# -- one run at a time ----------------------------------------------------------------------

class SingleRun:
    """A system-wide named lock (a Windows mutex, a lock file elsewhere), not waited for."""

    def __init__(self, name: str):
        self.name, self.handle = name, None

    def acquire(self) -> bool:
        if os.name == "nt":
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_bool, ctypes.c_wchar_p]
            kernel32.CreateMutexW.restype = ctypes.c_void_p
            kernel32.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
            kernel32.ReleaseMutex.argtypes = kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
            handle = kernel32.CreateMutexW(None, False, self.name)
            if not handle:
                raise BackupError(f"could not create the lock {self.name}")
            if kernel32.WaitForSingleObject(handle, 0) not in (0, 0x80):  # owned now, or abandoned by a dead run
                kernel32.CloseHandle(ctypes.c_void_p(handle))
                return False
            self.handle = (kernel32, handle)
            return True
        import fcntl
        lock = open(config.default_data_dir() / "backup.lock", "w")
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            lock.close()
            return False
        self.handle = lock
        return True

    def release(self) -> None:
        if self.handle is None:
            return
        if os.name == "nt":
            kernel32, handle = self.handle
            kernel32.ReleaseMutex(ctypes.c_void_p(handle))
            kernel32.CloseHandle(ctypes.c_void_p(handle))
        else:
            self.handle.close()
        self.handle = None


# -- the Windows share ----------------------------------------------------------------------

def _split_unc(unc: str) -> tuple[str, str]:
    parts = unc.replace("/", "\\").lstrip("\\").split("\\")
    if len(parts) < 2 or not parts[0] or not parts[1]:
        raise BackupError(f"not a share like \\\\server\\name: {unc}")
    return parts[0], "\\".join(parts[1:])


def share_paths(cfg: config.Config, repository: str) -> tuple[str, str]:
    """(the share to connect, the repository through it), by IP if asked."""
    share = cfg.backup["share"]
    host, rest = _split_unc(share["path"])
    target = host
    if share["by_ip"]:
        try:
            target = socket.gethostbyname(host)
        except OSError as e:
            raise BackupError(f"cannot resolve {host}: {e}") from None
    unc = f"\\\\{target}\\{rest}"
    canonical = f"\\\\{host}\\{rest}".lower()
    repo = repository.replace("/", "\\")
    if repo.lower().startswith(canonical):
        repo = unc + repo[len(canonical):]
    return unc, repo


class _NetResource(ctypes.Structure):
    _fields_ = [("dwScope", ctypes.c_uint32), ("dwType", ctypes.c_uint32), ("dwDisplayType", ctypes.c_uint32),
                ("dwUsage", ctypes.c_uint32), ("lpLocalName", ctypes.c_wchar_p), ("lpRemoteName", ctypes.c_wchar_p),
                ("lpComment", ctypes.c_wchar_p), ("lpProvider", ctypes.c_wchar_p)]


def connect_share(unc: str, user: str, password_file: str) -> None:
    """Log into the share for this session only, replacing a previous login to it."""
    if os.name != "nt":
        raise BackupError("logging into a Windows share is only possible on Windows")
    password = Path(password_file).expanduser().read_text(encoding="utf-8-sig").strip()
    mpr = ctypes.WinDLL("mpr")
    mpr.WNetCancelConnection2W(unc, 0, True)
    resource = _NetResource(dwType=1, lpRemoteName=unc)  # RESOURCETYPE_DISK
    code = mpr.WNetAddConnection2W(ctypes.byref(resource), password, user or None, 0)
    if code != 0:
        raise BackupError(f"could not log into {unc} as {user}: Windows error {code} ({ctypes.FormatError(code).strip()})")


def disconnect_share(unc: str) -> None:
    if os.name == "nt":
        ctypes.WinDLL("mpr").WNetCancelConnection2W(unc, 0, True)


# -- the run -----------------------------------------------------------------------------------

def prune_due(cfg: config.Config, now: datetime) -> bool:
    """On the run at the prune time, or to catch up after 30 hours without one. The prune time
    is matched within an hour either way, because a change to or from daylight saving time can
    move a run by an hour, but then at most once in 12 hours. With no record of a prune yet,
    only at the prune time."""
    prune_hour = int(cfg.backup["schedule"]["prune_at"][:2])
    near = min((now.hour - prune_hour) % 24, (prune_hour - now.hour) % 24) <= 1
    try:
        last = datetime.fromisoformat(_last_prune_file().read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return near
    return (near and now - last > PRUNE_SPACING) or now - last > PRUNE_CATCH_UP


def run(cfg: config.Config, prune: str = "auto") -> int:
    """One backup run; returns 0 or restic's exit code. `prune`: auto, always or never."""
    settings = cfg.backup
    if not settings["sources"]:
        raise config.ConfigError(f"no backup sources in {cfg.path}: set them in the Scheduler page")
    repository = cfg.repository_label()
    cfg.check_password_source()
    log = Log(log_file(cfg))
    lock = SingleRun(settings["lock_name"])
    unc = None
    try:
        if not lock.acquire():
            log.line("SKIP: another backup run is still going")
            return 0
        log.line("START")
        env = dict(os.environ)
        for key, value in cfg.restic_env.items():
            if value is None:
                env.pop(key, None)
            else:
                env[key] = value
        if settings["share"]["path"]:
            unc, repository = share_paths(cfg, repository)
            connect_share(unc, settings["share"]["user"], settings["share"]["password_file"])
            log.line(f"connected {unc}")
            if not Path(repository, "config").exists():
                raise BackupError(f"the repository is not reachable: {repository}")
        env["RESTIC_REPOSITORY"] = repository
        env.pop("RESTIC_REPOSITORY_FILE", None)

        command = [cfg.restic_binary, "backup", *settings["sources"]]
        if cfg.excludes_file:
            command += ["--exclude-file", str(cfg.excludes_file)]
        if settings["use_fs_snapshot"] and os.name == "nt":
            command.append("--use-fs-snapshot")
        log.line("restic " + " ".join(command[1:]))
        code = subprocess.run(command, env=env, stdin=subprocess.DEVNULL, stdout=log.file,
                              stderr=subprocess.STDOUT, **_NO_WINDOW).returncode
        if code != 0:
            log.line(f"ERROR: backup failed ({code})")
            return code

        now = datetime.now()
        if prune == "always" or (prune == "auto" and prune_due(cfg, now)):
            if not config.has_rules(cfg.retention):
                log.line("no retention policy set: nothing forgotten or pruned")
            else:
                command = [cfg.restic_binary, "forget", *config.forget_args(cfg.retention),
                           *(["--prune"] if cfg.retention.get("prune", True) else [])]
                log.line("restic " + " ".join(command[1:]))
                code = subprocess.run(command, env=env, stdin=subprocess.DEVNULL, stdout=log.file,
                                      stderr=subprocess.STDOUT, **_NO_WINDOW).returncode
                if code != 0:
                    log.line(f"ERROR: forget/prune failed ({code})")
                    return code
                _last_prune_file().write_text(now.isoformat(timespec="seconds"), encoding="utf-8")
        log.line("OK")
        return 0
    except (BackupError, OSError) as e:
        log.line(f"ERROR: {e}")
        return 1
    finally:
        if unc:
            disconnect_share(unc)
        lock.release()
        log.close()
