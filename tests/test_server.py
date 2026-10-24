# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 David Maluenda

"""The local web server: who gets in, and what the API answers."""

import http.client
import io
import json
import tempfile
import threading
import unittest
import zipfile
from contextlib import closing
from pathlib import Path

from restic_machine import config, db, server
from restic_machine.indexer import Indexer
from test_indexer import FakeRestic, snapshot_id

NO_REPOSITORY = {"RESTIC_REPOSITORY": None, "RESTIC_REPOSITORY_FILE": None,
                 "RESTIC_PASSWORD": None, "RESTIC_PASSWORD_FILE": None, "RESTIC_PASSWORD_COMMAND": None}


def make_config(data_dir: Path, **env) -> config.Config:
    """A config that never sees the user's own restic environment variables."""
    return config.Config(data_dir=data_dir, source=data_dir / "config.toml", restic_env={**NO_REPOSITORY, **env})


class Served:
    """Start an App's server on a free port for a test, and talk to it."""

    def start(self, app):
        self.app = app
        self.httpd = server.make_server(app)
        self.port = self.httpd.server_address[1]
        self.serving = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.serving.start()
        self.addCleanup(self.httpd.server_close)
        self.addCleanup(self.httpd.shutdown)
        self.cookie = f"restic_machine_{self.port}={app.token}"

    def request(self, path, method="GET", host=None, cookie=None, headers=None, body=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        all_headers = {"Host": host or f"127.0.0.1:{self.port}", **(headers or {})}
        if cookie:
            all_headers["Cookie"] = cookie
        conn.request(method, path, body=body, headers=all_headers)
        response = conn.getresponse()
        data = response.read()
        conn.close()
        return response, data

    def get_json(self, path):
        response, body = self.request(path, cookie=self.cookie)
        self.assertEqual(response.status, 200, body)
        return json.loads(body)

    def post_json(self, path, data, status=200):
        response, body = self.request(path, "POST", cookie=self.cookie, body=json.dumps(data), headers={
            "X-Restic-Machine": "1", "Content-Type": "application/json"})
        self.assertEqual(response.status, status, body)
        return json.loads(body)


class ServerTest(Served, unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.data = Path(tmp.name)
        self.restic = FakeRestic()
        self.restic.add(1, "2026-01-01T10:00:00Z", {"/data/a": 1, "/data/b": 2})
        self.restic.add(2, "2026-01-02T10:00:00Z", {"/data/a": 5, "/data/c": 3})
        with closing(db.open_index(self.data, "f" * 64)) as con:
            Indexer(con, self.restic).run()
        cfg = make_config(self.data, RESTIC_REPOSITORY="fake:repo", RESTIC_PASSWORD="test-only")
        self.start(server.App(cfg, restic_factory=lambda binary, env: self.restic))

    def test_nothing_without_the_token(self):
        for path in ("/", "/app.js", "/api/status", "/source"):
            response, _ = self.request(path)
            self.assertEqual(response.status, 403, path)
        response, _ = self.request("/api/status", cookie=f"restic_machine_{self.port}=wrong")
        self.assertEqual(response.status, 403)
        response, _ = self.request("/?token=wrong")
        self.assertEqual(response.status, 403)

    def test_token_link_sets_the_cookie(self):
        response, _ = self.request(f"/?token={self.app.token}")
        self.assertEqual(response.status, 303)
        cookie = response.getheader("Set-Cookie")
        self.assertIn(f"restic_machine_{self.port}={self.app.token}", cookie)
        self.assertIn("SameSite=Strict", cookie)
        self.assertIn("HttpOnly", cookie)
        response, body = self.request("/", cookie=self.cookie)
        self.assertEqual(response.status, 200)
        self.assertIn(b"restic-machine", body)
        self.assertIn("frame-ancestors 'none'", response.getheader("Content-Security-Policy"))

    def test_foreign_host_is_refused_even_with_the_cookie(self):
        response, _ = self.request("/api/status", host=f"evil.example:{self.port}", cookie=self.cookie)
        self.assertEqual(response.status, 403)

    def test_post_needs_the_custom_header_and_our_origin(self):
        response, _ = self.request("/api/index", "POST", cookie=self.cookie)
        self.assertEqual(response.status, 403)
        response, _ = self.request("/api/index", "POST", cookie=self.cookie,
                                   headers={"X-Restic-Machine": "1", "Origin": "http://evil.example"})
        self.assertEqual(response.status, 403)
        self.restic.add(3, "2026-01-03T10:00:00Z", {"/data/a": 5})
        response, body = self.request("/api/index", "POST", cookie=self.cookie, headers={
            "X-Restic-Machine": "1", "Origin": f"http://127.0.0.1:{self.port}"})
        self.assertEqual((response.status, json.loads(body)), (200, {"started": True}))
        self.app.wait_for_indexing(10)
        self.assertEqual(len(self.get_json("/api/snapshots")), 3)
        self.assertFalse(self.get_json("/api/status")["indexing"]["running"])

    def test_api(self):
        status = self.get_json("/api/status")
        self.assertEqual(status["attribution"], "restic-machine by David Maluenda")
        snapshots = self.get_json("/api/snapshots")
        self.assertEqual([(s["id"], s["size"], s["files"]) for s in snapshots],
                         [(snapshot_id(1), 3, 2), (snapshot_id(2), 8, 2)])
        listing = self.get_json(f"/api/dir?snapshot={snapshot_id(2)}&path=/data")
        self.assertEqual(listing["previous"], snapshot_id(1))
        self.assertEqual([(e["name"], e["status"]) for e in listing["entries"]], [("a", "modified"), ("c", "added")])
        self.assertEqual([e["name"] for e in listing["removed"]], ["b"])
        history = self.get_json(f"/api/history?snapshot={snapshot_id(2)}&path=/data/b")
        self.assertEqual([p["present"] for p in history["points"]], [True, False])
        response, body = self.request(f"/api/dir?snapshot={snapshot_id(2)}&path=/nowhere", cookie=self.cookie)
        self.assertEqual(response.status, 404)
        self.assertIn("not in the index", json.loads(body)["error"])

    def test_stop_button(self):
        response, _ = self.request("/api/stop", "POST", cookie=self.cookie, headers={"X-Restic-Machine": "1"})
        self.assertEqual(response.status, 200)
        self.serving.join(10)
        self.assertFalse(self.serving.is_alive())

    def test_runtime_file_finds_the_running_server(self):
        runtime = self.data / "server.json"
        server.write_runtime(runtime, self.port, self.app.token)
        self.assertEqual(server.running_instance(runtime), server.url_of(self.port, self.app.token))
        server.remove_runtime(runtime, "someone else's token")
        self.assertTrue(runtime.exists())
        self.httpd.shutdown()
        self.httpd.server_close()
        self.assertIsNone(server.running_instance(runtime))
        server.remove_runtime(runtime, self.app.token)
        self.assertFalse(runtime.exists())

    def test_retention_policy_is_simulated_and_saved(self):
        self.assertEqual(self.get_json("/api/retention"), {
            "policy": {}, "file": str(self.data / "config.toml"), "command": None})
        result = self.post_json("/api/retention/simulate", {"policy": {"keep_last": 1}})
        self.assertEqual((result["kept"], result["removed"]), (1, 1))
        self.assertEqual([(s["id"], s["keep"], s["reasons"]) for s in result["snapshots"]],
                         [(snapshot_id(1), False, []), (snapshot_id(2), True, ["last snapshot"])])
        self.post_json("/api/retention/simulate", {"policy": {}}, status=400)
        self.post_json("/api/retention/simulate", {"policy": {"keep_daily": "seven"}}, status=400)
        saved = self.post_json("/api/retention", {"policy": {"keep_last": 3, "keep_tag": ["keep"]}})
        self.assertEqual(saved["command"], "restic forget --keep-last 3 --keep-tag keep --prune")
        self.assertEqual(config.load(self.data / "config.toml").retention,
                         {"keep_last": 3, "keep_tag": ["keep"], "prune": True})

    def test_actions_want_json(self):
        response, _ = self.request("/api/retention", "POST", cookie=self.cookie, body="keep_last=1",
                                   headers={"X-Restic-Machine": "1", "Content-Type": "text/plain"})
        self.assertEqual(response.status, 415)

    def test_source_code_is_offered(self):
        response, body = self.request("/source", cookie=self.cookie)
        self.assertEqual(response.status, 200)
        names = zipfile.ZipFile(io.BytesIO(body)).namelist()
        self.assertIn("restic_machine/server.py", names)
        self.assertIn("restic_machine/static/app.js", names)


class SetupTest(Served, unittest.TestCase):
    """With no repository configured, the server still runs, for the settings page."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.data = Path(tmp.name)
        self.restic = FakeRestic()
        self.restic.add(1, "2026-01-01T10:00:00Z", {"/data/a": 1})
        self.start(server.App(make_config(self.data), restic_factory=lambda binary, env: self.restic))

    def test_settings_make_it_ready(self):
        status = self.get_json("/api/status")
        self.assertFalse(status["ready"])
        self.assertIn("no repository configured", status["problem"])
        response, _ = self.request("/api/snapshots", cookie=self.cookie)
        self.assertEqual(response.status, 409)
        self.assertFalse(self.post_json("/api/settings/test", {"repository": "fake:repo"})["ok"])  # no password
        values = {"repository": "fake:repo", "password_file": str(self.data / "pw.txt")}
        self.assertEqual(self.post_json("/api/settings/test", values)["repository_id"], "f" * 64)
        self.assertFalse(self.get_json("/api/status")["ready"])  # a test saves nothing
        settings = self.post_json("/api/settings", values)
        self.assertTrue(settings["ready"])
        self.assertEqual(settings["repository"], "fake:repo")
        self.assertEqual(config.load(self.data / "config.toml").restic_env["RESTIC_REPOSITORY"], "fake:repo")
        self.app.wait_for_indexing(10)
        self.assertEqual(len(self.get_json("/api/snapshots")), 1)


class IdleTest(unittest.TestCase):
    def test_stops_by_itself_when_no_page_asks_for_a_while(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = server.App(make_config(Path(tmp)), idle_timeout=0.2)
            httpd = server.make_server(app)
            serving = threading.Thread(target=httpd.serve_forever, daemon=True)
            serving.start()
            serving.join(10)
            httpd.server_close()
            self.assertFalse(serving.is_alive())

    def test_not_while_indexing(self):
        app = server.App(make_config(Path("unused")), idle_timeout=60)
        app.last_seen -= 120
        app.indexing["running"] = True
        self.assertFalse(app.idle())
        app.indexing["running"] = False
        self.assertTrue(app.idle())


if __name__ == "__main__":
    unittest.main()
