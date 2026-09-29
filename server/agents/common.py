# -*- coding: utf-8 -*-
"""公共契约与 LLM 客户端（多智能体共享）。"""
import json, os, re, urllib.request

SOURCE_BLUE, SOURCE_GREEN, SOURCE_GRAY = "blue", "green", "gray"   # 黄=缺失即不产生字段

class HisDataset(dict):
    """聚合智能体输出：标准化诊疗数据。
    keys: anchor_dt, vitals{名:值}, exams[{date,item,impression}],
          abnormal_labs[{item,result,units,ref}], orders[{text,dose,route}],
          clinic_diags[str], hospital_days
    """

class Element(dict):
    """抽取智能体输出：医学要素。keys: section, item, text, turns[list[int]], confidence"""

class DraftField(dict):
    """草稿字段。keys: label, value, source, basis, confidence"""

def llm_available():
    return bool(os.environ.get("LLM_API_BASE") and os.environ.get("LLM_API_KEY"))

def llm_chat(messages, max_tokens=800, timeout=90, temperature=0.2):
    base = os.environ["LLM_API_BASE"].rstrip("/")
    body = json.dumps({"model": os.environ.get("LLM_MODEL", "Deepseek-v4-flash"),
                       "messages": messages, "max_tokens": max_tokens,
                       "temperature": temperature}).encode()
    req = urllib.request.Request(base + "/chat/completions", data=body,
                                 headers={"Content-Type": "application/json",
                                          "Authorization": "Bearer " + os.environ["LLM_API_KEY"]})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        data = json.loads(r.read().decode())
    return data["choices"][0]["message"]["content"].strip()

_JSON_RE = re.compile(r"\{.*\}|\[.*\]", re.S)

def llm_json(messages, max_tokens=1200, timeout=90):
    """要求 JSON 输出并健壮解析（容忍 markdown 代码围栏）。失败抛异常由调用方降级。"""
    out = llm_chat(messages, max_tokens=max_tokens, timeout=timeout)
    out = re.sub(r"^```(json)?|```$", "", out.strip(), flags=re.M).strip()
    try:
        return json.loads(out)
    except json.JSONDecodeError:
        m = _JSON_RE.search(out)
        if m:
            return json.loads(m.group(0))
        raise

def sanitize(text):
    """送 LLM 前兜底脱敏：手机号/身份证/连续长数字（研发态云端合规，PRD Q10）。"""
    t = str(text)
    t = re.sub(r"1[3-9]\d{9}", "1**********", t)
    t = re.sub(r"\d{17}[0-9Xx]", "（证件号）", t)
    t = re.sub(r"\d{9,}", "（编号）", t)
    return t

def trace_step(name, ms, summary, warnings=None, status="ok"):
    return {"agent": name, "status": status, "ms": round(ms), "summary": summary,
            "warnings": warnings or []}
