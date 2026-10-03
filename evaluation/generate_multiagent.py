# -*- coding: utf-8 -*-
"""多智能体生成器（评测适配层）：与 generate_v0 同契约接入 evaluate_v0。
模式A（默认）：无录音——transcript 为空，仅 HIS 可推导字段（病史类字段 writer 硬拦截，不编造）。
模式B：--mode B 时给 extractor 喂模拟对话（demo/data/dialogue.json，按金标准反构）。
模式C（录音链路彩排，P2-O）：transcript 来自 ASR 转写稿文件——env
MULTIAGENT_MODE=C + REHEARSAL_TRANSCRIPT=<转写.txt 路径>（tools/asr_ingest.py 产物）。
真实录音到位后同一路径即插即跑（asr_ingest --transcribe → 本模式评测）。

P2-H 住院次限定：病历资料目录的 HIS 视图为患者级导出，多次住院的申请单诊断/检查/医嘱
混在一起，曾导致初步诊断跨住院次张冠李戴（评测 ×11 未命中的主因之一）。此处按文书名
日期（入院/首程=入院日，出院记录=出院日）做窗口过滤，只保留本次住院的数据行；
窗口无数据行时原样返回（降级安全，不掩盖数据缺失）。
"""
import os, sys, json, datetime

_AGENTS_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "server")
if _AGENTS_PATH not in sys.path:
    sys.path.insert(0, _AGENTS_PATH)
from agents import run_pipeline  # noqa: E402

_MODE = os.environ.get("MULTIAGENT_MODE", "A")
_DIALOGUE = None

# 各文书类型的住院窗口（天）：首程/入院记录以入院日为中心；出院记录覆盖整个住院过程
STAY_WINDOW_DAYS = {"入院记录": 3, "首次病程记录": 3, "出院记录": 14}
# HIS 视图中的日期列名（按优先级；实测列名见 检查申请主表/检验申请主表/生命体征）
DATE_COLS = ("EXAM_DATE_TIME", "REQ_DATE_TIME", "EXECUTE_DATE", "REQUESTED_DATE_TIME",
             "RESULTS_RPT_DATE_TIME", "REPORT_DATE_TIME", "TIME_POINT", "APPLY_DATE", "ORDER_DATE")

def _dialogue():
    global _DIALOGUE
    if _DIALOGUE is None:
        p = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "demo", "data", "dialogue.json")
        _DIALOGUE = json.load(open(p, encoding="utf-8")) if os.path.exists(p) else []
    return _DIALOGUE

def _row_date(row):
    for col in DATE_COLS:
        raw = row.get(col)
        if raw in (None, ""): continue
        try:
            return datetime.datetime(1899, 12, 30) + datetime.timedelta(days=float(raw))
        except (TypeError, ValueError):
            continue
    return None

def _filter_views_by_stay(views, stay_dt, doc_type):
    """按住院窗口过滤各视图行。区分两种情况：
    - 表内无任何可解析日期行（联接表如 检查申请项目/检查结果）：原样保留，交由主表 EXAM_NO 联接限定范围；
    - 表内有日期行但窗口内为空：清空（本次住院确无该类数据，不回填其他住院次的行）。"""
    if not stay_dt:
        return views
    win = STAY_WINDOW_DAYS.get(doc_type, 3)
    lo = stay_dt - datetime.timedelta(days=win)
    hi = stay_dt + datetime.timedelta(days=win)
    out = {}
    for name, rows in views.items():
        dated = [r for r in rows if _row_date(r)]
        if rows and not dated:
            out[name] = rows
            continue
        out[name] = [r for r in dated if lo <= _row_date(r) <= hi]
    return out

def generate(gold_fields, views, anchor="入院", doc_type="入院记录", stay_dt=None):
    labels = {f["label"] for f in gold_fields}
    transcript = None
    if _MODE == "B" and doc_type == "入院记录":
        transcript = _dialogue()
    elif _MODE == "C":
        p = os.environ.get("REHEARSAL_TRANSCRIPT", "")
        if p and os.path.exists(p):
            transcript = open(p, encoding="utf-8").read().strip()
        elif p:
            print(f"[mode-C] 转写稿不存在：{p}（降级为无录音模式）", file=sys.stderr)
    r = run_pipeline(transcript=transcript, his_views=_filter_views_by_stay(views, stay_dt, doc_type),
                     doc_type=doc_type, target_labels=labels)
    gen = {}
    for f in r["fields"]:
        gen[f["label"]] = f["value"]
    return gen
