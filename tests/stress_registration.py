#!/usr/bin/env python3
"""Isolated concurrent registration validator for the OMMS-STXK Rust service.

The tool never connects to Redis port 6379 and never opens the repository's
``club_system.db``.  It creates a temporary SQLite database, launches a private
``redis-server`` on an ephemeral loopback port, seeds one independent Redis
session per virtual student, starts the selected Rust release, and removes all
temporary state when it exits.
"""

from __future__ import annotations

import argparse
import asyncio
import collections
import contextlib
import json
import math
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
from dataclasses import dataclass
from typing import BinaryIO, Iterable, Sequence


REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_RUST_BIN = REPO_ROOT / "club-hot" / "target" / "release" / "club-hot"
FORBIDDEN_REDIS_PORT = 6379
CLUB_ID = 1
SESSION_TTL_SECONDS = 3600


class SetupError(RuntimeError):
    """The isolated test environment could not be prepared."""


class RedisProtocolError(RuntimeError):
    """The private Redis instance returned an invalid or error response."""


@dataclass(frozen=True)
class Attempt:
    student_id: int
    latency_ms: float
    status: int | None
    payload: object | None
    error: str | None


@dataclass(frozen=True)
class DatabaseState:
    registrations: int
    unique_students: int
    current_students: int
    max_students: int
    registered_student_ids: frozenset[int]
    operation_ids: dict[int, str | None]


@dataclass(frozen=True)
class RedisState:
    stock: int | None
    stureg: dict[int, str]
    reservations: dict[int, str]
    operations: dict[int, str]


def positive_int(value: str) -> int:
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be greater than zero")
    return parsed


def positive_float(value: str) -> float:
    parsed = float(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be greater than zero")
    return parsed


def parse_args(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Launch an isolated Redis + SQLite + Rust stack and make one "
            "simultaneous registration attempt per virtual student."
        )
    )
    parser.add_argument("--seats", type=positive_int, default=10)
    parser.add_argument("--users", type=positive_int, default=100)
    parser.add_argument("--concurrency", type=positive_int, default=50)
    parser.add_argument(
        "--rust-bin",
        type=Path,
        default=DEFAULT_RUST_BIN,
        help="Rust release executable to launch (default: %(default)s)",
    )
    parser.add_argument(
        "--redis-server",
        default=shutil.which("redis-server") or "redis-server",
        help="redis-server executable (default: resolved from PATH)",
    )
    parser.add_argument(
        "--request-timeout",
        type=positive_float,
        default=20.0,
        help="client timeout for each registration request in seconds",
    )
    parser.add_argument(
        "--startup-timeout",
        type=positive_float,
        default=10.0,
        help="maximum startup wait for Redis and Rust in seconds",
    )
    parser.add_argument(
        "--server-max-concurrency",
        type=positive_int,
        default=1024,
        help="MAX_CONCURRENCY supplied to club-hot",
    )
    parser.add_argument(
        "--server-request-timeout",
        type=positive_int,
        default=15,
        help="REQUEST_TIMEOUT_SECS supplied to club-hot",
    )
    parser.add_argument(
        "--reservation-ttl",
        type=positive_int,
        default=60,
        help="RESV_TTL supplied to club-hot",
    )
    return parser.parse_args(argv)


def choose_loopback_port(*, forbidden: Iterable[int] = ()) -> int:
    forbidden_set = set(forbidden) | {FORBIDDEN_REDIS_PORT}
    for _ in range(50):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.bind(("127.0.0.1", 0))
            port = int(sock.getsockname()[1])
        if port not in forbidden_set:
            return port
    raise SetupError("could not allocate a safe ephemeral loopback port")


