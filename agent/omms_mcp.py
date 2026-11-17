#!/usr/bin/env python3
"""社团选课系统的 AI 代理接口：MCP 服务（stdio）与命令行入口，只用 Python 标准库。

    python3 omms_mcp.py                               作为 MCP 服务运行，供 AI 客户端连接
    python3 omms_mcp.py tools                         列出全部工具
    python3 omms_mcp.py call 工具名 '{"参数": "值"}'     调用一个工具，输出 JSON
    python3 omms_mcp.py register claude-desktop       在 Claude 桌面版的配置里登记本服务
    python3 omms_mcp.py register codex                在 Codex 的配置里登记本服务
    python3 omms_mcp.py register show                 输出各客户端的配置写法

管理操作经本机回环地址调用管理接口，以管理员身份登录；启动与停止调用 run.sh。
运行指南见同目录的 AGENTS.md。
"""

from __future__ import annotations

import contextlib
import datetime as dt
import http.client
import ipaddress
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import socket
import sqlite3
import subprocess
import sys
import time
import traceback
import urllib.parse

VERSION = "1.0.0"
AGENT_DIR = Path(__file__).resolve().parent
APP_DIR = AGENT_DIR.parent
RUN_SH = APP_DIR / "run.sh"
MAIN_PY = APP_DIR / "main.py"
GUIDE = AGENT_DIR / "AGENTS.md"
sys.path.insert(0, str(APP_DIR))
try:
    import netinfo  # noqa: E402  与控制台共用的局域网地址模块
except ImportError:  # pragma: no cover
    netinfo = None
PROTOCOL_VERSIONS = ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")
EXTRA_PATH = ("/opt/homebrew/bin", "/opt/homebrew/sbin", "/usr/local/bin", "/usr/local/sbin")
PASSWORD_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZabcdefghjkmnpqrstuvwxyz23456789"
IMPORT_BATCH = 200
ACCOUNT_FILE_ROWS = 5000
MAX_TABLE_BYTES = 10 * 1024 * 1024
MAX_IMAGE_BYTES = 10 * 1024 * 1024
MAX_RESTORE_BYTES = 64 * 1024 * 1024
IMAGE_TYPES = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png", ".webp": "image/webp", ".gif": "image/gif"}
LOG_FILES = {
    "python": "python.log", "rust": "rust.log", "nginx": "nginx-error.log", "access": "nginx-access.log",
    "redis": "redis.log", "build": "build.log", "start": "agent-start.log",
}
INSTRUCTIONS = (
    "本服务用于运行和管理鄂尔多斯市实验中学的社团选课系统，使用者是学校的管理员。"
    "所有操作通过本服务的工具完成；仓库中的程序、页面和配置模板保持只读，运行参数用 get_settings 与 update_settings 调整。"
    "删除、清空、恢复、重置密码、调剂、锁定和停止服务会改变数据或暂停报名：先向管理员说明影响并取得同意，再带 confirm: true 调用。"
    "学生名单、账号密码和导出文件只交给管理员本人。"
    "开始工作前调用 service_status 了解现状；完整的运行指南在资源 omms://guide。"
)

# 键、默认值、取值规则、说明。run.sh 只从 deploy/omms.env 读取这些键。
SETTINGS = (
    ("HTTP_PORT", "8080", ("port",), "学生与管理员访问的端口。设为 80 时访问地址只写 IP。"),
    ("TLS_CERT", "", ("file",), "HTTPS 证书文件的绝对路径，与 TLS_KEY 一起设置后改用 HTTPS。"),
    ("TLS_KEY", "", ("file",), "HTTPS 私钥文件的绝对路径。"),
    ("TLS_PORT", "8443", ("port",), "HTTPS 端口；启用 HTTPS 后，HTTP 端口的请求跳转到这里。"),
    ("REAL_IP_FROM", "", ("networks",), "内网穿透或反向代理所在的地址（IP 或网段，逗号分隔）。设置后按学生的真实地址限流。"),
    ("REAL_IP_HEADER", "X-Forwarded-For", ("header",), "学生真实地址所在的请求头；Cloudflare Tunnel 为 CF-Connecting-IP。"),
    ("APP_UTC_OFFSET", "+08:00", ("offset",), "学校所在时区，开放与截止时间按它解释。"),
    ("OMMS_PYTHON_ONLY", "0", ("bool",), "1：只运行 Python 服务，跳过 Rust 编译。"),
    ("OMMS_OPEN_BROWSER", "1", ("bool",), "1：双击启动后在本机浏览器打开控制台。"),
    ("PYTHON", "python3", ("command",), "运行服务所用的 Python 命令或绝对路径。"),
    ("BACKUP_KEEP", "30", ("int", 1, 1000), "保留的自动快照份数。"),
    ("MAX_IMPORT_STUDENTS", "500", ("int", 1, 5000), "单次导入请求的学生上限。"),
    ("SESSION_TTL", "28800", ("int", 600, 604800), "登录状态保持的秒数。"),
    ("LOGIN_MAX_FAILS", "10", ("int", 3, 100), "同一账号在同一设备上一分钟内允许输错密码的次数。"),
    ("AUTH_CONCURRENCY", "", ("int", 1, 64), "同时校验密码的线程数；留空时按核数自动选择。内存 2 GB 以下的电脑可设为 2。"),
    ("MAX_HTTP_WORKERS", "128", ("int", 8, 1024), "Python 服务的处理线程上限。"),
    ("SEATS_CACHE_MS", "500", ("int", 0, 5000), "名额快照的缓存毫秒数。"),
    ("LOG_LEVEL", "INFO", ("choice", "DEBUG", "INFO", "WARNING", "ERROR"), "服务日志级别。"),
)
SETTING_KEYS = tuple(item[0] for item in SETTINGS)

ACTION_LABELS = {
    "registration.created": "报名", "registration.cancelled": "退选",
    "registration.admin_assigned": "代为报名", "registration.admin_removed": "移出社团",
    "registration.admin_transferred": "调整社团", "registration.start_time_updated": "设置开放时间",
    "registration.end_time_updated": "设置截止时间", "registration.lock_updated": "名单锁定",
    "student.password_reset": "重置学生密码", "student.password_changed": "学生修改密码", "student.deleted": "删除学生",
    "student.updated": "修改学生信息", "students.passwords_reset": "按班重置密码", "students.imported": "导入学生",
    "students.deleted_all": "删除全部学生", "club.updated": "编辑社团", "club.deleted": "删除社团",
    "club.image_uploaded": "更换社团图片", "club.image_removed": "恢复默认图片", "clubs.imported": "添加社团",
    "clubs.deleted_all": "删除全部社团", "admin.password_changed": "修改管理员密码",
    "admin.password_reset": "重置管理员密码", "registrations.adjusted": "调剂", "registrations.cleared": "清空报名",
    "registrations.exported": "导出报名数据", "students.exported": "导出学生账号", "backup.downloaded": "下载备份",
    "backup.restored": "恢复备份",
}


class ToolError(Exception):
    """返回给代理的提示，说明下一步。"""


class NeedsConfirm(ToolError):
    """会改变数据或暂停报名的操作：说明影响，等管理员同意后带 confirm: true 再调用。"""

    def __init__(self, action: str, details: dict | None = None) -> None:
        text = "待确认：{}。向管理员说明并取得同意后，带 \"confirm\": true 再次调用。".format(action)
        if details:
            text += "\n" + dumps(details)
        super().__init__(text)


def dumps(value) -> str:
    return json.dumps(value, ensure_ascii=False, indent=1)


# ---------------------------------------------------------------------------
# 路径与运行参数
# ---------------------------------------------------------------------------
def settings_path() -> Path:
    return Path(os.environ.get("OMMS_SETTINGS") or APP_DIR / "deploy" / "omms.env")


def db_path() -> Path:
    return Path(os.environ.get("DB_PATH") or APP_DIR / "club_system.db").expanduser().resolve()


def run_dir() -> Path:
    return Path(os.environ.get("OMMS_RUN_DIR") or APP_DIR / "run").expanduser()


def export_dir() -> Path:
    return Path(os.environ.get("OMMS_EXPORT_DIR") or APP_DIR / "exports").expanduser()


def secret_file() -> Path:
    return Path(os.environ.get("ADMIN_SECRET_FILE") or db_path().parent / "admin-password.txt")


def backend_url() -> str:
    return os.environ.get("OMMS_BACKEND") or "http://127.0.0.1:2001"


def read_settings() -> tuple[dict, list]:
    values, lines = {}, []
    path = settings_path()
    if path.is_file():
        for line in path.read_text(encoding="utf-8").splitlines():
            lines.append(line)
            if "=" in line and not line.lstrip().startswith("#"):
                key, value = line.split("=", 1)
                values[key.strip()] = value.strip()
    return values, lines


def effective(key: str) -> str:
    """命令行环境优先，其次 deploy/omms.env，最后是默认值；与 run.sh 一致。"""
    if key in os.environ:
        return os.environ[key]
    values, _lines = read_settings()
    if key in values:
        return values[key]
    return next((item[1] for item in SETTINGS if item[0] == key), "")


def child_env(**extra) -> dict:
    env = dict(os.environ)
    parts = [part for part in env.get("PATH", "").split(os.pathsep) if part]
    front = [item for item in EXTRA_PATH + (str(Path.home() / ".cargo" / "bin"),)
             if item not in parts and os.path.isdir(item)]
    env["PATH"] = os.pathsep.join(front + (parts or ["/usr/bin", "/bin", "/usr/sbin", "/sbin"]))
    env.update({key: str(value) for key, value in extra.items()})
    return env


def python_command() -> str:
    command = effective("PYTHON") or "python3"
    return shutil.which(command, path=child_env()["PATH"]) or command


def school_tz() -> dt.timezone:
    match = re.fullmatch(r"([+-])(\d{2}):(\d{2})", effective("APP_UTC_OFFSET") or "+08:00")
    if not match:
        return dt.timezone(dt.timedelta(hours=8))
    minutes = int(match.group(2)) * 60 + int(match.group(3))
    return dt.timezone(dt.timedelta(minutes=minutes if match.group(1) == "+" else -minutes))


def school_time(ms) -> str | None:
    if not isinstance(ms, (int, float)) or isinstance(ms, bool):
        return None
    return dt.datetime.fromtimestamp(ms / 1000, school_tz()).strftime("%Y-%m-%d %H:%M:%S")


def parse_time(text, field: str) -> str:
    """接受 2026-11-20 08:00、2026/11/20 8:00、2026-11-20T08:00:00、2026年11月20日 8:00 等写法。"""
    value = str(text or "").strip().replace("年", "-").replace("月", "-").replace("日", " ").replace("：", ":")
    match = re.fullmatch(r"(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})[T\s]+(\d{1,2}):(\d{2})(?::(\d{2}))?", value.strip())
    if not match:
        raise ToolError("{} 的格式应为 YYYY-MM-DD HH:MM（学校时间），收到：{}".format(field, text))
    parts = [int(part) for part in match.groups(default="0")]
    try:
        moment = dt.datetime(*parts)
    except ValueError as exc:
        raise ToolError("{} 应为有效的日期时间：{}".format(field, text)) from exc
    return moment.strftime("%Y-%m-%d %H:%M:%S")


