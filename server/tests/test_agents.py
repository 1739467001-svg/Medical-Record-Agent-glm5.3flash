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


if __name__ == "__main__":
    unittest.main()
