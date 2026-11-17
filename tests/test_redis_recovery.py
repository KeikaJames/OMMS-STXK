#!/usr/bin/env python3
"""Redis 恢复与名额回滚语义的隔离回归测试。

每个用例使用临时 SQLite 和私有 redis-server（随机回环端口）。
"""

from __future__ import annotations

import contextlib
import http.client
import importlib.util
import json
import os
from pathlib import Path
import secrets
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest

try:
    import redis
except ImportError:  # pragma: no cover
    redis = None


REPO_ROOT = Path(__file__).resolve().parents[1]
MAIN_PY = REPO_ROOT / "main.py"
LUA_DIR = REPO_ROOT / "shared" / "lua"
RUST_SEATS = REPO_ROOT / "club-hot" / "src" / "redis_seats.rs"
REPLICATE_GUARD = "if redis.replicate_commands then redis.replicate_commands() end"
PREFIX = "omms:"


def key(*parts: object) -> str:
    return PREFIX + ":".join(str(part) for part in parts)


def lua_script(name: str) -> str:
    return (LUA_DIR / f"{name}.lua").read_text(encoding="utf-8")


def free_port(*, forbidden: set[int] | None = None) -> int:
    blocked = {6379} | (forbidden or set())
    for _ in range(100):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.bind(("127.0.0.1", 0))
            port = int(sock.getsockname()[1])
        if port not in blocked:
            return port
    raise RuntimeError("no free loopback port")


def start_redis(port: int, workdir: Path):
    process = subprocess.Popen(
        [shutil.which("redis-server"), "--bind", "127.0.0.1", "--port", str(port),
         "--save", "", "--appendonly", "no", "--dir", str(workdir), "--loglevel", "warning"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    client = redis.Redis(host="127.0.0.1", port=port, decode_responses=True,
                         socket_connect_timeout=0.2, socket_timeout=2.0)
    deadline = time.monotonic() + 10.0
    while time.monotonic() < deadline:
        try:
            if client.ping():
                return process, client
        except redis.RedisError:
            time.sleep(0.05)
    process.kill()
    raise RuntimeError("private redis-server did not start")


def stop_process(process: subprocess.Popen | None) -> None:
    if process is None or process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=5.0)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5.0)


def seed_settings(db_path: Path) -> None:
    with contextlib.closing(sqlite3.connect(db_path)) as conn:
        conn.executescript(
            """
            CREATE TABLE settings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                registration_start_time TEXT,
                admin_username TEXT DEFAULT 'admin',
                admin_password TEXT DEFAULT NULL
            );
            INSERT INTO settings (registration_start_time, admin_username, admin_password)
            VALUES ('2000-01-01 00:00:00', 'admin', '$argon2id$test-seed');
            """
        )
        conn.commit()


def seed_club_and_student(db_path: Path, capacity: int) -> None:
    with contextlib.closing(sqlite3.connect(db_path, timeout=10.0)) as conn:
        conn.execute("INSERT INTO clubs (id, name, max_students, current_students) VALUES (1, 'club', ?, 0)",
                     (capacity,))
        conn.execute(
            "INSERT INTO students (id, name, class, student_id, username, password) "
            "VALUES (1, 'student', 'class', 'S0001', 'student1', 'unused')"
        )
        conn.commit()


def write_session(client, student_id: int = 1) -> str:
    token = secrets.token_urlsafe(32)
    payload = {"role": "student", "student_id": student_id, "name": "student", "class": "class",
               "student_no": "S0001", "_session_epoch": 0, "_session_version": 1}
    client.set(key("sessgen", "ver", "student", student_id), 1)
    client.set(key("sess", token), json.dumps(payload, separators=(",", ":")), ex=3600)
    return token


