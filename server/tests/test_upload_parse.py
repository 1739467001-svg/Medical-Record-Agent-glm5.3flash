# -*- coding: utf-8 -*-
"""上传入口文本解析（demo/js/app.js parseTranscriptText）回归测试
====================================================================
张老师 10.20 要求的上传入口：若雨的转写稿两种形态都要吃下去——
  对话体（"医生：…/患者：…"）按行分轮；结构化叙述体（主诉/现病史…）按段作为素材。
从 app.js 抽取函数本体执行（不依赖浏览器/DOM），node 直接跑：
  node demo/tests/upload_parse.test.js
任何用例失败退出码 1。check_repo.py [1] 会调用本脚本。"""
import os, subprocess, sys, unittest

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
TEST_JS = os.path.join(REPO, "demo", "tests", "upload_parse.test.js")

class TestUploadParse(unittest.TestCase):
    def test_js_parse_regression(self):
        if not os.path.exists(TEST_JS):
            self.fail(f"缺少测试脚本：{TEST_JS}")
        try:
            r = subprocess.run(["node", TEST_JS], cwd=REPO, capture_output=True, text=True, timeout=60)
        except FileNotFoundError:
            self.skipTest("本机无 node，跳过 JS 回归")
        self.assertEqual(r.returncode, 0, (r.stdout + r.stderr).strip()[:500])

if __name__ == "__main__":
    unittest.main()
