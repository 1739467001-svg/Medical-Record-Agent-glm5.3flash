# -*- coding: utf-8 -*-
"""LLM 客户端协议适配单测（agents/common.py）
——Anthropic 兼容网关（qianwenaiapi 等）与 OpenAI 风格双协议：
system 角色提升为顶层参数、温度 0..1 截断、content 文本块拼接、
风格自动探测（base URL 含 anthropic）与显式 LLM_API_STYLE 覆盖。
全程 mock urlopen，不联网。"""
import json, os, sys, unittest, urllib.error
from unittest import mock

SERVER_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, SERVER_DIR)

from agents import common  # noqa: E402

ENV_KEYS = ("LLM_API_BASE", "LLM_API_KEY", "LLM_MODEL", "LLM_API_STYLE")


def _fake_resp(payload):
    class _R:
        def __init__(self):
            self._b = json.dumps(payload).encode()
        def read(self):
            return self._b
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False
    return _R()


class TestApiStyle(unittest.TestCase):
    def tearDown(self):
        for k in ENV_KEYS:
            os.environ.pop(k, None)

    def test_auto_detect_anthropic_from_url(self):
        os.environ["LLM_API_BASE"] = "https://gw.example.com/apps/anthropic"
        self.assertEqual(common.api_style(), "anthropic")

    def test_auto_detect_openai(self):
        os.environ["LLM_API_BASE"] = "https://api.deepseek.com/v1"
        self.assertEqual(common.api_style(), "openai")

    def test_explicit_style_wins_over_url(self):
        os.environ["LLM_API_BASE"] = "https://gw.example.com/apps/anthropic"
        os.environ["LLM_API_STYLE"] = "openai"
        self.assertEqual(common.api_style(), "openai")


class TestAnthropicChat(unittest.TestCase):
    def setUp(self):
        os.environ["LLM_API_BASE"] = "https://gw.example.com/apps/anthropic"
        os.environ["LLM_API_KEY"] = "test-key"
        os.environ["LLM_MODEL"] = "qwen-max"

    def tearDown(self):
        for k in ENV_KEYS:
            os.environ.pop(k, None)

    def _capture(self, payload):
        captured = {}

        def fake_urlopen(req, timeout=None):
            captured["url"] = req.full_url
            captured["headers"] = {k.lower(): v for k, v in req.header_items()}
            captured["body"] = json.loads(req.data.decode())
            return _fake_resp(payload)

        return captured, fake_urlopen

    def test_system_hoisted_headers_and_endpoint(self):
        captured, fake = self._capture({"content": [{"type": "text", "text": "你好"}]})
        with mock.patch("urllib.request.urlopen", fake):
            out = common.llm_chat([{"role": "system", "content": "你是医生"},
                                   {"role": "user", "content": "你好"}], max_tokens=100, temperature=0.2)
        self.assertEqual(out, "你好")
        self.assertTrue(captured["url"].endswith("/v1/messages"))
        self.assertEqual(captured["body"]["system"], "你是医生")
        self.assertEqual(captured["body"]["messages"], [{"role": "user", "content": "你好"}])
        self.assertEqual(captured["body"]["model"], "qwen-max")
        self.assertEqual(captured["body"]["max_tokens"], 100)
        self.assertEqual(captured["headers"]["x-api-key"], "test-key")
        self.assertIn("anthropic-version", captured["headers"])

    def test_no_system_message_omits_system_param(self):
        captured, fake = self._capture({"content": [{"type": "text", "text": "ok"}]})
        with mock.patch("urllib.request.urlopen", fake):
            common.llm_chat([{"role": "user", "content": "x"}], max_tokens=10)
        self.assertNotIn("system", captured["body"])

    def test_temperature_clamped_to_anthropic_range(self):
        captured, fake = self._capture({"content": [{"type": "text", "text": "ok"}]})
        with mock.patch("urllib.request.urlopen", fake):
            common.llm_chat([{"role": "user", "content": "x"}], max_tokens=10, temperature=1.5)
        self.assertEqual(captured["body"]["temperature"], 1.0)

    def test_multi_text_blocks_joined_non_text_skipped(self):
        payload = {"content": [{"type": "text", "text": "A"},
                               {"type": "tool_use", "id": "x"},
                               {"type": "text", "text": "B"}]}
        _, fake = self._capture(payload)
        with mock.patch("urllib.request.urlopen", fake):
            out = common.llm_chat([{"role": "user", "content": "x"}], max_tokens=10)
        self.assertEqual(out, "AB")

    def test_empty_content_raises(self):
        _, fake = self._capture({"content": []})
        with mock.patch("urllib.request.urlopen", fake):
            with self.assertRaises(RuntimeError):
                common.llm_chat([{"role": "user", "content": "x"}], max_tokens=10)


