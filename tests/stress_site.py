#!/usr/bin/env python3
"""Whole-site load test that behaves like real browsers.

Every run starts a private stack in a temporary directory: ``redis-server`` on
an ephemeral loopback port, a fresh SQLite database, ``main.py``, optionally
the Rust ``club-hot`` service, and nginx built from the repository's
``nginx.conf``.  Each virtual student sends its own ``X-Forwarded-For`` so the
per-IP limits apply per student, and follows the frontend's retry rules:

* ``login``  - everybody signs in at once; 503/429/network errors are retried
  after ``Retry-After`` for up to 120 seconds, like ``web/js/login.js``.
* ``hot``    - everybody registers for one club with ``--hot-seats`` places,
  retrying 202/429/5xx with the same ``request_id`` for 25 seconds.
* ``spread`` - everybody registers for a random club; almost all succeed.
* ``poll``   - everybody polls ``/api/seats`` every 2-2.8 seconds with
  ``If-None-Match`` for ``--poll-seconds``.

``--wrap`` prefixes every server command, which makes it possible to imitate a
slow machine, e.g. ``--wrap "taskpolicy -b"`` on macOS (efficiency cores at
background priority) or ``--wrap "taskset -c 0,1"`` on Linux.

The report lists outcomes, time until each student saw a final result, request
counts per status, CPU seconds and resident memory of every server process,
and, for registration scenarios, database and Redis consistency checks.
Exit status: 0 when every student finished and all checks passed, 1 otherwise,
2 when the environment could not be prepared.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path
import random
import secrets
import shlex
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time

REPO_ROOT = Path(__file__).resolve().parents[1]
RUST_BIN = REPO_ROOT / "club-hot" / "target" / "release" / "club-hot"
PREFIX = "omms:"
PASSWORD = "stress-site-password"
LOGIN_WINDOW = 120.0
REGISTER_WINDOW = 25.0


class SetupError(RuntimeError):
    pass


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        port = int(sock.getsockname()[1])
    if port == 6379:
        return free_port()
    return port


def http_ok(port: int, path: str) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=1) as sock:
            sock.sendall(f"GET {path} HTTP/1.1\r\nHost: 127.0.0.1\r\nConnection: close\r\n\r\n".encode())
            return b" 200 " in sock.makefile("rb").readline()
    except OSError:
        return False


def redis_cli(port: int, *args: str, stdin: str | None = None) -> str:
    result = subprocess.run(["redis-cli", "-p", str(port), *args], input=stdin,
                            capture_output=True, text=True, check=True)
    return result.stdout.strip()


def process_stats(pids: list[int]) -> tuple[float, int]:
    """Total CPU seconds and resident KiB of the given processes."""
    out = subprocess.run(["ps", "-o", "time=,rss=", "-p", ",".join(map(str, pids))],
                         capture_output=True, text=True).stdout
    cpu, rss = 0.0, 0
    for line in out.splitlines():
        parts = line.split()
        if len(parts) != 2:
            continue
        seconds = 0.0
        for chunk in parts[0].replace("-", ":").split(":"):
            seconds = seconds * 60 + float(chunk)
        cpu += seconds
        rss += int(parts[1])
    return cpu, rss


def children(pid: int) -> list[int]:
    out = subprocess.run(["pgrep", "-P", str(pid)], capture_output=True, text=True).stdout
    return [int(value) for value in out.split()]


def percentile(values: list[float], fraction: float) -> float:
    if not values:
        return float("nan")
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, round((len(ordered) - 1) * fraction))]


class Stack:
    def __init__(self, args: argparse.Namespace, temp: Path) -> None:
        self.args = args
        self.temp = temp
        self.db = temp / "site.db"
        self.redis_port = free_port()
        self.python_port = free_port()
        self.rust_port = free_port()
        self.nginx_port = free_port()
        self.wrap = shlex.split(args.wrap)
        self.processes: dict[str, subprocess.Popen[bytes]] = {}

    def env(self) -> dict[str, str]:
        env = os.environ.copy()
        env.update({
            "DB_PATH": str(self.db),
            "REDIS_URL": f"redis://127.0.0.1:{self.redis_port}/0",
            "REDIS_PREFIX": PREFIX,
            "HOST": "127.0.0.1",
            "PORT": str(self.python_port),
            "BIND": f"127.0.0.1:{self.rust_port}",
            "WEB_DIR": str(REPO_ROOT / "web"),
            "NGINX_ACCESS_LOG": str(self.temp / "nginx-access.log"),
            "LOG_LEVEL": "WARNING",
            "RUST_LOG": "warn",
        })
        for item in self.args.server_env:
            key, _, value = item.partition("=")
            env[key] = value
        return env

    def spawn(self, name: str, command: list[str]) -> None:
        log = (self.temp / f"{name}.log").open("wb")
        self.processes[name] = subprocess.Popen(self.wrap + command, cwd=REPO_ROOT, env=self.env(),
                                                stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT)

    def wait_http(self, name: str, port: int, path: str) -> None:
        deadline = time.monotonic() + self.args.startup_timeout
        while time.monotonic() < deadline:
            if self.processes[name].poll() is not None:
                break
            if http_ok(port, path):
                return
            time.sleep(0.2)
        tail = (self.temp / f"{name}.log").read_text(errors="replace")[-2000:]
        raise SetupError(f"{name} did not become ready:\n{tail}")

    def start(self) -> None:
        self.spawn("redis", ["redis-server", "--bind", "127.0.0.1", "--port", str(self.redis_port),
                             "--save", "", "--appendonly", "no", "--dir", str(self.temp),
                             "--maxmemory", "256mb", "--maxmemory-policy", "noeviction"])
        deadline = time.monotonic() + self.args.startup_timeout
        while True:
            try:
                if redis_cli(self.redis_port, "ping") == "PONG":
                    break
            except subprocess.CalledProcessError:
                pass
            if time.monotonic() > deadline:
                raise SetupError("redis-server did not start")
            time.sleep(0.05)
        subprocess.run([sys.executable, "-c", "import main; main.init_db()"], cwd=REPO_ROOT,
                       env=self.env(), check=True, capture_output=True)
        self.seed()
        redis_cli(self.redis_port, "flushall")
        self.spawn("python", [sys.executable, str(REPO_ROOT / "main.py")])
        self.wait_http("python", self.python_port, "/readyz")
        if self.args.backend == "rust":
            if not RUST_BIN.is_file():
                raise SetupError("build club-hot first: cargo build --release --manifest-path club-hot/Cargo.toml")
            self.spawn("rust", [str(RUST_BIN)])
            self.wait_http("rust", self.rust_port, "/readyz")
        self.start_nginx()

    def seed(self) -> None:
        from argon2 import PasswordHasher

        args = self.args
        password_hash = PasswordHasher(time_cost=2, memory_cost=19456, parallelism=1).hash(PASSWORD)
        others = max(1, args.clubs - 1)
        per_club = max(1, (args.users - args.hot_seats) // others + 2)
        conn = sqlite3.connect(self.db)
        try:
            conn.execute("BEGIN")
            conn.execute("INSERT INTO clubs (id, name, max_students) VALUES (1, 'hot-club', ?)", (args.hot_seats,))
            conn.executemany(
                "INSERT INTO clubs (id, name, max_students, description) VALUES (?, ?, ?, ?)",
                [(club, f"club-{club}", per_club, "weekly meeting " * 8) for club in range(2, args.clubs + 1)])
            grades = ["高一", "高二", "高三"]
            conn.executemany(
                "INSERT INTO students (id, name, class, grade, student_id, username, password) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                [(i, f"student-{i}", f"{grades[i % 3]}{i % 12 + 1}班", grades[i % 3], f"S{i:07d}",
                  f"s{i:05d}", password_hash) for i in range(1, args.users + 1)])
            conn.execute("UPDATE settings SET registration_start_time = '2000-01-01 00:00:00'")
            conn.commit()
        finally:
            conn.close()

    def start_nginx(self) -> None:
        conf = (REPO_ROOT / "nginx.conf").read_text(encoding="utf-8")
        conf = conf.replace("__APP_ROOT__", str(REPO_ROOT)).replace("__RUN_DIR__", str(self.temp))
        conf = conf.replace("worker_processes auto;", f"worker_processes {self.args.nginx_workers};")
        conf = conf.replace("listen 8080 backlog=2048;", f"listen {self.nginx_port} backlog=2048;\n        set_real_ip_from 127.0.0.1;\n"
                                            "        real_ip_header X-Forwarded-For;")
        if self.args.backend == "python":
            conf = conf.replace("        server 127.0.0.1:2002 max_fails=2 fail_timeout=5s;\n", "")
            conf = conf.replace("server 127.0.0.1:2001 backup;", "server 127.0.0.1:2001;")
            conf = conf.replace("        keepalive 64;\n", "")
        conf = conf.replace("127.0.0.1:2002", f"127.0.0.1:{self.rust_port}")
        conf = conf.replace("127.0.0.1:2001", f"127.0.0.1:{self.python_port}")
        path = self.temp / "nginx.conf"
        path.write_text(conf, encoding="utf-8")
        self.spawn("nginx", ["nginx", "-p", str(self.temp), "-c", str(path), "-g", "daemon off;"])
        self.wait_http("nginx", self.nginx_port, "/login.html")

    def stats(self) -> dict[str, tuple[float, int]]:
        result = {}
        for name, process in self.processes.items():
            pids = [process.pid] + children(process.pid)
            result[name] = process_stats(pids)
        return result

    def stop(self) -> None:
        for name in ("nginx", "rust", "python", "redis"):
            process = self.processes.get(name)
            if process is not None and process.poll() is None:
                process.terminate()
        for process in self.processes.values():
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()

    def sessions(self) -> dict[int, str]:
        """Create one Redis session per student, as a successful login would."""
        conn = sqlite3.connect(self.db)
        rows = conn.execute("SELECT id, name, class, grade, student_id FROM students ORDER BY id").fetchall()
        conn.close()
        commands, sessions = [], {}
        for student_id, name, klass, grade, number in rows:
            token = secrets.token_urlsafe(32)
            sessions[student_id] = token
            payload = json.dumps({"role": "student", "student_id": student_id, "name": name, "class": klass,
                                  "grade": grade, "student_no": number, "_session_epoch": 0,
                                  "_session_version": 1}, ensure_ascii=False, separators=(",", ":"))
            commands.append(f"SET {PREFIX}sessgen:ver:student:{student_id} 1")
            commands.append(f"SET {PREFIX}sess:{token} {shlex.quote(payload)} EX 3600")
        redis_cli(self.redis_port, stdin="\n".join(commands) + "\n")
        return sessions


async def request(port: int, method: str, path: str, *, body: object = None, token: str | None = None,
                  ip: str, headers: dict[str, str] | None = None) -> tuple[int, dict[str, str], object]:
    """One HTTP/1.1 request; returns (0, {}, None) on network failure."""
    try:
        reader, writer = await asyncio.wait_for(asyncio.open_connection("127.0.0.1", port), 30)
    except (OSError, asyncio.TimeoutError):
        return 0, {}, None
    try:
        data = json.dumps(body).encode() if body is not None else b""
        lines = [f"{method} {path} HTTP/1.1", "Host: 127.0.0.1", "Connection: close", f"X-Forwarded-For: {ip}"]
        if data:
            lines += ["Content-Type: application/json", f"Content-Length: {len(data)}", "X-Requested-With: XMLHttpRequest"]
        if token:
            lines.append(f"Cookie: session={token}")
        lines += [f"{key}: {value}" for key, value in (headers or {}).items()]
        writer.write(("\r\n".join(lines) + "\r\n\r\n").encode() + data)
        await writer.drain()
        raw = await asyncio.wait_for(reader.read(), 60)
    except (OSError, asyncio.TimeoutError):
        return 0, {}, None
    finally:
        writer.close()
    head, _, payload = raw.partition(b"\r\n\r\n")
    head_lines = head.decode("latin-1").split("\r\n")
    try:
        status = int(head_lines[0].split()[1])
    except (IndexError, ValueError):
        return 0, {}, None
    response_headers: dict[str, str] = {}
    for line in head_lines[1:]:
        key, _, value = line.partition(":")
        key = key.strip().lower()
        if key == "set-cookie" and value.strip().startswith("session="):
            response_headers["session"] = value.strip()[8:].split(";", 1)[0]
        response_headers.setdefault(key, value.strip())
    if "chunked" in response_headers.get("transfer-encoding", ""):
        chunks, rest = b"", payload
        while rest:
            size_line, _, rest = rest.partition(b"\r\n")
            size = int(size_line.split(b";")[0] or b"0", 16)
            if size == 0:
                break
            chunks, rest = chunks + rest[:size], rest[size + 2:]
        payload = chunks
    try:
        parsed = json.loads(payload) if payload else None
    except ValueError:
        parsed = None
    return status, response_headers, parsed


def retry_after(headers: dict[str, str]) -> float:
    try:
        value = int(headers.get("retry-after", ""))
    except ValueError:
        value = 1
    return min(max(value, 1), 30) + random.random() * 0.6


def student_ip(student_id: int) -> str:
    return f"10.{student_id // 250 % 250}.{student_id % 250}.7"


def label(status: int, payload: object) -> str:
    code = payload.get("code") if isinstance(payload, dict) else None
    return str(status) if status == 200 or not code else f"{status}:{code}"


async def run_login(port: int, users: int) -> dict[str, object]:
    start = asyncio.Event()
    statuses: dict[str, int] = {}

    async def student(student_id: int) -> tuple[bool, float]:
        await start.wait()
        began = time.perf_counter()
        while True:
            status, headers, payload = await request(
                port, "POST", "/api/login", ip=student_ip(student_id),
                body={"username": f"s{student_id:05d}", "password": PASSWORD})
            name = label(status, payload)
            statuses[name] = statuses.get(name, 0) + 1
            if status == 200 and isinstance(payload, dict) and payload.get("success"):
                return True, time.perf_counter() - began
            throttled = isinstance(payload, dict) and payload.get("code") == "login_throttled"
            waiting = status in (0, 503) or (status == 429 and not throttled)
            if not waiting or time.perf_counter() - began > LOGIN_WINDOW:
                return False, time.perf_counter() - began
            await asyncio.sleep(retry_after(headers))

    tasks = [asyncio.ensure_future(student(i)) for i in range(1, users + 1)]
    await asyncio.sleep(0.2)
    began = time.perf_counter()
    start.set()
    results = await asyncio.gather(*tasks)
    times = [elapsed for ok, elapsed in results if ok]
    return {
        "elapsed_s": round(time.perf_counter() - began, 1),
        "signed_in": len(times), "gave_up": users - len(times),
        "time_to_sign_in_s": {"p50": round(percentile(times, .5), 2), "p95": round(percentile(times, .95), 2),
                              "max": round(max(times), 2) if times else None},
        "requests": sum(statuses.values()), "statuses": statuses,
        "_finished": len(times) == users,
    }


async def run_register(port: int, sessions: dict[int, str], clubs: list[int], hot: bool) -> dict[str, object]:
    start = asyncio.Event()
    statuses: dict[str, int] = {}
    chooser = random.Random(7)

    async def student(student_id: int, token: str) -> tuple[str, float]:
        club = 1 if hot else chooser.choice(clubs)
        request_id = secrets.token_hex(12)
        await start.wait()
        began = time.perf_counter()
        while True:
            status, headers, payload = await request(
                port, "POST", "/api/register_club", token=token, ip=student_ip(student_id),
                body={"club_id": club, "request_id": request_id})
            name = label(status, payload)
            statuses[name] = statuses.get(name, 0) + 1
            if status == 200 and isinstance(payload, dict):
                return str(payload.get("code")), time.perf_counter() - began
            if status in (0, 202, 429) or status >= 500:
                if time.perf_counter() - began > REGISTER_WINDOW:
                    return "gave_up", time.perf_counter() - began
                await asyncio.sleep(retry_after(headers))
                continue
            return f"http_{status}", time.perf_counter() - began

    tasks = [asyncio.ensure_future(student(sid, token)) for sid, token in sessions.items()]
    await asyncio.sleep(0.2)
    began = time.perf_counter()
    start.set()
    results = await asyncio.gather(*tasks)
    outcomes: dict[str, int] = {}
    for outcome, _elapsed in results:
        outcomes[outcome] = outcomes.get(outcome, 0) + 1
    times = [elapsed for _outcome, elapsed in results]
    return {
        "elapsed_s": round(time.perf_counter() - began, 1), "outcomes": outcomes,
        "time_to_result_s": {"p50": round(percentile(times, .5), 2), "p95": round(percentile(times, .95), 2),
                             "p99": round(percentile(times, .99), 2), "max": round(max(times), 2)},
        "requests": sum(statuses.values()), "statuses": statuses,
        "_finished": set(outcomes) <= {"registered", "full"},
    }


async def run_poll(port: int, sessions: dict[int, str], seconds: float) -> dict[str, object]:
    stop = time.perf_counter() + seconds
    latencies: list[float] = []
    statuses: dict[str, int] = {}

    async def student(student_id: int, token: str) -> None:
        etag = None
        await asyncio.sleep(random.random() * 2)
        while time.perf_counter() < stop:
            began = time.perf_counter()
            status, headers, payload = await request(
                port, "GET", "/api/seats", token=token, ip=student_ip(student_id),
                headers={"If-None-Match": etag} if etag else None)
            latencies.append(time.perf_counter() - began)
            name = label(status, payload)
            statuses[name] = statuses.get(name, 0) + 1
            if status == 200:
                etag = headers.get("etag")
            await asyncio.sleep(2 + random.random() * 0.8)

    await asyncio.gather(*(student(sid, token) for sid, token in sessions.items()))
    ok = sum(count for name, count in statuses.items() if name in ("200", "304"))
    return {"requests": len(latencies), "statuses": statuses,
            "latency_ms": {"p50": round(percentile(latencies, .5) * 1000, 1),
                           "p95": round(percentile(latencies, .95) * 1000, 1),
                           "p99": round(percentile(latencies, .99) * 1000, 1)},
            "_finished": ok == len(latencies)}


def consistency(stack: Stack) -> dict[str, int]:
    conn = sqlite3.connect(stack.db)
    try:
        over = conn.execute("SELECT COUNT(*) FROM clubs c WHERE "
                            "(SELECT COUNT(*) FROM registrations r WHERE r.club_id = c.id) > c.max_students").fetchone()[0]
        duplicates = conn.execute("SELECT COUNT(*) - COUNT(DISTINCT student_id) FROM registrations").fetchone()[0]
        left = conn.execute("SELECT id, max_students - (SELECT COUNT(*) FROM registrations r "
                            "WHERE r.club_id = clubs.id) FROM clubs").fetchall()
        total = conn.execute("SELECT COUNT(*) FROM registrations").fetchone()[0]
    finally:
        conn.close()
    mismatched = sum(redis_cli(stack.redis_port, "get", f"{PREFIX}stock:{club}") != str(seats)
                     for club, seats in left)
    leases = redis_cli(stack.redis_port, "--scan", "--pattern", f"{PREFIX}op:*").split()
    return {"registrations": total, "over_capacity_clubs": over, "duplicate_students": duplicates,
            "redis_stock_mismatch_clubs": mismatched, "leftover_leases": len(leases)}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("scenario", choices=["login", "hot", "spread", "poll"])
    parser.add_argument("--backend", choices=["python", "rust"], default="python")
    parser.add_argument("--users", type=int, default=1000)
    parser.add_argument("--clubs", type=int, default=20)
    parser.add_argument("--hot-seats", type=int, default=100)
    parser.add_argument("--poll-seconds", type=float, default=30.0)
    parser.add_argument("--nginx-workers", type=int, default=2)
    parser.add_argument("--wrap", default="", help='prefix for server commands, e.g. "taskpolicy -b"')
    parser.add_argument("--server-env", action="append", default=[], metavar="KEY=VALUE")
    parser.add_argument("--startup-timeout", type=float, default=90.0)
    parser.add_argument("--keep", action="store_true", help="keep the temporary directory with service logs")
    args = parser.parse_args()
    for tool in ("redis-server", "redis-cli", "nginx"):
        if shutil.which(tool) is None:
            print(f"missing {tool}", file=sys.stderr)
            return 2

    temp_name = tempfile.mkdtemp(prefix="omms-site-")
    try:
        stack = Stack(args, Path(temp_name))
        try:
            stack.start()
            idle = stack.stats()
            before = stack.stats()
            if args.scenario == "login":
                report = asyncio.run(run_login(stack.nginx_port, args.users))
            elif args.scenario == "poll":
                report = asyncio.run(run_poll(stack.nginx_port, stack.sessions(), args.poll_seconds))
            else:
                clubs = list(range(2, args.clubs + 1)) or [1]
                report = asyncio.run(run_register(stack.nginx_port, stack.sessions(), clubs,
                                                  hot=args.scenario == "hot"))
                time.sleep(2)
                report["consistency"] = consistency(stack)
            after = stack.stats()
        except (SetupError, subprocess.CalledProcessError, OSError) as exc:
            print(f"setup failed: {exc}", file=sys.stderr)
            return 2
        finally:
            stack.stop()
    finally:
        if args.keep:
            print(f"logs kept in {temp_name}", file=sys.stderr)
        else:
            shutil.rmtree(temp_name, ignore_errors=True)

    report["cpu_seconds"] = {name: round(after[name][0] - before[name][0], 2) for name in after}
    report["memory_mib"] = {name: {"idle": round(idle[name][1] / 1024, 1), "after": round(after[name][1] / 1024, 1)}
                            for name in after}
    finished = report.pop("_finished")
    checks = report.get("consistency", {})
    passed = finished and not any(checks.get(key) for key in
                                  ("over_capacity_clubs", "duplicate_students",
                                   "redis_stock_mismatch_clubs", "leftover_leases"))
    print(json.dumps({"scenario": args.scenario, "backend": args.backend, "users": args.users,
                      "wrap": args.wrap, **report}, ensure_ascii=False, indent=2))
    print("result: PASS" if passed else "result: FAIL")
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