def create_database(path: Path, *, users: int, seats: int) -> None:
    if path.resolve() == (REPO_ROOT / "club_system.db").resolve():
        raise SetupError("refusing to use the repository database")
    conn = sqlite3.connect(path)
    try:
        conn.executescript(
            """
            PRAGMA journal_mode=WAL;
            PRAGMA synchronous=NORMAL;
            PRAGMA foreign_keys=ON;

            CREATE TABLE students (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                class TEXT NOT NULL,
                student_id TEXT NOT NULL UNIQUE,
                username TEXT NOT NULL UNIQUE,
                password TEXT NOT NULL
            );
            CREATE TABLE clubs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE,
                max_students INTEGER NOT NULL CHECK(max_students > 0),
                current_students INTEGER DEFAULT 0
            );
            CREATE TABLE registrations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                student_id INTEGER NOT NULL,
                club_id INTEGER NOT NULL,
                registration_time TEXT NOT NULL,
                operation_id TEXT,
                FOREIGN KEY (student_id) REFERENCES students (id),
                FOREIGN KEY (club_id) REFERENCES clubs (id),
                UNIQUE (student_id)
            );
            CREATE INDEX idx_registrations_club_id
                ON registrations(club_id);
            CREATE UNIQUE INDEX idx_registrations_operation_id
                ON registrations(operation_id) WHERE operation_id IS NOT NULL;
            CREATE TRIGGER registrations_capacity_guard
            BEFORE INSERT ON registrations FOR EACH ROW BEGIN
                SELECT CASE WHEN
                    (SELECT COUNT(*) FROM registrations
                     WHERE club_id = NEW.club_id) >=
                    (SELECT max_students FROM clubs WHERE id = NEW.club_id)
                THEN RAISE(ABORT, 'club full') END;
            END;
            CREATE TABLE settings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                registration_start_time TEXT,
                admin_username TEXT DEFAULT 'admin',
                admin_password TEXT DEFAULT NULL
            );
            """
        )
        conn.execute(
            "INSERT INTO clubs (id, name, max_students, current_students) "
            "VALUES (?, ?, ?, 0)",
            (CLUB_ID, "stress-club", seats),
        )
        conn.execute(
            "INSERT INTO settings "
            "(registration_start_time, admin_username, admin_password) "
            "VALUES (?, ?, ?)",
            ("2000-01-01 00:00:00", "admin", "unused-by-stress-test"),
        )
        conn.executemany(
            "INSERT INTO students "
            "(id, name, class, student_id, username, password) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                (
                    student_id,
                    f"student-{student_id}",
                    "stress-class",
                    f"S{student_id:08d}",
                    f"stress{student_id}",
                    "unused-by-stress-test",
                )
                for student_id in range(1, users + 1)
            ),
        )
        conn.commit()
    finally:
        conn.close()


def encode_redis_command(parts: Sequence[object]) -> bytes:
    encoded = []
    for part in parts:
        if isinstance(part, bytes):
            encoded.append(part)
        else:
            encoded.append(str(part).encode("utf-8"))
    result = [f"*{len(encoded)}\r\n".encode("ascii")]
    for part in encoded:
        result.append(f"${len(part)}\r\n".encode("ascii"))
        result.append(part)
        result.append(b"\r\n")
    return b"".join(result)


def read_redis_response(stream: BinaryIO) -> object:
    marker = stream.read(1)
    if not marker:
        raise RedisProtocolError("unexpected EOF")
    line = stream.readline()
    if not line.endswith(b"\r\n"):
        raise RedisProtocolError("unterminated response")
    value = line[:-2]
    if marker == b"+":
        return value.decode("utf-8")
    if marker == b"-":
        raise RedisProtocolError(value.decode("utf-8", "replace"))
    if marker == b":":
        return int(value)
    if marker == b"$":
        length = int(value)
        if length == -1:
            return None
        body = stream.read(length)
        trailer = stream.read(2)
        if len(body) != length or trailer != b"\r\n":
            raise RedisProtocolError("invalid bulk response")
        return body.decode("utf-8")
    if marker == b"*":
        count = int(value)
        if count == -1:
            return None
        return [read_redis_response(stream) for _ in range(count)]
    raise RedisProtocolError(f"unknown response marker {marker!r}")


def redis_pipeline(port: int, commands: Sequence[Sequence[object]]) -> list[object]:
    if port == FORBIDDEN_REDIS_PORT:
        raise SetupError("refusing to connect to Redis port 6379")
    if not commands:
        return []
    payload = b"".join(encode_redis_command(command) for command in commands)
    with socket.create_connection(("127.0.0.1", port), timeout=2.0) as sock:
        sock.settimeout(5.0)
        sock.sendall(payload)
        stream = sock.makefile("rb")
        return [read_redis_response(stream) for _ in commands]


def redis_command(port: int, *parts: object) -> object:
    return redis_pipeline(port, [parts])[0]


