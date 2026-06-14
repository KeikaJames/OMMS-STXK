#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
社团选课系统 —— Python 服务(硬化版 / 管理面 + 迁移期热路径)

设计要点(见 ~/.claude/plans/debug-bug-virtual-emerson.md):
  * 连接池:queue.Queue 阻塞池(修掉手搓池的死锁/泄漏),每连接 WAL/FK/busy_timeout/autocommit。
  * 名额:Redis 原子 Lua 抢占(根治超卖),SQLite 仅落库;current_students 为派生镜像。
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
from datetime import datetime
from http import cookies as http_cookies

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
POOL_SIZE = int(os.environ.get("DB_POOL_SIZE", "12"))
SESSION_TTL = int(os.environ.get("SESSION_TTL", str(8 * 3600)))
RESV_TTL = int(os.environ.get("RESV_TTL", "15"))         # 抢占预留 TTL(秒)
LOGIN_MAX_FAILS = int(os.environ.get("LOGIN_MAX_FAILS", "10"))        # 每分钟每用户失败上限
LOGIN_IP_MAX_FAILS = int(os.environ.get("LOGIN_IP_MAX_FAILS", "50"))  # 每分钟每 IP 失败上限(放宽,避免校园 NAT 误伤)
MAX_BODY = int(os.environ.get("MAX_BODY", str(8 * 1024 * 1024)))  # 请求体上限

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("club")

# Redis 键契约(与 Rust 热服务一致)
K_STOCK = "stock:club:{}"        # 剩余名额(真相源)
K_STUREG = "student:reg:{}"      # 已确认报名 -> club_id
K_RESV = "resv:{}"               # 抢占预留态(TTL)
K_SESS = "sess:{}"               # 会话 token -> JSON
K_OPENAT = "open_at"             # 报名开放 epoch 秒
K_CACHE_CLUBS = "cache:clubs"
K_INIT = "seats:initialized"

# 抢名额 Lua:KEYS[1]=stock, KEYS[2]=student:reg, KEYS[3]=resv;ARGV[1]=club_id, ARGV[2]=ttl
# 返回 1 成功 / 0 满员 / -1 已报名(含重复并发)/ -2 未初始化
LUA_ACQUIRE = """
if redis.call('EXISTS', KEYS[1]) == 0 then return -2 end
if redis.call('EXISTS', KEYS[2]) == 1 then return -1 end
if redis.call('EXISTS', KEYS[3]) == 1 then return -1 end
local left = tonumber(redis.call('GET', KEYS[1]))
if left <= 0 then return 0 end
redis.call('DECR', KEYS[1])
redis.call('SET', KEYS[3], ARGV[1], 'EX', tonumber(ARGV[2]))
return 1
"""

