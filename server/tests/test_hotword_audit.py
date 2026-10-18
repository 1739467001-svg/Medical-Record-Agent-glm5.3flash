# -*- coding: utf-8 -*-
"""热词规则全语料审计器（tools/hotword_audit.py）的纯函数层单测
——双向判据（漏放误伤 / 过度拦截）、白名单建议来源、待判读清单口径。
用合成数据直测，不读语料库、不依赖音频。"""
import importlib.util, os, unittest

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_spec = importlib.util.spec_from_file_location(
    "hotword_audit", os.path.join(REPO, "tools", "hotword_audit.py"))
ha = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ha)


class TestAuditClassification(unittest.TestCase):
    RULES = {"提个": "体格", "顿疼": "钝疼"}
    GUARDS = {"提个": ["给你提个"], "顿疼": []}

    def _audit(self, text, refs_text):
        refs = [{"file": "ref.txt", "text": refs_text}]
        return ha.audit_text(text, "case.json", self.RULES, self.GUARDS, refs)

    def test_guard_blocks_surgical_phrase(self):
        """白名单命中处不算命中生效（guarded=True）。"""
        hits = self._audit("等下给你提个早下手术", "体格检查")
        h = next(h for h in hits if h["wrong"] == "提个")
        self.assertTrue(h["guarded"])

    def test_miss_when_ref_keeps_wrong_form(self):
        """漏放误伤：规则生效了，但参照稿里保留着原错词形（说明本处是合法用法）。"""
        hits = self._audit("提个醒儿了", "先说提个醒儿")
        miss, _ = ha.classify(hits)
        self.assertTrue(any(h["wrong"] == "提个" for h in miss))

    def test_over_block_when_ref_has_correct_word(self):
        """过度拦截：被白名单拦下，但参照稿里是正确词（说明这里本该修正）。"""
        hits = self._audit("给你提个体格检查", "先做体格检查")
        _, over = ha.classify(hits)
        self.assertTrue(any(h["wrong"] == "提个" for h in over))

    def test_plain_apply_is_neither_risk(self):
        """无白名单规则正常生效且参照含正确词：既非漏放也非过度拦截。"""
        hits = self._audit("隐隐地顿疼", "一直钝疼")
        miss, over = ha.classify(hits)
        self.assertEqual((miss, over), ([], []))

    def test_suggest_only_from_miss(self):
        """白名单建议只来自误伤证据（参照保留错词形），不从正常修正点取候选。"""
        apply_hits = self._audit("提个检查正常", "体格检查正常")   # 参照已改成体格＝真错写
        self.assertEqual(ha.suggest_guards(apply_hits), {})
        miss_hits = self._audit("请你说提个醒", "他说提个醒")       # 参照保留提个＝合法搭配
        sug = ha.suggest_guards(miss_hits)
        self.assertIn("提个", sug)
        self.assertTrue(any("提个醒" in c for c in sug["提个"]))

    def test_review_list_skips_registered_rules(self):
        """已登记白名单的规则（含空数组＝已审无需保护）不进待判读清单。"""
        hits = self._audit("顿疼提个", "钝疼提个")
        review = ha.review_list(hits, self.GUARDS)
        self.assertNotIn("提个", review)
        self.assertNotIn("顿疼", review)  # 空数组已在表内，视为已审

    def test_review_list_flags_unregistered_short_rule(self):
        """未登记 + ≤2 字 + 有生效命中 → 进待判读清单并带上下文。"""
        rules = {"小指": "小针"}
        hits = ha.audit_text("打个小指", "case.json", rules, {}, [])
        review = ha.review_list(hits, {})
        self.assertIn("小指", review)
        self.assertTrue(review["小指"]["contexts"])

    def test_long_rules_never_flagged_for_review(self):
        """长规则（>2 字）不误伤倾向低，不进待判读清单。"""
        rules = {"从来没有通过": "从来没痛过"}
        hits = ha.audit_text("从来没有通过", "c.json", rules, {}, [])
        self.assertEqual(ha.review_list(hits, {}), {})


if __name__ == "__main__":
    unittest.main()
