#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
社团选课系统 —— Python 服务(硬化版 / 管理面 + 迁移期热路径)

设计要点(见 ~/.claude/plans/debug-bug-virtual-emerson.md):
  * 连接池:queue.Queue 阻塞池(修掉手搓池的死锁/泄漏),每连接 WAL/FK/busy_timeout/autocommit。
  * 名额:Redis 原子 Lua 抢占(并发不超卖;进程被强杀的窗口可能少卖,靠重启 rebuild 对账修正),
    SQLite 仅落库;current_students 为派生镜像。
  * 安全:argon2 口令哈希、随机每人口令、服务端 session(Redis)+ HttpOnly/SameSite Cookie、
          全接口角色鉴权、IDOR 修复(身份只取自 session)、静态白名单(消灭整库/源码下载)、
          import 入库消毒(防存储型 XSS)、登录限流。
  * Redis 不可用:写端点(报名/退选)拒绝(绝不回落无锁路径);只读端点回落 SQLite。

与 Rust 热服务(club-hot)共享同一 SQLite 文件与 Redis(键契约一致),可被 nginx 按路由灰度替换。
"""

import http.server
import socketserver
import json
import sqlite3
import urllib.parse
import os
import secrets
import io
import csv
import queue
import contextlib
import threading
import logging
import time
import ipaddress
from datetime import datetime

# ---- 可选重依赖(有 fallback 不致命) -------------------------------------
try:
    import redis as _redis
except ImportError:
    _redis = None

try:
    from argon2 import PasswordHasher
    from argon2.exceptions import VerifyMismatchError, InvalidHashError
    _PH = PasswordHasher()  # 默认参数即交互档
except ImportError:  # pragma: no cover
    _PH = None

try:
    from pypinyin import lazy_pinyin  # noqa: F401  (导入学生用户名时可用,缺失有 fallback)
    HAS_PYPINYIN = True
except ImportError:
    HAS_PYPINYIN = False

# ---- 配置(环境变量可覆盖) ------------------------------------------------
DB_PATH = os.environ.get("DB_PATH", "club_system.db")
REDIS_URL = os.environ.get("REDIS_URL", "redis://127.0.0.1:6379/0")
HOST = os.environ.get("HOST", "127.0.0.1")   # 默认仅本机;公网经 nginx 反代
PORT = int(os.environ.get("PORT", "2001"))
POOL_SIZE = max(1, int(os.environ.get("DB_POOL_SIZE", "12")))
SESSION_TTL = max(60, int(os.environ.get("SESSION_TTL", str(8 * 3600))))
RESV_TTL = max(1, int(os.environ.get("RESV_TTL", "60"))) # 抢占预留 TTL(秒),需覆盖最坏落库时间
LOGIN_MAX_FAILS = max(1, int(os.environ.get("LOGIN_MAX_FAILS", "10")))
MAX_BODY = max(1024, int(os.environ.get("MAX_BODY", str(8 * 1024 * 1024))))
AUTH_CONCURRENCY = max(1, int(os.environ.get("AUTH_CONCURRENCY", "4")))
REGISTER_CONCURRENCY = max(1, int(os.environ.get("REGISTER_CONCURRENCY", "1")))
REGISTER_QUEUE_TIMEOUT = max(0.1, float(os.environ.get("REGISTER_QUEUE_TIMEOUT", "10")))
MAX_HTTP_WORKERS = max(8, int(os.environ.get("MAX_HTTP_WORKERS", "128")))
SEAT_OP_TTL = max(60, int(os.environ.get("SEAT_OP_TTL", "120")))
MAINTENANCE_TTL = max(60, int(os.environ.get("MAINTENANCE_TTL", "300")))
SESSION_MUTATION_TTL = 60
MAX_USERNAME = 80
MAX_PASSWORD = 256
MAX_IMPORT_STUDENTS = max(1, int(os.environ.get("MAX_IMPORT_STUDENTS", "5000")))
# A school club cannot meaningfully hold more than this; keeping the bound
# below SQLite's integer range also makes import failure per-row and predictable.
MAX_CLUB_CAPACITY = 10_000
COOKIE_SECURE = os.environ.get("COOKIE_SECURE", "0").strip().lower() in {"1", "true", "yes", "on"}

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("club")

# Redis 键契约(与 Rust 热服务一致)
K_STOCK = "stock:club:{}"        # 剩余名额(热准入计数;SQLite trigger 是最终容量底线)
K_STUREG = "student:reg:{}"      # 已确认报名 -> club_id|operation_id
K_RESV = "resv:{}"               # 抢占预留态 -> club_id|operation_id (TTL)
K_SESS = "sess:{}"               # 会话 token -> JSON
K_SESS_EPOCH = "sess:epoch:{}"   # role-wide generation; delete-all makes old sessions invalid
K_SESS_VERSION = "sess:version:{}:{}"  # principal generation; login/password change/delete revokes old tokens
K_SESS_ROLE_MUTATION = "sess:mutation:role:{}"
K_SESS_PRINCIPAL_MUTATION = "sess:mutation:{}:{}"
K_OPENAT = "open_at"             # 报名开放 epoch 秒
K_CACHE_CLUBS = "cache:clubs"
K_INIT = "seats:initialized"
K_MAINT = "seats:maintenance"
K_OP = "seat:op:{}"

# 抢名额 Lua:开放时间检查、维护闸门、查重与扣减在同一个 Redis 原子操作内。
# KEYS=stock/student:reg/resv/open_at/maintenance/seat:op;
# ARGV=reservation_value/reservation_ttl/operation_ttl。
# 返回 1 成功 / 0 满员 / -1 已报名 / -2 库存未初始化 / -3 维护中 / -4 未开放。
LUA_ACQUIRE = """
local reservation_ttl = tonumber(ARGV[2])
local operation_ttl = tonumber(ARGV[3])
if not reservation_ttl or reservation_ttl <= 0 or not operation_ttl or operation_ttl <= 0 then return -5 end
if redis.call('GET', KEYS[6]) == ARGV[1] then
  if redis.call('GET', KEYS[3]) ~= ARGV[1] then
    redis.call('SET', KEYS[3], ARGV[1], 'EX', reservation_ttl)
  end
  return 1
end
if redis.call('EXISTS', KEYS[5]) == 1 then return -3 end
local open_at = redis.call('GET', KEYS[4])
if not open_at then return -2 end
local now = redis.call('TIME')
if tonumber(now[1]) < tonumber(open_at) then return -4 end
if redis.call('EXISTS', KEYS[1]) == 0 then return -2 end
if redis.call('EXISTS', KEYS[2]) == 1 then return -1 end
if redis.call('EXISTS', KEYS[3]) == 1 then return -1 end
if redis.call('EXISTS', KEYS[6]) == 1 then return -1 end
local left = tonumber(redis.call('GET', KEYS[1]))
if left <= 0 then return 0 end
local resv_set = redis.pcall('SET', KEYS[3], ARGV[1], 'EX', reservation_ttl)
if type(resv_set) == 'table' and resv_set.err then return -5 end
local op_set = redis.pcall('SET', KEYS[6], ARGV[1], 'EX', operation_ttl)
if type(op_set) == 'table' and op_set.err then
  redis.call('DEL', KEYS[3])
  return -5
end
local decremented = redis.pcall('DECR', KEYS[1])
if type(decremented) == 'table' and decremented.err then
  redis.call('DEL', KEYS[3])
  redis.call('DEL', KEYS[6])
  return -5
end
return 1
"""

# 旧请求只能确认/回滚自己的 reservation,不能删除 TTL 后出现的新一代状态。
LUA_CONFIRM = """
if redis.call('GET', KEYS[2]) == ARGV[1] then
  if redis.call('GET', KEYS[1]) == ARGV[1] then redis.call('DEL', KEYS[1]) end
  if redis.call('GET', KEYS[3]) == ARGV[1] then redis.call('DEL', KEYS[3]) end
  return 1
end
if redis.call('GET', KEYS[1]) ~= ARGV[1] and redis.call('GET', KEYS[3]) ~= ARGV[1] then return 0 end
redis.call('SET', KEYS[2], ARGV[1])
if redis.call('GET', KEYS[1]) == ARGV[1] then redis.call('DEL', KEYS[1]) end
if redis.call('GET', KEYS[3]) == ARGV[1] then redis.call('DEL', KEYS[3]) end
return 1
"""

LUA_ROLLBACK = """
local stock = redis.call('GET', KEYS[1])
if not stock or not tonumber(stock) then return -1 end
if redis.call('EXISTS', KEYS[3]) == 1 then return 0 end
redis.call('SET', KEYS[3], '1', 'EX', 604800)
redis.call('INCR', KEYS[1])
if redis.call('GET', KEYS[2]) == ARGV[1] then redis.call('DEL', KEYS[2]) end
if redis.call('GET', KEYS[4]) == ARGV[1] then redis.call('DEL', KEYS[4]) end
return 1
"""

# 仅释放与已删除 SQLite registration 同一 operation 的确认态/预留态。
LUA_CANCEL = """
local stock = redis.call('GET', KEYS[1])
if not stock or not tonumber(stock) then return -1 end
if redis.call('EXISTS', KEYS[4]) == 1 then return 0 end
redis.call('SET', KEYS[4], '1', 'EX', 604800)
local confirmed = redis.call('GET', KEYS[2])
if confirmed == ARGV[1] or confirmed == ARGV[2] then redis.call('DEL', KEYS[2]) end
if redis.call('GET', KEYS[3]) == ARGV[1] then redis.call('DEL', KEYS[3]) end
redis.call('INCR', KEYS[1])
return 1
"""

LUA_MAINT_END = """
if redis.call('GET', KEYS[1]) == ARGV[1] then return redis.call('DEL', KEYS[1]) end
return 0
"""

LUA_STUDENT_OP_BEGIN = """
if redis.call('GET', KEYS[2]) == ARGV[1] then return 1 end
if redis.call('EXISTS', KEYS[1]) == 1 then return -1 end
if redis.call('SET', KEYS[2], ARGV[1], 'NX', 'EX', tonumber(ARGV[2])) then return 1 end
return 0
"""

# 会话创建必须和“当前帐号/角色代际”比较并在同一 Redis 脚本内递增代际。
# 登录读到学生记录后，若管理员在此期间删除学生、清空账号或修改密码，脚本
# 会拒绝下发新会话，避免旧 DB 读取在删除后重新激活一个 token。
# KEYS = role epoch / principal version / session token / role mutation lock /
# principal mutation lock; ARGV = JSON / TTL / expected role epoch / expected
# principal version。
LUA_SESSION_CREATE = """
local epoch = tonumber(redis.call('GET', KEYS[1]) or '0')
local version = tonumber(redis.call('GET', KEYS[2]) or '0')
if not epoch or not version then return -1 end
if redis.call('EXISTS', KEYS[4]) == 1 or redis.call('EXISTS', KEYS[5]) == 1 then return -2 end
if epoch ~= tonumber(ARGV[3]) or version ~= tonumber(ARGV[4]) then return 0 end
local next_version = version + 1
local payload = cjson.decode(ARGV[1])
payload['_session_epoch'] = epoch
payload['_session_version'] = next_version
local created = redis.pcall('SET', KEYS[3], cjson.encode(payload), 'EX', tonumber(ARGV[2]))
if type(created) == 'table' and created.err then return -3 end
local advanced = redis.pcall('INCR', KEYS[2])
if type(advanced) == 'table' and advanced.err then
  redis.call('DEL', KEYS[3])
  return -3
