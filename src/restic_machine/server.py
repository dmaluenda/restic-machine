# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 David Maluenda

"""Local web server: the browser interface and its JSON API.

Listening on 127.0.0.1 is not enough on its own, because any web page open in the browser
can send requests to localhost. So, like Jupyter, every launch makes a random token: the
browser gets it once, in the address it is opened with, and keeps it in a SameSite=Strict
cookie; requests without it are refused, and so is any Host header other than our own
address (which defeats DNS rebinding). Requests that change something are POST and must
also carry a custom header, which a page from another origin can't add.

The address of a running server is written to a runtime file next to the index, so a
second launch opens the browser on it instead of starting another one.
"""

import hmac
import http.client
import http.server
import io
import json
import os
import secrets
import select
import socket
import subprocess
import threading
import time
import traceback
import zipfile
from contextlib import closing
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from http import HTTPStatus
from http.cookies import CookieError, SimpleCookie
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from . import __version__, backup, config, db, excludes, files, query, retention, schedule
from .indexer import IndexBusy, Indexer, timestamp_us
from .restic import RepositoryUnavailable, Restic, ResticError

ATTRIBUTION = "restic-machine by David Maluenda"
LICENSE = "AGPL-3.0-or-later"
PACKAGE_DIR = Path(__file__).parent
_STATIC = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/style.css": ("style.css", "text/css; charset=utf-8"),
    **{f"/{name}": (name, "text/javascript; charset=utf-8")
       for name in ("app.js", "backups.js", "common.js", "compare.js", "retention.js", "settings.js")},
}
# Read once, with the code: a server keeps serving the page that goes with it even if the files
# are updated while it runs (a new page against an old server breaks). Restart to get a new version.
_PAGES = {path: ((PACKAGE_DIR / "static" / name).read_bytes(), content_type)
          for path, (name, content_type) in _STATIC.items()}
_CSP = ("default-src 'self'; img-src 'self' data:; object-src 'none'; base-uri 'none';"
        " form-action 'none'; frame-ancestors 'none'")


class NotReady(Exception):
    """There is no usable repository yet; the settings page says why."""


@dataclass
class Repository:
    restic: Restic
    repository_id: str
    index_path: Path


