#!/usr/bin/env python3
"""Unit tests for behaviour that matters on small, old servers.

``main`` is imported in-process with a Redis URL that points at an unused
loopback port and a temporary database path, so nothing here touches a real
Redis instance or the repository database.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock


REPO_ROOT = Path(__file__).resolve().parents[1]


def unused_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


TEMP = tempfile.TemporaryDirectory(prefix="omms-low-end-")
ISOLATED_ENV = {
    "REDIS_URL": f"redis://127.0.0.1:{unused_port()}/0",
    "REDIS_TIMEOUT": "0.2",
    "DB_PATH": str(Path(TEMP.name) / "unused.sqlite3"),
    "NGINX_ACCESS_LOG": str(Path(TEMP.name) / "nginx-access.log"),
    "LOG_LEVEL": "ERROR",
}
os.environ.update(ISOLATED_ENV)
sys.path.insert(0, str(REPO_ROOT))
import main  # noqa: E402


def tearDownModule() -> None:
    TEMP.cleanup()


class AuthQueueTests(unittest.TestCase):
    def test_queue_bounds_waiting_work(self) -> None:
        queue = main.AuthQueue(workers=1, limit=2)
        release = threading.Event()
        running = threading.Event()

        def slow() -> str:
            running.set()
            release.wait(5)
            return "first"

        first, _ = queue.submit(slow)
        self.assertTrue(running.wait(5))
        second, _ = queue.submit(lambda: "second")
        self.assertGreaterEqual(queue.admission(), 1)
        rejected, wait = queue.submit(lambda: "third")
        self.assertIsNone(rejected)
        self.assertGreaterEqual(wait, 1)
        busy = queue.run(lambda: "fourth")
        self.assertIsInstance(busy, main.AuthBusy)
        self.assertLessEqual(busy.wait, 30)
        forced, _ = queue.submit(lambda: "forced", force=True)
        self.assertIsNotNone(forced)
        release.set()
        self.assertEqual("first", first.result(timeout=5))
        self.assertEqual("second", second.result(timeout=5))
        self.assertEqual("forced", forced.result(timeout=5))
        self.assertEqual(0, queue.admission())
        self.assertEqual("again", queue.run(lambda: "again"))

    def test_runs_in_arrival_order_on_a_fixed_number_of_threads(self) -> None:
        queue = main.AuthQueue(workers=1, limit=50)
        order: list[int] = []
        threads: set[str] = set()

        def work(index: int) -> None:
            threads.add(threading.current_thread().name)
            order.append(index)

        futures = [queue.submit(work, index)[0] for index in range(20)]
        for future in futures:
            future.result(timeout=5)
        self.assertEqual(list(range(20)), order)
        self.assertEqual(1, len(threads))

    def test_timed_out_wait_releases_its_place(self) -> None:
        queue = main.AuthQueue(workers=1, limit=2)
        release = threading.Event()
        blocker, _ = queue.submit(release.wait, 5)
        original = main.AUTH_WAIT
        main.AUTH_WAIT = 0.05
        try:
            self.assertIsInstance(queue.run(lambda: "late"), main.AuthBusy)
        finally:
            main.AUTH_WAIT = original
        release.set()
        blocker.result(timeout=5)
        self.assertEqual(0, queue.admission())

    def test_turned_away_requests_get_spaced_return_times(self) -> None:
        # 每次 0.5 秒、一个线程：被拒的请求按 0.5 秒一个依次约定回来的时间。
        queue = main.AuthQueue(workers=1, limit=1)
        queue._seconds = 0.5
        release = threading.Event()
        blocker, _ = queue.submit(release.wait, 5)
        waits = [queue.admission() for _ in range(20)]
        self.assertEqual(sorted(waits), waits)
        self.assertEqual(2, waits[0])
        self.assertEqual(11, waits[-1])
        release.set()
        blocker.result(timeout=5)


class AccessLogTests(unittest.TestCase):
    def test_snapshot_reads_old_and_new_nginx_log_formats(self) -> None:
        now = time.time()
        lines = [
            f'ts={now:.3f} status=200 upstream="127.0.0.1:2002" request_time=0.004 limit_req=PASSED limit_conn=PASSED',
            f'ts={now:.3f} status=429 upstream="-" request_time=0.000 limit_req=REJECTED limit_conn=-',
            f'ts={now:.3f} status=200 upstream="127.0.0.1:2001" request_time=0.010',
            f'ts={now:.3f} status=429 upstream="-" request_time=0.000 ',
            f'ts={now - 3600:.3f} status=200 upstream="127.0.0.1:2001" request_time=0.010',
        ]
        log_path = Path(ISOLATED_ENV["NGINX_ACCESS_LOG"])
        log_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        with main.METRICS_CACHE_LOCK:
            main.METRICS_CACHE["payload"] = None
        # 其他测试文件可能先导入了 main，日志路径以模块属性为准。
        with mock.patch.object(main, "NGINX_ACCESS_LOG", str(log_path)):
            edge = main.operations_snapshot()["edge"]
        self.assertEqual(4, edge["requests"])
        self.assertEqual(2, edge["responses"]["429"])
        self.assertEqual(2, edge["limited"]["rate"])
        self.assertEqual({"rust": 1, "python_backup": 1, "unavailable": 2}, edge["upstream"])


class FootprintTests(unittest.TestCase):
    def test_import_defers_pinyin_and_imaging_libraries(self) -> None:
        env = os.environ.copy()
        env.update(ISOLATED_ENV)
        probe = (
            "import sys; sys.path.insert(0, {root!r}); import main, json; "
            "print(json.dumps(['pypinyin' in sys.modules, 'PIL' in sys.modules]))"
        ).format(root=str(REPO_ROOT))
        output = subprocess.run(
            [sys.executable, "-c", probe], env=env, capture_output=True, text=True, check=True, timeout=60
        ).stdout.strip().splitlines()[-1]
        self.assertEqual([False, False], json.loads(output))

    def test_usernames_use_the_single_character_table(self) -> None:
        if not main.HAS_PYPINYIN:
            self.skipTest("pypinyin unavailable")

        class Cursor:
            def execute(self, *_args: object) -> None:
                return None

            def fetchone(self) -> None:
                return None

        self.assertEqual("zhangsan", main.gen_username("张三", Cursor(), set()))
        self.assertEqual("true", os.environ.get("PYPINYIN_NO_PHRASES"))

    def test_busy_response_carries_a_code_and_message(self) -> None:
        body = json.loads(main.BUSY_BODY)
        self.assertEqual("queued", body["code"])
        self.assertEqual(main.MESSAGES["queued"], body["message"])


if __name__ == "__main__":
    unittest.main()
