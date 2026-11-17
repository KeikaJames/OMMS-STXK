#!/usr/bin/env python3
"""Regression tests for the Python Redis/SQLite registration protocol.

The suite is deliberately isolated from developer data: it creates a temporary
SQLite database, launches a private ``redis-server`` on an ephemeral loopback
port other than 6379, and starts ``main.py`` on another ephemeral port.  Student
sessions are written directly to that private Redis instance, so the tests do
not need to know or generate login passwords.
"""

from __future__ import annotations

import concurrent.futures
import contextlib
import http.client
import io
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
from typing import BinaryIO, Iterable, Sequence


REPO_ROOT = Path(__file__).resolve().parents[1]
MAIN_PY = REPO_ROOT / "main.py"
FORBIDDEN_REDIS_PORT = 6379
CLUB_ID = 1
PREFIX = "omms:"


def key(*parts: object) -> str:
    return PREFIX + ":".join(str(part) for part in parts)


class RedisProtocolError(RuntimeError):
    """The private Redis process returned an invalid RESP response."""


def choose_loopback_port(*, forbidden: Iterable[int] = ()) -> int:
    forbidden_ports = set(forbidden) | {FORBIDDEN_REDIS_PORT}
    for _ in range(100):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.bind(("127.0.0.1", 0))
            port = int(sock.getsockname()[1])
        if port not in forbidden_ports:
            return port
    raise RuntimeError("could not allocate an isolated loopback port")


def encode_redis_command(parts: Sequence[object]) -> bytes:
    encoded = [
        part if isinstance(part, bytes) else str(part).encode("utf-8")
        for part in parts
    ]
    chunks = [f"*{len(encoded)}\r\n".encode("ascii")]
    for part in encoded:
        chunks.extend(
            (
                f"${len(part)}\r\n".encode("ascii"),
                part,
                b"\r\n",
            )
        )
    return b"".join(chunks)


def read_redis_response(stream: BinaryIO) -> object:
    marker = stream.read(1)
    if not marker:
        raise RedisProtocolError("unexpected EOF")
    line = stream.readline()
    if not line.endswith(b"\r\n"):
        raise RedisProtocolError("unterminated RESP line")
    value = line[:-2]
    if marker == b"+":
        return value.decode("utf-8")
    if marker == b"-":
        raise RedisProtocolError(value.decode("utf-8", "replace"))
    if marker == b":":
        return int(value)
    if marker == b"$":
        size = int(value)
        if size == -1:
            return None
        body = stream.read(size)
        if len(body) != size or stream.read(2) != b"\r\n":
            raise RedisProtocolError("invalid RESP bulk string")
        return body.decode("utf-8")
    if marker == b"*":
        count = int(value)
        if count == -1:
            return None
        return [read_redis_response(stream) for _ in range(count)]
    raise RedisProtocolError(f"unsupported RESP marker: {marker!r}")


def redis_pipeline(port: int, commands: Sequence[Sequence[object]]) -> list[object]:
    if port == FORBIDDEN_REDIS_PORT:
        raise RuntimeError("refusing to connect to Redis port 6379")
    if not commands:
        return []
    request = b"".join(encode_redis_command(command) for command in commands)
    with socket.create_connection(("127.0.0.1", port), timeout=2.0) as sock:
        sock.settimeout(5.0)
        sock.sendall(request)
        stream = sock.makefile("rb")
        return [read_redis_response(stream) for _ in commands]


def redis_command(port: int, *parts: object) -> object:
    return redis_pipeline(port, [parts])[0]


