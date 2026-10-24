# -*- coding: utf-8 -*-
"""公共契约与 LLM 客户端（多智能体共享）。"""
import json, os, re, time, urllib.error, urllib.request

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

_RETRYABLE_HTTP = (429, 500, 502, 503, 504)

def _urlopen_retry(req, timeout, attempts=2):
    """网关偶发抖动重试（2026-10-24 qianwenaiapi 实测：SSL UNEXPECTED_EOF_WHILE_READING
    致 extractor 整段降级）。SSL/连接类错误与超时指数退避重试（1s/2s）；
    HTTPError 仅重试 429/5xx——4xx 是确定性错误，重试无意义。"""
    last = None
    for i in range(attempts):
        try:
            return urllib.request.urlopen(req, timeout=timeout)
        except urllib.error.HTTPError as e:
            last = e
            if e.code not in _RETRYABLE_HTTP:
                raise
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            last = e
        if i < attempts - 1:
            time.sleep(2 ** i)
    raise last

def llm_available():
    return bool(os.environ.get("LLM_API_BASE") and os.environ.get("LLM_API_KEY"))

def api_style():
    """LLM 网关协议风格：显式 LLM_API_STYLE 优先，否则按 base URL 关键词自动探测。
    Anthropic 风格 = POST {base}/v1/messages + x-api-key 头（qianwenaiapi 等
    Anthropic 兼容网关，实测 OpenAI 风格 /chat/completions 直接 Not support）；
    OpenAI 风格 = POST {base}/chat/completions + Bearer 头（DeepSeek 等）。"""
    explicit = (os.environ.get("LLM_API_STYLE") or "").strip().lower()
    if explicit in ("anthropic", "openai"):
        return explicit
    return "anthropic" if "anthropic" in os.environ.get("LLM_API_BASE", "").lower() else "openai"

def llm_chat(messages, max_tokens=800, timeout=90, temperature=0.2):
    return _llm_call(messages, max_tokens, timeout, temperature)[0]

def _llm_call(messages, max_tokens, timeout, temperature):
    """按当前协议风格调用 LLM，返回 (text, stop_reason|None)。
    stop_reason 用于上层识别输出截断（anthropic=max_tokens / openai=length）。"""
    if api_style() == "anthropic":
        return _llm_call_anthropic(messages, max_tokens, timeout, temperature)
    base = os.environ["LLM_API_BASE"].rstrip("/")
    body = json.dumps({"model": os.environ.get("LLM_MODEL", "Deepseek-v4-flash"),
                       "messages": messages, "max_tokens": max_tokens,
                       "temperature": temperature}).encode()
    req = urllib.request.Request(base + "/chat/completions", data=body,
                                 headers={"Content-Type": "application/json",
                                          "Authorization": "Bearer " + os.environ["LLM_API_KEY"]})
    with _urlopen_retry(req, timeout=timeout) as r:
        data = json.loads(r.read().decode())
    return (data["choices"][0]["message"]["content"].strip(),
            (data.get("choices") or [{}])[0].get("finish_reason"))

def _llm_call_anthropic(messages, max_tokens, timeout, temperature):
    base = os.environ["LLM_API_BASE"].rstrip("/")
    # Anthropic 协议：system 是顶层参数而非消息角色，必须提升；消息里只留 user/assistant
    system = "\n\n".join(m["content"] for m in messages if m.get("role") == "system")
    msgs = [{"role": m["role"], "content": m["content"]} for m in messages if m.get("role") != "system"]
    payload = {"model": os.environ.get("LLM_MODEL", "qwen-max"),
               "max_tokens": max_tokens, "messages": msgs}
    if system:
        payload["system"] = system
    if temperature is not None:
        payload["temperature"] = min(1.0, max(0.0, temperature))   # Anthropic 协议取值 0..1（OpenAI 是 0..2）
    req = urllib.request.Request(base + "/v1/messages", data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json",
                                          "x-api-key": os.environ["LLM_API_KEY"],
                                          "anthropic-version": "2023-06-01"})
    with _urlopen_retry(req, timeout=timeout) as r:
        data = json.loads(r.read().decode())
    text = "".join(b.get("text", "") for b in (data.get("content") or []) if b.get("type") == "text")
    if not text:
        raise RuntimeError(f"LLM 响应无文本块：{str(data)[:200]}")
    return text.strip(), data.get("stop_reason")

_JSON_RE = re.compile(r"\{.*\}|\[.*\]", re.S)

def llm_json(messages, max_tokens=1200, timeout=90, list_key=None, temperature=0.2):
    """要求 JSON 输出并健壮解析（容忍 markdown 代码围栏）。失败抛异常由调用方降级。
    list_key：qwen 等模型偶发把约定的对象返回成裸数组（如直接输出 [...]
    而非 {"fixes": [...]}，2026-10-24 公网实测致 extractor/QC 降级）——
    传 list_key 时裸数组自动包装为 {list_key: [...]}, 调用方无需分辨两种形态。
    输出截断自愈：stop_reason=max_tokens/length 时 JSON 必然不完整，
    自动翻倍输出预算重试一次（2026-10-24 实测 patient01 长对话在 2000 token 处截断）。"""
    def _parse(text):
        text = re.sub(r"^```(json)?|```$", "", text.strip(), flags=re.M).strip()
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            m = _JSON_RE.search(text)
            if not m:
                raise
            return json.loads(m.group(0))
    text, stop = _llm_call(messages, max_tokens, timeout, temperature)
    try:
        data = _parse(text)
    except Exception:
        if stop not in ("max_tokens", "length"):
            raise
        text, _ = _llm_call(messages, max_tokens=max_tokens * 2, timeout=timeout, temperature=temperature)
        data = _parse(text)
    if list_key and isinstance(data, list):
        data = {list_key: data}
    return data

_ICD_TAIL = ("，其他的", ",其他的", "，其他", ",其他")

def normalize_diag_text(value):
    """诊断规范用语改写（代码层，writer/QC 共用，评测实证 P2-H）：
    申请单诊断常带疑问号（"视神经脊髓炎？"）与 ICD 尾缀（"急性胃炎，其他的"），
    病历书写须规范表述——去"？"、去"其他的"尾缀。只删不改不增。"""
    v = str(value or "").strip().replace("？", "").replace("?", "")
    for tail in _ICD_TAIL:
        if v.endswith(tail):
            v = v[: -len(tail)]
    return v.strip()

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