def wait_for_redis(process: subprocess.Popen[bytes], port: int, timeout: float) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise SetupError(f"redis-server exited with status {process.returncode}")
        try:
            if redis_command(port, "PING") == "PONG":
                return
        except (OSError, RedisProtocolError):
            pass
        time.sleep(0.05)
    raise SetupError("timed out waiting for private redis-server")


def seed_sessions(port: int, users: int) -> dict[int, str]:
    sessions: dict[int, str] = {}
    commands: list[Sequence[object]] = []
    for student_id in range(1, users + 1):
        token = secrets.token_urlsafe(32)
        sessions[student_id] = token
        payload = json.dumps(
            {
                "role": "student",
                "student_id": student_id,
                "name": f"student-{student_id}",
                "class": "stress-class",
                "student_no": f"S{student_id:08d}",
                "_session_epoch": 0,
                "_session_version": 1,
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )
        commands.append(("SET", f"sess:version:student:{student_id}", "1"))
        commands.append(("SET", f"sess:{token}", payload, "EX", SESSION_TTL_SECONDS))
        if len(commands) >= 1000:
            replies = redis_pipeline(port, commands)
            if any(reply != "OK" for reply in replies):
                raise SetupError("failed to seed virtual-student sessions")
            commands.clear()
    if commands:
        replies = redis_pipeline(port, commands)
        if any(reply != "OK" for reply in replies):
            raise SetupError("failed to seed virtual-student sessions")
    return sessions


def get_http_status(port: int, path: str, timeout: float = 1.0) -> int | None:
    request = (
        f"GET {path} HTTP/1.1\r\n"
        f"Host: 127.0.0.1:{port}\r\n"
        "Connection: close\r\n\r\n"
    ).encode("ascii")
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=timeout) as sock:
            sock.settimeout(timeout)
            sock.sendall(request)
            first_line = sock.makefile("rb").readline().decode("ascii", "replace")
        parts = first_line.split()
        return int(parts[1]) if len(parts) >= 2 else None
    except (OSError, ValueError):
        return None


def wait_for_rust(process: subprocess.Popen[bytes], port: int, timeout: float) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise SetupError(f"club-hot exited with status {process.returncode}")
        if get_http_status(port, "/healthz") == 200 and get_http_status(port, "/readyz") == 200:
            return
        time.sleep(0.05)
    raise SetupError("timed out waiting for club-hot health/readiness")


async def read_chunked_body(reader: asyncio.StreamReader) -> bytes:
    body = bytearray()
    while True:
        size_line = await reader.readline()
        if not size_line:
            raise ValueError("unexpected EOF in chunked response")
        size = int(size_line.split(b";", 1)[0].strip(), 16)
        if size == 0:
            while True:
                trailer = await reader.readline()
                if trailer in (b"\r\n", b""):
                    return bytes(body)
        body.extend(await reader.readexactly(size))
        if await reader.readexactly(2) != b"\r\n":
            raise ValueError("invalid chunk terminator")


async def register_once(
    port: int,
    student_id: int,
    token: str,
    request_timeout: float,
) -> Attempt:
    started = time.perf_counter()
    writer: asyncio.StreamWriter | None = None
    try:
        body = json.dumps({"club_id": CLUB_ID}, separators=(",", ":")).encode("utf-8")
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection("127.0.0.1", port),
            timeout=request_timeout,
        )
        request = (
            "POST /api/register_club HTTP/1.1\r\n"
            f"Host: 127.0.0.1:{port}\r\n"
            "Content-Type: application/json\r\n"
            f"Content-Length: {len(body)}\r\n"
            f"Cookie: session={token}\r\n"
            "Connection: close\r\n\r\n"
        ).encode("ascii") + body
        writer.write(request)
        await asyncio.wait_for(writer.drain(), timeout=request_timeout)
        header_block = await asyncio.wait_for(
            reader.readuntil(b"\r\n\r\n"),
            timeout=request_timeout,
        )
        lines = header_block[:-4].split(b"\r\n")
        status_parts = lines[0].split()
        if len(status_parts) < 2:
            raise ValueError("invalid HTTP status line")
        status = int(status_parts[1])
        headers: dict[str, str] = {}
        for line in lines[1:]:
            if b":" in line:
                key, value = line.split(b":", 1)
                headers[key.decode("ascii", "ignore").lower()] = value.decode(
                    "latin-1"
                ).strip()
        if "content-length" in headers:
            response_body = await asyncio.wait_for(
                reader.readexactly(int(headers["content-length"])),
                timeout=request_timeout,
            )
        elif "chunked" in headers.get("transfer-encoding", "").lower():
            response_body = await asyncio.wait_for(
                read_chunked_body(reader),
                timeout=request_timeout,
            )
        else:
            response_body = await asyncio.wait_for(reader.read(), timeout=request_timeout)
        try:
            payload: object | None = json.loads(response_body) if response_body else None
        except json.JSONDecodeError:
            payload = response_body.decode("utf-8", "replace")
        return Attempt(
            student_id=student_id,
            latency_ms=(time.perf_counter() - started) * 1000,
            status=status,
            payload=payload,
            error=None,
        )
    except Exception as exc:  # the result must retain every transport failure
        return Attempt(
            student_id=student_id,
            latency_ms=(time.perf_counter() - started) * 1000,
            status=None,
            payload=None,
            error=f"{type(exc).__name__}: {exc}",
        )
    finally:
        if writer is not None:
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()


