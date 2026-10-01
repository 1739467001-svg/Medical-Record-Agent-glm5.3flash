# -*- coding: utf-8 -*-
"""records 单元测试：审签状态机 / 角色权限 / 修改回流聚合。
运行前将 auth._ACTIVE_SQLITE 指向临时库（不触碰研发 local_dev.db）；
records 模块级 init_db 会在临时库建 archives 表并播种演示数据。
"""
import os, sys, sqlite3, tempfile, unittest

SERVER_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, SERVER_DIR)
os.environ.pop("LLM_API_BASE", None)
os.environ.pop("LLM_API_KEY", None)

import auth
_TMP = tempfile.mkdtemp(prefix="mra_records_test_")
auth._ACTIVE_SQLITE = os.path.join(_TMP, "test.db")
_conn = sqlite3.connect(auth._ACTIVE_SQLITE)
for _ddl in auth._DDL_SQLITE:
    _conn.execute(_ddl)
_conn.commit()
_conn.close()
import records   # noqa: E402  模块级 init_db → 临时库 + 播种


def make_handler(path="/records"):
    class H:
        def __init__(self):
            self.response = None
            self.path = path
        def _json(self, code, obj, set_cookies=None):
            self.response = (code, obj)
    return H()

ATT = {"id": 2, "username": "att", "name": "李主任", "role": "attending"}
RES = {"id": 3, "username": "res", "name": "王医生", "role": "resident"}
QC = {"id": 4, "username": "qc", "name": "质控员", "role": "qc"}

BASE_FIELDS = [
    {"label": "主诉", "value": "右下腹痛1天", "binding": "A01.03"},
    {"label": "体温", "value": "37.0", "binding": ""},
    {"label": "个人史", "value": "", "binding": ""},
]

def submit(user=RES, edits=None, fields=None):
    """走真实 do_submit 归档一条；返回 (record_id, record_no)。"""
    h = make_handler("/records/submit")
    records.do_submit(h, {"code": "DA0001", "admission": 1, "doc": "入院记录", "disease": "急性阑尾炎",
                          "fields": fields or BASE_FIELDS, "qc": {}, "edits": edits or [], "xml": ""}, user)
    assert h.response[0] == 200, h.response
    return h.response[1]["id"], h.response[1]["record_no"]


class TestSubmit(unittest.TestCase):
    def test_submit_creates_submitted_record(self):
        rid, no = submit()
        self.assertRegex(no, r"^AR-\d{8}-\d{4}$")
        h = make_handler(f"/records/detail?id={rid}")
        records.do_detail(h, RES, {"id": str(rid)})
        rec = h.response[1]["record"]
        self.assertEqual(rec["status"], "submitted")
        self.assertEqual(rec["total_cnt"], 3)
        self.assertEqual(rec["yellow_cnt"], 1)   # 个人史为空 → 黄

    def test_submit_requires_fields(self):
        h = make_handler("/records/submit")
        records.do_submit(h, {"code": "DA0001", "doc": "入院记录", "fields": []}, RES)
        self.assertEqual(h.response[0], 400)


