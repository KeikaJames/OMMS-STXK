#!/usr/bin/env python3
"""AI 代理接口：MCP 协议、工具清单与参数结构、各工具对临时服务的端到端操作、运行参数文件。"""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from test_lifecycle import Instance  # noqa: E402
from test_redis_recovery import REPO_ROOT, redis  # noqa: E402

import sheets  # noqa: E402

AGENT = REPO_ROOT / "agent" / "omms_mcp.py"


class Client:
    """以子进程运行 MCP 服务，按行收发 JSON-RPC。"""

    def __init__(self, env: dict) -> None:
        self.process = subprocess.Popen([sys.executable, str(AGENT)], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                        stderr=subprocess.PIPE, env=env)
        self.counter = 0

    def send(self, message: dict) -> None:
        self.process.stdin.write((json.dumps(message, ensure_ascii=False) + "\n").encode("utf-8"))
        self.process.stdin.flush()

    def request(self, method: str, params: dict | None = None) -> dict:
        self.counter += 1
        message = {"jsonrpc": "2.0", "id": self.counter, "method": method}
        if params is not None:
            message["params"] = params
        self.send(message)
        reply = json.loads(self.process.stdout.readline())
        assert reply["id"] == self.counter, reply
        return reply

    def call(self, tool_name: str, **arguments) -> tuple[bool, object]:
        result = self.request("tools/call", {"name": tool_name, "arguments": arguments})["result"]
        text = result["content"][0]["text"]
        return result["isError"], (text if result["isError"] else json.loads(text))

    def ok(self, tool_name: str, **arguments) -> dict:
        failed, value = self.call(tool_name, **arguments)
        if failed:
            raise AssertionError("{} 失败：{}".format(tool_name, value))
        return value

    def close(self) -> None:
        self.process.stdin.close()
        self.process.wait(timeout=20)
        self.process.stdout.close()
        self.process.stderr.close()


def union_types(node) -> list:
    found = []
    if isinstance(node, dict):
        if isinstance(node.get("type"), list):
            found.append(node)
        for value in node.values():
            found.extend(union_types(value))
    elif isinstance(node, list):
        for value in node:
            found.extend(union_types(value))
    return found


