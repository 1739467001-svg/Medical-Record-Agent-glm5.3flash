# -*- coding: utf-8 -*-
"""mcp-server ASR 层单测：医疗热词后处理 / LocalWhisperEngine WAV 直读（PyAV 绕行修复回归保护）。"""
import os, sys, struct, tempfile, unittest, wave

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(REPO, "mcp-server"))
import asr_mcp_server as asr  # noqa: E402


def make_transcript(text, segs=None):
    return asr.Transcript("test-engine", "/tmp/x.wav", text, segs or [], warnings=[])


class TestHotwords(unittest.TestCase):
    def setUp(self):
        asr._HOTWORDS_CACHE = None   # 每用例重载词表

    def tearDown(self):
        asr._HOTWORDS_CACHE = None

    def test_basic_replacement_and_warning(self):
        t, applied = asr.correct_hotwords(make_transcript("篮尾盐，做了个篮尾分时"))
        self.assertIn("阑尾炎", t.text)
        self.assertIn("阑尾粪石", t.text)
        self.assertNotIn("篮尾", t.text)
        self.assertTrue(any("热词" in w for w in t.warnings))
        self.assertEqual(len(applied), 2)

    def test_longest_first(self):
        """短语规则优先于单字规则：篮尾分时 必须整体替换，不吃掉 篮尾→阑尾 后残留 分时。"""
        t, _ = asr.correct_hotwords(make_transcript("篮尾分时"))
        self.assertEqual(t.text, "阑尾粪石")

    def test_segments_sync(self):
        t, _ = asr.correct_hotwords(make_transcript(
            "篮尾言", [{"start": 0, "end": 1, "text": "篮尾言"}]))
        self.assertEqual(t.segments[0]["text"], "阑尾炎")

    def test_no_match_untouched(self):
        t, applied = asr.correct_hotwords(make_transcript("正常对话文本"))
        self.assertEqual(t.text, "正常对话文本")
        self.assertEqual(applied, [])
        self.assertEqual(t.warnings, [])

    def test_external_wordlist_env(self):
        tmp = tempfile.mkdtemp(prefix="mra_hot_")
        p = os.path.join(tmp, "w.json")
        import json
        json.dump({"规则（勿改本键）": {"副补": "腹部"}}, open(p, "w", encoding="utf-8"), ensure_ascii=False)
        os.environ["MRA_MEDICAL_HOTWORDS"] = p
        try:
            t, applied = asr.correct_hotwords(make_transcript("做了个副补B超"))
            self.assertEqual(t.text, "做了个腹部B超")
            self.assertEqual(len(applied), 1)
        finally:
            os.environ.pop("MRA_MEDICAL_HOTWORDS", None)


def write_wav(path, rate, channels, frames):
    with wave.open(path, "wb") as w:
        w.setnchannels(channels); w.setsampwidth(2); w.setframerate(rate)
        w.writeframes(frames)


class TestWavDirectRead(unittest.TestCase):
    """LocalWhisperEngine._wav_to_float32：WAV 标准库直读（PyAV 15+ 不兼容的绕行路径）。"""

    def test_16k_mono_passthrough(self):
        tmp = tempfile.mkdtemp(prefix="mra_wav_")
        p = os.path.join(tmp, "a.wav")
        frames = b"".join(struct.pack("<h", v) for v in (1000, -2000, 3000))
        write_wav(p, 16000, 1, frames)
        pcm, rate = asr.LocalWhisperEngine._wav_to_float32(p)
        self.assertEqual(rate, 16000)
        self.assertEqual(len(pcm), 3)
        self.assertAlmostEqual(pcm[0], 1000 / 32768.0, places=5)

    def test_stereo_and_rate_resampled(self):
        tmp = tempfile.mkdtemp(prefix="mra_wav_")
        p = os.path.join(tmp, "b.wav")
        # 8kHz 双声道 80 帧 → 混单+重采样到 16k ≈ 160 样本
        frames = b"".join(struct.pack("<hh", v, v) for v in (100, -100) * 40)
        write_wav(p, 8000, 2, frames)
        pcm, rate = asr.LocalWhisperEngine._wav_to_float32(p)
        self.assertEqual(rate, 16000)
        self.assertTrue(150 <= len(pcm) <= 170, f"重采样后样本数异常: {len(pcm)}")

    def test_non_wav_returns_none(self):
        tmp = tempfile.mkdtemp(prefix="mra_wav_")
        p = os.path.join(tmp, "c.bin")
        open(p, "wb").write(b"ID3 garbage not a wav" * 50)
        pcm, rate = asr.LocalWhisperEngine._wav_to_float32(p)
        self.assertIsNone(pcm)
        self.assertIsNone(rate)


class TestDialectVariants(unittest.TestCase):
    """患者F 首例真实方言录音沉淀：同音错字"变体族"修正 + 领域提示安全边界。"""

    def setUp(self):
        asr._HOTWORDS_CACHE = None
        asr._PROMPT_CACHE = None

    def tearDown(self):
        asr._HOTWORDS_CACHE = None
        asr._PROMPT_CACHE = None

    def test_xiaoyanzhen_variant_family(self):
        """消炎针变体族（实测 6 种误识形态）全部归一。"""
        for wrong in ("小院人", "小阳人", "小眼人", "小眼帘", "小眼脸", "小眼点"):
            t, applied = asr.correct_hotwords(make_transcript(f"打了个小{wrong[1:]}"))
            self.assertIn("消炎针", t.text, f"{wrong} 未修正")

    def test_tige_to_tige(self):
        t, _ = asr.correct_hotwords(make_transcript("平时提个挺好，踢个还行"))
        self.assertEqual(t.text, "平时体格挺好，体格还行")

    def test_medical_prompt_short_default(self):
        """默认领域提示必须是短主题式（实测长词表诱发 whisper 幻觉），且含关键锚定词。"""
        p = asr._medical_prompt()
        self.assertLessEqual(len(p), 220)
        self.assertIn("问诊", p)
        self.assertIn("消炎针", p)

    def test_medical_prompt_override_and_cache(self):
        os.environ["ASR_LOCAL_PROMPT"] = "自定义提示"
        try:
            self.assertEqual(asr._medical_prompt(), "自定义提示")
            self.assertEqual(asr._medical_prompt(), "自定义提示")  # 缓存命中不重算
        finally:
            os.environ.pop("ASR_LOCAL_PROMPT", None)
            asr._PROMPT_CACHE = None


if __name__ == "__main__":
    unittest.main()