class App:
    """What the request handlers share: the configuration, the repository and its index, and
    the state of indexing. Without a usable repository it still runs, so that the settings
    page can put that right."""

    def __init__(self, cfg: config.Config, idle_timeout: float | None = None, restic_factory=Restic):
        self.cfg = cfg
        self.restic_factory = restic_factory
        self.token = secrets.token_urlsafe(32)
        self.indexing = {"running": False, "done": 0, "total": 0, "current": None, "error": None,
                         "unavailable": False, "finished_at": None}
        # Stop by itself once no page has asked anything for this many seconds (None: never).
        # An open page asks for the status at least every 30 s.
        self.idle_timeout = idle_timeout
        self.last_seen = time.monotonic()
        self.stop = lambda: None  # set by make_server
        self.repo: Repository | None = None
        self.problem: str | None = None  # why there is no repository, for the settings page
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._snapshots_cache = None  # (repo, when, `restic snapshots`)
        self._texts = {}  # (snapshot id, path) -> lines, for comparing: a snapshot never changes
        self.connect_repository()

    def connect_repository(self) -> bool:
        """(Re)open the configured repository and its index. Never raises: what is wrong
        ends up in `problem`."""
        self.repo, self.problem = None, None
        try:
            label = self.cfg.repository_label()
            self.cfg.check_password_source()
            restic = self.restic_factory(self.cfg.restic_binary, self.cfg.restic_env)
            try:
                repository_id = restic.repository_id()
                db.remember_repository(self.cfg.data_dir, label, repository_id)
                available = True
            except RepositoryUnavailable:
                repository_id = db.known_repository(self.cfg.data_dir, label)
                if repository_id is None:
                    raise
                available = False  # its index can still be browsed as last updated
            db.open_index(self.cfg.data_dir, repository_id).close()  # create or upgrade it now
        except (config.ConfigError, ResticError, db.IndexSchemaError, OSError) as e:
            self.problem = str(e)
            return False
        self.repo = Repository(restic, repository_id, db.index_file(self.cfg.data_dir, repository_id))
        self.indexing["unavailable"] = not available
        return True

    def require_repo(self) -> Repository:
        if self.repo is None:
            raise NotReady(self.problem or "no repository configured")
        return self.repo

    def live_snapshots(self, max_age: float = 60.0) -> list[dict]:
        """`restic snapshots`, remembered for a minute: the calendar asks again at every click."""
        repo = self.require_repo()
        with self._lock:
            cached = self._snapshots_cache
            if cached and cached[0] is repo and time.monotonic() - cached[1] < max_age:
                return cached[2]
        snapshots = repo.restic.snapshots()
        with self._lock:
            self._snapshots_cache = (repo, time.monotonic(), snapshots)
        return snapshots

    def connect(self):
        return db.connect(self.require_repo().index_path)

    def snapshot_text(self, snap, path: str, info: dict) -> list[str]:
        """A text file as it was in a snapshot, remembered for the next comparisons."""
        key = (snap["restic_id"], path)
        with self._lock:
            if key in self._texts:
                return self._texts[key]
        lines = files.snapshot_text(self.require_repo().restic, snap, path, info)
        with self._lock:
            self._texts[key] = lines
            while len(self._texts) > 16:
                del self._texts[next(iter(self._texts))]  # the oldest
        return lines

    def idle(self) -> bool:
        return (self.idle_timeout is not None and not self.indexing["running"]
                and time.monotonic() - self.last_seen > self.idle_timeout)

    def start_indexing(self) -> bool:
        """Read new snapshots in the background; False if that is already happening (or
        there is no repository)."""
        with self._lock:
            if self.repo is None or (self._thread is not None and self._thread.is_alive()):
                return False
            self.indexing.update(running=True, done=0, total=0, current=None, error=None)
            self._thread = threading.Thread(target=self._index, args=(self.repo,), name="indexer", daemon=True)
            self._thread.start()
            return True

    def wait_for_indexing(self, timeout: float | None = None) -> None:
        if self._thread is not None:
            self._thread.join(timeout)

    def _index(self, repo: Repository) -> None:
        try:
            with closing(db.connect(repo.index_path)) as con:
                Indexer(con, repo.restic, progress=self._progress).run()
            self.indexing["unavailable"] = False
        except RepositoryUnavailable as e:
            self.indexing.update(unavailable=True, error=f"repository temporarily unavailable: {e}")
        except (IndexBusy, ResticError) as e:
            self.indexing["error"] = str(e)
        except Exception as e:
            self.indexing["error"] = f"indexing failed: {e}"
            traceback.print_exc()
        finally:
            self.indexing.update(running=False, current=None, finished_at=time.time())

    def _progress(self, event: str, **info) -> None:
        if event == "start":
            self.indexing["total"] = info["total"]
        elif event == "reading":
            self.indexing["current"] = info["snapshot"]["time"]
        elif event == "snapshot":
            self.indexing["done"] = info["n"]


class ApiError(Exception):
    def __init__(self, status: HTTPStatus, message: str):
        super().__init__(message)
        self.status = status


