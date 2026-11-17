#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import collections
import http.server
import random
import socketserver
import json
import sqlite3
import sys
import tempfile
import urllib.parse
import os
import secrets
import io
import csv
import queue
import concurrent.futures
import contextlib
import threading
import logging
import time
import ipaddress
import re
import hashlib
import importlib.util
import warnings
import zipfile
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import netinfo  # noqa: E402
import sheets as tables  # noqa: E402

# ---- 可选重依赖(有 fallback 不致命) -------------------------------------
try:
    import redis as _redis
except ImportError:
    _redis = None

try:
    from argon2 import PasswordHasher
    from argon2.exceptions import VerifyMismatchError, InvalidHashError
except ImportError:  # pragma: no cover
    PasswordHasher = None

# pypinyin 与 Pillow 在导入学生、上传图片时才加载，常驻内存少约 55 MB。
HAS_PYPINYIN = importlib.util.find_spec("pypinyin") is not None
HAS_PILLOW = importlib.util.find_spec("PIL") is not None

# ---- 配置(环境变量可覆盖) ------------------------------------------------
DB_PATH = os.environ.get("DB_PATH", "club_system.db")
REDIS_URL = os.environ.get("REDIS_URL", "redis://127.0.0.1:6379/0")
REDIS_PREFIX = os.environ.get("REDIS_PREFIX", "omms:")
REDIS_TIMEOUT = max(0.2, float(os.environ.get("REDIS_TIMEOUT", "2")))
HOST = os.environ.get("HOST", "127.0.0.1")   # 默认仅本机，公网经 nginx 反代
PORT = int(os.environ.get("PORT", "2001"))
CPU_COUNT = os.cpu_count() or 1
POOL_SIZE = max(1, int(os.environ.get("DB_POOL_SIZE", "12")))
SQLITE_SYNCHRONOUS = os.environ.get("SQLITE_SYNCHRONOUS", "NORMAL").strip().upper()
if SQLITE_SYNCHRONOUS not in {"OFF", "NORMAL", "FULL", "EXTRA"}:
    SQLITE_SYNCHRONOUS = "NORMAL"
