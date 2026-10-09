# -*- coding: utf-8 -*-
"""tools/case_ingest.py 单测：真实案例接入传送带的纯函数层
（事实探测 / his 节构建 / 脱敏改写 / 红线扫描 / 骨架文书 / 启发式角色标注）。
不依赖 xls 文件与 LLM——用合成行字典直测；LLM 角色标注走网络不在单测范围。"""
import datetime, importlib.util, json, os, sys, unittest

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_spec = importlib.util.spec_from_file_location("case_ingest", os.path.join(REPO, "tools", "case_ingest.py"))
ci = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ci)


def dserial(d, h=0):
    """日期 → Excel 序列号（1899-12-30 基准）。"""
    return (d - datetime.date(1899, 12, 30)).days + h / 24.0


class TestDetectFacts(unittest.TestCase):
    def setUp(self):
        d = dserial(datetime.date(2026, 10, 2), 6)
        self.views = {
            "生命体征": [{"VITAL_SIGNS": "入院", "TIME_POINT": d, "RECORDING_DATE": dserial(datetime.date(2026, 10, 2))}],
            "医嘱": [
                {"ORDER_TEXT": "按普通外科护理常规", "START_DATE_TIME": d},
                {"ORDER_TEXT": '通知: 2026-10-02在静吸复合麻醉下进行"腹腔镜下阑尾切除术"手术', "START_DATE_TIME": d},
                {"ORDER_TEXT": "今日出院", "START_DATE_TIME": dserial(datetime.date(2026, 10, 8), 9)},
            ],
            "检查申请主表": [{"CLIN_DIAG": "阑尾炎"}, {"CLIN_DIAG": "腹痛"}],
            "检验申请主表": [{"RELEVANT_CLINIC_DIAG": "", "NAME": "某某某", "SEX": "男", "AGE": 28.0,
                              "REQUESTED_DATE_TIME": d}],
        }

    def test_detect(self):
        f = ci.detect_facts(self.views)
        self.assertEqual(f["admit_dt"].date(), datetime.date(2026, 10, 2))
        self.assertEqual(f["discharge_dt"].date(), datetime.date(2026, 10, 8))
        self.assertEqual(f["dept"], "普通外科")
        self.assertTrue(f["has_surgery"])
        self.assertEqual(f["disease"], "阑尾炎（手术）")   # 症状条目（腹痛）不作主诊断
        self.assertEqual(f["sex"], "男")
        self.assertEqual(f["age"], "28岁")
        self.assertIn("某某某", f["patient_names"])


class TestBuildHisNode(unittest.TestCase):
    def test_node_shapes(self):
        d = dserial(datetime.date(2026, 10, 2), 6)
        facts = {"admit_dt": datetime.datetime(2026, 10, 2, 6, 0)}
        views = {
            "生命体征": [{"VITAL_SIGNS": "脉搏", "VITAL_SIGNS_CVALUES": "103", "UNITS": "次/分",
                          "TIME_POINT": d, "RECORDING_DATE": d}],
            "检查申请主表": [{"EXAM_NO": "1", "EXAM_DATE_TIME": d}],
            "检查申请项目": [{"EXAM_NO": "1", "EXAM_ITEM": "CT下腹部平扫"}],
            "检查结果": [{"EXAM_NO": "1", "IMPRESSION": "阑尾炎。", "ABNORMAL": 1}],
            "检验结果": [{"REPORT_ITEM_NAME": "白细胞", "RESULT": "19.15", "UNITS": "10^9/L",
                          "ABNORMAL_INDICATOR": "H", "PRINT_CONTEXT": "3.5--9.5", "RESULT_DATE_TIME": d}],
            "医嘱": [{"ORDER_TEXT": "静脉输液", "DOSAGE": "", "DOSAGE_UNITS": "",
                      "ADMINISTRATION": "", "FREQUENCY": "", "START_DATE_TIME": d}],
        }
        his = ci.build_his_node(views, facts)
        self.assertEqual(his["vitals"][0], {"name": "脉搏", "last": "103", "unit": "次/分",
                                            "date": "2026-10-02"})
        self.assertEqual(his["exams"][0]["item"], "CT下腹部平扫")
        self.assertIn("阑尾炎", his["exams"][0]["impression"])
        self.assertEqual(his["abnormal_labs"][0]["item"], "白细胞")
        self.assertEqual(his["abnormal_labs"][0]["ref"], "3.5--9.5")
        self.assertEqual(his["orders"][0]["text"], "静脉输液")


