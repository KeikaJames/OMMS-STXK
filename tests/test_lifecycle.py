#!/usr/bin/env python3
"""Lifecycle regressions: first start, opening time and administrative maintenance."""

from __future__ import annotations

import contextlib
import http.client
import io
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
import zipfile

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

    def post(self, token: str, path: str, body: dict) -> tuple[int, dict]:
        return request_json(self.http_port, "POST", path, token=token, body=body)

    def cli(self, *arguments: str) -> subprocess.CompletedProcess:
        return subprocess.run([sys.executable, str(MAIN_PY), *arguments], cwd=REPO_ROOT, env=self.env(),
                              capture_output=True, text=True, timeout=60)

    @property
    def backups(self) -> Path:
        return self.workdir / "backups"


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



def counts(path: Path) -> tuple[int, int, int]:
    with contextlib.closing(sqlite3.connect(path)) as conn:
        return tuple(conn.execute("SELECT COUNT(*) FROM " + table).fetchone()[0]
                     for table in ("students", "clubs", "registrations"))


@unittest.skipIf(redis is None or shutil.which("redis-server") is None, "redis unavailable")
class MaintenanceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="omms-maintenance-")
        self.instance = Instance(Path(self.tmp.name))
        self.instance.start()
        self.instance.sql("UPDATE settings SET registration_start_time='2000-01-01 00:00:00'")
        self.instance.seed(students=3, capacity=5)
        self.admin = self.instance.admin_token()

    def tearDown(self) -> None:
        self.instance.close()
        self.tmp.cleanup()

    def lock(self, locked: bool = True) -> dict:
        status, body = self.instance.post(self.admin, "/api/update_registration_lock",
                                          {"locked": locked, "reason": "测试", "request_id": os.urandom(8).hex()})
        self.assertEqual(200, status, body)
        return body

    def test_clearing_registrations_needs_the_lock_backs_up_and_republishes_seats(self) -> None:
        app = self.instance
        for student in (1, 2):
            status, body = app.register(student)
            self.assertTrue(body.get("success"), body)
        self.assertEqual("3", app.client.get(key("stock", 1)))
        status, body = app.post(self.admin, "/api/admin/clear_registrations", {"request_id": "clear-request-1"})
        self.assertEqual((409, "请先锁定名单"), (status, body.get("message")))

        locked = self.lock()
        self.assertRegex(locked["backup"], r"^\d{8}-\d{6}-locked\.db$")
        self.assertEqual(2, counts(app.backups / locked["backup"])[2])

        status, body = app.post(self.admin, "/api/admin/clear_registrations", {"request_id": "clear-request-1"})
        self.assertEqual(200, status, body)
        self.assertEqual(2, body["cleared"])
        self.assertEqual((3, 1, 2), counts(app.backups / body["backup"]))
        self.assertEqual((3, 1, 0), counts(app.db_path))
        self.assertEqual([(0,)], app.sql("SELECT current_students FROM clubs"))
        self.assertEqual("5", app.client.get(key("stock", 1)))
        self.assertIsNone(app.client.get(key("reg", 1)))

        status, again = app.post(self.admin, "/api/admin/clear_registrations", {"request_id": "clear-request-1"})
        self.assertEqual(200, status, again)
        self.assertTrue(again["replayed"])
        self.assertEqual(body["backup"], again["backup"])
        self.assertEqual([(1,)], app.sql("SELECT COUNT(*) FROM audit_events WHERE action='registrations.cleared'"))

        self.lock(False)
        status, body = app.register(3)
        self.assertTrue(body.get("success"), body)

    def test_full_backup_downloads_and_restores_database_and_photos(self) -> None:
        app = self.instance
        app.register(1)
        image_dir = app.workdir / "club-images"
        image_dir.mkdir(exist_ok=True)
        photo = "1-" + "a" * 32 + ".jpg"
        (image_dir / photo).write_bytes(b"photo-bytes")
        app.sql("UPDATE clubs SET image_path=?", ("/club-images/" + photo,))

        status, headers, raw = request(app.http_port, "GET", "/api/admin/backup", token=self.admin)
        self.assertEqual(200, status)
        self.assertEqual("application/zip", headers["content-type"])
        self.assertIn("filename*=UTF-8''", headers["content-disposition"])
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            self.assertEqual(["club_system.db", "club-images/" + photo], archive.namelist())
        backup_file = app.workdir / "full.zip"
        backup_file.write_bytes(raw)

        locked = self.lock()
        status, listing = request_json(app.http_port, "GET", "/api/admin/backups", token=self.admin)
        self.assertEqual(200, status)
        self.assertEqual([(locked["backup"], "名单锁定")], [(item["name"], item["label"]) for item in listing["backups"]])
        status, _headers, snapshot = request(app.http_port, "GET", "/api/admin/backup?name=" + locked["backup"],
                                             token=self.admin)
        self.assertEqual((200, (app.backups / locked["backup"]).read_bytes()), (status, snapshot))
        for name in ("../club_system.db", "x.db"):
            status, _body = request_json(app.http_port, "GET", "/api/admin/backup?name=" + name, token=self.admin)
            self.assertEqual(400, status)

        app.stop()
        app.sql("DELETE FROM registrations")
        app.sql("DELETE FROM students")
        (image_dir / photo).unlink()
        app.client.set(key("sess", "stale-session"), "{}")
        result = app.cli("restore", str(backup_file))
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertIn("已恢复：学生 3 名，社团 1 个，报名 1 条，照片 1 张。", result.stdout)
        self.assertEqual((3, 1, 1), counts(app.db_path))
        self.assertEqual(b"photo-bytes", (image_dir / photo).read_bytes())
        self.assertIsNone(app.client.get(key("sess", "stale-session")))
        before = [name for name in os.listdir(app.backups) if name.endswith("-before-restore.db")]
        self.assertEqual(1, len(before))
        self.assertEqual((0, 1, 0), counts(app.backups / before[0]))
        app.start()
        self.assertEqual("4", app.client.get(key("stock", 1)))

        broken = app.workdir / "broken.db"
        broken.write_bytes(b"not a database")
        result = app.cli("restore", str(broken))
        self.assertEqual(1, result.returncode)
        self.assertIn("备份文件不可用", result.stdout)

    def test_reset_admin_password_from_the_command_line(self) -> None:
        app = self.instance
        result = app.cli("reset-admin-password")
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        password = re.search(r"新密码 (\S+)", result.stdout).group(1)
        status, _body = request_json(app.http_port, "GET", "/api/admin/summary", token=self.admin)
        self.assertEqual(401, status)
        status, _headers, _raw = request(app.http_port, "POST", "/api/admin_login",
                                         body={"username": "admin", "password": "admin-password"})
        self.assertEqual(401, status)
        status, _headers, _raw = request(app.http_port, "POST", "/api/admin_login",
                                         body={"username": "admin", "password": password})
        self.assertEqual(200, status)
        self.assertEqual([("命令行",)], app.sql("SELECT actor_id FROM audit_events WHERE action='admin.password_reset'"))


if __name__ == "__main__":
    unittest.main()
