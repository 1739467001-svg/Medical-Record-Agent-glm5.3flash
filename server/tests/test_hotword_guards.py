# -*- coding: utf-8 -*-
"""热词白名单防线（风险#3）：高危 2 字短规则的误伤保护。
患者F 实测误伤句：『给你提个早下手术』被 提个→体格 误改为『体格早下手术』。"""
import importlib.util, os, sys, unittest

_spec = importlib.util.spec_from_file_location(
    "asr_mcp_server", os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                   "..", "mcp-server", "asr_mcp_server.py"))
asr = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(asr)


def _T(text):
    return asr.Transcript("test", "x.wav", text, [{"text": text}])


class TestHotwordGuards(unittest.TestCase):
    def test_surgical_phrase_not_corrupted(self):
        """误伤原句：手术语境的『给你提个早下手术』不动。"""
        t2, applied = asr.correct_hotwords(_T("等下给你提个早下手术，先做个提个检查"))
        self.assertEqual(t2.text, "等下给你提个早下手术，先做个体格检查")
        self.assertEqual(applied, ["提个→体格×1"])

    def test_genuine_dialect_still_fixed(self):
        """方言错写（=体格）照常修正。"""
        t2, applied = asr.correct_hotwords(_T("平时提个挺好，体个检查正常"))
        self.assertIn("体格挺好", t2.text)
        self.assertIn("体格检查", t2.text)

    def test_admin_context_protected(self):
        """行政语境『审核没通过』不改；医学语境『没通过』(=没痛过) 改。"""
        t2, applied = asr.correct_hotwords(_T("审核没通过，问你痛过吗没通过"))
        self.assertIn("审核没通过", t2.text)
        self.assertIn("没痛过", t2.text)
        self.assertEqual(applied, ["没通过→没痛过×1"])
    def test_guard_loader_skips_doc_key(self):
        """『高危短语白名单』的 _说明 键不得泄漏为规则。"""
        guards = asr._load_hotword_guards()
        self.assertNotIn("_说明", guards)
        self.assertIn("提个", guards)

    def test_unguarded_rules_unchanged(self):
        """无白名单的普通规则行为不变。"""
        t2, applied = asr.correct_hotwords(_T("恶信呕吐明显"))
        self.assertEqual(t2.text, "恶心呕吐明显")
        self.assertTrue(applied)


if __name__ == "__main__":
    unittest.main()