def http_json(port: int, method: str, path: str, *, token: str | None = None,
              body: dict | None = None) -> tuple[int, object]:
    encoded = json.dumps(body or {}).encode("utf-8")
    headers = {"Connection": "close"}
    if token:
        headers["Cookie"] = f"session={token}"
    if method == "POST":
        headers.update({"Content-Type": "application/json", "Content-Length": str(len(encoded)),
                        "X-Requested-With": "XMLHttpRequest"})
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10.0)
    try:
        conn.request(method, path, body=encoded if method == "POST" else None, headers=headers)
        response = conn.getresponse()
        raw = response.read()
        return response.status, json.loads(raw) if raw else None
    finally:
        conn.close()


def wait_status(port: int, path: str, expected: int, timeout: float) -> int:
    deadline = time.monotonic() + timeout
    status = -1
    while time.monotonic() < deadline:
        try:
            status, _ = http_json(port, "GET", path)
            if status == expected:
                return status
        except (OSError, http.client.HTTPException):
            pass
        time.sleep(0.1)
    return status


@unittest.skipIf(redis is None or shutil.which("redis-server") is None, "redis unavailable")
class LuaScriptTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="omms-lua-")
        self.redis_process, self.redis = start_redis(free_port(), Path(self.tmp.name))

    def tearDown(self) -> None:
        stop_process(self.redis_process)
        self.tmp.cleanup()

    def test_scripts_that_read_time_enable_effects_replication_first(self) -> None:
        rust = RUST_SEATS.read_text(encoding="utf-8")
        for name in ("acquire", "cancel_begin"):
            code = [line for line in lua_script(name).splitlines() if not line.startswith("--")]
            self.assertIn("redis.call('TIME')", "\n".join(code))
            self.assertEqual(REPLICATE_GUARD, code[0], name)
            self.assertIn('include_str!("../../shared/lua/{}.lua")'.format(name), rust)

    def test_rollback_returns_a_seat_only_while_the_lease_is_owned(self) -> None:
        script = lua_script("rollback")
        self.redis.set(key("stock", 1), 0)
        self.redis.set(key("op", 1), "1|1-a|n1", ex=60)
        self.redis.sadd(key("inflight", 1), "1|1|1-a|n1")
        owned = [key("stock", 1), key("op", 1), key("inflight", 1), key("rb", "1|1-a|n1")]
        self.assertEqual(1, self.redis.eval(script, 4, *owned, "1|1-a|n1", "1|1|1-a|n1", "return"))
        self.assertEqual("1", self.redis.get(key("stock", 1)))
        self.assertEqual(0, self.redis.scard(key("inflight", 1)))
        self.assertEqual(0, self.redis.eval(script, 4, *owned, "1|1-a|n1", "1|1|1-a|n1", "return"))
        self.assertEqual("1", self.redis.get(key("stock", 1)))
        expired = [key("stock", 1), key("op", 2), key("inflight", 1), key("rb", "1|2-b|n2")]
        self.assertEqual(2, self.redis.eval(script, 4, *expired, "1|2-b|n2", "2|1|2-b|n2", "return"))
        self.assertEqual("1", self.redis.get(key("stock", 1)))

    def test_rollback_absorbs_a_seat_when_the_database_is_full(self) -> None:
        script = lua_script("rollback")
        self.redis.set(key("stock", 1), 5)
        self.redis.set(key("op", 3), "1|3-c|n3", ex=60)
        keys = [key("stock", 1), key("op", 3), key("inflight", 1), key("rb", "1|3-c|n3")]
        self.assertEqual(1, self.redis.eval(script, 4, *keys, "1|3-c|n3", "3|1|3-c|n3", "absorb"))
        self.assertEqual("5", self.redis.get(key("stock", 1)))
        self.assertIsNone(self.redis.get(key("op", 3)))


