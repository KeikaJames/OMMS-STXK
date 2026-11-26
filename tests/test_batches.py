#!/usr/bin/env python3
"""分批报名：各批在自己的时段报名，社团名额按批计算，当前批次截止后切换到下一批，最后一批截止后锁定。"""

from __future__ import annotations

import contextlib
import http.client
import json
import os
from pathlib import Path
import shutil
import sqlite3
import sys
import tempfile
import time
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_lifecycle import Instance, request, request_json, school_time  # noqa: E402
from test_redis_recovery import key, redis, write_session  # noqa: E402

STUDENTS = [(1, "甲", "高一1班"), (2, "乙", "高一1班"), (3, "丙", "高一2班"), (4, "丁", "高一2班"),
            (5, "戊", "高一3班")]


@unittest.skipIf(redis is None or shutil.which("redis-server") is None, "redis unavailable")
class BatchTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="omms-batches-")
        self.app = Instance(Path(self.tmp.name))
        self.app.start()
        self.app.sql("INSERT INTO clubs (id, name, max_students, current_students) VALUES (1, '激光雕切', 1, 0)")
        for student_id, name, klass in STUDENTS:
            self.app.sql("INSERT INTO students (id, name, class, grade, student_id, username, password) "
                         "VALUES (?, ?, ?, '高一', ?, ?, 'unused')",
                         (student_id, name, klass, "S{:04d}".format(student_id), "stu{}".format(student_id)))
        self.app.republish()
        self.admin = self.app.admin_token()

    def tearDown(self) -> None:
        self.app.close()
        self.tmp.cleanup()

    def save(self, batch_mode: bool, batches: list | None = None) -> tuple[int, dict]:
        body = {"batch_mode": batch_mode, "request_id": os.urandom(8).hex()}
        if batches is not None:
            body["batches"] = batches
        return self.app.post(self.admin, "/api/admin/batches", body)

    def register(self, student_id: int, club_id: int = 1) -> tuple[int, dict]:
        token = write_session(self.app.client, student_id)
        return request_json(self.app.http_port, "POST", "/api/register_club", token=token, body={"club_id": club_id})

    def cancel(self, student_id: int) -> tuple[int, dict]:
        token = write_session(self.app.client, student_id)
        return request_json(self.app.http_port, "POST", "/api/cancel_registration", token=token, body={})

    def wait_for(self, check, timeout: float = 8.0) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if check():
                return
            time.sleep(0.05)
        self.fail("condition not reached within {} s".format(timeout))

    def test_batches_are_validated_and_numbered_by_opening_time(self) -> None:
        later = {"start_time": school_time(7200), "end_time": school_time(9000), "classes": ["高一2班"]}
        status, body = self.save(True, [{"start_time": school_time(3600), "end_time": school_time(1800),
                                         "classes": ["高一1班"]}])
        self.assertEqual((400, "第 1 批的截止时间请晚于开放时间"), (status, body.get("message")))
        status, body = self.save(True, [{"start_time": school_time(3600), "end_time": school_time(8000),
                                         "classes": ["高一1班"]}, later])
        self.assertEqual((400, "第 2 批请在第 1 批截止后开放"), (status, body.get("message")))
        status, body = self.save(True, [{"start_time": school_time(3600), "end_time": school_time(5400),
                                         "classes": ["高一2班"]}, later])
        self.assertEqual((400, "“高一2班”请只放在一个批次里"), (status, body.get("message")))
        status, body = self.save(True, [dict(later, classes=[])])
        self.assertEqual((400, "请为第 1 批选择班级"), (status, body.get("message")))

        earlier = {"start_time": school_time(3600), "end_time": school_time(5400), "classes": ["高一1班"]}
        status, body = self.save(True, [later, earlier])
        self.assertEqual(200, status, body)
        self.assertEqual([1, 2], [batch["id"] for batch in body["batches"]])
        self.assertEqual([["高一1班"], ["高一2班"]], [batch["classes"] for batch in body["batches"]])
        self.assertEqual(["高一3班"], [item["class"] for item in body["unassigned"]])
        status, info = request_json(self.app.http_port, "GET", "/api/batches")
        self.assertEqual((True, 1), (info["batch_mode"], info["current"]))
        self.assertEqual(["第 1 批", "第 2 批"], [batch["name"] for batch in info["batches"]])
        self.assertEqual("1", self.app.client.get(key("batch")))
        self.assertEqual({"高一1班": "1", "高一2班": "2"}, self.app.client.hgetall(key("batchmap")))
        self.assertEqual([(1, 1, earlier["start_time"])], self.app.sql(
            "SELECT batch_mode, current_batch, registration_start_time FROM settings"))
        status, body = self.app.post(self.admin, "/api/update_registration_time", {"start_time": school_time(60)})
        self.assertEqual((409, "分批报名时，请在各批次里设置时间"), (status, body.get("message")))

    def test_each_batch_registers_in_its_own_window_with_its_own_seats(self) -> None:
        switch = school_time(4)
        status, body = self.save(True, [
            {"start_time": school_time(-60), "end_time": switch, "classes": ["高一1班"]},
            {"start_time": switch, "end_time": school_time(3600), "classes": ["高一2班"]},
        ])
        self.assertEqual(200, status, body)
        status, body = self.register(1)
        self.assertTrue(body.get("success"), body)
        status, body = self.register(2)
        self.assertEqual("full", body.get("code"), body)
        for student_id in (3, 5):
            status, body = self.register(student_id)
            self.assertEqual("not_open", body.get("code"), (student_id, body))
        status, seats = request_json(self.app.http_port, "GET", "/api/seats")
        self.assertEqual((1, [[1, 0]], False), (seats["batch"], seats["seats"], seats["locked"]))

        # 第 1 批截止后切换到第 2 批：第 2 批有自己的名额，第 1 批的学生停止报名和退选。
        self.wait_for(lambda: request_json(self.app.http_port, "GET", "/api/seats")[1]["batch"] == 2)
        status, seats = request_json(self.app.http_port, "GET", "/api/seats")
        self.assertEqual(([[1, 1]], False), (seats["seats"], seats["locked"]))
        status, body = self.register(3)
        self.assertTrue(body.get("success"), body)
        status, body = self.register(4)
        self.assertEqual("full", body.get("code"), body)
        status, body = self.register(2)
        self.assertEqual("locked", body.get("code"), body)
        status, body = self.cancel(1)
        self.assertEqual("locked", body.get("code"), body)
        self.assertEqual([(2, switch)], self.app.sql("SELECT current_batch, registration_start_time FROM settings"))
        self.assertEqual([(0,)], self.app.sql("SELECT registration_locked FROM settings"))
        with self.assertRaises(sqlite3.IntegrityError):
            with contextlib.closing(sqlite3.connect(self.app.db_path)) as conn:
                conn.execute("INSERT INTO registrations (student_id, club_id, registration_time, operation_id) "
                             "VALUES (4, 1, '2026-01-01 00:00:00', 'direct-4')")
                conn.commit()
        status, body = self.cancel(3)
        self.assertTrue(body.get("success"), body)
        status, body = self.register(4)
        self.assertTrue(body.get("success"), body)

    def batch_now(self, batch_id: int, action: str) -> tuple[int, dict]:
        return self.app.post(self.admin, "/api/admin/batch_now",
                             {"batch_id": batch_id, "action": action, "request_id": os.urandom(8).hex()})

    def test_open_now_and_close_now_follow_the_lab_session(self) -> None:
        status, body = self.save(True, [
            {"start_time": school_time(600), "end_time": school_time(1200), "classes": ["高一1班"]},
            {"start_time": school_time(1800), "end_time": school_time(2400), "classes": ["高一2班"]},
        ])
        self.assertEqual(200, status, body)
        status, body = self.register(1)
        self.assertEqual("not_open", body.get("code"), body)
        status, body = self.batch_now(1, "close")
        self.assertEqual((409, "第 1 批报名中时可以现在截止"), (status, body.get("message")))
        status, body = self.batch_now(1, "open")
        self.assertEqual(200, status, body)
        status, body = self.batch_now(2, "open")
        self.assertEqual((409, "请先截止第 1 批"), (status, body.get("message")))
        status, body = self.register(1)
        self.assertTrue(body.get("success"), body)

        status, body = self.batch_now(1, "close")
        self.assertEqual((200, 2), (status, body.get("current_batch")), body)
        status, body = self.register(2)
        self.assertEqual("locked", body.get("code"), body)
        status, body = self.batch_now(2, "open")
        self.assertEqual(200, status, body)
        status, body = self.register(3)
        self.assertTrue(body.get("success"), body)
        self.assertEqual(4, len(self.app.sql("SELECT 1 FROM audit_events WHERE action='batches.updated'")))

        conn = http.client.HTTPConnection("127.0.0.1", self.app.http_port, timeout=10.0)
        try:
            conn.request("GET", "/api/batches")
            response = conn.getresponse()
            tag, payload = response.getheader("ETag"), json.loads(response.read())
            conn.request("GET", "/api/batches", headers={"If-None-Match": tag})
            again = conn.getresponse()
            again.read()
        finally:
            conn.close()
        self.assertEqual((2, "open"), (payload["current"], payload["batches"][1]["state"]))
        self.assertEqual(304, again.status)

    def test_last_batch_closing_locks_and_adjustment_stays_within_each_batch(self) -> None:
        self.app.sql("INSERT INTO clubs (id, name, max_students, current_students) VALUES (2, '影视拍摄', 1, 0)")
        self.app.republish()
        middle = school_time(2)
        status, body = self.save(True, [
            {"start_time": school_time(-60), "end_time": middle, "classes": ["高一1班"]},
            {"start_time": middle, "end_time": school_time(4), "classes": ["高一2班"]},
        ])
        self.assertEqual(200, status, body)
        self.wait_for(lambda: self.app.sql("SELECT registration_locked FROM settings") == [(1,)], timeout=10.0)
        self.assertEqual([("系统", "最后一批截止")], self.app.sql(
            "SELECT actor_id, reason FROM audit_events WHERE action='registration.lock_updated'"))
        status, body = self.register(1)
        self.assertEqual("locked", body.get("code"), body)

        status, plan = request_json(self.app.http_port, "GET", "/api/admin/adjustment", token=self.admin)
        self.assertEqual(200, status, plan)
        self.assertEqual((True, 5, 4, 1), (plan["batch_mode"], plan["unregistered"], plan["assignable"],
                                           plan["unbatched"]))
        self.assertEqual({(1, 1), (1, 2), (2, 1), (2, 2)}, {(club["batch"], club["id"]) for club in plan["clubs"]})
        status, body = self.app.post(self.admin, "/api/admin/adjust_registrations", {"request_id": os.urandom(8).hex()})
        self.assertEqual((200, 4, 1), (status, body.get("assigned"), body.get("unassigned")), body)
        self.assertEqual([(1, 1, 1), (1, 2, 1), (2, 1, 1), (2, 2, 1)], self.app.sql(
            "SELECT bc.batch_id, r.club_id, COUNT(*) FROM registrations r JOIN students s ON s.id = r.student_id "
            "JOIN batch_classes bc ON bc.class_name = s.class GROUP BY 1, 2 ORDER BY 1, 2"))

        # 管理员解除锁定后保持解除；各批时段已过，报名仍然停止。
        status, body = self.app.post(self.admin, "/api/update_registration_lock",
                                     {"locked": False, "reason": "测试", "request_id": os.urandom(8).hex()})
        self.assertEqual(200, status, body)
        time.sleep(1.5)
        self.assertEqual([(0,)], self.app.sql("SELECT registration_locked FROM settings"))
        status, body = self.cancel(1)
        self.assertEqual("locked", body.get("code"), body)

    def test_switching_modes_checks_capacity_and_exports_the_batch_column(self) -> None:
        app = self.app
        app.sql("UPDATE clubs SET max_students=3")
        status, body = app.post(self.admin, "/api/update_registration_time", {"start_time": school_time(-60)})
        self.assertEqual(200, status, body)
        app.republish()
        for student_id in (1, 2, 3):
            status, body = self.register(student_id)
            self.assertTrue(body.get("success"), (student_id, body))

        together = [{"start_time": school_time(-60), "end_time": school_time(3600),
                     "classes": ["高一1班", "高一2班"]}]
        status, body = self.save(True, together)
        self.assertEqual(200, status, body)
        update = {"club_id": 1, "max_students": 2, "reason": "测试", "request_id": os.urandom(8).hex()}
        status, body = app.post(self.admin, "/api/update_club", update)
        self.assertEqual((400, "每批名额至少为 3"), (status, body.get("message")))
        split = [{"start_time": school_time(-60), "end_time": school_time(1800), "classes": ["高一1班"]},
                 {"start_time": school_time(1800), "end_time": school_time(3600), "classes": ["高一2班"]}]
        status, body = self.save(True, split)
        self.assertEqual(200, status, body)
        status, body = app.post(self.admin, "/api/update_club", dict(update, request_id=os.urandom(8).hex()))
        self.assertEqual(200, status, body)
        status, body = self.save(True, together)
        self.assertEqual((409, "请把“激光雕切”的每批名额设为至少 3：第 1 批已有 3 人报名"),
                         (status, body.get("message")))

        status, headers, raw = request(app.http_port, "GET", "/api/export_all_data", token=self.admin)
        self.assertEqual(200, status)
        lines = raw.decode("utf-8-sig").splitlines()
        self.assertEqual("批次,姓名,班级,社团,方式,报名时间", lines[0])
        self.assertEqual(["第 1 批", "第 1 批", "第 2 批", "第 2 批", "待分批"], [line.split(",")[0] for line in lines[1:]])

        # 改回同时报名时全体共用一个名额池：3 人已报名，人数上限需不少于 3。
        status, body = self.save(False)
        self.assertEqual((409, "请把“激光雕切”的人数上限设为至少 3：已有 3 人报名"),
                         (status, body.get("message")))
        status, body = app.post(self.admin, "/api/update_club", dict(update, max_students=5,
                                                                      request_id=os.urandom(8).hex()))
        self.assertEqual(200, status, body)
        status, body = self.save(False)
        self.assertEqual(200, status, body)
        self.assertEqual([(0, None, None, None)], app.sql(
            "SELECT batch_mode, current_batch, registration_start_time, registration_end_time FROM settings"))
        self.assertIsNone(app.client.get(key("batch")))
        status, state = request_json(app.http_port, "GET", "/api/check_registration_time")
        self.assertEqual((False, None, False), (state["batch_mode"], state["batch"], state["can_register"]))
        status, headers, raw = request(app.http_port, "GET", "/api/export_all_data", token=self.admin)
        self.assertEqual("姓名,班级,社团,方式,报名时间", raw.decode("utf-8-sig").splitlines()[0])


if __name__ == "__main__":
    unittest.main()
