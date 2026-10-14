# -*- coding: utf-8 -*-
"""agents 单元测试（规范用语改写 / 无素材拦截 / 诊断规则直填 / QC 回流）。
运行：cd server && python3 -m unittest discover -s tests -v
零第三方依赖（unittest + 标准库），研发机与生产 alpine 容器均可跑。
"""
import os, sys, unittest

SERVER_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, SERVER_DIR)

# 测试进程禁用 LLM（默认走规则路径；LLM 行为用 monkeypatch 单独测）
os.environ.pop("LLM_API_BASE", None)
os.environ.pop("LLM_API_KEY", None)

from agents.common import normalize_diag_text
from agents import writer, qc


class TestNormalizeDiagText(unittest.TestCase):
    """P2-H 规范用语归一：只删不改不增。"""

    def test_strips_question_mark(self):
        self.assertEqual(normalize_diag_text("视神经脊髓炎？"), "视神经脊髓炎")
        self.assertEqual(normalize_diag_text("急性阑尾炎?"), "急性阑尾炎")

    def test_strips_icd_tail(self):
        self.assertEqual(normalize_diag_text("急性胃炎，其他的"), "急性胃炎")
        self.assertEqual(normalize_diag_text("膝关节病,其他"), "膝关节病")

    def test_keeps_normal_text(self):
        self.assertEqual(normalize_diag_text("急性化脓性阑尾炎"), "急性化脓性阑尾炎")
        self.assertEqual(normalize_diag_text(""), "")

    def test_does_not_touch_middle_text(self):
        # "其他的"在中间不动（只处理 ICD 尾缀）
        self.assertEqual(normalize_diag_text("其他的检查"), "其他的检查")


class TestRuleDiagnosis(unittest.TestCase):
    """writer._rule_diagnosis：申请单诊断 → 规范改写 → 编号列表直填。"""

    def _dataset(self, diags):
        return {"clinic_diags": diags, "vitals": {}, "exams": [], "abnormal_labs": [], "orders": []}

    def test_numbered_list_with_normalization(self):
        f = writer._rule_diagnosis(self._dataset(["急性阑尾炎？", "急性胃炎，其他的", "急性阑尾炎？"]), None, "入院记录")
        self.assertEqual(f["label"], "初步诊断")
        self.assertEqual(f["value"], "1.急性阑尾炎\n2.急性胃炎")  # 去疑去尾缀 + 去重

    def test_label_adapts_to_doc_type_and_template(self):
        f = writer._rule_diagnosis(self._dataset(["冠心病"]), {"出院诊断"}, "出院记录")
        self.assertEqual(f["label"], "出院诊断")
        # 模板只认"初步诊断"时回退
        f2 = writer._rule_diagnosis(self._dataset(["冠心病"]), {"初步诊断"}, "出院记录")
        self.assertEqual(f2["label"], "初步诊断")

    def test_no_diags_returns_none(self):
        self.assertIsNone(writer._rule_diagnosis(self._dataset([]), None, "入院记录"))

    def test_template_without_any_diag_label_returns_none(self):
        self.assertIsNone(writer._rule_diagnosis(self._dataset(["冠心病"]), {"主诉", "现病史"}, "入院记录"))


class TestWriterNoTranscript(unittest.TestCase):
    """无录音模式铁律：病史类字段不产出（LLM 不可用时由规则路径保证）。"""

    def _dataset(self):
        return {"clinic_diags": ["慢性阑尾炎急性发作"], "vitals": {"体温": "36.5", "脉搏": "78"},
                "exams": [], "abnormal_labs": [], "orders": []}

    def test_no_history_fields_without_elements(self):
        draft, tr = writer.generate([], self._dataset(), [], doc_type="入院记录", target_labels=None)
        labels = [d["label"] for d in draft]
        for h in writer.HISTORY_LABELS + writer.REASONING_LABELS:
            self.assertNotIn(h, labels, f"无素材时不得生成 {h}")
        # 规则诊断直填存在且规范
        dx = next(d for d in draft if d["label"] == "初步诊断")
        self.assertIn("1.慢性阑尾炎急性发作", dx["value"])
        # 蓝：HIS 体征直填
        self.assertIn("体温", labels)

    def test_llm_history_output_blocked_without_elements(self):
        """LLM 可用但无对话素材：LLM 幻觉出的主诉/诊断依据必须被硬拦截（monkeypatch 模拟）。"""
        saved_avail, saved_json = writer.llm_available, writer.llm_json
        writer.llm_available = lambda: True
        writer.llm_json = lambda messages, **kw: {"fields": [
            {"label": "主诉", "value": "编造的腹痛2天", "source": "green", "basis": "", "confidence": 0.9},
            {"label": "诊断依据", "value": "编造的依据链", "source": "green", "basis": "", "confidence": 0.9},
            {"label": "初步诊断", "value": "1.阑尾炎", "source": "green", "basis": "", "confidence": 0.9},
        ]}
        try:
            draft, tr = writer.generate([], self._dataset(), [], doc_type="入院记录", target_labels=None)
        finally:
            writer.llm_available, writer.llm_json = saved_avail, saved_json
        labels = [d["label"] for d in draft]
        self.assertNotIn("主诉", labels)
        self.assertNotIn("诊断依据", labels)
        self.assertIn("初步诊断", labels)   # 诊断允许（申请单规范改写域）
        self.assertTrue(any("硬拦截" in w for w in tr["warnings"]), tr["warnings"])


