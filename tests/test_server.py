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

from restic_machine import db, server
from restic_machine.indexer import Indexer
from test_indexer import FakeRestic, snapshot_id


class ServerTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.restic = FakeRestic()
        self.restic.add(1, "2026-01-01T10:00:00Z", {"/data/a": 1, "/data/b": 2})
        self.restic.add(2, "2026-01-02T10:00:00Z", {"/data/a": 5, "/data/c": 3})
        with closing(db.open_index(Path(tmp.name), "f" * 64)) as con:
            Indexer(con, self.restic).run()
        self.app = server.App(db.index_file(Path(tmp.name), "f" * 64), self.restic, "f" * 64)
        self.httpd = server.make_server(self.app)
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.addCleanup(self.httpd.server_close)
        self.addCleanup(self.httpd.shutdown)
        self.cookie = f"restic_machine_{self.port}={self.app.token}"

    def request(self, path, method="GET", host=None, cookie=None, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        all_headers = {"Host": host or f"127.0.0.1:{self.port}", **(headers or {})}
        if cookie:
            all_headers["Cookie"] = cookie
        conn.request(method, path, headers=all_headers)
        response = conn.getresponse()
        body = response.read()
        conn.close()
        return response, body

    def get_json(self, path):
        response, body = self.request(path, cookie=self.cookie)
        self.assertEqual(response.status, 200, body)
        return json.loads(body)

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

    def test_source_code_is_offered(self):
        response, body = self.request("/source", cookie=self.cookie)
        self.assertEqual(response.status, 200)
        names = zipfile.ZipFile(io.BytesIO(body)).namelist()
        self.assertIn("restic_machine/server.py", names)
        self.assertIn("restic_machine/static/app.js", names)


if __name__ == "__main__":
    unittest.main()