# ---------------------------------------------------------------------------
# 本机状态
# ---------------------------------------------------------------------------
def port_open(port: int, host: str = "127.0.0.1") -> bool:
    try:
        with socket.create_connection((host, port), timeout=0.5):
            return True
    except OSError:
        return False


def pid_alive(name: str) -> int | None:
    try:
        pid = int((run_dir() / (name + ".pid")).read_text().strip())
    except (OSError, ValueError):
        return None
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return None
    except PermissionError:
        return pid
    return pid


def lan_ip() -> str:
    if sys.platform == "darwin":
        with contextlib.suppress(OSError, subprocess.SubprocessError):
            route = subprocess.run(["route", "-n", "get", "default"], capture_output=True, text=True, timeout=3).stdout
            interface = re.search(r"interface:\s*(\S+)", route)
            if interface:
                address = subprocess.run(["ipconfig", "getifaddr", interface.group(1)],
                                         capture_output=True, text=True, timeout=3).stdout.strip()
                if address:
                    return address
    with contextlib.suppress(OSError):
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            probe.connect(("10.255.255.255", 1))
            return probe.getsockname()[0]
    return "127.0.0.1"


def web_scheme_port() -> tuple[str, int]:
    if effective("TLS_CERT") and effective("TLS_KEY"):
        return "https", int(effective("TLS_PORT") or 8443)
    return "http", int(effective("HTTP_PORT") or 8080)


def web_base(host: str) -> str:
    scheme, port = web_scheme_port()
    suffix = "" if (scheme, port) in (("http", 80), ("https", 443)) else ":{}".format(port)
    return "{}://{}{}".format(scheme, host, suffix)


def addresses() -> dict:
    """默认路由所在网卡的地址在前；机器同时接有线和无线时，其他网卡的地址另列。"""
    items = (netinfo.lan_addresses() if netinfo else []) or [{"address": lan_ip(), "label": ""}]
    base = web_base(items[0]["address"])
    result = {"学生访问": base, "管理控制台": base + "/admin/dashboard", "本机访问": web_base("127.0.0.1")}
    if items[0].get("label") and len(items) > 1:
        result["学生访问所在网卡"] = items[0]["label"]
    if len(items) > 1:
        result["其他网卡"] = ["{}{}".format(web_base(item["address"]), "（{}）".format(item["label"]) if item.get("label") else "")
                          for item in items[1:]]
    return result


def backend_port() -> int:
    return urllib.parse.urlsplit(backend_url()).port or 80


def redis_port() -> int:
    return urllib.parse.urlsplit(os.environ.get("REDIS_URL") or "redis://127.0.0.1:6379/0").port or 6379


def tail(text: str, lines: int = 20) -> str:
    return "\n".join(text.splitlines()[-lines:])


def open_target(target: str, reveal: bool = False) -> None:
    command = None
    if sys.platform == "darwin":
        command = ["open", "-R", target] if reveal else ["open", target]
    elif sys.platform.startswith("linux") and shutil.which("xdg-open"):
        command = ["xdg-open", os.path.dirname(target) if reveal else target]
    elif os.name == "nt":
        command = ["explorer", "/select," + target] if reveal else ["cmd", "/c", "start", "", target]
    if command:
        with contextlib.suppress(OSError):
            subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


# ---------------------------------------------------------------------------
# 管理员身份与管理接口
# ---------------------------------------------------------------------------
def run_admin_login() -> dict:
    """调用 main.py admin-login：核对密码文件与数据库（需要时生成新密码），并签发一次性登录链接。"""
    base = web_base("127.0.0.1") if port_open(web_scheme_port()[1]) else backend_url()
    command = [python_command(), str(MAIN_PY), "admin-login", base]
    env = child_env()
    env.setdefault("DB_PATH", str(db_path()))
    try:
        result = subprocess.run(command, cwd=str(APP_DIR), env=env, capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.SubprocessError) as exc:
        raise ToolError("请检查 Python 环境（运行 {}：{}）".format(" ".join(command[:2]), exc)) from exc
    values = {}
    for line in result.stdout.splitlines():
        key, sep, value = line.partition("=")
        if sep and re.fullmatch(r"[A-Z_]+", key):
            values[key] = value
    if values.get("ERROR"):
        raise ToolError("请检查数据库与 Python 依赖（读取管理员账号）：" + values["ERROR"])
    if not values.get("ADMIN_PASSWORD"):
        raise ToolError("请检查数据库与 Python 依赖（读取管理员账号）：" + tail(result.stderr or result.stdout, 6))
    return values


def file_credentials() -> tuple[str, str]:
    try:
        password = secret_file().read_text(encoding="utf-8").strip()
    except OSError:
        password = ""
    username = "admin"
    with contextlib.suppress(sqlite3.Error):
        uri = "file:{}?mode=ro".format(urllib.parse.quote(str(db_path())))
        with contextlib.closing(sqlite3.connect(uri, uri=True, timeout=5)) as conn:
            row = conn.execute("SELECT admin_username FROM settings ORDER BY id DESC LIMIT 1").fetchone()
            if row and row[0]:
                username = row[0]
    return username, password


class Reply:
    def __init__(self, status: int, headers: list, data: bytes) -> None:
        self.status = status
        self.headers = headers
        self.data = data

    def header(self, name: str) -> str:
        return next((value for key, value in self.headers if key.lower() == name.lower()), "")

    def json(self):
        try:
            return json.loads(self.data.decode("utf-8")) if self.data else None
        except (UnicodeDecodeError, ValueError):
            return None

    def filename(self) -> str:
        disposition = self.header("Content-Disposition")
        encoded = re.search(r"filename\*=UTF-8''([^;]+)", disposition, re.I)
        if encoded:
            return urllib.parse.unquote(encoded.group(1))
        plain = re.search(r'filename="?([^";]+)"?', disposition, re.I)
        return plain.group(1) if plain else ""

    def retry_after(self) -> float:
        try:
            return max(0.5, min(10.0, float(self.header("Retry-After") or 1)))
        except ValueError:
            return 1.0


class Backend:
    """以管理员身份调用 main.py 的管理接口；会话到期时自动重新登录。"""

    def __init__(self) -> None:
        self.cookie = ""
        self.username = ""
        self.password = ""

    def send(self, method: str, path: str, body: bytes | None = None, headers: dict | None = None,
             timeout: float = 60.0) -> Reply:
        parts = urllib.parse.urlsplit(backend_url())
        conn = http.client.HTTPConnection(parts.hostname or "127.0.0.1", parts.port or 80, timeout=timeout)
        request_headers = {"X-Requested-With": "XMLHttpRequest", "Connection": "close", "User-Agent": "omms-agent/" + VERSION}
        if self.cookie:
            request_headers["Cookie"] = self.cookie
        if body is not None:
            request_headers["Content-Length"] = str(len(body))
        request_headers.update(headers or {})
        try:
            conn.request(method, path, body=body, headers=request_headers)
            response = conn.getresponse()
            return Reply(response.status, response.getheaders(), response.read())
        except ConnectionRefusedError as exc:
            raise ToolError("管理服务需要启动（{}）：调用 start_service。".format(backend_url())) from exc
        except (OSError, http.client.HTTPException) as exc:
            raise ToolError("请稍后重试（连接管理服务：{}）".format(exc)) from exc
        finally:
            conn.close()

    def login(self, heal: bool = False) -> None:
        if heal or not self.password:
            if heal:
                values = run_admin_login()
                self.username, self.password = values.get("ADMIN_USER") or "admin", values["ADMIN_PASSWORD"]
            else:
                self.username, self.password = file_credentials()
                if not self.password:
                    return self.login(heal=True)
        body = json.dumps({"username": self.username, "password": self.password}).encode("utf-8")
        reply = self.send("POST", "/api/admin_login", body, {"Content-Type": "application/json"})
        data = reply.json() or {}
        if reply.status == 200 and data.get("success"):
            cookies = [value.split(";", 1)[0].strip() for key, value in reply.headers if key.lower() == "set-cookie"]
            self.cookie = "; ".join(cookie for cookie in cookies if cookie)
            return None
        if reply.status == 401 and not heal:
            return self.login(heal=True)
        raise ToolError("管理员登录：{}".format(data.get("message") or "HTTP {}".format(reply.status)))

    def api(self, method: str, path: str, payload=None, *, raw: bytes | None = None, filename: str = "",
            content_type: str = "application/octet-stream", binary: bool = False, retry: bool = True,
            timeout: float = 60.0):
        attempts = 0
        while True:
            attempts += 1
            if not self.cookie:
                self.login()
            headers, body = {}, None
            if raw is not None:
                body = raw
                headers["Content-Type"] = content_type
                if filename:
                    headers["X-File-Name"] = urllib.parse.quote(filename)
            elif method == "POST":
                body = json.dumps(payload if payload is not None else {}, ensure_ascii=False).encode("utf-8")
                headers["Content-Type"] = "application/json"
            reply = self.send(method, path, body, headers, timeout)
            if reply.status == 401 and attempts == 1:
                self.cookie = ""
                continue
            if reply.status == 503 and retry and attempts < 4:
                time.sleep(reply.retry_after())
                continue
            break
        if binary and 200 <= reply.status < 300:
            return reply
        data = reply.json()
        failed = not 200 <= reply.status < 300 or (isinstance(data, dict) and data.get("success") is False)
        if failed:
            text = data.get("message") if isinstance(data, dict) else ""
            raise ToolError(text or "请重试（HTTP {}）".format(reply.status))
        return data


BACKEND = Backend()


def get(path: str):
    return BACKEND.api("GET", path)


def post(path: str, payload: dict, **options):
    return BACKEND.api("POST", path, payload, **options)


def rid() -> str:
    return secrets.token_urlsafe(18)


# ---------------------------------------------------------------------------
# 数据整理
# ---------------------------------------------------------------------------
ROOM = re.compile(r"^(.*?)（\s*([0-9A-Za-z]+)\s*）\s*$")
NUMERALS = "一二三四五六七八九十"


def short_name(name: str) -> str:
    match = ROOM.match(name or "")
    return match.group(1).strip() if match else (name or "")


def natural_key(text: str) -> list:
    text = re.sub("[{}]".format(NUMERALS), lambda m: str(NUMERALS.index(m.group(0)) + 1), str(text))
    return [(0, int(part), "") if part.isdigit() else (1, 0, part) for part in re.findall(r"\d+|\D+", text)]


def clean_text(value, field: str, limit: int = 50) -> str:
    text = re.sub(r"\s+", " ", str(value if value is not None else "")).strip()
    if not text:
        raise ToolError("请填写{}".format(field))
    if len(text) > limit or re.search(r"[<>\x00-\x1f]", text):
        raise ToolError("{} 的长度应在 {} 字以内，且只含文字".format(field, limit))
    return text


def text_list(value) -> list:
    if value is None:
        return []
    items = value if isinstance(value, list) else re.split(r"[,，、;；\s]+", str(value))
    return [str(item).strip() for item in items if str(item).strip()]