async def run_spike(
    port: int,
    sessions: dict[int, str],
    concurrency: int,
    request_timeout: float,
) -> list[Attempt]:
    semaphore = asyncio.Semaphore(concurrency)
    start = asyncio.Event()

    async def worker(student_id: int, token: str) -> Attempt:
        async with semaphore:
            await start.wait()
            return await asyncio.wait_for(
                register_once(port, student_id, token, request_timeout),
                timeout=request_timeout + 1.0,
            )

    tasks = [
        asyncio.create_task(worker(student_id, token))
        for student_id, token in sessions.items()
    ]
    # Give the first wave a chance to acquire the semaphore before releasing it.
    await asyncio.sleep(0.05)
    start.set()
    results = await asyncio.gather(*tasks, return_exceptions=True)
    attempts: list[Attempt] = []
    for student_id, result in zip(sessions, results):
        if isinstance(result, Attempt):
            attempts.append(result)
        else:
            attempts.append(
                Attempt(
                    student_id=student_id,
                    latency_ms=(request_timeout + 1.0) * 1000,
                    status=None,
                    payload=None,
                    error=f"{type(result).__name__}: {result}",
                )
            )
    return attempts


def read_database_state(path: Path) -> DatabaseState:
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        registrations, unique_students = conn.execute(
            "SELECT COUNT(*), COUNT(DISTINCT student_id) "
            "FROM registrations WHERE club_id = ?",
            (CLUB_ID,),
        ).fetchone()
        current_students, max_students = conn.execute(
            "SELECT current_students, max_students FROM clubs WHERE id = ?",
            (CLUB_ID,),
        ).fetchone()
        registered_rows = list(
            conn.execute(
                "SELECT student_id, operation_id FROM registrations WHERE club_id = ?",
                (CLUB_ID,),
            )
        )
        registered_student_ids = frozenset(int(row[0]) for row in registered_rows)
        operation_ids = {
            int(student_id): (str(operation_id) if operation_id is not None else None)
            for student_id, operation_id in registered_rows
        }
        return DatabaseState(
            registrations=int(registrations),
            unique_students=int(unique_students),
            current_students=int(current_students),
            max_students=int(max_students),
            registered_student_ids=registered_student_ids,
            operation_ids=operation_ids,
        )
    finally:
        conn.close()


def read_redis_state(port: int, users: int) -> RedisState:
    commands: list[Sequence[object]] = [("GET", f"stock:club:{CLUB_ID}")]
    commands.extend(("GET", f"student:reg:{sid}") for sid in range(1, users + 1))
    commands.extend(("GET", f"resv:{sid}") for sid in range(1, users + 1))
    commands.extend(("GET", f"seat:op:{sid}") for sid in range(1, users + 1))
    replies = redis_pipeline(port, commands)
    stock_raw = replies[0]
    stock = int(stock_raw) if stock_raw is not None else None
    stureg_replies = replies[1 : users + 1]
    resv_replies = replies[users + 1 : 2 * users + 1]
    operation_replies = replies[2 * users + 1 :]
    stureg = {
        sid: str(value)
        for sid, value in enumerate(stureg_replies, start=1)
        if value is not None
    }
    reservations = {
        sid: str(value)
        for sid, value in enumerate(resv_replies, start=1)
        if value is not None
    }
    operations = {
        sid: str(value)
        for sid, value in enumerate(operation_replies, start=1)
        if value is not None
    }
    return RedisState(
        stock=stock,
        stureg=stureg,
        reservations=reservations,
        operations=operations,
    )


