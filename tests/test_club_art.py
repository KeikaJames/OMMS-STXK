#!/usr/bin/env python3
"""社团卡片插图：新社团按名称先选一幅，管理员在编辑页更换；改名不影响已选的插图。"""

from __future__ import annotations

import ast
import json
import os
from pathlib import Path
import re
import shutil
import socket
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_lifecycle import Instance, request_json  # noqa: E402
from test_redis_recovery import REPO_ROOT, redis  # noqa: E402


def unused_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


# 只为读取插图键名和 suggest_art 导入 main.py：指向空闲端口和临时数据库，6379 与仓库数据库保持原样。
TEMP = tempfile.TemporaryDirectory(prefix="omms-art-import-")
os.environ.update({"REDIS_URL": f"redis://127.0.0.1:{unused_port()}/0", "REDIS_TIMEOUT": "0.2",
                   "DB_PATH": str(Path(TEMP.name) / "unused.sqlite3"), "LOG_LEVEL": "ERROR"})
sys.path.insert(0, str(REPO_ROOT))
import main  # noqa: E402


def agent_art_keys() -> list:
    tree = ast.parse((REPO_ROOT / "agent" / "omms_mcp.py").read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(getattr(target, "id", "") == "CLUB_ART" for target in node.targets):
            return list(ast.literal_eval(node.value))
    raise AssertionError("CLUB_ART missing from agent/omms_mcp.py")


class ArtCatalogTests(unittest.TestCase):
    def test_python_agent_and_page_share_the_same_illustrations(self) -> None:
        script = (REPO_ROOT / "web" / "js" / "club-art.js").read_text(encoding="utf-8")
        listed = re.findall(r"\['([a-z0-9]+)', '[^']+'\]", script)
        drawn = re.findall(r"^    ([a-z0-9]+): '", script, re.M)
        self.assertEqual(list(main.CLUB_ART), listed)
        self.assertEqual(sorted(main.CLUB_ART), sorted(drawn))
        self.assertEqual(list(main.CLUB_ART), agent_art_keys())

    def test_new_clubs_start_with_an_illustration_that_fits_the_name(self) -> None:
        cases = {"激光雕切": "laser", "Photoshop设计": "design", "3D设计": "model3d", "3D打印": "print",
                 "影视拍摄制作": "media", "木工": "wood", "Scratch 编程": "code", "机器人": "robot",
                 "美术": "paint", "合唱团": "music", "化学实验": "science", "篮球": "sports",
                 "辩论社": "book", "棋艺社": "club"}
        self.assertEqual(cases, {name: main.suggest_art(name) for name in cases})
        self.assertTrue(set(cases.values()) <= set(main.CLUB_ART))


@unittest.skipIf(redis is None or shutil.which("redis-server") is None, "redis unavailable")
class ClubArtTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="omms-art-")
        self.app = Instance(Path(self.tmp.name))
        self.app.start()
        self.admin = self.app.admin_token()

    def tearDown(self) -> None:
        self.app.close()
        self.tmp.cleanup()

    def catalog(self) -> dict:
        status, body = request_json(self.app.http_port, "GET", "/api/catalog")
        self.assertEqual(200, status)
        return {club["name"]: club for club in body}

    def update(self, club: dict, **fields) -> tuple[int, dict]:
        body = {"club_id": club["id"], "request_id": os.urandom(8).hex(), "reason": "测试"}
        body.update(fields)
        return self.app.post(self.admin, "/api/update_club", body)

    def test_the_admin_chooses_the_illustration_and_a_rename_keeps_it(self) -> None:
        status, body = self.app.post(self.admin, "/api/import_clubs", {"clubs": [
            {"name": "激光雕切", "max_students": 30}, {"name": "棋艺社", "max_students": 30}]})
        self.assertEqual((200, 2), (status, body.get("success")), body)
        clubs = self.catalog()
        self.assertEqual(("laser", "club"), (clubs["激光雕切"]["art"], clubs["棋艺社"]["art"]))

        status, body = self.update(clubs["棋艺社"], art="book")
        self.assertEqual(200, status, body)
        self.assertEqual("book", body["club"]["art"])
        status, body = self.update(self.catalog()["棋艺社"], name="3D打印")
        self.assertEqual(200, status, body)
        self.assertEqual("book", self.catalog()["3D打印"]["art"])
        status, body = self.update(self.catalog()["3D打印"], art="poster")
        self.assertEqual((400, "请从列出的插图中选择"), (status, body.get("message")))
        status, clubs = request_json(self.app.http_port, "GET", "/api/get_clubs")
        self.assertEqual({"laser", "book"}, {club["art"] for club in clubs})
        recorded = self.app.sql("SELECT before_json, after_json FROM audit_events WHERE action='club.updated' "
                                "ORDER BY id LIMIT 1")[0]
        self.assertEqual(("club", "book"), (json.loads(recorded[0])["art"], json.loads(recorded[1])["art"]))

    def test_clubs_from_an_older_database_get_an_illustration_on_upgrade(self) -> None:
        self.app.sql("INSERT INTO clubs (id, name, max_students, current_students, art) "
                     "VALUES (7, '影视拍摄制作', 30, 0, '')")
        self.app.stop()
        self.app.start()
        self.assertEqual([("media",)], self.app.sql("SELECT art FROM clubs WHERE id=7"))


if __name__ == "__main__":
    unittest.main()
