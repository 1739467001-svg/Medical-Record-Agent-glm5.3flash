# -*- coding: utf-8 -*-
"""多智能体核心包（PRD 4.3）：六个真实智能体 + 编排器。

三个纯代码智能体（本不该用 LLM）：aggregator / retriever / mapper
三个 LLM 智能体（DeepSeek，OpenAI 兼容）：extractor / writer / qc

统一数据契约（common.py）：
  HisDataset —— 聚合后的标准化诊疗数据（双源适配：评测=HIS xls，服务端=patients.json）
  Element    —— 从对话/材料抽取的医学要素（带出处）
  DraftField —— 草稿字段（值 + 来源四色 + 依据 + 置信度）
  trace      —— 每个智能体的运行留痕（耗时/输入输出摘要/警告）
"""
from .orchestrator import run_pipeline  # noqa: F401