def parse_registration_value(raw: str) -> tuple[int, str] | None:
    """Parse the current ``<club_id>|<operation_id>`` Redis value contract."""
    club_text, separator, operation_id = raw.partition("|")
    if not separator or not club_text or not operation_id:
        return None
    try:
        club_id = int(club_text)
    except ValueError:
        return None
    return club_id, operation_id


def percentile(values: Sequence[float], fraction: float) -> float:
    if not values:
        return math.nan
    ordered = sorted(values)
    position = (len(ordered) - 1) * fraction
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    weight = position - lower
    return ordered[lower] * (1.0 - weight) + ordered[upper] * weight


def classify_attempts(
    attempts: Sequence[Attempt],
) -> tuple[list[Attempt], list[Attempt], list[Attempt], list[Attempt], list[Attempt]]:
    successes: list[Attempt] = []
    full: list[Attempt] = []
    other_business: list[Attempt] = []
    http_errors: list[Attempt] = []
    transport_errors: list[Attempt] = []
    for attempt in attempts:
        if attempt.error is not None or attempt.status is None:
            transport_errors.append(attempt)
            continue
        if attempt.status != 200:
            http_errors.append(attempt)
            continue
        payload = attempt.payload
        if isinstance(payload, dict) and payload.get("success") is True:
            successes.append(attempt)
        elif isinstance(payload, dict) and "满员" in str(payload.get("message", "")):
            full.append(attempt)
        else:
            other_business.append(attempt)
    return successes, full, other_business, http_errors, transport_errors