class TestOpenAiChatRegression(unittest.TestCase):
    """OpenAI 风格既有行为回归：/chat/completions + Bearer + choices 解析。"""

    def test_openai_style_untouched(self):
        os.environ["LLM_API_BASE"] = "https://api.deepseek.com/v1"
        os.environ["LLM_API_KEY"] = "k"
        os.environ.pop("LLM_API_STYLE", None)
        captured = {}

        def fake_urlopen(req, timeout=None):
            captured["url"] = req.full_url
            captured["headers"] = {k.lower(): v for k, v in req.header_items()}
            captured["body"] = json.loads(req.data.decode())
            return _fake_resp({"choices": [{"message": {"content": " hi "}}]})

        with mock.patch("urllib.request.urlopen", fake_urlopen):
            out = common.llm_chat([{"role": "user", "content": "x"}], max_tokens=10, temperature=0.5)
        self.assertEqual(out, "hi")
        self.assertTrue(captured["url"].endswith("/chat/completions"))
        self.assertEqual(captured["headers"]["authorization"], "Bearer k")
        self.assertEqual(captured["body"]["temperature"], 0.5)


if __name__ == "__main__":
    unittest.main()


class TestLlmJsonListKey(unittest.TestCase):
    """qwen 偶发把约定对象返回成裸数组（2026-10-24 公网实测致 extractor/QC
    降级：'list' object has no attribute 'get'）——list_key 归一。"""

    def tearDown(self):
        for k in ENV_KEYS:
            os.environ.pop(k, None)

    def _run(self, llm_out, list_key):
        os.environ["LLM_API_BASE"] = "https://gw.example.com/apps/anthropic"
        os.environ["LLM_API_KEY"] = "k"
        with mock.patch.object(common, "_llm_call", return_value=(llm_out, "end_turn")):
            return common.llm_json([{"role": "user", "content": "x"}], list_key=list_key)

    def test_bare_list_wrapped(self):
        self.assertEqual(self._run('[{"a":1},{"a":2}]', "fixes"), {"fixes": [{"a": 1}, {"a": 2}]})

    def test_bare_empty_list_wrapped(self):
        self.assertEqual(self._run("[]", "elements"), {"elements": []})

    def test_dict_response_untouched(self):
        self.assertEqual(self._run('{"fixes":[]}', "fixes"), {"fixes": []})

    def test_no_list_key_keeps_list(self):
        self.assertEqual(self._run("[1,2]", None), [1, 2])

    def test_fenced_bare_list_wrapped(self):
        self.assertEqual(self._run('```json\n[{"a":1}]\n```', "fields"), {"fields": [{"a": 1}]})


class TestUrlopenRetry(unittest.TestCase):
    """网关偶发抖动重试：SSL EOF/超时指数退避重试；HTTP 4xx 不重试、429/5xx 重试。"""

    def setUp(self):
        os.environ["LLM_API_BASE"] = "https://gw.example.com/apps/anthropic"
        os.environ["LLM_API_KEY"] = "k"
        os.environ["LLM_MODEL"] = "qwen-max"

    def tearDown(self):
        for k in ENV_KEYS:
            os.environ.pop(k, None)

    def test_ssl_eof_then_success(self):
        calls = {"n": 0}
        def flaky(req, timeout=None):
            calls["n"] += 1
            if calls["n"] == 1:
                raise urllib.error.URLError("[SSL: UNEXPECTED_EOF_WHILE_READING] unexpected eof")
            return _fake_resp({"content": [{"type": "text", "text": "好"}]})
        with mock.patch("urllib.request.urlopen", flaky), mock.patch("time.sleep"):
            out = common.llm_chat([{"role": "user", "content": "x"}], max_tokens=10)
        self.assertEqual(out, "好")
        self.assertEqual(calls["n"], 2)

    def test_4xx_no_retry(self):
        calls = {"n": 0}
        def denied(req, timeout=None):
            calls["n"] += 1
            raise urllib.error.HTTPError(req.full_url, 401, "Unauthorized", {}, None)
        with mock.patch("urllib.request.urlopen", denied), mock.patch("time.sleep"):
            with self.assertRaises(urllib.error.HTTPError):
                common.llm_chat([{"role": "user", "content": "x"}], max_tokens=10)
        self.assertEqual(calls["n"], 1)

    def test_429_retried_then_success(self):
        calls = {"n": 0}
        def limited(req, timeout=None):
            calls["n"] += 1
            if calls["n"] == 1:
                raise urllib.error.HTTPError(req.full_url, 429, "rate", {}, None)
            return _fake_resp({"content": [{"type": "text", "text": "ok"}]})
        with mock.patch("urllib.request.urlopen", limited), mock.patch("time.sleep"):
            out = common.llm_chat([{"role": "user", "content": "x"}], max_tokens=10)
        self.assertEqual(out, "ok")
        self.assertEqual(calls["n"], 2)

    def test_all_attempts_exhausted(self):
        def always_eof(req, timeout=None):
            raise urllib.error.URLError("[SSL] eof")
        with mock.patch("urllib.request.urlopen", always_eof), mock.patch("time.sleep"):
            with self.assertRaises(urllib.error.URLError):
                common.llm_chat([{"role": "user", "content": "x"}], max_tokens=10)


