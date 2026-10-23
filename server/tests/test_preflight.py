# -*- coding: utf-8 -*-
"""演示前预检工具（tools/preflight.py）纯函数层单测
——LLM 失效事件判定（密钥 401/未配置/超时 → 降级）与 orchestrator.llm_ok_from_trace 同源。
不联网、不起服务。运行：cd server && python3 -m unittest discover -s tests"""
import importlib.util, os, sys, unittest

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_spec = importlib.util.spec_from_file_location("preflight", os.path.join(REPO, "tools", "preflight.py"))
pf = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(pf)


class TestEventsShowLlmFailure(unittest.TestCase):
    """公网密钥 401 失效时事件长这样：extractor 降级 + writer 告警——必须判失效。"""

    def test_extractor_degraded_is_failure(self):
        events = [{"event": "agent_done", "agent": "extractor", "status": "degraded",
                   "summary": "抽取失败已降级：HTTP Error 401: Unauthorized", "warnings": []}]
        self.assertTrue(pf.events_show_llm_failure(events))

    def test_writer_llm_warning_is_failure(self):
        events = [{"event": "agent_done", "agent": "extractor", "summary": "抽取 5 项", "warnings": []},
                  {"event": "agent_done", "agent": "writer", "summary": "生成 24 字段",
                   "warnings": ["LLM 生成失败已降级（仅规则字段）：HTTP Error 401"]}]
        self.assertTrue(pf.events_show_llm_failure(events))

    def test_writer_unconfigured_is_failure(self):
        events = [{"event": "agent_done", "agent": "writer", "summary": "生成 19 字段",
                   "warnings": ["LLM 未配置：仅输出规则字段"]}]
        self.assertTrue(pf.events_show_llm_failure(events))

    def test_healthy_events_are_not_failure(self):
        events = [{"event": "agent_done", "agent": "extractor", "summary": "抽取 8 项", "warnings": ["低置信要素 头痛"]},
                  {"event": "agent_done", "agent": "writer", "summary": "生成 30 字段", "warnings": []},
                  {"event": "final", "fields": [], "stats": {"llm_ok": True}}]
        self.assertFalse(pf.events_show_llm_failure(events))

    def test_normal_retriever_degradation_is_not_failure(self):
        # retriever 语料为空是正常降级（非 LLM 故障）——不能误报成密钥失效
        events = [{"event": "agent_done", "agent": "retriever", "status": "degraded",
                   "summary": "语料 0 条 → 仅规范要点", "warnings": []}]
        self.assertFalse(pf.events_show_llm_failure(events))

    def test_empty_events_are_not_failure(self):
        self.assertFalse(pf.events_show_llm_failure([]))


if __name__ == "__main__":
    unittest.main()