@unittest.skipIf(redis is None or shutil.which("redis-server") is None, "redis unavailable")
class LateRedisTests(unittest.TestCase):
    def test_python_service_recovers_when_redis_starts_after_it(self) -> None:
        with tempfile.TemporaryDirectory(prefix="omms-late-redis-") as tmp:
            workdir = Path(tmp)
            db_path = workdir / "late.sqlite3"
            seed_settings(db_path)
            redis_port = free_port()
            http_port = free_port(forbidden={redis_port})
            env = os.environ.copy()
            env.update({
                "DB_PATH": str(db_path), "REDIS_URL": f"redis://127.0.0.1:{redis_port}/0",
                "HOST": "127.0.0.1", "PORT": str(http_port), "WEB_DIR": str(REPO_ROOT / "web"),
                "LOG_LEVEL": "CRITICAL",
            })
            app = subprocess.Popen([sys.executable, str(MAIN_PY)], cwd=REPO_ROOT, env=env,
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            redis_process = None
            try:
                self.assertEqual(200, wait_status(http_port, "/api/get_clubs", 200, 15.0))
                self.assertEqual(503, http_json(http_port, "GET", "/healthz")[0])
                seed_club_and_student(db_path, capacity=1)
                redis_process, client = start_redis(redis_port, workdir)
                self.assertEqual(200, wait_status(http_port, "/healthz", 200, 10.0))
                self.assertEqual(200, wait_status(http_port, "/readyz", 200, 15.0))
                token = write_session(client)
                status, payload = http_json(http_port, "POST", "/api/register_club",
                                            token=token, body={"club_id": 1})
                self.assertEqual(200, status, payload)
                self.assertTrue(payload.get("success"), payload)
                self.assertEqual("0", client.get(key("stock", 1)))
            finally:
                stop_process(app)
                stop_process(redis_process)


@unittest.skipIf(redis is None or shutil.which("redis-server") is None, "redis unavailable")
class PostCommitFailureTests(unittest.TestCase):
    def test_failure_after_commit_keeps_the_seat_taken(self) -> None:
        with tempfile.TemporaryDirectory(prefix="omms-post-commit-") as tmp:
            workdir = Path(tmp)
            db_path = workdir / "post-commit.sqlite3"
            seed_settings(db_path)
            redis_port = free_port()
            redis_process, client = start_redis(redis_port, workdir)
            saved = {name: os.environ.get(name) for name in ("DB_PATH", "REDIS_URL", "LOG_LEVEL")}
            os.environ.update({"DB_PATH": str(db_path),
                               "REDIS_URL": f"redis://127.0.0.1:{redis_port}/0",
                               "LOG_LEVEL": "CRITICAL"})
            httpd = None
            module = None
            try:
                spec = importlib.util.spec_from_file_location("omms_main_post_commit", MAIN_PY)
                module = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(module)
                module.init_db()
                seed_club_and_student(db_path, capacity=1)
                client.set(key("stock", 1), 1)
                token = write_session(client)

                def fail_after_commit(*_args, **_kwargs):
                    raise RuntimeError("simulated failure after commit")

                module.ClubSystemHandler._confirm_registration = fail_after_commit
                http_port = free_port(forbidden={redis_port})
                httpd = module.Server(("127.0.0.1", http_port), module.ClubSystemHandler)
                threading.Thread(target=httpd.serve_forever, daemon=True).start()

                status, _payload = http_json(http_port, "POST", "/api/register_club",
                                             token=token, body={"club_id": 1})
                self.assertEqual(500, status)
                with contextlib.closing(sqlite3.connect(db_path)) as conn:
                    self.assertEqual(1, conn.execute("SELECT COUNT(*) FROM registrations").fetchone()[0])
                self.assertEqual("0", client.get(key("stock", 1)))
                self.assertEqual([], client.keys(key("rb", "*")))
            finally:
                if httpd is not None:
                    httpd.shutdown()
                    httpd.server_close()
                if module is not None:
                    module.RECONCILE_STOP.set()
                    if module.DB_POOL:
                        module.DB_POOL.close_all()
                for name, value in saved.items():
                    if value is None:
                        os.environ.pop(name, None)
                    else:
                        os.environ[name] = value
                stop_process(redis_process)


if __name__ == "__main__":
    unittest.main(verbosity=2)