# ==========================================================================
# Redis 客户端(优雅降级:不可用时读路径回落、写路径拒绝)
# ==========================================================================
class RedisGate:
    def __init__(self, url):
        self._url = url
        self._r = None
        self._acquire = None
        if _redis is not None:
            try:
                self._r = _redis.Redis.from_url(
                    url, decode_responses=True,
                    socket_timeout=0.5, socket_connect_timeout=0.5,
                )
                self._r.ping()
                self._acquire = self._r.register_script(LUA_ACQUIRE)
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

    def acquire_seat(self, student_id, club_id):
        """原子抢占。返回 1/0/-1/-2;Redis 不可用抛 RuntimeError。"""
        if self._r is None or self._acquire is None:
            raise RuntimeError("redis unavailable")
        return int(self._acquire(
            keys=[K_STOCK.format(club_id), K_STUREG.format(student_id), K_RESV.format(student_id)],
            args=[club_id, RESV_TTL],
        ))

    def confirm_seat(self, student_id, club_id):
        try:
            pipe = self._r.pipeline()
            pipe.set(K_STUREG.format(student_id), club_id)
            pipe.delete(K_RESV.format(student_id))
            pipe.execute()
        except Exception as e:  # noqa: BLE001
            log.error("confirm_seat 失败 sid=%s: %s", student_id, e)

    def release_seat(self, student_id, club_id):
        try:
            pipe = self._r.pipeline()
            pipe.incr(K_STOCK.format(club_id))
            pipe.delete(K_RESV.format(student_id))
            pipe.delete(K_STUREG.format(student_id))
            pipe.execute()
        except Exception as e:  # noqa: BLE001
            log.error("release_seat 失败 sid=%s: %s", student_id, e)

    def stock_left(self, club_ids):
        """批量取剩余名额 dict{club_id:int};不可用返回 None。"""
        if self._r is None or not club_ids:
            return None
        try:
            vals = self._r.mget([K_STOCK.format(c) for c in club_ids])
            return {c: (int(v) if v is not None else None) for c, v in zip(club_ids, vals)}
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
    def session_create(self, payload):
        token = secrets.token_urlsafe(32)
        if self._r is not None:
            try:
                self._r.set(K_SESS.format(token), json.dumps(payload), ex=SESSION_TTL)
                return token
            except Exception as e:  # noqa: BLE001
                log.error("session_create 失败: %s", e)
        _MEM_SESSIONS[token] = payload   # 极端降级:内存兜底(单进程)
        return token

    def session_get(self, token):
        if not token:
            return None
        if self._r is not None:
            try:
                raw = self._r.get(K_SESS.format(token))
                return json.loads(raw) if raw else None
            except Exception:  # noqa: BLE001
                pass
        return _MEM_SESSIONS.get(token)

    def session_del(self, token):
        if not token:
            return
        if self._r is not None:
            try:
                self._r.delete(K_SESS.format(token))
            except Exception:  # noqa: BLE001
                pass
        _MEM_SESSIONS.pop(token, None)

    def login_blocked(self, key, limit):
        """只读检查:失败计数是否超限(成功登录不计数,避免校园 NAT 误伤)。"""
        if self._r is None:
            return False
        try:
            n = self._r.get("loginfail:{}".format(key))
            return int(n or 0) > limit
        except Exception:  # noqa: BLE001
            return False

    def login_fail(self, key):
        """仅在登录失败时计数。"""
        if self._r is None:
            return
        try:
            k = "loginfail:{}".format(key)
            if self._r.incr(k) == 1:
                self._r.expire(k, 60)
        except Exception:  # noqa: BLE001
            pass

    def login_ok(self, key):
        """登录成功清零该用户失败计数。"""
        if self._r is not None:
            try:
                self._r.delete("loginfail:{}".format(key))
            except Exception:  # noqa: BLE001
                pass

    def open_at_set(self, epoch):
        if self._r is not None:
            try:
                self._r.set(K_OPENAT, int(epoch))
            except Exception:  # noqa: BLE001
                pass

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


