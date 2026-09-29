# -*- coding: utf-8 -*-
"""信息抽取智能体（LLM，PRD 4.3.1）：转写稿 → 医学要素清单（带对话出处）。
无录音模式（transcript 为空）→ 返回空清单，流水线继续（只生成 HIS 可推导字段）。
"""
import time
from .common import llm_available, llm_json, sanitize, Element, trace_step

SECTIONS = ("主诉", "现病史", "既往史", "个人史", "婚育史", "家族史", "其他")

def _transcript_text(transcript):
    """接受两种形态：'医生：…\n患者：…' 文本 或 [{role,text}] 列表，统一为带轮次编号文本。"""
    if not transcript:
        return ""
    if isinstance(transcript, list):
        return "\n".join(f"[{i+1}] {t.get('role','?')}：{t.get('text','')}" for i, t in enumerate(transcript))
    return transcript

def extract(transcript, doc_type="入院记录"):
    """返回 (elements, trace)。LLM 不可用/解析失败 → 空要素 + warning（宁缺毋造）。"""
    t0 = time.time()
    text = _transcript_text(transcript)
    if not text.strip():
        return [], trace_step("extractor", 0, "无转写稿输入（无录音模式）——跳过，仅生成结构化可推导字段")
    if not llm_available():
        return [], trace_step("extractor", 0, "LLM 未配置，跳过抽取", status="skipped")
    messages = [
        {"role": "system", "content": "你是严谨的病历信息抽取器。只从对话中抽取明确说到的信息，绝不推断、绝不补全。输出纯 JSON。"},
        {"role": "user", "content": (
            f"以下是医生与患者的问诊对话转写稿（{doc_type}场景，[n]为轮次编号）：\n"
            f"{sanitize(text)[:6000]}\n\n"
            "抽取医学要素，输出 JSON：{\"elements\":[{\"section\":\"主诉|现病史|既往史|个人史|婚育史|家族史|其他\","
            "\"item\":\"要素名（如 主要症状/持续时间/过敏史/吸烟史…）\",\"text\":\"原文语义的规范短语\","
            "\"turns\":[出现的轮次编号],\"confidence\":0~1}]}\n"
            "要求：1) 患者原话中医学信息都要抽，医生问而患者答'无'的记为 text='无'+item；"
            "2) 方言语义转普通话；3) 不确定的 confidence≤0.5；4) 只输出 JSON。")},
    ]
    try:
        data = llm_json(messages, max_tokens=2000)
        elems = [Element(section=str(e.get("section", "其他")), item=str(e.get("item", "")),
                         text=str(e.get("text", "")),
                         turns=[int(t) for t in e.get("turns", []) if str(t).isdigit()],
                         confidence=float(e.get("confidence", 0.8)))
                for e in data.get("elements", []) if e.get("text")]
        warns = [f"低置信要素 {e['item']}" for e in elems if e["confidence"] < 0.5]
        return elems, trace_step("extractor", (time.time() - t0) * 1000,
                                 f"从 {text.count(chr(10)) + 1} 轮对话抽取 {len(elems)} 项医学要素（带轮次出处）", warns)
    except Exception as e:
        return [], trace_step("extractor", (time.time() - t0) * 1000, f"抽取失败已降级：{e}", status="degraded")