class Handler(http.server.BaseHTTPRequestHandler):
    app: App  # set by make_server
    server_version = "restic-machine"

    # -- plumbing ------------------------------------------------------------------------

    def do_GET(self):
        self._dispatch(self._get)

    def do_POST(self):
        self._dispatch(self._post)

    def log_request(self, code="-", size="-"):
        pass  # keep the console quiet; errors are still logged

    def _dispatch(self, method) -> None:
        url = urlsplit(self.path)
        params = {key: values[-1] for key, values in parse_qs(url.query).items()}
        try:
            if self.headers.get("Host") not in self._own_hosts():
                raise ApiError(HTTPStatus.FORBIDDEN, "unexpected Host header")
            method(url.path, params)
        except ApiError as e:
            self._send_error(url.path, str(e), e.status)
        except NotReady as e:
            self._send_error(url.path, f"not ready: {e}", HTTPStatus.CONFLICT)
        except RepositoryUnavailable as e:
            self._send_error(url.path, f"repository temporarily unavailable: {e}", HTTPStatus.SERVICE_UNAVAILABLE)
        except LookupError as e:
            self._send_error(url.path, e.args[0] if e.args else str(e), HTTPStatus.NOT_FOUND)
        except files.FileError as e:
            self._send_error(url.path, str(e), HTTPStatus.CONFLICT)
        except Exception as e:
            if self._client_gone():
                return  # stopped because the browser gave up on it: nobody to answer
            traceback.print_exc()
            self._send_error(url.path, f"internal error: {e}", HTTPStatus.INTERNAL_SERVER_ERROR)

    def _client_gone(self) -> bool:
        """Has the browser hung up? It does when it aborts a request (a newer click on the
        timeline): the connection then reads as closed, while a pending request reads nothing."""
        try:
            readable, _, _ = select.select([self.connection], [], [], 0)
            return bool(readable) and not self.connection.recv(1, socket.MSG_PEEK)
        except OSError:
            return True

    def _connect_for_reading(self):
        """The index, for a read the browser may give up on: then its queries stop."""
        con = self.app.connect()
        con.set_progress_handler(self._client_gone, 100_000)  # a true return interrupts the query
        return con

    def _own_hosts(self) -> set[str]:
        port = self.server.server_address[1]
        return {f"127.0.0.1:{port}", f"localhost:{port}"}

    @property
    def _cookie_name(self) -> str:
        return f"restic_machine_{self.server.server_address[1]}"  # cookies ignore ports

    def _authorized(self) -> bool:
        try:
            morsel = SimpleCookie(self.headers.get("Cookie", "")).get(self._cookie_name)
        except CookieError:
            return False
        return morsel is not None and hmac.compare_digest(morsel.value, self.app.token)

    def _require_auth(self) -> None:
        if not self._authorized():
            raise ApiError(HTTPStatus.FORBIDDEN,
                           "not authorized: open restic-machine with the link that `restic-machine serve` printed")
        self.app.last_seen = time.monotonic()

    def _send(self, body: bytes, content_type: str, status=HTTPStatus.OK, headers=()) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        for name, value in headers:
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(body)

    def _send_json(self, data, status=HTTPStatus.OK) -> None:
        self._send(json.dumps(data).encode(), "application/json", status)

    def _send_error(self, path: str, message: str, status: HTTPStatus) -> None:
        if path.startswith("/api/"):
            self._send_json({"error": message}, status)
        else:  # someone looking at the page in a browser
            self._send(f"restic-machine: {message}\n".encode(), "text/plain; charset=utf-8", status)

    # -- routes ----------------------------------------------------------------------------

    def _get(self, path: str, params: dict) -> None:
        if path == "/" and "token" in params:
            if not hmac.compare_digest(params["token"], self.app.token):
                raise ApiError(HTTPStatus.FORBIDDEN, "wrong token: use the link `restic-machine serve` printed")
            self.send_response(HTTPStatus.SEE_OTHER)
            self.send_header("Location", "/")
            self.send_header("Set-Cookie", f"{self._cookie_name}={self.app.token}; HttpOnly; SameSite=Strict; Path=/")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        self._require_auth()
        if path in _PAGES:
            body, content_type = _PAGES[path]
            self._send(body, content_type, headers=[("Content-Security-Policy", _CSP)])
        elif path == "/source":
            self._send(_source_zip(), "application/zip", headers=[
                ("Content-Disposition", f'attachment; filename="restic-machine-{__version__}-source.zip"')])
        elif path == "/api/status":
            self._send_json(self._status())
        elif path == "/api/snapshots":
            self._send_json(self._snapshots())
        elif path == "/api/dir":
            self._send_json(self._dir(params))
        elif path == "/api/tree":
            self._send_json(self._tree(params))
        elif path == "/api/history":
            self._send_json(self._history(params))
        elif path == "/api/diff":
            self._send_json(self._diff(params))
        elif path == "/api/settings":
            self._send_json(self._settings())
        elif path == "/api/retention":
            self._send_json(self._retention())
        elif path == "/api/excludes":
            self._send_json(self._excludes())
        elif path == "/api/backup":
            self._send_json(self._backup())
        else:
            raise ApiError(HTTPStatus.NOT_FOUND, f"no such page: {path}")

    def _post(self, path: str, params: dict) -> None:
        self._require_auth()
        origin = self.headers.get("Origin")
        if self.headers.get("X-Restic-Machine") != "1" or (
                origin is not None and origin.removeprefix("http://") not in self._own_hosts()):
            raise ApiError(HTTPStatus.FORBIDDEN, "cross-site request refused")
        if path == "/api/index":
            self._send_json({"started": self.app.start_indexing()})
        elif path == "/api/stop":
            self._send_json({"stopping": True})
            self.app.stop()
        elif path == "/api/settings":
            self._send_json(self._save_settings(self._json_body()))
        elif path == "/api/settings/test":
            self._send_json(self._test_settings(self._json_body()))
        elif path == "/api/retention":
            self._send_json(self._save_retention(self._json_body()))
        elif path == "/api/retention/simulate":
            self._send_json(self._simulate(self._json_body()))
        elif path == "/api/retention/calendar":
            self._send_json(self._calendar(self._json_body()))
        elif path == "/api/excludes":
            self._send_json(self._exclude(self._json_body()))
        elif path == "/api/excludes/remove":
            self._send_json(self._include(self._json_body()))
        elif path == "/api/restore":
            self._send_json(self._restore(self._json_body()))
        elif path == "/api/backup/settings":
            self._send_json(self._save_backup(self._json_body()))
        elif path == "/api/backup/install":
            self._send_json(self._install_schedule(self._json_body()))
        elif path == "/api/backup/run":
            self._send_json(schedule.run_now())
        else:
            raise ApiError(HTTPStatus.NOT_FOUND, f"no such action: {path}")

    def _json_body(self) -> dict:
        if self.headers.get_content_type() != "application/json":
            raise ApiError(HTTPStatus.UNSUPPORTED_MEDIA_TYPE, "expected a JSON body")
        length = int(self.headers.get("Content-Length") or 0)
        if length > 65536:
            raise ApiError(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "request too large")
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
        except ValueError:
            raise ApiError(HTTPStatus.BAD_REQUEST, "invalid JSON") from None
        if not isinstance(body, dict):
            raise ApiError(HTTPStatus.BAD_REQUEST, "expected a JSON object")
        return body

    def _status(self) -> dict:
        series = []
        if self.app.repo is not None:
            with closing(self.app.connect()) as con:
                series = [{"id": row["id"], "hostname": row["hostname"], "paths": json.loads(row["paths"])}
                          for row in con.execute("SELECT * FROM series ORDER BY id")]
        return {"version": __version__, "ready": self.app.repo is not None, "problem": self.app.problem,
                "repository": self.app.repo.repository_id if self.app.repo else None,
                "name": self.app.cfg.name, "series": series,
                "indexing": self.app.indexing, "attribution": ATTRIBUTION, "license": LICENSE,
                "source": "/source"}

    # -- settings and retention -----------------------------------------------------------

    def _settings(self) -> dict:
        return {**self.app.cfg.describe(), "ready": self.app.repo is not None, "problem": self.app.problem,
                "repository_id": self.app.repo.repository_id if self.app.repo else None}

    def _test_settings(self, values: dict) -> dict:
        """Try settings without saving them: can restic open the repository with them?"""
        try:
            candidate = self.app.cfg.with_settings(values)
            candidate.repository_label()
            candidate.check_password_source()
            restic = self.app.restic_factory(candidate.restic_binary, candidate.restic_env)
            return {"ok": True, "repository_id": restic.repository_id()}
        except (config.ConfigError, ResticError) as e:
            return {"ok": False, "error": str(e)}

    def _save_settings(self, values: dict) -> dict:
        if self.app.indexing["running"]:
            raise ApiError(HTTPStatus.CONFLICT, "the index is being updated: save again when it is done")
        try:
            candidate = self.app.cfg.with_settings(values)
        except config.ConfigError as e:
            raise ApiError(HTTPStatus.BAD_REQUEST, str(e)) from None
        config.save(candidate)
        self.app.cfg = candidate
        if self.app.connect_repository() and not self.app.indexing["unavailable"]:
            self.app.start_indexing()
        return self._settings()

    def _retention(self) -> dict:
        policy = self.app.cfg.retention
        return {"policy": policy, "file": str(self.app.cfg.path),
                "command": _forget_command(policy) if config.has_rules(policy) else None}

    def _policy(self, body: dict) -> dict:
        try:
            policy = config.normalize_policy(body.get("policy") or {})
        except config.ConfigError as e:
            raise ApiError(HTTPStatus.BAD_REQUEST, str(e)) from None
        if not config.has_rules(policy):
            raise ApiError(HTTPStatus.BAD_REQUEST, "a policy needs at least one rule")
        return policy

    def _save_retention(self, body: dict) -> dict:
        self.app.cfg.retention = self._policy(body)
        config.save(self.app.cfg)
        return self._retention()

    def _simulate(self, body: dict) -> dict:
        """What the policy would keep and remove now: `restic forget --dry-run`, which only
        reads. Every snapshot with the rules that keep it, oldest first."""
        policy = self._policy(body)
        groups = self.app.require_repo().restic.forget_preview(config.forget_args(policy))
        snapshots = []
        for group in groups:
            reasons = {reason["snapshot"]["id"]: reason["matches"] for reason in group.get("reasons") or []}
            for keep, listed in ((True, group.get("keep")), (False, group.get("remove"))):
                for snap in listed or []:
                    snapshots.append({"id": snap["id"], "time": snap["time"], "hostname": snap.get("hostname"),
                                      "tags": snap.get("tags") or [], "keep": keep,
                                      "reasons": reasons.get(snap["id"], []) if keep else []})
        snapshots.sort(key=lambda snap: timestamp_us(snap["time"]))
        kept = sum(snap["keep"] for snap in snapshots)
        return {"snapshots": snapshots, "kept": kept, "removed": len(snapshots) - kept,
                "command": _forget_command(policy)}

    def _calendar(self, body: dict) -> dict:
        """What the policy would leave on a given day: today's snapshots that exist by then, plus
        the backups still to come (assumed at the usual times of the last days), judged with
        restic's rules as copied in retention.py. Each host+paths group on its own, as restic."""
        policy = self._policy(body)
        try:
            day = date.fromisoformat(str(body.get("date") or date.today().isoformat()))
        except ValueError:
            raise ApiError(HTTPStatus.BAD_REQUEST, "date must be YYYY-MM-DD") from None
        if day > date.today() + timedelta(days=3 * 366):
            raise ApiError(HTTPStatus.BAD_REQUEST, "pick a day within the next three years")
        groups: dict[tuple, list[retention.Snapshot]] = {}
        hosts = {}
        for info in self.app.live_snapshots():
            key = (info.get("hostname") or "", tuple(sorted(info.get("paths") or [])))
            snap = retention.Snapshot(datetime.fromisoformat(info["time"]), info["id"], info.get("tags") or [])
            groups.setdefault(key, []).append(snap)
            hosts[info["id"]] = key[0]
        snapshots, schedule = [], set()
        for real in groups.values():
            existing = [snap for snap in real if snap.time.date() <= day]
            future = retention.projected(real, day) if day > date.today() else []  # today means now
            schedule |= {f"{snap.time:%H:%M}" for snap in future}
            for snap, keep, reasons in retention.apply(existing + future, policy):
                snapshots.append({"id": snap.id, "time": snap.time.isoformat(), "projected": snap.id is None,
                                  "hostname": hosts.get(snap.id), "tags": snap.tags, "keep": keep,
                                  "reasons": reasons})
        snapshots.sort(key=lambda snap: timestamp_us(snap["time"]))
        real_kept = sum(s["keep"] for s in snapshots if not s["projected"])
        return {"date": day.isoformat(), "today": date.today().isoformat(), "snapshots": snapshots,
                "real_kept": real_kept, "real_removed": sum(not s["projected"] for s in snapshots) - real_kept,
                "future_kept": sum(s["keep"] for s in snapshots if s["projected"]),
                "schedule": sorted(schedule),
                "grouping_differs": policy.get("group_by", "host,paths") != "host,paths"}

    def _snapshots(self) -> list[dict]:
        with closing(self.app.connect()) as con:
            rows = query.snapshots(con)
        return [{"id": row["restic_id"], "series": row["series"], "time": row["time"],
                 "indexed": row["seq"] is not None, "tags": json.loads(row["info"]).get("tags") or [],
                 "entries": row["n_entries"], "added": row["n_added"], "modified": row["n_modified"],
                 "removed": row["n_removed"], "size": row["total_size"], "files": row["total_files"]}
                for row in rows]

    def _dir(self, params: dict) -> dict:
        """One directory (the tree's first level); see _tree."""
        tree = self._tree({**params, "depth": "1"})
        listing = tree["dirs"][tree["path"]]
        return {key: tree[key] for key in ("snapshot", "previous", "path", "excludes")} | listing

    def _tree(self, params: dict) -> dict:
        """A directory and the ones below it, `depth` levels down (breadth first, up to `limit`
        entries: a directory that alone would go past it stays closed; when comparing, only what
        differs is listed), as the Browse tree expands them. Changes are against `compare` (any snapshot of the same series) or else the
        previous one; with `changed_only`, only the directories with changes below them are
        opened, and with `with_removed` the removed ones too (which are then shown). A removed
        directory lists what it had, all of it removed."""
        path = query.normalize(params.get("path", "/"))
        try:
            depth = max(1, int(params.get("depth", 1)))
            limit = max(1, min(int(params.get("limit", 3000)), 20000))
        except ValueError:
            raise ApiError(HTTPStatus.BAD_REQUEST, "depth and limit must be numbers") from None
        changed_only = params.get("changed_only") in ("1", "true")
        with_removed = params.get("with_removed") in ("1", "true")
        comparing = bool(params.get("compare"))
        patterns = excludes.read(self.app.cfg.excludes_file) if self.app.cfg.excludes_file else None
        dirs, queue, rows, skipped = {}, [(path, 1)], 0, False
        with closing(self._connect_for_reading()) as con:
            snap = query.find_snapshot(con, _required(params, "snapshot"))
            if comparing:
                previous = query.find_snapshot(con, params["compare"])
                if previous["series"] != snap["series"]:
                    raise ApiError(HTTPStatus.BAD_REQUEST, "only snapshots of the same host and paths can be compared")
            else:
                previous = query.previous_snapshot(con, snap)
            while queue and rows < limit:
                current, level = queue.pop(0)
                try:
                    entries = query.list_dir(con, snap, current, previous)
                except LookupError:
                    if previous is None:
                        raise
                    entries = []  # gone since `previous` (else the next line says it isn't there)
                if comparing:
                    entries = [e for e in entries if e["status"]]  # only what differs is shown
                removed = query.removed_entries(con, snap, previous, current) if previous is not None else []
                shown = len(entries) + (len(removed) if with_removed or comparing else 0)
                if dirs and rows + shown > limit:
                    skipped = True  # left closed: it alone would go past the limit
                    continue
                for entry in entries:
                    entry["native"] = excludes.native_path(f"{current.rstrip('/')}/{entry['name']}")
                    entry["excluded"] = excludes.excluded_by(entry["native"], patterns) if patterns else None
                dirs[current] = {"entries": entries, "removed": removed}
                rows += shown
                if level < depth:
                    queue += [(f"{current.rstrip('/')}/{e['name']}", level + 1)
                              for e in entries + (removed if with_removed else [])
                              if e["type"] == "dir" and (e["status"] or not changed_only)]
        return {"snapshot": snap["restic_id"], "previous": previous["restic_id"] if previous else None,
                "path": path, "dirs": dirs, "truncated": bool(queue) or skipped, "excludes": patterns is not None}

    # -- the backup's exclude file ----------------------------------------------------------

    # -- backups and their schedule ----------------------------------------------------------

    def _backup(self) -> dict:
        settings = self.app.cfg.backup
        try:
            tasks, tasks_error = schedule.state(), None
        except (schedule.ScheduleError, OSError, subprocess.SubprocessError) as e:
            tasks, tasks_error = {"supported": schedule.supported(), "ours": None, "others": []}, str(e)
        log = backup.log_file(self.app.cfg)
        try:
            tail = log.read_text(encoding="utf-8", errors="replace").splitlines()[-60:]
        except OSError:
            tail = []
        return {"settings": settings, "excludes_file": str(self.app.cfg.excludes_file or ""),
                "summary": schedule.describe(settings["schedule"]), "run_times": schedule.run_times(settings["schedule"]),
                "tasks": tasks, "tasks_error": tasks_error, "task_name": schedule.TASK_NAME,
                "log": tail, "log_file": str(log)}

    def _save_backup(self, body: dict) -> dict:
        raw = body.get("settings") or {}
        current = self.app.cfg.backup
        try:
            settings = config.normalize_backup({**raw, "lock_name": current["lock_name"]})
            schedule.check(settings["schedule"])
        except (config.ConfigError, schedule.ScheduleError) as e:
            raise ApiError(HTTPStatus.BAD_REQUEST, str(e)) from None
        self.app.cfg.backup = settings
        config.save(self.app.cfg)
        return self._backup()

    def _install_schedule(self, body: dict) -> dict:
        """Register the task after the UAC prompt; this waits for the user to answer it."""
        disable = [str(name) for name in body.get("disable") or []]
        try:
            result = schedule.install(self.app.cfg, disable)
        except schedule.ScheduleError as e:
            raise ApiError(HTTPStatus.BAD_REQUEST, str(e)) from None
        return {**result, **self._backup()}

    def _excludes_file(self) -> Path:
        if not self.app.cfg.excludes_file:
            raise ApiError(HTTPStatus.CONFLICT, "set the backup's exclude file first (click restic-machine, top left)")
        return self.app.cfg.excludes_file

    def _excludes(self) -> dict:
        file = self.app.cfg.excludes_file
        return {"file": str(file) if file else None, "exists": bool(file and file.exists()),
                "patterns": excludes.read(file) if file else []}

    def _exclude(self, body: dict) -> dict:
        path = excludes.native_path(query.normalize(_required(body, "path")))
        excludes.add(self._excludes_file(), path)
        return {"pattern": path, **self._excludes()}

    def _include(self, body: dict) -> dict:
        removed = excludes.remove(self._excludes_file(), _required(body, "pattern"))
        return {"removed": removed, **self._excludes()}

    def _history(self, params: dict) -> dict:
        path = query.normalize(_required(params, "path"))
        with closing(self._connect_for_reading()) as con:
            snap = query.find_snapshot(con, _required(params, "snapshot"))
            points = query.history(con, snap, path)
        return {"path": path, "points": points}

    # -- a file: restore it, compare its versions ---------------------------------------------

    def _restore(self, body: dict) -> dict:
        """The file as it was in the snapshot, next to the original (files.restored_path)."""
        path = query.normalize(_required(body, "path"))
        with closing(self.app.connect()) as con:
            snap = query.find_snapshot(con, _required(body, "snapshot"))
            info = query.entry(con, snap, path)
        return {"target": str(files.restore(self.app.require_repo().restic, snap, path, info))}

    def _diff(self, params: dict) -> dict:
        """Two versions of a text file side by side: `left` and `right` are each a snapshot id
        or `disk` (the file as it is now)."""
        path = query.normalize(_required(params, "path"))
        native = excludes.native_path(path)
        sides = {}
        for side in ("left", "right"):
            source = _required(params, side)
            if source == "disk":
                sides[side] = files.disk_text(native)
                continue
            with closing(self._connect_for_reading()) as con:
                snap = query.find_snapshot(con, source)
                try:
                    info = query.entry(con, snap, path)
                except LookupError:
                    sides[side] = None  # not in that snapshot
                    continue
            sides[side] = self.app.snapshot_text(snap, path, info)
        rows, changes = files.side_by_side(sides["left"] or [], sides["right"] or [])
        return {"path": path, "native": native, "rows": rows, "changes": changes,
                "missing": {side: lines is None for side, lines in sides.items()}}


