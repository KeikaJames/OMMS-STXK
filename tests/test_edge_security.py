#!/usr/bin/env python3
"""Isolated edge/session regression tests for the security remediation.

This module intentionally starts its own loopback-only Redis, Python, Rust,
and Nginx processes.  It neither reads nor touches the interactive assessment
stack, repository database, Redis :6379, or the Kali bridge listener.
"""

from __future__ import annotations

import contextlib
import http.client
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
import time
import unittest

import redis
from argon2 import PasswordHasher


REPO_ROOT = Path(__file__).resolve().parents[1]
PYTHON_APP = REPO_ROOT / "main.py"
RUST_BINARY = REPO_ROOT / "club-hot" / "target" / "release" / "club-hot"
NGINX_TEMPLATE = REPO_ROOT / "nginx.conf"
FORBIDDEN_REDIS_PORT = 6379
PASSWORD_HASHER = PasswordHasher()


def loopback_port(*, forbidden: set[int] | None = None) -> int:
    blocked = {FORBIDDEN_REDIS_PORT} | (forbidden or set())
    for _ in range(100):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.bind(("127.0.0.1", 0))
            port = int(sock.getsockname()[1])
        if port not in blocked:
            return port
    raise RuntimeError("could not allocate loopback port")


def stop_process(process: subprocess.Popen[bytes] | None) -> None:
    if process is None or process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=5.0)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5.0)


