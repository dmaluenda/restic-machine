# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 David Maluenda

"""Read-only wrapper around the restic command line.

Every call adds `--no-lock`. Without it each read would write a lock file into the
repository, and a browser left open (or an indexer that died) could make the owner's
`forget --prune` fail. The price is that a read may fail while a prune is running: that
is reported as RepositoryUnavailable, i.e. "try again later". Nothing here ever writes.
"""

import json
import os
import subprocess
import tempfile
from collections.abc import Iterator, Mapping


class ResticError(Exception):
    """restic can't be used as configured (binary missing, wrong password, no repository...)."""


class RepositoryUnavailable(ResticError):
    """The repository can't be read right now: unreachable, or changing under us."""


# restic >= 0.17 exit codes that mean "fix your configuration", not "try later"
_CONFIG_EXIT_CODES = {10: "repository does not exist", 12: "wrong password or no key found"}

# Don't flash a console window when started from a windowless launcher on Windows.
_NO_WINDOW = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}


class Restic:
    def __init__(self, binary: str = "restic", env: Mapping[str, str | None] | None = None):
        self.binary = binary
        self.env = dict(os.environ)
        for key, value in (env or {}).items():
            if value is None:
                self.env.pop(key, None)
            else:
                self.env[key] = value

    def repository_id(self) -> str:
        return self._run_json("cat", "config")["id"]

    def snapshots(self) -> list[dict]:
        return self._run_json("snapshots", "--json") or []

    def ls(self, snapshot_id: str) -> Iterator[dict]:
        """Every node (file, directory, symlink...) of a snapshot."""
        for item in self._iter_json_lines("ls", "--json", snapshot_id):
            if "node" in (item.get("message_type"), item.get("struct_type")):
                yield item

    def _popen_args(self, args) -> dict:
        return dict(args=[self.binary, "--no-lock", *args], env=self.env, stdin=subprocess.DEVNULL,
                    encoding="utf-8", errors="replace", **_NO_WINDOW)

    def _error(self, code: int, stderr: str) -> ResticError:
        lines = [line for line in stderr.strip().splitlines() if line.strip()]
        detail = lines[-1] if lines else f"restic exited with code {code}"
        if code in _CONFIG_EXIT_CODES:
            return ResticError(f"{_CONFIG_EXIT_CODES[code]} ({detail})")
        return RepositoryUnavailable(detail)

    def _run_json(self, *args):
        try:
            proc = subprocess.run(capture_output=True, **self._popen_args(args))
        except FileNotFoundError:
            raise ResticError(f"restic not found: {self.binary}") from None
        if proc.returncode != 0:
            raise self._error(proc.returncode, proc.stderr)
        return json.loads(proc.stdout)

    def _iter_json_lines(self, *args) -> Iterator[dict]:
        # stderr goes to a file: a full stderr pipe could block restic while we read stdout.
        with tempfile.TemporaryFile() as stderr:
            try:
                proc = subprocess.Popen(stdout=subprocess.PIPE, stderr=stderr, **self._popen_args(args))
            except FileNotFoundError:
                raise ResticError(f"restic not found: {self.binary}") from None
            finished = False
            try:
                for line in proc.stdout:
                    if line.strip():
                        yield json.loads(line)
                finished = True
            finally:
                if not finished:
                    proc.kill()
                proc.stdout.close()
                code = proc.wait()
            if code != 0:
                stderr.seek(0)
                raise self._error(code, stderr.read().decode("utf-8", "replace"))