class TestReviewStateMachine(unittest.TestCase):
    def test_sign_then_guard_409(self):
        rid, _ = submit()
        h = make_handler()
        records.do_review(h, {"id": rid, "action": "sign", "comment": "同意签发"}, ATT)
        self.assertEqual(h.response[0], 200)
        self.assertEqual(h.response[1]["status"], "signed")
        h2 = make_handler()
        records.do_review(h2, {"id": rid, "action": "reject", "comment": "再退"}, ATT)
        self.assertEqual(h2.response[0], 409)   # 已审签不可重复处置

    def test_reject_requires_comment(self):
        rid, _ = submit()
        h = make_handler()
        records.do_review(h, {"id": rid, "action": "reject", "comment": ""}, ATT)
        self.assertEqual(h.response[0], 400)
        records.do_review(h, {"id": rid, "action": "reject", "comment": "个人史待补"}, ATT)
        self.assertEqual(h.response[0], 200)
        h2 = make_handler(f"/records/detail?id={rid}")
        records.do_detail(h2, RES, {"id": str(rid)})
        self.assertEqual(h2.response[1]["record"]["status"], "rejected")
        self.assertIn("个人史待补", h2.response[1]["record"]["sign_comment"])  # 退回原因回流

    def test_role_guards(self):
        rid, _ = submit()
        h = make_handler()
        records.do_review(h, {"id": rid, "action": "sign"}, QC)          # 质控科不可审签
        self.assertEqual(h.response[0], 403)
        h2 = make_handler()
        records.do_spotcheck(h2, {"id": rid, "result": "ok"}, ATT)       # 上级医师不可抽查
        self.assertEqual(h2.response[0], 403)
        h3 = make_handler()
        records.do_spotcheck(h3, {"id": rid, "result": "issue", "comment": "缺项"}, QC)
        self.assertEqual(h3.response[0], 200)
        h4 = make_handler(f"/records/detail?id={rid}")
        records.do_detail(h4, QC, {"id": str(rid)})
        self.assertEqual(h4.response[1]["record"]["spot_result"], "issue")

    def test_detail_owner_only(self):
        rid, _ = submit(user=RES)
        h = make_handler(f"/records/detail?id={rid}")
        records.do_detail(h, QC, {"id": str(rid)})   # 无关住院医师不可看他人文书
        other = {"id": 99, "username": "other", "name": "别的医生", "role": "resident"}
        h2 = make_handler(f"/records/detail?id={rid}")
        records.do_detail(h2, other, {"id": str(rid)})
        self.assertEqual(h2.response[0], 403)


class TestRoleVisibility(unittest.TestCase):
    def test_resident_sees_only_own(self):
        rid, _ = submit()
        h = make_handler("/records")
        records.do_list(h, RES, {})
        own = h.response[1]["records"]
        self.assertTrue(own)
        self.assertTrue(all(r["record_no"] != "AR-20260929-0001" for r in own))  # 种子（resident_id=0）不可见
        h2 = make_handler("/records")
        records.do_list(h2, ATT, {})
        self.assertGreater(len(h2.response[1]["records"]), len(own))             # 上级医师全量


class TestEditsAggregation(unittest.TestCase):
    def test_hot_color_and_samples(self):
        rid, _ = submit(edits=[
            {"field": "主诉", "before": "腹痛", "after": "右下腹痛1天", "time": "10:00:00", "doctor": "王医生"},
            {"field": "体温", "before": "36.5", "after": "37.0", "time": "10:05:00", "doctor": "王医生"},
            {"field": "发育", "before": "", "after": "正常", "time": "10:06:00", "doctor": "王医生"},
        ])
        h = make_handler("/records/edits")
        records.do_edits(h, RES, {})
        d = h.response[1]
        hot = {x["label"]: x for x in d["hot"]}
        self.assertEqual(hot["主诉"]["color"], "green")
        self.assertEqual(hot["体温"]["color"], "blue")
        self.assertEqual(hot["发育"]["color"], "gray")
        sample = next(s for s in hot["主诉"]["samples"] if s["record_no"].startswith("AR-") and s["after"] == "右下腹痛1天")
        self.assertEqual(sample["before"], "腹痛")
        self.assertTrue(any(r["field"] == "发育" for r in d["recent"]))

    def test_field_color_matches_frontend_rules(self):
        f = records.field_color
        self.assertEqual(f("主诉", "", "x"), "green")
        self.assertEqual(f("体温", "", "36.5"), "blue")
        self.assertEqual(f("发育", "", "正常"), "gray")
        self.assertEqual(f("个人史", "", ""), "yellow")          # 空值 → 黄
        self.assertEqual(f("姓名", "Patient", "张"), "blue")     # Patient 绑定 → 蓝
        self.assertEqual(f("医生签名", "", "王"), "yellow")       # 签名 → 黄


if __name__ == "__main__":
    unittest.main()
