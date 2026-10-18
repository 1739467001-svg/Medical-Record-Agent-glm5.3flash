# -*- coding: utf-8 -*-
"""语音增强 A/B 实验台（tools/asr_denoise_ab.py）纯函数层单测
——一致率口径（同 asr_reconcile：去标点+去语气词）与增强预设档的完整性与安全性。
不跑 ffmpeg、不加载模型。"""
import importlib.util, os, unittest

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_spec = importlib.util.spec_from_file_location(
    "asr_denoise_ab", os.path.join(REPO, "tools", "asr_denoise_ab.py"))
ab = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ab)


class TestAgreement(unittest.TestCase):
    def test_identical_is_one(self):
        self.assertEqual(ab.agree("你好世界", "你好世界"), 1.0)

    def test_punctuation_and_filler_ignored(self):
        """标点与语气词双侧同去，不影响一致率（否则忠实转写被伪分歧压低）。"""
        self.assertEqual(ab.agree("嗯，你好吧！世界。", "你好世界"), 1.0)

    def test_disjoint_is_zero(self):
        self.assertEqual(ab.agree("甲乙丙", "丁戊己"), 0.0)

    def test_empty_safe(self):
        """空文本不除零（噪声音频可能出现空转写）。"""
        self.assertEqual(ab.agree("", "你好"), 0.0)
        self.assertEqual(ab.agree("", ""), 0.0)

    def test_partial_between_zero_and_one(self):
        v = ab.agree("今天天气很好", "今天天气不错")
        self.assertGreater(v, 0.0)
        self.assertLess(v, 1.0)


class TestPresets(unittest.TestCase):
    def test_three_levels_present(self):
        self.assertEqual(set(ab.PRESETS), {"mild", "standard", "strong"})

    def test_all_include_highpass(self):
        """每档都先去低频轰鸣——门诊录音最常见的噪声成分。"""
        for name, chain in ab.PRESETS.items():
            self.assertIn("highpass", chain, name)

    def test_loudness_normalized_except_mild(self):
        """标准/强档做响度归一（EBU R128），轻档只做高通（不动动态）。"""
        self.assertIn("loudnorm", ab.PRESETS["standard"])
        self.assertIn("loudnorm", ab.PRESETS["strong"])
        self.assertNotIn("loudnorm", ab.PRESETS["mild"])

    def test_single_quote_free(self):
        """滤镜链不得含引号——会被拼进 subprocess 参数列表造成误解析。"""
        for chain in ab.PRESETS.values():
            self.assertNotIn("'", chain)
            self.assertNotIn('"', chain)


if __name__ == "__main__":
    unittest.main()