def phase_text(info: dict) -> str:
    if info.get("registration_locked"):
        return "名单已锁定"
    if info.get("can_register"):
        return "报名中"
    if info.get("start_time"):
        return "待开放"
    return "开放时间待定"


def all_clubs() -> list:
    data = get("/api/get_clubs")
    return data if isinstance(data, list) else []


def all_students() -> list:
    data = get("/api/get_all_students")
    return data if isinstance(data, list) else []


def club_row(club: dict) -> dict:
    custom = isinstance(club.get("image_path"), str) and club["image_path"].startswith("/club-images/")
    return {
        "编号": club["id"], "名称": club["name"], "人数上限": club["max_students"], "已报名": club["current_students"],
        "剩余": club["max_students"] - club["current_students"], "开放报名": club["enabled"],
        "年级范围": club.get("allowed_grades") or "全部年级", "班级范围": club.get("allowed_classes") or "全部班级",
        "简介": club.get("description") or "", "指导老师": club.get("advisor_name") or "",
        "活动时间": club.get("meeting_time") or "", "地点": club.get("location") or "",
        "图片": "上传的图片" if custom else "默认插图",
    }


def find_club(ref, clubs: list | None = None) -> dict:
    clubs = all_clubs() if clubs is None else clubs
    text = str(ref if ref is not None else "").strip()
    if not text:
        raise ToolError("请指定社团（名称或编号）")
    if text.isdigit():
        for club in clubs:
            if str(club["id"]) == text:
                return club
    exact = [club for club in clubs if text in (club["name"], short_name(club["name"]))]
    if len(exact) == 1:
        return exact[0]
    partial = [club for club in clubs if text in club["name"]]
    if len(partial) == 1:
        return partial[0]
    candidates = exact or partial
    if candidates:
        raise ToolError("“{}”对应多个社团：{}".format(text, "、".join(club["name"] for club in candidates)))
    raise ToolError("未找到社团“{}”。现有社团：{}".format(text, "、".join(club["name"] for club in clubs) or "无"))


def student_row(student: dict, clubs: dict) -> dict:
    club = clubs.get(student.get("club_id"))
    return {"姓名": student["name"], "班级": student["class"], "账号": student["username"],
            "社团": club["name"] if club else ("社团 {}".format(student["club_id"]) if student.get("club_id") else "未报名")}


def find_student(ref, students: list | None = None) -> dict:
    students = all_students() if students is None else students
    text = str(ref if ref is not None else "").strip()
    if not text:
        raise ToolError("请指定学生（账号，或“姓名 班级”）")
    for student in students:
        if student["username"] == text:
            return student
    match = re.fullmatch(r"(.+?)\s*[（(]\s*(.+?)\s*[)）]", text) or re.fullmatch(r"(\S+)\s+(\S+)", text)
    name, klass = (match.group(1), match.group(2)) if match else (text, None)
    found = [student for student in students if student["name"] == name and klass in (None, student["class"])]
    if len(found) == 1:
        return found[0]
    if not found:
        raise ToolError("未找到学生“{}”。可用 list_students 按姓名或班级查找。".format(text))
    raise ToolError("找到多名学生，请改用账号指定：" + "；".join(
        "{}（{}）账号 {}".format(item["name"], item["class"], item["username"]) for item in found[:20]))


