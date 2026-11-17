#!/usr/bin/env python3
"""The background scan reads in-flight members and their leases in two round
trips.  An operation confirmed between the two reads must not be reported as
stale, otherwise the scan fences a busy club for a repair it does not need.

``main`` is imported in-process with a Redis URL that points at an unused
loopback port and a temporary database path; Redis is replaced by a fake that
lets the test run code between the two reads.
"""

from __future__ import annotations

import os
from pathlib import Path
import socket
import sys
import tempfile
import unittest


REPO_ROOT = Path(__file__).resolve().parents[1]


def unused_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


TEMP = tempfile.TemporaryDirectory(prefix="omms-inflight-")
os.environ.update({
    "REDIS_URL": f"redis://127.0.0.1:{unused_port()}/0",
    "REDIS_TIMEOUT": "0.2",
    "DB_PATH": str(Path(TEMP.name) / "unused.sqlite3"),
    "LOG_LEVEL": "ERROR",
})
sys.path.insert(0, str(REPO_ROOT))
import main  # noqa: E402


def tearDownModule() -> None:
    TEMP.cleanup()


class FakeRedis:
    """Just enough of redis-py for ``RedisGate.inflight_state``."""

    def __init__(self, members: set[str], leases: dict[str, str]) -> None:
        self.members = set(members)
        self.leases = dict(leases)
        self.between_reads = None

    def smembers(self, _key: str) -> set[str]:
        return set(self.members)

    def mget(self, keys: list[str]) -> list[str | None]:
        if self.between_reads:
            self.between_reads(self)
            self.between_reads = None
        return [self.leases.get(key) for key in keys]

    def pipeline(self, transaction: bool = False) -> "FakePipeline":
        return FakePipeline(self)


class FakePipeline:
    def __init__(self, redis: FakeRedis) -> None:
        self.redis = redis
        self.queued: list[str] = []

    def sismember(self, _key: str, member: str) -> None:
        self.queued.append(member)

    def execute(self) -> list[bool]:
        return [member in self.redis.members for member in self.queued]


def gate(fake: FakeRedis) -> main.RedisGate:
    instance = main.RedisGate.__new__(main.RedisGate)
    instance._r = fake
    instance._scripts = {}
    return instance


class InflightScanTests(unittest.TestCase):
    def test_operation_confirmed_between_reads_is_not_stale(self) -> None:
        fake = FakeRedis({"7|lease-a", "8|lease-b"}, {main.k_op(7): "lease-a", main.k_op(8): "lease-b"})

        def confirm_student_8(redis: FakeRedis) -> None:
            redis.members.discard("8|lease-b")
            redis.leases.pop(main.k_op(8))

        fake.between_reads = confirm_student_8
        self.assertEqual((1, 0), gate(fake).inflight_state(1))

    def test_member_without_its_lease_is_stale(self) -> None:
        fake = FakeRedis({"7|lease-a", "8|lease-b"}, {main.k_op(7): "lease-a"})
        self.assertEqual((1, 1), gate(fake).inflight_state(1))

    def test_member_with_a_replaced_lease_is_stale(self) -> None:
        fake = FakeRedis({"7|lease-a"}, {main.k_op(7): "lease-new"})
        self.assertEqual((0, 1), gate(fake).inflight_state(1))


if __name__ == "__main__":
    unittest.main()