end
if tonumber(advanced) ~= next_version then
  redis.call('DEL', KEYS[3])
  return -3
end
return 1
"""

# 失败计数的过期时间只在首次失败时设置，攻击流量不能借由每次失败无限续期。
LUA_LOGIN_FAIL = """
local n = redis.call('INCR', KEYS[1])
if n == 1 then redis.call('EXPIRE', KEYS[1], tonumber(ARGV[1])) end
return n
"""


def _session_identity(payload):
    """Return the stable Redis principal tuple for a session payload.

    The two backends deliberately use only the durable student primary key or
    the administrator username here.  User-Agent/IP are useful audit metadata,
    but are not reliable authentication factors on a school network.
    """
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
    """Parse an untrusted Redis counter without accepting bool/float values."""
    try:
        parsed = int(value) if value is not None else 0
    except (TypeError, ValueError):
        raise RuntimeError("invalid session generation")
    if parsed < 0:
        raise RuntimeError("invalid session generation")
    return parsed


def _session_cookie_token(raw):
    """Return one unambiguous session cookie, or ``None``.

    SimpleCookie keeps the last duplicate name while Rust historically picked
    the first.  Rejecting duplicate `session` names is deterministic for both
    services and prevents a second cookie from changing the authenticated
    identity, logout target, or edge limiter bucket.
    """
    if not raw:
        return None
    token = None
    for part in raw.split(";"):
        if "=" not in part:
            continue
        name, value = part.split("=", 1)
        if name.strip() != "session":
            continue
        value = value.strip()
        if token is not None or not value:
            return None
        token = value
    return token


def _session_cookie_token_from_headers(headers):
    """Return a session token only when exactly one Cookie field was received.

    HTTP permits repeated field lines in general, but accepting a first Cookie
    line while ignoring later ones weakens the explicit single-cookie invariant
    and makes direct/backend behavior depend on proxy normalization.
    """
    values = headers.get_all("Cookie") or []
    if len(values) != 1:
        return None
    return _session_cookie_token(values[0])

# ==========================================================================
# Redis 客户端(优雅降级:不可用时读路径回落、写路径拒绝)
# ==========================================================================
class RedisGate:
    def __init__(self, url):
        self._url = url
        self._r = None
        self._acquire = None
        self._confirm = None
        self._rollback = None
        self._cancel = None
        self._maint_end = None
        self._student_op_begin = None
        self._session_create = None
        self._login_fail_script = None
        if _redis is not None:
            try:
                self._r = _redis.Redis.from_url(
                    url, decode_responses=True,
                    socket_timeout=0.5, socket_connect_timeout=0.5,
                )
                self._r.ping()
                self._acquire = self._r.register_script(LUA_ACQUIRE)
                self._confirm = self._r.register_script(LUA_CONFIRM)
                self._rollback = self._r.register_script(LUA_ROLLBACK)
                self._cancel = self._r.register_script(LUA_CANCEL)
                self._maint_end = self._r.register_script(LUA_MAINT_END)
                self._student_op_begin = self._r.register_script(LUA_STUDENT_OP_BEGIN)
                self._session_create = self._r.register_script(LUA_SESSION_CREATE)
                self._login_fail_script = self._r.register_script(LUA_LOGIN_FAIL)
                log.info("Redis 已连接: %s", url)
            except Exception as e:  # noqa: BLE001
                log.warning("Redis 连接失败(将降级): %s", e)
                self._r = None
        else:
            log.warning("redis 模块未安装,名额/会话能力降级")

    @property
    def r(self):
        return self._r

    def alive(self):
        if self._r is None:
            return False
        try:
            self._r.ping()
            return True
        except Exception:  # noqa: BLE001
            return False

    @staticmethod
    def reservation_value(club_id, operation_id):
        return "{}|{}".format(int(club_id), operation_id)

    def acquire_seat(self, student_id, club_id, reservation_value):
        """原子抢占。返回 1/0/-1/-2;Redis 不可用抛 RuntimeError。"""
        if self._r is None or self._acquire is None:
            raise RuntimeError("redis unavailable")
        last_error = None
        for _ in range(2):
            try:
                return int(self._acquire(
                    keys=[K_STOCK.format(club_id), K_STUREG.format(student_id),
                          K_RESV.format(student_id), K_OPENAT, K_MAINT,
                          K_OP.format(student_id)],
                    args=[reservation_value, RESV_TTL, SEAT_OP_TTL],
                ))
            except Exception as e:  # noqa: BLE001
                last_error = e
        try:
            if self._r.get(K_OP.format(student_id)) == reservation_value:
                return 1
        except Exception as e:  # noqa: BLE001
            last_error = e
        raise RuntimeError("redis unavailable") from last_error

    def confirm_seat(self, student_id, reservation_value):
        """CAS 确认自己的 reservation;返回是否确认,依赖失败抛 RuntimeError。"""
        last_error = None
        for _ in range(2):
            try:
                return bool(self._confirm(
                    keys=[K_RESV.format(student_id), K_STUREG.format(student_id),
                          K_OP.format(student_id)],
                    args=[reservation_value],
                ))
            except Exception as e:  # noqa: BLE001
                last_error = e
        raise RuntimeError("redis unavailable") from last_error

    def rollback_reservation(self, student_id, club_id, reservation_value):
        """仅补偿自己的未落库 reservation;幂等且不会删除确认态。"""
        last_error = None
        for _ in range(2):
            try:
                changed = int(self._rollback(
                    keys=[K_STOCK.format(club_id), K_RESV.format(student_id),
                          "seat:rollback:{}".format(reservation_value), K_OP.format(student_id)],
                    args=[reservation_value],
                ))
                if changed < 0:
                    raise RuntimeError("invalid stock state")
                return bool(changed)
            except Exception as e:  # noqa: BLE001
                last_error = e
        raise RuntimeError("redis unavailable") from last_error

    def release_registration(self, event_id, student_id, club_id, reservation_value):
        """释放已从 SQLite 删除的同一 registration;CAS 保证最多归还一次。"""
        last_error = None
        for _ in range(2):
            try:
                changed = int(self._cancel(
                    keys=[K_STOCK.format(club_id), K_STUREG.format(student_id),
                          K_RESV.format(student_id), "seat:cancel:{}".format(event_id)],
                    args=[reservation_value, str(club_id)],
                ))
                if changed < 0:
                    raise RuntimeError("invalid stock state")
                return bool(changed)
            except Exception as e:  # noqa: BLE001
                last_error = e
        raise RuntimeError("redis unavailable") from last_error

    def begin_maintenance(self):
        """阻止新抢占;若已有在途 reservation 则立即撤销并返回 None。"""
        if not self.alive():
            return None
        token = secrets.token_urlsafe(18)
        try:
            acquired = False
            for _ in range(2):
                try:
                    if self._r.set(K_MAINT, token, nx=True, ex=MAINTENANCE_TTL):
                        acquired = True
                        break
                    acquired = self._r.get(K_MAINT) == token
                    break
                except Exception:  # noqa: BLE001
                    continue
            if not acquired:
                return None
            has_resv = next(self._r.scan_iter(match="resv:*", count=1), None) is not None
            has_other_op = next(
                self._r.scan_iter(match="seat:op:*", count=1), None) is not None
            if has_resv or has_other_op:
                self.end_maintenance(token)
                return None
            return token
        except Exception:  # noqa: BLE001
            self.end_maintenance(token)
            return None

    def end_maintenance(self, token):
        if not token or self._maint_end is None:
            return
        try:
            self._maint_end(keys=[K_MAINT], args=[token])
        except Exception:  # noqa: BLE001
            pass

    def maintenance_owned(self, token):
        try:
            return bool(token) and self._r.get(K_MAINT) == token
        except Exception:  # noqa: BLE001
            return False

    def begin_student_op(self, student_id):
        """报名以外的学生写操作也加入跨服务维护协议。"""
        if self._student_op_begin is None:
            return None
        token = secrets.token_urlsafe(18)
        for _ in range(2):
            try:
                result = int(self._student_op_begin(
                    keys=[K_MAINT, K_OP.format(student_id)],
                    args=[token, SEAT_OP_TTL],
                ))
                return token if result == 1 else None
            except Exception:  # noqa: BLE001
                continue
        return None

    def end_student_op(self, student_id, token):
        if not token or self._maint_end is None:
            return
        try:
            self._maint_end(keys=[K_OP.format(student_id)], args=[token])
        except Exception:  # noqa: BLE001
            pass

    def stock_left(self, club_ids):
        """批量取剩余名额 dict{club_id:int};不可用返回 None。"""
        if self._r is None or not club_ids:
            return None
        try:
            vals = self._r.mget([K_STOCK.format(c) for c in club_ids])
            return {c: (int(v) if v is not None else None) for c, v in zip(club_ids, vals)}
        except Exception:  # noqa: BLE001
            return None

    def has_active_operations(self):
        if self._r is None:
            return None
        try:
            return next(self._r.scan_iter(match="seat:op:*", count=128), None) is not None
        except Exception:  # noqa: BLE001
            return None

    def now_epoch(self):
        """统一时钟:优先 Redis TIME,回落本机。"""
        if self._r is not None:
            try:
                sec, _usec = self._r.time()
                return int(sec)
            except Exception:  # noqa: BLE001
                pass
        return int(time.time())

    # 会话
    def session_role_epoch(self, role):
        if self._r is None:
            raise RuntimeError("redis unavailable")
        try:
            return _counter_value(self._r.get(K_SESS_EPOCH.format(role)))
        except RuntimeError:
            raise
        except Exception as e:  # noqa: BLE001
            raise RuntimeError("redis unavailable") from e

    def session_principal_version(self, role, principal):
        if self._r is None:
            raise RuntimeError("redis unavailable")
        try:
            return _counter_value(self._r.get(K_SESS_VERSION.format(role, principal)))
        except RuntimeError:
            raise
        except Exception as e:  # noqa: BLE001
            raise RuntimeError("redis unavailable") from e

    def session_fence(self, role, principal):
        """Read the role/account generations used to fence a later login."""
        if self._r is None:
            raise RuntimeError("redis unavailable")
        try:
            epoch, version = self._r.mget([
                K_SESS_EPOCH.format(role), K_SESS_VERSION.format(role, principal),
            ])
            return _counter_value(epoch), _counter_value(version)
        except RuntimeError:
            raise
        except Exception as e:  # noqa: BLE001
            raise RuntimeError("redis unavailable") from e

    def session_create(self, payload, expected_epoch, expected_version):
        """Create one active session if its DB-read generation is still current.

        A successful login increments the principal generation.  Therefore a
        second login, password change, student deletion, or delete-all makes
        every older token immediately unusable in both Python and Rust, even if
        the old `sess:*` value has not expired yet.
        """
        identity = _session_identity(payload)
        if identity is None or self._r is None or self._session_create is None:
            raise RuntimeError("redis unavailable")
        role, principal = identity
        token = secrets.token_urlsafe(32)
        try:
            result = int(self._session_create(
                keys=[K_SESS_EPOCH.format(role), K_SESS_VERSION.format(role, principal),
                      K_SESS.format(token), K_SESS_ROLE_MUTATION.format(role),
                      K_SESS_PRINCIPAL_MUTATION.format(role, principal)],
                args=[json.dumps(payload, ensure_ascii=False, separators=(",", ":")), SESSION_TTL,
                      expected_epoch, expected_version],
            ))
            if result == 1:
                return token
            if result in (0, -2):
                return None
            raise RuntimeError("invalid session generation")
        except RuntimeError:
            raise
        except Exception as e:  # noqa: BLE001
            log.error("session_create 失败: %s", e)
            raise RuntimeError("redis unavailable") from e

    def session_get(self, token):
        if not token:
            return None
        if self._r is None:
            raise RuntimeError("redis unavailable")
        try:
            raw = self._r.get(K_SESS.format(token))
            if not raw:
                return None
            payload = json.loads(raw)
            identity = _session_identity(payload)
            if identity is None:
                return None
            epoch = payload.get("_session_epoch")
            version = payload.get("_session_version")
            if type(epoch) is not int or type(version) is not int:
                return None  # sessions issued before this security migration re-login once
            role, principal = identity
            current_epoch, current_version = self.session_fence(role, principal)
            if epoch != current_epoch or version != current_version:
                return None
            return payload
        except (TypeError, ValueError, json.JSONDecodeError):
            return None
        except RuntimeError:
            raise
        except Exception as e:  # noqa: BLE001
            raise RuntimeError("redis unavailable") from e

    def session_del(self, token):
        if not token:
            return
        if self._r is not None:
            try:
                self._r.delete(K_SESS.format(token))
            except Exception:  # noqa: BLE001
                pass

    def session_revoke_identity(self, role, principal):
        """Invalidate all sessions for one account before a sensitive mutation."""
        if self._r is None:
            raise RuntimeError("redis unavailable")
        try:
            self._r.incr(K_SESS_VERSION.format(role, principal))
        except Exception as e:  # noqa: BLE001
            raise RuntimeError("redis unavailable") from e

    def session_begin_mutation(self, role, principal=None):
        """Fence session creation while the matching SQLite mutation commits."""
        if self._r is None:
            raise RuntimeError("redis unavailable")
        key = (K_SESS_ROLE_MUTATION.format(role) if principal is None
               else K_SESS_PRINCIPAL_MUTATION.format(role, principal))
        token = secrets.token_urlsafe(18)
        try:
            if self._r.set(key, token, nx=True, ex=SESSION_MUTATION_TTL):
                return key, token
            return None
        except Exception as e:  # noqa: BLE001
            raise RuntimeError("redis unavailable") from e

    def session_end_mutation(self, lock):
        if not lock or self._maint_end is None:
            return
        key, token = lock
        try:
            self._maint_end(keys=[key], args=[token])
        except Exception:  # noqa: BLE001
            pass

    def session_revoke_role(self, role):
        """Invalidate every session of a role in O(1), used by delete-all."""
        if self._r is None:
            raise RuntimeError("redis unavailable")
        try:
            self._r.incr(K_SESS_EPOCH.format(role))
        except Exception as e:  # noqa: BLE001
            raise RuntimeError("redis unavailable") from e

    def login_blocked(self, key, limit):
        """只读检查:失败计数是否超限(成功登录不计数,避免校园 NAT 误伤)。"""
        if self._r is None:
            return False
        try:
            n = self._r.get("loginfail:{}".format(key))
            return int(n or 0) >= limit
        except Exception:  # noqa: BLE001
            return False

    def login_fail(self, key):
        """仅在登录失败时计数。"""
        if self._r is None or self._login_fail_script is None:
            return
        try:
            self._login_fail_script(keys=["loginfail:{}".format(key)], args=[60])
        except Exception:  # noqa: BLE001
            pass

    def login_ok(self, key):
        """登录成功清零该用户失败计数。"""
        if self._r is not None:
            try:
                self._r.delete("loginfail:{}".format(key))
            except Exception:  # noqa: BLE001
                pass

    def open_at_set(self, epoch, nx=False):
        if self._r is None:
            raise RuntimeError("redis unavailable")
        try:
            return self._r.set(K_OPENAT, int(epoch), nx=nx) is not None
        except Exception as e:  # noqa: BLE001
            raise RuntimeError("redis unavailable") from e

    def open_at_get(self):
        if self._r is not None:
            try:
                v = self._r.get(K_OPENAT)
                return int(v) if v is not None else None
            except Exception:  # noqa: BLE001
                pass
        return None

    def cache_del(self, *keys):
        if self._r is not None:
            try:
                self._r.delete(*keys)
            except Exception:  # noqa: BLE001
                pass


RG = RedisGate(REDIS_URL)
AUTH_SLOTS = threading.BoundedSemaphore(AUTH_CONCURRENCY)
REGISTRATION_SLOTS = threading.BoundedSemaphore(REGISTER_CONCURRENCY)
RECONCILE_REQUESTED = threading.Event()
RECONCILE_STOP = threading.Event()


# ==========================================================================
# 口令哈希(argon2;兼容存量明文,登录时就地升级)
# ==========================================================================
def hash_password(plain):
    if _PH is None:  # 极端 fallback:不应发生(argon2-cffi 已装)
        return "plain$" + plain
    return _PH.hash(plain)


def verify_password(stored, plain):
    """返回 (ok: bool, needs_upgrade: bool)。"""
    if stored is None:
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
    # 存量明文(如 "123456"):明文比对,通过则需升级
    return secrets.compare_digest(stored, plain), True


def gen_password():
    """随机每人口令(避免易混字符);明文仅用于一次性下发,入库存哈希。"""
    alphabet = "ABCDEFGHJKMNPQRSTUVWXYZabcdefghjkmnpqrstuvwxyz23456789"
    return "".join(secrets.choice(alphabet) for _ in range(8))


# ==========================================================================
# 输入消毒(服务端防存储型 XSS:拒绝危险字符,不改数据语义)
# ==========================================================================
# 仅拒尖括号与控制字符:前端一律用 textContent 输出、SQL 全参数化、CSV 走 csv 模块,
# 故 & ' " 等在合法姓名里(如 王&李、O'Brien)是安全的,放行以免静默丢学生。
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
        conn.execute("PRAGMA synchronous=NORMAL")
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
            conn.rollback()  # 清残留事务,防脏连接
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
# 用户名生成(复用调用者连接 + 同事务/本批去重,原子)
# ==========================================================================
def gen_username(name, cursor, seen):
    raw = str(name).strip()
    if HAS_PYPINYIN:
        base = "".join(c for c in "".join(lazy_pinyin(raw)).lower() if c.isalnum()) or "stu"
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
def init_db():
    global DB_POOL
    conn = sqlite3.connect(DB_PATH)
    try:
        cur = conn.cursor()
        cur.execute("PRAGMA journal_mode=WAL")
        cur.executescript(
            """
            CREATE TABLE IF NOT EXISTS students (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL, class TEXT NOT NULL,
                student_id TEXT NOT NULL UNIQUE, username TEXT NOT NULL UNIQUE,
                password TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS clubs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE,
                max_students INTEGER NOT NULL CHECK(max_students > 0),
                current_students INTEGER DEFAULT 0);
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
                admin_username TEXT DEFAULT 'admin',
                admin_password TEXT DEFAULT NULL);
            """
        )
        conn.execute("BEGIN IMMEDIATE")
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
        # 即使库存因运维或故障漂移,第 max+1 条 registration 也无法落库。
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
        # 初始化 settings
        cur.execute("SELECT id, admin_password FROM settings LIMIT 1")
        row = cur.fetchone()
        if row is None:
            # 首启随机生成管理员口令,只打印一次到运行窗口(开源仓库不保留弱默认口令)。
            # 用去歧义字母表(无 0/O/1/l/I),方便部署者从终端抄走后立即登录改密。
            _pw_alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZabcdefghjkmnpqrstuvwxyz23456789"
            init_admin_pw = "".join(secrets.choice(_pw_alphabet) for _ in range(12))
            cur.execute(
                "INSERT INTO settings (registration_start_time, admin_password) VALUES (?, ?)",
                (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), hash_password(init_admin_pw)),
            )
            log.warning("=" * 60)
            log.warning("首次初始化:管理员账号 admin   初始密码  %s", init_admin_pw)
            log.warning("此密码仅显示这一次,请立即登录后修改;切勿用于生产明文环境。")
            log.warning("=" * 60)
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
    if not rebuild_stock():
        RECONCILE_REQUESTED.set()
    seed_open_at()


def rebuild_stock(maintenance_token=None):
    """Redis 冷启动或维护窗口内,以 SQLite 实计重建名额与确认镜像。"""
    if not RG.alive():
        log.warning("Redis 不可用,跳过名额重建(秒杀能力降级)")
        return False
    owns_maintenance = False
    if maintenance_token is None:
        maintenance_token = RG.begin_maintenance()
        if not maintenance_token:
            log.warning("存在报名/退选操作,本次名额对账已跳过")
            return False
        owns_maintenance = True
    try:
        if not RG.maintenance_owned(maintenance_token):
            log.error("名额对账失去 maintenance 租约,已放弃")
            return False
        with DB_POOL.connection() as conn:
            conn.execute("BEGIN")
            cur = conn.cursor()
            cur.execute(
                "SELECT c.id, c.max_students, "
                "(SELECT COUNT(*) FROM registrations r WHERE r.club_id=c.id) "
                "FROM clubs c"
            )
            rows = cur.fetchall()
            cur.execute("SELECT student_id, club_id, operation_id FROM registrations")
            regs = cur.fetchall()
            cur.execute("SELECT registration_start_time FROM settings ORDER BY id DESC LIMIT 1")
            time_row = cur.fetchone()
            conn.commit()
        open_at_epoch = None
        if time_row and time_row[0]:
            try:
                open_at_epoch = int(
                    datetime.strptime(time_row[0], "%Y-%m-%d %H:%M:%S").timestamp())
            except ValueError:
                log.warning("对账时发现非法报名时间: %s", time_row[0])
        stock_keys = list(RG.r.scan_iter(match="stock:club:*"))
        registration_keys = list(RG.r.scan_iter(match="student:reg:*"))
        pipe = RG.r.pipeline()
        pipe.watch(K_MAINT)
        if pipe.get(K_MAINT) != maintenance_token:
            pipe.reset()
            log.error("名额发布前 maintenance 租约已变化,已放弃")
            return False
        pipe.multi()
        for k in stock_keys:
            pipe.delete(k)
        for k in registration_keys:
            pipe.delete(k)
        for cid, maxs, used in rows:
            pipe.set(K_STOCK.format(cid), max(0, int(maxs) - int(used)))
        for sid, cid, operation_id in regs:
            value = RG.reservation_value(cid, operation_id) if operation_id else str(cid)
            pipe.set(K_STUREG.format(sid), value)
        if open_at_epoch is not None:
            pipe.set(K_OPENAT, open_at_epoch)
        pipe.set(K_INIT, "1")
        pipe.execute()
        RG.cache_del(K_CACHE_CLUBS)
        log.info("Redis 名额已重建: %d 个社团", len(rows))
        return True
    except Exception as e:  # noqa: BLE001
        log.error("rebuild_stock 失败: %s", e)
        return False
    finally:
        if owns_maintenance:
            RG.end_maintenance(maintenance_token)


def reconcile_worker():
    """故障后等待在途 operation 排空，再执行 maintenance-fenced 全量对账。"""
    while not RECONCILE_STOP.is_set():
        if not RECONCILE_REQUESTED.wait(timeout=1.0):
            continue
        if RECONCILE_STOP.is_set():
            break
        if rebuild_stock():
            RECONCILE_REQUESTED.clear()
        else:
            RECONCILE_STOP.wait(2.0)


def seed_open_at():
    try:
        with DB_POOL.connection() as conn:
            cur = conn.cursor()
            cur.execute("SELECT registration_start_time FROM settings ORDER BY id DESC LIMIT 1")
            row = cur.fetchone()
        if row and row[0]:
            try:
                dt = datetime.strptime(row[0], "%Y-%m-%d %H:%M:%S")
                RG.open_at_set(int(dt.timestamp()), nx=True)
            except ValueError:
                log.warning("settings.registration_start_time 格式非法,未写入 open_at")
            except RuntimeError as e:
                log.warning("Redis 开放时间播种失败: %s", e)
    except Exception as e:  # noqa: BLE001
        log.error("seed_open_at 失败: %s", e)


# ==========================================================================
# 路由角色表(public / student / admin)
# ==========================================================================
ROLE_PUBLIC, ROLE_STUDENT, ROLE_ADMIN = "public", "student", "admin"

GET_ROUTES = {
    "/healthz": ("_h_health", ROLE_PUBLIC),
    "/readyz": ("_h_ready", ROLE_PUBLIC),
    "/api/check_registration_time": ("_h_check_time", ROLE_PUBLIC),
    "/api/get_clubs": ("_h_get_clubs", ROLE_PUBLIC),
    "/api/get_student_info": ("_h_get_student_info", ROLE_STUDENT),
    "/api/get_registrations": ("_h_get_registrations", ROLE_ADMIN),
    "/api/get_all_students": ("_h_get_all_students", ROLE_ADMIN),
    "/api/export_students_csv": ("_h_export_students_csv", ROLE_ADMIN),
    "/api/export_all_data": ("_h_export_all_data", ROLE_ADMIN),
    "/api/export_unregistered": ("_h_export_unregistered", ROLE_ADMIN),
}
POST_ROUTES = {
    "/api/login": ("_h_login", ROLE_PUBLIC),
    "/api/admin_login": ("_h_admin_login", ROLE_PUBLIC),
    "/api/logout": ("_h_logout", ROLE_PUBLIC),
    "/api/register_club": ("_h_register_club", ROLE_STUDENT),
    "/api/cancel_registration": ("_h_cancel_registration", ROLE_STUDENT),
    "/api/change_password": ("_h_change_password", ROLE_STUDENT),
    "/api/admin_change_password": ("_h_admin_change_password", ROLE_ADMIN),
    "/api/import_students": ("_h_import_students", ROLE_ADMIN),
    "/api/import_clubs": ("_h_import_clubs", ROLE_ADMIN),
    "/api/update_registration_time": ("_h_update_time", ROLE_ADMIN),
    "/api/delete_student": ("_h_delete_student", ROLE_ADMIN),
    "/api/delete_all_students": ("_h_delete_all_students", ROLE_ADMIN),
    "/api/delete_club": ("_h_delete_club", ROLE_ADMIN),
    "/api/delete_all_clubs": ("_h_delete_all_clubs", ROLE_ADMIN),
}
WEB_DIR = os.environ.get("WEB_DIR", "web")  # 前端资源目录
PAGES = {
    "/": "login.html",
    "/student/dashboard": "student_dashboard.html",
    "/student/profile": "student_profile.html",
    "/admin/dashboard": "admin_dashboard.html",
}
STATIC = {
    "/app.css": ("app.css", "text/css; charset=utf-8"),
    "/easter-egg.js": ("easter-egg.js", "application/javascript; charset=utf-8"),
    "/student-dashboard.js": ("student-dashboard.js", "application/javascript; charset=utf-8"),
    "/favicon.svg": ("favicon.svg", "image/svg+xml"),
}


class RequestBodyError(ValueError):
    """HTTP/1.1 framing that cannot safely be drained on this connection."""

    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


# ==========================================================================
# 请求处理器
# ==========================================================================
class ClubSystemHandler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    timeout = 30  # 抗 slowloris:卡死连接回收

    # ---- 响应辅助(统一 Content-Length,启用 keep-alive) ----
    def _send(self, code, body=b"", ctype="application/json; charset=utf-8", extra=None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        if self.close_connection:
            self.send_header("Connection", "close")
        if extra:
            for k, v in extra:
                self.send_header(k, v)
        self.end_headers()
        if body:
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass

    def _json(self, code, obj, extra=None):
        self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"), extra=extra)

    def _csv(self, rows, header, filename):
        out = io.StringIO()
        w = csv.writer(out)
        w.writerow([_csv_safe(c) for c in header])
        for r in rows:
            w.writerow([_csv_safe(c) for c in r])
        body = out.getvalue().encode("utf-8-sig")  # BOM 便于 Excel 识别中文
        self._send(200, body, ctype="text/csv; charset=utf-8",
                   extra=[("Content-Disposition", 'attachment; filename="{}"'.format(filename))])

    def log_message(self, fmt, *args):  # 默认 stderr 噪声改走 logging
        log.debug("%s - %s", self.address_string(), fmt % args)

    # ---- 会话 ----
    def _session(self):
        return RG.session_get(_session_cookie_token_from_headers(self.headers))

    def _set_session_cookie(self, token):
        secure = "; Secure" if COOKIE_SECURE else ""
        return ("Set-Cookie",
                "session={}; HttpOnly; SameSite=Strict; Path=/; Max-Age={}{}".format(
                    token, SESSION_TTL, secure))

    def _clear_cookie(self):
        secure = "; Secure" if COOKIE_SECURE else ""
        return ("Set-Cookie", "session=; HttpOnly; SameSite=Strict; Path=/; Max-Age=0{}".format(secure))

    def _client_ip(self):
        """仅信任本机反代写入的 X-Real-IP,避免直连客户端伪造。"""
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
        """返回 session(public 时可为 None)。鉴权失败时已发响应并返回 False。"""
        if role == ROLE_PUBLIC:
            return None
        try:
            sess = self._session()
        except RuntimeError:
            self._json(503, {"success": False, "message": "会话服务暂不可用,请稍后重试"})
            return False
        if sess is None:
            self._json(401, {"success": False, "message": "未登录或会话已过期"})
            return False
        if role == ROLE_ADMIN and sess.get("role") != "admin":
            self._json(403, {"success": False, "message": "需要管理员权限"})
            return False
        if role == ROLE_STUDENT and sess.get("role") != "student":
            self._json(403, {"success": False, "message": "需要学生身份"})
            return False
        return sess

    def _read_raw_body(self):
        """Consume one complete POST body before any route/auth early return.

        `BaseHTTPRequestHandler` keeps the TCP connection alive by default.
        Returning a 401/403/404 before consuming a body turns its bytes into
        the next request line on an Nginx-reused upstream connection.  Every
        path here therefore either drains exactly one Content-Length body or
        marks the connection closed before replying.
        """
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
        if n > MAX_BODY:
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
        """GET never needs a body; close rather than leave bytes for reuse."""
        transfer_encodings = self.headers.get_all("Transfer-Encoding") or []
        lengths = self.headers.get_all("Content-Length") or []
        if transfer_encodings or len(lengths) > 1:
            self.close_connection = True
            self._json(400, {"success": False, "message": "GET 请求不能携带请求体"})
            return True
        if lengths:
            value = lengths[0].strip()
            if not value.isdecimal() or int(value) != 0:
                self.close_connection = True
                self._json(400, {"success": False, "message": "GET 请求不能携带请求体"})
                return True
        return False

    # ---- 分发 ----
    def do_GET(self):
        if self._reject_get_body():
            return
        path = self.path.split("?")[0]
        if path in PAGES:
            return self._serve_page(PAGES[path])
        if path in STATIC:
            return self._serve_static(STATIC[path])
        if path.startswith("/fonts/") and path.endswith(".woff2") and "/" not in path[7:] and ".." not in path:
            return self._serve_static((path[1:], "font/woff2"))
        if path.startswith("/img/") and "/" not in path[5:] and ".." not in path:
            ext = path.rsplit(".", 1)[-1].lower() if "." in path else ""
            mt = {"jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png",
                  "webp": "image/webp", "svg": "image/svg+xml"}.get(ext)
            if mt:
                return self._serve_static((path[1:], mt))
        route = GET_ROUTES.get(path)
        if route is None and path.startswith("/api/export_club_data"):
            route = ("_h_export_club_data", ROLE_ADMIN)
        if route is None:
            return self._json(404, {"success": False, "message": "未找到"})
        name, role = route
        sess = self._require(role)
        if sess is False:
            return
        try:
            getattr(self, name)(sess)
        except Exception as e:  # noqa: BLE001
            log.exception("GET %s 处理异常: %s", path, e)
            self._json(500, {"success": False, "message": "服务器错误"})

    def do_POST(self):
        try:
            raw = self._read_raw_body()
        except RequestBodyError as e:
            return self._json(e.status, {"success": False, "message": "请求格式错误: {}".format(e)})
        path = self.path.split("?")[0]
        route = POST_ROUTES.get(path)
        if route is None:
            return self._json(404, {"success": False, "message": "未找到"})
        name, role = route
        sess = self._require(role)
        if sess is False:
            return
        try:
            data = json.loads(raw) if raw else {}
            if not isinstance(data, dict):
                raise ValueError("JSON 顶层必须为对象")
        except (ValueError, json.JSONDecodeError):
            return self._json(400, {"success": False, "message": "JSON 解析失败"})
        try:
            if path in ("/api/register_club", "/api/cancel_registration"):
                if RECONCILE_REQUESTED.is_set():
                    return self._json(
                        503,
                        {"success": False, "message": "名额状态正在安全对账,请稍后重试"},
                        extra=[("Retry-After", "2")],
                    )
                if not REGISTRATION_SLOTS.acquire(timeout=REGISTER_QUEUE_TIMEOUT):
                    return self._json(
                        503,
                        {"success": False, "message": "报名队列繁忙,请稍后重试"},
                        extra=[("Retry-After", "1")],
                    )
                try:
                    if RECONCILE_REQUESTED.is_set():
                        return self._json(
                            503,
                            {"success": False, "message": "名额状态正在安全对账,请稍后重试"},
                            extra=[("Retry-After", "2")],
                        )
                    getattr(self, name)(sess, data)
                finally:
                    REGISTRATION_SLOTS.release()
            else:
                getattr(self, name)(sess, data)
        except Exception as e:  # noqa: BLE001
            log.exception("POST %s 处理异常: %s", path, e)
            self._json(500, {"success": False, "message": "服务器错误"})

    # ---- 静态页(白名单;无通用文件嗅探,消灭整库/源码下载与遍历) ----
    def _serve_page(self, fname):
        try:
            with open(os.path.join(WEB_DIR, fname), "rb") as f:
                body = f.read()
        except OSError:
            return self._json(404, {"success": False, "message": "页面不存在"})
        self._send(200, body, ctype="text/html; charset=utf-8")

    def _serve_static(self, spec):
        fname, ctype = spec
        try:
            with open(os.path.join(WEB_DIR, fname), "rb") as f:
                body = f.read()
        except OSError:
            return self._json(404, {"success": False, "message": "资源不存在"})
        self._send(200, body, ctype=ctype, extra=[("Cache-Control", "public, max-age=300")])

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
            if RECONCILE_REQUESTED.is_set():
                return self._json(503, {"status": "not-ready", "reason": "reconcile-pending"})
            if not RG.alive() or not RG.r.exists(K_INIT):
                RECONCILE_REQUESTED.set()
                return self._json(503, {"status": "not-ready"})
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
            drifted = sum(
                1 for cid, max_students, used in rows
                if used > max_students or live.get(cid) is None
                or live[cid] != max(0, max_students - used)
            )
            if drifted:
                active = RG.has_active_operations()
                if active is None:
                    return self._json(503, {"status": "not-ready", "reason": "redis"})
                if not active:
                    RECONCILE_REQUESTED.set()
                return self._json(503, {"status": "not-ready",
                                        "reason": "operations-active" if active else "stock-drift",
                                        "clubs": drifted})
            self._json(200, {"status": "ready"})
        except Exception as e:  # noqa: BLE001
            log.error("readyz 检查失败: %s", e)
            self._json(503, {"status": "not-ready"})

    def _h_check_time(self, sess):
        open_at = RG.open_at_get()
        start_str = None
        if open_at is None:
            try:
                with DB_POOL.connection() as conn:
                    cur = conn.cursor()
                    cur.execute("SELECT registration_start_time FROM settings ORDER BY id DESC LIMIT 1")
                    row = cur.fetchone()
                if row and row[0]:
                    start_str = row[0]
                    try:
                        open_at = int(datetime.strptime(row[0], "%Y-%m-%d %H:%M:%S").timestamp())
                    except ValueError:
                        open_at = None
            except Exception:  # noqa: BLE001
                pass
        else:
            start_str = datetime.fromtimestamp(open_at).strftime("%Y-%m-%d %H:%M:%S")
        can = (open_at is not None) and (RG.now_epoch() >= open_at)
        self._json(200, {"can_register": can, "start_time": start_str})

    def _h_get_clubs(self, sess):
        try:
            with DB_POOL.connection() as conn:
                cur = conn.cursor()
                cur.execute("SELECT id, name, max_students, current_students FROM clubs ORDER BY id")
                rows = cur.fetchall()
        except Exception as e:  # noqa: BLE001
            log.error("get_clubs 失败: %s", e)
            return self._json(500, {"success": False, "message": "服务器错误"})
        ids = [r[0] for r in rows]
        live = RG.stock_left(ids)  # 实时名额(Redis 实时);None 则回落 current_students
        data = []
        for cid, name, maxs, cur_s in rows:
            if live is not None and live.get(cid) is not None:
                used = maxs - live[cid]
            else:
                used = cur_s
            data.append({"id": cid, "name": name, "max_students": maxs,
                         "current_students": max(0, min(maxs, used))})
        self._json(200, data)

    def _h_login(self, sess, data):
        username = data.get("username") or ""
        password = data.get("password") or ""
        if not isinstance(username, str) or not isinstance(password, str):
            return self._json(400, {"success": False, "message": "用户名或密码格式错误"})
        username = username.strip()
        if not username or not password:
            return self._json(400, {"success": False, "message": "用户名和密码不能为空"})
        if len(username) > MAX_USERNAME or len(password) > MAX_PASSWORD:
            return self._json(400, {"success": False, "message": "用户名或密码过长"})
        # A campus egress IP may represent hundreds of students.  Lock only
        # the attempted account here; Nginx supplies the source-IP resource
        # ceiling.  A shared IP counter would let one student lock everyone.
        login_key = "u:" + username
        if RG.login_blocked(login_key, LOGIN_MAX_FAILS):
            return self._json(429, {"success": False, "message": "尝试过于频繁,请稍后再试"})
        try:
            role_epoch = RG.session_role_epoch("student")
        except RuntimeError:
            return self._json(503, {"success": False, "message": "会话服务不可用,请稍后重试"})
        with DB_POOL.connection() as conn:
            cur = conn.cursor()
            cur.execute("SELECT id, name, class, student_id, password FROM students WHERE username = ?",
                        (username,))
            row = cur.fetchone()
        if not row:
            RG.login_fail(login_key)
            return self._json(401, {"success": False, "message": "用户名或密码错误"})
        sid, name, klass, student_no, stored = row
        try:
            principal_version = RG.session_principal_version("student", str(sid))
        except RuntimeError:
            return self._json(503, {"success": False, "message": "会话服务不可用,请稍后重试"})
        if not AUTH_SLOTS.acquire(timeout=1.0):
            return self._json(503, {"success": False, "message": "登录繁忙,请稍后重试"},
                              extra=[("Retry-After", "1")])
        try:
            ok, upgrade = verify_password(stored, password)
            upgraded_hash = hash_password(password) if ok and upgrade else None
        finally:
            AUTH_SLOTS.release()
        if not ok:
            RG.login_fail(login_key)
            return self._json(401, {"success": False, "message": "用户名或密码错误"})
        # The record must still be exactly the one that was authenticated.  A
        # deletion/password change racing the Argon2 work otherwise could turn
        # a stale DB read into a fresh, valid Redis session.
        with DB_POOL.connection() as conn:
            current = conn.execute("SELECT password FROM students WHERE id = ?", (sid,)).fetchone()
        if not current or not secrets.compare_digest(str(current[0]), str(stored)):
            return self._json(401, {"success": False, "message": "用户名或密码错误"})
        try:
            token = RG.session_create({"role": "student", "student_id": sid,
                                       "name": name, "class": klass, "student_no": student_no},
                                      role_epoch, principal_version)
        except RuntimeError:
            return self._json(503, {"success": False, "message": "会话服务不可用,请稍后重试"})
        if token is None:
            return self._json(503, {"success": False, "message": "账号状态已变化,请重新登录"},
                              extra=[("Retry-After", "1")])
        if upgraded_hash:
            try:
                with DB_POOL.connection() as conn:
                    conn.execute("UPDATE students SET password = ? WHERE id = ? AND password = ?",
                                 (upgraded_hash, sid, stored))
            except sqlite3.Error:
                pass
        RG.login_ok(login_key)
        self._json(200, {"success": True, "student_id": sid, "name": name,
                         "class": klass, "student_no": student_no},
                   extra=[self._set_session_cookie(token)])

    def _h_admin_login(self, sess, data):
        username = data.get("username") or ""
        password = data.get("password") or ""
        if not isinstance(username, str) or not isinstance(password, str):
            return self._json(400, {"success": False, "message": "用户名或密码格式错误"})
        username = username.strip()
        if not username or not password:
            return self._json(400, {"success": False, "message": "用户名和密码不能为空"})
        if len(username) > MAX_USERNAME or len(password) > MAX_PASSWORD:
            return self._json(400, {"success": False, "message": "用户名或密码过长"})
        login_key = "admin:u:" + username
        if RG.login_blocked(login_key, LOGIN_MAX_FAILS):
            return self._json(429, {"success": False, "message": "尝试过于频繁,请稍后再试"})
        try:
            role_epoch = RG.session_role_epoch("admin")
        except RuntimeError:
            return self._json(503, {"success": False, "message": "会话服务不可用,请稍后重试"})
        with DB_POOL.connection() as conn:
            cur = conn.cursor()
            cur.execute("SELECT id, admin_password FROM settings WHERE admin_username = ?", (username,))
            row = cur.fetchone()
        if not row:
            RG.login_fail(login_key)
            return self._json(401, {"success": False, "message": "用户名或密码错误"})
        try:
            principal_version = RG.session_principal_version("admin", username)
        except RuntimeError:
            return self._json(503, {"success": False, "message": "会话服务不可用,请稍后重试"})
        if not AUTH_SLOTS.acquire(timeout=1.0):
            return self._json(503, {"success": False, "message": "登录繁忙,请稍后重试"},
                              extra=[("Retry-After", "1")])
        try:
            ok, upgrade = verify_password(row[1], password)
            upgraded_hash = hash_password(password) if ok and upgrade else None
        finally:
            AUTH_SLOTS.release()
        if not ok:
            RG.login_fail(login_key)
            return self._json(401, {"success": False, "message": "用户名或密码错误"})
        with DB_POOL.connection() as conn:
            current = conn.execute("SELECT admin_password FROM settings WHERE id = ?", (row[0],)).fetchone()
        if not current or not secrets.compare_digest(str(current[0]), str(row[1])):
            return self._json(401, {"success": False, "message": "用户名或密码错误"})
        try:
            token = RG.session_create({"role": "admin", "username": username},
                                      role_epoch, principal_version)
        except RuntimeError:
            return self._json(503, {"success": False, "message": "会话服务不可用,请稍后重试"})
        if token is None:
            return self._json(503, {"success": False, "message": "账号状态已变化,请重新登录"},
                              extra=[("Retry-After", "1")])
        if upgraded_hash:
            try:
                with DB_POOL.connection() as conn:
                    conn.execute("UPDATE settings SET admin_password = ? WHERE id = ? AND admin_password = ?",
                                 (upgraded_hash, row[0], row[1]))
            except sqlite3.Error:
                pass
        RG.login_ok(login_key)
        self._json(200, {"success": True}, extra=[self._set_session_cookie(token)])

    def _h_logout(self, sess, data):
        token = _session_cookie_token_from_headers(self.headers)
        if token:
            RG.session_del(token)
        self._json(200, {"success": True}, extra=[self._clear_cookie()])

    # ======================================================================
    # 学生端点(身份只取自 session,IDOR 已消除)
    # ======================================================================
    def _h_get_student_info(self, sess):
        sid = sess["student_id"]
        with DB_POOL.connection() as conn:
            cur = conn.cursor()
            cur.execute("SELECT name, class, student_id, username FROM students WHERE id = ?", (sid,))
            stu = cur.fetchone()
            if not stu:
                return self._json(404, {"success": False, "message": "学生不存在"})
            cur.execute(
                "SELECT c.name, r.registration_time FROM registrations r "
                "JOIN clubs c ON r.club_id = c.id WHERE r.student_id = ?", (sid,))
            reg = cur.fetchone()
        self._json(200, {
            "name": stu[0], "class": stu[1], "student_id": stu[2], "username": stu[3],
            "registered_club": reg[0] if reg else None,
            "registration_time": reg[1] if reg else None,
        }, extra=[("Cache-Control", "no-store")])

    def _h_register_club(self, sess, data):
        sid = sess["student_id"]
        club_id = data.get("club_id")
        try:
            club_id = int(club_id)
        except (TypeError, ValueError):
            return self._json(400, {"success": False, "message": "缺少或非法的社团ID"})
        if club_id <= 0:
            return self._json(400, {"success": False, "message": "缺少或非法的社团ID"})

        # `stock:club:{id}` is intentionally absent for a nonexistent club.
        # Distinguish that normal business rejection from a missing Redis key
        # for a real club before entering the acquire/reconcile protocol.
        try:
            with DB_POOL.connection() as conn:
                club_exists = conn.execute(
                    "SELECT EXISTS(SELECT 1 FROM clubs WHERE id = ?)", (club_id,)
                ).fetchone()[0]
        except Exception as e:  # noqa: BLE001
            log.error("报名社团存在性检查失败 sid=%s club=%s: %s", sid, club_id, e)
            return self._json(503, {"success": False, "message": "系统繁忙,请稍后重试"})
        if not club_exists:
            return self._json(200, {"success": False, "message": "社团不存在"})

        operation_id = secrets.token_urlsafe(18)
        reservation_value = RG.reservation_value(club_id, operation_id)

        # Redis 原子抢占；每次操作带唯一代际,旧 confirm/rollback 不能碰新状态。
        try:
            code = RG.acquire_seat(sid, club_id, reservation_value)
        except RuntimeError:
            RECONCILE_REQUESTED.set()
            return self._json(503, {"success": False, "message": "系统繁忙,请稍后重试"})
        if code == -2:
            # 缺关键库存键时绝不根据不完整快照在线猜容量；由冷启动/维护重建恢复。
            RECONCILE_REQUESTED.set()
            return self._json(503, {"success": False, "message": "名额状态暂不可用,请联系管理员"})
        if code == -3:
            return self._json(503, {"success": False, "message": "系统维护中,请稍后重试"},
                              extra=[("Retry-After", "2")])
        if code == -4:
            return self._json(200, {"success": False, "message": "报名尚未开始"})
        if code == 0:
            return self._json(200, {"success": False, "message": "该社团已满员"})
        if code == -1:
            with DB_POOL.connection() as conn:
                existing = conn.execute(
                    "SELECT club_id FROM registrations WHERE student_id = ?", (sid,)).fetchone()
            if existing and int(existing[0]) == club_id:
                return self._json(200, {"success": True, "message": "您已报名该社团"})
            return self._json(200, {"success": False, "message": "您已报名其他社团或请勿重复提交"})
        if code != 1:
            return self._json(503, {"success": False, "message": "名额状态异常,请稍后重试"})

        # 抢到 -> 同步落库
        try:
            with DB_POOL.connection() as conn:
                cur = conn.cursor()
                conn.execute("BEGIN IMMEDIATE")
                cur.execute(
                    "INSERT INTO registrations "
                    "(student_id, club_id, registration_time, operation_id) VALUES (?,?,?,?)",
                    (sid, club_id, datetime.now().strftime("%Y-%m-%d %H:%M:%S"), operation_id))
                registration_id = cur.lastrowid
                cur.execute("UPDATE clubs SET current_students = current_students + 1 WHERE id = ?",
                            (club_id,))
                conn.commit()
            try:
                confirmed = RG.confirm_seat(sid, reservation_value)
            except RuntimeError as e:
                confirmed = False
                log.error("报名已落库但 Redis 确认失败 reg=%s: %s", registration_id, e)
                RECONCILE_REQUESTED.set()
            # confirmed=False 也可能是并发退选已经删除了同一 registration。
            if not confirmed:
                RECONCILE_REQUESTED.set()
                with DB_POOL.connection() as conn:
                    still_exists = conn.execute(
                        "SELECT 1 FROM registrations WHERE id=? AND operation_id=?",
                        (registration_id, operation_id)).fetchone() is not None
                if not still_exists:
                    return self._json(200, {"success": False, "message": "报名已被并发退选取消"})
            self._json(200, {"success": True,
                             "message": "报名成功" if confirmed else "报名成功,状态同步稍有延迟"})
        except sqlite3.IntegrityError as e:
            try:
                RG.rollback_reservation(sid, club_id, reservation_value)
            except RuntimeError as re:
                log.error("报名回滚同步失败 sid=%s op=%s: %s", sid, operation_id, re)
                RECONCILE_REQUESTED.set()
            is_full = "club full" in str(e).lower()
            try:
                with DB_POOL.connection() as conn:
                    club_exists = conn.execute(
                        "SELECT EXISTS(SELECT 1 FROM clubs WHERE id = ?)", (club_id,)
                    ).fetchone()[0]
                    existing = conn.execute(
                        "SELECT club_id FROM registrations WHERE student_id = ?", (sid,)).fetchone()
            except Exception as lookup_error:  # noqa: BLE001
                log.error("报名冲突后的 SQLite 检查失败 sid=%s club=%s: %s",
                          sid, club_id, lookup_error)
                RECONCILE_REQUESTED.set()
                return self._json(503, {"success": False, "message": "系统繁忙,请稍后重试"})
            if not club_exists:
                # The pre-check passed but a maintenance-protected admin delete
                # won the race. Exact rollback restored Redis, so this is a
                # business missing response rather than global stock drift.
                return self._json(200, {"success": False, "message": "社团不存在"})
            RECONCILE_REQUESTED.set()
            if not is_full:
                if existing and int(existing[0]) == club_id:
                    return self._json(200, {"success": True, "message": "您已报名该社团"})
            message = "该社团已满员" if is_full else "您已报名其他社团或请勿重复提交"
            self._json(200, {"success": False, "message": message})
        except Exception as e:  # noqa: BLE001
            log.error("报名落库失败 sid=%s club=%s: %s", sid, club_id, e)
            try:
                RG.rollback_reservation(sid, club_id, reservation_value)
            except RuntimeError as re:
                log.error("报名回滚同步失败 sid=%s op=%s: %s", sid, operation_id, re)
                RECONCILE_REQUESTED.set()
            RECONCILE_REQUESTED.set()
            self._json(200, {"success": False, "message": "报名失败,请重试"})

    def _h_cancel_registration(self, sess, data):
        sid = sess["student_id"]
        operation_lock = RG.begin_student_op(sid)
        if not operation_lock:
            return self._json(503, {"success": False,
                                    "message": "报名状态正在处理,请稍后重试"})
        try:
            return self._cancel_registration_inner(sid)
        finally:
            RG.end_student_op(sid, operation_lock)

    def _cancel_registration_inner(self, sid):
        with DB_POOL.connection() as conn:
            cur = conn.cursor()
            try:
                # 先取得 SQLite 写锁再查询,双退选不可能同时看见同一 registration。
                conn.execute("BEGIN IMMEDIATE")
                cur.execute(
                    "SELECT id, club_id, operation_id FROM registrations WHERE student_id = ?",
                    (sid,))
                reg = cur.fetchone()
                if not reg:
                    conn.rollback()
                    return self._json(200, {"success": False, "message": "您还未报名任何社团"})
                registration_id, club_id, operation_id = reg
                cur.execute("DELETE FROM registrations WHERE student_id = ?", (sid,))
                if cur.rowcount != 1:
                    conn.rollback()
                    return self._json(200, {"success": False, "message": "报名状态已变化,请刷新"})
                cur.execute(
                    "UPDATE clubs SET current_students = MAX(0, current_students - 1) WHERE id = ?",
                    (club_id,))
                conn.commit()
            except sqlite3.Error as e:
                conn.rollback()
                log.error("退选失败 sid=%s: %s", sid, e)
                return self._json(200, {"success": False, "message": "取消报名失败,请重试"})
        reservation_value = (RG.reservation_value(club_id, operation_id)
                             if operation_id else str(club_id))
        try:
            event_id = operation_id or "legacy-{}-{}-{}".format(registration_id, sid, club_id)
            RG.release_registration(event_id, sid, club_id, reservation_value)
        except RuntimeError as e:
            log.error("退选已落库但名额同步失败 reg=%s: %s", registration_id, e)
            RECONCILE_REQUESTED.set()
            return self._json(503, {"success": False,
                                    "message": "退选已记录,后台将在操作排空后安全对账"})
        self._json(200, {"success": True, "message": "取消报名成功"})

    def _h_change_password(self, sess, data):
        sid = sess["student_id"]
        cur_pw = data.get("current") or ""
        new_pw = data.get("new") or ""
        if not isinstance(cur_pw, str) or not isinstance(new_pw, str):
            return self._json(400, {"success": False, "message": "密码格式错误"})
        if len(new_pw) < 6 or len(new_pw) > MAX_PASSWORD or len(cur_pw) > MAX_PASSWORD:
            return self._json(400, {"success": False, "message": "新密码须为 6–256 位"})
        with DB_POOL.connection() as conn:
            c = conn.cursor()
            c.execute("SELECT password FROM students WHERE id = ?", (sid,))
            row = c.fetchone()
        if not row:
            return self._json(404, {"success": False, "message": "用户不存在"})
        if not AUTH_SLOTS.acquire(blocking=False):
            return self._json(503, {"success": False, "message": "密码服务繁忙,请稍后重试"})
        try:
            ok, _ = verify_password(row[0], cur_pw)
            new_hash = hash_password(new_pw) if ok else None
        finally:
            AUTH_SLOTS.release()
        if not ok:
            return self._json(400, {"success": False, "message": "当前密码不正确"})
        try:
            mutation_lock = RG.session_begin_mutation("student", str(sid))
        except RuntimeError:
            return self._json(503, {"success": False, "message": "会话服务不可用,请稍后重试"})
        if not mutation_lock:
            return self._json(503, {"success": False, "message": "账号状态正在更新,请稍后重试"})
        try:
            # The mutation lock is visible to the session-create Lua before
            # this generation changes, and stays until SQLite has committed.
            # A concurrent old-password login therefore cannot mint a current
            # token between revoke and UPDATE.
            RG.session_revoke_identity("student", str(sid))
            with DB_POOL.connection() as conn:
                conn.execute("UPDATE students SET password = ? WHERE id = ?", (new_hash, sid))
            self._json(200, {"success": True, "message": "密码已修改,请重新登录"},
                       extra=[self._clear_cookie()])
        except RuntimeError:
            self._json(503, {"success": False, "message": "会话服务不可用,请稍后重试"})
        finally:
            RG.session_end_mutation(mutation_lock)

    # ======================================================================
    # 管理端点(均 admin 鉴权)
    # ======================================================================
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
        if not AUTH_SLOTS.acquire(blocking=False):
            return self._json(503, {"success": False, "message": "密码服务繁忙,请稍后重试"})
        try:
            ok, _ = verify_password(row[1], cur_pw) if row else (False, False)
            new_hash = hash_password(new_pw) if ok else None
        finally:
            AUTH_SLOTS.release()
        if not ok:
            return self._json(400, {"success": False, "message": "当前密码不正确"})
        try:
            mutation_lock = RG.session_begin_mutation("admin", uname)
        except RuntimeError:
            return self._json(503, {"success": False, "message": "会话服务不可用,请稍后重试"})
        if not mutation_lock:
            return self._json(503, {"success": False, "message": "账号状态正在更新,请稍后重试"})
        try:
            RG.session_revoke_identity("admin", uname)
            with DB_POOL.connection() as conn:
                conn.execute("UPDATE settings SET admin_password = ? WHERE id = ?",
                             (new_hash, row[0]))
            self._json(200, {"success": True, "message": "管理员密码已修改,请重新登录"},
                       extra=[self._clear_cookie()])
        except RuntimeError:
            self._json(503, {"success": False, "message": "会话服务不可用,请稍后重试"})
        finally:
            RG.session_end_mutation(mutation_lock)

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
            cur.execute("SELECT id, name, class, student_id, username FROM students ORDER BY class, name")
            rows = cur.fetchall()
        data = [{"id": r[0], "name": r[1], "class": r[2], "student_id": r[3], "username": r[4]}
                for r in rows]
        self._json(200, data, extra=[("Cache-Control", "no-store")])

    def _h_export_students_csv(self, sess):
        with DB_POOL.connection() as conn:
            cur = conn.cursor()
            cur.execute("SELECT name, class, student_id, username FROM students ORDER BY class, name")
            rows = cur.fetchall()
        self._csv(rows, ["姓名", "班级", "学号", "用户名"],
                  "students_{}.csv".format(datetime.now().strftime("%Y%m%d_%H%M%S")))

    def _h_export_all_data(self, sess):
        with DB_POOL.connection() as conn:
            cur = conn.cursor()
            cur.execute(
                "SELECT s.name, s.class, s.student_id, COALESCE(c.name,'未报名') FROM students s "
                "LEFT JOIN registrations r ON s.id = r.student_id "
                "LEFT JOIN clubs c ON r.club_id = c.id ORDER BY s.class, s.name")
            rows = cur.fetchall()
        self._csv(rows, ["姓名", "班级", "学号", "报名社团"],
                  "registrations_{}.csv".format(datetime.now().strftime("%Y%m%d_%H%M%S")))

    def _h_export_unregistered(self, sess):
        with DB_POOL.connection() as conn:
            cur = conn.cursor()
            cur.execute(
                "SELECT s.name, s.class, s.student_id FROM students s "
                "LEFT JOIN registrations r ON s.id = r.student_id WHERE r.id IS NULL "
                "ORDER BY s.class, s.name")
            rows = cur.fetchall()
        self._csv(rows, ["姓名", "班级", "学号"],
                  "unregistered_{}.csv".format(datetime.now().strftime("%Y%m%d_%H%M%S")))

    def _h_export_club_data(self, sess):
        qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        club_id = (qs.get("club_id") or [None])[0]
        if not club_id or not str(club_id).isdigit():
            return self._json(400, {"success": False, "message": "无效的 club_id"})
        club_id = int(club_id)
        with DB_POOL.connection() as conn:
            cur = conn.cursor()
            cur.execute("SELECT name FROM clubs WHERE id = ?", (club_id,))
            club = cur.fetchone()
            if not club:
                return self._json(404, {"success": False, "message": "社团不存在"})
            cur.execute(
                "SELECT s.name, s.class, s.student_id FROM students s "
                "JOIN registrations r ON s.id = r.student_id WHERE r.club_id = ? "
                "ORDER BY s.class, s.name", (club_id,))
            rows = cur.fetchall()
        self._csv(rows, ["姓名", "班级", "学号"],
                  "club_{}_{}.csv".format(club_id, datetime.now().strftime("%Y%m%d_%H%M%S")))

    def _h_import_students(self, sess, data):
        students = data.get("students", [])
        if not isinstance(students, list) or not students:
            return self._json(400, {"success": False, "message": "没有学生数据"})
        if len(students) > MAX_IMPORT_STUDENTS:
            return self._json(400, {"success": False,
                                    "message": "单次最多导入 {} 人".format(MAX_IMPORT_STUDENTS)})
        results = {"success": 0, "failed": 0}
        credentials = []  # 一次性回显明文供管理员下发
        prepared = []
        # Argon2 在事务外完成,避免几百次哈希长期占住唯一 SQLite writer。
        for st in students:
            if not isinstance(st, dict):
                results["failed"] += 1
                continue
            name = clean_text(st.get("name"))
            klass = clean_text(st.get("class"))
            student_no = clean_text(st.get("student_id"), maxlen=40)
            if not name or not klass or not student_no:
                results["failed"] += 1
                continue
            plain = gen_password()
            with AUTH_SLOTS:
                password_hash = hash_password(plain)
            prepared.append((name, klass, student_no, plain, password_hash))
        try:
            with DB_POOL.connection() as conn:
                cur = conn.cursor()
                conn.execute("BEGIN IMMEDIATE")
                seen = set()
                for name, klass, student_no, plain, password_hash in prepared:
                    username = gen_username(name, cur, seen)
                    try:
                        cur.execute(
                            "INSERT INTO students (name, class, student_id, username, password) "
                            "VALUES (?,?,?,?,?)",
                            (name, klass, student_no, username, password_hash))
                        results["success"] += 1
                        credentials.append({"name": name, "class": klass, "username": username, "password": plain})
                    except sqlite3.IntegrityError:
                        results["failed"] += 1  # 学号/用户名重复
                        seen.discard(username)
                conn.commit()
        except Exception as e:  # noqa: BLE001
            log.error("导入学生失败: %s", e)
            return self._json(500, {"success": False, "message": "导入失败"})
        self._json(200, {"success": results["success"], "failed": results["failed"],
                         "credentials": credentials})

    def _h_import_clubs(self, sess, data):
        maintenance = RG.begin_maintenance()
        if not maintenance:
            return self._json(503, {"success": False,
                                    "message": "有报名请求处理中,请稍后再导入社团"})
        try:
            return self._import_clubs_inner(data)
        finally:
            RG.end_maintenance(maintenance)

    def _import_clubs_inner(self, data):
        clubs = data.get("clubs", [])
        if not isinstance(clubs, list) or not clubs:
            return self._json(400, {"success": False, "message": "没有社团数据"})
        results = {"success": 0, "failed": 0}
        new_ids = []
        try:
            with DB_POOL.connection() as conn:
                cur = conn.cursor()
                cur.execute("SELECT name FROM clubs")
                existing = {r[0] for r in cur.fetchall()}
                conn.execute("BEGIN")
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
                conn.commit()
        except Exception as e:  # noqa: BLE001
            log.error("导入社团失败: %s", e)
            return self._json(500, {"success": False, "message": "导入失败"})
        sync_ok = RG.alive()
        if sync_ok:
            for cid, maxs in new_ids:
                try:
                    # 若请求已先看到新社团,它只会收到缺键 503；SET NX 不覆盖现有状态。
                    if not RG.r.set(K_STOCK.format(cid), maxs, nx=True):
                        sync_ok = False
                except Exception:  # noqa: BLE001
                    sync_ok = False
            RG.cache_del(K_CACHE_CLUBS)
        if new_ids and not sync_ok:
            RECONCILE_REQUESTED.set()
            return self._json(503, {"success": results["success"], "failed": results["failed"],
                                    "message": "社团已导入,后台将在操作排空后安全对账"})
        self._json(200, {"success": results["success"], "failed": results["failed"]})

    def _h_update_time(self, sess, data):
        maintenance = RG.begin_maintenance()
        if not maintenance:
            return self._json(503, {"success": False,
                                    "message": "有报名请求处理中,请稍后更新时间"})
        try:
            return self._update_time_inner(data)
        finally:
            RG.end_maintenance(maintenance)

    def _update_time_inner(self, data):
        start_time = data.get("start_time") or ""
        if not isinstance(start_time, str):
            return self._json(400, {"success": False, "message": "时间格式错误"})
        start_time = start_time.strip()
        try:
            dt = datetime.strptime(start_time, "%Y-%m-%d %H:%M:%S")
        except ValueError:
            return self._json(400, {"success": False, "message": "时间格式应为 YYYY-MM-DD HH:MM:SS"})
        try:
            with DB_POOL.connection() as conn:
                conn.execute("BEGIN")
                conn.execute("UPDATE settings SET registration_start_time = ?", (start_time,))
                conn.commit()
            RG.open_at_set(int(dt.timestamp()))
            self._json(200, {"success": True})
        except sqlite3.Error as e:
            log.error("更新报名时间失败: %s", e)
            self._json(500, {"success": False, "message": "更新失败"})
        except RuntimeError as e:
            log.error("报名时间已保存但 Redis 同步失败: %s", e)
            RECONCILE_REQUESTED.set()
            self._json(503, {"success": False,
                             "message": "时间已保存,热路径同步失败,请重试"})

    def _h_delete_student(self, sess, data):
        sid = data.get("student_id")
        try:
            sid = int(sid)
        except (TypeError, ValueError):
            return self._json(400, {"success": False, "message": "缺少或非法的学生ID"})
        maintenance = RG.begin_maintenance()
        if not maintenance:
            return self._json(503, {"success": False, "message": "有报名请求处理中,请稍后再试"})
        session_lock = None
        try:
            # Fence login before deleting the DB row.  An in-flight login that
            # read this student earlier carries the old version and its final
            # Redis session-create script will now reject it.
            try:
                session_lock = RG.session_begin_mutation("student", str(sid))
            except RuntimeError:
                return self._json(503, {"success": False, "message": "会话服务不可用,请稍后重试"})
            if not session_lock:
                return self._json(503, {"success": False, "message": "账号状态正在更新,请稍后再试"})
            try:
                RG.session_revoke_identity("student", str(sid))
            except RuntimeError:
                return self._json(503, {"success": False, "message": "会话服务不可用,请稍后再试"})
            reg = None
            with DB_POOL.connection() as conn:
                cur = conn.cursor()
                try:
                    conn.execute("BEGIN IMMEDIATE")
                    cur.execute(
                        "SELECT id, club_id, operation_id FROM registrations WHERE student_id = ?",
                        (sid,))
                    reg = cur.fetchone()
                    cur.execute("DELETE FROM registrations WHERE student_id = ?", (sid,))
                    cur.execute("DELETE FROM students WHERE id = ?", (sid,))
                    if cur.rowcount == 0:
                        conn.rollback()
                        return self._json(404, {"success": False, "message": "学生不存在"})
                    if reg:
                        cur.execute(
                            "UPDATE clubs SET current_students = MAX(0, current_students - 1) WHERE id = ?",
                            (reg[1],))
                    conn.commit()
                except sqlite3.Error as e:
                    conn.rollback()
                    log.error("删除学生失败: %s", e)
                    return self._json(500, {"success": False, "message": "删除失败"})
            if reg:
                registration_id, club_id, operation_id = reg
                value = RG.reservation_value(club_id, operation_id) if operation_id else str(club_id)
                try:
                    event_id = operation_id or "legacy-{}-{}-{}".format(
                        registration_id, sid, club_id)
                    RG.release_registration(event_id, sid, club_id, value)
                except RuntimeError as e:
                    log.error("删除学生后名额同步失败 reg=%s: %s", registration_id, e)
                    RECONCILE_REQUESTED.set()
                    return self._json(503, {"success": False, "message": "学生已删除,名额同步失败"})
            self._json(200, {"success": True, "message": "学生删除成功"})
        finally:
            RG.session_end_mutation(session_lock)
            RG.end_maintenance(maintenance)

    def _h_delete_all_students(self, sess, data):
        maintenance = RG.begin_maintenance()
        if not maintenance:
            return self._json(503, {"success": False, "message": "有报名请求处理中,请稍后再试"})
        session_lock = None
        try:
            try:
                session_lock = RG.session_begin_mutation("student")
            except RuntimeError:
                return self._json(503, {"success": False, "message": "会话服务不可用,请稍后重试"})
            if not session_lock:
                return self._json(503, {"success": False, "message": "账号状态正在更新,请稍后再试"})
            try:
                # One role generation invalidates every current student token
                # without a SCAN over `sess:*`, so this stays bounded even for
                # a large import/delete operation.
                RG.session_revoke_role("student")
            except RuntimeError:
                return self._json(503, {"success": False, "message": "会话服务不可用,请稍后再试"})
            with DB_POOL.connection() as conn:
                try:
                    conn.execute("BEGIN IMMEDIATE")
                    conn.execute("DELETE FROM registrations")
                    conn.execute("DELETE FROM students")
                    conn.execute("UPDATE clubs SET current_students = 0")
                    conn.commit()
                except sqlite3.Error as e:
                    conn.rollback()
                    log.error("清空学生失败: %s", e)
                    return self._json(500, {"success": False, "message": "删除失败"})
            if not rebuild_stock(maintenance_token=maintenance):
                RECONCILE_REQUESTED.set()
                return self._json(503, {"success": False,
                                        "message": "学生已删除,名额重建失败"})
            self._json(200, {"success": True, "message": "所有学生数据已删除"})
        finally:
            RG.session_end_mutation(session_lock)
            RG.end_maintenance(maintenance)

    def _h_delete_club(self, sess, data):
        club_id = data.get("club_id")
        try:
            club_id = int(club_id)
        except (TypeError, ValueError):
            return self._json(400, {"success": False, "message": "缺少或非法的社团ID"})
        maintenance = RG.begin_maintenance()
        if not maintenance:
            return self._json(503, {"success": False, "message": "有报名请求处理中,请稍后再试"})
        try:
            with DB_POOL.connection() as conn:
                cur = conn.cursor()
                try:
                    conn.execute("BEGIN IMMEDIATE")
                    cur.execute("SELECT COUNT(*) FROM registrations WHERE club_id = ?", (club_id,))
                    if cur.fetchone()[0] > 0:
                        conn.rollback()
                        return self._json(400, {"success": False, "message": "该社团已有学生报名,无法删除"})
                    cur.execute("DELETE FROM clubs WHERE id = ?", (club_id,))
                    if cur.rowcount == 0:
                        conn.rollback()
                        return self._json(404, {"success": False, "message": "社团不存在"})
                    conn.commit()
                except sqlite3.Error as e:
                    conn.rollback()
                    log.error("删除社团失败: %s", e)
                    return self._json(500, {"success": False, "message": "删除失败"})
            try:
                RG.r.delete(K_STOCK.format(club_id))
                RG.cache_del(K_CACHE_CLUBS)
            except Exception as e:  # noqa: BLE001
                log.error("社团已删除但 Redis 清理失败 cid=%s: %s", club_id, e)
                RECONCILE_REQUESTED.set()
                return self._json(503, {"success": False,
                                        "message": "社团已删除,后台将安全清理名额状态"})
            self._json(200, {"success": True, "message": "社团删除成功"})
        finally:
            RG.end_maintenance(maintenance)

    def _h_delete_all_clubs(self, sess, data):
        maintenance = RG.begin_maintenance()
        if not maintenance:
            return self._json(503, {"success": False, "message": "有报名请求处理中,请稍后再试"})
        try:
            with DB_POOL.connection() as conn:
                try:
                    conn.execute("BEGIN IMMEDIATE")
                    conn.execute("DELETE FROM registrations")
                    conn.execute("DELETE FROM clubs")
                    conn.commit()
                except sqlite3.Error as e:
                    conn.rollback()
                    log.error("清空社团失败: %s", e)
                    return self._json(500, {"success": False, "message": "删除失败"})
            if not rebuild_stock(maintenance_token=maintenance):
                RECONCILE_REQUESTED.set()
                return self._json(503, {"success": False,
                                        "message": "社团已删除,名额重建失败"})
            self._json(200, {"success": True, "message": "所有社团数据已删除"})
        finally:
            RG.end_maintenance(maintenance)


# ==========================================================================
# 抬高 backlog 的多线程服务器
# ==========================================================================
class Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True
    request_queue_size = 256   # 抬高 listen backlog(默认 5 -> 开放瞬间不被 reset)

    def __init__(self, *args, **kwargs):
        self._request_slots = threading.BoundedSemaphore(MAX_HTTP_WORKERS)
        super().__init__(*args, **kwargs)

    def process_request(self, request, client_address):
        if not self._request_slots.acquire(blocking=False):
            body = b'{"success":false,"message":"service busy"}'
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
    reconciler = threading.Thread(target=reconcile_worker, name="seat-reconciler", daemon=True)
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
    main()