class TestQcNormalizeBackflow(unittest.TestCase):
    """QC 规范用语回流：诊断含？/ICD 尾缀 → 修正写回字段 + warnings 记录。"""

    def _ds(self):
        return {"vitals": {}, "clinic_diags": ["视神经脊髓炎？"]}

    def test_dx_normalized_in_place(self):
        f = {"label": "初步诊断", "value": "1.视神经脊髓炎？\n2.急性胃炎，其他的", "source": "green", "basis": "", "confidence": 0.8}
        passed, tr = qc.qc([f], self._ds(), doc_type="入院记录")
        out = next(x for x in passed if x["label"] == "初步诊断")
        self.assertEqual(out["value"], "1.视神经脊髓炎\n2.急性胃炎")
        self.assertTrue(any("规范用语回流" in w for w in tr["report"]["warnings"]))

    def test_normal_dx_untouched(self):
        f = {"label": "初步诊断", "value": "1.急性阑尾炎", "source": "green", "basis": "", "confidence": 0.8}
        passed, tr = qc.qc([f], self._ds(), doc_type="入院记录")
        out = next(x for x in passed if x["label"] == "初步诊断")
        self.assertEqual(out["value"], "1.急性阑尾炎")
        self.assertFalse(any("规范用语回流" in w for w in tr["report"]["warnings"]))

    def test_code_layer_warnings_survive_without_llm(self):
        """代码层 warnings 不得被复核层重置（回归 bug 防线）。"""
        f = {"label": "初步诊断", "value": "1.脊髓炎？", "source": "green", "basis": "", "confidence": 0.8}
        passed, tr = qc.qc([f], self._ds(), doc_type="入院记录")
        self.assertTrue(any("规范用语回流" in w for w in tr["report"]["warnings"]))

    def test_vital_range_blocks_outlier(self):
        f = {"label": "体温", "value": "58", "source": "blue", "basis": "", "confidence": 0.9}
        passed, tr = qc.qc([f], {"vitals": {}}, doc_type="入院记录")
        self.assertFalse(any(x["label"] == "体温" for x in passed))
        self.assertTrue(any(b["label"] == "体温" for b in tr["report"]["blocked"]))


class TestDocCutoff(unittest.TestCase):
    """时间穿越防线（患者F 实证）：入院记录写于入院当时，术后数据（病理/复查/术后申请单）
    不得进入素材。DOC_CUTOFF 环境变量由评测层按文书锚点传入。"""

    D = staticmethod(lambda days: 46297.0 + days)  # 2026-10-02 基准的 Excel 序列号

    def _views(self):
        adm = self.D(0)
        return {
            "生命体征": [
                {"VITAL_SIGNS": "入院", "TIME_POINT": adm},
                {"VITAL_SIGNS": "脉搏", "VITAL_SIGNS_CVALUES": "103", "TIME_POINT": adm},
                {"VITAL_SIGNS": "脉搏", "VITAL_SIGNS_CVALUES": "88", "TIME_POINT": self.D(3)},   # 术后
            ],
            "检查申请主表": [
                {"EXAM_NO": "1", "EXAM_DATE_TIME": adm, "CLIN_DIAG": "阑尾炎", "EXAM_SUB_CLASS": "CT"},
                {"EXAM_NO": "2", "EXAM_DATE_TIME": self.D(4), "CLIN_DIAG": "阑尾炎术后"},          # 术后复查
            ],
            "检查申请项目": [{"EXAM_NO": "1", "EXAM_ITEM": "CT下腹部平扫"},
                             {"EXAM_NO": "2", "EXAM_ITEM": "彩超腹部检查"}],
            "检查结果": [
                {"EXAM_NO": "1", "IMPRESSION": "阑尾炎。"},
                {"EXAM_NO": "2", "IMPRESSION": "阑尾炎术后所见"},                                   # 术后复查报告
            ],
            "检验结果": [
                {"REPORT_ITEM_NAME": "白细胞", "RESULT": "19.15", "ABNORMAL_INDICATOR": "H",
                 "RESULT_DATE_TIME": adm},
                {"REPORT_ITEM_NAME": "降钙素原", "RESULT": "1.98", "ABNORMAL_INDICATOR": "H",
                 "RESULT_DATE_TIME": self.D(3)},                                                    # 术后复查值
            ],
            "医嘱": [
                {"ORDER_TEXT": "血液分析", "START_DATE_TIME": adm},
                {"ORDER_TEXT": "今日出院", "START_DATE_TIME": self.D(6)},                            # 出院日医嘱
            ],
        }

    def test_cutoff_excludes_post_op_data(self):
        from agents import aggregator
        os.environ["DOC_CUTOFF"] = "2026-10-03T00:00:00"   # 入院次日 0 点
        try:
            ds = aggregator.from_his_views(self._views())
        finally:
            os.environ.pop("DOC_CUTOFF", None)
        # 术后复查检查/报告被截断：只剩术前 CT
        self.assertEqual([e["item"] for e in ds["exams"]], ["CT下腹部平扫"])
        self.assertTrue(all("术后" not in e["impression"] for e in ds["exams"]))
        # 术后复查检验值被截断
        self.assertEqual([l["item"] for l in ds["abnormal_labs"]], ["白细胞"])
        # 术后申请单诊断不污染 clinic_diags
        self.assertEqual(ds["clinic_diags"], ["阑尾炎"])
        # 出院日医嘱被截断
        self.assertTrue(all(o["text"] != "今日出院" for o in ds["orders"]))
        # 体征仍在锚点 ±24h 窗口（不受截断影响）
        self.assertEqual(ds["vitals"].get("脉搏"), "103")

    def test_no_cutoff_keeps_all(self):
        from agents import aggregator
        os.environ.pop("DOC_CUTOFF", None)
        ds = aggregator.from_his_views(self._views())
        self.assertEqual(len(ds["exams"]), 2)
        self.assertEqual(len(ds["abnormal_labs"]), 2)
        self.assertIn("今日出院", [o["text"] for o in ds["orders"]])


if __name__ == "__main__":
    unittest.main()
