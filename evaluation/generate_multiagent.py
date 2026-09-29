# -*- coding: utf-8 -*-
"""多智能体生成器（评测适配层）：与 generate_v0 同契约接入 evaluate_v0。
模式A（默认）：无录音——transcript 为空，仅 HIS 可推导字段。
模式B：--mode B 时给 extractor 喂模拟对话（demo/data/dialogue.json，按金标准反构）。
"""
import os, sys, json

_AGENTS_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "server")
if _AGENTS_PATH not in sys.path:
    sys.path.insert(0, _AGENTS_PATH)
from agents import run_pipeline  # noqa: E402

_MODE = os.environ.get("MULTIAGENT_MODE", "A")
_DIALOGUE = None

def _dialogue():
    global _DIALOGUE
    if _DIALOGUE is None:
        p = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "demo", "data", "dialogue.json")
        _DIALOGUE = json.load(open(p, encoding="utf-8")) if os.path.exists(p) else []
    return _DIALOGUE

def generate(gold_fields, views, anchor="入院", doc_type="入院记录"):
    labels = {f["label"] for f in gold_fields}
    transcript = _dialogue() if (_MODE == "B" and doc_type == "入院记录") else None
    r = run_pipeline(transcript=transcript, his_views=views, doc_type=doc_type, target_labels=labels)
    gen = {}
    for f in r["fields"]:
        gen[f["label"]] = f["value"]
    return gen