SESSION_TTL = max(60, int(os.environ.get("SESSION_TTL", str(8 * 3600))))
LOGIN_MAX_FAILS = max(1, int(os.environ.get("LOGIN_MAX_FAILS", "10")))
LOGIN_WINDOW = 60
DEVICE_TTL = 180 * 24 * 3600
MAX_BODY = max(1024, int(os.environ.get("MAX_BODY", str(8 * 1024 * 1024))))
# 口令校验线程：核数的一半，不少于 4、不多于 8，核数更少时取核数；另一半核留给 nginx、Redis 和报名。
AUTH_CONCURRENCY = max(1, int(os.environ.get("AUTH_CONCURRENCY", str(min(CPU_COUNT, 8, max(4, CPU_COUNT // 2))))))
AUTH_WAIT = 10.0
ARGON2_TIME_COST = max(1, int(os.environ.get("ARGON2_TIME_COST", "2")))
ARGON2_MEMORY_KIB = max(8192, int(os.environ.get("ARGON2_MEMORY_KIB", "19456")))
ARGON2_PARALLELISM = max(1, int(os.environ.get("ARGON2_PARALLELISM", "1")))
REGISTER_CONCURRENCY = max(1, int(os.environ.get("REGISTER_CONCURRENCY", "1")))
REGISTER_QUEUE_TIMEOUT = max(0.1, float(os.environ.get("REGISTER_QUEUE_TIMEOUT", "10")))
MAX_HTTP_WORKERS = max(8, int(os.environ.get("MAX_HTTP_WORKERS", "128")))
# 排队等口令校验的请求各占一个线程，最多用掉一半线程。
AUTH_QUEUE = max(AUTH_CONCURRENCY, int(os.environ.get(
    "AUTH_QUEUE", str(min(AUTH_CONCURRENCY * 16, max(AUTH_CONCURRENCY, MAX_HTTP_WORKERS // 2))))))
SEAT_OP_TTL = max(5, int(os.environ.get("SEAT_OP_TTL", "30")))
MAINTENANCE_TTL = 300
FENCE_TTL = 30
FENCE_WAIT = 5.0
SESSION_MUTATION_TTL = 60
SEATS_CACHE_SECONDS = max(0.0, float(os.environ.get("SEATS_CACHE_MS", "500")) / 1000)
CATALOG_CACHE_SECONDS = 1.0
REPAIR_INTERVAL = 1.0
ORPHAN_SCAN_INTERVAL = 10.0
MAX_USERNAME = 80
MAX_PASSWORD = 256
MAX_IMPORT_STUDENTS = max(1, int(os.environ.get("MAX_IMPORT_STUDENTS", "500")))
MAX_CLUB_CAPACITY = 10_000
CLUB_IMAGE_DIR = os.path.abspath(os.environ.get(
    "CLUB_IMAGE_DIR", os.path.join(os.path.dirname(os.path.abspath(DB_PATH)), "club-images"),
))
MAX_CLUB_IMAGE_BYTES = 10 * 1024 * 1024
MAX_CLUB_IMAGE_OUTPUT_BYTES = 4 * 1024 * 1024
MAX_CLUB_IMAGE_PIXELS = 50_000_000
CLUB_IMAGE_SIDE = 960           # 照片长边缩到 960 像素，卡片在 2 倍屏上仍然清晰
MAX_CLUB_IMAGE_WIDTH = 1920     # GIF 不缩放，保持原有尺寸上限
MAX_CLUB_IMAGE_HEIGHT = 1080
MAX_CLUB_GIF_FRAMES = 120
MAX_CLUB_GIF_DURATION_MS = 30_000
MAX_CLUB_GIF_DECODED_PIXELS = 12_000_000
IMAGE_PROCESS_SLOTS = threading.BoundedSemaphore(1)
MAX_TABLE_BYTES = 10 * 1024 * 1024
MAX_RESTORE_BYTES = 64 * 1024 * 1024
MAX_ACCOUNT_ROWS = 5000
ADMIN_LOGIN_TTL = 120
ADMIN_SECRET_FILE = os.path.abspath(os.environ.get(
    "ADMIN_SECRET_FILE", os.path.join(os.path.dirname(os.path.abspath(DB_PATH)), "admin-password.txt"),
))
XLSX_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
COOKIE_SECURE = os.environ.get("COOKIE_SECURE", "0").strip().lower() in {"1", "true", "yes", "on"}
NGINX_ACCESS_LOG = os.environ.get("NGINX_ACCESS_LOG", "/tmp/club_nginx_access.log")
METRICS_WINDOW_SECONDS = 60
METRICS_CACHE_SECONDS = 1.0
SHARED_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "shared")
REQUEST_ID_RE = re.compile(r"[A-Za-z0-9_-]{8,64}")
DEVICE_RE = re.compile(r"[A-Za-z0-9_-]{20,64}")


def _parse_utc_offset(text):
    match = re.fullmatch(r"([+-])(\d{2}):?(\d{2})", (text or "").strip())
    if not match:
        return timezone(timedelta(hours=8))
    sign = 1 if match.group(1) == "+" else -1
    return timezone(sign * timedelta(hours=int(match.group(2)), minutes=int(match.group(3))))


# 报名时间按学校所在时区解释，与服务器系统时区无关。
APP_TZ = _parse_utc_offset(os.environ.get("APP_UTC_OFFSET", "+08:00"))
TIME_FORMAT = "%Y-%m-%d %H:%M:%S"


def now_text():
    return datetime.now(APP_TZ).strftime(TIME_FORMAT)


def parse_local_time(text):
    return int(datetime.strptime(text, TIME_FORMAT).replace(tzinfo=APP_TZ).timestamp())


def format_local_time(epoch):
    return datetime.fromtimestamp(epoch, APP_TZ).strftime(TIME_FORMAT)


logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("club")

# nginx 1.17.6 之前没有 limit_req/limit_conn 字段，run.sh 会去掉它们。
_ACCESS_LOG_RE = re.compile(
    r"^ts=(?P<ts>\d+(?:\.\d+)?)\s+status=(?P<status>\d{3})\s+"
    r"upstream=\"(?P<upstream>[^\"]*)\"\s+request_time=(?P<request_time>\S+)"
    r"(?:\s+limit_req=(?P<limit_req>\S+)\s+limit_conn=(?P<limit_conn>\S+))?\s*$"
)

with open(os.path.join(SHARED_DIR, "messages.json"), encoding="utf-8") as _messages_file:
    MESSAGES = json.load(_messages_file)


def _load_lua(name):
    with open(os.path.join(SHARED_DIR, "lua", name + ".lua"), encoding="utf-8") as fh:
        return fh.read()


# ---- Redis 键(与 Rust 热服务共用) ----------------------------------------
def rk(*parts):
    return REDIS_PREFIX + ":".join(str(part) for part in parts)


K_OPEN_AT = rk("open_at")      # 报名开放 epoch 秒
K_CLOSE_AT = rk("close_at")    # 截止 epoch 秒，到点后报名与退选停止
K_LOCKED = rk("locked")        # "1" 表示名单已锁定
K_INIT = rk("init")            # 整体发布完成的标记
K_MAINT = rk("maint")          # 全局维护锁(清空类操作与冷启动)
K_REPAIR = rk("repair")        # 待修复的社团 id 集合


def k_stock(club_id):
    return rk("stock", club_id)


def k_reg(student_id):
    return rk("reg", student_id)


def k_op(student_id):
    return rk("op", student_id)


def k_inflight(club_id):
    return rk("inflight", club_id)


def k_fence(club_id):
    return rk("fence", club_id)


def k_rollback(lease):
    return rk("rb", lease)


def k_cancel(event_id):
    return rk("cx", event_id)


def k_sess(token):
    return rk("sess", token)


def k_sess_epoch(role):
    return rk("sessgen", "epoch", role)


def k_sess_version(role, principal):
    return rk("sessgen", "ver", role, principal)


def k_sess_role_mutation(role):
    return rk("sessmut", "role", role)


def k_sess_principal_mutation(role, principal):
    return rk("sessmut", role, principal)


def k_fail(key):
    return rk("fail", key)


def k_trust(role, username, device):
    return rk("trust", role, username, device)


def reservation_mirror(club_id, operation_id):
    return "{}|{}".format(int(club_id), operation_id) if operation_id else str(int(club_id))


def _session_identity(payload):
    """会话主体：学生用数据库主键，管理员用用户名。"""
    if not isinstance(payload, dict):
        return None
    role = payload.get("role")
    if role == "student":
        student_id = payload.get("student_id")
        if type(student_id) is int and student_id > 0:
            return role, str(student_id)
    elif role == "admin":
        username = payload.get("username")
        if isinstance(username, str) and username and len(username) <= MAX_USERNAME:
            return role, username
    return None


def _counter_value(value):
    try:
        parsed = int(value) if value is not None else 0
    except (TypeError, ValueError):
        raise RuntimeError("invalid session generation")
    if parsed < 0:
        raise RuntimeError("invalid session generation")
    return parsed


def _cookie_value(raw, name):
    """只接受唯一的同名 Cookie，Python 与 Rust 判定一致。"""
    if not raw:
        return None
    found = None
    for part in raw.split(";"):
        if "=" not in part:
            continue
        key, value = part.split("=", 1)
        if key.strip() != name:
            continue
        value = value.strip()
        if found is not None or not value:
            return None
        found = value
    return found


def _session_cookie_token(raw):
    return _cookie_value(raw, "session")


def _single_cookie_header(headers):
    values = headers.get_all("Cookie") or []
    return values[0] if len(values) == 1 else None


def _session_cookie_token_from_headers(headers):
    return _session_cookie_token(_single_cookie_header(headers))


# ==========================================================================
# Redis 名额与会话
# ==========================================================================
class RedisGate:
    SCRIPT_NAMES = ("acquire", "confirm", "rollback", "cancel_begin", "cancel_release",
                    "lease_end", "fence_publish", "mirror_set", "session_create", "login_fail")

    def __init__(self, url):
        self._r = None
        self._scripts = {}
        if _redis is None:
            log.warning("未安装 redis 模块，登录与报名需要它")
            return
        # 客户端与脚本在启动时建好，连接池在 Redis 可用后自动重连。
        self._r = _redis.Redis.from_url(
            url, decode_responses=True,
            socket_timeout=REDIS_TIMEOUT, socket_connect_timeout=REDIS_TIMEOUT,
        )
        for name in self.SCRIPT_NAMES:
            self._scripts[name] = self._r.register_script(_load_lua(name))
        if self.alive():
            log.info("Redis 已连接: %s", url)
        else:
            log.warning("Redis 暂未就绪，恢复后自动连接: %s", url)

    @property
    def r(self):
        return self._r

    def alive(self):
        if self._r is None:
            return False
        try:
            return bool(self._r.ping())
        except Exception:  # noqa: BLE001
            return False

    def _run(self, name, keys, args, attempts=2):
        if self._r is None:
            raise RuntimeError("redis unavailable")
        last_error = None
        for _ in range(attempts):
            try:
                return self._scripts[name](keys=keys, args=args)
            except Exception as e:  # noqa: BLE001
                last_error = e
        raise RuntimeError("redis unavailable") from last_error

    def _call(self, name, *args, **kwargs):
        if self._r is None:
            raise RuntimeError("redis unavailable")
        try:
            return getattr(self._r, name)(*args, **kwargs)
        except Exception as e:  # noqa: BLE001
            raise RuntimeError("redis unavailable") from e

    # ---- 抢位、确认、撤销 ----
    def acquire(self, student_id, club_id, lease, member, idem_prefix):
        """返回 acquire.lua 的结果码；本次请求的重放(2)按抢到(1)处理。"""
        keys = [k_stock(club_id), k_reg(student_id), k_op(student_id), K_OPEN_AT, K_MAINT,
                K_LOCKED, k_fence(club_id), k_inflight(club_id), K_CLOSE_AT]
        args = [lease, SEAT_OP_TTL, member, idem_prefix]
        code = int(self._run("acquire", keys, args))
        return 1 if code == 2 else code

    def confirm(self, student_id, club_id, lease, mirror, member):
        return int(self._run("confirm", [k_op(student_id), k_reg(student_id), k_inflight(club_id)],
                             [lease, mirror, member])) == 1

    def rollback(self, student_id, club_id, lease, member, absorb=False):
        code = int(self._run(
            "rollback",
            [k_stock(club_id), k_op(student_id), k_inflight(club_id), k_rollback(lease)],
            [lease, member, "absorb" if absorb else "return"],
        ))
        if code == 2:
            self.request_repair(club_id)
        if code < 0:
            raise RuntimeError("stock missing")
        return code

    def cancel_begin(self, student_id, club_id, lease, member):
        return int(self._run(
            "cancel_begin",
            [K_MAINT, k_fence(club_id), k_op(student_id), k_inflight(club_id), K_LOCKED, K_CLOSE_AT],
            [lease, SEAT_OP_TTL, member],
        ))

    def cancel_release(self, student_id, club_id, mirror, lease, member, event_id):
        code = int(self._run(
            "cancel_release",
            [k_stock(club_id), k_reg(student_id), k_op(student_id), k_inflight(club_id),
             k_cancel(event_id)],
            [mirror, str(int(club_id)), lease, member],
        ))
        if code == 2:
            self.request_repair(club_id)
        if code < 0:
            raise RuntimeError("stock missing")
        return code

    def lease_end(self, key, value, set_key=None, member=None):
        keys = [key] + ([set_key] if set_key else [])
        args = [value] + ([member] if set_key else [])
        try:
            self._run("lease_end", keys, args)
        except RuntimeError:
            pass

    def student_lease_begin(self, student_id, lease):
        return bool(self._call("set", k_op(student_id), lease, nx=True, ex=SEAT_OP_TTL))

    def lease_value(self, student_id):
        return self._call("get", k_op(student_id))

    def mirror_value(self, student_id):
        return self._call("get", k_reg(student_id))

    def mirror_set(self, student_id, mode, value):
        return int(self._run("mirror_set", [k_reg(student_id)], [mode, value]))

    def mirror_write(self, student_id, value):
        if value:
            self._call("set", k_reg(student_id), value)
        else:
            self._call("delete", k_reg(student_id))

    # ---- 单社团修复 ----
    def request_repair(self, club_id):
        try:
            self._r.sadd(K_REPAIR, int(club_id))
        except Exception as e:  # noqa: BLE001
            log.warning("登记待修复社团失败 club=%s: %s", club_id, e)

    def pending_repairs(self):
        try:
            return {int(value) for value in self._r.smembers(K_REPAIR)}
        except Exception:  # noqa: BLE001
            return set()

    def repair_done(self, club_id):
        try:
            self._r.srem(K_REPAIR, int(club_id))
        except Exception:  # noqa: BLE001
            pass

    def inflight_state(self, club_id):
        """返回 (仍持有租约的在途成员数, 已失效的成员数)。"""
        key = k_inflight(club_id)
        members = list(self._call("smembers", key))
        if not members:
            return 0, 0
        parsed = [(member.partition("|")[0], member.partition("|")[2]) for member in members]
        current = self._call("mget", [k_op(student) for student, _lease in parsed])
        stale = [member for member, (_student, lease), value in zip(members, parsed, current) if value != lease]
        live = len(members) - len(stale)
        if stale:
            # 两次读取之间刚确认或撤销的操作已经移出集合，仍在集合里的才算失效。
            try:
                pipe = self._r.pipeline(transaction=False)
                for member in stale:
                    pipe.sismember(key, member)
                stale = [member for member, present in zip(stale, pipe.execute()) if present]
            except Exception as e:  # noqa: BLE001
                raise RuntimeError("redis unavailable") from e
        return live, len(stale)

    def fence_acquire(self, club_id, wait=FENCE_WAIT):
        """取得社团栅栏并等该社团的在途操作完成；超时则放弃。"""
        token = secrets.token_urlsafe(12)
        try:
            if not self._r.set(k_fence(club_id), token, nx=True, ex=FENCE_TTL):
                return None
            deadline = time.monotonic() + wait
            while True:
                live, _dead = self.inflight_state(club_id)
                if live == 0:
                    return token
                if time.monotonic() >= deadline:
                    break
                time.sleep(0.05)
        except Exception as e:  # noqa: BLE001
            log.warning("取得社团栅栏失败 club=%s: %s", club_id, e)
        self.fence_release(club_id, token)
        return None

    def fence_release(self, club_id, token):
        self.lease_end(k_fence(club_id), token)

    def fence_publish(self, club_id, token, stock, mirrors):
        keys = [k_fence(club_id), k_stock(club_id), k_inflight(club_id)]
        args = [token, int(stock)]
        for student_id, value in sorted(mirrors.items()):
            keys.append(k_reg(student_id))
            args.append(value or "")
        return int(self._run("fence_publish", keys, args)) == 1

    def drop_club(self, club_id, token):
        if self._call("get", k_fence(club_id)) == token:
            self._call("delete", k_stock(club_id), k_inflight(club_id), k_fence(club_id))

    # ---- 全局维护(冷启动与清空类操作) ----
    def maintenance_begin(self, wait=FENCE_WAIT):
        if self._r is None:
            return None
        token = secrets.token_urlsafe(18)
        try:
            if not self._r.set(K_MAINT, token, nx=True, ex=MAINTENANCE_TTL):
                return None
            deadline = time.monotonic() + wait
            while self.has_active_operations():
                if time.monotonic() >= deadline:
                    self.maintenance_end(token)
                    return None
                time.sleep(0.05)
            return token
        except Exception:  # noqa: BLE001
            self.maintenance_end(token)
            return None

    def maintenance_end(self, token):
        if token:
            self.lease_end(K_MAINT, token)

    def has_active_operations(self):
        return next(self._r.scan_iter(match=rk("op", "*"), count=1000), None) is not None

    def publish_all(self, token, stocks, mirrors, open_at, locked, close_at=None):
        """持有全局维护锁时整体发布库存、镜像、开放与截止时间、锁定状态。"""
        stale = []
        for name in ("stock", "reg", "inflight", "fence"):
            stale.extend(self._r.scan_iter(match=rk(name, "*"), count=1000))
        pipe = self._r.pipeline()
        try:
            pipe.watch(K_MAINT)
            if pipe.get(K_MAINT) != token:
                raise RuntimeError("maintenance changed")
            pipe.multi()
            if stale:
                pipe.delete(*stale)
            for club_id, remaining in stocks.items():
                pipe.set(k_stock(club_id), int(remaining))
            for student_id, value in mirrors.items():
                pipe.set(k_reg(student_id), value)
            if open_at is None:
                pipe.delete(K_OPEN_AT)
            else:
                pipe.set(K_OPEN_AT, int(open_at))
            if close_at is None:
                pipe.delete(K_CLOSE_AT)
            else:
                pipe.set(K_CLOSE_AT, int(close_at))
            if locked:
                pipe.set(K_LOCKED, "1")
            else:
                pipe.delete(K_LOCKED)
            pipe.delete(K_REPAIR)
            pipe.set(K_INIT, "1")
            pipe.execute()
        finally:
            pipe.reset()

    def stock_left(self, club_ids):
        """批量读取剩余名额 {club_id: int|None}；Redis 不可用时返回 None。"""
        if self._r is None or not club_ids:
            return {} if club_ids == [] else None
        try:
            values = self._r.mget([k_stock(club_id) for club_id in club_ids])
            return {club_id: (int(value) if value is not None else None)
                    for club_id, value in zip(club_ids, values)}
        except Exception:  # noqa: BLE001
            return None

    def now_epoch_ms(self):
        """统一时钟：优先 Redis TIME，回落本机。"""
        if self._r is not None:
            try:
                sec, usec = self._r.time()
                return int(sec) * 1000 + int(usec) // 1000
            except Exception:  # noqa: BLE001
                pass
        return int(time.time() * 1000)

    # ---- 开放时间与锁定 ----
    def open_at_get(self):
        try:
            value = self._r.get(K_OPEN_AT)
            return int(value) if value is not None else None
        except Exception:  # noqa: BLE001
            return None

    def open_at_set(self, epoch):
        if epoch is None:
            self._call("delete", K_OPEN_AT)
        else:
            self._call("set", K_OPEN_AT, int(epoch))

    def close_at_get(self):
        try:
            value = self._r.get(K_CLOSE_AT)
            return int(value) if value is not None else None
        except Exception:  # noqa: BLE001
            return None

    def close_at_set(self, epoch):
        if epoch is None:
            self._call("delete", K_CLOSE_AT)
        else:
            self._call("set", K_CLOSE_AT, int(epoch))

    def take(self, key):
        """读取并删除一个键，键不存在时返回 None。"""
        try:
            pipe = self._r.pipeline(transaction=True)
            pipe.get(key)
            pipe.delete(key)
            value, _deleted = pipe.execute()
            return value
        except Exception:  # noqa: BLE001
            return None

    def locked_get(self):
        try:
            return self._r.get(K_LOCKED) == "1"
        except Exception:  # noqa: BLE001
            return None

    def locked_set(self, locked):
        if locked:
            self._call("set", K_LOCKED, "1")
        else:
            self._call("delete", K_LOCKED)

    # ---- 会话 ----
    def session_role_epoch(self, role):
        return _counter_value(self._call("get", k_sess_epoch(role)))

    def session_principal_version(self, role, principal):
        return _counter_value(self._call("get", k_sess_version(role, principal)))

    def session_fence(self, role, principal):
        epoch, version = self._call("mget", [k_sess_epoch(role), k_sess_version(role, principal)])
        return _counter_value(epoch), _counter_value(version)

    def session_create(self, payload, expected_epoch, expected_version):
        """只有账号代际仍等于登录读取时的值，才签发新会话；成功后旧会话全部失效。"""
        identity = _session_identity(payload)
        if identity is None:
            raise RuntimeError("invalid session payload")
        role, principal = identity
        token = secrets.token_urlsafe(32)
        result = int(self._run(
            "session_create",
            [k_sess_epoch(role), k_sess_version(role, principal), k_sess(token),
             k_sess_role_mutation(role), k_sess_principal_mutation(role, principal)],
            [json.dumps(payload, ensure_ascii=False, separators=(",", ":")), SESSION_TTL,
             expected_epoch, expected_version],
            attempts=1,
        ))
        if result == 1:
            return token
        if result in (0, -2):
            return None
        raise RuntimeError("invalid session generation")

    def session_get(self, token):
        if not token:
            return None
        raw = self._call("get", k_sess(token))
        if not raw:
            return None
        try:
            payload = json.loads(raw)
        except (TypeError, ValueError):
            return None
        identity = _session_identity(payload)
        if identity is None:
            return None
        epoch = payload.get("_session_epoch")
        version = payload.get("_session_version")
        if type(epoch) is not int or type(version) is not int:
            return None
        current_epoch, current_version = self.session_fence(*identity)
        if epoch != current_epoch or version != current_version:
            return None
        return payload

    def session_del(self, token):
        if token and self._r is not None:
            try:
                self._r.delete(k_sess(token))
            except Exception:  # noqa: BLE001
                pass

    def session_revoke_identity(self, role, principal):
        self._call("incr", k_sess_version(role, principal))

    def session_revoke_role(self, role):
        self._call("incr", k_sess_epoch(role))

    def session_begin_mutation(self, role, principal=None):
        """账号变更提交期间暂停签发会话。"""
        key = (k_sess_role_mutation(role) if principal is None
               else k_sess_principal_mutation(role, principal))
        token = secrets.token_urlsafe(18)
        if self._call("set", key, token, nx=True, ex=SESSION_MUTATION_TTL):
            return key, token
        return None

    def session_end_mutation(self, lock):
        if lock:
            self.lease_end(lock[0], lock[1])

    # ---- 登录限流与受信设备 ----
    def login_blocked(self, key):
        try:
            return int(self._r.get(k_fail(key)) or 0) >= LOGIN_MAX_FAILS
        except Exception:  # noqa: BLE001
            return False

    def login_fail(self, key):
        try:
            self._run("login_fail", [k_fail(key)], [LOGIN_WINDOW], attempts=1)
        except RuntimeError:
            pass

    def login_ok(self, *keys):
        try:
            self._r.delete(*[k_fail(key) for key in keys])
        except Exception:  # noqa: BLE001
            pass

    def device_trusted(self, role, username, device):
        try:
            return bool(device) and bool(self._r.exists(k_trust(role, username, device)))
        except Exception:  # noqa: BLE001
            return False

    def trust_device(self, role, username, device):
        try:
            self._r.set(k_trust(role, username, device), "1", ex=DEVICE_TTL)
        except Exception:  # noqa: BLE001
            pass


RG = RedisGate(REDIS_URL)
REGISTRATION_SLOTS = threading.BoundedSemaphore(REGISTER_CONCURRENCY)
RECONCILE_REQUESTED = threading.Event()   # 需要整体发布(Redis 重启或首次启动)
RECONCILE_STOP = threading.Event()
METRICS_CACHE_LOCK = threading.Lock()
METRICS_CACHE = {"created": 0.0, "payload": None}


# ==========================================================================
# 口令哈希(argon2id；兼容存量明文，登录时就地升级)
# ==========================================================================
_PH = (PasswordHasher(time_cost=ARGON2_TIME_COST, memory_cost=ARGON2_MEMORY_KIB,
                      parallelism=ARGON2_PARALLELISM)
       if PasswordHasher is not None else None)


def hash_password(plain):
    if _PH is None:
        raise RuntimeError("argon2-cffi 不可用，拒绝写入口令")
    return _PH.hash(plain)


def verify_password(stored, plain):
    """返回 (ok, needs_upgrade)。参数与当前配置不同的哈希在登录成功后升级。"""
    if _PH is None or stored is None:
        return False, False
    if stored.startswith("$argon2"):
        try:
            _PH.verify(stored, plain)
            return True, _PH.check_needs_rehash(stored)
        except (VerifyMismatchError, InvalidHashError):
            return False, False
        except Exception:  # noqa: BLE001
            return False, False
    if stored.startswith("plain$"):
        return secrets.compare_digest(stored[6:], plain), True
    return secrets.compare_digest(stored, plain), True


class AuthBusy:
    """口令队列排不进去：wait 是建议浏览器等待的秒数。"""

    def __init__(self, wait):
        self.wait = wait


class AuthQueue:
    """口令哈希在固定数量的线程里先到先算，占用内存不超过线程数 × ARGON2_MEMORY_KIB。

    排不进去的请求按服务速度依次约定回来的时间（Retry-After），回来时队列正好有空位。
    """

    def __init__(self, workers, limit):
        self._workers = workers
        self._limit = limit
        self._pool = concurrent.futures.ThreadPoolExecutor(max_workers=workers, thread_name_prefix="auth")
        self._lock = threading.Lock()
        self._pending = 0
        self._seconds = 0.1
        self._next_slot = 0.0

    def _timed(self, fn, args):
        started = time.monotonic()
        try:
            return fn(*args)
        finally:
            elapsed = time.monotonic() - started
            with self._lock:
                self._seconds += (elapsed - self._seconds) * 0.2
                self._pending -= 1

    def _saturated_locked(self):
        # 队列满，或按最近耗时估算排到也要接近 AUTH_WAIT，都请浏览器稍后再来。
        return (self._pending >= self._limit
                or self._pending * self._seconds / self._workers >= AUTH_WAIT * 0.8)

    def _defer_locked(self):
        now = time.monotonic()
        interval = self._seconds / self._workers
        self._next_slot = max(self._next_slot, now + self._pending * interval) + interval
        return max(1, min(30, int(self._next_slot - now) + 1))

    def admission(self):
        """有空位返回 0，否则返回建议等待的秒数。"""
        with self._lock:
            return self._defer_locked() if self._saturated_locked() else 0

    def submit(self, fn, *args, force=False):
        """返回 (future, 0)；排不进去时返回 (None, 建议等待的秒数)。"""
        with self._lock:
            if not force and self._saturated_locked():
                return None, self._defer_locked()
            self._pending += 1
        return self._pool.submit(self._timed, fn, args), 0

    def run(self, fn, *args):
        """排队执行 fn 并返回结果；排不进去或等待超过 AUTH_WAIT 时返回 AuthBusy。"""
        future, wait = self.submit(fn, *args)
        if future is None:
            return AuthBusy(wait)
        try:
            return future.result(timeout=AUTH_WAIT)
        except concurrent.futures.TimeoutError:
            if future.cancel():
                with self._lock:
                    self._pending -= 1
            return AuthBusy(1)


AUTH = AuthQueue(AUTH_CONCURRENCY, AUTH_QUEUE)


def verify_and_upgrade(stored, plain):
    ok, upgrade = verify_password(stored, plain)
    return ok, (hash_password(plain) if ok and upgrade else None)


def verify_and_replace(stored, current, new):
    ok, _ = verify_password(stored, current)
    return ok, (hash_password(new) if ok else None)


def gen_password(length=8):
    """随机每人口令(避免易混字符);明文仅用于一次性下发，入库存哈希。"""
    alphabet = "ABCDEFGHJKMNPQRSTUVWXYZabcdefghjkmnpqrstuvwxyz23456789"
    return "".join(secrets.choice(alphabet) for _ in range(length))


# ==========================================================================
# 输入消毒(服务端防存储型 XSS:拒绝危险字符，不改数据语义)
# ==========================================================================
# 仅拒尖括号与控制字符:前端一律用 textContent 输出、SQL 全参数化、CSV 走 csv 模块,
# 故 & ' " 等在合法姓名里(如 王&李、O'Brien)是安全的，放行以免静默丢学生。
_BAD_CHARS = set('<>')


def clean_text(s, maxlen=50):
    if s is None:
        return None
    s = str(s).strip()
    if not s or len(s) > maxlen:
        return None
    if any(c in _BAD_CHARS for c in s):
        return None
    if any(ord(c) < 32 for c in s):  # 控制字符
        return None
    return s


def derive_grade(klass):
    """Best-effort compatibility bridge for legacy free-form class labels."""
    if not isinstance(klass, str):
        return ""
    match = re.search(r"(?:高|初)[一二三123]", klass.strip())
    if not match:
        return ""
    value = match.group(0)
    return {"高1": "高一", "高2": "高二", "高3": "高三",
            "初1": "初一", "初2": "初二", "初3": "初三"}.get(value, value)


def clean_text_list(value, *, max_items=50, item_maxlen=80):
    if value is None:
        return []
    if not isinstance(value, list) or len(value) > max_items:
        return None
    result, seen = [], set()
    for item in value:
        if not isinstance(item, str):
            return None
        cleaned = clean_text(item, maxlen=item_maxlen)
        if not cleaned:
            return None
        if cleaned not in seen:
            seen.add(cleaned)
            result.append(cleaned)
    return result


def stored_text_list(value):
    """Best-effort decoder used only for read/display compatibility."""
    try:
        parsed = json.loads(value or "[]")
    except (TypeError, ValueError, json.JSONDecodeError):
        return []
    if not isinstance(parsed, list) or not all(isinstance(item, str) for item in parsed):
        return []
    return parsed


def strict_text_list(value):
    """Decode a persisted eligibility list without turning corruption into allow-all.

    Admin writes always serialize a bounded list, but an older hand-edited
    database can contain malformed JSON.  The hot Python path must then agree
    with Rust and reject admission until an administrator repairs the club,
    rather than silently treating the restriction as empty.
    """
    try:
        parsed = json.loads(value or "[]")
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    if not isinstance(parsed, list) or not all(isinstance(item, str) for item in parsed):
        return None
    return parsed


def student_matches_restrictions(grade, klass, allowed_grades, allowed_classes):
    grades = strict_text_list(allowed_grades)
    classes = strict_text_list(allowed_classes)
    if grades is None or classes is None:
        return False, "社团限制配置异常"
    if grades and grade not in grades:
        return False, "不符合年级限制"
    if classes and klass not in classes:
        return False, "不符合班级限制"
    return True, None


def _audit_json(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")) if value is not None else None


def append_audit_event(cur, *, event_id, actor_role, actor_id, action,
                       target_type, target_id=None, request_id=None, reason=None,
                       before=None, after=None, metadata=None):
    """Append an immutable, non-secret event in the caller's SQL transaction."""
    cur.execute(
        "INSERT INTO audit_events "
        "(event_id, occurred_at, actor_role, actor_id, action, target_type, target_id, "
        "request_id, reason, before_json, after_json, metadata_json) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (event_id, int(time.time() * 1000), actor_role, str(actor_id), action,
         target_type, str(target_id) if target_id is not None else None, request_id,
         reason, _audit_json(before), _audit_json(after), _audit_json(metadata)),
    )


def admin_request_id(data):
    """Validate the caller-supplied idempotency key for an admin mutation."""
    value = data.get("request_id")
    if value is None:
        return None
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{8,128}", value):
        return None
    return value


class ClubImageError(ValueError):
    """An uploaded club image failed bounded decode or normalization."""


class ClubImageFeatureUnavailable(RuntimeError):
    """The server is intentionally refusing unvalidated image persistence."""


_CLUB_IMAGE_NAME = re.compile(
    r"^(?P<club_id>[1-9][0-9]*)-(?P<object_id>[0-9a-f]{32})\.(?P<ext>gif|jpg|png|webp)$"
)
_CLUB_IMAGE_MIME = {
    "GIF": ("gif", "image/gif"),
    "JPEG": ("jpg", "image/jpeg"),
    "PNG": ("png", "image/png"),
    "WEBP": ("webp", "image/webp"),
}


def _sanitize_club_image(raw, declared_type):
    """Decode, bound, and re-encode JPEG/PNG/WebP/GIF before persistence."""
    if not HAS_PILLOW:
        raise ClubImageFeatureUnavailable("服务器暂未启用图片处理，请联系管理员")
    from PIL import Image, ImageOps, ImageSequence, UnidentifiedImageError
    if not raw or len(raw) > MAX_CLUB_IMAGE_BYTES:
        raise ClubImageError("图片内容为空或超过 10 MB")
    declared_type = (declared_type or "").split(";", 1)[0].strip().lower()
    if declared_type not in {mime for _ext, mime in _CLUB_IMAGE_MIME.values()}:
        raise ClubImageError("仅支持 JPEG、PNG、WebP 和 GIF")
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(raw)) as source:
                image_format = (source.format or "").upper()
                if image_format not in _CLUB_IMAGE_MIME:
                    raise ClubImageError("无法识别图片格式")
                extension, actual_type = _CLUB_IMAGE_MIME[image_format]
                if declared_type != actual_type:
                    raise ClubImageError("图片内容与 Content-Type 不一致")
                width, height = source.size
                if image_format == "GIF" and not (1 <= width <= MAX_CLUB_IMAGE_WIDTH
                                                  and 1 <= height <= MAX_CLUB_IMAGE_HEIGHT):
                    raise ClubImageError("GIF 尺寸不能超过 1920×1080")
                if not 1 <= width * height <= MAX_CLUB_IMAGE_PIXELS:
                    raise ClubImageError("图片不能超过 5000 万像素")
                output = io.BytesIO()
                frames, duration_ms = 1, 0
                if image_format == "GIF":
                    rendered, durations = [], []
                    decoded_pixels = 0
                    for frames, frame in enumerate(ImageSequence.Iterator(source), start=1):
                        if frames > MAX_CLUB_GIF_FRAMES:
                            raise ClubImageError("GIF 不能超过 120 帧")
                        frame.load()
                        decoded_pixels += width * height
                        if decoded_pixels > MAX_CLUB_GIF_DECODED_PIXELS:
                            raise ClubImageError("GIF 解码规模过大")
                        duration = frame.info.get("duration", source.info.get("duration", 100))
                        duration = max(80, min(int(duration) if isinstance(duration, (int, float)) else 100, 2000))
                        duration_ms += duration
                        if duration_ms > MAX_CLUB_GIF_DURATION_MS:
                            raise ClubImageError("GIF 总时长不能超过 30 秒")
                        rendered.append(frame.convert("RGBA"))
                        durations.append(duration)
                    rendered[0].save(output, format="GIF", save_all=True, append_images=rendered[1:],
                                     duration=durations, loop=int(source.info.get("loop", 0) or 0),
                                     disposal=2, optimize=True)
                else:
                    # 照片统一缩到长边 CLUB_IMAGE_SIDE；带透明通道的存 PNG，其余存 JPEG（旧版 iOS 不支持 WebP）。
                    if image_format == "JPEG":
                        source.draft("RGB", (CLUB_IMAGE_SIDE * 2, CLUB_IMAGE_SIDE * 2))
                    source.load()
                    image = ImageOps.exif_transpose(source)
                    image.thumbnail((CLUB_IMAGE_SIDE, CLUB_IMAGE_SIDE), Image.LANCZOS)
                    width, height = image.size
                    if "A" in image.getbands() or image.mode == "P" and "transparency" in image.info:
                        image.convert("RGBA").save(output, format="PNG", optimize=True)
                        extension, actual_type = _CLUB_IMAGE_MIME["PNG"]
                    else:
                        image.convert("RGB").save(output, format="JPEG", quality=82, optimize=True,
                                                  progressive=True, subsampling="4:2:0")
                        extension, actual_type = _CLUB_IMAGE_MIME["JPEG"]
    except ClubImageError:
        raise
    except (UnidentifiedImageError, OSError, ValueError) as exc:
        raise ClubImageError("图片损坏或无法完整解码") from exc
    normalized = output.getvalue()
    if not normalized or len(normalized) > MAX_CLUB_IMAGE_OUTPUT_BYTES:
        raise ClubImageError("处理后的图片过大")
    return normalized, extension, actual_type, width, height, frames, duration_ms


def _club_image_disk_path(image_path):
    if not isinstance(image_path, str) or not image_path.startswith("/club-images/"):
        return None
    name = image_path[len("/club-images/"):]
    if not _CLUB_IMAGE_NAME.fullmatch(name):
        return None
    root = os.path.realpath(CLUB_IMAGE_DIR)
    target = os.path.realpath(os.path.join(root, name))
    try:
        return target if os.path.commonpath((root, target)) == root else None
    except ValueError:
        return None


def _remove_managed_club_image(image_path):
    target = _club_image_disk_path(image_path)
    if not target:
        return
    # Immutable object names are unique per upload, but keep a DB reference
    # check as a second line of defense for legacy files or operator repairs.
    # If the database is unavailable, retaining an orphan is safer than
    # deleting a currently referenced public asset.
    try:
        with DB_POOL.connection() as conn:
            if conn.execute("SELECT 1 FROM clubs WHERE image_path=? LIMIT 1", (image_path,)).fetchone():
                return
    except Exception as exc:  # noqa: BLE001
        log.warning("跳过社团图片回收 path=%s error=%s", image_path, exc)
        return
    try:
        os.unlink(target)
    except FileNotFoundError:
        pass
    except OSError as exc:
        log.warning("清理旧社团图片失败 path=%s error=%s", image_path, exc)


def _percentile(values, fraction):
    if not values:
        return None
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, int((len(ordered) - 1) * fraction)))
    return round(ordered[index], 2)


def operations_snapshot():
    """Fixed-window admin metrics without exposing raw logs or paths."""
    now = time.time()
    with METRICS_CACHE_LOCK:
        if METRICS_CACHE["payload"] is not None and now - METRICS_CACHE["created"] < METRICS_CACHE_SECONDS:
            return METRICS_CACHE["payload"]

    response_counts = {"2xx": 0, "4xx": 0, "429": 0, "5xx": 0, "503": 0}
    limited = {"rate": 0, "connections": 0}
    upstream = {"rust": 0, "python_backup": 0, "unavailable": 0}
    request_times, requests = [], 0
    try:
        with open(NGINX_ACCESS_LOG, "rb") as log_file:
            log_file.seek(0, os.SEEK_END)
            log_file.seek(max(0, log_file.tell() - 1_048_576))
            lines = log_file.read().decode("utf-8", "replace").splitlines()
        for line in lines:
            match = _ACCESS_LOG_RE.match(line)
            if not match:
                continue
            try:
                timestamp, status = float(match.group("ts")), int(match.group("status"))
            except ValueError:
                continue
            if timestamp < now - METRICS_WINDOW_SECONDS:
                continue
            requests += 1
            if 200 <= status < 300:
                response_counts["2xx"] += 1
            elif status == 429:
                response_counts["429"] += 1
                response_counts["4xx"] += 1
            elif 400 <= status < 500:
                response_counts["4xx"] += 1
            elif status == 503:
                response_counts["503"] += 1
                response_counts["5xx"] += 1
            elif status >= 500:
                response_counts["5xx"] += 1
            limit_req, limit_conn = match.group("limit_req"), match.group("limit_conn")
            if limit_req is None:
                limited["rate"] += status == 429
            elif limit_req not in ("-", "", "PASSED"):
                limited["rate"] += 1
            if limit_conn not in (None, "-", "", "PASSED"):
                limited["connections"] += 1
            address = match.group("upstream")
            # nginx emits all attempted upstreams separated by ", ".  The
            # final address is the backend that served the response, so a
            # Rust timeout followed by Python backup is counted correctly.
            addresses = re.findall(r"(?:\[[^\]]+\]|[^,\s]+):\d+", address)
            final_address = addresses[-1] if addresses else ""
            if final_address.endswith(":2002"):
                upstream["rust"] += 1
            elif final_address.endswith(":2001"):
                upstream["python_backup"] += 1
            elif address in ("-", ""):
                upstream["unavailable"] += 1
            try:
                request_times.append(float(match.group("request_time")) * 1000)
            except ValueError:
                pass
    except OSError:
        pass

    try:
        with DB_POOL.connection() as conn:
            conn.execute("SELECT 1").fetchone()
            pending_clubs = int(conn.execute(
                "SELECT COUNT(*) FROM clubs WHERE seat_sync_pending=1"
            ).fetchone()[0])
        db_ok = True
    except Exception:  # noqa: BLE001
        db_ok = False
        pending_clubs = None
    try:
        maintenance = bool(RG.r and RG.r.exists(K_MAINT))
        active_operations = RG.has_active_operations()
    except Exception:  # noqa: BLE001
        maintenance = None
        active_operations = None
    payload = {
        "sampled_at_ms": int(now * 1000),
        "window_seconds": METRICS_WINDOW_SECONDS,
        "edge": {
            "requests": requests,
            "rps": round(requests / METRICS_WINDOW_SECONDS, 2),
            "responses": response_counts,
            "limited": limited,
            "upstream": upstream,
            "request_time_ms": {"p50": _percentile(request_times, 0.50),
                                "p95": _percentile(request_times, 0.95)},
        },
        "seat_state": {
            "redis": RG.alive(), "sqlite": db_ok,
            "reconcile_pending": RECONCILE_REQUESTED.is_set(),
            "maintenance": maintenance, "active_operations": active_operations,
            "repairs_pending": len(RG.pending_repairs()),
            "clubs_sync_pending": pending_clubs,
        },
    }
    with METRICS_CACHE_LOCK:
        METRICS_CACHE["created"] = now
        METRICS_CACHE["payload"] = payload
    return payload


# ==========================================================================
# 调剂：锁定名单后，把未报名的学生随机安排到还有名额的社团
# ==========================================================================
def registration_method(operation_id):
    text = operation_id or ""
    if text.startswith("adjust-"):
        return "调剂"
    if text.startswith("admin-"):
        return "管理员安排"
    return "自选"


def parse_optional_time(text):
    if not text:
        return None
    try:
        return parse_local_time(text)
    except ValueError:
        return None


def deadline_passed(close_at, now_ms=None):
    if close_at is None:
        return False
    return (now_ms if now_ms is not None else time.time() * 1000) >= close_at * 1000


# 这些操作会改变报名表的内容；工作台据此判断最近一次导出与备份是否为最新数据。
DATA_CHANGE_ACTIONS = (
    "registration.created", "registration.cancelled", "registration.admin_assigned",
    "registration.admin_removed", "registration.admin_transferred", "registration.lock_updated",
    "registrations.adjusted", "registrations.cleared", "students.imported", "student.updated",
    "student.deleted", "students.deleted_all", "clubs.imported", "club.updated", "club.deleted",
    "clubs.deleted_all",
)


def registration_locked(conn):
    """名单已锁定，或已到截止时间（后台随后写入锁定）。"""
    row = conn.execute(
        "SELECT COALESCE(registration_locked, 0), registration_end_time FROM settings ORDER BY id DESC LIMIT 1"
    ).fetchone()
    return bool(row and (row[0] or deadline_passed(parse_optional_time(row[1]))))


def _max_flow(groups, seats):
    """groups：{可报社团集合: 人数}；seats：{社团: 空余名额}。返回 {社团集合: {社团: 人数}}，安排人数最多。"""
    capacity = collections.defaultdict(dict)

    def edge(u, v, amount):
        capacity[u][v] = capacity[u].get(v, 0) + amount
        capacity[v].setdefault(u, 0)

    keys = list(groups)
    for index, options in enumerate(keys):
        edge("source", ("group", index), groups[options])
        for club in sorted(options):
            edge(("group", index), ("club", club), groups[options])
    for club, free in seats.items():
        edge(("club", club), "sink", free)
    flow = collections.defaultdict(lambda: collections.defaultdict(int))
    while True:
        parent = {"source": None}
        pending = collections.deque(["source"])
        while pending and "sink" not in parent:
            node = pending.popleft()
            for nxt, cap in capacity[node].items():
                if nxt not in parent and cap - flow[node][nxt] > 0:
                    parent[nxt] = node
                    pending.append(nxt)
        if "sink" not in parent:
            break
        path, node = [], "sink"
        while parent[node] is not None:
            path.append((parent[node], node))
            node = parent[node]
        amount = min(capacity[u][v] - flow[u][v] for u, v in path)
        for u, v in path:
            flow[u][v] += amount
            flow[v][u] -= amount
    return {options: {club: flow[("group", index)][("club", club)] for club in options
                      if flow[("group", index)][("club", club)] > 0}
            for index, options in enumerate(keys)}


def plan_adjustment(conn, rng=None):
    """返回 (安排 [(学生, 社团)], 无法安排的学生, {社团: 空余名额})。

    只用开放中的社团，遵守年级和班级限制；能安排的人数最多，同组学生之间随机。
    """
    students = conn.execute(
        "SELECT s.id, s.grade, s.class FROM students s "
        "LEFT JOIN registrations r ON r.student_id = s.id WHERE r.id IS NULL ORDER BY s.id"
    ).fetchall()
    clubs = conn.execute(
        "SELECT c.id, c.max_students - (SELECT COUNT(*) FROM registrations r WHERE r.club_id = c.id), "
        "c.enabled, c.allowed_grades, c.allowed_classes FROM clubs c ORDER BY c.id"
    ).fetchall()
    seats = {club: free for club, free, enabled, _grades, _classes in clubs if enabled and free > 0}
    groups = collections.OrderedDict()
    for student, grade, klass in students:
        options = frozenset(club for club, _free, _enabled, grades, classes in clubs
                            if club in seats and student_matches_restrictions(grade, klass, grades, classes)[0])
        groups.setdefault(options, []).append(student)
    allocation = _max_flow({options: len(members) for options, members in groups.items() if options}, seats)
    rng = rng or random.SystemRandom()
    pairs, unplaced = [], []
    for options, members in groups.items():
        members = list(members)
        rng.shuffle(members)
        for club, count in sorted(allocation.get(options, {}).items()):
            pairs.extend((student, club) for student in members[:count])
            members = members[count:]
        unplaced.extend(members)
    return pairs, unplaced, seats


# ==========================================================================
# 导出：Excel（标准库生成）与 CSV
# ==========================================================================
_FILE_NAME_BAD = re.compile(r'[\\/?*\[\]:"<>|]')
xlsx_bytes = tables.write_xlsx


def attachment_header(filename, fallback):
    """中文文件名按 RFC 5987 给出，旧浏览器使用 ASCII 备用名。"""
    return ("Content-Disposition",
            "attachment; filename=\"{}\"; filename*=UTF-8''{}".format(fallback, urllib.parse.quote(filename)))


def export_stamp():
    return datetime.now(APP_TZ).strftime("%Y%m%d_%H%M")


def gen_admin_password():
    # 字母表只含容易辨认的字符。
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZabcdefghjkmnpqrstuvwxyz23456789"
    return "".join(secrets.choice(alphabet) for _ in range(12))


def read_admin_secret():
    try:
        with open(ADMIN_SECRET_FILE, encoding="utf-8") as source:
            return source.read().strip() or None
    except OSError:
        return None


def write_admin_secret(password):
    """管理员密码存于数据库旁的文件（仅本机账户可读），启动脚本每次打印。"""
    directory = os.path.dirname(ADMIN_SECRET_FILE)
    os.makedirs(directory, exist_ok=True)
    partial = ADMIN_SECRET_FILE + ".part"
    descriptor = os.open(partial, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as target:
        target.write(password + "\n")
    os.replace(partial, ADMIN_SECRET_FILE)


# ==========================================================================
# 备份：危险操作前与锁定时自动保存数据库快照；完整备份含数据库与社团照片
# ==========================================================================
BACKUP_DIR = os.path.abspath(os.environ.get(
    "BACKUP_DIR", os.path.join(os.path.dirname(os.path.abspath(DB_PATH)), "backups"),
))
BACKUP_KEEP = max(1, int(os.environ.get("BACKUP_KEEP", "30")))
BACKUP_NAME_RE = re.compile(r"(\d{8}-\d{6})-([a-z]+(?:-[a-z]+)*)(?:-\d+)?\.db")
BACKUP_LABELS = {
    "locked": "名单锁定", "before-adjust": "调剂前", "before-clear": "清空报名前",
    "before-delete-students": "清空学生前", "before-delete-clubs": "清空社团前", "before-restore": "恢复前",
}
BACKUP_DB_NAME = "club_system.db"
BACKUP_TABLES = {"settings", "students", "clubs", "registrations"}


def copy_database(source_path, target_path):
    """用 SQLite 备份接口复制：源库同时被写入时，得到的是某一时刻的完整状态。"""
    source = sqlite3.connect(source_path, timeout=30.0)
    try:
        target = sqlite3.connect(target_path, timeout=30.0)
        try:
            source.backup(target)
        finally:
            target.close()
    finally:
        source.close()


def list_snapshots():
    try:
        names = os.listdir(BACKUP_DIR)
    except OSError:
        return []
    return sorted((name for name in names if BACKUP_NAME_RE.fullmatch(name)), reverse=True)


def snapshot_database(label):
    """把当前数据库存入备份目录，返回文件名；只保留最近 BACKUP_KEEP 份。"""
    os.makedirs(BACKUP_DIR, mode=0o700, exist_ok=True)
    stamp = datetime.now(APP_TZ).strftime("%Y%m%d-%H%M%S")
    name, number = "{}-{}.db".format(stamp, label), 2
    while os.path.exists(os.path.join(BACKUP_DIR, name)):
        name, number = "{}-{}-{}.db".format(stamp, label, number), number + 1
    path = os.path.join(BACKUP_DIR, name)
    partial = path + ".part"
    try:
        copy_database(DB_PATH, partial)
        os.replace(partial, path)
    finally:
        with contextlib.suppress(OSError):
            os.remove(partial)
    for stale in list_snapshots()[BACKUP_KEEP:]:
        with contextlib.suppress(OSError):
            os.remove(os.path.join(BACKUP_DIR, stale))
    return name


def backup_archive():
    """完整备份：数据库与当前的社团照片，打成 zip。"""
    out = io.BytesIO()
    with tempfile.TemporaryDirectory(prefix="omms-backup-") as tmp:
        database = os.path.join(tmp, BACKUP_DB_NAME)
        copy_database(DB_PATH, database)
        with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.write(database, BACKUP_DB_NAME)
            with contextlib.suppress(OSError):
                for entry in sorted(os.listdir(CLUB_IMAGE_DIR)):
                    if _CLUB_IMAGE_NAME.fullmatch(entry):
                        archive.write(os.path.join(CLUB_IMAGE_DIR, entry), "club-images/" + entry,
                                      compress_type=zipfile.ZIP_STORED)
    return out.getvalue()


def database_counts(path):
    conn = sqlite3.connect(path, timeout=30.0)
    try:
        if conn.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise ValueError("数据库文件损坏")
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        missing = BACKUP_TABLES - tables
        if missing:
            raise ValueError("缺少数据表：" + "、".join(sorted(missing)))
        return tuple(conn.execute("SELECT COUNT(*) FROM " + table).fetchone()[0]
                     for table in ("students", "clubs", "registrations"))
    finally:
        conn.close()


def cli_admin_login(base_url):
    """启动脚本调用：核对密码文件与数据库，必要时生成新密码；签发一次性登录链接。"""
    if not os.path.isfile(DB_PATH):
        print("ERROR=数据库不存在")
        return 1
    conn = sqlite3.connect(DB_PATH, timeout=30.0)
    try:
        row = conn.execute(
            "SELECT id, admin_username, admin_password FROM settings ORDER BY id DESC LIMIT 1").fetchone()
        if not row:
            print("ERROR=数据库没有系统设置")
            return 1
        password = read_admin_secret()
        if not password or not verify_and_upgrade(row[2], password)[0]:
            password = gen_admin_password()
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("UPDATE settings SET admin_password=? WHERE id=?", (hash_password(password), row[0]))
            append_audit_event(
                conn.cursor(), event_id="admin-password-reset-" + secrets.token_urlsafe(18), actor_role="admin",
                actor_id="系统", action="admin.password_reset", target_type="settings", target_id=row[0],
                after={"password_reset": True},
            )
            conn.commit()
            write_admin_secret(password)
    finally:
        conn.close()
    url = ""
    token = secrets.token_urlsafe(32)
    with contextlib.suppress(Exception):
        RG.r.set(rk("admin_login", token), "1", ex=ADMIN_LOGIN_TTL)
        url = base_url.rstrip("/") + "/admin/login?token=" + token
    print("ADMIN_USER=" + (row[1] or "admin"))
    print("ADMIN_PASSWORD=" + password)
    print("LOGIN_URL=" + url)
    return 0


def restriction_code(grade, klass, allowed_grades, allowed_classes):
    """资格不符时返回提示码，符合返回 None。"""
    grades = strict_text_list(allowed_grades)
    classes = strict_text_list(allowed_classes)
    if grades is None or classes is None:
        return "ineligible_config"
    if grades and grade not in grades:
        return "ineligible_grade"
    if classes and klass not in classes:
        return "ineligible_class"
    return None


def admission_result(row):
    """row = (enabled, allowed_grades, allowed_classes, seat_sync_pending, grade, class)。
    可以抢位返回 None，否则返回 (status, code)。"""
    if not row:
        return 200, "club_missing"
    enabled, allowed_grades, allowed_classes, sync_pending, grade, klass = row
    if not enabled:
        return 200, "club_disabled"
    if sync_pending:
        return 503, "syncing"
    code = restriction_code(grade, klass, allowed_grades, allowed_classes)
    return (200, code) if code else None


def club_seat_truth(club_id):
    """SQLite 中单个社团的容量、报名数与报名镜像；社团不存在返回 None。"""
    with DB_POOL.connection() as conn:
        row = conn.execute("SELECT max_students FROM clubs WHERE id=?", (club_id,)).fetchone()
        if not row:
            return None
        registrations = conn.execute(
            "SELECT student_id, operation_id FROM registrations WHERE club_id=?", (club_id,)
        ).fetchall()
    mirrors = {int(student_id): reservation_mirror(club_id, operation_id)
               for student_id, operation_id in registrations}
    return int(row[0]), len(registrations), mirrors


def publish_club_with_fence(club_id, token):
    """持有社团栅栏时以 SQLite 为准发布库存与镜像，并清除该社团的同步标记。"""
    truth = club_seat_truth(club_id)
    if truth is None:
        RG.drop_club(club_id, token)
        RG.repair_done(club_id)
        return True
    maximum, used, mirrors = truth
    if not RG.fence_publish(club_id, token, max(0, maximum - used), mirrors):
        return False
    with DB_POOL.connection() as conn:
        conn.execute("UPDATE clubs SET current_students=?, seat_sync_pending=0 WHERE id=?",
                     (used, club_id))
    RG.repair_done(club_id)
    SEATS_CACHE.invalidate()
    return True


def repair_club(club_id):
    """单社团修复：只暂停这一个社团的抢位，通常在毫秒级完成。"""
    token = RG.fence_acquire(club_id)
    if not token:
        return False
    try:
        return publish_club_with_fence(club_id, token)
    except Exception as e:  # noqa: BLE001
        log.warning("社团名额修复失败 club=%s: %s", club_id, e)
        return False
    finally:
        RG.fence_release(club_id, token)


def acquire_seat_locks(club_ids, student_id=None):
    """管理员调整名额前取得学生租约与相关社团栅栏；任一失败则全部释放并返回 None。"""
    held = {"clubs": {}, "student": None}
    try:
        if student_id is not None:
            lease = "admin|" + secrets.token_urlsafe(9)
            if not RG.student_lease_begin(student_id, lease):
                return None
            held["student"] = (student_id, lease)
        for club_id in sorted({int(c) for c in club_ids if c}):
            token = RG.fence_acquire(club_id)
            if not token:
                release_seat_locks(held)
                return None
            held["clubs"][club_id] = token
        return held
    except RuntimeError:
        release_seat_locks(held)
        return None


def release_seat_locks(held):
    if not held:
        return
    for club_id, token in held["clubs"].items():
        RG.fence_release(club_id, token)
    if held["student"]:
        student_id, lease = held["student"]
        RG.lease_end(k_op(student_id), lease)


def publish_seat_locks(held, student_mirror=None):
    """落库后发布受影响社团；student_mirror 为 "" 时删除该学生的镜像。返回是否全部成功。"""
    ok = True
    for club_id, token in held["clubs"].items():
        try:
            ok = publish_club_with_fence(club_id, token) and ok
        except Exception as e:  # noqa: BLE001
            log.warning("社团名额发布失败 club=%s: %s", club_id, e)
            ok = False
    if held["student"] and student_mirror is not None:
        try:
            RG.mirror_write(held["student"][0], student_mirror)
        except RuntimeError:
            ok = False
    return ok


class ConditionalCache:
    """按时间缓存一份 JSON 响应体及其 ETag。"""

    def __init__(self, ttl, builder):
        self._ttl = ttl
        self._builder = builder
        self._lock = threading.Lock()
        self._value = None
        self._created = 0.0

    def get(self):
        now = time.monotonic()
        with self._lock:
            if self._value is not None and now - self._created < self._ttl:
                return self._value
        body = json.dumps(self._builder(), ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        value = (body, 'W/"{}"'.format(hashlib.sha1(body).hexdigest()[:20]))
        with self._lock:
            self._value, self._created = value, time.monotonic()
        return value

    def invalidate(self):
        with self._lock:
            self._value = None


def build_catalog():
    with DB_POOL.connection() as conn:
        rows = conn.execute(
            "SELECT id, name, max_students, description, advisor_name, meeting_time, location, "
            "image_path, allowed_grades, allowed_classes, enabled, revision, seat_sync_pending "
            "FROM clubs ORDER BY id"
        ).fetchall()
    return [{
        "id": cid, "name": name, "max_students": maximum, "description": description,
        "advisor_name": advisor, "meeting_time": meeting, "location": location,
        "image_path": image_path, "allowed_grades": stored_text_list(grades),
        "allowed_classes": stored_text_list(classes), "enabled": bool(enabled),
        "revision": revision, "seat_sync_pending": bool(pending),
    } for (cid, name, maximum, description, advisor, meeting, location, image_path, grades,
           classes, enabled, revision, pending) in rows]


def build_seats():
    _catalog_body, catalog_etag = CATALOG_CACHE.get()
    with DB_POOL.connection() as conn:
        rows = conn.execute("SELECT id, max_students, current_students FROM clubs ORDER BY id").fetchall()
        setting = conn.execute(
            "SELECT registration_start_time, registration_locked, registration_end_time "
            "FROM settings ORDER BY id DESC LIMIT 1"
        ).fetchone()
    live = RG.stock_left([row[0] for row in rows]) or {}
    seats = []
    for club_id, maximum, current in rows:
        remaining = live.get(club_id)
        if remaining is None:
            remaining = maximum - (current or 0)
        seats.append([club_id, max(0, min(maximum, remaining))])
    open_at = RG.open_at_get()
    if open_at is None and setting and setting[0]:
        try:
            open_at = parse_local_time(setting[0])
        except ValueError:
            open_at = None
    close_at = parse_optional_time(setting[2]) if setting else None
    locked = bool(setting and setting[1]) or bool(RG.locked_get()) or deadline_passed(close_at, RG.now_epoch_ms())
    return {"catalog": catalog_etag, "open_at": open_at * 1000 if open_at is not None else None,
            "close_at": close_at * 1000 if close_at is not None else None,
            "end_time": setting[2] if close_at is not None else None, "locked": locked, "seats": seats}


CATALOG_CACHE = ConditionalCache(CATALOG_CACHE_SECONDS, build_catalog)
SEATS_CACHE = ConditionalCache(SEATS_CACHE_SECONDS, build_seats)


def _csv_safe(v):
    """防 CSV 公式注入:首字符为 = + - @ 或制表/回车时前置单引号,Excel 打开不会当公式执行。"""
    if isinstance(v, str) and v and v[0] in "=+-@\t\r":
        return "'" + v
    return v


# ==========================================================================
# 连接池(queue.Queue 阻塞池;每连接 WAL/FK/busy_timeout/autocommit)
# ==========================================================================
class DatabaseConnectionPool:
    def __init__(self, db_path=DB_PATH, size=POOL_SIZE, timeout=10.0):
        self._db_path = db_path
        self._timeout = timeout
        self._pool = queue.Queue(maxsize=size)
        self._all = []
        self._lock = threading.Lock()
        for _ in range(size):
            conn = self._new()
            self._all.append(conn)
            self._pool.put(conn)

    def _new(self):
        conn = sqlite3.connect(
            self._db_path,
            check_same_thread=False,
            timeout=self._timeout,
            isolation_level=None,  # autocommit;写事务显式 BEGIN/COMMIT
        )
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=10000")
        conn.execute("PRAGMA synchronous={}".format(SQLITE_SYNCHRONOUS))
        return conn

    def get(self):
        try:
            return self._pool.get(timeout=self._timeout)
        except queue.Empty:
            raise RuntimeError("数据库连接池耗尽(超时未获得连接)")

    def put(self, conn):
        if conn is None:
            return
        try:
            conn.rollback()  # 清残留事务，防脏连接
        except sqlite3.Error:
            pass
        try:
            self._pool.put_nowait(conn)
        except queue.Full:
            pass  # 幂等:多还/重复还直接丢弃

    @contextlib.contextmanager
    def connection(self):
        conn = self.get()
        try:
            yield conn
        finally:
            self.put(conn)

    def close_all(self):
        with self._lock:
            for conn in self._all:
                try:
                    conn.close()
                except sqlite3.Error:
                    pass
            self._all.clear()


DB_POOL = None  # init_db() 后赋值


# ==========================================================================
# 用户名生成(复用调用者连接 + 同事务/本批去重，原子)
# ==========================================================================
def _lazy_pinyin():
    # 用户名只需可读、唯一，加载单字表即可；词组表另占约 40 MB。
    os.environ.setdefault("PYPINYIN_NO_PHRASES", "true")
    os.environ.setdefault("PYPINYIN_NO_DICT_COPY", "true")
    try:
        from pypinyin import lazy_pinyin
    except ImportError:
        return None
    return lazy_pinyin


def gen_username(name, cursor, seen):
    raw = str(name).strip()
    to_pinyin = _lazy_pinyin() if HAS_PYPINYIN else None
    if to_pinyin is not None:
        base = "".join(c for c in "".join(to_pinyin(raw)).lower() if c.isalnum()) or "stu"
    else:
        base = "".join(c for c in raw if c.strip()) or "user"
    candidate = base
    i = 0
    while True:
        if candidate not in seen:
            cursor.execute("SELECT 1 FROM students WHERE username = ?", (candidate,))
            if cursor.fetchone() is None:
                seen.add(candidate)
                return candidate
        i += 1
        candidate = "{}{}".format(base, i)


# ==========================================================================
# 建库 + 启动自愈 + 口令迁移 + Redis 名额重建
# ==========================================================================
def ensure_schema(conn):
    """建表、在线迁移与触发器；启动时和恢复备份后各执行一次，事务由调用方提交。"""
    cur = conn.cursor()
    cur.executescript(
        """
        CREATE TABLE IF NOT EXISTS students (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL, class TEXT NOT NULL,
            student_id TEXT NOT NULL UNIQUE, username TEXT NOT NULL UNIQUE,
            password TEXT NOT NULL,
            grade TEXT NOT NULL DEFAULT '');
        CREATE TABLE IF NOT EXISTS clubs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE,
            max_students INTEGER NOT NULL CHECK(max_students > 0),
            current_students INTEGER DEFAULT 0,
            description TEXT NOT NULL DEFAULT '',
            advisor_name TEXT NOT NULL DEFAULT '',
            meeting_time TEXT NOT NULL DEFAULT '',
            location TEXT NOT NULL DEFAULT '',
            image_path TEXT,
            allowed_grades TEXT NOT NULL DEFAULT '[]',
            allowed_classes TEXT NOT NULL DEFAULT '[]',
            enabled INTEGER NOT NULL DEFAULT 1,
            revision INTEGER NOT NULL DEFAULT 0,
            seat_revision INTEGER NOT NULL DEFAULT 0,
            seat_sync_pending INTEGER NOT NULL DEFAULT 0);
        CREATE TABLE IF NOT EXISTS registrations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            student_id INTEGER NOT NULL, club_id INTEGER NOT NULL,
            registration_time TEXT NOT NULL,
            operation_id TEXT,
            FOREIGN KEY (student_id) REFERENCES students (id),
            FOREIGN KEY (club_id) REFERENCES clubs (id),
            UNIQUE (student_id));
        CREATE INDEX IF NOT EXISTS idx_registrations_club_id ON registrations(club_id);
        CREATE TABLE IF NOT EXISTS settings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            registration_start_time TEXT,
            registration_locked INTEGER NOT NULL DEFAULT 0,
            admin_username TEXT DEFAULT 'admin',
            admin_password TEXT DEFAULT NULL);
        CREATE TABLE IF NOT EXISTS audit_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_id TEXT NOT NULL UNIQUE,
            occurred_at INTEGER NOT NULL,
            actor_role TEXT NOT NULL,
            actor_id TEXT NOT NULL,
            action TEXT NOT NULL,
            target_type TEXT NOT NULL,
            target_id TEXT,
            request_id TEXT,
            reason TEXT,
            before_json TEXT,
            after_json TEXT,
            metadata_json TEXT);
        CREATE INDEX IF NOT EXISTS idx_audit_events_time
            ON audit_events(occurred_at DESC);
        CREATE INDEX IF NOT EXISTS idx_audit_events_target
            ON audit_events(target_type, target_id, occurred_at DESC);
        CREATE UNIQUE INDEX IF NOT EXISTS idx_audit_events_request
            ON audit_events(actor_role, actor_id, action, request_id)
            WHERE request_id IS NOT NULL;
        """
    )
    conn.execute("BEGIN IMMEDIATE")
    def ensure_column(table, name, definition):
        cur.execute("PRAGMA table_info({})".format(table))
        if name not in {row[1] for row in cur.fetchall()}:
            cur.execute("ALTER TABLE {} ADD COLUMN {}".format(table, definition))

    ensure_column("students", "grade", "grade TEXT NOT NULL DEFAULT ''")
    ensure_column("settings", "registration_locked", "registration_locked INTEGER NOT NULL DEFAULT 0")
    ensure_column("settings", "registration_end_time", "registration_end_time TEXT")
    for name, definition in (
        ("description", "description TEXT NOT NULL DEFAULT ''"),
        ("advisor_name", "advisor_name TEXT NOT NULL DEFAULT ''"),
        ("meeting_time", "meeting_time TEXT NOT NULL DEFAULT ''"),
        ("location", "location TEXT NOT NULL DEFAULT ''"),
        ("image_path", "image_path TEXT"),
        ("allowed_grades", "allowed_grades TEXT NOT NULL DEFAULT '[]'"),
        ("allowed_classes", "allowed_classes TEXT NOT NULL DEFAULT '[]'"),
        ("enabled", "enabled INTEGER NOT NULL DEFAULT 1"),
        ("revision", "revision INTEGER NOT NULL DEFAULT 0"),
        ("seat_revision", "seat_revision INTEGER NOT NULL DEFAULT 0"),
        ("seat_sync_pending", "seat_sync_pending INTEGER NOT NULL DEFAULT 0"),
    ):
        ensure_column("clubs", name, definition)
    # 旧库在线迁移:operation_id 用来让 Redis confirm/cancel 精确匹配同一代操作。
    cur.execute("PRAGMA table_info(registrations)")
    if "operation_id" not in {r[1] for r in cur.fetchall()}:
        cur.execute("ALTER TABLE registrations ADD COLUMN operation_id TEXT")
    cur.execute(
        "UPDATE registrations SET operation_id = lower(hex(randomblob(16))) "
        "WHERE operation_id IS NULL OR operation_id = ''"
    )
    cur.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_registrations_operation_id "
        "ON registrations(operation_id) WHERE operation_id IS NOT NULL"
    )
    # Redis 是快速准入层；SQLite trigger 是跨 Python/Rust 的最终容量保险。
    # 即使库存因运维或故障漂移，第 max+1 条 registration 也无法落库。
    cur.execute(
        """
        CREATE TRIGGER IF NOT EXISTS registrations_capacity_guard
        BEFORE INSERT ON registrations
        FOR EACH ROW
        BEGIN
          SELECT CASE WHEN
            (SELECT COUNT(*) FROM registrations WHERE club_id = NEW.club_id) >=
            (SELECT max_students FROM clubs WHERE id = NEW.club_id)
          THEN RAISE(ABORT, 'club full') END;
        END
        """
    )
    cur.execute(
        """
        CREATE TRIGGER IF NOT EXISTS registrations_capacity_guard_update
        BEFORE UPDATE OF club_id ON registrations
        FOR EACH ROW WHEN NEW.club_id != OLD.club_id
        BEGIN
          SELECT CASE WHEN
            (SELECT COUNT(*) FROM registrations WHERE club_id = NEW.club_id) >=
            (SELECT max_students FROM clubs WHERE id = NEW.club_id)
          THEN RAISE(ABORT, 'club full') END;
        END
        """
    )
    # `clubs.max_students` is business data, but it still needs a durable
    # bound: Python integers are unbounded while SQLite INTEGER is signed
    # 64-bit.  Without this guard one malformed import rolls back a whole
    # otherwise valid batch with OverflowError.
    cur.execute(
        f"""
        CREATE TRIGGER IF NOT EXISTS clubs_capacity_limit_insert
        BEFORE INSERT ON clubs
        FOR EACH ROW WHEN NEW.max_students < 1 OR NEW.max_students > {MAX_CLUB_CAPACITY}
        BEGIN
          SELECT RAISE(ABORT, 'invalid club capacity');
        END
        """
    )
    cur.execute(
        f"""
        CREATE TRIGGER IF NOT EXISTS clubs_capacity_limit_update
        BEFORE UPDATE OF max_students ON clubs
        FOR EACH ROW WHEN NEW.max_students < 1 OR NEW.max_students > {MAX_CLUB_CAPACITY}
        BEGIN
          SELECT RAISE(ABORT, 'invalid club capacity');
        END
        """
    )
    cur.execute(
        """
        CREATE TRIGGER IF NOT EXISTS clubs_capacity_registration_guard
        BEFORE UPDATE OF max_students ON clubs
        FOR EACH ROW WHEN NEW.max_students <
            (SELECT COUNT(*) FROM registrations WHERE club_id = OLD.id)
        BEGIN
          SELECT RAISE(ABORT, 'club capacity below registrations');
        END
        """
    )
    cur.execute(
        """
        CREATE TRIGGER IF NOT EXISTS audit_events_append_only_update
        BEFORE UPDATE ON audit_events
        BEGIN
          SELECT RAISE(ABORT, 'audit events are append-only');
        END
        """
    )
    cur.execute(
        """
        CREATE TRIGGER IF NOT EXISTS audit_events_append_only_delete
        BEFORE DELETE ON audit_events
        BEGIN
          SELECT RAISE(ABORT, 'audit events are append-only');
        END
        """
    )
    cur.execute(
        """
        UPDATE students
        SET grade = CASE
            WHEN class LIKE '高一%' THEN '高一'
            WHEN class LIKE '高二%' THEN '高二'
            WHEN class LIKE '高三%' THEN '高三'
            WHEN class LIKE '初一%' THEN '初一'
            WHEN class LIKE '初二%' THEN '初二'
            WHEN class LIKE '初三%' THEN '初三'
            ELSE grade
        END
        WHERE grade = ''
        """
    )


def init_db():
    global DB_POOL
    if _PH is None:
        raise RuntimeError("缺少 argon2-cffi；为避免明文口令回退，服务拒绝启动")
    conn = sqlite3.connect(DB_PATH)
    try:
        cur = conn.cursor()
        cur.execute("PRAGMA journal_mode=WAL")
        ensure_schema(conn)
        # 初始化 settings
        cur.execute("SELECT id, admin_password FROM settings LIMIT 1")
        row = cur.fetchone()
        if row is None:
            # 首次启动生成管理员密码，写入本机密码文件，由启动脚本打印。
            init_admin_pw = gen_admin_password()
            cur.execute("INSERT INTO settings (admin_password) VALUES (?)", (hash_password(init_admin_pw),))
            write_admin_secret(init_admin_pw)
        else:
            # 管理员口令一次性迁移成 argon2
            sid, apw = row
            if apw and not apw.startswith("$argon2"):
                cur.execute("UPDATE settings SET admin_password = ? WHERE id = ?",
                            (hash_password(apw), sid))
                log.info("管理员口令已迁移为 argon2 哈希")
        # 启动自愈:删孤儿 + 重算 current_students
        cur.execute(
            "DELETE FROM registrations WHERE student_id NOT IN (SELECT id FROM students) "
            "OR club_id NOT IN (SELECT id FROM clubs)"
        )
        cur.execute(
            "UPDATE clubs SET current_students = "
            "(SELECT COUNT(*) FROM registrations r WHERE r.club_id = clubs.id)"
        )
        conn.commit()
    finally:
        conn.close()

    DB_POOL = DatabaseConnectionPool()
    if not RG.alive():
        RECONCILE_REQUESTED.set()
    elif not RG.r.exists(K_INIT):
        if not rebuild_all():
            RECONCILE_REQUESTED.set()
    else:
        seed_settings_mirror()


def rebuild_all(token=None):
    """以 SQLite 为准整体发布 Redis 名额状态：首次启动、Redis 重启或清空类操作后使用。"""
    if not RG.alive():
        return False
    owns = token is None
    if owns:
        token = RG.maintenance_begin()
        if not token:
            return False
    try:
        with DB_POOL.connection() as conn:
            conn.execute("BEGIN")
            rows = conn.execute(
                "SELECT c.id, c.max_students, "
                "(SELECT COUNT(*) FROM registrations r WHERE r.club_id=c.id) FROM clubs c"
            ).fetchall()
            registrations = conn.execute(
                "SELECT student_id, club_id, operation_id FROM registrations"
            ).fetchall()
            setting = conn.execute(
                "SELECT registration_start_time, registration_locked, registration_end_time "
                "FROM settings ORDER BY id DESC LIMIT 1"
            ).fetchone()
            conn.commit()
        open_at = None
        if setting and setting[0]:
            try:
                open_at = parse_local_time(setting[0])
            except ValueError:
                log.warning("报名开放时间格式无效: %s", setting[0])
        stocks = {int(cid): max(0, int(maximum) - int(used)) for cid, maximum, used in rows}
        mirrors = {int(sid): reservation_mirror(cid, op) for sid, cid, op in registrations}
        RG.publish_all(token, stocks, mirrors, open_at, bool(setting and setting[1]),
                       parse_optional_time(setting[2]) if setting else None)
        with DB_POOL.connection() as conn:
            conn.execute(
                "UPDATE clubs SET current_students="
                "(SELECT COUNT(*) FROM registrations r WHERE r.club_id=clubs.id), seat_sync_pending=0"
            )
        CATALOG_CACHE.invalidate()
        SEATS_CACHE.invalidate()
        log.info("Redis 名额已整体发布: %d 个社团", len(rows))
        return True
    except Exception as e:  # noqa: BLE001
        log.error("整体发布名额失败: %s", e)
        return False
    finally:
        if owns:
            RG.maintenance_end(token)


def seed_settings_mirror():
    """把 SQLite 中的开放时间与锁定状态同步到 Redis。"""
    try:
        with DB_POOL.connection() as conn:
            row = conn.execute(
                "SELECT registration_start_time, registration_locked, registration_end_time "
                "FROM settings ORDER BY id DESC LIMIT 1"
            ).fetchone()
        if not row:
            return
        RG.open_at_set(parse_local_time(row[0]) if row[0] else None)
        RG.close_at_set(parse_optional_time(row[2]))
        RG.locked_set(bool(row[1]))
    except (RuntimeError, ValueError) as e:
        log.warning("同步开放时间与锁定状态失败: %s", e)


def lock_at_deadline():
    """到截止时间后写入锁定：清除截止时间，记入操作记录，并保存快照。抢位脚本此前已按截止时间拒绝。"""
    with DB_POOL.connection() as conn:
        row = conn.execute(
            "SELECT id, registration_end_time, COALESCE(registration_locked, 0) FROM settings ORDER BY id DESC LIMIT 1"
        ).fetchone()
    close_at = parse_optional_time(row[1]) if row else None
    if close_at is None or not deadline_passed(close_at, RG.now_epoch_ms()):
        return False
    if row[2]:
        with DB_POOL.connection() as conn:
            conn.execute("UPDATE settings SET registration_end_time=NULL WHERE id=? AND registration_end_time=?",
                         (row[0], row[1]))
        RG.close_at_set(None)
        SEATS_CACHE.invalidate()
        return False
    with DB_POOL.connection() as conn:
        cur = conn.cursor()
        conn.execute("BEGIN IMMEDIATE")
        current = cur.execute(
            "SELECT registration_end_time, COALESCE(registration_locked, 0) FROM settings WHERE id=?", (row[0],)
        ).fetchone()
        if not current or current[0] != row[1] or current[1]:
            conn.rollback()
            return False
        cur.execute("UPDATE settings SET registration_locked=1, registration_end_time=NULL WHERE id=?", (row[0],))
        append_audit_event(
            cur, event_id="registration-deadline-" + secrets.token_urlsafe(18), actor_role="admin",
            actor_id="系统", action="registration.lock_updated", target_type="settings", target_id=row[0],
            reason="到达截止时间", before={"locked": False, "end_time": row[1]}, after={"locked": True},
        )
        conn.commit()
    RG.locked_set(True)
    RG.close_at_set(None)
    SEATS_CACHE.invalidate()
    log.info("到达截止时间 %s，名单已锁定", row[1])
    try:
        snapshot_database("locked")
    except (OSError, sqlite3.Error) as exc:
        log.error("锁定后自动备份失败: %s", exc)
    return True


def seat_worker():
    """后台维护名额：Redis 重启后整体发布，按需修复单个社团，定期清理失效的在途成员，到截止时间写入锁定。"""
    last_scan = 0.0
    while not RECONCILE_STOP.wait(REPAIR_INTERVAL):
        try:
            if not RG.alive():
                continue
            lock_at_deadline()
            if RECONCILE_REQUESTED.is_set() or not RG.r.exists(K_INIT):
                RECONCILE_REQUESTED.set()
                if rebuild_all():
                    RECONCILE_REQUESTED.clear()
                continue
            targets = RG.pending_repairs()
            scan = time.monotonic() - last_scan >= ORPHAN_SCAN_INTERVAL
            with DB_POOL.connection() as conn:
                targets.update(int(row[0]) for row in conn.execute(
                    "SELECT id FROM clubs WHERE seat_sync_pending=1"))
                club_ids = [int(row[0]) for row in conn.execute("SELECT id FROM clubs")] if scan else []
            if scan:
                last_scan = time.monotonic()
                for club_id in club_ids:
                    _live, dead = RG.inflight_state(club_id)
                    if dead:
                        targets.add(club_id)
            for club_id in sorted(targets):
                repair_club(club_id)
        except Exception as e:  # noqa: BLE001
            log.warning("名额后台维护出错: %s", e)


# ==========================================================================
# 路由角色表(public / student / admin)
# ==========================================================================
ROLE_PUBLIC, ROLE_STUDENT, ROLE_ADMIN = "public", "student", "admin"

GET_ROUTES = {
    "/healthz": ("_h_health", ROLE_PUBLIC),
    "/readyz": ("_h_ready", ROLE_PUBLIC),
    "/api/check_registration_time": ("_h_check_time", ROLE_PUBLIC),
    "/api/get_clubs": ("_h_get_clubs", ROLE_PUBLIC),
    "/api/catalog": ("_h_catalog", ROLE_PUBLIC),
    "/api/seats": ("_h_seats", ROLE_PUBLIC),
    "/api/get_student_info": ("_h_get_student_info", ROLE_STUDENT),
    "/api/get_registrations": ("_h_get_registrations", ROLE_ADMIN),
    "/api/get_all_students": ("_h_get_all_students", ROLE_ADMIN),
    "/api/export_students_csv": ("_h_export_students_csv", ROLE_ADMIN),
    "/api/export_all_data": ("_h_export_all_data", ROLE_ADMIN),
    "/api/export_unregistered": ("_h_export_unregistered", ROLE_ADMIN),
    "/api/admin/audit_events": ("_h_audit_events", ROLE_ADMIN),
    "/api/admin/operations_snapshot": ("_h_operations_snapshot", ROLE_ADMIN),
    "/api/admin/summary": ("_h_admin_summary", ROLE_ADMIN),
    "/api/admin/adjustment": ("_h_adjustment_plan", ROLE_ADMIN),
    "/api/export_workbook": ("_h_export_workbook", ROLE_ADMIN),
    "/api/admin/backup": ("_h_backup", ROLE_ADMIN),
    "/api/admin/backups": ("_h_backups", ROLE_ADMIN),
    "/api/admin/addresses": ("_h_addresses", ROLE_ADMIN),
    "/api/admin/preflight_plan": ("_h_preflight_plan", ROLE_ADMIN),
}
POST_ROUTES = {
    "/api/login": ("_h_login", ROLE_PUBLIC),
    "/api/admin_login": ("_h_admin_login", ROLE_PUBLIC),
    "/api/logout": ("_h_logout", ROLE_PUBLIC),
    "/api/register_club": ("_h_register_club", ROLE_STUDENT),
    "/api/cancel_registration": ("_h_cancel_registration", ROLE_STUDENT),
    "/api/admin_change_password": ("_h_admin_change_password", ROLE_ADMIN),
    "/api/import_students": ("_h_import_students", ROLE_ADMIN),
    "/api/import_clubs": ("_h_import_clubs", ROLE_ADMIN),
    "/api/update_registration_time": ("_h_update_time", ROLE_ADMIN),
    "/api/update_registration_lock": ("_h_update_registration_lock", ROLE_ADMIN),
    "/api/delete_student": ("_h_delete_student", ROLE_ADMIN),
    "/api/delete_all_students": ("_h_delete_all_students", ROLE_ADMIN),
    "/api/delete_club": ("_h_delete_club", ROLE_ADMIN),
    "/api/delete_all_clubs": ("_h_delete_all_clubs", ROLE_ADMIN),
    "/api/admin/reset_student_password": ("_h_reset_student_password", ROLE_ADMIN),
    "/api/update_club": ("_h_update_club", ROLE_ADMIN),
    "/api/admin/assign_registration": ("_h_admin_assign_registration", ROLE_ADMIN),
    "/api/admin/remove_registration": ("_h_admin_remove_registration", ROLE_ADMIN),
    "/api/admin/transfer_registration": ("_h_admin_transfer_registration", ROLE_ADMIN),
    "/api/admin/adjust_registrations": ("_h_adjust_registrations", ROLE_ADMIN),
    "/api/admin/clear_registrations": ("_h_clear_registrations", ROLE_ADMIN),
    "/api/admin/update_student": ("_h_update_student", ROLE_ADMIN),
    "/api/admin/reset_class_passwords": ("_h_reset_class_passwords", ROLE_ADMIN),
    "/api/remove_club_image": ("_h_remove_club_image", ROLE_ADMIN),
    "/api/admin/restore": ("_h_restore", ROLE_ADMIN),
    "/api/admin/accounts_workbook": ("_h_accounts_workbook", ROLE_ADMIN),
}
RAW_POST_ROUTES = {
    "/api/upload_club_image": ("_h_upload_club_image", ROLE_ADMIN, MAX_CLUB_IMAGE_BYTES),
    "/api/admin/import/parse": ("_h_import_parse", ROLE_ADMIN, MAX_TABLE_BYTES),
    "/api/admin/restore_upload": ("_h_restore_upload", ROLE_ADMIN, MAX_RESTORE_BYTES),
}
WEB_DIR = os.environ.get("WEB_DIR", "web")  # 前端资源目录
PAGES = {
    "/": "login.html",
    "/student/dashboard": "student_dashboard.html",
    "/admin/dashboard": "admin_dashboard.html",
}
STATIC_TYPES = {
    "css": "text/css; charset=utf-8",
    "js": "application/javascript; charset=utf-8",
    "svg": "image/svg+xml",
    "png": "image/png",
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "webp": "image/webp",
    "woff2": "font/woff2",
}
STATIC_PATH_RE = re.compile(r"^/(?:(?:js|img)/)?[A-Za-z0-9][A-Za-z0-9._-]*\.([a-z0-9]+)$")
SECURITY_HEADERS = [
    ("X-Content-Type-Options", "nosniff"),
    ("X-Frame-Options", "DENY"),
    ("Referrer-Policy", "strict-origin-when-cross-origin"),
    ("Content-Security-Policy",
     "default-src 'self'; base-uri 'none'; object-src 'none'; frame-ancestors 'none'; "
     "form-action 'self'; img-src 'self' data: blob:; font-src 'self'; connect-src 'self'; "
     "style-src 'self'; script-src 'self'"),
]


class RequestBodyError(ValueError):
    """无法在当前连接上安全读完的请求体。"""

    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


# ==========================================================================
# 请求处理器
# ==========================================================================
class ClubSystemHandler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    timeout = 30  # 回收卡住的连接

    # ---- 响应辅助 ----
    def _send(self, code, body=b"", ctype="application/json; charset=utf-8", extra=None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        if self.close_connection:
            self.send_header("Connection", "close")
        for key, value in extra or ():
            self.send_header(key, value)
        self.end_headers()
        if body:
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass

    def _json(self, code, obj, extra=None):
        self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"), extra=extra)

    def _reply(self, status, code, success=False, extra=None, **fields):
        """统一的业务响应：success、code、message 与附加字段。"""
        body = {"success": success, "code": code, "message": MESSAGES.get(code, code)}
        body.update(fields)
        headers = list(extra or [])
        if status in (202, 429, 503) and not any(k == "Retry-After" for k, _v in headers):
            headers.append(("Retry-After", "1"))
        self._json(status, body, extra=headers)

    def _conditional(self, body, etag):
        """带 ETag 的公共数据；If-None-Match 命中时返回 304。"""
        headers = [("ETag", etag), ("Cache-Control", "no-cache")]
        candidates = [value.strip() for value in (self.headers.get("If-None-Match") or "").split(",")]
        if etag in candidates:
            self.send_response(304)
            for key, value in headers:
                self.send_header(key, value)
            if self.close_connection:
                self.send_header("Connection", "close")
            self.end_headers()
            return
        self._send(200, body, extra=headers)

    def _csv(self, rows, header, filename, fallback):
        out = io.StringIO()
        writer = csv.writer(out)
        writer.writerow([_csv_safe(c) for c in header])
        for row in rows:
            writer.writerow([_csv_safe(c) for c in row])
        body = out.getvalue().encode("utf-8-sig")  # BOM 便于 Excel 识别中文
        self._send(200, body, ctype="text/csv; charset=utf-8",
                   extra=[attachment_header(filename, fallback), ("Cache-Control", "no-store")])

    def log_message(self, fmt, *args):
        log.debug("%s - %s", self.address_string(), fmt % args)

    # ---- 会话与 Cookie ----
    def _session(self):
        return RG.session_get(_session_cookie_token_from_headers(self.headers))

    def _cookie(self, token_name, value, max_age):
        secure = "; Secure" if COOKIE_SECURE else ""
        return ("Set-Cookie", "{}={}; HttpOnly; SameSite=Strict; Path=/; Max-Age={}{}".format(
            token_name, value, max_age, secure))

    def _set_session_cookie(self, token):
        return self._cookie("session", token, SESSION_TTL)

    def _clear_cookie(self):
        return self._cookie("session", "", 0)

    def _device_token(self):
        value = _cookie_value(_single_cookie_header(self.headers), "device")
        return value if value and DEVICE_RE.fullmatch(value) else None

    def _client_ip(self):
        """只信任本机反代写入的 X-Real-IP。"""
        peer = self.client_address[0]
        try:
            peer_ip = ipaddress.ip_address(peer)
        except ValueError:
            return peer
        if not peer_ip.is_loopback:
            return peer
        forwarded = (self.headers.get("X-Real-IP") or "").strip()
        try:
            return str(ipaddress.ip_address(forwarded)) if forwarded else peer
        except ValueError:
            return peer

    def _require(self, role):
        """返回会话(public 时为 None)；鉴权未通过时已发送响应并返回 False。"""
        if role == ROLE_PUBLIC:
            return None
        try:
            sess = self._session()
        except RuntimeError:
            self._reply(503, "unavailable")
            return False
        if sess is None:
            self._reply(401, "unauthorized")
            return False
        if sess.get("role") != role:
            self._reply(403, "forbidden")
            return False
        return sess

    def _read_raw_body(self, max_bytes=None):
        """读完恰好一个 Content-Length 请求体，或标记连接关闭，保证复用连接上的请求边界。"""
        transfer_encodings = self.headers.get_all("Transfer-Encoding") or []
        lengths = self.headers.get_all("Content-Length") or []
        if transfer_encodings:
            self.close_connection = True
            raise RequestBodyError("不支持 Transfer-Encoding 请求体")
        if len(lengths) != 1:
            self.close_connection = True
            raise RequestBodyError("缺少或重复 Content-Length")
        length = lengths[0].strip()
        if not length.isdecimal():
            self.close_connection = True
            raise RequestBodyError("非法 Content-Length")
        n = int(length)
        limit = MAX_BODY if max_bytes is None else int(max_bytes)
        if n > limit:
            self.close_connection = True
            raise RequestBodyError("请求体过大", status=413)
        try:
            raw = self.rfile.read(n) if n else b""
        except (OSError, TimeoutError) as e:
            self.close_connection = True
            raise RequestBodyError("读取请求体失败") from e
        if len(raw) != n:
            self.close_connection = True
            raise RequestBodyError("请求体长度不完整")
        return raw

    def _reject_get_body(self):
        transfer_encodings = self.headers.get_all("Transfer-Encoding") or []
        lengths = self.headers.get_all("Content-Length") or []
        if transfer_encodings or len(lengths) > 1:
            self.close_connection = True
            self._reply(400, "bad_request")
            return True
        if lengths:
            value = lengths[0].strip()
            if not value.isdecimal() or int(value) != 0:
                self.close_connection = True
                self._reply(400, "bad_request")
                return True
        return False

    # ---- 分发 ----
    def do_GET(self):
        if self._reject_get_body():
            return
        path = self.path.split("?")[0]
        if path == "/admin/login":
            return self._token_login()
        if path in PAGES:
            return self._serve_page(PAGES[path])
        if path.startswith("/club-images/"):
            return self._serve_club_image(path)
        static = STATIC_PATH_RE.fullmatch(path)
        if static and ".." not in path and static.group(1) in STATIC_TYPES:
            return self._serve_static(path[1:], STATIC_TYPES[static.group(1)])
        route = GET_ROUTES.get(path)
        if route is None and path.startswith("/api/export_club_data"):
            route = ("_h_export_club_data", ROLE_ADMIN)
        if route is None:
            return self._reply(404, "not_found")
        name, role = route
        sess = self._require(role)
        if sess is False:
            return
        try:
            getattr(self, name)(sess)
        except Exception as e:  # noqa: BLE001
            log.exception("GET %s 处理异常: %s", path, e)
            self._reply(500, "server_error")

    def do_POST(self):
        path = self.path.split("?")[0]
        # 页面脚本发出的请求都带这个头；跨站表单无法设置自定义请求头。
        if self.headers.get("X-Requested-With") != "XMLHttpRequest":
            self.close_connection = True
            return self._reply(403, "forbidden")
        route = POST_ROUTES.get(path)
        raw_route = RAW_POST_ROUTES.get(path)
        try:
            raw = self._read_raw_body(raw_route[2] if raw_route is not None else None)
        except RequestBodyError as e:
            return self._json(e.status, {"success": False, "code": "bad_request",
                                         "message": "请求格式错误：{}".format(e)})
        if raw_route is not None:
            name, role, _limit = raw_route
            sess = self._require(role)
            if sess is False:
                return
            try:
                return getattr(self, name)(sess, raw)
            except Exception as e:  # noqa: BLE001
                log.exception("raw POST %s 处理异常: %s", path, e)
                return self._reply(500, "server_error")
        if route is None:
            return self._reply(404, "not_found")
        name, role = route
        sess = self._require(role)
        if sess is False:
            return
        try:
            data = json.loads(raw) if raw else {}
            if not isinstance(data, dict):
                raise ValueError("JSON 顶层必须为对象")
        except (ValueError, json.JSONDecodeError):
            return self._reply(400, "bad_request")
        try:
            getattr(self, name)(sess, data)
        except Exception as e:  # noqa: BLE001
            log.exception("POST %s 处理异常: %s", path, e)
            self._reply(500, "server_error")

    # ---- 静态资源(白名单路径，不做通用文件映射) ----
    def _serve_page(self, fname):
        try:
            with open(os.path.join(WEB_DIR, fname), "rb") as f:
                body = f.read()
        except OSError:
            return self._reply(404, "not_found")
        self._send(200, body, ctype="text/html; charset=utf-8",
                   extra=SECURITY_HEADERS + [("Cache-Control", "no-cache")])

    def _serve_static(self, relative, ctype):
        root = os.path.realpath(WEB_DIR)
        target = os.path.realpath(os.path.join(root, relative))
        if os.path.commonpath((root, target)) != root:
            return self._reply(404, "not_found")
        try:
            with open(target, "rb") as f:
                body = f.read()
        except OSError:
            return self._reply(404, "not_found")
        self._send(200, body, ctype=ctype,
                   extra=SECURITY_HEADERS + [("Cache-Control", "no-cache")])

    def _serve_club_image(self, path):
        target = _club_image_disk_path(path)
        match = _CLUB_IMAGE_NAME.fullmatch(os.path.basename(target or ""))
        content_type = {
            "gif": "image/gif", "jpg": "image/jpeg", "png": "image/png", "webp": "image/webp",
        }.get(match.group("ext") if match else "")
        if not target or not content_type:
            return self._reply(404, "not_found")
        # 只有当前社团仍在引用的图片对外可见。
        try:
            with DB_POOL.connection() as conn:
                referenced = conn.execute(
                    "SELECT 1 FROM clubs WHERE image_path=? LIMIT 1", (path,)
                ).fetchone() is not None
        except Exception as exc:  # noqa: BLE001
            log.warning("社团图片引用检查失败 path=%s error=%s", path, exc)
            referenced = False
        if not referenced:
            return self._reply(404, "not_found")
        try:
            descriptor = os.open(target, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
            with os.fdopen(descriptor, "rb") as source:
                body = source.read(MAX_CLUB_IMAGE_OUTPUT_BYTES + 1)
        except OSError:
            return self._reply(404, "not_found")
        if len(body) > MAX_CLUB_IMAGE_OUTPUT_BYTES:
            return self._reply(404, "not_found")
        self._send(200, body, ctype=content_type,
                   extra=[("Cache-Control", "public, max-age=31536000, immutable")])

    # ======================================================================
    # 公共端点
    # ======================================================================
    def _h_health(self, sess):
        try:
            with DB_POOL.connection() as conn:
                conn.execute("SELECT 1").fetchone()
            db_ok = True
        except Exception:  # noqa: BLE001
            db_ok = False
        redis_ok = RG.alive()
        code = 200 if db_ok and redis_ok else 503
        self._json(code, {"status": "ok" if code == 200 else "degraded",
                          "db": db_ok, "redis": redis_ok})

    def _h_ready(self, sess):
        try:
            if not RG.alive():
                return self._json(503, {"status": "not-ready", "reason": "redis"})
            if RECONCILE_REQUESTED.is_set() or not RG.r.exists(K_INIT):
                RECONCILE_REQUESTED.set()
                return self._json(503, {"status": "not-ready", "reason": "initializing"})
            if RG.r.exists(K_MAINT):
                return self._json(503, {"status": "not-ready", "reason": "maintenance"})
            with DB_POOL.connection() as conn:
                rows = conn.execute(
                    "SELECT c.id, c.max_students, "
                    "(SELECT COUNT(*) FROM registrations r WHERE r.club_id=c.id) "
                    "FROM clubs c ORDER BY c.id").fetchall()
            live = RG.stock_left([row[0] for row in rows])
            if live is None:
                return self._json(503, {"status": "not-ready", "reason": "redis"})
            drifted = 0
            for club_id, maximum, used in rows:
                active, _dead = RG.inflight_state(club_id)
                if active:
                    continue
                if used > maximum or live.get(club_id) != max(0, maximum - used):
                    RG.request_repair(club_id)
                    drifted += 1
            if drifted:
                return self._json(503, {"status": "not-ready", "reason": "repairing", "clubs": drifted})
            self._json(200, {"status": "ready"})
        except Exception as e:  # noqa: BLE001
            log.error("readyz 检查失败: %s", e)
            self._json(503, {"status": "not-ready"})

    def _h_check_time(self, sess):
        open_at = RG.open_at_get()
        start_str = None
        end_str = None
        db_locked = False
        try:
            with DB_POOL.connection() as conn:
                row = conn.execute(
                    "SELECT registration_start_time, registration_locked, registration_end_time "
                    "FROM settings ORDER BY id DESC LIMIT 1"
                ).fetchone()
            if row:
                db_locked = bool(row[1])
                end_str = row[2]
                if open_at is None and row[0]:
                    start_str = row[0]
                    try:
                        open_at = parse_local_time(row[0])
                    except ValueError:
                        open_at = None
        except Exception:  # noqa: BLE001
            pass
        if open_at is not None and start_str is None:
            start_str = format_local_time(open_at)
        close_at = parse_optional_time(end_str)
        server_now = RG.now_epoch_ms()
        locked = db_locked or bool(RG.locked_get()) or deadline_passed(close_at, server_now)
        can = open_at is not None and server_now >= open_at * 1000 and not locked
        self._json(200, {
            "can_register": can,
            "start_time": start_str,
            "open_at": open_at * 1000 if open_at is not None else None,
            "server_now": server_now,
            "registration_locked": locked,
            "end_time": end_str if close_at is not None else None,
            "close_at": close_at * 1000 if close_at is not None else None,
        }, extra=[("Cache-Control", "no-store")])

    def _h_get_clubs(self, sess):
        try:
            with DB_POOL.connection() as conn:
                rows = conn.execute(
                    "SELECT id, name, max_students, current_students, description, advisor_name, "
                    "meeting_time, location, image_path, allowed_grades, allowed_classes, enabled, "
                    "revision, seat_sync_pending FROM clubs ORDER BY id"
                ).fetchall()
        except Exception as e:  # noqa: BLE001
            log.error("get_clubs 失败: %s", e)
            return self._reply(500, "server_error")
        live = RG.stock_left([r[0] for r in rows]) or {}
        data = []
        for (cid, name, maxs, cur_s, description, advisor_name, meeting_time,
             location, image_path, allowed_grades, allowed_classes, enabled, revision,
             seat_sync_pending) in rows:
            used = maxs - live[cid] if live.get(cid) is not None else cur_s
            data.append({
                "id": cid, "name": name, "max_students": maxs,
                "current_students": max(0, min(maxs, used)),
                "description": description, "advisor_name": advisor_name,
                "meeting_time": meeting_time, "location": location,
                "image_path": image_path,
                "allowed_grades": stored_text_list(allowed_grades),
                "allowed_classes": stored_text_list(allowed_classes),
                "enabled": bool(enabled),
                "revision": revision,
                "seat_sync_pending": bool(seat_sync_pending),
            })
        self._json(200, data)

    def _h_catalog(self, sess):
        try:
            body, etag = CATALOG_CACHE.get()
        except Exception as e:  # noqa: BLE001
            log.error("catalog 失败: %s", e)
            return self._reply(503, "unavailable")
        self._conditional(body, etag)

    def _h_seats(self, sess):
        try:
            body, etag = SEATS_CACHE.get()
        except Exception as e:  # noqa: BLE001
            log.error("seats 失败: %s", e)
            return self._reply(503, "unavailable")
        self._conditional(body, etag)

    # ---- 登录 ----
    def _h_login(self, sess, data):
        self._login(data, "student")

    def _h_admin_login(self, sess, data):
        self._login(data, "admin")

    def _login(self, data, role):
        username = data.get("username") or ""
        password = data.get("password") or ""
        if not isinstance(username, str) or not isinstance(password, str):
            return self._reply(400, "bad_request")
        username = username.strip()
        if not username or not password:
            return self._reply(400, "login_empty")
        if len(username) > MAX_USERNAME or len(password) > MAX_PASSWORD:
            return self._reply(400, "bad_request")
        wait = AUTH.admission()
        if wait:
            return self._login_queued(wait)
        # 受信设备按"账号+设备"计数，其他设备按账号计数：别人输错密码不影响本人设备登录。
        device = self._device_token()
        trusted = RG.device_trusted(role, username, device)
        account_key = "{}:u:{}".format(role, username)
        throttle_key = "{}:d:{}:{}".format(role, username, device) if trusted else account_key
        if RG.login_blocked(throttle_key):
            return self._reply(429, "login_throttled", extra=[("Retry-After", str(LOGIN_WINDOW))])
        try:
            role_epoch = RG.session_role_epoch(role)
        except RuntimeError:
            return self._reply(503, "unavailable")
        with DB_POOL.connection() as conn:
            if role == "student":
                row = conn.execute(
                    "SELECT id, password, name, class, grade, student_id FROM students WHERE username = ?",
                    (username,)).fetchone()
            else:
                row = conn.execute(
                    "SELECT id, admin_password FROM settings WHERE admin_username = ?",
                    (username,)).fetchone()
        if not row:
            RG.login_fail(throttle_key)
            return self._reply(401, "login_failed")
        principal = str(row[0]) if role == "student" else username
        stored = row[1]
        try:
            principal_version = RG.session_principal_version(role, principal)
        except RuntimeError:
            return self._reply(503, "unavailable")
        verified = AUTH.run(verify_and_upgrade, stored, password)
        if isinstance(verified, AuthBusy):
            return self._login_queued(verified.wait)
        ok, upgraded_hash = verified
        if not ok:
            RG.login_fail(throttle_key)
            return self._reply(401, "login_failed")
        # 账号必须仍是刚才验证的那一条，防止哈希期间的删除或改密被旧读取覆盖。
        with DB_POOL.connection() as conn:
            if role == "student":
                current = conn.execute("SELECT password FROM students WHERE id = ?", (row[0],)).fetchone()
            else:
                current = conn.execute("SELECT admin_password FROM settings WHERE id = ?", (row[0],)).fetchone()
        if not current or not secrets.compare_digest(str(current[0]), str(stored)):
            return self._reply(401, "login_failed")
        if role == "student":
            payload = {"role": "student", "student_id": row[0], "name": row[2], "class": row[3],
                       "grade": row[4], "student_no": row[5]}
        else:
            payload = {"role": "admin", "username": username}
        try:
            token = RG.session_create(payload, role_epoch, principal_version)
        except RuntimeError:
            return self._reply(503, "unavailable")
        if token is None:
            return self._reply(503, "account_changed")
        if upgraded_hash:
            table, column = ("students", "password") if role == "student" else ("settings", "admin_password")
            try:
                with DB_POOL.connection() as conn:
                    conn.execute("UPDATE {} SET {} = ? WHERE id = ? AND {} = ?".format(table, column, column),
                                 (upgraded_hash, row[0], stored))
            except sqlite3.Error:
                pass
        RG.login_ok(throttle_key, account_key)
        if not device:
            device = secrets.token_urlsafe(24)
        RG.trust_device(role, username, device)
        cookies = [self._cookie("device", device, DEVICE_TTL), self._set_session_cookie(token)]
        if role == "student":
            return self._json(200, {"success": True, "student_id": row[0], "name": row[2],
                                    "class": row[3], "student_no": row[5]}, extra=cookies)
        self._json(200, {"success": True}, extra=cookies)

    def _login_queued(self, wait):
        return self._reply(503, "login_queued", extra=[("Retry-After", str(wait))])

    def _auth_busy(self, busy):
        return self._json(503, {"success": False, "message": "密码服务繁忙，请稍后重试"},
                          extra=[("Retry-After", str(busy.wait))])

    def _h_logout(self, sess, data):
        token = _session_cookie_token_from_headers(self.headers)
        if token:
            RG.session_del(token)
        self._json(200, {"success": True}, extra=[self._clear_cookie()])

    # ======================================================================
    # 学生端点(身份只取自会话)
    # ======================================================================
    def _h_get_student_info(self, sess):
        sid = sess["student_id"]
        with DB_POOL.connection() as conn:
            stu = conn.execute(
                "SELECT name, class, grade, student_id, username FROM students WHERE id = ?", (sid,)
            ).fetchone()
            if not stu:
                return self._reply(404, "not_found")
            reg = conn.execute(
                "SELECT c.name, r.registration_time, r.club_id FROM registrations r "
                "JOIN clubs c ON r.club_id = c.id WHERE r.student_id = ?", (sid,)).fetchone()
        self._json(200, {
            "name": stu[0], "class": stu[1], "grade": stu[2], "student_id": stu[3], "username": stu[4],
            "registered_club": reg[0] if reg else None,
            "registered_club_id": reg[2] if reg else None,
            "registration_time": reg[1] if reg else None,
        }, extra=[("Cache-Control", "no-store")])

    def _h_register_club(self, sess, data):
        sid = int(sess["student_id"])
        try:
            club_id = int(data.get("club_id"))
        except (TypeError, ValueError):
            return self._reply(400, "bad_request")
        request_id = data.get("request_id")
        if club_id <= 0 or (request_id is not None and (
                not isinstance(request_id, str) or not REQUEST_ID_RE.fullmatch(request_id))):
            return self._reply(400, "bad_request")
        if RECONCILE_REQUESTED.is_set():
            return self._reply(503, "unavailable")
        try:
            with DB_POOL.connection() as conn:
                row = conn.execute(
                    "SELECT c.enabled, c.allowed_grades, c.allowed_classes, c.seat_sync_pending, "
                    "s.grade, s.class FROM clubs c JOIN students s ON s.id=? WHERE c.id=?",
                    (sid, club_id)).fetchone()
        except Exception as e:  # noqa: BLE001
            log.error("报名准入检查失败 sid=%s club=%s: %s", sid, club_id, e)
            return self._reply(503, "unavailable")
        rejected = admission_result(row)
        if rejected:
            return self._reply(*rejected)
        # 同一 request_id 的重试共享操作代际；租约再带上本次尝试的随机后缀。
        operation_id = "{}-{}".format(sid, request_id or secrets.token_urlsafe(12))
        mirror = reservation_mirror(club_id, operation_id)
        lease = "{}|{}".format(mirror, secrets.token_urlsafe(6))
        self._register_with_lease(sid, club_id, operation_id, mirror, lease, repair_mirror=True)

    def _register_with_lease(self, sid, club_id, operation_id, mirror, lease, repair_mirror):
        member = "{}|{}".format(sid, lease)
        try:
            code = RG.acquire(sid, club_id, lease, member, mirror + "|")
        except RuntimeError:
            RG.request_repair(club_id)
            return self._reply(503, "unavailable")
        if code == 1:
            return self._finalize_registration(sid, club_id, operation_id, mirror, lease, member)
        if code == 3:
            if self._lookup_registration(operation_id):
                return self._reply(200, "registered", success=True)
            return self._reply(202, "pending")
        if code == 0:
            return self._reply(200, "full")
        if code == -4:
            return self._reply(200, "not_open")
        if code == -6:
            return self._reply(200, "locked")
        if code == -3:
            return self._reply(503, "syncing")
        if code == -2:
            if RG.r.exists(K_INIT):
                RG.request_repair(club_id)
            else:
                RECONCILE_REQUESTED.set()
            return self._reply(503, "syncing")
        if code == -1:
            return self._resolve_existing(sid, club_id, operation_id, mirror, lease, repair_mirror)
        return self._reply(503, "unavailable")

    def _resolve_existing(self, sid, club_id, operation_id, mirror, lease, repair_mirror):
        """抢位返回"已报名或有其他操作"时，以数据库为准给出结果。"""
        with DB_POOL.connection() as conn:
            row = conn.execute(
                "SELECT club_id, operation_id FROM registrations WHERE student_id=?", (sid,)
            ).fetchone()
        if row:
            if int(row[0]) != club_id:
                return self._reply(200, "registered_other")
            code = "registered" if row[1] == operation_id else "registered_already"
            return self._reply(200, code, success=True)
        try:
            if RG.lease_value(sid):
                return self._reply(202, "pending")
            stale = RG.mirror_value(sid)
        except RuntimeError:
            return self._reply(503, "unavailable")
        if stale and repair_mirror:
            # 数据库没有报名记录而镜像还在：清掉这条旧镜像后重试一次。
            RG.mirror_set(sid, "clear", stale)
            return self._register_with_lease(sid, club_id, operation_id, mirror, lease, repair_mirror=False)
        return self._reply(202, "pending")

    def _finalize_registration(self, sid, club_id, operation_id, mirror, lease, member):
        if not REGISTRATION_SLOTS.acquire(timeout=REGISTER_QUEUE_TIMEOUT):
            self._safe_rollback(sid, club_id, lease, member)
            return self._reply(503, "queued")
        try:
            try:
                persisted = self._persist_registration(sid, club_id, operation_id)
            except sqlite3.IntegrityError as e:
                return self._registration_conflict(sid, club_id, operation_id, lease, member, e)
            except Exception as e:  # noqa: BLE001
                log.error("报名落库失败 sid=%s club=%s: %s", sid, club_id, e)
                persisted = self._lookup_registration(operation_id)
                if persisted is None:
                    RG.request_repair(club_id)
                    return self._reply(202, "pending")
                if persisted is False:
                    self._safe_rollback(sid, club_id, lease, member)
                    return self._reply(503, "unavailable")
        finally:
            REGISTRATION_SLOTS.release()
        if isinstance(persisted, tuple):
            self._safe_rollback(sid, club_id, lease, member)
            return self._reply(*persisted)
        # 提交之后只做确认与响应，任何异常都不再回滚名额。
        self._confirm_registration(sid, club_id, mirror, lease, member)
        SEATS_CACHE.invalidate()
        self._reply(200, "registered", success=True)

    def _persist_registration(self, sid, club_id, operation_id):
        """写入报名；成功返回 registration id，准入未通过返回 (status, code)。"""
        with DB_POOL.connection() as conn:
            cur = conn.cursor()
            conn.execute("BEGIN IMMEDIATE")
            locked = cur.execute(
                "SELECT COALESCE(registration_locked, 0) FROM settings ORDER BY id DESC LIMIT 1"
            ).fetchone()
            row = cur.execute(
                "SELECT c.enabled, c.allowed_grades, c.allowed_classes, c.seat_sync_pending, "
                "s.grade, s.class FROM clubs c JOIN students s ON s.id=? WHERE c.id=?",
                (sid, club_id)).fetchone()
            rejected = (200, "locked") if locked and locked[0] else admission_result(row)
            if rejected:
                conn.rollback()
                return rejected
            cur.execute(
                "INSERT INTO registrations "
                "(student_id, club_id, registration_time, operation_id) VALUES (?,?,?,?)",
                (sid, club_id, now_text(), operation_id))
            registration_id = cur.lastrowid
            cur.execute("UPDATE clubs SET current_students = current_students + 1 WHERE id = ?",
                        (club_id,))
            append_audit_event(
                cur, event_id="registration-" + operation_id,
                actor_role="student", actor_id=sid, action="registration.created",
                target_type="registration", target_id=registration_id,
                before={"registered_club_id": None},
                after={"registration_id": registration_id, "club_id": club_id},
                metadata={"operation_id": operation_id},
            )
            conn.commit()
            return registration_id

    @staticmethod
    def _lookup_registration(operation_id):
        """按操作代际查库：返回 registration id、False(不存在)或 None(查询失败)。"""
        try:
            with DB_POOL.connection() as conn:
                row = conn.execute(
                    "SELECT id FROM registrations WHERE operation_id=?", (operation_id,)
                ).fetchone()
        except Exception as e:  # noqa: BLE001
            log.error("按操作代际查询报名失败 op=%s: %s", operation_id, e)
            return None
        return int(row[0]) if row else False

    @staticmethod
    def _safe_rollback(sid, club_id, lease, member, absorb=False):
        try:
            RG.rollback(sid, club_id, lease, member, absorb=absorb)
        except RuntimeError as e:
            log.error("撤销抢位失败 sid=%s club=%s: %s", sid, club_id, e)
            RG.request_repair(club_id)

    @staticmethod
    def _confirm_registration(sid, club_id, mirror, lease, member):
        try:
            if RG.confirm(sid, club_id, lease, mirror, member):
                return
        except RuntimeError as e:
            log.error("报名已落库，确认镜像失败 sid=%s: %s", sid, e)
        # 租约已失效：补写镜像，库存交给单社团修复。
        try:
            RG.mirror_set(sid, "absent", mirror)
        except RuntimeError:
            pass
        RG.request_repair(club_id)

    def _registration_conflict(self, sid, club_id, operation_id, lease, member, error):
        text = str(error).lower()
        if "club full" in text:
            # 数据库已满而 Redis 放行：不归还这一个名额，用来抵消多出的库存。
            self._safe_rollback(sid, club_id, lease, member, absorb=True)
            RG.request_repair(club_id)
            return self._reply(200, "full")
        self._safe_rollback(sid, club_id, lease, member)
        try:
            with DB_POOL.connection() as conn:
                row = conn.execute(
                    "SELECT club_id, operation_id FROM registrations WHERE student_id = ?", (sid,)
                ).fetchone()
                club_exists = conn.execute(
                    "SELECT EXISTS(SELECT 1 FROM clubs WHERE id = ?)", (club_id,)).fetchone()[0]
        except Exception as e:  # noqa: BLE001
            log.error("报名冲突后的查询失败 sid=%s: %s", sid, e)
            return self._reply(503, "unavailable")
        if row:
            try:
                RG.mirror_set(sid, "absent", reservation_mirror(row[0], row[1]))
            except RuntimeError:
                pass
            if int(row[0]) != club_id:
                return self._reply(200, "registered_other")
            code = "registered" if row[1] == operation_id else "registered_already"
            return self._reply(200, code, success=True)
        if not club_exists:
            return self._reply(200, "club_missing")
        return self._reply(503, "unavailable")

    def _h_cancel_registration(self, sess, data):
        sid = int(sess["student_id"])
        if RECONCILE_REQUESTED.is_set():
            return self._reply(503, "unavailable")
        with DB_POOL.connection() as conn:
            reg = conn.execute(
                "SELECT r.id, r.club_id, r.operation_id, c.seat_sync_pending FROM registrations r "
                "JOIN clubs c ON c.id=r.club_id WHERE r.student_id=?", (sid,)).fetchone()
        if not reg:
            return self._reply(200, "not_registered")
        registration_id, club_id, operation_id, sync_pending = reg
        if sync_pending:
            return self._reply(503, "syncing")
        lease = "{}|x-{}".format(club_id, secrets.token_urlsafe(9))
        member = "{}|{}".format(sid, lease)
        try:
            code = RG.cancel_begin(sid, club_id, lease, member)
        except RuntimeError:
            return self._reply(503, "unavailable")
        if code == -6:
            return self._reply(200, "locked")
        if code == -3:
            return self._reply(503, "syncing")
        if code != 1:
            return self._reply(202, "pending")
        released = False
        try:
            if not REGISTRATION_SLOTS.acquire(timeout=REGISTER_QUEUE_TIMEOUT):
                return self._reply(503, "queued")
            try:
                outcome = self._delete_registration(sid, registration_id, club_id, operation_id)
            finally:
                REGISTRATION_SLOTS.release()
            if outcome != "deleted":
                return self._reply(503 if outcome in ("syncing", "unavailable") else 200, outcome)
            event_id = operation_id or "legacy-{}-{}-{}".format(registration_id, sid, club_id)
            try:
                RG.cancel_release(sid, club_id, reservation_mirror(club_id, operation_id),
                                  lease, member, event_id)
                released = True
            except RuntimeError as e:
                log.error("退选已落库，归还名额失败 sid=%s: %s", sid, e)
                RG.request_repair(club_id)
            SEATS_CACHE.invalidate()
            self._reply(200, "cancelled", success=True)
        finally:
            if not released:
                RG.lease_end(k_op(sid), lease, k_inflight(club_id), member)

    def _delete_registration(self, sid, registration_id, club_id, operation_id):
        with DB_POOL.connection() as conn:
            cur = conn.cursor()
            try:
                conn.execute("BEGIN IMMEDIATE")
                row = cur.execute(
                    "SELECT r.id, c.seat_sync_pending, COALESCE((SELECT registration_locked FROM settings "
                    "ORDER BY id DESC LIMIT 1), 0) FROM registrations r JOIN clubs c ON c.id=r.club_id "
                    "WHERE r.student_id = ?", (sid,)).fetchone()
                if not row or int(row[0]) != registration_id:
                    conn.rollback()
                    return "not_registered"
                if row[2]:
                    conn.rollback()
                    return "locked"
                if row[1]:
                    conn.rollback()
                    return "syncing"
                cur.execute("DELETE FROM registrations WHERE id = ? AND student_id = ?", (registration_id, sid))
                if cur.rowcount != 1:
                    conn.rollback()
                    return "not_registered"
                cur.execute("UPDATE clubs SET current_students = MAX(0, current_students - 1) WHERE id = ?",
                            (club_id,))
                append_audit_event(
                    cur, event_id="registration-cancel-" + (operation_id or "legacy-{}".format(registration_id)),
                    actor_role="student", actor_id=sid, action="registration.cancelled",
                    target_type="registration", target_id=registration_id,
                    before={"registration_id": registration_id, "club_id": club_id},
                    after={"registered_club_id": None},
                    metadata={"operation_id": operation_id} if operation_id else {},
                )
                conn.commit()
                return "deleted"
            except sqlite3.Error as e:
                conn.rollback()
                log.error("退选落库失败 sid=%s: %s", sid, e)
                return "unavailable"

    def _h_admin_change_password(self, sess, data):
        cur_pw = data.get("current") or ""
        new_pw = data.get("new") or ""
        if not isinstance(cur_pw, str) or not isinstance(new_pw, str):
            return self._json(400, {"success": False, "message": "密码格式错误"})
        if len(new_pw) < 8 or len(new_pw) > MAX_PASSWORD or len(cur_pw) > MAX_PASSWORD:
            return self._json(400, {"success": False, "message": "新密码须为 8–256 位"})
        uname = sess.get("username", "admin")
        with DB_POOL.connection() as conn:
            c = conn.cursor()
            c.execute("SELECT id, admin_password FROM settings WHERE admin_username = ?", (uname,))
            row = c.fetchone()
        verified = AUTH.run(verify_and_replace, row[1] if row else None, cur_pw, new_pw)
        if isinstance(verified, AuthBusy):
            return self._auth_busy(verified)
        ok, new_hash = verified
        if not ok:
            return self._json(400, {"success": False, "message": "当前密码不正确"})
        try:
            mutation_lock = RG.session_begin_mutation("admin", uname)
        except RuntimeError:
            return self._json(503, {"success": False, "message": "会话服务不可用，请稍后重试"})
        if not mutation_lock:
            return self._json(503, {"success": False, "message": "账号状态更新中，请稍后重试"})
        try:
            RG.session_revoke_identity("admin", uname)
            with DB_POOL.connection() as conn:
                cur = conn.cursor()
                conn.execute("BEGIN IMMEDIATE")
                cur.execute("UPDATE settings SET admin_password = ? WHERE id = ?", (new_hash, row[0]))
                write_admin_secret(new_pw)
                append_audit_event(
                    cur, event_id="admin-password-change-" + secrets.token_urlsafe(18),
                    actor_role="admin", actor_id=uname, action="admin.password_changed",
                    target_type="settings", target_id=row[0],
                    before=None, after={"password_changed": True},
                )
                conn.commit()
            self._json(200, {"success": True, "message": "管理员密码已修改，请重新登录"},
                       extra=[self._clear_cookie()])
        except RuntimeError:
            self._json(503, {"success": False, "message": "会话服务不可用，请稍后重试"})
        finally:
            RG.session_end_mutation(mutation_lock)

    def _h_reset_student_password(self, sess, data):
        student_id = data.get("student_id")
        try:
            student_id = int(student_id)
        except (TypeError, ValueError):
            return self._json(400, {"success": False, "message": "缺少或非法的学生ID"})
        request_id = admin_request_id(data)
        if request_id is None:
            return self._json(400, {"success": False, "message": "request_id 格式错误"})
        reason = clean_text(data.get("reason"), maxlen=400)
        if not reason:
            return self._json(400, {"success": False, "message": "重置原因格式错误"})
        actor = sess.get("username", "admin")
        with DB_POOL.connection() as conn:
            replay = conn.execute(
                "SELECT event_id, target_id FROM audit_events WHERE actor_role='admin' AND actor_id=? "
                "AND action='student.password_reset' AND request_id=?",
                (actor, request_id),
            ).fetchone()
            student = conn.execute("SELECT id, username, name FROM students WHERE id=?", (student_id,)).fetchone()
        if replay:
            if str(replay[1]) != str(student_id):
                return self._json(409, {"success": False,
                                        "message": "request_id 已用于其他学生的密码重置"})
            return self._json(200, {"success": True, "replayed": True, "event_id": replay[0],
                                    "request_id": request_id,
                                    "message": "该重置请求已执行，临时密码仅在首次返回时显示"})
        if not student:
            return self._json(404, {"success": False, "message": "学生不存在"})
        temporary_password = gen_password(length=12)
        password_hash = AUTH.run(hash_password, temporary_password)
        if isinstance(password_hash, AuthBusy):
            return self._auth_busy(password_hash)
        try:
            mutation_lock = RG.session_begin_mutation("student", str(student_id))
        except RuntimeError:
            return self._json(503, {"success": False, "message": "会话服务不可用，请稍后重试"})
        if not mutation_lock:
            return self._json(503, {"success": False, "message": "账号状态更新中，请稍后重试"})
        event_id = "password-reset-" + secrets.token_urlsafe(18)
        try:
            RG.session_revoke_identity("student", str(student_id))
            with DB_POOL.connection() as conn:
                cur = conn.cursor()
                conn.execute("BEGIN IMMEDIATE")
                row = cur.execute("SELECT username, name FROM students WHERE id=?", (student_id,)).fetchone()
                if not row:
                    conn.rollback()
                    return self._json(404, {"success": False, "message": "学生不存在"})
                cur.execute("UPDATE students SET password=? WHERE id=?", (password_hash, student_id))
                append_audit_event(
                    cur, event_id=event_id, actor_role="admin", actor_id=actor,
                    action="student.password_reset", target_type="student", target_id=student_id,
                    request_id=request_id, reason=reason,
                    before={"username": row[0], "name": row[1]}, after={"password_reset": True},
                )
                conn.commit()
            self._json(200, {
                "success": True, "event_id": event_id, "request_id": request_id,
                "student_id": student_id, "username": student[1],
                "temporary_password": temporary_password,
                "message": "临时密码仅显示一次",
            }, extra=[("Cache-Control", "no-store")])
        except sqlite3.IntegrityError:
            return self._json(409, {"success": False, "message": "重复的重置请求"})
        except RuntimeError:
            return self._json(503, {"success": False, "message": "会话服务不可用，请稍后重试"})
        finally:
            RG.session_end_mutation(mutation_lock)

    def _h_update_student(self, sess, data):
        """修改姓名、班级、学号；账号和密码不变。班级变化时该生重新登录。"""
        try:
            student_id = int(data.get("student_id"))
        except (TypeError, ValueError):
            return self._json(400, {"success": False, "message": "缺少或非法的学生ID"})
        request_id = admin_request_id(data)
        name = clean_text(data.get("name"))
        klass = clean_text(data.get("class"))
        number = clean_text(data.get("student_no"), maxlen=40)
        if request_id is None or not name or not klass:
            return self._json(400, {"success": False, "message": "请填写姓名和班级"})
        actor = sess.get("username", "admin")
        replay = self._admin_replay(actor, "student.updated", request_id)
        if replay:
            if str(replay[1]) != str(student_id):
                return self._json(409, {"success": False, "message": "request_id 已用于其他学生"})
            return self._json(200, {"success": True, "replayed": True, "changed": True})
        try:
            with DB_POOL.connection() as conn:
                cur = conn.cursor()
                conn.execute("BEGIN IMMEDIATE")
                row = cur.execute("SELECT name, class, student_id FROM students WHERE id=?",
                                  (student_id,)).fetchone()
                if not row:
                    conn.rollback()
                    return self._json(404, {"success": False, "message": "学生不存在"})
                number = number or row[2]
                if (name, klass, number) == tuple(row):
                    conn.rollback()
                    return self._json(200, {"success": True, "changed": False})
                cur.execute("UPDATE students SET name=?, class=?, grade=?, student_id=? WHERE id=?",
                            (name, klass, derive_grade(klass), number, student_id))
                append_audit_event(
                    cur, event_id="student-update-" + secrets.token_urlsafe(18), actor_role="admin",
                    actor_id=actor, action="student.updated", target_type="student", target_id=student_id,
                    request_id=request_id,
                    before={"name": row[0], "class": row[1], "student_no": row[2]},
                    after={"name": name, "class": klass, "student_no": number},
                )
                conn.commit()
        except sqlite3.IntegrityError:
            return self._json(409, {"success": False, "message": "学号 {} 已被其他学生使用".format(number)})
        except sqlite3.Error as e:
            log.error("修改学生失败: %s", e)
            return self._json(500, {"success": False, "message": "保存失败"})
        if klass != row[1]:
            with contextlib.suppress(RuntimeError):
                RG.session_revoke_identity("student", str(student_id))
        self._json(200, {"success": True, "changed": True})

    def _h_reset_class_passwords(self, sess, data):
        """为一个班的全部学生生成新密码；明文只在本次响应中出现。"""
        klass = clean_text(data.get("class"))
        request_id = admin_request_id(data)
        reason = clean_text(data.get("reason"), maxlen=400)
        if not klass or request_id is None or not reason:
            return self._json(400, {"success": False, "message": "请选择班级并填写原因"})
        actor = sess.get("username", "admin")
        replay = self._admin_replay(actor, "students.passwords_reset", request_id)
        if replay:
            after = json.loads(replay[3] or "{}")
            return self._json(200, {"success": True, "replayed": True, "count": after.get("count", 0),
                                    "credentials": [],
                                    "message": "该请求已执行，新密码仅在首次返回时显示"})
        with DB_POOL.connection() as conn:
            students = conn.execute(
                "SELECT id, name, class, username FROM students WHERE class=? ORDER BY student_id, id", (klass,)
            ).fetchall()
        if not students:
            return self._json(404, {"success": False, "message": "该班级没有学生"})
        passwords = [gen_password() for _ in students]
        hashes = [future.result() for future in
                  [AUTH.submit(hash_password, password, force=True)[0] for password in passwords]]
        locks = []
        try:
            for student in students:
                lock = RG.session_begin_mutation("student", str(student[0]))
                if not lock:
                    return self._json(503, {"success": False, "message": "账号状态更新中，请稍后重试"},
                                      extra=[("Retry-After", "2")])
                locks.append(lock)
            for student in students:
                RG.session_revoke_identity("student", str(student[0]))
            with DB_POOL.connection() as conn:
                cur = conn.cursor()
                conn.execute("BEGIN IMMEDIATE")
                cur.executemany("UPDATE students SET password=? WHERE id=?",
                                [(password_hash, student[0]) for password_hash, student in zip(hashes, students)])
                append_audit_event(
                    cur, event_id="class-password-reset-" + secrets.token_urlsafe(18), actor_role="admin",
                    actor_id=actor, action="students.passwords_reset", target_type="students", target_id=klass,
                    request_id=request_id, reason=reason, before={"class": klass}, after={"count": len(students)},
                )
                conn.commit()
        except RuntimeError:
            return self._json(503, {"success": False, "message": "会话服务不可用，请稍后重试"})
        except sqlite3.Error as e:
            log.error("按班重置密码失败: %s", e)
            return self._json(500, {"success": False, "message": "重置失败"})
        finally:
            for lock in locks:
                RG.session_end_mutation(lock)
        credentials = [{"name": name, "class": student_class, "username": username, "password": password}
                       for (_sid, name, student_class, username), password in zip(students, passwords)]
        self._json(200, {"success": True, "count": len(students), "credentials": credentials},
                   extra=[("Cache-Control", "no-store")])

    def _h_update_club(self, sess, data):
        club_id = data.get("club_id")
        try:
            club_id = int(club_id)
        except (TypeError, ValueError):
            return self._json(400, {"success": False, "message": "缺少或非法的社团ID"})
        request_id = admin_request_id(data)
        reason = clean_text(data.get("reason"), maxlen=400)
        if request_id is None or not reason:
            return self._json(400, {"success": False, "message": "更新参数格式错误"})
        actor = sess.get("username", "admin")
        with DB_POOL.connection() as conn:
            existing = conn.execute(
                "SELECT id, name, max_students, description, advisor_name, meeting_time, location, "
                "image_path, allowed_grades, allowed_classes, enabled, revision, seat_sync_pending "
                "FROM clubs WHERE id=?",
                (club_id,),
            ).fetchone()
            replay = conn.execute(
                "SELECT target_id, after_json FROM audit_events WHERE actor_role='admin' AND actor_id=? "
                "AND action='club.updated' AND request_id=?", (actor, request_id)
            ).fetchone()
        if replay:
            if str(replay[0]) != str(club_id):
                return self._json(409, {"success": False,
                                        "message": "request_id 已用于其他社团更新"})
            after = json.loads(replay[1]) if replay[1] else None
            # 同一请求重试：社团信息已保存，只需补发名额。
            if existing and existing[12]:
                if not repair_club(club_id):
                    return self._json(503, {"success": False,
                                            "message": "社团信息已保存，名额同步中，请稍后重试"},
                                      extra=[("Retry-After", "2")])
                if isinstance(after, dict):
                    after["seat_sync_pending"] = False
                return self._json(200, {"success": True, "replayed": True, "request_id": request_id,
                                        "club": after, "seat_sync": "repaired"})
            return self._json(200, {"success": True, "replayed": True, "request_id": request_id,
                                    "club": after, "seat_sync": "not_needed"})
        if not existing:
            return self._json(404, {"success": False, "message": "社团不存在"})
        (_id, old_name, old_max, old_description, old_advisor, old_meeting,
         old_location, _old_image_path, old_grades, old_classes, old_enabled,
         _old_revision, _old_sync_pending) = existing

        def choose_text(key, old, maxlen):
            if key not in data:
                return old
            value = data[key]
            if not isinstance(value, str):
                return None
            return clean_text(value, maxlen=maxlen) if value.strip() else ""

        name = choose_text("name", old_name, 80)
        description = choose_text("description", old_description, 1000)
        advisor_name = choose_text("advisor_name", old_advisor, 80)
        meeting_time = choose_text("meeting_time", old_meeting, 120)
        location = choose_text("location", old_location, 120)
        if (not name or not all(value is not None
                            for value in (description, advisor_name, meeting_time, location))):
            return self._json(400, {"success": False, "message": "社团信息格式错误"})
        allowed_grades = (clean_text_list(data["allowed_grades"], item_maxlen=20)
                          if "allowed_grades" in data else strict_text_list(old_grades))
        allowed_classes = (clean_text_list(data["allowed_classes"], item_maxlen=80)
                           if "allowed_classes" in data else strict_text_list(old_classes))
        if allowed_grades is None or allowed_classes is None:
            return self._json(400, {"success": False, "message": "年级或班级限制格式错误"})
        if "enabled" in data and type(data["enabled"]) is not bool:
            return self._json(400, {"success": False, "message": "enabled 必须为布尔值"})
        enabled = data.get("enabled", bool(old_enabled))
        max_students = data.get("max_students", old_max)
        if type(max_students) is not int or not 1 <= max_students <= MAX_CLUB_CAPACITY:
            return self._json(400, {"success": False, "message": "社团容量格式错误"})
        expected_revision = data.get("expected_revision")
        if expected_revision is not None and (type(expected_revision) is not int or expected_revision < 0):
            return self._json(400, {"success": False, "message": "expected_revision 格式错误"})

        capacity_changed = max_students != int(old_max)
        held = None
        if capacity_changed:
            # 只暂停这一个社团的抢位，等它的在途操作完成后再改容量。
            held = acquire_seat_locks([club_id])
            if not held:
                return self._json(503, {"success": False,
                                        "message": "该社团正在处理报名，请稍后修改容量"},
                                  extra=[("Retry-After", "2")])
        event_id = None
        try:
            with DB_POOL.connection() as conn:
                cur = conn.cursor()
                conn.execute("BEGIN IMMEDIATE")
                current = cur.execute(
                    "SELECT name, max_students, description, advisor_name, meeting_time, location, "
                    "image_path, allowed_grades, allowed_classes, enabled, revision, seat_sync_pending "
                    "FROM clubs WHERE id=?", (club_id,)
                ).fetchone()
                if not current:
                    conn.rollback()
                    return self._json(404, {"success": False, "message": "社团不存在"})
                if expected_revision is not None and current[10] != expected_revision:
                    conn.rollback()
                    return self._json(409, {"success": False, "message": "社团已被其他管理员修改",
                                            "revision": current[10]})
                used = cur.execute("SELECT COUNT(*) FROM registrations WHERE club_id=?", (club_id,)).fetchone()[0]
                if max_students < used:
                    conn.rollback()
                    return self._json(400, {"success": False, "message": "人数上限至少为已报名人数"})
                previous_max = int(current[1])
                capacity_changed = max_students != previous_max
                new_revision = int(current[10]) + 1
                cur.execute(
                    "UPDATE clubs SET name=?, max_students=?, description=?, advisor_name=?, meeting_time=?, "
                    "location=?, allowed_grades=?, allowed_classes=?, enabled=?, revision=?, "
                    "seat_revision=seat_revision + ?, seat_sync_pending=? WHERE id=?",
                    (name, max_students, description, advisor_name, meeting_time, location,
                     _audit_json(allowed_grades), _audit_json(allowed_classes), int(enabled),
                     new_revision, int(capacity_changed), int(capacity_changed or current[11]), club_id),
                )
                after = {
                    "id": club_id, "name": name, "max_students": max_students,
                    "current_students": used, "description": description,
                    "advisor_name": advisor_name, "meeting_time": meeting_time,
                    "location": location, "image_path": current[6],
                    "allowed_grades": allowed_grades, "allowed_classes": allowed_classes,
                    "enabled": enabled, "revision": new_revision,
                    "seat_sync_pending": bool(capacity_changed or current[11]),
                }
                before = {
                    "id": club_id, "name": current[0], "max_students": current[1],
                    "description": current[2], "advisor_name": current[3], "meeting_time": current[4],
                    "location": current[5], "image_path": current[6],
                    "allowed_grades": stored_text_list(current[7]),
                    "allowed_classes": stored_text_list(current[8]), "enabled": bool(current[9]),
                    "revision": current[10], "seat_sync_pending": bool(current[11]),
                }
                event_id = "club-update-" + secrets.token_urlsafe(18)
                append_audit_event(
                    cur, event_id=event_id, actor_role="admin", actor_id=actor,
                    action="club.updated", target_type="club", target_id=club_id,
                    request_id=request_id, reason=reason, before=before, after=after,
                    metadata={"capacity_delta": max_students - previous_max,
                              "seat_sync_pending": bool(capacity_changed)},
                )
                conn.commit()
            CATALOG_CACHE.invalidate()
            seat_sync = "not_needed"
            if held:
                if not publish_seat_locks(held):
                    return self._json(503, {"success": False, "event_id": event_id,
                                            "request_id": request_id, "retriable": True,
                                            "message": "社团信息已保存，名额同步中；请用同一请求编号重试"},
                                      extra=[("Retry-After", "2")])
                seat_sync = "published"
                after["seat_sync_pending"] = False
            self._json(200, {"success": True, "event_id": event_id, "request_id": request_id,
                             "club": after, "seat_sync": seat_sync})
        except sqlite3.IntegrityError:
            return self._json(409, {"success": False, "message": "重复的更新请求或社团名称冲突"})
        finally:
            release_seat_locks(held)

    def _h_upload_club_image(self, sess, raw):
        qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        try:
            club_id = int((qs.get("club_id") or [None])[0])
            expected_revision = int((qs.get("expected_revision") or [None])[0])
        except (TypeError, ValueError):
            return self._json(400, {"success": False, "message": "club_id 或 expected_revision 格式错误"})
        request_id = (qs.get("request_id") or [None])[0]
        reason = clean_text((qs.get("reason") or [None])[0], maxlen=400)
        if (not isinstance(request_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{8,128}", request_id)
                or not reason or club_id <= 0 or expected_revision < 0):
            return self._json(400, {"success": False, "message": "上传参数格式错误"})
        actor = sess.get("username", "admin")
        # A network retry must not spend another Pillow decode/write slot or
        # create a second immutable object.  Bind the idempotency key to this
        # club so a client bug cannot replay a prior image into another card.
        with DB_POOL.connection() as conn:
            replay = conn.execute(
                "SELECT target_id, after_json FROM audit_events WHERE actor_role='admin' AND actor_id=? "
                "AND action='club.image_uploaded' AND request_id=?",
                (actor, request_id),
            ).fetchone()
        if replay:
            if str(replay[0]) != str(club_id):
                return self._json(409, {"success": False,
                                        "message": "request_id 已用于其他社团图片操作"})
            return self._json(200, {"success": True, "replayed": True, "request_id": request_id,
                                    "club": json.loads(replay[1]) if replay[1] else None})
        if not IMAGE_PROCESS_SLOTS.acquire(timeout=1.0):
            return self._json(503, {"success": False, "message": "图片处理繁忙，请稍后重试"})
        try:
            normalized, extension, content_type, width, height, frames, duration_ms = _sanitize_club_image(
                raw, self.headers.get("Content-Type")
            )
        except ClubImageFeatureUnavailable as e:
            return self._json(503, {"success": False, "message": str(e)},
                              extra=[("Retry-After", "60")])
        except ClubImageError as e:
            return self._json(400, {"success": False, "message": str(e)})
        finally:
            IMAGE_PROCESS_SLOTS.release()

        digest = hashlib.sha256(normalized).hexdigest()
        # Never share a final object path across mutations.  Content-addressed
        # names plus "delete on failed transaction" can otherwise let one
        # version-conflicted upload unlink a file another request just
        # committed.  The digest remains audit metadata, not an ownership key.
        image_name = "{}-{}.{}".format(club_id, secrets.token_hex(16), extension)
        image_path = "/club-images/" + image_name
        os.makedirs(CLUB_IMAGE_DIR, mode=0o700, exist_ok=True)
        target = _club_image_disk_path(image_path)
        if target is None:
            return self._json(500, {"success": False, "message": "图片存储路径异常"})
        created_file = False
        if not os.path.exists(target):
            temp_path = target + "." + secrets.token_urlsafe(8) + ".tmp"
            try:
                with open(temp_path, "xb") as output:
                    output.write(normalized)
                    output.flush()
                    os.fsync(output.fileno())
                os.replace(temp_path, target)
                created_file = True
            except OSError:
                try:
                    os.unlink(temp_path)
                except OSError:
                    pass
                return self._json(503, {"success": False, "message": "图片保存失败，请稍后重试"})

        event_id = "club-image-" + secrets.token_urlsafe(18)
        old_image_path = None
        db_committed = False
        try:
            with DB_POOL.connection() as conn:
                cur = conn.cursor()
                conn.execute("BEGIN IMMEDIATE")
                club = cur.execute(
                    "SELECT name, max_students, description, advisor_name, meeting_time, location, "
                    "allowed_grades, allowed_classes, enabled, image_path, revision, seat_sync_pending "
                    "FROM clubs WHERE id=?", (club_id,)
                ).fetchone()
                if not club:
                    conn.rollback()
                    return self._json(404, {"success": False, "message": "社团不存在"})
                old_image_path, revision = club[9], club[10]
                if revision != expected_revision:
                    conn.rollback()
                    return self._json(409, {"success": False, "message": "社团已被其他管理员修改", "revision": revision})
                new_revision = revision + 1
                cur.execute("UPDATE clubs SET image_path=?, revision=? WHERE id=?", (image_path, new_revision, club_id))
                used = cur.execute(
                    "SELECT COUNT(*) FROM registrations WHERE club_id=?", (club_id,)
                ).fetchone()[0]
                after = {
                    "id": club_id, "name": club[0], "max_students": club[1],
                    "current_students": used, "description": club[2], "advisor_name": club[3],
                    "meeting_time": club[4], "location": club[5], "image_path": image_path,
                    "allowed_grades": stored_text_list(club[6]),
                    "allowed_classes": stored_text_list(club[7]), "enabled": bool(club[8]),
                    "revision": new_revision, "seat_sync_pending": bool(club[11]),
                }
                append_audit_event(
                    cur, event_id=event_id, actor_role="admin", actor_id=actor,
                    action="club.image_uploaded", target_type="club", target_id=club_id,
                    request_id=request_id, reason=reason,
                    before={"image_path": old_image_path, "revision": revision}, after=after,
                    metadata={"content_type": content_type, "width": width, "height": height,
                              "frames": frames, "duration_ms": duration_ms,
                              "sha256": digest},
                )
                conn.commit()
                db_committed = True
            CATALOG_CACHE.invalidate()
            if old_image_path and old_image_path != image_path:
                _remove_managed_club_image(old_image_path)
            self._json(200, {"success": True, "event_id": event_id, "request_id": request_id,
                             "club": after})
        except sqlite3.IntegrityError:
            # Concurrent delivery of the same idempotency key can race past
            # the cheap precheck.  Read the committed immutable result before
            # declaring a conflict; the losing request owns a unique orphan
            # object and its finally block will safely remove it.
            with DB_POOL.connection() as conn:
                replay = conn.execute(
                    "SELECT target_id, after_json FROM audit_events WHERE actor_role='admin' AND actor_id=? "
                    "AND action='club.image_uploaded' AND request_id=?", (actor, request_id)
                ).fetchone()
            if replay and str(replay[0]) == str(club_id):
                return self._json(200, {"success": True, "replayed": True,
                                        "request_id": request_id,
                                        "club": json.loads(replay[1]) if replay[1] else None})
            return self._json(409, {"success": False, "message": "重复的图片上传请求"})
        finally:
            if created_file and not db_committed:
                _remove_managed_club_image(image_path)

    def _h_remove_club_image(self, sess, data):
        try:
            club_id = int(data.get("club_id"))
            expected_revision = int(data.get("expected_revision"))
        except (TypeError, ValueError):
            return self._json(400, {"success": False, "message": "club_id 或 expected_revision 格式错误"})
        request_id = admin_request_id(data)
        reason = clean_text(data.get("reason"), maxlen=400)
        if request_id is None or not reason:
            return self._json(400, {"success": False, "message": "移除参数格式错误"})
        actor = sess.get("username", "admin")
        with DB_POOL.connection() as conn:
            cur = conn.cursor()
            conn.execute("BEGIN IMMEDIATE")
            replay = cur.execute(
                "SELECT target_id, after_json FROM audit_events WHERE actor_role='admin' AND actor_id=? "
                "AND action='club.image_removed' AND request_id=?", (actor, request_id)
            ).fetchone()
            if replay:
                conn.rollback()
                if str(replay[0]) != str(club_id):
                    return self._json(409, {"success": False,
                                            "message": "request_id 已用于其他社团图片操作"})
                return self._json(200, {"success": True, "replayed": True, "request_id": request_id,
                                        "club": json.loads(replay[1]) if replay[1] else None})
            club = cur.execute(
                "SELECT name, max_students, description, advisor_name, meeting_time, location, "
                "allowed_grades, allowed_classes, enabled, image_path, revision, seat_sync_pending "
                "FROM clubs WHERE id=?", (club_id,)
            ).fetchone()
            if not club:
                conn.rollback()
                return self._json(404, {"success": False, "message": "社团不存在"})
            old_image_path, revision = club[9], club[10]
            if revision != expected_revision:
                conn.rollback()
                return self._json(409, {"success": False, "message": "社团已被其他管理员修改", "revision": revision})
            new_revision = revision + 1
            cur.execute("UPDATE clubs SET image_path=NULL, revision=? WHERE id=?", (new_revision, club_id))
            event_id = "club-image-remove-" + secrets.token_urlsafe(18)
            used = cur.execute(
                "SELECT COUNT(*) FROM registrations WHERE club_id=?", (club_id,)
            ).fetchone()[0]
            after = {
                "id": club_id, "name": club[0], "max_students": club[1],
                "current_students": used, "description": club[2], "advisor_name": club[3],
                "meeting_time": club[4], "location": club[5], "image_path": None,
                "allowed_grades": stored_text_list(club[6]),
                "allowed_classes": stored_text_list(club[7]), "enabled": bool(club[8]),
                "revision": new_revision, "seat_sync_pending": bool(club[11]),
            }
            append_audit_event(
                cur, event_id=event_id, actor_role="admin", actor_id=actor,
                action="club.image_removed", target_type="club", target_id=club_id,
                request_id=request_id, reason=reason,
                before={"image_path": old_image_path, "revision": revision}, after=after,
            )
            conn.commit()
        _remove_managed_club_image(old_image_path)
        CATALOG_CACHE.invalidate()
        self._json(200, {"success": True, "event_id": event_id, "request_id": request_id,
                         "club": after})

    def _admin_registration_input(self, data, *, needs_club=True):
        student_id = data.get("student_id")
        club_id = data.get("club_id") if needs_club else None
        try:
            student_id = int(student_id)
            if needs_club:
                club_id = int(club_id)
        except (TypeError, ValueError):
            return None
        if student_id <= 0 or (needs_club and club_id <= 0):
            return None
        request_id = admin_request_id(data)
        reason = clean_text(data.get("reason"), maxlen=400)
        if request_id is None or not reason:
            return None
        if "override_eligibility" in data and type(data["override_eligibility"]) is not bool:
            return None
        return student_id, club_id, request_id, reason, bool(data.get("override_eligibility", False))

    def _snapshot_before(self, label):
        """危险操作前保存快照；失败时直接回复并返回 None。"""
        try:
            return snapshot_database(label)
        except (OSError, sqlite3.Error) as exc:
            log.error("自动备份失败: %s", exc)
            self._json(500, {"success": False, "message": "自动备份失败，操作未执行"})
            return None

    def _admin_replay(self, actor, action, request_id):
        with DB_POOL.connection() as conn:
            return conn.execute(
                "SELECT event_id, target_id, before_json, after_json FROM audit_events "
                "WHERE actor_role='admin' AND actor_id=? AND action=? AND request_id=?",
                (actor, action, request_id),
            ).fetchone()

    def _admin_replay_reply(self, replay, request_id, club_ids):
        """同一请求重试：数据已保存，只补发相关社团的名额。"""
        if all(repair_club(club_id) for club_id in club_ids if club_id):
            return self._json(200, {"success": True, "replayed": True, "event_id": replay[0],
                                    "request_id": request_id, "seat_sync": "repaired"})
        return self._json(503, {"success": False, "message": "调整已保存，名额同步中，请稍后重试"},
                          extra=[("Retry-After", "2")])

    def _admin_seat_busy(self):
        return self._json(503, {"success": False, "message": "相关学生或社团正在处理报名，请稍后重试"},
                          extra=[("Retry-After", "2")])

    def _admin_published(self, held, payload, student_mirror=None):
        if publish_seat_locks(held, student_mirror=student_mirror):
            payload["seat_sync"] = "published"
            return self._json(200, payload)
        payload.update({"success": False, "retriable": True,
                        "message": "调整已保存，名额同步中；请用同一请求编号重试"})
        return self._json(503, payload, extra=[("Retry-After", "2")])

    @staticmethod
    def _student_registration(student_id):
        with DB_POOL.connection() as conn:
            return conn.execute(
                "SELECT id, club_id, operation_id FROM registrations WHERE student_id=?", (student_id,)
            ).fetchone()

    def _h_admin_assign_registration(self, sess, data):
        parsed = self._admin_registration_input(data)
        if not parsed:
            return self._json(400, {"success": False, "message": "调整参数格式错误"})
        student_id, club_id, request_id, reason, override_eligibility = parsed
        actor = sess.get("username", "admin")
        replay = self._admin_replay(actor, "registration.admin_assigned", request_id)
        if replay:
            try:
                recorded = json.loads(replay[3]) if replay[3] else {}
                if str(replay[1]) != str(student_id) or int(recorded.get("club_id")) != club_id:
                    return self._json(409, {"success": False, "message": "request_id 已用于其他调整操作"})
            except (TypeError, ValueError):
                return self._json(409, {"success": False, "message": "request_id 对应的审计记录异常"})
            return self._admin_replay_reply(replay, request_id, (club_id,))
        held = acquire_seat_locks([club_id], student_id)
        if not held:
            return self._admin_seat_busy()
        try:
            with DB_POOL.connection() as conn:
                cur = conn.cursor()
                conn.execute("BEGIN IMMEDIATE")
                student = cur.execute("SELECT name, class, grade FROM students WHERE id=?", (student_id,)).fetchone()
                club = cur.execute(
                    "SELECT name, max_students, enabled, allowed_grades, allowed_classes FROM clubs WHERE id=?",
                    (club_id,),
                ).fetchone()
                if not student:
                    conn.rollback()
                    return self._json(404, {"success": False, "message": "学生不存在"})
                if not club:
                    conn.rollback()
                    return self._json(404, {"success": False, "message": "社团不存在"})
                if cur.execute("SELECT 1 FROM registrations WHERE student_id=?", (student_id,)).fetchone():
                    conn.rollback()
                    return self._json(409, {"success": False, "message": "该学生已报名其他社团"})
                eligible, ineligible_reason = student_matches_restrictions(student[2], student[1], club[3], club[4])
                if (not club[2] or not eligible) and not override_eligibility:
                    conn.rollback()
                    return self._json(400, {"success": False,
                                            "message": ineligible_reason or "该社团当前未开放报名"})
                used = cur.execute("SELECT COUNT(*) FROM registrations WHERE club_id=?", (club_id,)).fetchone()[0]
                if used >= club[1]:
                    conn.rollback()
                    return self._json(400, {"success": False, "message": "该社团已满员"})
                operation_id = "admin-" + secrets.token_urlsafe(18)
                cur.execute(
                    "INSERT INTO registrations (student_id, club_id, registration_time, operation_id) VALUES (?,?,?,?)",
                    (student_id, club_id, now_text(), operation_id),
                )
                registration_id = cur.lastrowid
                cur.execute("UPDATE clubs SET current_students=current_students+1, seat_sync_pending=1 WHERE id=?",
                            (club_id,))
                event_id = "admin-assign-" + secrets.token_urlsafe(18)
                append_audit_event(
                    cur, event_id=event_id, actor_role="admin", actor_id=actor,
                    action="registration.admin_assigned", target_type="student", target_id=student_id,
                    request_id=request_id, reason=reason, before={"registered_club_id": None},
                    after={"registration_id": registration_id, "club_id": club_id,
                           "operation_id": operation_id},
                    metadata={"override_eligibility": override_eligibility},
                )
                conn.commit()
            return self._admin_published(held, {
                "success": True, "event_id": event_id, "request_id": request_id,
                "registration_id": registration_id, "club_id": club_id,
            }, student_mirror=reservation_mirror(club_id, operation_id))
        except sqlite3.IntegrityError:
            return self._json(409, {"success": False, "message": "调整请求冲突"})
        finally:
            release_seat_locks(held)

    def _h_admin_remove_registration(self, sess, data):
        parsed = self._admin_registration_input(data, needs_club=False)
        if not parsed:
            return self._json(400, {"success": False, "message": "调整参数格式错误"})
        student_id, _club_id, request_id, reason, _override = parsed
        actor = sess.get("username", "admin")
        replay = self._admin_replay(actor, "registration.admin_removed", request_id)
        if replay:
            if str(replay[1]) != str(student_id):
                return self._json(409, {"success": False, "message": "request_id 已用于其他调整操作"})
            try:
                source_club_id = int((json.loads(replay[2]) if replay[2] else {}).get("club_id"))
            except (TypeError, ValueError):
                return self._json(409, {"success": False, "message": "request_id 对应的审计记录异常"})
            return self._admin_replay_reply(replay, request_id, (source_club_id,))
        registration = self._student_registration(student_id)
        if not registration:
            return self._json(404, {"success": False, "message": "该学生未报名任何社团"})
        registration_id, club_id, operation_id = registration
        held = acquire_seat_locks([club_id], student_id)
        if not held:
            return self._admin_seat_busy()
        try:
            with DB_POOL.connection() as conn:
                cur = conn.cursor()
                conn.execute("BEGIN IMMEDIATE")
                current = cur.execute(
                    "SELECT id, club_id FROM registrations WHERE student_id=?", (student_id,)
                ).fetchone()
                if not current or tuple(current) != (registration_id, club_id):
                    conn.rollback()
                    return self._json(409, {"success": False, "message": "学生报名已变化，请刷新后重试"})
                cur.execute("DELETE FROM registrations WHERE id=? AND student_id=?", (registration_id, student_id))
                cur.execute("UPDATE clubs SET current_students=MAX(0,current_students-1), seat_sync_pending=1 "
                            "WHERE id=?", (club_id,))
                event_id = "admin-remove-" + secrets.token_urlsafe(18)
                append_audit_event(
                    cur, event_id=event_id, actor_role="admin", actor_id=actor,
                    action="registration.admin_removed", target_type="student", target_id=student_id,
                    request_id=request_id, reason=reason,
                    before={"registration_id": registration_id, "club_id": club_id,
                            "operation_id": operation_id}, after={"registered_club_id": None},
                )
                conn.commit()
            return self._admin_published(held, {
                "success": True, "event_id": event_id, "request_id": request_id,
                "registration_id": registration_id, "club_id": club_id,
            }, student_mirror="")
        finally:
            release_seat_locks(held)

    def _h_admin_transfer_registration(self, sess, data):
        parsed = self._admin_registration_input(data)
        if not parsed:
            return self._json(400, {"success": False, "message": "调整参数格式错误"})
        student_id, target_club_id, request_id, reason, override_eligibility = parsed
        actor = sess.get("username", "admin")
        replay = self._admin_replay(actor, "registration.admin_transferred", request_id)
        if replay:
            try:
                before = json.loads(replay[2]) if replay[2] else {}
                after = json.loads(replay[3]) if replay[3] else {}
                if str(replay[1]) != str(student_id) or int(after.get("club_id")) != target_club_id:
                    return self._json(409, {"success": False, "message": "request_id 已用于其他调整操作"})
                clubs = (int(before.get("club_id")), int(after.get("club_id")))
            except (TypeError, ValueError):
                return self._json(409, {"success": False, "message": "request_id 对应的审计记录异常"})
            return self._admin_replay_reply(replay, request_id, clubs)
        registration = self._student_registration(student_id)
        if not registration:
            return self._json(404, {"success": False, "message": "该学生未报名任何社团"})
        registration_id, source_club_id, old_operation_id = registration
        if source_club_id == target_club_id:
            return self._json(400, {"success": False, "message": "学生已在目标社团"})
        held = acquire_seat_locks([source_club_id, target_club_id], student_id)
        if not held:
            return self._admin_seat_busy()
        try:
            with DB_POOL.connection() as conn:
                cur = conn.cursor()
                conn.execute("BEGIN IMMEDIATE")
                student = cur.execute("SELECT class, grade FROM students WHERE id=?", (student_id,)).fetchone()
                current = cur.execute(
                    "SELECT id, club_id FROM registrations WHERE student_id=?", (student_id,)
                ).fetchone()
                target = cur.execute(
                    "SELECT max_students, enabled, allowed_grades, allowed_classes FROM clubs WHERE id=?",
                    (target_club_id,),
                ).fetchone()
                if not student:
                    conn.rollback()
                    return self._json(404, {"success": False, "message": "学生不存在"})
                if not current or tuple(current) != (registration_id, source_club_id):
                    conn.rollback()
                    return self._json(409, {"success": False, "message": "学生报名已变化，请刷新后重试"})
                if not target:
                    conn.rollback()
                    return self._json(404, {"success": False, "message": "目标社团不存在"})
                eligible, ineligible_reason = student_matches_restrictions(student[1], student[0], target[2], target[3])
                if (not target[1] or not eligible) and not override_eligibility:
                    conn.rollback()
                    return self._json(400, {"success": False,
                                            "message": ineligible_reason or "目标社团当前未开放报名"})
                target_used = cur.execute("SELECT COUNT(*) FROM registrations WHERE club_id=?",
                                          (target_club_id,)).fetchone()[0]
                if target_used >= target[0]:
                    conn.rollback()
                    return self._json(400, {"success": False, "message": "目标社团已满员"})
                new_operation_id = "admin-" + secrets.token_urlsafe(18)
                cur.execute(
                    "UPDATE registrations SET club_id=?, registration_time=?, operation_id=? WHERE id=?",
                    (target_club_id, now_text(), new_operation_id, registration_id),
                )
                cur.execute("UPDATE clubs SET current_students=MAX(0,current_students-1) WHERE id=?",
                            (source_club_id,))
                cur.execute("UPDATE clubs SET current_students=current_students+1 WHERE id=?", (target_club_id,))
                cur.execute("UPDATE clubs SET seat_sync_pending=1 WHERE id IN (?,?)",
                            (source_club_id, target_club_id))
                event_id = "admin-transfer-" + secrets.token_urlsafe(18)
                append_audit_event(
                    cur, event_id=event_id, actor_role="admin", actor_id=actor,
                    action="registration.admin_transferred", target_type="student", target_id=student_id,
                    request_id=request_id, reason=reason,
                    before={"registration_id": registration_id, "club_id": source_club_id,
                            "operation_id": old_operation_id},
                    after={"registration_id": registration_id, "club_id": target_club_id,
                           "operation_id": new_operation_id},
                    metadata={"override_eligibility": override_eligibility},
                )
                conn.commit()
            return self._admin_published(held, {
                "success": True, "event_id": event_id, "request_id": request_id,
                "registration_id": registration_id, "from_club_id": source_club_id,
                "to_club_id": target_club_id,
            }, student_mirror=reservation_mirror(target_club_id, new_operation_id))
        except sqlite3.IntegrityError:
            return self._json(409, {"success": False, "message": "调整请求冲突"})
        finally:
            release_seat_locks(held)

    def _h_audit_events(self, sess):
        qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        try:
            cursor = int((qs.get("cursor") or ["0"])[0])
            limit = int((qs.get("limit") or ["50"])[0])
        except ValueError:
            return self._json(400, {"success": False, "message": "cursor 或 limit 格式错误"})
        role = (qs.get("role") or [""])[0]
        if cursor < 0 or not 1 <= limit <= 100 or role not in {"", "admin", "student"}:
            return self._json(400, {"success": False, "message": "cursor、limit 或 role 超出范围"})
        with DB_POOL.connection() as conn:
            rows = conn.execute(
                "SELECT id,event_id,occurred_at,actor_role,actor_id,action,target_type,target_id,"
                "request_id,reason,before_json,after_json,metadata_json FROM audit_events "
                "WHERE (?=0 OR id<?) AND (?='' OR actor_role=?) ORDER BY id DESC LIMIT ?",
                (cursor, cursor, role, role, limit),
            ).fetchall()
        events = [{
            "id": row[0], "event_id": row[1], "occurred_at": row[2],
            "actor_role": row[3], "actor_id": row[4], "action": row[5],
            "target_type": row[6], "target_id": row[7], "request_id": row[8],
            "reason": row[9], "before": json.loads(row[10]) if row[10] else None,
            "after": json.loads(row[11]) if row[11] else None,
            "metadata": json.loads(row[12]) if row[12] else None,
        } for row in rows]
        self._json(200, {"events": events, "next_cursor": events[-1]["id"] if events else None},
                   extra=[("Cache-Control", "no-store")])

    def _h_operations_snapshot(self, sess):
        self._json(200, operations_snapshot(), extra=[("Cache-Control", "no-store")])

    def _h_preflight_plan(self, sess):
        self._json(200, {
            "mode": "isolated-cli-only",
            "command": "python3 tests/stress_registration.py --seats 100 --users 1000 --concurrency 300",
            "requirements": [
                "使用临时 SQLite、随机 loopback Redis 与独立 Rust 端口",
                "不得使用运行中的数据库、Redis :6379、服务入口 :8080 或真实会话",
                "在部署机 CLI 或 CI 中执行，不从管理网页启动",
            ],
        }, extra=[("Cache-Control", "no-store")])

    def _h_admin_summary(self, sess):
        """工作台每 6 秒刷新一次：人数、开放社团的名额合计，以及最近一次导出、备份和数据变化的时间。"""
        actions = DATA_CHANGE_ACTIONS + ("registrations.exported", "backup.downloaded")
        with DB_POOL.connection() as conn:
            students, registered, clubs, capacity = conn.execute(
                "SELECT (SELECT COUNT(*) FROM students), (SELECT COUNT(*) FROM registrations), "
                "(SELECT COUNT(*) FROM clubs), (SELECT COALESCE(SUM(max_students), 0) FROM clubs WHERE enabled=1)"
            ).fetchone()
            latest = dict(conn.execute(
                "SELECT action, MAX(occurred_at) FROM audit_events WHERE action IN ({}) GROUP BY action".format(
                    ",".join("?" * len(actions))), actions).fetchall())
        changed = max((latest.get(action) or 0 for action in DATA_CHANGE_ACTIONS), default=0)
        self._json(200, {"students": students, "registered": registered, "clubs": clubs, "capacity": capacity,
                         "last_export": latest.get("registrations.exported"),
                         "last_backup": latest.get("backup.downloaded"), "last_change": changed or None},
                   extra=[("Cache-Control", "no-store")])

    def _record_download(self, sess, action, metadata):
        """导出与备份下载记入操作记录，工作台据此判断导出的文件是否为最新数据。"""
        try:
            with DB_POOL.connection() as conn:
                cur = conn.cursor()
                conn.execute("BEGIN IMMEDIATE")
                append_audit_event(cur, event_id="download-" + secrets.token_urlsafe(18), actor_role="admin",
                                   actor_id=sess.get("username", "admin"), action=action, target_type="data",
                                   metadata=metadata)
                conn.commit()
        except sqlite3.Error as exc:
            log.warning("记录下载失败: %s", exc)

    def _h_get_registrations(self, sess):
        with DB_POOL.connection() as conn:
            cur = conn.cursor()
            cur.execute(
                "SELECT s.name, s.class, s.student_id, c.name FROM students s "
                "LEFT JOIN registrations r ON s.id = r.student_id "
                "LEFT JOIN clubs c ON r.club_id = c.id ORDER BY s.class, s.name")
            rows = cur.fetchall()
        data = [{"name": r[0], "class": r[1], "student_id": r[2],
                 "club_name": r[3] if r[3] else "未报名"} for r in rows]
        self._json(200, data, extra=[("Cache-Control", "no-store")])

    def _h_get_all_students(self, sess):
        with DB_POOL.connection() as conn:
            cur = conn.cursor()
            cur.execute("SELECT s.id, s.name, s.class, s.grade, s.student_id, s.username, r.club_id "
                        "FROM students s LEFT JOIN registrations r ON r.student_id = s.id "
                        "ORDER BY s.class, s.name")
            rows = cur.fetchall()
        data = [{"id": r[0], "name": r[1], "class": r[2], "grade": r[3],
                 "student_id": r[4], "username": r[5], "club_id": r[6]}
                for r in rows]
        self._json(200, data, extra=[("Cache-Control", "no-store")])

    def _h_export_students_csv(self, sess):
        with DB_POOL.connection() as conn:
            rows = conn.execute("SELECT name, class, username FROM students ORDER BY class, name, id").fetchall()
        stamp = export_stamp()
        self._record_download(sess, "students.exported", {"kind": "students_csv"})
        self._csv(rows, ["姓名", "班级", "账号"], "学生账号_{}.csv".format(stamp), "students_{}.csv".format(stamp))

    @staticmethod
    def _registration_rows(conn):
        """每名学生一行：姓名、班级、社团编号、社团、方式、报名时间。"""
        rows = conn.execute(
            "SELECT s.name, s.class, c.id, c.name, r.operation_id, r.registration_time "
            "FROM students s LEFT JOIN registrations r ON s.id = r.student_id "
            "LEFT JOIN clubs c ON r.club_id = c.id ORDER BY s.class, s.name, s.id"
        ).fetchall()
        return [(name, klass, club_id, club, registration_method(operation) if club_id else "", when or "")
                for name, klass, club_id, club, operation, when in rows]

    def _h_export_all_data(self, sess):
        with DB_POOL.connection() as conn:
            rows = self._registration_rows(conn)
        stamp = export_stamp()
        self._record_download(sess, "registrations.exported", {"kind": "all_csv"})
        self._csv([(name, klass, club or "未报名", method, when)
                   for name, klass, _club_id, club, method, when in rows],
                  ["姓名", "班级", "社团", "方式", "报名时间"],
                  "报名总表_{}.csv".format(stamp), "registrations_{}.csv".format(stamp))

    def _h_export_unregistered(self, sess):
        with DB_POOL.connection() as conn:
            rows = conn.execute(
                "SELECT s.name, s.class FROM students s "
                "LEFT JOIN registrations r ON s.id = r.student_id WHERE r.id IS NULL "
                "ORDER BY s.class, s.name, s.id").fetchall()
        stamp = export_stamp()
        self._record_download(sess, "registrations.exported", {"kind": "unregistered_csv"})
        self._csv(rows, ["姓名", "班级"], "未报名名单_{}.csv".format(stamp),
                  "unregistered_{}.csv".format(stamp))

    def _h_export_club_data(self, sess):
        qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        club_id = (qs.get("club_id") or [None])[0]
        if not club_id or not str(club_id).isdigit():
            return self._json(400, {"success": False, "message": "无效的 club_id"})
        club_id = int(club_id)
        with DB_POOL.connection() as conn:
            club = conn.execute("SELECT name FROM clubs WHERE id = ?", (club_id,)).fetchone()
            if not club:
                return self._json(404, {"success": False, "message": "社团不存在"})
            rows = conn.execute(
                "SELECT s.name, s.class, r.operation_id, r.registration_time FROM students s "
                "JOIN registrations r ON s.id = r.student_id WHERE r.club_id = ? "
                "ORDER BY s.class, s.name, s.id", (club_id,)).fetchall()
        stamp = export_stamp()
        self._record_download(sess, "registrations.exported", {"kind": "club_csv", "club": club[0]})
        self._csv([(name, klass, registration_method(operation), when)
                   for name, klass, operation, when in rows],
                  ["姓名", "班级", "方式", "报名时间"],
                  "{}_名单_{}.csv".format(_FILE_NAME_BAD.sub(" ", club[0]).strip() or club_id, stamp),
                  "club_{}_{}.csv".format(club_id, stamp))

    def _h_export_workbook(self, sess):
        """报名表：总表、每个社团一张表、未报名名单（有未报名学生时）。"""
        with DB_POOL.connection() as conn:
            rows = self._registration_rows(conn)
            clubs = conn.execute("SELECT id, name FROM clubs ORDER BY id").fetchall()
        summary = [(name, klass, club or "未报名", method, when)
                   for name, klass, _club_id, club, method, when in rows]
        book = [("总表", ["姓名", "班级", "社团", "方式", "报名时间"], summary, [10, 12, 16, 10, 20])]
        for club_id, club_name in clubs:
            members = [(name, klass, method, when)
                       for name, klass, member_club, _club, method, when in rows if member_club == club_id]
            book.append((club_name, ["姓名", "班级", "方式", "报名时间"], members, [10, 12, 10, 20]))
        missing = [(name, klass) for name, klass, club_id, *_rest in rows if not club_id]
        if missing:
            book.append(("未报名", ["姓名", "班级"], missing, [10, 12]))
        stamp = export_stamp()
        self._record_download(sess, "registrations.exported", {"kind": "workbook"})
        self._send(200, xlsx_bytes(book), ctype=XLSX_TYPE,
                   extra=[attachment_header("社团报名表_{}.xlsx".format(stamp), "registrations_{}.xlsx".format(stamp)),
                          ("Cache-Control", "no-store")])

    # ---- 调剂 ----
    def _h_adjustment_plan(self, sess):
        with DB_POOL.connection() as conn:
            conn.execute("BEGIN")
            try:
                pairs, unplaced, seats = plan_adjustment(conn, rng=random.Random(0))
                names = dict(conn.execute("SELECT id, name FROM clubs").fetchall())
                locked = registration_locked(conn)
            finally:
                conn.rollback()
        planned = collections.Counter(club for _student, club in pairs)
        self._json(200, {
            "locked": locked, "unregistered": len(pairs) + len(unplaced),
            "assignable": len(pairs), "seats": sum(seats.values()),
            "clubs": [{"id": club, "name": names.get(club), "remaining": seats[club], "assign": planned.get(club, 0)}
                      for club in sorted(seats)],
        }, extra=[("Cache-Control", "no-store")])

    def _h_adjust_registrations(self, sess, data):
        request_id = admin_request_id(data)
        if request_id is None:
            return self._json(400, {"success": False, "message": "请求编号无效"})
        actor = sess.get("username", "admin")
        replay = self._admin_replay(actor, "registrations.adjusted", request_id)
        if replay:
            after = json.loads(replay[3] or "{}")
            after.pop("students", None)
            return self._json(200, dict(after, success=True, replayed=True))
        maintenance = RG.maintenance_begin()
        if not maintenance:
            return self._json(503, {"success": False, "message": "报名请求处理中，请稍后重试"},
                              extra=[("Retry-After", "2")])
        try:
            with DB_POOL.connection() as conn:
                locked_now = registration_locked(conn)
            if not locked_now:
                return self._json(409, {"success": False, "message": "请先锁定名单"})
            backup = self._snapshot_before("before-adjust")
            if backup is None:
                return
            with DB_POOL.connection() as conn:
                try:
                    conn.execute("BEGIN IMMEDIATE")
                    if not registration_locked(conn):
                        conn.rollback()
                        return self._json(409, {"success": False, "message": "请先锁定名单"})
                    pairs, unplaced, _seats = plan_adjustment(conn)
                    batch = secrets.token_urlsafe(9)
                    stamp = now_text()
                    cur = conn.cursor()
                    cur.executemany(
                        "INSERT INTO registrations (student_id, club_id, registration_time, operation_id) "
                        "VALUES (?,?,?,?)",
                        [(student, club, stamp, "adjust-{}-{}".format(batch, student)) for student, club in pairs])
                    planned = collections.Counter(club for _student, club in pairs)
                    cur.executemany("UPDATE clubs SET current_students = current_students + ? WHERE id = ?",
                                    [(count, club) for club, count in planned.items()])
                    summary = {"assigned": len(pairs), "unassigned": len(unplaced),
                               "by_club": {str(club): count for club, count in sorted(planned.items())}}
                    append_audit_event(
                        cur, event_id="adjust-" + batch, actor_role="admin", actor_id=actor,
                        action="registrations.adjusted", target_type="registrations", request_id=request_id,
                        before={"unregistered": len(pairs) + len(unplaced)},
                        after=dict(summary, students=[[student, club] for student, club in pairs]),
                        metadata={"batch": batch, "backup": backup},
                    )
                    conn.commit()
                except sqlite3.Error as e:
                    conn.rollback()
                    log.error("调剂失败: %s", e)
                    return self._json(500, {"success": False, "message": "调剂失败"})
            if not rebuild_all(maintenance):
                RECONCILE_REQUESTED.set()
                return self._json(503, dict(summary, success=False, message="调剂已保存，名额稍后发布"),
                                  extra=[("Retry-After", "2")])
            self._json(200, dict(summary, success=True, backup=backup))
        finally:
            RG.maintenance_end(maintenance)

    def _h_clear_registrations(self, sess, data):
        """名单锁定后删除全部报名，学生与社团保留；清空前自动备份。"""
        request_id = admin_request_id(data)
        if request_id is None:
            return self._json(400, {"success": False, "message": "请求编号无效"})
        actor = sess.get("username", "admin")
        replay = self._admin_replay(actor, "registrations.cleared", request_id)
        if replay:
            return self._json(200, dict(json.loads(replay[3] or "{}"), success=True, replayed=True))
        maintenance = RG.maintenance_begin()
        if not maintenance:
            return self._json(503, {"success": False, "message": "报名请求处理中，请稍后重试"},
                              extra=[("Retry-After", "2")])
        try:
            with DB_POOL.connection() as conn:
                locked_now = registration_locked(conn)
            if not locked_now:
                return self._json(409, {"success": False, "message": "请先锁定名单"})
            backup = self._snapshot_before("before-clear")
            if backup is None:
                return
            with DB_POOL.connection() as conn:
                try:
                    conn.execute("BEGIN IMMEDIATE")
                    cur = conn.cursor()
                    by_club = dict(cur.execute(
                        "SELECT club_id, COUNT(*) FROM registrations GROUP BY club_id").fetchall())
                    summary = {"cleared": sum(by_club.values()), "backup": backup}
                    cur.execute("DELETE FROM registrations")
                    cur.execute("UPDATE clubs SET current_students = 0")
                    append_audit_event(
                        cur, event_id="clear-" + secrets.token_urlsafe(18), actor_role="admin", actor_id=actor,
                        action="registrations.cleared", target_type="registrations", request_id=request_id,
                        before={"registrations": summary["cleared"],
                                "by_club": {str(club): count for club, count in sorted(by_club.items())}},
                        after=summary,
                    )
                    conn.commit()
                except sqlite3.Error as e:
                    conn.rollback()
                    log.error("清空报名失败: %s", e)
                    return self._json(500, {"success": False, "message": "清空失败"})
            if not rebuild_all(maintenance):
                RECONCILE_REQUESTED.set()
                return self._json(503, dict(summary, success=False, message="报名已清空，名额稍后发布"),
                                  extra=[("Retry-After", "2")])
            self._json(200, dict(summary, success=True))
        finally:
            RG.maintenance_end(maintenance)

    def _token_login(self):
        """启动脚本在本机打开的一次性链接：校验后签发管理员会话并转到控制台。"""
        qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        token = (qs.get("token") or [""])[0]
        target, cookies = "/", []
        try:
            local = ipaddress.ip_address(self._client_ip()).is_loopback
        except ValueError:
            local = False
        if local and re.fullmatch(r"[A-Za-z0-9_-]{40,64}", token) and RG.take(rk("admin_login", token)):
            with DB_POOL.connection() as conn:
                row = conn.execute("SELECT admin_username FROM settings ORDER BY id DESC LIMIT 1").fetchone()
            username = (row[0] if row else None) or "admin"
            try:
                session = RG.session_create({"role": "admin", "username": username},
                                            RG.session_role_epoch("admin"),
                                            RG.session_principal_version("admin", username))
            except RuntimeError:
                session = None
            if session:
                cookies.append(self._set_session_cookie(session))
                target = "/admin/dashboard"
        self._send(303, b"", ctype="text/plain; charset=utf-8",
                   extra=[("Location", target), ("Cache-Control", "no-store"),
                          ("Referrer-Policy", "no-referrer")] + cookies)

    def _h_import_parse(self, sess, raw):
        """解析上传的名单表格，逐行返回供管理员核对，不写数据库。"""
        filename = urllib.parse.unquote(self.headers.get("X-File-Name") or "")[:200]
        if not raw:
            return self._json(400, {"success": False, "message": "文件为空"})
        try:
            parsed = tables.parse_roster(filename, raw)
        except tables.TableError as exc:
            return self._json(400, {"success": False, "message": str(exc)})
        except Exception as exc:  # noqa: BLE001
            log.warning("名单解析失败 file=%s error=%r", filename, exc)
            return self._json(400, {"success": False, "message": "表格格式无法识别"})
        with DB_POOL.connection() as conn:
            existing = set(conn.execute("SELECT name, class FROM students").fetchall())
        for row in parsed["rows"]:
            if (row["name"], row["class"]) in existing:
                row["notes"].append("已导入")
        self._json(200, dict(parsed, success=True), extra=[("Cache-Control", "no-store")])

    def _h_accounts_workbook(self, sess, data):
        """把本页的新账号写成 Excel：姓名、班级、账号、密码。"""
        accounts = data.get("accounts")
        if not isinstance(accounts, list) or not 0 < len(accounts) <= MAX_ACCOUNT_ROWS:
            return self._json(400, {"success": False, "message": "账号列表无效"})
        keys = ("name", "class", "username", "password")
        rows = []
        for item in accounts:
            values = [item.get(key) for key in keys] if isinstance(item, dict) else []
            if len(values) != 4 or not all(isinstance(value, str) and 0 < len(value) <= 80 for value in values):
                return self._json(400, {"success": False, "message": "账号列表无效"})
            rows.append(dict(zip(keys, values)))
        stamp = export_stamp()
        self._record_download(sess, "students.exported", {"kind": "accounts_xlsx", "count": len(rows)})
        self._send(200, tables.accounts_xlsx(rows), ctype=XLSX_TYPE,
                   extra=[attachment_header("学生账号_{}.xlsx".format(stamp), "accounts_{}.xlsx".format(stamp)),
                          ("Cache-Control", "no-store")])

    def _h_restore(self, sess, data):
        name = data.get("name")
        if not isinstance(name, str) or not BACKUP_NAME_RE.fullmatch(name):
            return self._json(400, {"success": False, "message": "备份无效"})
        path = os.path.join(BACKUP_DIR, name)
        if not os.path.isfile(path):
            return self._json(404, {"success": False, "message": "备份不存在"})
        with tempfile.TemporaryDirectory(prefix="omms-restore-") as tmp:
            source = os.path.join(tmp, BACKUP_DB_NAME)
            copy_database(path, source)
            self._restore_from(sess, source, BACKUP_LABELS.get(BACKUP_NAME_RE.fullmatch(name).group(2), name))

    def _h_restore_upload(self, sess, raw):
        label = urllib.parse.unquote(self.headers.get("X-File-Name") or "")[:120] or "上传的备份"
        images = {}
        with tempfile.TemporaryDirectory(prefix="omms-restore-") as tmp:
            source = os.path.join(tmp, BACKUP_DB_NAME)
            try:
                if raw.startswith(b"PK\x03\x04"):
                    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
                        info = archive.getinfo(BACKUP_DB_NAME)
                        if info.file_size > MAX_RESTORE_BYTES * 4:
                            raise ValueError("too large")
                        with archive.open(info) as src, open(source, "wb") as dst:
                            dst.write(src.read(MAX_RESTORE_BYTES * 4 + 1))
                        for entry in archive.infolist():
                            base = entry.filename[len("club-images/"):] if entry.filename.startswith("club-images/") else ""
                            if _CLUB_IMAGE_NAME.fullmatch(base) and entry.file_size <= MAX_CLUB_IMAGE_OUTPUT_BYTES:
                                with archive.open(entry) as src:
                                    images[base] = src.read(MAX_CLUB_IMAGE_OUTPUT_BYTES + 1)[:MAX_CLUB_IMAGE_OUTPUT_BYTES]
                elif raw.startswith(b"SQLite format 3\x00"):
                    with open(source, "wb") as dst:
                        dst.write(raw)
                else:
                    raise ValueError("unknown format")
            except (KeyError, ValueError, OSError, zipfile.BadZipFile):
                return self._json(400, {"success": False, "message": "备份无效"})
            self._restore_from(sess, source, label, images)

    def _restore_from(self, sess, source, label, images=None):
        """在线恢复：维护期间整库替换，保留当前管理员账号，学生需重新登录。"""
        try:
            students, clubs, registrations = database_counts(source)
        except (ValueError, sqlite3.Error):
            return self._json(400, {"success": False, "message": "备份无效"})
        maintenance = RG.maintenance_begin()
        if not maintenance:
            return self._json(503, {"success": False, "message": "报名请求处理中，请稍后重试"},
                              extra=[("Retry-After", "2")])
        try:
            with DB_POOL.connection() as conn:
                admin = conn.execute(
                    "SELECT admin_username, admin_password FROM settings ORDER BY id DESC LIMIT 1").fetchone()
            saved = self._snapshot_before("before-restore")
            if saved is None:
                return
            copy_database(source, DB_PATH)
            if images:
                os.makedirs(CLUB_IMAGE_DIR, mode=0o700, exist_ok=True)
                for base, content in images.items():
                    descriptor = os.open(os.path.join(CLUB_IMAGE_DIR, base),
                                         os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
                    with os.fdopen(descriptor, "wb") as target:
                        target.write(content)
            conn = sqlite3.connect(DB_PATH, timeout=30.0)
            try:
                for kind, name in conn.execute(
                        "SELECT type, name FROM sqlite_master WHERE type IN ('trigger', 'view')").fetchall():
                    conn.execute('DROP {} IF EXISTS "{}"'.format(kind.upper(), name.replace('"', '""')))
                conn.commit()
                ensure_schema(conn)
                if admin:
                    conn.execute("UPDATE settings SET admin_username=?, admin_password=?", admin)
                conn.execute("DELETE FROM registrations WHERE student_id NOT IN (SELECT id FROM students) "
                             "OR club_id NOT IN (SELECT id FROM clubs)")
                conn.execute("UPDATE clubs SET current_students = "
                             "(SELECT COUNT(*) FROM registrations r WHERE r.club_id = clubs.id)")
                append_audit_event(
                    conn.cursor(), event_id="restore-" + secrets.token_urlsafe(18), actor_role="admin",
                    actor_id=sess.get("username", "admin"), action="backup.restored", target_type="data",
                    before={"backup": saved},
                    after={"source": label, "students": students, "clubs": clubs, "registrations": registrations},
                )
                conn.commit()
            finally:
                conn.close()
            with contextlib.suppress(RuntimeError):
                RG.session_revoke_role("student")
            CATALOG_CACHE.invalidate()
            SEATS_CACHE.invalidate()
            if not rebuild_all(maintenance):
                RECONCILE_REQUESTED.set()
            self._json(200, {"success": True, "students": students, "clubs": clubs,
                             "registrations": registrations, "backup": saved})
        finally:
            RG.maintenance_end(maintenance)

    def _h_backup(self, sess):
        """不带参数时生成完整备份；name 指定时下载该份自动快照。"""
        qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        name = (qs.get("name") or [""])[0]
        if name:
            if not BACKUP_NAME_RE.fullmatch(name):
                return self._json(400, {"success": False, "message": "备份名称无效"})
            try:
                with open(os.path.join(BACKUP_DIR, name), "rb") as source:
                    body = source.read()
            except OSError:
                return self._json(404, {"success": False, "message": "备份不存在"})
            self._record_download(sess, "backup.downloaded", {"kind": "snapshot", "name": name})
            return self._send(200, body, ctype="application/vnd.sqlite3",
                              extra=[attachment_header(name, name), ("Cache-Control", "no-store")])
        try:
            body = backup_archive()
        except (OSError, sqlite3.Error) as exc:
            log.error("生成完整备份失败: %s", exc)
            return self._json(500, {"success": False, "message": "生成备份失败"})
        stamp = export_stamp()
        self._record_download(sess, "backup.downloaded", {"kind": "full"})
        self._send(200, body, ctype="application/zip",
                   extra=[attachment_header("社团报名备份_{}.zip".format(stamp), "omms-backup_{}.zip".format(stamp)),
                          ("Cache-Control", "no-store")])

    def _h_backups(self, sess):
        items = []
        for name in list_snapshots():
            stamp, label = BACKUP_NAME_RE.fullmatch(name).groups()
            try:
                size = os.path.getsize(os.path.join(BACKUP_DIR, name))
            except OSError:
                continue
            items.append({"name": name, "label": BACKUP_LABELS.get(label, label), "size": size,
                          "time": datetime.strptime(stamp, "%Y%m%d-%H%M%S").strftime(TIME_FORMAT)})
        self._json(200, {"success": True, "backups": items, "keep": BACKUP_KEEP, "directory": BACKUP_DIR},
                   extra=[("Cache-Control", "no-store")])

    def _h_addresses(self, sess):
        """本机各网卡的局域网地址，控制台据此显示学生访问地址；端口与协议由浏览器按当前页面补全。"""
        items = [{"address": item["address"], "label": item["label"]} for item in netinfo.lan_addresses()]
        self._json(200, {"success": True, "addresses": items}, extra=[("Cache-Control", "no-store")])

    def _h_import_students(self, sess, data):
        students = data.get("students", [])
        if not isinstance(students, list) or not students:
            return self._json(400, {"success": False, "message": "学生名单为空"})
        if len(students) > MAX_IMPORT_STUDENTS:
            return self._json(400, {"success": False,
                                    "message": "单次最多导入 {} 人".format(MAX_IMPORT_STUDENTS),
                                    "max_batch": MAX_IMPORT_STUDENTS})
        results = {"success": 0, "failed": 0}
        credentials = []  # 明文只在本次响应中出现一次
        rows = []
        for st in students:
            if not isinstance(st, dict):
                results["failed"] += 1
                continue
            name = clean_text(st.get("name"))
            klass = clean_text(st.get("class"))
            student_no = clean_text(st.get("student_id"), maxlen=40)
            if not name or not klass:
                results["failed"] += 1
                continue
            rows.append((name, klass, derive_grade(klass), student_no, gen_password()))

        # 哈希在事务外由口令队列完成，写事务只做插入。
        futures = [AUTH.submit(hash_password, row[4], force=True)[0] for row in rows]
        hashes = [future.result() for future in futures]
        try:
            with DB_POOL.connection() as conn:
                cur = conn.cursor()
                conn.execute("BEGIN IMMEDIATE")
                seen = set()
                for (name, klass, grade, student_no, plain), password_hash in zip(rows, hashes):
                    username = gen_username(name, cur, seen)
                    try:
                        cur.execute(
                            "INSERT INTO students (name, class, grade, student_id, username, password) "
                            "VALUES (?,?,?,?,?,?)",
                            (name, klass, grade, student_no or username, username, password_hash))
                        results["success"] += 1
                        credentials.append({"name": name, "class": klass, "username": username,
                                            "password": plain})
                    except sqlite3.IntegrityError:
                        results["failed"] += 1  # 学号或用户名重复
                        seen.discard(username)
                append_audit_event(
                    cur, event_id="students-import-" + secrets.token_urlsafe(18),
                    actor_role="admin", actor_id=sess.get("username", "admin"),
                    action="students.imported", target_type="students",
                    before=None, after={"success": results["success"], "failed": results["failed"]},
                    metadata={"requested": len(students)},
                )
                conn.commit()
        except Exception as e:  # noqa: BLE001
            log.error("导入学生失败: %s", e)
            return self._json(500, {"success": False, "message": "导入失败"})
        self._json(200, {"success": results["success"], "failed": results["failed"],
                         "credentials": credentials}, extra=[("Cache-Control", "no-store")])

    def _h_import_clubs(self, sess, data):
        clubs = data.get("clubs", [])
        if not isinstance(clubs, list) or not clubs:
            return self._json(400, {"success": False, "message": "社团名单为空"})
        actor = sess.get("username", "admin")
        results = {"success": 0, "failed": 0}
        new_ids = []
        try:
            with DB_POOL.connection() as conn:
                cur = conn.cursor()
                conn.execute("BEGIN IMMEDIATE")
                existing = {row[0] for row in cur.execute("SELECT name FROM clubs")}
                for cb in clubs:
                    if not isinstance(cb, dict):
                        results["failed"] += 1
                        continue
                    name = clean_text(cb.get("name"))
                    maxs = cb.get("max_students")
                    if (not name or name in existing or type(maxs) is not int
                            or not 1 <= maxs <= MAX_CLUB_CAPACITY):
                        results["failed"] += 1
                        continue
                    try:
                        cur.execute("INSERT INTO clubs (name, max_students, current_students) VALUES (?,?,0)",
                                    (name, maxs))
                        new_ids.append((cur.lastrowid, maxs))
                        existing.add(name)
                        results["success"] += 1
                    except (sqlite3.IntegrityError, OverflowError, TypeError):
                        results["failed"] += 1
                append_audit_event(
                    cur, event_id="clubs-import-" + secrets.token_urlsafe(18),
                    actor_role="admin", actor_id=actor, action="clubs.imported", target_type="clubs",
                    before=None, after={"success": results["success"], "failed": results["failed"]},
                    metadata={"requested": len(clubs)},
                )
                conn.commit()
        except Exception as e:  # noqa: BLE001
            log.error("导入社团失败: %s", e)
            return self._json(500, {"success": False, "message": "导入失败"})
        CATALOG_CACHE.invalidate()
        SEATS_CACHE.invalidate()
        published = True
        for club_id, maxs in new_ids:
            try:
                RG.r.set(k_stock(club_id), maxs, nx=True)
            except Exception:  # noqa: BLE001
                published = False
                RG.request_repair(club_id)
        if not published:
            return self._json(503, {"success": results["success"], "failed": results["failed"],
                                    "message": "社团已导入，名额稍后自动发布"})
        self._json(200, {"success": results["success"], "failed": results["failed"]})

    def _h_update_time(self, sess, data):
        """start_time、end_time 出现即更新，空字符串表示清除；到截止时间后名单自动锁定。"""
        values = {}
        for field in ("start_time", "end_time"):
            if field not in data:
                continue
            text = data.get(field) or ""
            if not isinstance(text, str):
                return self._json(400, {"success": False, "message": "时间格式错误"})
            text = text.strip() or None
            if text:
                try:
                    parse_local_time(text)
                except ValueError:
                    return self._json(400, {"success": False, "message": "时间格式应为 YYYY-MM-DD HH:MM:SS"})
            values[field] = text
        if not values:
            return self._json(400, {"success": False, "message": "缺少开放时间或截止时间"})
        actor = sess.get("username", "admin")
        columns = {"start_time": ("registration_start_time", "registration.start_time_updated"),
                   "end_time": ("registration_end_time", "registration.end_time_updated")}
        try:
            with DB_POOL.connection() as conn:
                cur = conn.cursor()
                conn.execute("BEGIN IMMEDIATE")
                row = cur.execute(
                    "SELECT id, registration_start_time, registration_end_time FROM settings ORDER BY id DESC LIMIT 1"
                ).fetchone()
                if not row:
                    conn.rollback()
                    return self._json(500, {"success": False, "message": "系统设置不存在"})
                current = {"start_time": row[1], "end_time": row[2]}
                merged = dict(current, **values)
                open_at = parse_optional_time(merged["start_time"])
                close_at = parse_optional_time(merged["end_time"])
                if values.get("end_time") and close_at * 1000 <= RG.now_epoch_ms():
                    conn.rollback()
                    return self._json(400, {"success": False, "message": "截止时间应晚于当前时间"})
                if open_at is not None and close_at is not None and close_at <= open_at:
                    conn.rollback()
                    return self._json(400, {"success": False, "message": "截止时间应晚于开放时间"})
                for field, value in values.items():
                    if value == current[field]:
                        continue
                    column, action = columns[field]
                    cur.execute("UPDATE settings SET {} = ? WHERE id = ?".format(column), (value, row[0]))
                    append_audit_event(
                        cur, event_id="registration-time-" + secrets.token_urlsafe(18),
                        actor_role="admin", actor_id=actor, action=action, target_type="settings",
                        target_id=row[0], before={field: current[field]}, after={field: value},
                    )
                conn.commit()
        except sqlite3.Error as e:
            log.error("更新报名时间失败: %s", e)
            return self._json(500, {"success": False, "message": "更新失败"})
        SEATS_CACHE.invalidate()
        try:
            RG.open_at_set(open_at)
            RG.close_at_set(close_at)
        except RuntimeError as e:
            log.error("报名时间已保存，同步到 Redis 失败: %s", e)
            RECONCILE_REQUESTED.set()
            return self._json(503, {"success": False, "message": "时间已保存，正在同步"},
                              extra=[("Retry-After", "2")])
        self._json(200, {"success": True, "start_time": merged["start_time"], "end_time": merged["end_time"]})

    def _h_update_registration_lock(self, sess, data):
        locked = data.get("locked")
        if type(locked) is not bool:
            return self._json(400, {"success": False, "message": "locked 必须为布尔值"})
        request_id = admin_request_id(data)
        reason = clean_text(data.get("reason"), maxlen=400)
        if request_id is None or not reason:
            return self._json(400, {"success": False, "message": "锁定参数格式错误"})
        actor = sess.get("username", "admin")
        event_id = None
        changed = True
        clear_close = False
        try:
            with DB_POOL.connection() as conn:
                cur = conn.cursor()
                conn.execute("BEGIN IMMEDIATE")
                replay = cur.execute(
                    "SELECT after_json FROM audit_events WHERE actor_role='admin' AND actor_id=? "
                    "AND action='registration.lock_updated' AND request_id=?",
                    (actor, request_id),
                ).fetchone()
                if replay:
                    conn.rollback()
                    after = json.loads(replay[0]) if replay[0] else {"locked": locked}
                    if bool(after.get("locked")) != locked:
                        return self._json(409, {"success": False,
                                                "message": "request_id 已用于相反的锁定操作"})
                    changed = None
                else:
                    row = cur.execute(
                        "SELECT id, registration_locked FROM settings ORDER BY id DESC LIMIT 1"
                    ).fetchone()
                    if not row:
                        conn.rollback()
                        return self._json(500, {"success": False, "message": "系统设置不存在"})
                    settings_id, previous_locked = row[0], bool(row[1])
                    if previous_locked == locked:
                        conn.rollback()
                        changed = False
                    else:
                        cur.execute("UPDATE settings SET registration_locked=? WHERE id=?",
                                    (int(locked), settings_id))
                        if not locked:
                            end_row = cur.execute("SELECT registration_end_time FROM settings WHERE id=?",
                                                  (settings_id,)).fetchone()
                            if end_row and deadline_passed(parse_optional_time(end_row[0])):
                                cur.execute("UPDATE settings SET registration_end_time=NULL WHERE id=?",
                                            (settings_id,))
                                clear_close = True
                        event_id = "registration-lock-" + secrets.token_urlsafe(18)
                        append_audit_event(
                            cur, event_id=event_id, actor_role="admin", actor_id=actor,
                            action="registration.lock_updated", target_type="settings",
                            target_id=settings_id, request_id=request_id, reason=reason,
                            before={"locked": previous_locked}, after={"locked": locked},
                        )
                        conn.commit()
        except sqlite3.Error as exc:
            log.error("更新报名锁失败: %s", exc)
            return self._json(500, {"success": False, "message": "更新报名锁失败"})
        SEATS_CACHE.invalidate()
        try:
            RG.locked_set(locked)
            if clear_close:
                RG.close_at_set(None)
        except RuntimeError:
            return self._json(503, {"success": False, "request_id": request_id, "retriable": True,
                                    "message": "锁定状态已保存，正在同步；请用同一请求编号重试"},
                              extra=[("Retry-After", "2")])
        if changed is None:
            return self._json(200, {"success": True, "replayed": True, "request_id": request_id,
                                    "locked": locked})
        payload = {"success": True, "locked": locked, "changed": changed, "request_id": request_id}
        if event_id:
            payload["event_id"] = event_id
            if locked:
                try:
                    payload["backup"] = snapshot_database("locked")
                except (OSError, sqlite3.Error) as exc:
                    log.error("锁定后自动备份失败: %s", exc)
        self._json(200, payload)

    def _h_delete_student(self, sess, data):
        try:
            sid = int(data.get("student_id"))
        except (TypeError, ValueError):
            return self._json(400, {"success": False, "message": "缺少或非法的学生ID"})
        registration = self._student_registration(sid)
        held = acquire_seat_locks([registration[1]] if registration else [], sid)
        if not held:
            return self._json(503, {"success": False, "message": "该学生正在处理报名，请稍后再试"},
                              extra=[("Retry-After", "2")])
        session_lock = None
        try:
            # 先暂停该账号的会话签发并撤销旧会话，再删除数据库记录。
            try:
                session_lock = RG.session_begin_mutation("student", str(sid))
                if not session_lock:
                    return self._json(503, {"success": False, "message": "账号状态更新中，请稍后重试"})
                RG.session_revoke_identity("student", str(sid))
            except RuntimeError:
                return self._json(503, {"success": False, "message": "会话服务不可用，请稍后重试"})
            with DB_POOL.connection() as conn:
                cur = conn.cursor()
                try:
                    conn.execute("BEGIN IMMEDIATE")
                    student = cur.execute("SELECT username, name, class, student_id FROM students WHERE id=?",
                                          (sid,)).fetchone()
                    reg = cur.execute("SELECT id, club_id, operation_id FROM registrations WHERE student_id = ?",
                                      (sid,)).fetchone()
                    if not student:
                        conn.rollback()
                        return self._json(404, {"success": False, "message": "学生不存在"})
                    if reg and (not registration or int(reg[1]) != int(registration[1])):
                        conn.rollback()
                        return self._json(409, {"success": False, "message": "学生报名已变化，请刷新后重试"})
                    cur.execute("DELETE FROM registrations WHERE student_id = ?", (sid,))
                    cur.execute("DELETE FROM students WHERE id = ?", (sid,))
                    if reg:
                        cur.execute("UPDATE clubs SET current_students = MAX(0, current_students - 1), "
                                    "seat_sync_pending=1 WHERE id = ?", (reg[1],))
                    append_audit_event(
                        cur, event_id="student-delete-" + secrets.token_urlsafe(18),
                        actor_role="admin", actor_id=sess.get("username", "admin"),
                        action="student.deleted", target_type="student", target_id=sid,
                        before={"username": student[0], "name": student[1], "class": student[2],
                                "student_id": student[3], "registration": reg},
                        after={"deleted": True},
                    )
                    conn.commit()
                except sqlite3.Error as e:
                    conn.rollback()
                    log.error("删除学生失败: %s", e)
                    return self._json(500, {"success": False, "message": "删除失败"})
            if not publish_seat_locks(held, student_mirror=""):
                return self._json(503, {"success": False, "message": "学生已删除，名额稍后自动同步"})
            self._json(200, {"success": True, "message": "学生删除成功"})
        finally:
            RG.session_end_mutation(session_lock)
            release_seat_locks(held)

    def _h_delete_all_students(self, sess, data):
        maintenance = RG.maintenance_begin()
        if not maintenance:
            return self._json(503, {"success": False, "message": "报名请求处理中，请稍后重试"},
                              extra=[("Retry-After", "2")])
        session_lock = None
        try:
            if self._snapshot_before("before-delete-students") is None:
                return
            try:
                session_lock = RG.session_begin_mutation("student")
                if not session_lock:
                    return self._json(503, {"success": False, "message": "账号状态更新中，请稍后重试"})
                RG.session_revoke_role("student")
            except RuntimeError:
                return self._json(503, {"success": False, "message": "会话服务不可用，请稍后重试"})
            with DB_POOL.connection() as conn:
                try:
                    conn.execute("BEGIN IMMEDIATE")
                    cur = conn.cursor()
                    student_count = cur.execute("SELECT COUNT(*) FROM students").fetchone()[0]
                    registration_count = cur.execute("SELECT COUNT(*) FROM registrations").fetchone()[0]
                    conn.execute("DELETE FROM registrations")
                    conn.execute("DELETE FROM students")
                    conn.execute("UPDATE clubs SET current_students = 0")
                    append_audit_event(
                        cur, event_id="students-delete-all-" + secrets.token_urlsafe(18),
                        actor_role="admin", actor_id=sess.get("username", "admin"),
                        action="students.deleted_all", target_type="students",
                        before={"students": student_count, "registrations": registration_count},
                        after={"students": 0, "registrations": 0},
                    )
                    conn.commit()
                except sqlite3.Error as e:
                    conn.rollback()
                    log.error("清空学生失败: %s", e)
                    return self._json(500, {"success": False, "message": "删除失败"})
            if not rebuild_all(maintenance):
                RECONCILE_REQUESTED.set()
                return self._json(503, {"success": False, "message": "学生已删除，名额稍后自动发布"})
            self._json(200, {"success": True, "message": "所有学生数据已删除"})
        finally:
            RG.session_end_mutation(session_lock)
            RG.maintenance_end(maintenance)

    def _h_delete_club(self, sess, data):
        try:
            club_id = int(data.get("club_id"))
        except (TypeError, ValueError):
            return self._json(400, {"success": False, "message": "缺少或非法的社团ID"})
        held = acquire_seat_locks([club_id])
        if not held:
            return self._json(503, {"success": False, "message": "该社团正在处理报名，请稍后重试"},
                              extra=[("Retry-After", "2")])
        try:
            with DB_POOL.connection() as conn:
                cur = conn.cursor()
                try:
                    conn.execute("BEGIN IMMEDIATE")
                    name_row = cur.execute("SELECT name, image_path FROM clubs WHERE id=?", (club_id,)).fetchone()
                    if cur.execute("SELECT COUNT(*) FROM registrations WHERE club_id = ?",
                                   (club_id,)).fetchone()[0] > 0:
                        conn.rollback()
                        return self._json(400, {"success": False, "message": "该社团已有学生报名，无法删除"})
                    cur.execute("DELETE FROM clubs WHERE id = ?", (club_id,))
                    if cur.rowcount == 0:
                        conn.rollback()
                        return self._json(404, {"success": False, "message": "社团不存在"})
                    append_audit_event(
                        cur, event_id="club-delete-" + secrets.token_urlsafe(18),
                        actor_role="admin", actor_id=sess.get("username", "admin"),
                        action="club.deleted", target_type="club", target_id=club_id,
                        before={"name": name_row[0] if name_row else None,
                                "image_path": name_row[1] if name_row else None},
                        after={"deleted": True},
                    )
                    conn.commit()
                except sqlite3.Error as e:
                    conn.rollback()
                    log.error("删除社团失败: %s", e)
                    return self._json(500, {"success": False, "message": "删除失败"})
            if name_row and name_row[1]:
                _remove_managed_club_image(name_row[1])
            CATALOG_CACHE.invalidate()
            SEATS_CACHE.invalidate()
            if not publish_seat_locks(held):
                return self._json(503, {"success": False, "message": "社团已删除，名额状态稍后自动清理"})
            self._json(200, {"success": True, "message": "社团删除成功"})
        finally:
            release_seat_locks(held)

    def _h_delete_all_clubs(self, sess, data):
        maintenance = RG.maintenance_begin()
        if not maintenance:
            return self._json(503, {"success": False, "message": "报名请求处理中，请稍后重试"},
                              extra=[("Retry-After", "2")])
        try:
            if self._snapshot_before("before-delete-clubs") is None:
                return
            with DB_POOL.connection() as conn:
                try:
                    conn.execute("BEGIN IMMEDIATE")
                    cur = conn.cursor()
                    club_count = cur.execute("SELECT COUNT(*) FROM clubs").fetchone()[0]
                    registration_count = cur.execute("SELECT COUNT(*) FROM registrations").fetchone()[0]
                    image_paths = [row[0] for row in cur.execute(
                        "SELECT image_path FROM clubs WHERE image_path IS NOT NULL").fetchall()]
                    conn.execute("DELETE FROM registrations")
                    conn.execute("DELETE FROM clubs")
                    append_audit_event(
                        cur, event_id="clubs-delete-all-" + secrets.token_urlsafe(18),
                        actor_role="admin", actor_id=sess.get("username", "admin"),
                        action="clubs.deleted_all", target_type="clubs",
                        before={"clubs": club_count, "registrations": registration_count},
                        after={"clubs": 0, "registrations": 0},
                    )
                    conn.commit()
                except sqlite3.Error as e:
                    conn.rollback()
                    log.error("清空社团失败: %s", e)
                    return self._json(500, {"success": False, "message": "删除失败"})
            for image_path in image_paths:
                _remove_managed_club_image(image_path)
            if not rebuild_all(maintenance):
                RECONCILE_REQUESTED.set()
                return self._json(503, {"success": False, "message": "社团已删除，名额稍后自动发布"})
            self._json(200, {"success": True, "message": "所有社团数据已删除"})
        finally:
            RG.maintenance_end(maintenance)


# ==========================================================================
# 有上限的多线程服务器
# ==========================================================================
BUSY_BODY = json.dumps({"success": False, "code": "queued", "message": MESSAGES["queued"]},
                       ensure_ascii=False).encode("utf-8")


class Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True
    request_queue_size = 1024  # 抬高 listen backlog(默认 5 -> 开放瞬间不被 reset)；实际上限取系统 somaxconn

    def __init__(self, *args, **kwargs):
        self._request_slots = threading.BoundedSemaphore(MAX_HTTP_WORKERS)
        super().__init__(*args, **kwargs)

    def process_request(self, request, client_address):
        if not self._request_slots.acquire(blocking=False):
            body = BUSY_BODY
            response = (
                b"HTTP/1.1 503 Service Unavailable\r\n"
                b"Content-Type: application/json; charset=utf-8\r\n"
                b"Connection: close\r\nRetry-After: 1\r\nContent-Length: "
                + str(len(body)).encode("ascii") + b"\r\n\r\n" + body
            )
            try:
                request.sendall(response)
            except OSError:
                pass
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except Exception:
            self._request_slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._request_slots.release()


def main():
    init_db()
    reconciler = threading.Thread(target=seat_worker, name="seat-worker", daemon=True)
    reconciler.start()
    httpd = Server((HOST, PORT), ClubSystemHandler)
    log.info("Python 服务启动 http://%s:%d  (Redis=%s)", HOST, PORT,
             "on" if RG.alive() else "off/degraded")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        log.info("正在停止...")
    finally:
        httpd.shutdown()
        RECONCILE_STOP.set()
        RECONCILE_REQUESTED.set()
        reconciler.join(timeout=2.0)
        if DB_POOL:
            DB_POOL.close_all()


if __name__ == "__main__":
    arguments = sys.argv[1:]
    if len(arguments) == 2 and arguments[0] == "admin-login":
        sys.exit(cli_admin_login(arguments[1]))
    if arguments:
        print("用法：python3 main.py [admin-login 控制台地址]")
        sys.exit(2)
    main()
