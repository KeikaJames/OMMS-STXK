#!/usr/bin/env python3
"""Lifecycle regressions: first start, opening time and administrative maintenance."""

from __future__ import annotations

import contextlib
import http.client
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_redis_recovery import (  # noqa: E402
    MAIN_PY,
    REPO_ROOT,
    free_port,
    key,
    redis,
    start_redis,
    stop_process,
    wait_status,
    write_session,
)


def request(port: int, method: str, path: str, *, token: str | None = None,
            body: dict | None = None) -> tuple[int, dict[str, str], bytes]:
    encoded = json.dumps(body or {}).encode("utf-8")
    headers = {"Connection": "close"}
    if token:
        headers["Cookie"] = f"session={token}"
    if method == "POST":
        headers.update({"Content-Type": "application/json", "Content-Length": str(len(encoded))})
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=20.0)
    try:
        conn.request(method, path, body=encoded if method == "POST" else None, headers=headers)
        response = conn.getresponse()
        return response.status, {k.lower(): v for k, v in response.getheaders()}, response.read()
    finally:
        conn.close()


def request_json(port: int, method: str, path: str, **kwargs) -> tuple[int, dict]:
    status, _headers, raw = request(port, method, path, **kwargs)
    return status, json.loads(raw) if raw else {}


class Instance:
    """A private Redis plus main.py on an empty database."""

    def __init__(self, workdir: Path) -> None:
        self.workdir = workdir
        self.db_path = workdir / "fresh.sqlite3"
        self.redis_port = free_port()
        self.http_port = free_port(forbidden={self.redis_port})
        self.redis_process = None
        self.client = None
        self.app = None

    def env(self) -> dict[str, str]:
        env = os.environ.copy()
        env.update({
            "DB_PATH": str(self.db_path), "REDIS_URL": f"redis://127.0.0.1:{self.redis_port}/0",
            "HOST": "127.0.0.1", "PORT": str(self.http_port), "WEB_DIR": str(REPO_ROOT / "web"),
            "LOG_LEVEL": "WARNING", "TZ": "UTC",
        })
        return env

    def start(self) -> None:
        if self.redis_process is None:
            self.redis_process, self.client = start_redis(self.redis_port, self.workdir)
        self.app = subprocess.Popen([sys.executable, str(MAIN_PY)], cwd=REPO_ROOT, env=self.env(),
                                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        if wait_status(self.http_port, "/readyz", 200, 20.0) != 200:
            stop_process(self.app)
            output = self.app.stdout.read().decode("utf-8", "replace") if self.app.stdout else ""
            raise RuntimeError("main.py did not become ready: " + output)

    def stop(self) -> None:
        stop_process(self.app)
        if self.app is not None and self.app.stdout is not None:
            self.app.stdout.close()
        self.app = None

    def close(self) -> None:
        self.stop()
        stop_process(self.redis_process)
        self.redis_process = None

    def sql(self, statement: str, params: tuple = ()) -> list[tuple]:
        with contextlib.closing(sqlite3.connect(self.db_path, timeout=10.0)) as conn:
            rows = conn.execute(statement, params).fetchall()
            conn.commit()
            return rows

    def admin_token(self) -> str:
        self.sql("UPDATE settings SET admin_password='admin-password'")
        status, headers, raw = request(self.http_port, "POST", "/api/admin_login",
                                       body={"username": "admin", "password": "admin-password"})
        if status != 200:
            raise AssertionError((status, raw))
        return headers["set-cookie"].split(";", 1)[0].split("=", 1)[1]

    def seed(self, students: int = 2, capacity: int = 5) -> None:
        self.sql("INSERT INTO clubs (id, name, max_students, current_students) VALUES (1, '3D打印', ?, 0)",
                 (capacity,))
        for number in range(1, students + 1):
            self.sql("INSERT INTO students (id, name, class, student_id, username, password) "
                     "VALUES (?, ?, '高一1班', ?, ?, 'unused')",
                     (number, "学生{}".format(number), "S{:04d}".format(number), "student{}".format(number)))
        self.republish()

    def republish(self) -> None:
        self.client.delete(key("init"))
        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline:
            if self.client.get(key("init")) == "1":
                return
            time.sleep(0.05)
        raise AssertionError("seats were not republished")

    def register(self, student_id: int) -> tuple[int, dict]:
        token = write_session(self.client, student_id)
        return request_json(self.http_port, "POST", "/api/register_club", token=token, body={"club_id": 1})


@unittest.skipIf(redis is None or shutil.which("redis-server") is None, "redis unavailable")
class FreshInstallTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="omms-lifecycle-")
        self.instance = Instance(Path(self.tmp.name))
        self.instance.start()

    def tearDown(self) -> None:
        self.instance.close()
        self.tmp.cleanup()

    def test_first_start_stays_closed_until_an_opening_time_is_set(self) -> None:
        app = self.instance
        self.assertEqual([(None, 0)], app.sql("SELECT registration_start_time, registration_locked FROM settings"))
        status, state = request_json(app.http_port, "GET", "/api/check_registration_time")
        self.assertEqual(200, status)
        self.assertFalse(state["can_register"])
        self.assertIsNone(state["start_time"])
        self.assertIsNone(state["open_at"])

        app.seed()
        self.assertIsNone(app.client.get(key("open_at")))
        status, body = app.register(1)
        self.assertEqual((200, "not_open"), (status, body.get("code")), body)

        admin = app.admin_token()
        status, body = request_json(app.http_port, "POST", "/api/update_registration_time", token=admin,
                                    body={"start_time": "2000-01-01 00:00:00"})
        self.assertEqual(200, status, body)
        status, body = app.register(1)
        self.assertEqual(200, status, body)
        self.assertTrue(body.get("success"), body)

        status, body = request_json(app.http_port, "POST", "/api/update_registration_time", token=admin,
                                    body={"start_time": ""})
        self.assertEqual(200, status, body)
        self.assertEqual([(None,)], app.sql("SELECT registration_start_time FROM settings"))
        self.assertIsNone(app.client.get(key("open_at")))
        status, state = request_json(app.http_port, "GET", "/api/check_registration_time")
        self.assertFalse(state["can_register"])
        self.assertIsNone(state["start_time"])
        status, body = app.register(2)
        self.assertEqual((200, "not_open"), (status, body.get("code")), body)

        app.stop()
        app.client.set(key("open_at"), 0)
        app.start()
        self.assertIsNone(app.client.get(key("open_at")))


if __name__ == "__main__":
    unittest.main()