def _forget_command(policy: dict) -> str:
    words = ["restic", "forget", *config.forget_args(policy), *(["--prune"] if policy.get("prune") else [])]
    return " ".join(f'"{word}"' if " " in word else word for word in words)


def _required(params: dict, name: str) -> str:
    if not params.get(name):
        raise ApiError(HTTPStatus.BAD_REQUEST, f"missing parameter: {name}")
    return params[name]


def _source_zip() -> bytes:
    """The code that is running, as the AGPL asks to offer to users over a network."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for file in sorted(PACKAGE_DIR.rglob("*")):
            if file.is_file() and "__pycache__" not in file.parts:
                archive.write(file, Path("restic_machine") / file.relative_to(PACKAGE_DIR))
        if PACKAGE_DIR.parent.name == "src":  # running from a source checkout
            root = PACKAGE_DIR.parent.parent
            for name in ("LICENSE", "NOTICE", "README.md", "pyproject.toml"):
                if (root / name).is_file():
                    archive.write(root / name, name)
    return buffer.getvalue()


def make_server(app: App, port: int = 0) -> http.server.ThreadingHTTPServer:
    handler = type("BoundHandler", (Handler,), {"app": app})
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", port), handler)
    httpd.daemon_threads = True
    # shutdown() waits for serve_forever() to return, so it can't run on a request's thread.
    app.stop = lambda: threading.Thread(target=httpd.shutdown, daemon=True).start()
    if app.idle_timeout is not None:
        threading.Thread(target=_stop_when_idle, args=(app,), name="idle", daemon=True).start()
    return httpd


def _stop_when_idle(app: App) -> None:
    while True:
        time.sleep(min(30.0, app.idle_timeout / 4))
        if app.idle():
            print(f"no page open for {app.idle_timeout / 60:g} min: stopping", flush=True)
            app.stop()
            return


def url_of(port: int, token: str) -> str:
    return f"http://127.0.0.1:{port}/?token={token}"


def write_runtime(path: Path, port: int, token: str) -> None:
    """Record where this server listens; private to the user (the token is the key)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump({"port": port, "token": token, "pid": os.getpid()}, f)
    os.replace(tmp, path)


def remove_runtime(path: Path, token: str) -> None:
    """Remove the runtime file if it is still ours."""
    try:
        if json.loads(path.read_text(encoding="utf-8")).get("token") == token:
            path.unlink()
    except (OSError, ValueError):
        pass


def running_instance(path: Path) -> str | None:
    """The address of the server a runtime file points to, if it is still answering."""
    try:
        info = json.loads(path.read_text(encoding="utf-8"))
        port, token = int(info["port"]), str(info["token"])
    except (OSError, ValueError, KeyError, TypeError):
        return None
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
    try:
        conn.request("GET", "/api/status", headers={
            "Host": f"127.0.0.1:{port}", "Cookie": f"restic_machine_{port}={token}"})
        alive = conn.getresponse().status == HTTPStatus.OK
    except OSError:
        alive = False
    finally:
        conn.close()
    return url_of(port, token) if alive else None