def save_file(data: bytes, filename: str, output_dir=None) -> str:
    directory = Path(str(output_dir)).expanduser() if output_dir else export_dir()
    if not directory.is_absolute():
        raise ToolError("output_dir 需要完整路径，例如 /Users/名字/Desktop")
    directory.mkdir(parents=True, exist_ok=True)
    safe = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "_", filename).strip(" .") or "export"
    target = directory / safe
    number = 2
    while target.exists():
        target = directory / "{} ({}){}".format(Path(safe).stem, number, Path(safe).suffix)
        number += 1
    descriptor = os.open(str(target), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(data)
    return str(target)


def read_input(path_text, limit: int) -> tuple[Path, bytes]:
    path = Path(str(path_text or "")).expanduser()
    if not str(path_text or "").strip() or not path.is_absolute():
        raise ToolError("请给出文件的完整路径，例如 /Users/名字/Desktop/名单.xlsx")
    if not path.is_file():
        raise ToolError("请确认文件路径：{}".format(path))
    if path.stat().st_size > limit:
        raise ToolError("请选择 {} MB 以内的文件：{}".format(limit // 1048576, path))
    return path, path.read_bytes()


def save_accounts(credentials: list, output_dir=None) -> list:
    rows = [{"name": item["name"], "class": item.get("class") or "", "username": item["username"],
             "password": item["password"]} for item in credentials]
    paths = []
    for start in range(0, len(rows), ACCOUNT_FILE_ROWS):
        reply = BACKEND.api("POST", "/api/admin/accounts_workbook", {"accounts": rows[start:start + ACCOUNT_FILE_ROWS]},
                            binary=True, timeout=180)
        paths.append(save_file(reply.data, reply.filename() or "学生账号.xlsx", output_dir))
    return paths


def flow_steps(summary: dict, info: dict) -> list:
    students, registered = summary.get("students", 0), summary.get("registered", 0)
    capacity = summary.get("capacity", 0)
    locked = bool(info.get("registration_locked"))
    changed = summary.get("last_change")

    def fresh(at) -> bool:
        return bool(at) and (not changed or at >= changed)

    def status(done: bool, warn: bool = False) -> str:
        return "需注意" if done and warn else ("完成" if done else "待办")

    exported = (school_time(summary.get("last_export")) if fresh(summary.get("last_export")) else None)
    backed = (school_time(summary.get("last_backup")) if fresh(summary.get("last_backup")) else None)
    return [
        {"步骤": "导入学生", "状态": status(students > 0), "说明": "{} 名学生".format(students)},
        {"步骤": "添加社团", "状态": status(summary.get("clubs", 0) > 0, students > capacity),
         "说明": "{} 个社团，开放报名的名额合计 {}{}".format(summary.get("clubs", 0), capacity,
                                                  "，学生 {} 名".format(students) if students > capacity else "")},
        {"步骤": "开放时间", "状态": status(bool(info.get("start_time"))),
         "说明": "开放 {}；截止 {}".format(info.get("start_time") or "待定", info.get("end_time") or "手动锁定")},
        {"步骤": "报名", "状态": status(locked), "说明": "{}，已报名 {} / {}".format(phase_text(info), registered, students)},
        {"步骤": "调剂", "状态": status(locked and registered >= students),
         "说明": "未报名 {} 名".format(students - registered) if locked else "名单锁定后安排未报名的学生"},
        {"步骤": "导出与备份", "状态": status(locked and bool(exported) and bool(backed)),
         "说明": "报名表{}；完整备份{}".format("已于 " + exported + " 导出" if exported else "待导出",
                                       "已于 " + backed + " 下载" if backed else "待下载")},
    ]


# ---------------------------------------------------------------------------
# 工具：服务
# ---------------------------------------------------------------------------
def start_in_progress() -> int | None:
    return pid_alive("agent-start")


def registration_running() -> dict | None:
    if not port_open(backend_port()):
        return None
    with contextlib.suppress(ToolError):
        info = get("/api/check_registration_time")
        return info if isinstance(info, dict) else None
    return None


def opening_soon(info: dict | None) -> bool:
    if not info or info.get("registration_locked"):
        return False
    if info.get("can_register"):
        return True
    open_at, now = info.get("open_at"), info.get("server_now")
    return isinstance(open_at, (int, float)) and isinstance(now, (int, float)) and 0 <= open_at - now <= 3600 * 1000


def tool_service_status(args: dict) -> dict:
    scheme, web_port = web_scheme_port()
    components = {
        "管理服务（Python :{}）".format(backend_port()): port_open(backend_port()),
        "热服务（Rust :2002）": port_open(2002),
        "网页入口（nginx :{}）".format(web_port): port_open(web_port),
        "Redis（:{}）".format(redis_port()): port_open(redis_port()),
    }
    result = {"服务": {name: "运行中" if up else "已停止" for name, up in components.items()}}
    if not port_open(2002) and port_open(backend_port()):
        result["运行模式"] = "只运行 Python 服务"
    if start_in_progress():
        result["启动进行中"] = "agent-start 进程 {}；用 service_logs 查看 start 日志".format(start_in_progress())
    result["访问地址"] = addresses()
    info = registration_running()
    if info:
        result["报名状态"] = phase_text(info)
        result["开放时间"] = info.get("start_time") or "待定"
        result["截止时间"] = info.get("end_time") or "手动锁定"
        result["服务器当前时间（学校时间）"] = school_time(info.get("server_now"))
    tools = {name: bool(shutil.which(name, path=child_env()["PATH"])) for name in ("redis-server", "nginx", "cargo")}
    probe = ("import importlib.util as u; print(' '.join(n for m, n in (('redis', 'redis'), ('argon2', 'argon2-cffi'), "
             "('pypinyin', 'pypinyin'), ('PIL', 'Pillow')) if u.find_spec(m) is None))")
    missing = "需要安装 Python"
    if shutil.which(python_command()):
        check = subprocess.run([python_command(), "-c", probe], env=child_env(), capture_output=True, text=True, timeout=30)
        missing = check.stdout.strip() if check.returncode == 0 else "需要安装 Python"
    result["环境"] = {
        "Python": python_command(),
        "Python 依赖": "齐全" if not missing else "需要安装：" + missing,
        "redis-server": "已安装" if tools["redis-server"] else "需要安装",
        "nginx": "已安装" if tools["nginx"] else "需要安装",
        "Rust（cargo，可选）": "已安装" if tools["cargo"] else "可选，装好后同时运行 Rust 热服务",
    }
    result["文件"] = {"程序目录": str(APP_DIR), "数据库": str(db_path()), "管理员密码文件": str(secret_file()),
                    "运行参数": str(settings_path()), "导出目录": str(export_dir()), "运行日志": str(run_dir())}
    values, _lines = read_settings()
    custom = {key: values[key] for key in SETTING_KEYS if key in values}
    if custom:
        result["自定义运行参数"] = custom
    hints = []
    if not all(components.values()):
        hints.append("调用 start_service 启动全部服务")
    if missing:
        hints.append("安装 Python 依赖：{} -m pip install redis argon2-cffi pypinyin Pillow".format(python_command()))
    if not tools["redis-server"] or not tools["nginx"]:
        hints.append("安装 Redis 与 nginx：brew install redis nginx（Mac）")
    if hints:
        result["下一步"] = hints
    return result


def tool_start_service(args: dict) -> dict:
    if start_in_progress():
        return {"状态": "启动进行中", "日志末尾": tool_service_logs({"source": "start", "lines": 15})["内容"]}
    info = registration_running()
    if info and opening_soon(info) and not args.get("confirm"):
        raise NeedsConfirm("服务正在运行且报名进行中或一小时内开放；重启会让正在操作的学生等待几秒",
                           {"报名状态": phase_text(info), "开放时间": info.get("start_time")})
    wait_seconds = max(5, min(900, int(args.get("wait_seconds") or 120)))
    directory = run_dir()
    directory.mkdir(parents=True, exist_ok=True)
    log_path = directory / LOG_FILES["start"]
    extra = {"OMMS_OPEN_BROWSER": "1" if args.get("open_browser") else "0"}
    if args.get("python_only"):
        extra["OMMS_PYTHON_ONLY"] = "1"
    with open(log_path, "wb") as log:
        process = subprocess.Popen(["bash", str(RUN_SH)], cwd=str(APP_DIR), env=child_env(**extra),
                                   stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                                   start_new_session=True)
    (directory / "agent-start.pid").write_text(str(process.pid))
    deadline = time.monotonic() + wait_seconds
    while process.poll() is None and time.monotonic() < deadline:
        time.sleep(0.5)
    output = log_path.read_text(encoding="utf-8", errors="replace")
    if process.poll() is None:
        return {"状态": "启动进行中（首次编译 Rust 在旧电脑上需要几分钟）", "日志末尾": tail(output, 15),
                "下一步": "稍后调用 service_status 或 service_logs(source=start) 查看"}
    with contextlib.suppress(OSError):
        (directory / "agent-start.pid").unlink()
    BACKEND.cookie = ""
    labels = ("学生访问", "管理控制台", "管理员账号", "管理员密码")
    summary = {}
    for line in output.splitlines():
        for label in labels:
            if line.startswith(label):
                summary[label] = line[len(label):].strip()
    result = {"状态": "已启动" if process.returncode == 0 else "需要处理", "退出码": process.returncode}
    result.update(summary)
    result["输出"] = tail(output, 25)
    if process.returncode != 0:
        result["下一步"] = "按输出中的提示处理；常见原因见指南“故障排查”"
    return result


def tool_stop_service(args: dict) -> dict:
    info = registration_running()
    if info and opening_soon(info) and not args.get("confirm"):
        raise NeedsConfirm("停止服务；报名进行中或一小时内开放，学生访问随之暂停",
                           {"报名状态": phase_text(info), "开放时间": info.get("start_time")})
    result = subprocess.run(["bash", str(RUN_SH), "stop"], cwd=str(APP_DIR), env=child_env(),
                            capture_output=True, text=True, timeout=180)
    BACKEND.cookie = ""
    return {"状态": "已停止" if result.returncode == 0 else "需要处理", "输出": tail(result.stdout + result.stderr, 10)}


def tool_service_logs(args: dict) -> dict:
    source = args.get("source") or "python"
    if source not in LOG_FILES:
        raise ToolError("source 可选：{}".format("、".join(LOG_FILES)))
    lines = max(1, min(400, int(args.get("lines") or 60)))
    path = run_dir() / LOG_FILES[source]
    try:
        with open(path, "rb") as handle:
            handle.seek(0, os.SEEK_END)
            handle.seek(max(0, handle.tell() - 256 * 1024))
            text = handle.read().decode("utf-8", "replace")
    except OSError:
        return {"文件": str(path), "内容": "（日志尚未生成）"}
    return {"文件": str(path), "内容": tail(text, lines)}


def tool_open_console(args: dict) -> dict:
    values = run_admin_login()
    link = values.get("LOGIN_URL") or ""
    if link and args.get("open_browser", True):
        open_target(link)
    return {"管理控制台": addresses()["管理控制台"], "本机登录链接（2 分钟内有效，只能使用一次）": link or "（启动 Redis 后生成）",
            "管理员账号": values.get("ADMIN_USER") or "admin", "管理员密码": values["ADMIN_PASSWORD"],
            "已在浏览器打开": bool(link and args.get("open_browser", True))}


# ---------------------------------------------------------------------------
# 工具：运行参数与管理员账号
# ---------------------------------------------------------------------------
def validate_setting(key: str, value) -> str:
    rule = next(item[2] for item in SETTINGS if item[0] == key)
    text = str(value).strip()
    if "\n" in text or "\r" in text:
        raise ToolError("{} 的值只能占一行".format(key))
    kind = rule[0]
    if text == "" and key in ("TLS_CERT", "TLS_KEY", "REAL_IP_FROM", "AUTH_CONCURRENCY"):
        return ""
    if kind == "port":
        if not text.isdigit() or not 1 <= int(text) <= 65535:
            raise ToolError("{} 应为 1–65535 的端口号".format(key))
        if int(text) in (2001, 2002, redis_port()):
            raise ToolError("{} 与内部服务端口冲突（2001、2002、{}）".format(key, redis_port()))
        return str(int(text))
    if kind == "file":
        path = Path(text).expanduser()
        if not path.is_absolute() or not path.is_file() or not os.access(str(path), os.R_OK):
            raise ToolError("{} 需要指向可读文件的完整路径：{}".format(key, text))
        return str(path)
    if kind == "networks":
        items = [item.strip() for item in text.split(",") if item.strip()]
        try:
            for item in items:
                ipaddress.ip_network(item, strict=False)
        except ValueError as exc:
            raise ToolError("{} 应为逗号分隔的 IP 或网段：{}".format(key, exc)) from exc
        return ",".join(items)
    if kind == "header":
        if not re.fullmatch(r"[A-Za-z0-9-]{1,64}", text):
            raise ToolError("{} 应为请求头名称，例如 X-Forwarded-For".format(key))
        return text
    if kind == "offset":
        match = re.fullmatch(r"([+-])(\d{2}):(\d{2})", text)
        if not match or int(match.group(2)) > 14 or int(match.group(3)) >= 60:
            raise ToolError("{} 的格式为 +08:00".format(key))
        return text
    if kind == "bool":
        mapping = {"1": "1", "0": "0", "true": "1", "false": "0", "yes": "1", "no": "0", "开": "1", "关": "0"}
        if text.lower() not in mapping:
            raise ToolError("{} 应为 1 或 0".format(key))
        return mapping[text.lower()]
    if kind == "command":
        if not (shutil.which(text, path=child_env()["PATH"]) or (Path(text).is_file() and os.access(text, os.X_OK))):
            raise ToolError("{} 应为可执行的命令或完整路径：{}".format(key, text))
        return text
    if kind == "int":
        if not re.fullmatch(r"\d+", text) or not rule[1] <= int(text) <= rule[2]:
            raise ToolError("{} 应为 {}–{} 的整数".format(key, rule[1], rule[2]))
        return str(int(text))
    if kind == "choice":
        if text.upper() not in rule[1:]:
            raise ToolError("{} 可选：{}".format(key, "、".join(rule[1:])))
        return text.upper()
    raise ToolError("未知参数类型：{}".format(key))


def tool_get_settings(args: dict) -> dict:
    values, _lines = read_settings()
    items = []
    for key, default, _rule, description in SETTINGS:
        items.append({"键": key, "当前值": effective(key), "默认值": default,
                      "来源": "启动环境" if key in os.environ else ("参数文件" if key in values else "默认"),
                      "说明": description})
    return {"参数文件": str(settings_path()), "参数": items,
            "生效方式": "update_settings 写入参数文件后重启服务（start_service）生效"}


def tool_update_settings(args: dict) -> dict:
    updates = args.get("values")
    if not isinstance(updates, dict) or not updates:
        raise ToolError("values 应为 {键: 值} 对象；值为 null 或空字符串时恢复默认")
    unknown = [key for key in updates if key not in SETTING_KEYS]
    if unknown:
        raise ToolError("可调整的键：{}（收到：{}）".format("、".join(SETTING_KEYS), "、".join(unknown)))
    cleaned = {}
    for key, value in updates.items():
        cleaned[key] = None if value is None or str(value).strip() == "" else validate_setting(key, value)
    values, lines = read_settings()
    merged = dict(values)
    for key, value in cleaned.items():
        if value is None:
            merged.pop(key, None)
        else:
            merged[key] = value
    if bool(merged.get("TLS_CERT")) != bool(merged.get("TLS_KEY")):
        raise ToolError("TLS_CERT 与 TLS_KEY 需要同时设置或同时清除")
    ports = [merged.get("HTTP_PORT") or "8080"]
    if merged.get("TLS_CERT"):
        ports.append(merged.get("TLS_PORT") or "8443")
    if len(set(ports)) != len(ports):
        raise ToolError("HTTP_PORT 与 TLS_PORT 需要不同")
    output, seen = [], set()
    for line in lines:
        key = line.split("=", 1)[0].strip() if "=" in line and not line.lstrip().startswith("#") else None
        if key in cleaned:
            seen.add(key)
            if cleaned[key] is not None:
                output.append("{}={}".format(key, cleaned[key]))
            continue
        output.append(line)
    for key, value in cleaned.items():
        if key not in seen and value is not None:
            output.append("{}={}".format(key, value))
    path = settings_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    partial = path.with_name(path.name + ".part")
    descriptor = os.open(str(partial), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write("\n".join(output) + "\n")
    os.replace(str(partial), str(path))
    result = {"参数文件": str(path), "已写入": {key: value if value is not None else "（恢复默认）" for key, value in cleaned.items()}}
    if args.get("restart"):
        result["重启"] = tool_start_service({"confirm": args.get("confirm"), "wait_seconds": args.get("wait_seconds")})
    else:
        result["生效方式"] = "重启服务后生效：调用 start_service（报名进行中时需管理员确认）"
    return result


def tool_admin_credentials(args: dict) -> dict:
    try:
        values = run_admin_login()
        username, password = values.get("ADMIN_USER") or "admin", values["ADMIN_PASSWORD"]
    except ToolError as exc:
        username, password = file_credentials()
        if not password:
            raise ToolError("请检查数据库与密码文件：{}".format(exc)) from exc
    BACKEND.username, BACKEND.password = username, password
    return {"管理员账号": username, "管理员密码": password, "密码文件": str(secret_file()), **addresses()}


def tool_change_admin_password(args: dict) -> dict:
    new = args.get("new_password")
    if new is None or str(new) == "":
        new = "".join(secrets.choice(PASSWORD_ALPHABET) for _ in range(12))
    new = str(new)
    if not 8 <= len(new) <= 64 or re.search(r"\s", new):
        raise ToolError("新密码为 8–64 位，由字母、数字和符号连续组成")
    if not BACKEND.password:
        BACKEND.username, BACKEND.password = file_credentials()
    if not BACKEND.cookie:
        BACKEND.login()
    post("/api/admin_change_password", {"current": BACKEND.password, "new": new}, retry=False)
    BACKEND.password = new
    BACKEND.cookie = ""
    stored = file_credentials()[1]
    return {"管理员账号": BACKEND.username or "admin", "新密码": new, "密码文件已更新": stored == new,
            "说明": "控制台需用新密码重新登录；下次启动时终端打印的也是新密码"}


# ---------------------------------------------------------------------------
# 工具：概览、时间与锁定
# ---------------------------------------------------------------------------
def tool_get_overview(args: dict) -> dict:
    clubs = all_clubs()
    summary = get("/api/admin/summary")
    info = get("/api/check_registration_time")
    students, registered = summary.get("students", 0), summary.get("registered", 0)
    return {
        "报名状态": phase_text(info),
        "开放时间": info.get("start_time") or "待定",
        "截止时间": info.get("end_time") or "手动锁定",
        "服务器当前时间（学校时间）": school_time(info.get("server_now")),
        "学生": students, "已报名": registered, "未报名": students - registered,
        "社团": len(clubs), "开放报名的名额合计": summary.get("capacity", 0),
        "各社团": [{"社团": club["name"], "已报名": club["current_students"], "人数上限": club["max_students"],
                  "开放报名": club["enabled"]} for club in clubs],
        "流程": flow_steps(summary, info),
        "访问地址": addresses(),
    }


def tool_set_schedule(args: dict) -> dict:
    payload = {}
    if args.get("open_at") not in (None, ""):
        payload["start_time"] = parse_time(args["open_at"], "open_at")
    if "close_at" in args:
        payload["end_time"] = "" if args["close_at"] in (None, "") else parse_time(args["close_at"], "close_at")
    if not payload:
        raise ToolError("请给出 open_at，或用 close_at 设置截止时间（空字符串表示取消截止时间）")
    before = get("/api/check_registration_time")
    now_text = school_time(before.get("server_now")) or ""
    if before.get("can_register") and payload.get("start_time", "") > now_text and not args.get("confirm"):
        raise NeedsConfirm("把开放时间改到 {}；报名正在进行，保存后报名暂停到新的开放时间".format(payload["start_time"]))
    post("/api/update_registration_time", payload)
    after = get("/api/check_registration_time")
    notes = []
    if payload.get("start_time") and payload["start_time"] <= now_text:
        notes.append("开放时间已到，报名即刻开放")
    if after.get("registration_locked"):
        notes.append("名单目前处于锁定状态，开放前用 set_lock 解除锁定")
    result = {"报名状态": phase_text(after), "开放时间": after.get("start_time") or "待定",
              "截止时间": after.get("end_time") or "手动锁定", "服务器当前时间（学校时间）": school_time(after.get("server_now"))}
    if notes:
        result["说明"] = notes
    return result


def tool_clear_schedule(args: dict) -> dict:
    info = get("/api/check_registration_time")
    if info.get("can_register") and not args.get("confirm"):
        raise NeedsConfirm("清除开放时间；报名正在进行，清除后报名立即暂停，学生页显示“开放时间待定”")
    post("/api/update_registration_time", {"start_time": ""})
    after = get("/api/check_registration_time")
    return {"报名状态": phase_text(after), "开放时间": after.get("start_time") or "待定", "截止时间": after.get("end_time") or "手动锁定"}


def tool_set_lock(args: dict) -> dict:
    if not isinstance(args.get("locked"), bool):
        raise ToolError("locked 应为 true（锁定）或 false（解除锁定）")
    locked = args["locked"]
    info = get("/api/check_registration_time")
    if locked and info.get("can_register") and not args.get("confirm"):
        raise NeedsConfirm("锁定名单：报名与退选立即停止，系统同时保存一份快照")
    reason = str(args.get("reason") or ("手动锁定" if locked else "解除锁定"))[:200]
    result = post("/api/update_registration_lock", {"locked": locked, "reason": reason, "request_id": rid()})
    after = get("/api/check_registration_time")
    out = {"名单": "已锁定" if after.get("registration_locked") else "未锁定", "报名状态": phase_text(after)}
    if isinstance(result, dict) and result.get("backup"):
        out["快照"] = result["backup"]
    return out


# ---------------------------------------------------------------------------
# 工具：学生
# ---------------------------------------------------------------------------
def parse_roster(path_text) -> tuple[Path, dict, list]:
    path, data = read_input(path_text, MAX_TABLE_BYTES)
    parsed = BACKEND.api("POST", "/api/admin/import/parse", raw=data, filename=path.name, timeout=180)
    rows = parsed.get("rows") or []
    for row in rows:
        row["position"] = "{}:{}".format(row.get("sheet") or "表", row.get("line"))
    return path, parsed, rows


def row_view(row: dict) -> dict:
    return {"位置": row["position"], "姓名": row["name"], "班级": row["class"], "标注": row["notes"]}


def class_counts(rows: list, key: str = "class") -> dict:
    counts = {}
    for row in rows:
        label = row.get(key) or "（待补班级）"
        counts[label] = counts.get(label, 0) + 1
    return {label: counts[label] for label in sorted(counts, key=natural_key)}


def tool_preview_roster(args: dict) -> dict:
    path, parsed, rows = parse_roster(args.get("file_path"))
    flagged = [row_view(row) for row in rows if row["notes"]]
    return {
        "文件": str(path),
        "识别到的学生行": len(rows),
        "略过的行（合计、说明、空姓名等）": parsed.get("skipped", 0),
        "工作表": [{"名称": sheet.get("name"), "表头所在行": sheet.get("header"), "学生行": sheet.get("rows")}
                 for sheet in parsed.get("sheets") or []],
        "各班人数": class_counts(rows),
        "系统中已有（同名同班）": sum(1 for row in rows if "已导入" in row["notes"]),
        "需核对的行数": len(flagged),
        "需核对的行": flagged[:300],
        "下一步": "向管理员说明需核对的行；确认后调用 import_roster。fixes 改正姓名或班级，skip 跳过某些行，都按“位置”指定。",
    }


def tool_import_roster(args: dict) -> dict:
    path, _parsed, rows = parse_roster(args.get("file_path"))
    by_position = {row["position"]: row for row in rows}
    for fix in args.get("fixes") or []:
        position = str((fix or {}).get("position") or "")
        if position not in by_position:
            raise ToolError("fixes 的位置请从 preview_roster 的结果中选择（收到：{}）".format(position))
        if fix.get("name") is not None:
            by_position[position]["name"] = str(fix["name"]).strip()
        if fix.get("class") is not None:
            by_position[position]["class"] = str(fix["class"]).strip()
        by_position[position]["fixed"] = True
    skip = {str(item) for item in args.get("skip") or []}
    unknown = sorted(skip - set(by_position))
    if unknown:
        raise ToolError("skip 的位置请从 preview_roster 的结果中选择（收到：{}）".format("、".join(unknown)))
    include_existing = bool(args.get("include_imported"))
    # 按改正后的姓名和班级对照现有学生，补全班级后再次导入同一份表格时同样跳过。
    present = {(student["name"], student["class"]) for student in all_students()}
    chosen, problems, existing = [], [], 0
    for row in rows:
        if row["position"] in skip:
            continue
        try:
            row["name"] = clean_text(row["name"], "姓名")
            row["class"] = clean_text(row["class"], "班级")
        except ToolError as exc:
            problems.append({**row_view(row), "问题": str(exc)})
            continue
        if (row["name"], row["class"]) in present and not include_existing:
            existing += 1
            continue
        chosen.append(row)
    if problems:
        raise ToolError("以下行需要用 fixes 改正或用 skip 跳过：\n" + dumps(problems[:80]))
    if not chosen:
        raise ToolError("表格中的学生都已在系统中（同名同班 {} 行已跳过）".format(existing))
    flagged = [row_view(row) for row in chosen if row["notes"] and not row.get("fixed")]
    if not args.get("confirm"):
        raise NeedsConfirm("导入 {} 名学生".format(len(chosen)), {
            "文件": str(path), "各班人数": class_counts(chosen), "系统中已有而跳过": existing,
            "带标注、仍会导入的行": flagged[:100]})
    batch = max(1, min(IMPORT_BATCH, int(effective("MAX_IMPORT_STUDENTS") or 500)))
    credentials, created, refused, stopped = [], 0, 0, ""
    for start in range(0, len(chosen), batch):
        part = chosen[start:start + batch]
        try:
            result = post("/api/import_students",
                          {"students": [{"name": row["name"], "class": row["class"]} for row in part]},
                          retry=False, timeout=600)
        except ToolError as exc:
            stopped = "第 {} 名起暂停：{}".format(start + 1, exc)
            break
        created += int(result.get("success") or 0)
        refused += int(result.get("failed") or 0)
        credentials.extend(result.get("credentials") or [])
    files = save_accounts(credentials, args.get("output_dir")) if credentials else []
    if files and args.get("reveal"):
        open_target(files[0], reveal=True)
    out = {"已导入": created, "跳过（账号重复等）": refused, "系统中已有而跳过": existing, "账号表": files,
           "各班人数": class_counts(chosen)}
    if stopped:
        out["暂停"] = stopped + "。已导入的学生在账号表里；再次调用 import_roster 时，已导入的行自动跳过。"
    return out


def tool_list_students(args: dict) -> dict:
    students = all_students()
    clubs = {club["id"]: club for club in all_clubs()}
    query = str(args.get("query") or "").strip().lower()
    klass = str(args.get("class") or "").strip()
    club_id = find_club(args["club"], list(clubs.values()))["id"] if args.get("club") else None
    status = args.get("status") or "all"
    rows = []
    for student in students:
        if klass and student["class"] != klass:
            continue
        if club_id is not None and student.get("club_id") != club_id:
            continue
        if status == "registered" and not student.get("club_id"):
            continue
        if status == "unregistered" and student.get("club_id"):
            continue
        row = student_row(student, clubs)
        if query and not any(query in str(value).lower() for value in row.values()):
            continue
        rows.append(row)
    limit = max(1, min(500, int(args.get("limit") or 50)))
    offset = max(0, int(args.get("offset") or 0))
    result = {"学生总数": len(students), "已报名": sum(1 for student in students if student.get("club_id")),
              "符合条件": len(rows), "列表": rows[offset:offset + limit]}
    if offset + limit < len(rows):
        result["下一页 offset"] = offset + limit
    if not (query or klass or club_id or status != "all"):
        result["各班人数"] = class_counts(students)
    return result


def tool_update_student(args: dict) -> dict:
    students = all_students()
    clubs = all_clubs()
    student = find_student(args.get("student"), students)
    done = []
    if args.get("name") is not None or args.get("class") is not None:
        name = clean_text(args["name"], "姓名") if args.get("name") is not None else student["name"]
        klass = clean_text(args["class"], "班级") if args.get("class") is not None else student["class"]
        if (name, klass) != (student["name"], student["class"]):
            post("/api/admin/update_student", {"student_id": student["id"], "name": name, "class": klass, "request_id": rid()})
            done.append("姓名、班级已更新为 {}（{}）".format(name, klass))
    if "club" in args:
        reference = args.get("club")
        target = None if reference in (None, "", "未报名", "无") else find_club(reference, clubs)
        current = student.get("club_id")
        target_id = target["id"] if target else None
        if target_id != current:
            path = ("/api/admin/assign_registration" if not current
                    else "/api/admin/remove_registration" if not target else "/api/admin/transfer_registration")
            payload = {"student_id": student["id"], "request_id": rid(),
                       "reason": str(args.get("reason") or "管理员调整")[:200]}
            if target:
                payload["club_id"] = target["id"]
                if args.get("ignore_limits"):
                    payload["override_eligibility"] = True
            post(path, payload)
            done.append("社团已改为 {}".format(target["name"]) if target else "已移出社团")
    if not done:
        done.append("内容与现有信息相同")
    refreshed = find_student(student["username"])
    return {"学生": student_row(refreshed, {club["id"]: club for club in all_clubs()}), "结果": done}


def tool_reset_passwords(args: dict) -> dict:
    reference, klass = args.get("student"), str(args.get("class") or "").strip()
    if bool(reference) == bool(klass):
        raise ToolError("student（单个学生）与 class（整个班级）二选一")
    reason = str(args.get("reason") or "管理员重置")[:200]
    if reference:
        student = find_student(reference)
        if not args.get("confirm"):
            raise NeedsConfirm("重置 {}（{}）的密码，新密码随即生效".format(student["name"], student["class"]))
        result = post("/api/admin/reset_student_password",
                      {"student_id": student["id"], "request_id": rid(), "reason": reason})
        password = result.get("temporary_password")
        out = {"姓名": student["name"], "班级": student["class"], "账号": result.get("username") or student["username"],
               "新密码": password or result.get("message")}
        if password and args.get("save_excel"):
            out["账号表"] = save_accounts([{"name": student["name"], "class": student["class"],
                                         "username": out["账号"], "password": password}], args.get("output_dir"))
        return out
    members = [student for student in all_students() if student["class"] == klass]
    if not members:
        raise ToolError("班级可选：{}（收到“{}”）".format("、".join(class_counts(all_students())), klass))
    if not args.get("confirm"):
        raise NeedsConfirm("重置 {} 全部 {} 名学生的密码，新密码随即生效".format(klass, len(members)))
    result = post("/api/admin/reset_class_passwords", {"class": klass, "reason": reason, "request_id": rid()})
    credentials = result.get("credentials") or []
    files = save_accounts(credentials, args.get("output_dir")) if credentials else []
    if files and args.get("reveal"):
        open_target(files[0], reveal=True)
    out = {"班级": klass, "已重置": result.get("count"), "账号表": files}
    if 0 < len(credentials) <= 60:
        out["新密码"] = [{"姓名": item["name"], "账号": item["username"], "密码": item["password"]} for item in credentials]
    return out


def tool_delete_student(args: dict) -> dict:
    student = find_student(args.get("student"))
    if not args.get("confirm"):
        raise NeedsConfirm("删除学生 {}（{}，账号 {}）及其报名记录".format(student["name"], student["class"], student["username"]))
    post("/api/delete_student", {"student_id": student["id"]})
    return {"已删除": "{}（{}）".format(student["name"], student["class"])}


# ---------------------------------------------------------------------------
# 工具：社团
# ---------------------------------------------------------------------------
def tool_list_clubs(args: dict) -> dict:
    clubs = all_clubs()
    return {"社团": [club_row(club) for club in clubs],
            "名额合计": sum(club["max_students"] for club in clubs if club["enabled"])}


def club_payload(club: dict, args: dict) -> dict:
    payload = {
        "club_id": club["id"], "request_id": rid(), "expected_revision": club["revision"],
        "reason": str(args.get("reason") or "管理员编辑社团")[:200],
        "name": club["name"], "max_students": club["max_students"], "description": club.get("description") or "",
        "advisor_name": club.get("advisor_name") or "", "meeting_time": club.get("meeting_time") or "",
        "location": club.get("location") or "", "allowed_grades": club.get("allowed_grades") or [],
        "allowed_classes": club.get("allowed_classes") or [], "enabled": bool(club.get("enabled")),
    }
    if args.get("name") is not None:
        payload["name"] = clean_text(args["name"], "名称", 80)
    if args.get("capacity") is not None:
        capacity = args["capacity"]
        if not isinstance(capacity, int) or isinstance(capacity, bool) or not 1 <= capacity <= 10000:
            raise ToolError("capacity 应为 1–10000 的整数")
        if capacity < club["current_students"]:
            raise ToolError("人数上限至少为已报名人数 {}".format(club["current_students"]))
        payload["max_students"] = capacity
    for field, key, limit in (("description", "description", 1000), ("advisor", "advisor_name", 80),
                              ("meeting_time", "meeting_time", 120), ("location", "location", 120)):
        if args.get(field) is not None:
            value = str(args[field]).strip()
            if len(value) > limit:
                raise ToolError("{} 的长度应在 {} 字以内".format(field, limit))
            payload[key] = value
    if args.get("grades") is not None:
        payload["allowed_grades"] = text_list(args["grades"])
    if args.get("classes") is not None:
        payload["allowed_classes"] = text_list(args["classes"])
    if args.get("enabled") is not None:
        payload["enabled"] = bool(args["enabled"])
    return payload


def apply_club_image(club: dict, image) -> str:
    if image in (None, ""):
        return ""
    if str(image).strip() in ("default", "默认", "恢复默认"):
        if not (isinstance(club.get("image_path"), str) and club["image_path"].startswith("/club-images/")):
            return "图片已是默认插图"
        post("/api/remove_club_image", {"club_id": club["id"], "expected_revision": club["revision"],
                                        "request_id": rid(), "reason": "恢复默认图片"})
        return "已恢复默认插图"
    path, data = read_input(image, MAX_IMAGE_BYTES)
    content_type = IMAGE_TYPES.get(path.suffix.lower())
    if not content_type:
        raise ToolError("图片格式可用 JPG、PNG、WebP、GIF：{}".format(path.name))
    query = urllib.parse.urlencode({"club_id": club["id"], "expected_revision": club["revision"],
                                    "request_id": rid(), "reason": "更换社团图片"})
    BACKEND.api("POST", "/api/upload_club_image?" + query, raw=data, filename=path.name, content_type=content_type,
                timeout=180)
    return "已更换为 {}".format(path.name)


def tool_add_club(args: dict) -> dict:
    name = clean_text(args.get("name"), "名称", 80)
    capacity = args.get("capacity")
    if not isinstance(capacity, int) or isinstance(capacity, bool) or not 1 <= capacity <= 10000:
        raise ToolError("capacity 应为 1–10000 的整数")
    result = post("/api/import_clubs", {"clubs": [{"name": name, "max_students": capacity}]})
    if not int(result.get("success") or 0):
        raise ToolError("社团“{}”已存在".format(name))
    club = find_club(name)
    extra = {key: args[key] for key in ("description", "advisor", "meeting_time", "location", "grades", "classes", "enabled")
             if args.get(key) is not None}
    if extra:
        updated = post("/api/update_club", club_payload(club, extra))
        club = updated.get("club") or find_club(club["id"])
    note = apply_club_image(club, args.get("image"))
    out = {"社团": club_row(find_club(club["id"]))}
    if note:
        out["图片"] = note
    return out


def tool_update_club(args: dict) -> dict:
    club = find_club(args.get("club"))
    fields = ("name", "capacity", "description", "advisor", "meeting_time", "location", "grades", "classes", "enabled")
    if any(args.get(key) is not None for key in fields):
        updated = post("/api/update_club", club_payload(club, args))
        club = updated.get("club") or find_club(club["id"])
    note = apply_club_image(club, args.get("image"))
    out = {"社团": club_row(find_club(club["id"]))}
    if note:
        out["图片"] = note
    return out


def tool_delete_club(args: dict) -> dict:
    club = find_club(args.get("club"))
    if not args.get("confirm"):
        raise NeedsConfirm("删除社团“{}”及其 {} 条报名记录".format(club["name"], club["current_students"]))
    post("/api/delete_club", {"club_id": club["id"]})
    return {"已删除": club["name"]}


# ---------------------------------------------------------------------------
# 工具：调剂、导出、记录
# ---------------------------------------------------------------------------
def tool_adjustment(args: dict) -> dict:
    plan = get("/api/admin/adjustment")
    view = {"名单已锁定": plan.get("locked"), "未报名": plan.get("unregistered"), "空余名额": plan.get("seats"),
            "可安排": plan.get("assignable"),
            "各社团": [{"社团": club.get("name"), "空余": club.get("remaining"), "将安排": club.get("assign")}
                     for club in plan.get("clubs") or []]}
    if plan.get("assignable", 0) < plan.get("unregistered", 0):
        view["还需名额"] = plan["unregistered"] - plan["assignable"]
    if not args.get("run"):
        return view
    if not plan.get("locked"):
        raise ToolError("调剂在名单锁定后进行：先调用 set_lock(locked=true)")
    if not plan.get("assignable"):
        return {**view, "结果": "暂无可安排的学生"}
    if not args.get("confirm"):
        raise NeedsConfirm("把 {} 名未报名的学生随机安排到有空余名额的社团（遵守年级和班级范围）".format(plan["assignable"]), view)
    result = post("/api/admin/adjust_registrations", {"request_id": rid()}, timeout=300)
    return {"已安排": result.get("assigned"), "待安排": result.get("unassigned", 0), "快照": result.get("backup")}


EXPORTS = {
    "workbook": ("/api/export_workbook", "报名表.xlsx"),
    "all": ("/api/export_all_data", "报名总表.csv"),
    "unregistered": ("/api/export_unregistered", "未报名名单.csv"),
    "students": ("/api/export_students_csv", "学生账号.csv"),
}


def tool_export_data(args: dict) -> dict:
    kind = args.get("kind") or "workbook"
    if kind == "club":
        club = find_club(args.get("club"))
        path, default = "/api/export_club_data?club_id={}".format(club["id"]), club["name"] + ".csv"
    elif kind in EXPORTS:
        path, default = EXPORTS[kind]
    else:
        raise ToolError("kind 可选：workbook、all、unregistered、students、club")
    reply = BACKEND.api("GET", path, binary=True, timeout=180)
    saved = save_file(reply.data, reply.filename() or default, args.get("output_dir"))
    if args.get("reveal"):
        open_target(saved, reveal=True)
    return {"文件": saved, "大小": "{} KB".format(max(1, len(reply.data) // 1024))}


def tool_audit_log(args: dict) -> dict:
    role = args.get("role") or "admin"
    limit = max(1, min(200, int(args.get("limit") or 30)))
    query = {"limit": limit}
    if role in ("admin", "student"):
        query["role"] = role
    data = get("/api/admin/audit_events?" + urllib.parse.urlencode(query))
    students = {}
    if role != "admin":
        students = {student["id"]: student for student in all_students()}
    events = []
    for event in data.get("events") or []:
        actor = event.get("actor_id")
        if event.get("actor_role") == "student":
            student = students.get(int(actor)) if str(actor).isdigit() else None
            actor = "{}（{}）".format(student["name"], student["class"]) if student else "学生 {}".format(actor)
        item = {"时间": school_time(event.get("occurred_at")), "操作人": actor,
                "操作": ACTION_LABELS.get(event.get("action"), event.get("action"))}
        if event.get("reason"):
            item["原因"] = event["reason"]
        details = {key: event[key] for key in ("target_type", "target_id", "before", "after", "metadata") if event.get(key)}
        if details:
            item["详情"] = details
        events.append(item)
    return {"记录": events, "条数": len(events)}


# ---------------------------------------------------------------------------
# 工具：备份、恢复、清空
# ---------------------------------------------------------------------------
def tool_backup_now(args: dict) -> dict:
    reply = BACKEND.api("GET", "/api/admin/backup", binary=True, timeout=300)
    saved = save_file(reply.data, reply.filename() or "社团报名备份.zip", args.get("output_dir"))
    if args.get("reveal"):
        open_target(saved, reveal=True)
    return {"文件": saved, "大小": "{} KB".format(max(1, len(reply.data) // 1024)),
            "说明": "完整备份含数据库和社团图片；建议另存到其他电脑或 U 盘"}


def tool_list_snapshots(args: dict) -> dict:
    data = get("/api/admin/backups")
    return {"快照": [{"名称": item["name"], "内容": item["label"], "时间": item["time"],
                    "大小": "{} KB".format(max(1, item["size"] // 1024))} for item in data.get("backups") or []],
            "保留份数": data.get("keep"), "目录": data.get("directory")}


def tool_restore(args: dict) -> dict:
    snapshot, file_path = args.get("snapshot"), args.get("file_path")
    if bool(snapshot) == bool(file_path):
        raise ToolError("snapshot（自动快照名称）与 file_path（备份文件路径）二选一")
    if snapshot:
        names = {item["name"]: item for item in get("/api/admin/backups").get("backups") or []}
        if snapshot not in names:
            raise ToolError("快照名称请从 list_snapshots 中选择（收到：{}）".format(snapshot))
        target = "{} · {}".format(names[snapshot]["label"], names[snapshot]["time"])
    else:
        path, data = read_input(file_path, MAX_RESTORE_BYTES)
        target = str(path)
    if not args.get("confirm"):
        raise NeedsConfirm("用“{}”替换当前全部数据；恢复前自动保存当前数据为快照，学生需重新登录".format(target))
    if snapshot:
        result = post("/api/admin/restore", {"name": snapshot}, timeout=600)
    else:
        result = BACKEND.api("POST", "/api/admin/restore_upload", raw=data, filename=path.name, timeout=600)
    return {"已恢复": target, "学生": result.get("students"), "社团": result.get("clubs"),
            "报名": result.get("registrations"), "恢复前的快照": result.get("backup")}


def tool_clear_data(args: dict) -> dict:
    target = args.get("target")
    summary = get("/api/admin/summary")
    info = get("/api/check_registration_time")
    if target == "registrations":
        if not info.get("registration_locked"):
            raise ToolError("清空报名在名单锁定后进行：先调用 set_lock(locked=true)")
        action, path, payload = "清空全部 {} 条报名记录（学生和社团保留）".format(summary.get("registered", 0)), \
            "/api/admin/clear_registrations", {"request_id": rid()}
    elif target == "students":
        action, path, payload = "删除全部 {} 名学生及其报名记录".format(summary.get("students", 0)), \
            "/api/delete_all_students", {"confirm": True}
    elif target == "clubs":
        action, path, payload = "删除全部 {} 个社团及其报名记录".format(summary.get("clubs", 0)), \
            "/api/delete_all_clubs", {"confirm": True}
    else:
        raise ToolError("target 可选：registrations（报名）、students（学生）、clubs（社团）")
    if not args.get("confirm"):
        raise NeedsConfirm(action + "；操作前自动保存快照")
    result = post(path, payload, timeout=300)
    after = get("/api/admin/summary")
    return {"已执行": action, "学生": after.get("students"), "社团": after.get("clubs"), "报名": after.get("registered"),
            "快照": result.get("backup") if isinstance(result, dict) else None}


# ---------------------------------------------------------------------------
# 工具清单
# ---------------------------------------------------------------------------
def schema(properties: dict | None = None, required: list | None = None) -> dict:
    result = {"type": "object", "properties": properties or {}, "additionalProperties": False}
    if required:
        result["required"] = required
    return result


CONFIRM = {"type": "boolean", "description": "管理员已同意执行。缺省时只返回将要发生的变化，不执行。"}
OUTPUT_DIR = {"type": "string", "description": "保存文件的目录（完整路径），缺省为程序目录下的 exports/"}
REVEAL = {"type": "boolean", "description": "保存后在访达（Finder）中显示文件"}
STUDENT = {"type": "string", "description": "学生账号；或“姓名 班级”“姓名（班级）”，重名时需给出账号"}
CLUB = {"type": "string", "description": "社团名称或编号"}
CLUB_FIELDS = {
    "description": {"type": "string", "description": "简介，1000 字以内"},
    "advisor": {"type": "string", "description": "指导老师"},
    "meeting_time": {"type": "string", "description": "活动时间，例如 每周三下午第三节"},
    "location": {"type": "string", "description": "地点"},
    "grades": {"type": "array", "items": {"type": "string"}, "description": "限报年级，例如 [\"高一\", \"高二\"]；空列表为全部年级"},
    "classes": {"type": "array", "items": {"type": "string"}, "description": "限报班级；空列表为全部班级"},
    "enabled": {"type": "boolean", "description": "是否开放报名"},
    "image": {"type": "string", "description": "图片文件的完整路径（JPG、PNG、WebP、GIF，10 MB 以内）；default 恢复默认插图"},
}

READ = {"readOnlyHint": True, "openWorldHint": False}
WRITE = {"readOnlyHint": False, "destructiveHint": False, "openWorldHint": False}
DANGER = {"readOnlyHint": False, "destructiveHint": True, "openWorldHint": False}

TOOLS = [
    ("service_status", "服务状态", "查看各组件是否运行、访问地址、报名状态、依赖是否齐全和数据文件位置。开始工作时先调用它。",
     schema(), tool_service_status, READ),
    ("start_service", "启动服务", "执行 run.sh 启动或平滑重启全部服务，返回访问地址与管理员账号密码。首次编译 Rust 时可能超过等待时间，届时返回“启动进行中”。",
     schema({"open_browser": {"type": "boolean", "description": "启动后在本机浏览器打开并登录控制台"},
             "python_only": {"type": "boolean", "description": "只运行 Python 服务，跳过 Rust"},
             "wait_seconds": {"type": "integer", "description": "最长等待秒数，默认 120，最大 900"},
             "confirm": CONFIRM}), tool_start_service, WRITE),
    ("stop_service", "停止服务", "执行 run.sh stop 停止全部服务，数据保留。报名进行中或一小时内开放时需要 confirm。",
     schema({"confirm": CONFIRM}), tool_stop_service, DANGER),
    ("service_logs", "查看日志", "读取运行日志的末尾：python、rust、nginx（nginx-error.log）、access（访问日志）、redis、build（Rust 编译）、start（最近一次 start_service）。",
     schema({"source": {"type": "string", "enum": list(LOG_FILES)}, "lines": {"type": "integer", "description": "行数，默认 60，最大 400"}}),
     tool_service_logs, READ),
    ("open_console", "打开控制台", "在本机浏览器打开管理控制台并自动登录（一次性链接，2 分钟内有效），同时返回管理员账号密码。",
     schema({"open_browser": {"type": "boolean", "description": "默认 true；false 时只返回链接"}}), tool_open_console, WRITE),
    ("get_settings", "查看运行参数", "列出可调整的运行参数：端口、HTTPS、代理地址、时区、导入上限、登录限制、线程数等，含当前值、默认值和说明。",
     schema(), tool_get_settings, READ),
    ("update_settings", "调整运行参数", "把参数写入 deploy/omms.env，重启服务后生效。值为 null 或空字符串时恢复默认。",
     schema({"values": {"type": "object", "description": "{键: 值}，键见 get_settings"},
             "restart": {"type": "boolean", "description": "写入后立即重启服务"},
             "wait_seconds": {"type": "integer"}, "confirm": CONFIRM}, ["values"]), tool_update_settings, WRITE),
    ("admin_credentials", "管理员账号密码", "返回管理员账号、当前密码和控制台地址。需要时自动生成新密码。",
     schema(), tool_admin_credentials, WRITE),
    ("change_admin_password", "修改管理员密码", "修改管理员密码并写入密码文件；不给 new_password 时随机生成 12 位。",
     schema({"new_password": {"type": "string", "description": "8–64 位，由字母、数字和符号连续组成"}}), tool_change_admin_password, WRITE),
    ("get_overview", "报名概览", "报名状态、开放与截止时间、学生和报名人数、各社团名额、使用流程各步骤的状态。",
     schema(), tool_get_overview, READ),
    ("set_schedule", "设置开放与截止时间", "按学校时间设置开放时间和截止时间；到开放时间自动开放，到截止时间自动锁定名单。",
     schema({"open_at": {"type": "string", "description": "开放时间，例如 2026-11-20 08:00"},
             "close_at": {"type": "string", "description": "截止时间；空字符串取消截止时间，缺省时保持原样"},
             "confirm": CONFIRM}), tool_set_schedule, WRITE),
    ("clear_schedule", "清除开放时间", "清除开放时间，学生页显示“开放时间待定”。报名进行中时需要 confirm。",
     schema({"confirm": CONFIRM}), tool_clear_schedule, WRITE),
    ("set_lock", "锁定或解除锁定", "锁定后报名与退选停止并保存快照；解除锁定后学生可继续报名。报名进行中锁定需要 confirm。",
     schema({"locked": {"type": "boolean"}, "reason": {"type": "string"}, "confirm": CONFIRM}, ["locked"]),
     tool_set_lock, WRITE),
    ("preview_roster", "预览名单表格", "读取名单表格（xlsx、xls、csv、网页表格、XML 表格；表头含“姓名”“班级”），列出各班人数和需核对的行，不写入数据。",
     schema({"file_path": {"type": "string", "description": "表格的完整路径"}}, ["file_path"]), tool_preview_roster, READ),
    ("import_roster", "导入名单", "按表格创建学生账号，生成账号表 Excel（姓名、班级、账号、密码）。先用 preview_roster 核对；系统中已有的同名同班学生默认跳过。",
     schema({"file_path": {"type": "string", "description": "表格的完整路径"},
             "fixes": {"type": "array", "description": "按位置改正：[{\"position\": \"高一:5\", \"name\": \"…\", \"class\": \"…\"}]",
                       "items": {"type": "object", "properties": {"position": {"type": "string"}, "name": {"type": "string"},
                                                                  "class": {"type": "string"}}, "required": ["position"]}},
             "skip": {"type": "array", "items": {"type": "string"}, "description": "跳过的位置，例如 [\"高一:12\"]"},
             "include_imported": {"type": "boolean", "description": "系统中已有的同名同班学生也导入（重名的另一位学生）"},
             "output_dir": OUTPUT_DIR, "reveal": REVEAL, "confirm": CONFIRM}, ["file_path"]), tool_import_roster, WRITE),
    ("list_students", "学生名单", "按姓名、班级、账号或社团查找学生；不带条件时附各班人数。",
     schema({"query": {"type": "string", "description": "姓名、账号或社团中的文字"}, "class": {"type": "string"},
             "club": CLUB, "status": {"type": "string", "enum": ["all", "registered", "unregistered"]},
             "limit": {"type": "integer", "description": "默认 50，最大 500"}, "offset": {"type": "integer"}}),
     tool_list_students, READ),
    ("update_student", "修改学生", "修改姓名、班级，或代为报名、调整社团、移出社团（club 为空字符串时移出）。",
     schema({"student": STUDENT, "name": {"type": "string"}, "class": {"type": "string"},
             "club": {"type": "string", "description": "社团名称或编号；空字符串表示移出社团，缺省时不改社团"},
             "ignore_limits": {"type": "boolean", "description": "忽略社团的年级、班级范围和暂停状态"},
             "reason": {"type": "string", "description": "记入操作记录"}}, ["student"]), tool_update_student, WRITE),
    ("reset_passwords", "重置学生密码", "重置单个学生（student）或整个班级（class）的密码。班级重置生成账号表 Excel；单个学生直接返回新密码。",
     schema({"student": STUDENT, "class": {"type": "string"}, "reason": {"type": "string"},
             "save_excel": {"type": "boolean", "description": "单个学生时也生成 Excel"},
             "output_dir": OUTPUT_DIR, "reveal": REVEAL, "confirm": CONFIRM}), tool_reset_passwords, DANGER),
    ("delete_student", "删除学生", "删除一名学生及其报名记录。",
     schema({"student": STUDENT, "confirm": CONFIRM}, ["student"]), tool_delete_student, DANGER),
    ("list_clubs", "社团列表", "列出社团的名额、报名人数、范围、简介、时间地点和图片。", schema(), tool_list_clubs, READ),
    ("add_club", "添加社团", "添加社团并可同时填写简介、老师、时间地点、范围和图片。",
     schema(dict({"name": {"type": "string"}, "capacity": {"type": "integer", "description": "人数上限 1–10000"}}, **CLUB_FIELDS),
            ["name", "capacity"]), tool_add_club, WRITE),
    ("update_club", "编辑社团", "修改社团名称、人数上限、简介、老师、时间地点、范围、开放状态；image 更换图片或恢复默认插图。",
     schema(dict({"club": CLUB, "name": {"type": "string"}, "capacity": {"type": "integer"},
                  "reason": {"type": "string"}}, **CLUB_FIELDS), ["club"]), tool_update_club, WRITE),
    ("delete_club", "删除社团", "删除社团及其报名记录。", schema({"club": CLUB, "confirm": CONFIRM}, ["club"]),
     tool_delete_club, DANGER),
    ("adjustment", "调剂", "查看调剂预览：未报名人数、各社团空余和将安排人数。run=true 且名单已锁定时执行调剂。",
     schema({"run": {"type": "boolean"}, "confirm": CONFIRM}), tool_adjustment, DANGER),
    ("export_data", "导出", "导出并保存文件：workbook（Excel 报名表：总表、各社团、未报名）、all（报名总表 CSV）、unregistered（未报名名单）、students（学生账号 CSV：姓名、班级、账号）、club（单个社团名单，需 club）。",
     schema({"kind": {"type": "string", "enum": ["workbook", "all", "unregistered", "students", "club"]}, "club": CLUB,
             "output_dir": OUTPUT_DIR, "reveal": REVEAL}), tool_export_data, WRITE),
    ("audit_log", "操作记录", "查看最近的操作记录：admin（管理操作）、student（学生报名与退选）、all。",
     schema({"role": {"type": "string", "enum": ["admin", "student", "all"]}, "limit": {"type": "integer"}}),
     tool_audit_log, READ),
    ("backup_now", "下载完整备份", "生成完整备份（数据库和社团图片，zip）并保存。", schema({"output_dir": OUTPUT_DIR, "reveal": REVEAL}),
     tool_backup_now, WRITE),
    ("list_snapshots", "自动快照", "列出系统自动保存的数据库快照（锁定、调剂、清空、恢复之前）。", schema(), tool_list_snapshots, READ),
    ("restore", "恢复数据", "从自动快照（snapshot）或备份文件（file_path，zip 或 .db）恢复全部数据。恢复前自动保存当前数据。",
     schema({"snapshot": {"type": "string", "description": "快照名称，见 list_snapshots"},
             "file_path": {"type": "string", "description": "备份文件的完整路径"}, "confirm": CONFIRM}), tool_restore, DANGER),
    ("clear_data", "清空数据", "registrations：清空报名（需先锁定）；students：删除全部学生；clubs：删除全部社团。操作前自动保存快照。",
     schema({"target": {"type": "string", "enum": ["registrations", "students", "clubs"]}, "confirm": CONFIRM}, ["target"]),
     tool_clear_data, DANGER),
]
TOOL_INDEX = {item[0]: item for item in TOOLS}


def run_tool(name: str, arguments: dict) -> tuple[str, bool]:
    if name not in TOOL_INDEX:
        return "工具名称请从 tools/list 中选择（收到：{}）".format(name), True
    if not isinstance(arguments, dict):
        return "参数应为对象", True
    try:
        result = TOOL_INDEX[name][4](arguments)
        return dumps(result), False
    except ToolError as exc:
        return str(exc), True
    except subprocess.TimeoutExpired as exc:
        return "命令超时：{}".format(exc), True
    except Exception as exc:  # noqa: BLE001 - 返回说明，服务继续运行
        traceback.print_exc(file=sys.stderr)
        return "请重试；详情 {}: {}".format(type(exc).__name__, exc), True


# ---------------------------------------------------------------------------
# MCP（JSON-RPC 2.0，stdio，每行一条消息）
# ---------------------------------------------------------------------------
def tool_descriptor(item) -> dict:
    name, title, description, input_schema, _handler, annotations = item
    return {"name": name, "title": title, "description": description, "inputSchema": input_schema,
            "annotations": dict(annotations, title=title)}


def guide_text() -> str:
    try:
        return GUIDE.read_text(encoding="utf-8")
    except OSError:
        return INSTRUCTIONS


def handle(message):
    if not isinstance(message, dict):
        return {"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "Invalid Request"}}
    method, params, ident = message.get("method"), message.get("params") or {}, message.get("id")
    if ident is None:
        return None
    if method == "initialize":
        requested = params.get("protocolVersion")
        result = {
            "protocolVersion": requested if requested in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[0],
            "capabilities": {"tools": {"listChanged": False}, "resources": {"listChanged": False}},
            "serverInfo": {"name": "omms", "title": "社团选课", "version": VERSION},
            "instructions": INSTRUCTIONS,
        }
    elif method == "ping":
        result = {}
    elif method == "tools/list":
        result = {"tools": [tool_descriptor(item) for item in TOOLS]}
    elif method == "tools/call":
        name = params.get("name")
        if name not in TOOL_INDEX:
            return {"jsonrpc": "2.0", "id": ident, "error": {"code": -32602, "message": "Unknown tool: {}".format(name)}}
        text, is_error = run_tool(name, params.get("arguments") or {})
        result = {"content": [{"type": "text", "text": text}], "isError": is_error}
    elif method == "resources/list":
        result = {"resources": [{"uri": "omms://guide", "name": "guide", "title": "社团选课运行指南",
                                 "description": "代理运行和管理本系统的完整指南", "mimeType": "text/markdown"}]}
    elif method == "resources/templates/list":
        result = {"resourceTemplates": []}
    elif method == "resources/read":
        if params.get("uri") != "omms://guide":
            return {"jsonrpc": "2.0", "id": ident, "error": {"code": -32002, "message": "Resource not found"}}
        result = {"contents": [{"uri": "omms://guide", "mimeType": "text/markdown", "text": guide_text()}]}
    else:
        return {"jsonrpc": "2.0", "id": ident, "error": {"code": -32601, "message": "Method not found: {}".format(method)}}
    return {"jsonrpc": "2.0", "id": ident, "result": result}


def serve() -> int:
    output = sys.stdout.buffer
    for raw in sys.stdin.buffer:
        line = raw.strip()
        if not line:
            continue
        try:
            message = json.loads(line.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            response = {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}}
        else:
            if isinstance(message, list):
                replies = [reply for reply in (handle(item) for item in message) if reply]
                response = replies or None
            else:
                response = handle(message)
        if response:
            output.write(json.dumps(response, ensure_ascii=False).encode("utf-8") + b"\n")
            output.flush()
    return 0


# ---------------------------------------------------------------------------
# 命令行
# ---------------------------------------------------------------------------
def claude_desktop_config() -> Path:
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "Claude" / "claude_desktop_config.json"
    if os.name == "nt":
        return Path(os.environ.get("APPDATA") or Path.home()) / "Claude" / "claude_desktop_config.json"
    return Path.home() / ".config" / "Claude" / "claude_desktop_config.json"


def register(target: str) -> int:
    script = str(Path(__file__).resolve())
    python = shutil.which("python3", path=child_env()["PATH"]) or sys.executable
    if target == "claude-desktop":
        config = claude_desktop_config()
        data = {}
        if config.is_file():
            try:
                data = json.loads(config.read_text(encoding="utf-8") or "{}")
            except ValueError:
                print("{} 需为有效的 JSON，配置保持原样".format(config))
                return 1
            shutil.copy2(str(config), str(config) + ".bak")
        data.setdefault("mcpServers", {})["omms"] = {"command": python, "args": [script]}
        config.parent.mkdir(parents=True, exist_ok=True)
        config.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print("已写入 {}。完全退出并重新打开 Claude 桌面版后，工具列表里出现 omms。".format(config))
        return 0
    if target == "codex":
        config = Path.home() / ".codex" / "config.toml"
        text = config.read_text(encoding="utf-8") if config.is_file() else ""
        if "[mcp_servers.omms]" in text:
            print("{} 中已登记 omms".format(config))
            return 0
        block = "\n[mcp_servers.omms]\ncommand = {}\nargs = [{}]\n".format(json.dumps(python), json.dumps(script))
        config.parent.mkdir(parents=True, exist_ok=True)
        with open(config, "a", encoding="utf-8") as handle_:
            handle_.write(block)
        print("已写入 {}。重新启动 Codex 后可用。".format(config))
        return 0
    if target == "show":
        entry = {"command": python, "args": [script]}
        print("Claude 桌面版（{}）：".format(claude_desktop_config()))
        print(json.dumps({"mcpServers": {"omms": entry}}, ensure_ascii=False, indent=2))
        print("\nClaude Code：claude mcp add --scope user omms -- {} {}".format(python, script))
        print("\nCodex（~/.codex/config.toml）：\n[mcp_servers.omms]\ncommand = {}\nargs = [{}]".format(
            json.dumps(python), json.dumps(script)))
        print("\nCursor（~/.cursor/mcp.json）与其他支持 MCP 的客户端：")
        print(json.dumps({"mcpServers": {"omms": entry}}, ensure_ascii=False, indent=2))
        print("\nVS Code（.vscode/mcp.json）：")
        print(json.dumps({"servers": {"omms": dict(entry, type="stdio")}}, ensure_ascii=False, indent=2))
        return 0
    print("用法：python3 omms_mcp.py register claude-desktop | codex | show")
    return 2


def main(argv: list) -> int:
    if not argv:
        if sys.stdin.isatty():
            print(__doc__, file=sys.stderr)
        return serve()
    command = argv[0]
    if command == "tools":
        for name, title, description, *_rest in TOOLS:
            print("{:22} {}：{}".format(name, title, description))
        return 0
    if command == "call" and len(argv) >= 2:
        try:
            arguments = json.loads(argv[2]) if len(argv) > 2 else {}
        except ValueError:
            print("参数应为 JSON 对象，例如 '{\"lines\": 20}'")
            return 2
        text, is_error = run_tool(argv[1], arguments)
        print(text)
        return 1 if is_error else 0
    if command == "register":
        return register(argv[1] if len(argv) > 1 else "show")
    print(__doc__)
    return 0 if command in ("-h", "--help", "help") else 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