class EdgeSecurityRegressionTests(unittest.TestCase):
    temporary_directory: tempfile.TemporaryDirectory[str] | None = None
    redis_process: subprocess.Popen[bytes] | None = None
    python_process: subprocess.Popen[bytes] | None = None
    rust_process: subprocess.Popen[bytes] | None = None
    nginx_process: subprocess.Popen[bytes] | None = None

    @classmethod
    def setUpClass(cls) -> None:
        if not RUST_BINARY.is_file():
            raise unittest.SkipTest("build club-hot release before running edge regressions")
        nginx = shutil.which("nginx")
        redis_server = shutil.which("redis-server")
        if nginx is None or redis_server is None:
            raise unittest.SkipTest("nginx or redis-server is unavailable")

        cls.temporary_directory = tempfile.TemporaryDirectory(prefix="omms-edge-security-")
        cls.temp_path = Path(cls.temporary_directory.name)
        cls.redis_port = loopback_port()
        cls.python_port = loopback_port(forbidden={cls.redis_port})
        cls.rust_port = loopback_port(forbidden={cls.redis_port, cls.python_port})
        cls.edge_port = loopback_port(
            forbidden={cls.redis_port, cls.python_port, cls.rust_port}
        )
        cls.db_path = cls.temp_path / "edge.sqlite3"
        cls.config_path = cls.temp_path / "nginx.conf"

        cls._seed_settings()
        cls.redis_process = subprocess.Popen(
            [
                redis_server,
                "--bind",
                "127.0.0.1",
                "--port",
                str(cls.redis_port),
                "--save",
                "",
                "--appendonly",
                "no",
                "--protected-mode",
                "yes",
                "--dir",
                str(cls.temp_path),
                "--loglevel",
                "warning",
            ],
            cwd=cls.temp_path,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
        cls._wait_for_redis()

        common_env = os.environ.copy()
        common_env.update(
            {
                "DB_PATH": str(cls.db_path),
                "REDIS_URL": f"redis://127.0.0.1:{cls.redis_port}/0",
                "SESSION_TTL": "3600",
                "RESV_TTL": "60",
                "COOKIE_SECURE": "0",
                "LOG_LEVEL": "WARNING",
            }
        )
        python_env = common_env.copy()
        python_env.update(
            {
                "HOST": "127.0.0.1",
                "PORT": str(cls.python_port),
                "WEB_DIR": str(REPO_ROOT / "web"),
            }
        )
        cls.python_process = subprocess.Popen(
            [sys.executable, str(PYTHON_APP)],
            cwd=REPO_ROOT,
            env=python_env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
        cls._wait_http(cls.python_port, "/api/get_clubs")

        rust_env = common_env.copy()
        rust_env.update(
            {
                "BIND": f"127.0.0.1:{cls.rust_port}",
                "MAX_CONCURRENCY": "128",
            }
        )
        cls.rust_process = subprocess.Popen(
            [str(RUST_BINARY)],
            cwd=REPO_ROOT,
            env=rust_env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
        cls._wait_http(cls.rust_port, "/api/get_clubs")

        template = NGINX_TEMPLATE.read_text(encoding="utf-8")
        # Lower only the test copy's IP bucket so cookie-rotation protection is
        # deterministic without creating a high-rate test.  The checked-in
        # production-oriented value remains 250r/s with a 1000-request burst.
        config = (
            template.replace("__APP_ROOT__", str(REPO_ROOT))
            .replace("listen 8080;", f"listen 127.0.0.1:{cls.edge_port};")
            .replace(
                "server 127.0.0.1:2002 max_fails=2 fail_timeout=5s;",
                f"server 127.0.0.1:{cls.rust_port} max_fails=2 fail_timeout=5s;",
            )
            .replace("server 127.0.0.1:2001 backup;", f"server 127.0.0.1:{cls.python_port} backup;")
            .replace(
                "upstream club_admin { server 127.0.0.1:2001; }",
                f"upstream club_admin {{ server 127.0.0.1:{cls.python_port}; }}",
            )
            .replace("error_log /tmp/club_nginx_error.log warn;", f"error_log {cls.temp_path / 'nginx-error.log'} warn;")
            .replace("pid /tmp/club_nginx.pid;", f"pid {cls.temp_path / 'nginx.pid'};")
            .replace("zone=hotip:10m rate=250r/s", "zone=hotip:10m rate=1r/s")
            .replace("limit_req zone=hotip burst=1000 nodelay;", "limit_req zone=hotip burst=20 nodelay;")
        )
        if "127.0.0.1:2001" in config or "127.0.0.1:2002" in config:
            raise RuntimeError("edge regression config still points at a non-isolated backend")
        cls.config_path.write_text(config, encoding="utf-8")
        checked = subprocess.run(
            [nginx, "-t", "-p", str(cls.temp_path), "-c", str(cls.config_path)],
            cwd=cls.temp_path,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            check=False,
        )
        if checked.returncode != 0:
            raise RuntimeError(checked.stdout.decode("utf-8", "replace"))
        cls.nginx_process = subprocess.Popen(
            [nginx, "-g", "daemon off;", "-p", str(cls.temp_path), "-c", str(cls.config_path)],
            cwd=cls.temp_path,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
        cls._wait_http(cls.edge_port, "/api/get_clubs")

    @classmethod
    def tearDownClass(cls) -> None:
        for process in (
            cls.nginx_process,
            cls.rust_process,
            cls.python_process,
            cls.redis_process,
        ):
            stop_process(process)
            if process is not None and process.stdout is not None:
                process.stdout.close()
        if cls.temporary_directory is not None:
            cls.temporary_directory.cleanup()

    @classmethod
    def _seed_settings(cls) -> None:
        with contextlib.closing(sqlite3.connect(cls.db_path)) as conn:
            conn.executescript(
                """
                CREATE TABLE settings (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    registration_start_time TEXT,
                    admin_username TEXT DEFAULT 'admin',
                    admin_password TEXT DEFAULT NULL
                );
                INSERT INTO settings
                    (registration_start_time, admin_username, admin_password)
                VALUES ('2000-01-01 00:00:00', 'admin', 'admin-password');
                """
            )
            conn.commit()

    @classmethod
    def _wait_for_redis(cls) -> None:
        cls.redis = redis.Redis(
            host="127.0.0.1", port=cls.redis_port, decode_responses=True,
            socket_connect_timeout=0.2, socket_timeout=0.5,
        )
        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline:
            try:
                if cls.redis.ping():
                    return
            except redis.RedisError:
                pass
            time.sleep(0.05)
        raise RuntimeError("timed out waiting for private redis")

    @classmethod
    def _wait_http(cls, port: int, path: str) -> None:
        deadline = time.monotonic() + 15.0
        while time.monotonic() < deadline:
            try:
                status, _headers, _body = cls._request_raw(port, "GET", path)
                if status == 200:
                    return
            except (OSError, TimeoutError, http.client.HTTPException):
                pass
            time.sleep(0.05)
        raise RuntimeError(f"timed out waiting for HTTP service on {port}")

    @classmethod
    def _reset_state(cls) -> None:
        cls.redis.flushdb()
        student_hash = PASSWORD_HASHER.hash("student-password")
        with contextlib.closing(sqlite3.connect(cls.db_path, timeout=10.0)) as conn:
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("DELETE FROM registrations")
            conn.execute("DELETE FROM students")
            conn.execute("DELETE FROM clubs")
            conn.execute("UPDATE settings SET admin_username='admin', admin_password='admin-password'")
            conn.execute(
                "INSERT INTO clubs (id, name, max_students, current_students) VALUES (1, 'edge-club', 3, 0)"
            )
            conn.executemany(
                "INSERT INTO students (id, name, class, student_id, username, password) VALUES (?,?,?,?,?,?)",
                [
                    (1, "edge-one", "edge", "EDGE0001", "edge1", student_hash),
                    (2, "edge-two", "edge", "EDGE0002", "edge2", student_hash),
                ],
            )
            conn.commit()
        cls.redis.mset({"seats:initialized": "1", "open_at": "0", "stock:club:1": "3"})

    @staticmethod
    def _request_raw(
        port: int,
        method: str,
        path: str,
        *,
        body: dict[str, object] | None = None,
        cookie: str | None = None,
    ) -> tuple[int, dict[str, str], bytes]:
        encoded = json.dumps(body or {}, separators=(",", ":")).encode("utf-8")
        headers = {"Connection": "close"}
        if cookie:
            headers["Cookie"] = cookie
        if method == "POST":
            headers["Content-Type"] = "application/json"
            headers["Content-Length"] = str(len(encoded))
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5.0)
        try:
            conn.request(method, path, body=encoded if method == "POST" else None, headers=headers)
            response = conn.getresponse()
            return response.status, {k.lower(): v for k, v in response.getheaders()}, response.read()
        finally:
            conn.close()

    @staticmethod
    def _session(headers: dict[str, str]) -> str:
        value = headers.get("set-cookie", "")
        if not value.startswith("session="):
            raise AssertionError(f"missing session cookie: {headers!r}")
        return value[len("session="):].split(";", 1)[0]

    @classmethod
    def _login(cls, port: int, username: str, password: str, *, admin: bool = False) -> str:
        path = "/api/admin_login" if admin else "/api/login"
        status, headers, body = cls._request_raw(
            port, "POST", path, body={"username": username, "password": password}
        )
        if status != 200:
            raise AssertionError((status, body.decode("utf-8", "replace")))
        return cls._session(headers)

    @staticmethod
    def _read_response(stream) -> tuple[int, dict[str, str], bytes]:
        status_line = stream.readline()
        if not status_line.endswith(b"\r\n"):
            raise AssertionError(f"invalid status line: {status_line!r}")
        parts = status_line[:-2].split()
        headers: dict[str, str] = {}
        while True:
            line = stream.readline()
            if line == b"\r\n":
                break
            name, value = line[:-2].split(b":", 1)
            headers[name.decode("ascii").lower()] = value.decode("latin-1").strip()
        size = int(headers.get("content-length", "0"))
        body = stream.read(size)
        return int(parts[1]), headers, body

    @classmethod
    def _pipelined_edge_pair(cls, first: bytes, second: bytes):
        with socket.create_connection(("127.0.0.1", cls.edge_port), timeout=5.0) as sock:
            sock.sendall(first + second)
            stream = sock.makefile("rb")
            return cls._read_response(stream), cls._read_response(stream)

    @classmethod
    def _raw_edge_request(cls, request: bytes) -> tuple[int, dict[str, str], bytes]:
        with socket.create_connection(("127.0.0.1", cls.edge_port), timeout=5.0) as sock:
            sock.sendall(request)
            return cls._read_response(sock.makefile("rb"))

    def test_z_nginx_headers_and_random_cookie_rotation_are_bounded_by_ip(self) -> None:
        self._reset_state()
        status, headers, _body = self._request_raw(self.edge_port, "GET", "/")
        self.assertEqual(200, status)
        for name in (
            "x-content-type-options",
            "x-frame-options",
            "referrer-policy",
            "permissions-policy",
            "content-security-policy",
        ):
            self.assertIn(name, headers)
        static_status, static_headers, _ = self._request_raw(self.edge_port, "GET", "/easter-egg.js")
        self.assertEqual(200, static_status)
        self.assertIn("content-security-policy", static_headers)

        # Every random cookie gets its own shape-only session bucket. The
        # private config lowers hotip to 1r/s burst=20, so a 429 proves the
        # independent IP bucket is actually applied.
        responses = []
        for _ in range(40):
            fake = secrets.token_urlsafe(32)
            status, response_headers, _body = self._request_raw(
                self.edge_port, "GET", "/api/get_clubs", cookie=f"session={fake}"
            )
            responses.append((status, response_headers))
        statuses = [status for status, _headers in responses]
        self.assertIn(429, statuses, statuses)
        throttled_headers = next(headers for status, headers in responses if status == 429)
        self.assertEqual("1", throttled_headers.get("retry-after"))

    def test_nginx_and_python_keep_responses_aligned_after_rejected_post_body(self) -> None:
        self._reset_state()
        body = b"GET / HTTP/1.1\r\nHost: smuggled\r\n\r\n"
        first = (
            f"POST /api/not_a_route HTTP/1.1\r\nHost: 127.0.0.1:{self.edge_port}\r\n"
            f"Content-Type: application/json\r\nContent-Length: {len(body)}\r\n"
            "Connection: keep-alive\r\n\r\n"
        ).encode("ascii") + body
        second = (
            f"GET /api/get_clubs HTTP/1.1\r\nHost: 127.0.0.1:{self.edge_port}\r\n"
            "Connection: close\r\n\r\n"
        ).encode("ascii")
        first_response, second_response = self._pipelined_edge_pair(first, second)
        self.assertEqual(404, first_response[0])
        self.assertEqual(200, second_response[0], second_response)
        self.assertTrue(second_response[1].get("content-type", "").startswith("application/json"))
        self.assertIsInstance(json.loads(second_response[2]), list)
        self.assertNotIn(b"Unsupported method", second_response[2])

    def test_shared_source_failed_logins_do_not_lock_other_students(self) -> None:
        """A campus NAT must not turn random failed names into a school-wide 429."""
        self._reset_state()
        for index in range(12):
            status, _headers, _body = self._request_raw(
                self.edge_port,
                "POST",
                "/api/login",
                body={"username": f"unknown-{index}", "password": "wrong-password"},
            )
            self.assertEqual(401, status)
        with contextlib.closing(sqlite3.connect(self.db_path)) as conn:
            stored = conn.execute("SELECT password FROM students WHERE username='edge1'").fetchone()
        self.assertTrue(stored and stored[0].startswith("$argon2"))
        token = self._login(self.edge_port, "edge1", "student-password")
        self.assertEqual(43, len(token))

    def test_missing_club_is_not_a_global_reconcile_trigger(self) -> None:
        self._reset_state()
        python_token = self._login(self.python_port, "edge1", "student-password")
        rust_token = self._login(self.rust_port, "edge2", "student-password")
        for port, token in ((self.python_port, python_token), (self.rust_port, rust_token)):
            for club_id in (900001, 900002, 900003):
                status, _headers, body = self._request_raw(
                    port,
                    "POST",
                    "/api/register_club",
                    body={"club_id": club_id},
                    cookie=f"session={token}",
                )
                self.assertEqual(200, status)
                self.assertEqual(
                    {"success": False, "message": "社团不存在"},
                    json.loads(body),
                )
        self.assertEqual("3", self.redis.get("stock:club:1"))
        self.assertEqual([], self.redis.keys("resv:*"))
        self.assertEqual([], self.redis.keys("seat:op:*"))

        for port in (self.python_port, self.rust_port):
            status, _headers, body = self._request_raw(port, "GET", "/readyz")
            self.assertEqual(200, status)
            self.assertEqual("ready", json.loads(body).get("status"))

        for port, token in ((self.python_port, python_token), (self.rust_port, rust_token)):
            status, _headers, body = self._request_raw(
                port,
                "POST",
                "/api/register_club",
                body={"club_id": 1},
                cookie=f"session={token}",
            )
            self.assertEqual(200, status)
            self.assertTrue(json.loads(body).get("success"))
        self.assertEqual("1", self.redis.get("stock:club:1"))

    def test_edge_rejects_multiple_cookie_header_fields_without_revoking_session(self) -> None:
        self._reset_state()
        student_token = self._login(self.edge_port, "edge1", "student-password")
        admin_token = self._login(self.edge_port, "admin", "admin-password", admin=True)
        cases = [
            ("/api/get_student_info", student_token),
            ("/api/get_registrations", admin_token),
        ]
        for path, token in cases:
            raw = (
                f"GET {path} HTTP/1.1\r\nHost: 127.0.0.1:{self.edge_port}\r\n"
                f"Cookie: session={token}\r\nCookie: theme=dark\r\nConnection: close\r\n\r\n"
            ).encode("ascii")
            self.assertEqual(401, self._raw_edge_request(raw)[0])
            status, _headers, _body = self._request_raw(
                self.edge_port, "GET", path, cookie=f"session={token}"
            )
            self.assertEqual(200, status)

    def test_python_and_rust_share_generation_and_duplicate_cookie_contract(self) -> None:
        self._reset_state()
        python_token = self._login(self.python_port, "edge1", "student-password")
        status, _headers, _body = self._request_raw(
            self.rust_port, "GET", "/api/get_student_info", cookie=f"session={python_token}"
        )
        self.assertEqual(200, status)

        # A mutation lock is the critical fence between a generation revoke and
        # its SQLite commit. Neither backend may sign a new session while it is
        # held, even though the old DB password/row is still readable.
        self.redis.set("sess:mutation:student:1", "test-lock", ex=60)
        for port in (self.python_port, self.rust_port):
            locked, _headers, _body = self._request_raw(
                port,
                "POST",
                "/api/login",
                body={"username": "edge1", "password": "student-password"},
            )
            self.assertEqual(503, locked)
        self.redis.delete("sess:mutation:student:1")

        rust_token = self._login(self.rust_port, "edge1", "student-password")
        status, _headers, _body = self._request_raw(
            self.python_port, "GET", "/api/get_student_info", cookie=f"session={python_token}"
        )
        self.assertEqual(401, status)
        status, _headers, _body = self._request_raw(
            self.python_port, "GET", "/api/get_student_info", cookie=f"session={rust_token}"
        )
        self.assertEqual(200, status)

        duplicate = f"session={rust_token}; session={self._login(self.rust_port, 'edge2', 'student-password')}"
        for port in (self.python_port, self.rust_port):
            status, _headers, _body = self._request_raw(
                port, "GET", "/api/get_student_info", cookie=duplicate
            )
            self.assertEqual(401, status)

        admin_token = self._login(self.python_port, "admin", "admin-password", admin=True)
        deleted, _headers, _body = self._request_raw(
            self.python_port,
            "POST",
            "/api/delete_student",
            body={"student_id": 1},
            cookie=f"session={admin_token}",
        )
        self.assertEqual(200, deleted)
        rejected, _headers, _body = self._request_raw(
            self.rust_port,
            "POST",
            "/api/register_club",
            body={"club_id": 1},
            cookie=f"session={rust_token}",
        )
        self.assertEqual(401, rejected)
        self.assertEqual("3", self.redis.get("stock:club:1"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