_MEM_SESSIONS = {}   # Redis 全挂时的单进程会话兜底
RG = RedisGate(REDIS_URL)


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
_BAD_CHARS = set('<>"\'&')


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
                FOREIGN KEY (student_id) REFERENCES students (id),
                FOREIGN KEY (club_id) REFERENCES clubs (id),
                UNIQUE (student_id));
            CREATE INDEX IF NOT EXISTS idx_registrations_club_id ON registrations(club_id);
            CREATE TABLE IF NOT EXISTS settings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                registration_start_time TEXT,
                admin_username TEXT DEFAULT 'admin',
                admin_password TEXT DEFAULT 'admin123');
            """
        )
        # 初始化 settings
        cur.execute("SELECT id, admin_password FROM settings LIMIT 1")
        row = cur.fetchone()
        if row is None:
            cur.execute(
                "INSERT INTO settings (registration_start_time, admin_password) VALUES (?, ?)",
                (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), hash_password("admin123")),
            )
            log.info("初始化 settings,默认管理员 admin/admin123(已哈希,请尽快改密)")
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
    rebuild_stock()
    seed_open_at()


def rebuild_stock():
    """以 registrations 实计重建 Redis 名额 + 占位镜像(幂等)。"""
    if not RG.alive():
        log.warning("Redis 不可用,跳过名额重建(秒杀能力降级)")
        return
    try:
        with DB_POOL.connection() as conn:
            cur = conn.cursor()
            cur.execute(
                "SELECT c.id, c.max_students, "
                "(SELECT COUNT(*) FROM registrations r WHERE r.club_id=c.id) "
                "FROM clubs c"
            )
            rows = cur.fetchall()
            cur.execute("SELECT student_id, club_id FROM registrations")
            regs = cur.fetchall()
        pipe = RG.r.pipeline()
        for k in RG.r.scan_iter(match="stock:club:*"):
            pipe.delete(k)
        for k in RG.r.scan_iter(match="student:reg:*"):
            pipe.delete(k)
        for cid, maxs, used in rows:
            pipe.set(K_STOCK.format(cid), max(0, int(maxs) - int(used)))
        for sid, cid in regs:
            pipe.set(K_STUREG.format(sid), cid)
        pipe.set(K_INIT, "1")
        pipe.execute()
        RG.cache_del(K_CACHE_CLUBS)
        log.info("Redis 名额已重建: %d 个社团", len(rows))
    except Exception as e:  # noqa: BLE001
        log.error("rebuild_stock 失败: %s", e)


def seed_open_at():
    try:
        with DB_POOL.connection() as conn:
            cur = conn.cursor()
            cur.execute("SELECT registration_start_time FROM settings ORDER BY id DESC LIMIT 1")
            row = cur.fetchone()
        if row and row[0]:
            try:
                dt = datetime.strptime(row[0], "%Y-%m-%d %H:%M:%S")
                RG.open_at_set(int(dt.timestamp()))
            except ValueError:
                log.warning("settings.registration_start_time 格式非法,未写入 open_at")
    except Exception as e:  # noqa: BLE001
        log.error("seed_open_at 失败: %s", e)


# ==========================================================================
# 路由角色表(public / student / admin)
# ==========================================================================
ROLE_PUBLIC, ROLE_STUDENT, ROLE_ADMIN = "public", "student", "admin"

GET_ROUTES = {
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
    "/api/import_students": ("_h_import_students", ROLE_ADMIN),
    "/api/import_clubs": ("_h_import_clubs", ROLE_ADMIN),
    "/api/update_registration_time": ("_h_update_time", ROLE_ADMIN),
    "/api/delete_student": ("_h_delete_student", ROLE_ADMIN),
    "/api/delete_all_students": ("_h_delete_all_students", ROLE_ADMIN),
    "/api/delete_club": ("_h_delete_club", ROLE_ADMIN),
    "/api/delete_all_clubs": ("_h_delete_all_clubs", ROLE_ADMIN),
}
PAGES = {
    "/": "login.html",
    "/student/dashboard": "student_dashboard.html",
    "/student/profile": "student_profile.html",
    "/admin/dashboard": "admin_dashboard.html",
}
STATIC = {
    "/app.css": ("app.css", "text/css; charset=utf-8"),
    "/favicon.svg": ("favicon.svg", "image/svg+xml"),
}


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
        w.writerow(header)
        for r in rows:
            w.writerow(r)
        body = out.getvalue().encode("utf-8-sig")  # BOM 便于 Excel 识别中文
        self._send(200, body, ctype="text/csv; charset=utf-8",
                   extra=[("Content-Disposition", 'attachment; filename="{}"'.format(filename))])

    def log_message(self, fmt, *args):  # 默认 stderr 噪声改走 logging
        log.debug("%s - %s", self.address_string(), fmt % args)

    # ---- 会话 ----
    def _session(self):
        raw = self.headers.get("Cookie")
        if not raw:
            return None
        try:
            c = http_cookies.SimpleCookie(raw)
        except http_cookies.CookieError:
            return None
        morsel = c.get("session")
        if not morsel:
            return None
        return RG.session_get(morsel.value)

    def _set_session_cookie(self, token):
        return ("Set-Cookie",
                "session={}; HttpOnly; SameSite=Strict; Path=/; Max-Age={}".format(token, SESSION_TTL))

    def _clear_cookie(self):
        return ("Set-Cookie", "session=; HttpOnly; SameSite=Strict; Path=/; Max-Age=0")

    def _require(self, role):
        """返回 session(public 时可为 None)。鉴权失败时已发响应并返回 False。"""
        if role == ROLE_PUBLIC:
            return self._session()
        sess = self._session()
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

    def _body(self):
        """读 JSON body:校验 Content-Length、上限、解析,失败抛 ValueError。"""
        cl = self.headers.get("Content-Length")
        if cl is None:
            raise ValueError("缺少 Content-Length")
        try:
            n = int(cl)
        except ValueError:
            raise ValueError("非法 Content-Length")
        if n < 0 or n > MAX_BODY:
            raise ValueError("请求体过大")
        data = self.rfile.read(n) if n else b""
        if not data:
            return {}
        return json.loads(data)

    # ---- 分发 ----
    def do_GET(self):
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
        path = self.path.split("?")[0]
        route = POST_ROUTES.get(path)
        if route is None:
            return self._json(404, {"success": False, "message": "未找到"})
        name, role = route
        sess = self._require(role)
        if sess is False:
            return
        try:
            data = self._body()
        except ValueError as e:
            return self._json(400, {"success": False, "message": "请求格式错误: {}".format(e)})
        except json.JSONDecodeError:
            return self._json(400, {"success": False, "message": "JSON 解析失败"})
        try:
            getattr(self, name)(sess, data)
        except Exception as e:  # noqa: BLE001
            log.exception("POST %s 处理异常: %s", path, e)
            self._json(500, {"success": False, "message": "服务器错误"})

    # ---- 静态页(白名单;无通用文件嗅探,消灭整库/源码下载与遍历) ----
    def _serve_page(self, fname):
        try:
            with open(fname, "rb") as f:
                body = f.read()
        except OSError:
            return self._json(404, {"success": False, "message": "页面不存在"})
        self._send(200, body, ctype="text/html; charset=utf-8")

    def _serve_static(self, spec):
        fname, ctype = spec
        try:
            with open(fname, "rb") as f:
                body = f.read()
        except OSError:
            return self._json(404, {"success": False, "message": "资源不存在"})
        self._send(200, body, ctype=ctype, extra=[("Cache-Control", "public, max-age=300")])

    # ======================================================================
    # 公共端点
    # ======================================================================
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
        live = RG.stock_left(ids)  # 实时名额(零滞后);None 则回落 current_students
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
        username = (data.get("username") or "").strip()
        password = data.get("password") or ""
        if not username or not password:
            return self._json(400, {"success": False, "message": "用户名和密码不能为空"})
        ip = self.client_address[0]
        if RG.login_blocked("u:" + username, LOGIN_MAX_FAILS) or RG.login_blocked("ip:" + ip, LOGIN_IP_MAX_FAILS):
            return self._json(429, {"success": False, "message": "尝试过于频繁,请稍后再试"})
        with DB_POOL.connection() as conn:
            cur = conn.cursor()
            cur.execute("SELECT id, name, class, student_id, password FROM students WHERE username = ?",
                        (username,))
            row = cur.fetchone()
            if not row:
                RG.login_fail("u:" + username); RG.login_fail("ip:" + ip)
                return self._json(401, {"success": False, "message": "用户名或密码错误"})
            sid, name, klass, student_no, stored = row
            ok, upgrade = verify_password(stored, password)
            if not ok:
                RG.login_fail("u:" + username); RG.login_fail("ip:" + ip)
                return self._json(401, {"success": False, "message": "用户名或密码错误"})
            if upgrade:
                try:
                    cur.execute("UPDATE students SET password = ? WHERE id = ?",
                                (hash_password(password), sid))
                except sqlite3.Error:
                    pass
        RG.login_ok("u:" + username)
        token = RG.session_create({"role": "student", "student_id": sid,
                                   "name": name, "class": klass, "student_no": student_no})
        self._json(200, {"success": True, "student_id": sid, "name": name,
                         "class": klass, "student_no": student_no},
                   extra=[self._set_session_cookie(token)])

    def _h_admin_login(self, sess, data):
        username = (data.get("username") or "").strip()
        password = data.get("password") or ""
        if not username or not password:
            return self._json(400, {"success": False, "message": "用户名和密码不能为空"})
        ip = self.client_address[0]
        if RG.login_blocked("admin:" + ip, LOGIN_MAX_FAILS):
            return self._json(429, {"success": False, "message": "尝试过于频繁,请稍后再试"})
        with DB_POOL.connection() as conn:
            cur = conn.cursor()
            cur.execute("SELECT id, admin_password FROM settings WHERE admin_username = ?", (username,))
            row = cur.fetchone()
        if not row:
            RG.login_fail("admin:" + ip)
            return self._json(401, {"success": False, "message": "用户名或密码错误"})
        ok, upgrade = verify_password(row[1], password)
        if not ok:
            RG.login_fail("admin:" + ip)
            return self._json(401, {"success": False, "message": "用户名或密码错误"})
        RG.login_ok("admin:" + ip)
        if upgrade:
            try:
                with DB_POOL.connection() as conn:
                    conn.execute("UPDATE settings SET admin_password = ? WHERE id = ?",
                                 (hash_password(password), row[0]))
            except sqlite3.Error:
                pass
        token = RG.session_create({"role": "admin", "username": username})
        self._json(200, {"success": True}, extra=[self._set_session_cookie(token)])

    def _h_logout(self, sess, data):
        raw = self.headers.get("Cookie")
        if raw:
            try:
                c = http_cookies.SimpleCookie(raw)
                if c.get("session"):
                    RG.session_del(c["session"].value)
            except http_cookies.CookieError:
                pass
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

        # 后端开放时间闸
        open_at = RG.open_at_get()
        if open_at is None or RG.now_epoch() < open_at:
            return self._json(200, {"success": False, "message": "报名尚未开始"})

        # Redis 原子抢占(根治超卖)
        try:
            code = RG.acquire_seat(sid, club_id)
        except RuntimeError:
            return self._json(503, {"success": False, "message": "系统繁忙,请稍后重试"})
        if code == -2:
            rebuild_stock()
            try:
                code = RG.acquire_seat(sid, club_id)
            except RuntimeError:
                return self._json(503, {"success": False, "message": "系统繁忙,请稍后重试"})
        if code == 0:
            return self._json(200, {"success": False, "message": "该社团已满员"})
        if code == -1:
            return self._json(200, {"success": False, "message": "您已报名其他社团或请勿重复提交"})
        if code != 1:
            return self._json(200, {"success": False, "message": "社团不存在或暂不可报名"})

        # 抢到 -> 同步落库
        try:
            with DB_POOL.connection() as conn:
                cur = conn.cursor()
                conn.execute("BEGIN")
                cur.execute(
                    "INSERT INTO registrations (student_id, club_id, registration_time) VALUES (?,?,?)",
                    (sid, club_id, datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
                cur.execute("UPDATE clubs SET current_students = current_students + 1 WHERE id = ?",
                            (club_id,))
                conn.commit()
            RG.confirm_seat(sid, club_id)
            self._json(200, {"success": True, "message": "报名成功"})
        except Exception as e:  # noqa: BLE001
            log.error("报名落库失败 sid=%s club=%s: %s", sid, club_id, e)
            RG.release_seat(sid, club_id)  # 回补名额
            self._json(200, {"success": False, "message": "报名失败,请重试"})

    def _h_cancel_registration(self, sess, data):
        sid = sess["student_id"]
        with DB_POOL.connection() as conn:
            cur = conn.cursor()
            cur.execute("SELECT club_id FROM registrations WHERE student_id = ?", (sid,))
            reg = cur.fetchone()
            if not reg:
                return self._json(200, {"success": False, "message": "您还未报名任何社团"})
            club_id = reg[0]
            try:
                conn.execute("BEGIN")
                cur.execute("DELETE FROM registrations WHERE student_id = ?", (sid,))
                cur.execute(
                    "UPDATE clubs SET current_students = MAX(0, current_students - 1) WHERE id = ?",
                    (club_id,))
                conn.commit()
            except sqlite3.Error as e:
                conn.rollback()
                log.error("退选失败 sid=%s: %s", sid, e)
                return self._json(200, {"success": False, "message": "取消报名失败,请重试"})
        RG.release_seat(sid, club_id)  # 名额还回
        self._json(200, {"success": True, "message": "取消报名成功"})

    def _h_change_password(self, sess, data):
        sid = sess["student_id"]
        cur_pw = data.get("current") or ""
        new_pw = data.get("new") or ""
        if len(new_pw) < 6:
            return self._json(400, {"success": False, "message": "新密码至少 6 位"})
        with DB_POOL.connection() as conn:
            c = conn.cursor()
            c.execute("SELECT password FROM students WHERE id = ?", (sid,))
            row = c.fetchone()
            if not row:
                return self._json(404, {"success": False, "message": "用户不存在"})
            ok, _ = verify_password(row[0], cur_pw)
            if not ok:
                return self._json(400, {"success": False, "message": "当前密码不正确"})
            conn.execute("UPDATE students SET password = ? WHERE id = ?",
                         (hash_password(new_pw), sid))
        self._json(200, {"success": True, "message": "密码已修改"})

    # ======================================================================
    # 管理端点(均 admin 鉴权)
    # ======================================================================
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
        if not students:
            return self._json(400, {"success": False, "message": "没有学生数据"})
        results = {"success": 0, "failed": 0}
        credentials = []  # 一次性回显明文供管理员下发
        try:
            with DB_POOL.connection() as conn:
                cur = conn.cursor()
                conn.execute("BEGIN")
                seen = set()
                for st in students:
                    name = clean_text(st.get("name"))
                    klass = clean_text(st.get("class"))
                    student_no = clean_text(st.get("student_id"), maxlen=40)
                    if not name or not klass or not student_no:
                        results["failed"] += 1
                        continue
                    username = gen_username(name, cur, seen)
                    plain = gen_password()
                    try:
                        cur.execute(
                            "INSERT INTO students (name, class, student_id, username, password) "
                            "VALUES (?,?,?,?,?)",
                            (name, klass, student_no, username, hash_password(plain)))
                        results["success"] += 1
                        credentials.append({"name": name, "username": username, "password": plain})
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
        clubs = data.get("clubs", [])
        if not clubs:
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
                    name = clean_text(cb.get("name"))
                    maxs = cb.get("max_students")
                    if not name or name in existing or not isinstance(maxs, int) or maxs <= 0:
                        results["failed"] += 1
                        continue
                    try:
                        cur.execute("INSERT INTO clubs (name, max_students, current_students) VALUES (?,?,0)",
                                    (name, maxs))
                        new_ids.append((cur.lastrowid, maxs))
                        existing.add(name)
                        results["success"] += 1
                    except sqlite3.IntegrityError:
                        results["failed"] += 1
                conn.commit()
        except Exception as e:  # noqa: BLE001
            log.error("导入社团失败: %s", e)
            return self._json(500, {"success": False, "message": "导入失败"})
        if RG.alive():
            for cid, maxs in new_ids:
                try:
                    RG.r.set(K_STOCK.format(cid), maxs)
                except Exception:  # noqa: BLE001
                    pass
            RG.cache_del(K_CACHE_CLUBS)
        self._json(200, {"success": results["success"], "failed": results["failed"]})

    def _h_update_time(self, sess, data):
        start_time = (data.get("start_time") or "").strip()
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

    def _h_delete_student(self, sess, data):
        sid = data.get("student_id")
        try:
            sid = int(sid)
        except (TypeError, ValueError):
            return self._json(400, {"success": False, "message": "缺少或非法的学生ID"})
        reg = None
        with DB_POOL.connection() as conn:
            cur = conn.cursor()
            try:
                conn.execute("BEGIN")
                cur.execute("SELECT club_id FROM registrations WHERE student_id = ?", (sid,))
                reg = cur.fetchone()
                cur.execute("DELETE FROM registrations WHERE student_id = ?", (sid,))
                cur.execute("DELETE FROM students WHERE id = ?", (sid,))
                if cur.rowcount == 0:
                    conn.rollback()
                    return self._json(404, {"success": False, "message": "学生不存在"})
                if reg:
                    cur.execute(
                        "UPDATE clubs SET current_students = MAX(0, current_students - 1) WHERE id = ?",
                        (reg[0],))
                conn.commit()
            except sqlite3.Error as e:
                conn.rollback()
                log.error("删除学生失败: %s", e)
                return self._json(500, {"success": False, "message": "删除失败"})
        if reg and RG.alive():
            RG.release_seat(sid, reg[0])
        self._json(200, {"success": True, "message": "学生删除成功"})

    def _h_delete_all_students(self, sess, data):
        with DB_POOL.connection() as conn:
            try:
                conn.execute("BEGIN")
                conn.execute("DELETE FROM registrations")
                conn.execute("DELETE FROM students")
                conn.execute("UPDATE clubs SET current_students = 0")
                conn.commit()
            except sqlite3.Error as e:
                conn.rollback()
                log.error("清空学生失败: %s", e)
                return self._json(500, {"success": False, "message": "删除失败"})
        rebuild_stock()
        self._json(200, {"success": True, "message": "所有学生数据已删除"})

    def _h_delete_club(self, sess, data):
        club_id = data.get("club_id")
        try:
            club_id = int(club_id)
        except (TypeError, ValueError):
            return self._json(400, {"success": False, "message": "缺少或非法的社团ID"})
        with DB_POOL.connection() as conn:
            cur = conn.cursor()
            try:
                conn.execute("BEGIN")
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
        if RG.alive():
            try:
                RG.r.delete(K_STOCK.format(club_id))
            except Exception:  # noqa: BLE001
                pass
            RG.cache_del(K_CACHE_CLUBS)
        self._json(200, {"success": True, "message": "社团删除成功"})

    def _h_delete_all_clubs(self, sess, data):
        with DB_POOL.connection() as conn:
            try:
                conn.execute("BEGIN")
                conn.execute("DELETE FROM registrations")
                conn.execute("DELETE FROM clubs")
                conn.commit()
            except sqlite3.Error as e:
                conn.rollback()
                log.error("清空社团失败: %s", e)
                return self._json(500, {"success": False, "message": "删除失败"})
        rebuild_stock()
        self._json(200, {"success": True, "message": "所有社团数据已删除"})


# ==========================================================================
# 抬高 backlog 的多线程服务器
# ==========================================================================
class Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True
    request_queue_size = 256   # 抬高 listen backlog(默认 5 -> 开放瞬间不被 reset)


def main():
    init_db()
    httpd = Server((HOST, PORT), ClubSystemHandler)
    log.info("Python 服务启动 http://%s:%d  (Redis=%s)", HOST, PORT,
             "on" if RG.alive() else "off/degraded")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        log.info("正在停止...")
    finally:
        httpd.shutdown()
        if DB_POOL:
            DB_POOL.close_all()


if __name__ == "__main__":
    main()