class TestLlmJsonTruncationRetry(unittest.TestCase):
    """长对话抽取在 max_tokens 处被截断 → JSON 不完整；llm_json 检测
    stop_reason=max_tokens/length 自动翻倍预算重试一次（2026-10-24 patient01 实测）。"""

    def tearDown(self):
        for k in ENV_KEYS:
            os.environ.pop(k, None)

    def test_truncated_json_retries_with_double_budget(self):
        os.environ["LLM_API_BASE"] = "https://gw.example.com/apps/anthropic"
        os.environ["LLM_API_KEY"] = "k"
        calls = []

        def fake_call(messages, max_tokens, timeout, temperature):
            calls.append(max_tokens)
            if max_tokens == 2000:
                return '{"elements": [{"item": "腹痛"', "max_tokens"   # 截断的 JSON
            return '{"elements": [{"item": "腹痛"}, {"item": "发热"}]}', "end_turn"

        with mock.patch.object(common, "_llm_call", fake_call):
            data = common.llm_json([{"role": "user", "content": "x"}], max_tokens=2000, list_key="elements")
        self.assertEqual(data, {"elements": [{"item": "腹痛"}, {"item": "发热"}]})
        self.assertEqual(calls, [2000, 4000])

    def test_parse_error_without_truncation_raises(self):
        os.environ["LLM_API_BASE"] = "https://gw.example.com/apps/anthropic"
        os.environ["LLM_API_KEY"] = "k"
        def fake_call(messages, max_tokens, timeout, temperature):
            return "完全不是 JSON 的回复", "end_turn"
        with mock.patch.object(common, "_llm_call", fake_call):
            with self.assertRaises(Exception):
                common.llm_json([{"role": "user", "content": "x"}], max_tokens=100)


class TestDeidScan(unittest.TestCase):
    """脱敏校验（作品集/合规演示）：演示数据红线模式扫描——手机号/证件号/长数字标识零命中。"""

    def setUp(self):
        import importlib.util, tempfile
        spec = importlib.util.spec_from_file_location(
            "llm_api", os.path.join(SERVER_DIR, "llm_api.py"))
        self.mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.mod)
        self.tmp = tempfile.mkdtemp()

    def _write(self, name, text):
        with open(os.path.join(self.tmp, name), "w", encoding="utf-8") as f:
            f.write(text)

    def test_clean_data(self):
        self._write("patients.json", '[{"code":"DA0001","display":"患者A"}]')
        self._write("t.md", "# 问诊记录\n医生：你好")
        r = self.mod.deid_scan(self.tmp)
        self.assertTrue(r["clean"])
        self.assertEqual(len(r["files"]), 2)
        self.assertEqual(r["hits"], [])

    def test_phone_detected_and_masked(self):
        self._write("a.json", '{"note":"联系 13812345678"}')
        r = self.mod.deid_scan(self.tmp)
        self.assertFalse(r["clean"])
        self.assertEqual(r["hits"][0]["pattern"], "手机号")
        self.assertNotIn("13812345678", r["hits"][0]["sample"])  # 样本掩码

    def test_id_card_detected(self):
        self._write("a.md", "证件 110101199003077758")
        r = self.mod.deid_scan(self.tmp)
        self.assertFalse(r["clean"])
        self.assertEqual(r["hits"][0]["pattern"], "身份证号")

    def test_long_digit_id_detected(self):
        self._write("a.json", '{"PATIENT_ID":"2001028657"}')
        r = self.mod.deid_scan(self.tmp)
        self.assertFalse(r["clean"])
        self.assertTrue(any("长数字" in h["pattern"] for h in r["hits"]))

    def test_non_data_exts_skipped(self):
        self._write("note.txt", "手机 13812345678 不应被扫描")
        r = self.mod.deid_scan(self.tmp)
        self.assertTrue(r["clean"])
        self.assertEqual(r["files"], [])
