# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 David Maluenda

"""Start restic-machine like a desktop app: no console window.

Used by the double-click launcher (restic-machine.pyw) and the `restic-machine-app` command
that pip installs. With no terminal to print to, the output goes to a log file in the data
directory; launching it again just opens the browser on the instance already running; it
stops by itself a while after its last browser tab is closed (or with the Stop button); and
if it can't start, it says why in a dialog.
"""

import os
import sys
from pathlib import Path

from . import cli, config

IDLE_MINUTES = 10
_MAX_LOG = 1_000_000


def main(argv: list[str] | None = None) -> int:
    log = _log_to_file(config.default_data_dir() / "restic-machine.log")
    argv = list(sys.argv[1:] if argv is None else argv)
    code = cli.main(["serve", "--idle-timeout", str(IDLE_MINUTES), *argv])
    if code != 0:
        _alert("restic-machine could not start.\n\n" + _last_lines(log) + f"\n\nLog: {log}")
    return code


def _log_to_file(log: Path) -> Path:
    log.parent.mkdir(parents=True, exist_ok=True)
    if log.exists() and log.stat().st_size > _MAX_LOG:
        os.replace(log, log.with_suffix(".log.1"))
    stream = open(log, "a", encoding="utf-8", buffering=1)  # stays open for the whole run
    sys.stdout = sys.stderr = stream
    return log


def _last_lines(log: Path, n: int = 6) -> str:
    try:
        lines = log.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return ""
    return "\n".join(lines[-n:])


def _alert(message: str) -> None:
    print(message, file=sys.stderr, flush=True)
    if os.name == "nt":
        import ctypes
        ctypes.windll.user32.MessageBoxW(None, message, "restic-machine", 0x10)  # MB_ICONERROR