class TestDeidAndRedline(unittest.TestCase):
    FACTS = {"patient_names": ["张三丰"], "patient_ids": ["2099000001"]}
    STAFF = {"李四海": "医生", "王五": "护士"}

    def test_deid_text(self):
        out = ci.deid_text("张三丰由李四海诊治，王五执行", self.FACTS, self.STAFF, "患者F")
        self.assertEqual(out, "患者F由李医生诊治，王护士执行")

    def test_redline_hits_raw(self):
        hits = ci.redline_scan(json.dumps({"n": "张三丰"}, ensure_ascii=False),
                               self.FACTS, self.STAFF, "张三丰")
        self.assertIn("张三丰", hits)

    def test_redline_clean_after_deid(self):
        payload = json.dumps({"n": ci.deid_text("张三丰", self.FACTS, self.STAFF, "患者F"),
                              "doc": "李医生主刀"}, ensure_ascii=False)
        self.assertEqual(ci.redline_scan(payload, self.FACTS, self.STAFF, "张三丰"), [])

    def test_redline_phone_and_idcard(self):
        self.assertIn("手机号", ci.redline_scan("联系13812345678", self.FACTS, self.STAFF, "x"))
        self.assertIn("证件号", ci.redline_scan("11010119900101123X", self.FACTS, self.STAFF, "x"))

    def test_redline_staff_name_blocked(self):
        self.assertIn("李四海", ci.redline_scan("李四海主刀", self.FACTS, self.STAFF, "x"))


class TestSkeletonDocs(unittest.TestCase):
    def test_skeleton_clears_values_keeps_labels(self):
        tpl_adm = {"docs": [
            {"type": "入院记录", "title": "阑尾炎", "ts": "20260902", "fields": [
                {"label": "", "binding": "Patient", "dropdown": False, "value": "某真实名"},
                {"label": "", "binding": "Patient", "dropdown": False, "value": "男"},
                {"label": "", "binding": "Patient", "dropdown": False, "value": "23岁"},
                {"label": "", "binding": "Patient", "dropdown": False, "value": "10****56"},
                {"label": "主诉", "binding": "A01.03", "dropdown": False, "value": "右下腹痛1个月"},
                {"label": "体温", "binding": "field9", "dropdown": False, "value": "36.5"},
            ]},
            {"type": "病程记录", "title": "张医生查房记录", "ts": "20260903", "fields": []},   # 非首程病程→剔除
            {"type": "病程记录", "title": "首次病程记录", "ts": "20260902", "fields": [
                {"label": "病例特点", "binding": "x", "dropdown": False, "value": "旧值"}]},
            {"type": "用药记录", "title": "某药", "ts": "20260902", "fields": []},            # 非骨架类型→剔除
        ]}
        facts = {"admit_dt": datetime.datetime(2026, 10, 2, 6, 0),
                 "discharge_dt": datetime.datetime(2026, 10, 8, 9, 0),
                 "patient_names": ["某真实名"], "patient_ids": ["2099000001"],
                 "sex": "男", "age": "28岁"}
        docs = ci.build_skeleton_docs(tpl_adm, facts, "患者F")
        types = [(d["type"], d["title"]) for d in docs]
        self.assertIn(("入院记录", "阑尾炎"), types)
        self.assertIn(("病程记录", "首次病程记录"), types)
        self.assertNotIn(("用药记录", "某药"), types)
        self.assertNotIn(("病程记录", "张医生查房记录"), types)
        adm_rec = next(d for d in docs if d["type"] == "入院记录")
        vals = [f["value"] for f in adm_rec["fields"]]
        self.assertNotIn("右下腹痛1个月", vals)          # 模板值必须清空
        self.assertNotIn("36.5", vals)
        self.assertIn("患者F", vals)                      # 姓名位→代号
        self.assertIn("男", vals)                         # 性别回填本案例
        self.assertIn("28岁", vals)                       # 年龄回填本案例
        self.assertIn("20******01", vals)                 # 住院号掩码回填（10位→首2尾2，中间6星）
        self.assertEqual(adm_rec["ts"], "20261002")       # 时间锚定本案例
        first_course = next(d for d in docs if d["title"] == "首次病程记录")
        self.assertEqual(first_course["fields"][0]["value"], "")


class TestHeuristicRoles(unittest.TestCase):
    def test_question_goes_doctor_and_merge(self):
        segs = [{"text": "哪里不舒服?"}, {"text": "肚子疼"}, {"text": "疼几天了?"}]
        turns = ci.tag_roles_heuristic(segs)
        self.assertEqual([t["role"] for t in turns], ["医生", "患者", "医生"])
        segs2 = [{"text": "肚子疼"}, {"text": "还吐了"}]
        self.assertEqual(len(ci.tag_roles_heuristic(segs2)), 1)   # 同角色相邻合并


if __name__ == "__main__":
    unittest.main()