@unittest.skipIf(redis is None or shutil.which("redis-server") is None, "redis unavailable")
class AgentTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="omms-agent-")
        self.work = Path(self.tmp.name)
        self.instance = Instance(self.work)
        self.instance.start()
        self.env = os.environ.copy()
        self.env.update({
            "OMMS_BACKEND": "http://127.0.0.1:{}".format(self.instance.http_port),
            "DB_PATH": str(self.instance.db_path),
            "REDIS_URL": "redis://127.0.0.1:{}/0".format(self.instance.redis_port),
            "OMMS_EXPORT_DIR": str(self.work / "exports"),
            "OMMS_SETTINGS": str(self.work / "omms.env"),
            "OMMS_RUN_DIR": str(self.work / "run"),
            "PYTHON": sys.executable,
        })
        self.client = Client(self.env)
        self.client.request("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                           "clientInfo": {"name": "test", "version": "1"}})
        self.client.send({"jsonrpc": "2.0", "method": "notifications/initialized"})

    def tearDown(self) -> None:
        self.client.close()
        self.instance.close()
        self.tmp.cleanup()

    def test_protocol_handshake_tools_and_guide(self) -> None:
        client = self.client
        reply = client.request("initialize", {"protocolVersion": "2024-11-05", "capabilities": {}})
        self.assertEqual("2024-11-05", reply["result"]["protocolVersion"])
        self.assertIn("只读", reply["result"]["instructions"])
        reply = client.request("initialize", {"protocolVersion": "1999-01-01", "capabilities": {}})
        self.assertEqual("2025-11-25", reply["result"]["protocolVersion"])
        self.assertEqual({}, client.request("ping")["result"])
        tools = client.request("tools/list")["result"]["tools"]
        names = {tool["name"] for tool in tools}
        for name in ("service_status", "start_service", "admin_credentials", "change_admin_password", "set_schedule",
                     "import_roster", "reset_passwords", "update_settings", "restore", "clear_data"):
            self.assertIn(name, names)
        for tool in tools:
            self.assertEqual("object", tool["inputSchema"]["type"], tool["name"])
            self.assertTrue(tool["description"], tool["name"])
            self.assertEqual([], union_types(tool["inputSchema"]), tool["name"])
        guide = client.request("resources/read", {"uri": "omms://guide"})["result"]["contents"][0]
        self.assertEqual("text/markdown", guide["mimeType"])
        self.assertIn("confirm", guide["text"])
        self.assertEqual(-32601, client.request("prompts/get", {"name": "x"})["error"]["code"])
        self.assertEqual(-32602, client.request("tools/call", {"name": "nope", "arguments": {}})["error"]["code"])

    def test_operations_through_the_tools(self) -> None:
        client = self.client
        secret = (self.work / "admin-password.txt").read_text(encoding="utf-8").strip()
        self.assertEqual(secret, client.ok("admin_credentials")["管理员密码"])

        schedule = client.ok("set_schedule", open_at="2099-01-01 08:00", close_at="2099/1/2 8:00")
        self.assertEqual(("待开放", "2099-01-01 08:00:00", "2099-01-02 08:00:00"),
                         (schedule["报名状态"], schedule["开放时间"], schedule["截止时间"]))
        failed, text = client.call("set_schedule", open_at="明天早上")
        self.assertTrue(failed)
        self.assertIn("YYYY-MM-DD", text)

        client.ok("add_club", name="3D打印", capacity=2, description="建模与打印", grades=["高一"])
        client.ok("add_club", name="木工", capacity=3)
        failed, text = client.call("add_club", name="木工", capacity=3)
        self.assertTrue(failed)
        self.assertIn("已存在", text)
        clubs = {club["名称"]: club for club in client.ok("list_clubs")["社团"]}
        self.assertEqual((["高一"], "建模与打印"), (clubs["3D打印"]["年级范围"], clubs["3D打印"]["简介"]))

        roster = self.work / "名单.csv"
        roster.write_text("序号,姓名,班级\n1,李四,\n2,张三,高一1班\n3,王五,高一2班\n合计,3 人,\n", encoding="utf-8")
        preview = client.ok("preview_roster", file_path=str(roster))
        self.assertEqual((3, 1), (preview["识别到的学生行"], preview["略过的行（合计、说明、空姓名等）"]))
        flagged = {row["姓名"]: row for row in preview["需核对的行"]}
        self.assertEqual(["缺少班级"], flagged["李四"]["标注"])
        position = flagged["李四"]["位置"]

        failed, text = client.call("import_roster", file_path=str(roster), confirm=True)
        self.assertTrue(failed)
        self.assertIn(position, text)
        fixes = [{"position": position, "class": "高一1班"}]
        failed, text = client.call("import_roster", file_path=str(roster), fixes=fixes)
        self.assertTrue(failed)
        self.assertIn("待确认", text)
        done = client.ok("import_roster", file_path=str(roster), fixes=fixes, confirm=True)
        self.assertEqual(3, done["已导入"])
        workbook = Path(done["账号表"][0])
        self.assertEqual(0o600, workbook.stat().st_mode & 0o777)
        rows = sheets.read_xlsx(workbook.read_bytes())[0].rows
        self.assertEqual(["姓名", "班级", "账号", "密码"], rows[0][:4])
        self.assertEqual({"李四", "张三", "王五"}, {row[0] for row in rows[1:] if row and row[0]})
        failed, text = client.call("import_roster", file_path=str(roster), fixes=fixes, confirm=True)
        self.assertTrue(failed)
        self.assertIn("可导入的行数为 0", text)

        listing = client.ok("list_students")
        self.assertEqual((3, {"高一1班": 2, "高一2班": 1}), (listing["学生总数"], listing["各班人数"]))
        wangwu = client.ok("list_students", query="王五")["列表"][0]["账号"]
        self.assertEqual("3D打印", client.ok("update_student", student="张三 高一1班", club="3D打印")["学生"]["社团"])
        self.assertEqual("3D打印", client.ok("update_student", student=wangwu, club="3D打印")["学生"]["社团"])
        failed, _text = client.call("update_student", student="李四", club="3D打印")
        self.assertTrue(failed)
        renamed = client.ok("update_student", student="李四", name="李思", **{"class": "高一2班"})
        self.assertEqual(("李思", "高一2班"), (renamed["学生"]["姓名"], renamed["学生"]["班级"]))

        failed, text = client.call("reset_passwords", **{"class": "高一1班"})
        self.assertTrue(failed)
        self.assertIn("待确认", text)
        reset = client.ok("reset_passwords", confirm=True, **{"class": "高一1班"})
        self.assertEqual((1, 1), (reset["已重置"], len(reset["新密码"])))
        self.assertTrue(Path(reset["账号表"][0]).is_file())
        single = client.ok("reset_passwords", student=wangwu, confirm=True)
        self.assertEqual(12, len(single["新密码"]))

        failed, text = client.call("update_settings", values={"HTTP_PORT": 2001})
        self.assertTrue(failed)
        failed, text = client.call("update_settings", values={"TLS_CERT": str(roster)})
        self.assertTrue(failed)
        self.assertIn("同时", text)
        client.ok("update_settings", values={"BACKUP_KEEP": 12, "HTTP_PORT": "80", "LOG_LEVEL": "warning"})
        self.assertEqual({"BACKUP_KEEP=12", "HTTP_PORT=80", "LOG_LEVEL=WARNING"},
                         set((self.work / "omms.env").read_text(encoding="utf-8").split()))
        settings = {item["键"]: item for item in client.ok("get_settings")["参数"]}
        self.assertEqual(("12", "参数文件"), (settings["BACKUP_KEEP"]["当前值"], settings["BACKUP_KEEP"]["来源"]))
        client.ok("update_settings", values={"HTTP_PORT": None})
        self.assertNotIn("HTTP_PORT", (self.work / "omms.env").read_text(encoding="utf-8"))

        failed, text = client.call("adjustment", run=True)
        self.assertTrue(failed)
        self.assertIn("锁定", text)
        self.assertEqual("已锁定", client.ok("set_lock", locked=True)["名单"])
        plan = client.ok("adjustment")
        self.assertEqual((1, 1), (plan["未报名"], plan["可安排"]))
        failed, text = client.call("adjustment", run=True)
        self.assertIn("待确认", text)
        self.assertEqual(1, client.ok("adjustment", run=True, confirm=True)["已安排"])

        exported = Path(client.ok("export_data", kind="workbook")["文件"])
        self.assertTrue(exported.read_bytes().startswith(b"PK"))
        self.assertTrue(Path(client.ok("export_data", kind="club", club="木工")["文件"]).is_file())
        backup = Path(client.ok("backup_now")["文件"])
        self.assertTrue(backup.read_bytes().startswith(b"PK"))

        failed, text = client.call("clear_data", target="registrations")
        self.assertIn("待确认", text)
        self.assertEqual(0, client.ok("clear_data", target="registrations", confirm=True)["报名"])
        snapshots = client.ok("list_snapshots")["快照"]
        before_clear = next(item["名称"] for item in snapshots if item["名称"].endswith("before-clear.db"))
        failed, text = client.call("restore", snapshot=before_clear)
        self.assertIn("待确认", text)
        self.assertEqual(3, client.ok("restore", snapshot=before_clear, confirm=True)["报名"])
        self.assertEqual(3, client.ok("get_overview")["已报名"])

        changed = client.ok("change_admin_password", new_password="Ab3dEf7hJk")
        self.assertTrue(changed["密码文件已更新"])
        self.assertEqual("Ab3dEf7hJk", client.ok("admin_credentials")["管理员密码"])
        self.assertEqual(3, client.ok("get_overview")["学生"])
        actions = {item["操作"] for item in client.ok("audit_log", limit=100)["记录"]}
        self.assertTrue({"导入学生", "调剂", "恢复备份", "修改管理员密码"} <= actions, actions)

        failed, text = client.call("delete_student", student=wangwu)
        self.assertIn("待确认", text)
        client.ok("delete_student", student=wangwu, confirm=True)
        self.assertEqual(2, client.ok("list_students")["学生总数"])

        result = subprocess.run([sys.executable, str(AGENT), "call", "get_overview"], env=self.env,
                                capture_output=True, text=True, timeout=60)
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertEqual(2, json.loads(result.stdout)["学生"])


class SettingsFileTests(unittest.TestCase):
    def test_run_script_reads_only_listed_keys_and_lets_the_command_line_win(self) -> None:
        script = (REPO_ROOT / "run.sh").read_text(encoding="utf-8")
        start = script.index('SETTINGS_FILE="${OMMS_SETTINGS')
        end = script.index("\nfi\n", start) + 4
        with tempfile.TemporaryDirectory(prefix="omms-settings-") as tmp:
            settings = Path(tmp) / "omms.env"
            settings.write_text("HTTP_PORT=8088\r\n# 注释\nDB_PATH=/elsewhere.db\nLOG_LEVEL=DEBUG\nAUTH_CONCURRENCY=3\n不是参数\n",
                                encoding="utf-8")
            probe = script[start:end] + 'echo "$HTTP_PORT|${DB_PATH:-}|$LOG_LEVEL|$AUTH_CONCURRENCY"\n'
            env = {key: value for key, value in os.environ.items() if key not in ("DB_PATH", "HTTP_PORT", "AUTH_CONCURRENCY")}
            env.update({"OMMS_SETTINGS": str(settings), "LOG_LEVEL": "WARNING", "DIR": tmp})
            result = subprocess.run(["bash", "-c", probe], env=env, capture_output=True, text=True, timeout=30)
        self.assertEqual("8088||WARNING|3", result.stdout.strip(), result.stderr)


if __name__ == "__main__":
    unittest.main()
