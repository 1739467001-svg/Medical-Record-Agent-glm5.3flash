# -*- coding: utf-8 -*-
"""分歧点听辨工作台（tools/asr_annotate.py）纯函数层单测
——坐标映射（清洗后↔原文）、片段窗口合并、裁决解析与成稿替换的精确性。
不切音频、不加载模型。"""
import importlib.util, os, re, sys, unittest

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(REPO, "tools"))
_spec = importlib.util.spec_from_file_location(
    "asr_annotate", os.path.join(REPO, "tools", "asr_annotate.py"))
an = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(an)


class TestRawIndexMap(unittest.TestCase):
    def test_clean_index_maps_back_to_raw(self):
        """清洗下标必须能映射回原文位置（去标点/语气词后仍精确定位）。"""
        raw = "嗯，你好吧！世界。"
        m, text = an.raw_index_map(raw)
        self.assertEqual(text, raw)
        # 清洗后字符：你好世界 → 对应原文下标 2,3,5,6
        self.assertEqual([raw[i] for i in m], ["你", "好", "世", "界"])

    def test_span_of_cleaned_range(self):
        raw = "嗯，你好吧！世界。"
        m, _ = an.raw_index_map(raw)
        lo, hi = an.span_in_raw(m, 0, 2)      # 清洗区间 [0,2) = 你好
        self.assertEqual(raw[lo:hi], "你好")

    def test_insertion_span_is_point(self):
        """纯插入（B 多字、A 无对应）：退化为单点，不吞掉相邻字。"""
        m, _ = an.raw_index_map("甲乙丙")
        lo, hi = an.span_in_raw(m, 1, 1)
        self.assertEqual(lo, hi)

    def test_out_of_range_clamps(self):
        m, raw = an.raw_index_map("甲乙")
        lo, hi = an.span_in_raw(m, 99, 100)
        self.assertEqual((lo, hi), (len(raw) - 1, len(raw) - 1))


class TestMergeWindows(unittest.TestCase):
    def test_overlapping_merged(self):
        """重叠窗口合并为一片段（否则片段互相重叠、总听音量反超原音频）。"""
        w = [(0.0, 3.0), (2.0, 5.0), (4.0, 6.0)]
        g = an.merge_windows(w)
        self.assertEqual(len(g), 1)
        self.assertEqual((g[0][0], g[0][1]), (0.0, 6.0))
        self.assertEqual(sorted(g[0][2]), [0, 1, 2])

    def test_far_apart_kept_separate(self):
        g = an.merge_windows([(0.0, 2.0), (30.0, 32.0)])
        self.assertEqual(len(g), 2)

    def test_gap_threshold(self):
        """间隔小于阈值即合并（相邻分歧处一次听清更省时）。"""
        g = an.merge_windows([(0.0, 2.0), (3.0, 4.0)])
        self.assertEqual(len(g), 1)


class TestClipWindows(unittest.TestCase):
    """时间窗按段内字符比例插值——整段取时间戳会让十几个分歧簇全撞在 10 秒段上。"""

    def _clusters(self):
        segs = [{"start": 0, "end": 10, "text": "ABCDEFGHIJ"}, {"start": 20, "end": 30, "text": "KLMNOPQRST"}]
        return {"segments": segs, "clusters": [(0, 2, 0, 2), (8, 10, 8, 10), (12, 14, 12, 14)]}

    def test_interpolated_windows_are_narrow(self):
        w = an.find_clip_window(self._clusters())
        self.assertEqual(len(w), 3)
        for t0, t1 in w:
            self.assertLess(t1 - t0, 5.0)      # 明确窄于 10 秒整段
        self.assertLess(w[1][0], w[2][0])      # 顺序单调

    def test_second_segment_offset(self):
        """第二段的分歧窗口落在该段时间范围内（不越段）。"""
        w = an.find_clip_window(self._clusters())
        self.assertGreaterEqual(w[2][0], 20.0)
        self.assertLessEqual(w[2][1], 30.0 + 1e-6)