def validate_and_report(
    args: argparse.Namespace,
    attempts: Sequence[Attempt],
    db: DatabaseState,
    redis: RedisState,
    elapsed_seconds: float,
) -> bool:
    successes, full, other, http_errors, transport_errors = classify_attempts(attempts)
    success_ids = {attempt.student_id for attempt in successes}
    expected_successes = min(args.seats, args.users)
    expected_full = args.users - expected_successes
    http_statuses = collections.Counter(
        attempt.status for attempt in http_errors if attempt.status is not None
    )
    latencies = [attempt.latency_ms for attempt in attempts]
    parsed_stureg = {
        student_id: parse_registration_value(raw)
        for student_id, raw in redis.stureg.items()
    }
    expected_stureg = {
        student_id: f"{CLUB_ID}|{operation_id}"
        for student_id, operation_id in db.operation_ids.items()
        if operation_id
    }

    checks: list[tuple[str, bool, str]] = [
        (
            "one terminal result per virtual student",
            len(attempts) == args.users,
            f"attempts={len(attempts)}, users={args.users}",
        ),
        (
            "all available seats are awarded exactly once",
            len(successes) == expected_successes,
            f"success={len(successes)}, expected={expected_successes}",
        ),
        (
            "all remaining users receive the full response",
            len(full) == expected_full,
            f"full={len(full)}, expected={expected_full}",
        ),
        (
            "no unexpected business response",
            not other,
            f"other_business={len(other)}",
        ),
        (
            "no HTTP or transport failure",
            not http_errors and not transport_errors,
            f"http={len(http_errors)}, transport={len(transport_errors)}",
        ),
        (
            "SQLite registration count matches successful responses",
            db.registrations == len(successes),
            f"db={db.registrations}, success={len(successes)}",
        ),
        (
            "SQLite contains no duplicate student registration",
            db.unique_students == db.registrations,
            f"unique={db.unique_students}, rows={db.registrations}",
        ),
        (
            "SQLite current_students matches registration rows",
            db.current_students == db.registrations,
            f"current={db.current_students}, rows={db.registrations}",
        ),
        (
            "SQLite never exceeds the configured club capacity",
            db.registrations <= db.max_students == args.seats,
            f"rows={db.registrations}, max={db.max_students}, seats={args.seats}",
        ),
        (
            "Redis stock matches SQLite ground truth",
            redis.stock == args.seats - db.registrations,
            f"stock={redis.stock}, expected={args.seats - db.registrations}",
        ),
        (
            "every SQLite registration has a non-empty operation ID",
            len(expected_stureg) == db.registrations,
            f"operation_ids={len(expected_stureg)}, rows={db.registrations}",
        ),
        (
            "Redis registration mirrors use club_id|operation_id values",
            all(
                parsed is not None and parsed[0] == CLUB_ID
                for parsed in parsed_stureg.values()
            ),
            f"encoded={sum(parsed is not None for parsed in parsed_stureg.values())}, "
            f"stureg={len(redis.stureg)}",
        ),
        (
            "Redis registration mirrors exactly match SQLite operations",
            set(redis.stureg) == set(db.registered_student_ids)
            and redis.stureg == expected_stureg,
            f"stureg={len(redis.stureg)}, db_students={len(db.registered_student_ids)}",
        ),
        (
            "successful response identities match SQLite",
            success_ids == set(db.registered_student_ids),
            f"success_ids={len(success_ids)}, db_students={len(db.registered_student_ids)}",
        ),
        (
            "no in-flight reservation remains after all responses",
            not redis.reservations,
            f"resv={len(redis.reservations)}",
        ),
        (
            "no student operation lease remains after all responses",
            not redis.operations,
            f"seat_ops={len(redis.operations)}",
        ),
    ]

    print("OMMS-STXK isolated registration stress result")
    print(
        f"load: seats={args.seats} users={args.users} "
        f"concurrency={args.concurrency} elapsed={elapsed_seconds:.3f}s "
        f"throughput={len(attempts) / elapsed_seconds:.1f} req/s"
    )
    print(
        "responses: "
        f"success={len(successes)} full={len(full)} other={len(other)} "
        f"http_errors={len(http_errors)} transport_errors={len(transport_errors)}"
    )
    if http_statuses:
        print(f"http error statuses: {dict(sorted(http_statuses.items()))}")
    if other:
        samples = [attempt.payload for attempt in other[:3]]
        print(f"unexpected business samples: {samples!r}")
    if transport_errors:
        samples = [attempt.error for attempt in transport_errors[:3]]
        print(f"transport error samples: {samples!r}")
    invalid_stureg = {
        student_id: raw
        for student_id, raw in redis.stureg.items()
        if parsed_stureg[student_id] is None
        or parsed_stureg[student_id][0] != CLUB_ID
        or expected_stureg.get(student_id) != raw
    }
    if invalid_stureg:
        print(f"stureg mismatch samples: {list(invalid_stureg.items())[:3]!r}")
    if redis.reservations:
        print(f"remaining reservation samples: {list(redis.reservations.items())[:3]!r}")
    if redis.operations:
        print(f"remaining operation samples: {list(redis.operations.items())[:3]!r}")
    print(
        "latency_ms: "
        f"p50={percentile(latencies, 0.50):.2f} "
        f"p95={percentile(latencies, 0.95):.2f} "
        f"p99={percentile(latencies, 0.99):.2f} "
        f"max={max(latencies, default=math.nan):.2f}"
    )
    print(
        "state: "
        f"db_count={db.registrations} current_students={db.current_students} "
        f"redis_stock={redis.stock} stureg={len(redis.stureg)} "
        f"resv={len(redis.reservations)} seat_ops={len(redis.operations)}"
    )
    print("invariants:")
    for name, passed, detail in checks:
        print(f"  [{'PASS' if passed else 'FAIL'}] {name} ({detail})")
    passed = all(check[1] for check in checks)
    print("result: PASS" if passed else "result: FAIL")
    return passed


def terminate_process(process: subprocess.Popen[bytes] | None) -> None:
    if process is None or process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=5.0)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5.0)


def tail_file(path: Path, lines: int = 30) -> str:
    try:
        return "\n".join(path.read_text(errors="replace").splitlines()[-lines:])
    except OSError:
        return "<log unavailable>"