class PythonRaceRegressionTests(unittest.TestCase):
    redis_process: subprocess.Popen[bytes] | None = None
    app_process: subprocess.Popen[bytes] | None = None
    temporary_directory: tempfile.TemporaryDirectory[str] | None = None

    @classmethod
    def setUpClass(cls) -> None:
        redis_server = shutil.which("redis-server")
        if redis_server is None:
            raise unittest.SkipTest("redis-server is not installed")

        redis_import = subprocess.run(
            [sys.executable, "-c", "import redis"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        if redis_import.returncode != 0:
            raise unittest.SkipTest("the Python redis package is not installed")

        cls.temporary_directory = tempfile.TemporaryDirectory(
            prefix="omms-python-races-"
        )
        cls.temp_path = Path(cls.temporary_directory.name)
        cls.db_path = cls.temp_path / "isolated.sqlite3"
        if cls.db_path.resolve() == (REPO_ROOT / "club_system.db").resolve():
            raise RuntimeError("refusing to use the repository database")

        cls.redis_port = choose_loopback_port()
        cls.http_port = choose_loopback_port(forbidden=(cls.redis_port,))
        cls._create_settings_seed()

        try:
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

            env = os.environ.copy()
            env.update(
                {
                    "DB_PATH": str(cls.db_path),
                    "REDIS_URL": f"redis://127.0.0.1:{cls.redis_port}/0",
                    "HOST": "127.0.0.1",
                    "PORT": str(cls.http_port),
                    "DB_POOL_SIZE": "32",
                    "SESSION_TTL": "3600",
                    "RESV_TTL": "60",
                    "WEB_DIR": str(REPO_ROOT / "web"),
                    "LOG_LEVEL": "WARNING",
                    "TZ": "UTC",
                    "SEAT_OP_TTL": "5",
                }
            )
            cls.app_process = subprocess.Popen(
                [sys.executable, str(MAIN_PY)],
                cwd=REPO_ROOT,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
            )
            cls._wait_for_app()
        except Exception:
            cls._stop_processes()
            if cls.temporary_directory is not None:
                cls.temporary_directory.cleanup()
                cls.temporary_directory = None
            raise

    @classmethod
    def tearDownClass(cls) -> None:
        cls._stop_processes()
        if cls.temporary_directory is not None:
            cls.temporary_directory.cleanup()
            cls.temporary_directory = None

    @classmethod
    def _create_settings_seed(cls) -> None:
        """Avoid hashing a random admin password during every test startup."""
        conn = sqlite3.connect(cls.db_path)
        try:
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
                VALUES
                    ('2000-01-01 00:00:00', 'admin', '$argon2id$test-seed');
                """
            )
            conn.commit()
        finally:
            conn.close()

    @classmethod
    def _wait_for_redis(cls) -> None:
        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline:
            if cls.redis_process is None or cls.redis_process.poll() is not None:
                raise RuntimeError(cls._process_failure("redis-server", cls.redis_process))
            try:
                if redis_command(cls.redis_port, "PING") == "PONG":
                    return
            except (OSError, RedisProtocolError):
                pass
            time.sleep(0.05)
        raise RuntimeError("timed out waiting for private redis-server")

    @classmethod
    def _wait_for_app(cls) -> None:
        deadline = time.monotonic() + 15.0
        while time.monotonic() < deadline:
            if cls.app_process is None or cls.app_process.poll() is not None:
                raise RuntimeError(cls._process_failure("main.py", cls.app_process))
            try:
                status, _payload = cls._request_json("GET", "/api/get_clubs")
                if status == 200:
                    return
            except (OSError, TimeoutError, http.client.HTTPException):
                pass
            time.sleep(0.05)
        raise RuntimeError("timed out waiting for isolated main.py")

    @staticmethod
    def _process_failure(
        label: str, process: subprocess.Popen[bytes] | None
    ) -> str:
        if process is None:
            return f"{label} was not started"
        output = b""
        if process.stdout is not None:
            output = process.stdout.read()
        detail = output.decode("utf-8", "replace").strip()
        return f"{label} exited with {process.returncode}: {detail}"

    @classmethod
    def _stop_processes(cls) -> None:
        for process in (cls.app_process, cls.redis_process):
            if process is None:
                continue
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5.0)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5.0)
            if process.stdout is not None:
                process.stdout.close()
        cls.app_process = None
        cls.redis_process = None

    @classmethod
    def _request_json(
        cls,
        method: str,
        path: str,
        *,
        token: str | None = None,
        cookie_header: str | None = None,
        body: dict[str, object] | None = None,
        timeout: float = 20.0,
    ) -> tuple[int, object]:
        status, payload, _headers = cls._request_json_with_headers(
            method,
            path,
            token=token,
            cookie_header=cookie_header,
            body=body,
            timeout=timeout,
        )
        return status, payload

    @classmethod
    def _request_json_with_headers(
        cls,
        method: str,
        path: str,
        *,
        token: str | None = None,
        cookie_header: str | None = None,
        body: dict[str, object] | None = None,
        timeout: float = 20.0,
    ) -> tuple[int, object, dict[str, str]]:
        encoded = json.dumps(body or {}, separators=(",", ":")).encode("utf-8")
        headers = {"Connection": "close"}
        if method == "POST":
            headers.update(
                {
                    "Content-Type": "application/json",
                    "Content-Length": str(len(encoded)),
                }
            )
        if cookie_header is not None:
            headers["Cookie"] = cookie_header
        elif token is not None:
            headers["Cookie"] = f"session={token}"
        conn = http.client.HTTPConnection("127.0.0.1", cls.http_port, timeout=timeout)
        try:
            conn.request(method, path, body=encoded if method == "POST" else None, headers=headers)
            response = conn.getresponse()
            response_body = response.read()
            payload: object = json.loads(response_body) if response_body else None
            return response.status, payload, {
                key.lower(): value for key, value in response.getheaders()
            }
        finally:
            conn.close()

    @staticmethod
    def _read_raw_response(stream: BinaryIO) -> tuple[int, dict[str, str], bytes]:
        status_line = stream.readline()
        if not status_line.endswith(b"\r\n"):
            raise AssertionError(f"missing HTTP status line: {status_line!r}")
        parts = status_line[:-2].split()
        if len(parts) < 2:
            raise AssertionError(f"invalid HTTP status line: {status_line!r}")
        headers: dict[str, str] = {}
        while True:
            line = stream.readline()
            if line == b"\r\n":
                break
            if not line.endswith(b"\r\n") or b":" not in line:
                raise AssertionError(f"invalid HTTP header: {line!r}")
            name, value = line[:-2].split(b":", 1)
            headers[name.decode("ascii").lower()] = value.decode("latin-1").strip()
        size = int(headers.get("content-length", "0"))
        body = stream.read(size)
        if len(body) != size:
            raise AssertionError("truncated HTTP response body")
        return int(parts[1]), headers, body

    @classmethod
    def _two_requests_on_one_connection(
        cls, first: bytes, second: bytes
    ) -> list[tuple[int, dict[str, str], bytes]]:
        with socket.create_connection(("127.0.0.1", cls.http_port), timeout=5.0) as sock:
            sock.settimeout(5.0)
            sock.sendall(first + second)
            stream = sock.makefile("rb")
            return [cls._read_raw_response(stream), cls._read_raw_response(stream)]

    @classmethod
    def _one_raw_request(cls, request: bytes) -> tuple[int, dict[str, str], bytes]:
        with socket.create_connection(("127.0.0.1", cls.http_port), timeout=5.0) as sock:
            sock.settimeout(5.0)
            sock.sendall(request)
            return cls._read_raw_response(sock.makefile("rb"))

    @staticmethod
    def _session_token_from_set_cookie(headers: dict[str, str]) -> str:
        value = headers.get("set-cookie", "")
        prefix = "session="
        if not value.startswith(prefix):
            raise AssertionError(f"missing session cookie: {headers!r}")
        token = value[len(prefix):].split(";", 1)[0]
        if len(token) != 43:
            raise AssertionError(f"unexpected session token shape: {token!r}")
        return token

    def _seed_scenario(
        self,
        *,
        users: int,
        capacity: int,
        redis_stock: int,
        existing_registration: bool = False,
    ) -> dict[int, str]:
        operation_id = "seed-operation" if existing_registration else None
        conn = sqlite3.connect(self.db_path, timeout=20.0)
        try:
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("DELETE FROM registrations")
            conn.execute("DELETE FROM students")
            conn.execute("DELETE FROM clubs")
            conn.execute(
                "INSERT INTO clubs (id, name, max_students, current_students) "
                "VALUES (?, ?, ?, ?)",
                (CLUB_ID, "race-club", capacity, 1 if existing_registration else 0),
            )
            conn.executemany(
                "INSERT INTO students "
                "(id, name, class, student_id, username, password) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    (
                        student_id,
                        f"student-{student_id}",
                        "race-class",
                        f"S{student_id:08d}",
                        f"race{student_id}",
                        "unused-by-regression-test",
                    )
                    for student_id in range(1, users + 1)
                ),
            )
            if existing_registration:
                conn.execute(
                    "INSERT INTO registrations "
                    "(student_id, club_id, registration_time, operation_id) "
                    "VALUES (?, ?, ?, ?)",
                    (1, CLUB_ID, "2000-01-01 00:00:01", operation_id),
                )
            conn.commit()
        finally:
            conn.close()

        sessions = {
            student_id: secrets.token_urlsafe(24)
            for student_id in range(1, users + 1)
        }
        commands: list[Sequence[object]] = [
            ("FLUSHDB",),
            ("SET", key("init"), "1"),
            ("SET", key("open_at"), "0"),
            ("SET", key("sessgen", "epoch", "student"), "0"),
            ("SET", key("stock", CLUB_ID), redis_stock),
        ]
        if existing_registration:
            commands.append(
                ("SET", key("reg", 1), f"{CLUB_ID}|{operation_id}")
            )
        for student_id, token in sessions.items():
            session = json.dumps(
                {
                    "role": "student",
                    "student_id": student_id,
                    "name": f"student-{student_id}",
                    "class": "race-class",
                    "student_no": f"S{student_id:08d}",
                    "_session_epoch": 0,
                    "_session_version": 1,
                },
                ensure_ascii=False,
                separators=(",", ":"),
            )
            commands.append(("SET", key("sessgen", "ver", "student", student_id), "1"))
            commands.append(("SET", key("sess", token), session, "EX", 3600))
        replies = redis_pipeline(self.redis_port, commands)
        self.assertEqual("OK", replies[1])
        self.assertEqual("OK", replies[2])
        self.assertEqual("OK", replies[3])
        return sessions

    def _admin_token(self) -> str:
        with contextlib.closing(sqlite3.connect(self.db_path)) as conn:
            conn.execute("UPDATE settings SET admin_password='admin-password' WHERE id=1")
            conn.commit()
        status, payload, headers = self._request_json_with_headers(
            "POST", "/api/admin_login", body={"username": "admin", "password": "admin-password"}
        )
        self.assertEqual(200, status, payload)
        return self._session_token_from_set_cookie(headers)

    def _parallel_post(
        self,
        *,
        requests: int,
        path: str,
        token_for_request,
        body: dict[str, object] | None = None,
    ) -> list[tuple[int, object]]:
        barrier = threading.Barrier(requests)

        def worker(index: int) -> tuple[int, object]:
            barrier.wait(timeout=30.0)
            return self._request_json(
                "POST",
                path,
                token=token_for_request(index),
                body=body,
            )

        with concurrent.futures.ThreadPoolExecutor(max_workers=requests) as executor:
            futures = [executor.submit(worker, index) for index in range(requests)]
            return [future.result(timeout=30.0) for future in futures]

    def test_same_request_id_is_idempotent(self) -> None:
        sessions = self._seed_scenario(users=1, capacity=2, redis_stock=2)
        body = {"club_id": CLUB_ID, "request_id": "retry-request-0001"}
        first = self._request_json("POST", "/api/register_club", token=sessions[1], body=body)
        second = self._request_json("POST", "/api/register_club", token=sessions[1], body=body)
        self.assertEqual((200, True, "registered"), (first[0], first[1]["success"], first[1]["code"]))
        self.assertEqual((200, True, "registered"), (second[0], second[1]["success"], second[1]["code"]))
        with contextlib.closing(sqlite3.connect(self.db_path)) as conn:
            rows = conn.execute("SELECT operation_id FROM registrations").fetchall()
        self.assertEqual([("1-retry-request-0001",)], rows)
        self.assertEqual("1", redis_command(self.redis_port, "GET", key("stock", 1)))

    def test_orphaned_lease_is_repaired_for_its_club_only(self) -> None:
        sessions = self._seed_scenario(users=2, capacity=1, redis_stock=0)
        lease = "1|9-orphan|n0"
        redis_pipeline(self.redis_port, [
            ("SET", key("op", 2), lease, "EX", 1),
            ("SADD", key("inflight", 1), f"2|{lease}"),
        ])
        status, payload = self._request_json("POST", "/api/register_club", token=sessions[1],
                                             body={"club_id": CLUB_ID})
        self.assertEqual("full", payload["code"])
        time.sleep(1.2)
        self.assertEqual(503, self._request_json("GET", "/readyz")[0])
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline and redis_command(self.redis_port, "GET", key("stock", 1)) != "1":
            time.sleep(0.05)
        self.assertEqual("1", redis_command(self.redis_port, "GET", key("stock", 1)))
        self.assertEqual(0, redis_command(self.redis_port, "SCARD", key("inflight", 1)))
        status, payload = self._request_json("POST", "/api/register_club", token=sessions[1],
                                             body={"club_id": CLUB_ID})
        self.assertEqual((200, True), (status, payload["success"]))

    def test_stale_registration_mirror_is_cleared_on_registration(self) -> None:
        sessions = self._seed_scenario(users=1, capacity=1, redis_stock=1)
        redis_command(self.redis_port, "SET", key("reg", 1), "1|left-over")
        status, payload = self._request_json("POST", "/api/register_club", token=sessions[1],
                                             body={"club_id": CLUB_ID})
        self.assertEqual((200, True), (status, payload["success"]))
        with contextlib.closing(sqlite3.connect(self.db_path)) as conn:
            operation_id = conn.execute("SELECT operation_id FROM registrations").fetchone()[0]
        self.assertEqual(f"1|{operation_id}", redis_command(self.redis_port, "GET", key("reg", 1)))

    def test_trusted_device_logs_in_while_others_are_throttled(self) -> None:
        self._seed_scenario(users=1, capacity=1, redis_stock=1)
        with contextlib.closing(sqlite3.connect(self.db_path)) as conn:
            conn.execute("UPDATE students SET password='device-password' WHERE id=1")
            conn.commit()
        status, _payload, headers = self._request_json_with_headers(
            "POST", "/api/login", body={"username": "race1", "password": "device-password"})
        self.assertEqual(200, status)
        conn = http.client.HTTPConnection("127.0.0.1", self.http_port, timeout=10)
        conn.request("POST", "/api/login", body=json.dumps({"username": "race1", "password": "device-password"}),
                     headers={"Content-Type": "application/json"})
        response = conn.getresponse()
        response.read()
        device = next(value.split(";", 1)[0] for name, value in response.getheaders()
                      if name.lower() == "set-cookie" and value.startswith("device="))
        conn.close()
        for _ in range(12):
            self._request_json("POST", "/api/login", body={"username": "race1", "password": "wrong"})
        throttled = self._request_json("POST", "/api/login",
                                       body={"username": "race1", "password": "device-password"})
        self.assertEqual(429, throttled[0])
        trusted = self._request_json("POST", "/api/login", cookie_header=device,
                                     body={"username": "race1", "password": "device-password"})
        self.assertEqual(200, trusted[0], trusted)

    def test_seat_snapshot_supports_conditional_requests(self) -> None:
        self._seed_scenario(users=1, capacity=3, redis_stock=3)
        status, payload, headers = self._request_json_with_headers("GET", "/api/seats")
        self.assertEqual(200, status)
        self.assertEqual([[CLUB_ID, 3]], payload["seats"])
        conn = http.client.HTTPConnection("127.0.0.1", self.http_port, timeout=10)
        conn.request("GET", "/api/seats", headers={"If-None-Match": headers["etag"]})
        response = conn.getresponse()
        self.assertEqual(304, response.status)
        self.assertEqual(b"", response.read())
        conn.close()

    def test_school_time_is_independent_of_the_host_timezone(self) -> None:
        self._seed_scenario(users=1, capacity=1, redis_stock=1)
        admin_token = self._admin_token()
        status, payload = self._request_json("POST", "/api/update_registration_time", token=admin_token,
                                             body={"start_time": "2030-01-01 08:00:00"})
        self.assertEqual(200, status, payload)
        status, payload = self._request_json("GET", "/api/check_registration_time")
        self.assertEqual(1893456000000, payload["open_at"])
        self.assertEqual("2030-01-01 08:00:00", payload["start_time"])
        self._request_json("POST", "/api/update_registration_time", token=admin_token,
                           body={"start_time": "2000-01-01 00:00:00"})

    def test_one_hundred_concurrent_cancels_release_exactly_once(self) -> None:
        sessions = self._seed_scenario(
            users=1,
            capacity=1,
            redis_stock=0,
            existing_registration=True,
        )

        responses = self._parallel_post(
            requests=100,
            path="/api/cancel_registration",
            token_for_request=lambda _index: sessions[1],
        )
        successes = [
            payload
            for status, payload in responses
            if status == 200
            and isinstance(payload, dict)
            and payload.get("success") is True
        ]

        self.assertTrue(all(status in (200, 202) for status, _payload in responses), responses)
        self.assertEqual(1, len(successes), responses)
        with contextlib.closing(sqlite3.connect(self.db_path)) as conn:
            registration_count = conn.execute(
                "SELECT COUNT(*) FROM registrations"
            ).fetchone()[0]
            current_students = conn.execute(
                "SELECT current_students FROM clubs WHERE id=?", (CLUB_ID,)
            ).fetchone()[0]
        self.assertEqual(0, registration_count)
        self.assertEqual(0, current_students)
        self.assertEqual("1", redis_command(self.redis_port, "GET", key("stock", 1)))
        self.assertIsNone(redis_command(self.redis_port, "GET", key("reg", 1)))
        self.assertEqual(0, redis_command(self.redis_port, "SCARD", key("inflight", 1)))
        self.assertIsNone(redis_command(self.redis_port, "GET", key("op", 1)))

    def test_sqlite_capacity_trigger_stops_inflated_redis_stock(self) -> None:
        capacity = 3
        users = 20
        sessions = self._seed_scenario(
            users=users,
            capacity=capacity,
            redis_stock=users,
        )
        with contextlib.closing(sqlite3.connect(self.db_path)) as conn:
            trigger = conn.execute(
                "SELECT sql FROM sqlite_master "
                "WHERE type='trigger' AND name='registrations_capacity_guard'"
            ).fetchone()
        self.assertIsNotNone(trigger, "main.py did not install the capacity trigger")

        responses = self._parallel_post(
            requests=users,
            path="/api/register_club",
            token_for_request=lambda index: sessions[index + 1],
            body={"club_id": CLUB_ID},
        )
        successes = [
            payload
            for status, payload in responses
            if status == 200
            and isinstance(payload, dict)
            and payload.get("success") is True
        ]

        self.assertTrue(
            all(status in (200, 503) for status, _payload in responses), responses
        )
        with contextlib.closing(sqlite3.connect(self.db_path)) as conn:
            registration_count = conn.execute(
                "SELECT COUNT(*) FROM registrations WHERE club_id=?", (CLUB_ID,)
            ).fetchone()[0]
            current_students, maximum = conn.execute(
                "SELECT current_students, max_students FROM clubs WHERE id=?",
                (CLUB_ID,),
            ).fetchone()
        self.assertEqual(capacity, registration_count)
        self.assertEqual(capacity, len(successes), responses)
        self.assertLessEqual(registration_count, maximum)
        self.assertEqual(registration_count, current_students)
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            if redis_command(self.redis_port, "GET", key("stock", 1)) == "0":
                break
            time.sleep(0.05)
        self.assertEqual("0", redis_command(self.redis_port, "GET", key("stock", 1)))
        for student_id in range(1, users + 1):
            self.assertIsNone(
                redis_command(self.redis_port, "GET", key("op", student_id))
            )
            self.assertIsNone(
                redis_command(self.redis_port, "GET", key("op", student_id))
            )

    def test_understock_readiness_triggers_safe_reconciliation(self) -> None:
        self._seed_scenario(users=1, capacity=3, redis_stock=0)
        status, payload = self._request_json("GET", "/readyz")
        self.assertEqual(503, status)
        self.assertIsInstance(payload, dict)
        self.assertEqual("repairing", payload.get("reason"))

        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            if redis_command(self.redis_port, "GET", key("stock", 1)) == "3":
                break
            time.sleep(0.05)
        self.assertEqual("3", redis_command(self.redis_port, "GET", key("stock", 1)))
        status, payload = self._request_json("GET", "/readyz")
        self.assertEqual(200, status)
        self.assertEqual("ready", payload.get("status"))

    def test_confirmed_stureg_exactly_matches_sqlite_operation_id(self) -> None:
        sessions = self._seed_scenario(users=1, capacity=1, redis_stock=1)
        status, payload = self._request_json(
            "POST",
            "/api/register_club",
            token=sessions[1],
            body={"club_id": CLUB_ID},
        )

        self.assertEqual(200, status)
        self.assertIsInstance(payload, dict)
        self.assertTrue(payload.get("success"), payload)
        with contextlib.closing(sqlite3.connect(self.db_path)) as conn:
            row = conn.execute(
                "SELECT club_id, operation_id FROM registrations WHERE student_id=1"
            ).fetchone()
        self.assertIsNotNone(row)
        club_id, operation_id = row
        self.assertTrue(operation_id)
        self.assertEqual(
            f"{club_id}|{operation_id}",
            redis_command(self.redis_port, "GET", key("reg", 1)),
        )
        self.assertEqual(0, redis_command(self.redis_port, "SCARD", key("inflight", 1)))
        self.assertIsNone(redis_command(self.redis_port, "GET", key("op", 1)))
        self.assertEqual("0", redis_command(self.redis_port, "GET", key("stock", 1)))

        retry_status, retry_payload = self._request_json(
            "POST",
            "/api/register_club",
            token=sessions[1],
            body={"club_id": CLUB_ID},
        )
        self.assertEqual(200, retry_status)
        self.assertIsInstance(retry_payload, dict)
        self.assertTrue(retry_payload.get("success"), retry_payload)
        with contextlib.closing(sqlite3.connect(self.db_path)) as conn:
            self.assertEqual(1, conn.execute("SELECT COUNT(*) FROM registrations").fetchone()[0])
        self.assertEqual("0", redis_command(self.redis_port, "GET", key("stock", 1)))

    def test_early_post_responses_cannot_desynchronize_keep_alive_connection(self) -> None:
        """F1: all 401/403/404 POST paths drain before a second request."""
        sessions = self._seed_scenario(users=1, capacity=1, redis_stock=1)
        body = b"GET / HTTP/1.1\r\nHost: smuggled\r\n\r\n"
        second = (
            f"GET /api/get_clubs HTTP/1.1\r\nHost: 127.0.0.1:{self.http_port}\r\n"
            "Connection: close\r\n\r\n"
        ).encode("ascii")
        cases = [
            ("/api/not_a_route", None, 404),
            ("/api/delete_all_students", sessions[1], 403),
        ]
        for path, token, expected_status in cases:
            cookie = f"Cookie: session={token}\r\n" if token else ""
            first = (
                f"POST {path} HTTP/1.1\r\nHost: 127.0.0.1:{self.http_port}\r\n"
                f"Content-Type: application/json\r\nContent-Length: {len(body)}\r\n"
                f"{cookie}Connection: keep-alive\r\n\r\n"
            ).encode("ascii") + body
            first_response, second_response = self._two_requests_on_one_connection(first, second)
            self.assertEqual(expected_status, first_response[0])
            self.assertEqual(200, second_response[0], second_response)
            self.assertEqual("application/json; charset=utf-8", second_response[1].get("content-type"))
            self.assertIsInstance(json.loads(second_response[2]), list)
            self.assertNotIn(b"Unsupported method", second_response[2])

    def test_duplicate_session_cookie_is_rejected_consistently(self) -> None:
        """F3: no Python endpoint may choose one of two competing cookies."""
        sessions = self._seed_scenario(users=2, capacity=2, redis_stock=2)
        status, payload = self._request_json(
            "GET",
            "/api/get_student_info",
            cookie_header=f"session={sessions[1]}; session={sessions[2]}",
        )
        self.assertEqual(401, status)
        self.assertIsInstance(payload, dict)

    def test_multiple_cookie_header_fields_are_rejected_without_deleting_session(self) -> None:
        sessions = self._seed_scenario(users=1, capacity=1, redis_stock=1)
        token = sessions[1]
        request = (
            f"GET /api/get_student_info HTTP/1.1\r\nHost: 127.0.0.1:{self.http_port}\r\n"
            f"Cookie: session={token}\r\nCookie: theme=dark\r\nConnection: close\r\n\r\n"
        ).encode("ascii")
        status, _headers, _body = self._one_raw_request(request)
        self.assertEqual(401, status)

        logout = (
            f"POST /api/logout HTTP/1.1\r\nHost: 127.0.0.1:{self.http_port}\r\n"
            f"Content-Type: application/json\r\nContent-Length: 2\r\n"
            f"Cookie: session={token}\r\nCookie: theme=dark\r\nConnection: close\r\n\r\n{{}}"
        ).encode("ascii")
        self.assertEqual(200, self._one_raw_request(logout)[0])
        self.assertEqual(200, self._request_json("GET", "/api/get_student_info", token=token)[0])

    def test_new_login_revokes_old_token_and_password_change_revokes_current_token(self) -> None:
        """F5: only the most recent session generation remains usable."""
        self._seed_scenario(users=1, capacity=1, redis_stock=1)
        with contextlib.closing(sqlite3.connect(self.db_path)) as conn:
            conn.execute("UPDATE students SET password='temporary-password' WHERE id=1")
            conn.commit()

        first_status, first_payload, first_headers = self._request_json_with_headers(
            "POST", "/api/login", body={"username": "race1", "password": "temporary-password"}
        )
        self.assertEqual(200, first_status, first_payload)
        first_token = self._session_token_from_set_cookie(first_headers)
        second_status, second_payload, second_headers = self._request_json_with_headers(
            "POST", "/api/login", body={"username": "race1", "password": "temporary-password"}
        )
        self.assertEqual(200, second_status, second_payload)
        second_token = self._session_token_from_set_cookie(second_headers)
        self.assertNotEqual(first_token, second_token)
        self.assertEqual(401, self._request_json("GET", "/api/get_student_info", token=first_token)[0])
        self.assertEqual(200, self._request_json("GET", "/api/get_student_info", token=second_token)[0])

        changed_status, changed_payload, changed_headers = self._request_json_with_headers(
            "POST",
            "/api/change_password",
            token=second_token,
            body={"current": "temporary-password", "new": "new-temporary-password"},
        )
        self.assertEqual(200, changed_status, changed_payload)
        self.assertIn("Max-Age=0", changed_headers.get("set-cookie", ""))
        self.assertEqual(401, self._request_json("GET", "/api/get_student_info", token=second_token)[0])

    def test_deleted_student_token_is_rejected_before_registration_side_effects(self) -> None:
        """F6: deletion revokes an old token before it can acquire Redis stock."""
        self._seed_scenario(users=1, capacity=1, redis_stock=1)
        with contextlib.closing(sqlite3.connect(self.db_path)) as conn:
            conn.execute("UPDATE students SET password='student-password' WHERE id=1")
            conn.execute("UPDATE settings SET admin_password='admin-password' WHERE id=1")
            conn.commit()
        _, _, student_headers = self._request_json_with_headers(
            "POST", "/api/login", body={"username": "race1", "password": "student-password"}
        )
        student_token = self._session_token_from_set_cookie(student_headers)
        admin_status, admin_payload, admin_headers = self._request_json_with_headers(
            "POST", "/api/admin_login", body={"username": "admin", "password": "admin-password"}
        )
        self.assertEqual(200, admin_status, admin_payload)
        admin_token = self._session_token_from_set_cookie(admin_headers)
        deleted_status, deleted_payload = self._request_json(
            "POST", "/api/delete_student", token=admin_token, body={"student_id": 1}
        )
        self.assertEqual(200, deleted_status, deleted_payload)
        self.assertEqual("1", redis_command(self.redis_port, "GET", key("stock", 1)))
        for method, path, body in [
            ("GET", "/api/get_student_info", None),
            ("POST", "/api/register_club", {"club_id": 1}),
            ("POST", "/api/cancel_registration", {}),
            ("POST", "/api/change_password", {"current": "x", "new": "abcdef"}),
        ]:
            status, _payload = self._request_json(method, path, token=student_token, body=body)
            self.assertEqual(401, status, (method, path, status))
        self.assertEqual("1", redis_command(self.redis_port, "GET", key("stock", 1)))
        self.assertEqual(0, redis_command(self.redis_port, "SCARD", key("inflight", 1)))
        self.assertIsNone(redis_command(self.redis_port, "GET", key("op", 1)))

    def test_import_clubs_rejects_huge_and_boolean_capacities_per_row(self) -> None:
        """F7: one malformed capacity cannot roll back valid club imports."""
        self._seed_scenario(users=1, capacity=1, redis_stock=1)
        with contextlib.closing(sqlite3.connect(self.db_path)) as conn:
            conn.execute("UPDATE settings SET admin_password='admin-password' WHERE id=1")
            conn.commit()
        _, _, admin_headers = self._request_json_with_headers(
            "POST", "/api/admin_login", body={"username": "admin", "password": "admin-password"}
        )
        admin_token = self._session_token_from_set_cookie(admin_headers)
        status, payload = self._request_json(
            "POST",
            "/api/import_clubs",
            token=admin_token,
            body={
                "clubs": [
                    {"name": "valid-before", "max_students": 2},
                    {"name": "huge", "max_students": 10**21},
                    {"name": "boolean", "max_students": True},
                    {"name": "valid-after", "max_students": 3},
                ]
            },
        )
        self.assertEqual(200, status, payload)
        self.assertEqual({"success": 2, "failed": 2}, payload)
        with contextlib.closing(sqlite3.connect(self.db_path)) as conn:
            names = {
                row[0]
                for row in conn.execute(
                    "SELECT name FROM clubs WHERE name IN ('valid-before', 'valid-after', 'huge', 'boolean')"
                )
            }
        self.assertEqual({"valid-before", "valid-after"}, names)

    def test_admin_password_reset_revokes_sessions_and_audits_without_secret(self) -> None:
        self._seed_scenario(users=1, capacity=2, redis_stock=2)
        with contextlib.closing(sqlite3.connect(self.db_path)) as conn:
            conn.execute("UPDATE students SET password='old-password' WHERE id=1")
            conn.commit()
        _, _, student_headers = self._request_json_with_headers(
            "POST", "/api/login", body={"username": "race1", "password": "old-password"}
        )
        old_token = self._session_token_from_set_cookie(student_headers)
        admin_token = self._admin_token()
        request_id = "reset-student-0001"
        status, payload = self._request_json(
            "POST", "/api/admin/reset_student_password", token=admin_token,
            body={"student_id": 1, "request_id": request_id, "reason": "忘记密码"},
        )
        self.assertEqual(200, status, payload)
        temporary_password = payload.get("temporary_password")
        self.assertIsInstance(temporary_password, str)
        self.assertEqual(12, len(temporary_password))
        self.assertEqual(401, self._request_json("GET", "/api/get_student_info", token=old_token)[0])
        login_status, login_payload, _headers = self._request_json_with_headers(
            "POST", "/api/login", body={"username": "race1", "password": temporary_password}
        )
        self.assertEqual(200, login_status, login_payload)
        replay_status, replay_payload = self._request_json(
            "POST", "/api/admin/reset_student_password", token=admin_token,
            body={"student_id": 1, "request_id": request_id, "reason": "忘记密码"},
        )
        self.assertEqual(200, replay_status, replay_payload)
        self.assertTrue(replay_payload.get("replayed"))
        self.assertNotIn("temporary_password", replay_payload)
        with contextlib.closing(sqlite3.connect(self.db_path)) as conn:
            event = conn.execute(
                "SELECT before_json, after_json, metadata_json FROM audit_events "
                "WHERE action='student.password_reset'"
            ).fetchone()
        self.assertIsNotNone(event)
        self.assertNotIn(temporary_password, "".join(value or "" for value in event))

    def test_update_club_capacity_metadata_and_restrictions_are_targeted_and_audited(self) -> None:
        sessions = self._seed_scenario(users=2, capacity=1, redis_stock=1)
        with contextlib.closing(sqlite3.connect(self.db_path)) as conn:
            conn.execute("UPDATE students SET grade='高一', class='高一(1)班' WHERE id=1")
            conn.execute("UPDATE students SET grade='高二', class='高二(1)班' WHERE id=2")
            conn.commit()
        admin_token = self._admin_token()
        request_id = "club-update-0001"
        status, payload = self._request_json(
            "POST", "/api/update_club", token=admin_token,
            body={
                "club_id": 1, "request_id": request_id, "reason": "增加名额和补充说明",
                "max_students": 3, "description": "动手搭建机器人", "advisor_name": "张老师",
                "meeting_time": "周三 16:30", "location": "科技楼 302",
                "allowed_grades": ["高一"], "allowed_classes": [], "enabled": True,
            },
        )
        self.assertEqual(200, status, payload)
        self.assertEqual("published", payload.get("seat_sync"))
        self.assertEqual("3", redis_command(self.redis_port, "GET", key("stock", 1)))
        status, clubs = self._request_json("GET", "/api/get_clubs")
        self.assertEqual(200, status)
        self.assertEqual("动手搭建机器人", clubs[0]["description"])
        self.assertEqual(["高一"], clubs[0]["allowed_grades"])
        rejected_status, rejected = self._request_json(
            "POST", "/api/register_club", token=sessions[2], body={"club_id": 1}
        )
        self.assertEqual(200, rejected_status)
        self.assertEqual("ineligible_grade", rejected.get("code"))
        accepted_status, accepted = self._request_json(
            "POST", "/api/register_club", token=sessions[1], body={"club_id": 1}
        )
        self.assertEqual(200, accepted_status)
        self.assertTrue(accepted.get("success"))
        replay_status, replay = self._request_json(
            "POST", "/api/update_club", token=admin_token,
            body={"club_id": 1, "request_id": request_id, "reason": "增加名额和补充说明"},
        )
        self.assertEqual(200, replay_status)
        self.assertTrue(replay.get("replayed"))
        with contextlib.closing(sqlite3.connect(self.db_path)) as conn:
            conn.execute(
                "INSERT INTO registrations (student_id,club_id,registration_time,operation_id) VALUES (2,1,?,?)",
                ("2000-01-01 00:00:00", "capacity-guard-test"),
            )
            self.assertRaises(
                sqlite3.IntegrityError,
                conn.execute,
                "UPDATE clubs SET max_students=1 WHERE id=1",
            )
            self.assertRaises(sqlite3.IntegrityError, conn.execute, "DELETE FROM audit_events")

    def test_adjustment_after_lock_places_most_students_and_workbook_lists_everyone(self) -> None:
        conn = sqlite3.connect(self.db_path, timeout=20.0)
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("DELETE FROM registrations")
            conn.execute("DELETE FROM students")
            conn.execute("DELETE FROM clubs")
            conn.executemany(
                "INSERT INTO clubs (id, name, max_students, current_students, allowed_grades) VALUES (?, ?, ?, ?, ?)",
                [(1, "3D打印", 2, 2, "[]"), (2, "传媒", 3, 1, "[]"), (3, "木工", 4, 0, '["高二"]')],
            )
            conn.executemany(
                "INSERT INTO students (id, name, class, grade, student_id, username, password) VALUES (?, ?, ?, ?, ?, ?, ?)",
                [(i, f"学生{i}", "高一1班" if i <= 6 else "高二1班", "高一" if i <= 6 else "高二",
                  f"S{i:04d}", f"adjust{i}", "unused") for i in range(1, 10)],
            )
            conn.executemany(
                "INSERT INTO registrations (student_id, club_id, registration_time, operation_id) VALUES (?, ?, ?, ?)",
                [(1, 1, "2026-01-01 00:00:01", "1-self"), (2, 1, "2026-01-01 00:00:02", "2-self"),
                 (3, 2, "2026-01-01 00:00:03", "admin-placed")],
            )
            conn.execute("UPDATE settings SET registration_locked = 0")
            conn.commit()
        finally:
            conn.close()
        redis_command(self.redis_port, "FLUSHDB")
        deadline = time.monotonic() + 15
        while redis_command(self.redis_port, "GET", key("init")) is None:
            self.assertLess(time.monotonic(), deadline, "seat state was not republished")
            time.sleep(0.1)
        admin_token = self._admin_token()
        try:
            status, plan = self._request_json("GET", "/api/admin/adjustment", token=admin_token)
            self.assertEqual(200, status, plan)
            self.assertEqual((False, 6, 5, 6), (plan["locked"], plan["unregistered"], plan["assignable"], plan["seats"]))
            self.assertEqual([(2, 2, 2), (3, 4, 3)], [(c["id"], c["remaining"], c["assign"]) for c in plan["clubs"]])

            status, refused = self._request_json(
                "POST", "/api/admin/adjust_registrations", token=admin_token, body={"request_id": "adjust-early-0001"})
            self.assertEqual(409, status, refused)

            status, _ = self._request_json(
                "POST", "/api/update_registration_lock", token=admin_token,
                body={"locked": True, "request_id": "adjust-lock-0001", "reason": "截止"})
            self.assertEqual(200, status)
            status, done = self._request_json(
                "POST", "/api/admin/adjust_registrations", token=admin_token, body={"request_id": "adjust-run-0001"})
            self.assertEqual(200, status, done)
            self.assertEqual((5, 1, {"2": 2, "3": 3}), (done["assigned"], done["unassigned"], done["by_club"]))
            status, replay = self._request_json(
                "POST", "/api/admin/adjust_registrations", token=admin_token, body={"request_id": "adjust-run-0001"})
            self.assertEqual((200, True, 5), (status, replay.get("replayed"), replay["assigned"]))

            with contextlib.closing(sqlite3.connect(self.db_path)) as check:
                rows = dict(check.execute("SELECT student_id, club_id FROM registrations").fetchall())
                counts = dict(check.execute("SELECT id, current_students FROM clubs").fetchall())
                adjusted = check.execute(
                    "SELECT COUNT(*) FROM registrations WHERE operation_id LIKE 'adjust-%'").fetchone()[0]
            self.assertEqual(8, len(rows))
            self.assertEqual({1: 2, 2: 3, 3: 3}, counts)
            self.assertEqual(5, adjusted)
            self.assertEqual({7: 3, 8: 3, 9: 3}, {sid: rows[sid] for sid in (7, 8, 9)})
            self.assertEqual(2, sum(1 for sid in (4, 5, 6) if rows.get(sid) == 2))
            self.assertEqual(("0", "1"), (redis_command(self.redis_port, "GET", key("stock", 2)),
                                          redis_command(self.redis_port, "GET", key("stock", 3))))
            self.assertTrue(str(redis_command(self.redis_port, "GET", key("reg", 7))).startswith("3|adjust-"))

            conn = http.client.HTTPConnection("127.0.0.1", self.http_port, timeout=20)
            conn.request("GET", "/api/export_workbook", headers={"Cookie": f"session={admin_token}"})
            response = conn.getresponse()
            workbook_bytes = response.read()
            disposition = response.getheader("Content-Disposition") or ""
            conn.close()
            self.assertEqual(200, response.status)
            self.assertIn("filename*=UTF-8''", disposition)
            try:
                import openpyxl
            except ImportError:
                openpyxl = None
            if openpyxl is not None:
                book = openpyxl.load_workbook(io.BytesIO(workbook_bytes))
                self.assertEqual(["总表", "3D打印", "传媒", "木工", "未报名"], book.sheetnames)
                summary = [[cell.value for cell in row] for row in book["总表"].iter_rows(min_row=2)]
                methods = {row[0]: row[4] for row in summary}
                self.assertEqual(("自选", "管理员安排", "调剂"), (methods["学生1"], methods["学生3"], methods["学生7"]))
                self.assertEqual(3, book["木工"].max_row - 1)
                self.assertEqual(1, book["未报名"].max_row - 1)

            conn = http.client.HTTPConnection("127.0.0.1", self.http_port, timeout=20)
            conn.request("GET", "/api/export_all_data", headers={"Cookie": f"session={admin_token}"})
            response = conn.getresponse()
            header_line = response.read().decode("utf-8-sig").splitlines()[0]
            disposition = response.getheader("Content-Disposition") or ""
            conn.close()
            self.assertEqual("姓名,班级,学号,社团,方式,报名时间", header_line)
            self.assertIn("%E6%8A%A5%E5%90%8D%E6%80%BB%E8%A1%A8", disposition)
        finally:
            self._request_json("POST", "/api/update_registration_lock", token=admin_token,
                               body={"locked": False, "request_id": "adjust-unlock-0001", "reason": "测试结束"})
            with contextlib.closing(sqlite3.connect(self.db_path)) as reset:
                reset.execute("UPDATE settings SET registration_locked = 0")
                reset.commit()
            redis_command(self.redis_port, "DEL", key("locked"))

    def test_admin_assign_remove_transfer_and_operations_snapshot(self) -> None:
        self._seed_scenario(users=2, capacity=2, redis_stock=2)
        with contextlib.closing(sqlite3.connect(self.db_path)) as conn:
            conn.execute("INSERT INTO clubs (id,name,max_students,current_students) VALUES (2,'second-club',2,0)")
            conn.commit()
        redis_command(self.redis_port, "SET", key("stock", 2), "2")
        admin_token = self._admin_token()
        assign_status, assigned = self._request_json(
            "POST", "/api/admin/assign_registration", token=admin_token,
            body={"student_id": 1, "club_id": 1, "request_id": "assign-0001", "reason": "线下确认"},
        )
        self.assertEqual(200, assign_status, assigned)
        self.assertEqual("1", redis_command(self.redis_port, "GET", key("stock", 1)))
        transfer_status, transferred = self._request_json(
            "POST", "/api/admin/transfer_registration", token=admin_token,
            body={"student_id": 1, "club_id": 2, "request_id": "transfer-0001", "reason": "调整到第二社团"},
        )
        self.assertEqual(200, transfer_status, transferred)
        self.assertEqual("2", redis_command(self.redis_port, "GET", key("stock", 1)))
        self.assertEqual("1", redis_command(self.redis_port, "GET", key("stock", 2)))
        list_status, students = self._request_json("GET", "/api/get_all_students", token=admin_token)
        self.assertEqual(200, list_status)
        self.assertEqual({1: 2, 2: None}, {row["id"]: row["club_id"] for row in students})
        remove_status, removed = self._request_json(
            "POST", "/api/admin/remove_registration", token=admin_token,
            body={"student_id": 1, "request_id": "remove-0001", "reason": "学生退出"},
        )
        self.assertEqual(200, remove_status, removed)
        self.assertEqual("2", redis_command(self.redis_port, "GET", key("stock", 2)))
        audit_status, audit = self._request_json("GET", "/api/admin/audit_events", token=admin_token)
        self.assertEqual(200, audit_status)
        actions = {event["action"] for event in audit["events"]}
        self.assertTrue({"registration.admin_assigned", "registration.admin_transferred", "registration.admin_removed"} <= actions)
        role_status, admin_events = self._request_json("GET", "/api/admin/audit_events?role=admin", token=admin_token)
        self.assertEqual(200, role_status)
        self.assertTrue(admin_events["events"])
        self.assertEqual({"admin"}, {event["actor_role"] for event in admin_events["events"]})
        bad_status, _ = self._request_json("GET", "/api/admin/audit_events?role=root", token=admin_token)
        self.assertEqual(400, bad_status)
        metrics_status, metrics = self._request_json("GET", "/api/admin/operations_snapshot", token=admin_token)
        self.assertEqual(200, metrics_status)
        self.assertEqual(60, metrics["window_seconds"])
        plan_status, plan = self._request_json("GET", "/api/admin/preflight_plan", token=admin_token)
        self.assertEqual(200, plan_status)
        self.assertEqual("isolated-cli-only", plan["mode"])

    def test_admin_upload_and_remove_club_image_are_revisioned_and_allowlisted(self) -> None:
        self._seed_scenario(users=1, capacity=2, redis_stock=2)
        admin_token = self._admin_token()
        try:
            from PIL import Image
        except ImportError:
            self.skipTest("Pillow unavailable")
        def upload(picture, revision: int, request_id: str) -> dict:
            out = io.BytesIO()
            picture.save(out, format="PNG")
            raw = out.getvalue()
            request = (
                f"POST /api/upload_club_image?club_id=1&expected_revision={revision}&request_id={request_id}"
                f"&reason=upload HTTP/1.1\r\nHost: 127.0.0.1:{self.http_port}\r\n"
                "Content-Type: image/png\r\n"
                f"Content-Length: {len(raw)}\r\nCookie: session={admin_token}\r\nConnection: close\r\n\r\n"
            ).encode("ascii") + raw
            status, headers, body = self._one_raw_request(request)
            self.assertEqual(200, status, body)
            self.assertEqual("application/json; charset=utf-8", headers.get("content-type"))
            return json.loads(body)

        def fetch(path: str) -> tuple[int, dict[str, str], bytes]:
            return self._one_raw_request(
                f"GET {path} HTTP/1.1\r\nHost: 127.0.0.1:{self.http_port}\r\nConnection: close\r\n\r\n".encode("ascii"))

        transparent = upload(Image.new("RGBA", (8, 8), (0, 0, 128, 200)), 0, "image-upload-0001")
        self.assertRegex(transparent["club"]["image_path"], r"^/club-images/1-[0-9a-f]{32}\.png$")
        image_status, image_headers, _image_body = fetch(transparent["club"]["image_path"])
        self.assertEqual((200, "image/png"), (image_status, image_headers.get("content-type")))

        uploaded = upload(Image.new("RGB", (2400, 1600), "navy"), transparent["club"]["revision"], "image-upload-0002")
        image_path = uploaded["club"]["image_path"]
        self.assertRegex(image_path, r"^/club-images/1-[0-9a-f]{32}\.jpg$")
        image_status, image_headers, image_body = fetch(image_path)
        self.assertEqual((200, "image/jpeg"), (image_status, image_headers.get("content-type")))
        self.assertEqual((960, 640), Image.open(io.BytesIO(image_body)).size)
        self.assertEqual(404, fetch(transparent["club"]["image_path"])[0])
        image_get = (
            f"GET {image_path} HTTP/1.1\r\nHost: 127.0.0.1:{self.http_port}\r\nConnection: close\r\n\r\n"
        ).encode("ascii")
        clubs_status, clubs = self._request_json("GET", "/api/get_clubs")
        self.assertEqual(200, clubs_status)
        self.assertEqual(image_path, clubs[0]["image_path"])
        self.assertEqual(uploaded["club"]["revision"], clubs[0]["revision"])
        remove_status, removed = self._request_json(
            "POST", "/api/remove_club_image", token=admin_token,
            body={"club_id": 1, "expected_revision": uploaded["club"]["revision"],
                  "request_id": "image-remove-0001", "reason": "remove"},
        )
        self.assertEqual(200, remove_status, removed)
        self.assertIsNone(removed["club"]["image_path"])
        self.assertEqual(404, self._one_raw_request(image_get)[0])


if __name__ == "__main__":
    unittest.main(verbosity=2)
