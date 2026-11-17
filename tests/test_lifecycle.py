#!/usr/bin/env python3
"""Lifecycle regressions: first start, opening time and administrative maintenance."""

from __future__ import annotations

import contextlib
from datetime import datetime, timedelta, timezone
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



SCHOOL_TZ = timezone(timedelta(hours=8))


def school_time(offset_seconds: float) -> str:
    return (datetime.now(SCHOOL_TZ) + timedelta(seconds=offset_seconds)).strftime("%Y-%m-%d %H:%M:%S")


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

    def test_full_backup_downloads_and_restores_the_database(self) -> None:
        app = self.instance
        app.register(1)

        status, headers, raw = request(app.http_port, "GET", "/api/admin/backup", token=self.admin)
        self.assertEqual(200, status)
        self.assertEqual("application/zip", headers["content-type"])
        self.assertIn("filename*=UTF-8''", headers["content-disposition"])
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            self.assertEqual(["club_system.db"], archive.namelist())
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
        app.client.set(key("sess", "stale-session"), "{}")
        result = app.cli("restore", str(backup_file))
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertIn("已恢复：学生 3 名，社团 1 个，报名 1 条。", result.stdout)
        self.assertEqual((3, 1, 1), counts(app.db_path))
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

    def test_close_time_stops_registration_and_locks_the_list(self) -> None:
        app = self.instance
        times = "/api/update_registration_time"
        status, body = app.post(self.admin, times, {"end_time": school_time(-60)})
        self.assertEqual((400, "截止时间应晚于当前时间"), (status, body.get("message")))
        status, body = app.post(self.admin, times, {"start_time": school_time(3600), "end_time": school_time(1800)})
        self.assertEqual((400, "截止时间应晚于开放时间"), (status, body.get("message")))

        end_time = school_time(3)
        status, body = app.post(self.admin, times, {"start_time": "2000-01-01 00:00:00", "end_time": end_time})
        self.assertEqual(200, status, body)
        status, state = request_json(app.http_port, "GET", "/api/check_registration_time")
        self.assertEqual((True, False, end_time), (state["can_register"], state["registration_locked"], state["end_time"]))
        self.assertIsNotNone(app.client.get(key("close_at")))
        status, body = app.register(1)
        self.assertTrue(body.get("success"), body)
        cancel_token = write_session(app.client, 1)

        deadline = int(app.client.get(key("close_at")))
        time.sleep(max(0.0, deadline - time.time()) + 0.05)
        status, body = app.register(2)
        self.assertEqual((200, "locked"), (status, body.get("code")), body)
        status, body = request_json(app.http_port, "POST", "/api/cancel_registration", token=cancel_token, body={})
        self.assertEqual("locked", body.get("code"), body)
        status, state = request_json(app.http_port, "GET", "/api/check_registration_time")
        self.assertTrue(state["registration_locked"])

        waited = time.monotonic() + 5.0
        while time.monotonic() < waited and app.sql("SELECT registration_locked FROM settings") != [(1,)]:
            time.sleep(0.05)
        self.assertEqual([(1, None)], app.sql("SELECT registration_locked, registration_end_time FROM settings"))
        self.assertEqual([("系统", "到达截止时间")], app.sql(
            "SELECT actor_id, reason FROM audit_events WHERE action='registration.lock_updated'"))
        self.assertEqual("1", app.client.get(key("locked")))
        self.assertIsNone(app.client.get(key("close_at")))
        self.assertTrue(any(name.endswith("-locked.db") for name in os.listdir(app.backups)))
        self.assertEqual((3, 1, 1), counts(app.db_path))

        self.lock(False)
        status, body = app.register(2)
        self.assertTrue(body.get("success"), body)

    def test_editing_a_student_keeps_the_account_and_signs_out_on_class_change(self) -> None:
        app = self.instance
        token = write_session(app.client, 1)
        path = "/api/admin/update_student"
        body = {"student_id": 1, "name": "学生1", "class": "高一1班", "student_no": "S0001", "request_id": "edit-same-1"}
        status, result = app.post(self.admin, path, body)
        self.assertEqual((200, False), (status, result.get("changed")), result)
        status, result = app.post(self.admin, path, dict(body, student_no="S0002", request_id="edit-dup-01"))
        self.assertEqual(409, status, result)
        status, result = app.post(self.admin, path, dict(body, name="学生甲", **{"class": "高二3班"}, request_id="edit-move-1"))
        self.assertEqual((200, True), (status, result.get("changed")), result)
        self.assertEqual([("学生甲", "高二3班", "高二", "S0001", "student1")],
                         app.sql("SELECT name, class, grade, student_id, username FROM students WHERE id=1"))
        status, _body = request_json(app.http_port, "GET", "/api/get_student_info", token=token)
        self.assertEqual(401, status)
        status, result = app.post(self.admin, path, dict(body, name="学生甲", **{"class": "高二3班"}, request_id="edit-move-1"))
        self.assertTrue(result.get("replayed"), result)
        self.assertEqual(1, len(app.sql("SELECT 1 FROM audit_events WHERE action='student.updated'")))

    def test_class_password_reset_returns_new_passwords_once(self) -> None:
        app = self.instance
        token = write_session(app.client, 2)
        path = "/api/admin/reset_class_passwords"
        status, result = app.post(self.admin, path, {"class": "高三9班", "reason": "测试", "request_id": "class-reset-0"})
        self.assertEqual(404, status, result)
        body = {"class": "高一1班", "reason": "账号条遗失", "request_id": "class-reset-1"}
        status, result = app.post(self.admin, path, body)
        self.assertEqual(200, status, result)
        self.assertEqual(3, result["count"])
        credentials = {item["username"]: item["password"] for item in result["credentials"]}
        self.assertEqual({"student1", "student2", "student3"}, set(credentials))
        self.assertTrue(all(len(password) == 8 for password in credentials.values()))
        status, _body = request_json(app.http_port, "GET", "/api/get_student_info", token=token)
        self.assertEqual(401, status)
        status, _headers, raw = request(app.http_port, "POST", "/api/login",
                                        body={"username": "student2", "password": credentials["student2"]})
        self.assertEqual(200, status, raw)
        status, again = app.post(self.admin, path, body)
        self.assertEqual((200, True, []), (status, again.get("replayed"), again.get("credentials")))

    def test_summary_tracks_capacity_exports_and_backups(self) -> None:
        app = self.instance
        status, summary = request_json(app.http_port, "GET", "/api/admin/summary", token=self.admin)
        self.assertEqual(200, status)
        self.assertEqual((3, 0, 1, 5), (summary["students"], summary["registered"], summary["clubs"], summary["capacity"]))
        self.assertEqual((None, None), (summary["last_export"], summary["last_backup"]))
        app.register(1)
        for path in ("/api/export_workbook", "/api/admin/backup"):
            status, _headers, _raw = request(app.http_port, "GET", path, token=self.admin)
            self.assertEqual(200, status, path)
        status, summary = request_json(app.http_port, "GET", "/api/admin/summary", token=self.admin)
        self.assertGreaterEqual(summary["last_export"], summary["last_change"])
        self.assertGreaterEqual(summary["last_backup"], summary["last_change"])
        time.sleep(0.01)
        app.register(2)
        status, summary = request_json(app.http_port, "GET", "/api/admin/summary", token=self.admin)
        self.assertGreater(summary["last_change"], summary["last_export"])
        self.assertEqual([("registrations.exported",), ("backup.downloaded",)], app.sql(
            "SELECT action FROM audit_events WHERE action IN ('registrations.exported', 'backup.downloaded') ORDER BY id"))

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