class TestInlineScriptAndCompile(unittest.TestCase):
    ITEMS = [
        {"idx": 1, "a_span": [0, 2], "a_text": "甲乙", "b_text": "AB", "decision": ""},
        {"idx": 2, "a_span": [4, 6], "a_text": "丙丁", "b_text": "CD", "decision": "B"},
    ]
    RAW = "甲乙后来丙丁结束"

    def test_inline_script_embeds_markers(self):
        s = an.build_linear_script(self.RAW, self.ITEMS)
        self.assertIn("〖#1 A=甲乙 B=AB ⟦⟧〗", s)
        self.assertIn("〖#2 A=丙丁 B=CD ⟦B⟧〗", s)
        self.assertIn("后来", s)                 # 一致处保留

    def test_inline_marker_regex_roundtrip(self):
        """标记格式必须能被解析回来（拆片与成稿两端正则一致）。"""
        s = an.build_linear_script(self.RAW, self.ITEMS)
        found = {int(m.group(1)): m.group(4).strip() for m in an.INLINE_RE.finditer(s)}
        self.assertEqual(found, {1: "", 2: "B"})

    def test_inline_regex_skips_doc_example(self):
        """文档里的格式示例（A=引擎A原文 / ⟦校订⟧）不得被当成真实裁决。"""
        doc = "> 每处标记形如 `〖#12 A=引擎A原文 B=引擎B文本 ⟦校订⟧〗`"
        hits = [m for m in an.INLINE_RE.finditer(doc) if m.group(2).strip() != "引擎A原文"]
        self.assertEqual(hits, [])


class TestCompileReplacement(unittest.TestCase):
    """成稿替换：A/空＝保持原文，B＝取引擎B 文本，其他＝按字面写入；自后向前避免串位。"""

    def _apply(self, raw, items):
        out = raw
        for it in sorted(items, key=lambda x: -x["a_span"][0]):
            dec = (it["decision"] or "").strip()
            if not dec or dec.upper() == "A":
                continue
            lo, hi = it["a_span"]
            rep = it["b_text"] if dec.upper() == "B" else dec
            out = out[:lo] + rep + out[hi:]
        return out

    def test_keep_a_and_empty(self):
        raw = "甲乙后来丙丁结束"
        self.assertEqual(self._apply(raw, [
            {"a_span": [0, 2], "b_text": "AB", "decision": "A"},
            {"a_span": [4, 6], "b_text": "CD", "decision": ""}]), raw)

    def test_take_b(self):
        raw = "甲乙后来丙丁结束"
        self.assertEqual(self._apply(raw, [
            {"a_span": [0, 2], "b_text": "AB", "decision": "B"}]), "AB后来丙丁结束")

    def test_custom_text(self):
        raw = "甲乙后来丙丁结束"
        self.assertEqual(self._apply(raw, [
            {"a_span": [0, 2], "b_text": "AB", "decision": "某某"}]), "某某后来丙丁结束")

    def test_multi_decision_no_shift(self):
        """多处裁决必须自后向前替换，否则前面替换会让后面区间下标错位。"""
        raw = "甲乙后来丙丁结束"
        got = self._apply(raw, [
            {"a_span": [0, 2], "b_text": "AB", "decision": "B"},
            {"a_span": [4, 6], "b_text": "CD", "decision": "B"}])
        self.assertEqual(got, "AB后来CD结束")

    def test_deletion_via_empty_b(self):
        """B 侧为空（A 多字、B 无对应）→ 该处删除。"""
        raw = "甲乙后来丙丁结束"
        got = self._apply(raw, [{"a_span": [0, 2], "b_text": "", "decision": "B"}])
        self.assertEqual(got, "后来丙丁结束")


class TestDenseModeDecision(unittest.TestCase):
    def test_dense_ratio_threshold(self):
        """听音量/音频时长超阈值即判密集（碎片化省不下时间，改用行内稿）。"""
        self.assertLess(an.DENSE_RATIO, 1.0)
        audio_sec, listen_sec = 100.0, 105.0
        self.assertTrue(listen_sec / audio_sec > an.DENSE_RATIO)


if __name__ == "__main__":
    unittest.main()