def run(args: argparse.Namespace) -> int:
    rust_bin = args.rust_bin.expanduser().resolve()
    redis_server = shutil.which(args.redis_server) or args.redis_server
    if not rust_bin.is_file() or not os.access(rust_bin, os.X_OK):
        raise SetupError(
            f"Rust release is missing or not executable: {rust_bin}; "
            "build it with `cargo build --release --manifest-path club-hot/Cargo.toml`"
        )
    if shutil.which(redis_server) is None and not Path(redis_server).is_file():
        raise SetupError(f"redis-server executable not found: {args.redis_server}")

    redis_process: subprocess.Popen[bytes] | None = None
    rust_process: subprocess.Popen[bytes] | None = None
    rust_log_tail = ""
    redis_log_tail = ""
    with tempfile.TemporaryDirectory(prefix="omms-stress-") as temp_name:
        temp_dir = Path(temp_name)
        redis_dir = temp_dir / "redis"
        redis_dir.mkdir()
        db_path = temp_dir / "stress.db"
        rust_log_path = temp_dir / "club-hot.log"
        redis_log_path = temp_dir / "redis.log"
        create_database(db_path, users=args.users, seats=args.seats)

        redis_port = choose_loopback_port()
        rust_port = choose_loopback_port(forbidden={redis_port})
        if redis_port == FORBIDDEN_REDIS_PORT:
            raise SetupError("internal safety check selected forbidden Redis port 6379")

        print(
            f"isolated runtime: redis=127.0.0.1:{redis_port} "
            f"rust=127.0.0.1:{rust_port} temp={temp_dir}"
        )
        try:
            with redis_log_path.open("wb") as redis_log:
                redis_process = subprocess.Popen(
                    [
                        redis_server,
                        "--bind",
                        "127.0.0.1",
                        "--port",
                        str(redis_port),
                        "--protected-mode",
                        "yes",
                        "--save",
                        "",
                        "--appendonly",
                        "no",
                        "--dir",
                        str(redis_dir),
                        "--dbfilename",
                        "stress.rdb",
                        "--loglevel",
                        "warning",
                    ],
                    stdin=subprocess.DEVNULL,
                    stdout=redis_log,
                    stderr=subprocess.STDOUT,
                )
                wait_for_redis(redis_process, redis_port, args.startup_timeout)
                sessions = seed_sessions(redis_port, args.users)

                env = os.environ.copy()
                env.update(
                    {
                        "BIND": f"127.0.0.1:{rust_port}",
                        "DB_PATH": str(db_path),
                        "REDIS_URL": f"redis://127.0.0.1:{redis_port}/0",
                        "SESSION_TTL": str(SESSION_TTL_SECONDS),
                        "RESV_TTL": str(args.reservation_ttl),
                        "DB_POOL_SIZE": "12",
                        "MAX_CONCURRENCY": str(args.server_max_concurrency),
                        "REQUEST_TIMEOUT_SECS": str(args.server_request_timeout),
                        "RUST_LOG": "warn",
                    }
                )
                with rust_log_path.open("wb") as rust_log:
                    rust_process = subprocess.Popen(
                        [str(rust_bin)],
                        cwd=REPO_ROOT,
                        env=env,
                        stdin=subprocess.DEVNULL,
                        stdout=rust_log,
                        stderr=subprocess.STDOUT,
                    )
                    wait_for_rust(rust_process, rust_port, args.startup_timeout)
                    started = time.perf_counter()
                    attempts = asyncio.run(
                        run_spike(
                            rust_port,
                            sessions,
                            args.concurrency,
                            args.request_timeout,
                        )
                    )
                    elapsed = time.perf_counter() - started
                    db_state = read_database_state(db_path)
                    redis_state = read_redis_state(redis_port, args.users)
                    passed = validate_and_report(
                        args, attempts, db_state, redis_state, elapsed
                    )
                    return 0 if passed else 1
        except Exception:
            rust_log_tail = tail_file(rust_log_path)
            redis_log_tail = tail_file(redis_log_path)
            raise
        finally:
            terminate_process(rust_process)
            terminate_process(redis_process)
            if rust_log_tail:
                print("club-hot log tail:", file=sys.stderr)
                print(rust_log_tail, file=sys.stderr)
            if redis_log_tail:
                print("redis log tail:", file=sys.stderr)
                print(redis_log_tail, file=sys.stderr)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    try:
        return run(args)
    except (SetupError, RedisProtocolError, OSError, sqlite3.Error) as exc:
        print(f"stress setup/runtime error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
