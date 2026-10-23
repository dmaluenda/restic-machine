# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 David Maluenda

"""Local web server: the browser interface and its JSON API.

Listening on 127.0.0.1 is not enough on its own, because any web page open in the browser
can send requests to localhost. So, like Jupyter, every launch makes a random token: the
browser gets it once, in the address it is opened with, and keeps it in a SameSite=Strict
cookie; requests without it are refused, and so is any Host header other than our own
address (which defeats DNS rebinding). Requests that change something are POST and must
also carry a custom header, which a page from another origin can't add.
"""

import hmac
import http.server
import io
import json
import secrets
import threading
import time
import traceback
import zipfile
from contextlib import closing
from http import HTTPStatus
from http.cookies import CookieError, SimpleCookie
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from . import __version__, db, query
from .indexer import IndexBusy, Indexer
from .restic import RepositoryUnavailable, ResticError

ATTRIBUTION = "restic-machine by David Maluenda"
LICENSE = "AGPL-3.0-or-later"
PACKAGE_DIR = Path(__file__).parent
_STATIC = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/app.js": ("app.js", "text/javascript; charset=utf-8"),
    "/style.css": ("style.css", "text/css; charset=utf-8"),
}
_CSP = ("default-src 'self'; img-src 'self' data:; object-src 'none'; base-uri 'none';"
        " form-action 'none'; frame-ancestors 'none'")


class App:
    """What the request handlers share: the index, restic and the state of indexing."""

    def __init__(self, index_path: Path, restic, repository_id: str, available: bool = True):
        self.index_path = index_path
        self.restic = restic
        self.repository_id = repository_id
        self.token = secrets.token_urlsafe(32)
        self.indexing = {"running": False, "done": 0, "total": 0, "current": None, "error": None,
                         "unavailable": not available, "finished_at": None}
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None

    def connect(self):
        return db.connect(self.index_path)

    def start_indexing(self) -> bool:
        """Read new snapshots in the background; False if that is already happening."""
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return False
            self.indexing.update(running=True, done=0, total=0, current=None, error=None)
            self._thread = threading.Thread(target=self._index, name="indexer", daemon=True)
            self._thread.start()
            return True

    def wait_for_indexing(self, timeout: float | None = None) -> None:
        if self._thread is not None:
            self._thread.join(timeout)

    def _index(self) -> None:
        try:
            with closing(self.connect()) as con:
                Indexer(con, self.restic, progress=self._progress).run()
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
        except LookupError as e:
            self._send_error(url.path, e.args[0] if e.args else str(e), HTTPStatus.NOT_FOUND)
        except Exception as e:
            traceback.print_exc()
            self._send_error(url.path, f"internal error: {e}", HTTPStatus.INTERNAL_SERVER_ERROR)

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
        if path in _STATIC:
            name, content_type = _STATIC[path]
            self._send((PACKAGE_DIR / "static" / name).read_bytes(), content_type,
                       headers=[("Content-Security-Policy", _CSP)])
        elif path == "/source":
            self._send(_source_zip(), "application/zip", headers=[
                ("Content-Disposition", f'attachment; filename="restic-machine-{__version__}-source.zip"')])
        elif path == "/api/status":
            self._send_json(self._status())
        elif path == "/api/snapshots":
            self._send_json(self._snapshots())
        elif path == "/api/dir":
            self._send_json(self._dir(params))
        elif path == "/api/history":
            self._send_json(self._history(params))
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
        else:
            raise ApiError(HTTPStatus.NOT_FOUND, f"no such action: {path}")

    def _status(self) -> dict:
        with closing(self.app.connect()) as con:
            series = [{"id": row["id"], "hostname": row["hostname"], "paths": json.loads(row["paths"])}
                      for row in con.execute("SELECT * FROM series ORDER BY id")]
        return {"version": __version__, "repository": self.app.repository_id, "series": series,
                "indexing": self.app.indexing, "attribution": ATTRIBUTION, "license": LICENSE,
                "source": "/source"}

    def _snapshots(self) -> list[dict]:
        with closing(self.app.connect()) as con:
            rows = query.snapshots(con)
        return [{"id": row["restic_id"], "series": row["series"], "time": row["time"],
                 "indexed": row["seq"] is not None, "tags": json.loads(row["info"]).get("tags") or [],
                 "entries": row["n_entries"], "added": row["n_added"], "modified": row["n_modified"],
                 "removed": row["n_removed"], "size": row["total_size"], "files": row["total_files"]}
                for row in rows]

    def _dir(self, params: dict) -> dict:
        path = query.normalize(params.get("path", "/"))
        with closing(self.app.connect()) as con:
            snap = query.find_snapshot(con, _required(params, "snapshot"))
            previous = query.previous_snapshot(con, snap)
            entries = query.list_dir(con, snap, path, previous)
            removed = query.removed_entries(con, snap, previous, path) if previous is not None else []
        return {"snapshot": snap["restic_id"], "previous": previous["restic_id"] if previous else None,
                "path": path, "entries": entries, "removed": removed}

    def _history(self, params: dict) -> dict:
        path = query.normalize(_required(params, "path"))
        with closing(self.app.connect()) as con:
            snap = query.find_snapshot(con, _required(params, "snapshot"))
            points = query.history(con, snap, path)
        return {"path": path, "points": points}


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
    return httpd
