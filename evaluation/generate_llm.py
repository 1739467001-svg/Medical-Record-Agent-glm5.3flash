# -*- coding: utf-8 -*-
"""
LLM 生成器 v1（PRD 4.3 多智能体核心的"病历生成智能体"评测实现）
================================================================
与 generate_v0 同契约：generate(gold_fields, views, anchor, doc_type) -> {label: value}

三个子生成器：
  A. 规则打底（复用 generate_v0）：生命体征、辅助检查初稿、诊断初稿
  B. LLM 归纳（DeepSeek，OpenAI 兼容接口）：
     - 诊断：申请单临床诊断片段 → 规范化编号诊断（去重/合并/去前缀）
     - 辅助检查：检查报告 + 异常检验 → 病历体"辅助检查结果"文本
     LLM 提示词强制"只依据给定材料，不得编造"；失败静默回退规则值。
  C. 模板常规（灰=规范所见）：体格检查非常规受控码字段按临床常规表述批量生成，
     须医生逐项核对（对应 PRD 四色中的灰色字段语义）。

数据安全：送 LLM 的内容仅医学数据（诊断名/检查项目/检验值/印象），已剔除
姓名、拼音、联系方式、证件与院内 ID 等列（研发态云端调用脱敏要求，PRD Q10）。
"""
import os, re, json, urllib.request

PE_DEFAULTS = {
    "发育": "正常", "营养": "中等", "神志": "清楚", "配合检查": "合作",
    "肝掌蜘蛛痣": "无肝掌、蜘蛛痣", "全身浅表淋巴结": "全身浅表淋巴结无肿大",
    "眼睑水肿": "无水肿", "结膜": "无充血、无苍白", "巩膜": "无黄染", "角膜": "正常",
    "瞳孔": "等大同圆", "对光反射": "正常", "齿龈": "无肿胀、无充血",
    "甲状腺异常": "无包块、血管杂音、压痛", "胸骨叩痛": "胸骨无叩痛",
    "呼吸运动": "正常", "呼吸规整": "规整", "心前区隆起": "无隆起", "心律": "齐",
    "心包摩擦音": "无心包摩擦音", "腹外形": "平坦", "腹壁静脉曲张": "无腹壁静脉曲张",
    "腹部紧张度": "柔软", "包块": "无包块", "肝脏": "肋下无触及", "肠鸣音": "正常",
    "直肠肛门": "未查", "肛门生殖器": "未查", "脊柱": "正常生理弯曲",
    "胸廓": "胸廓对称、无畸形", "四肢": "四肢关节活动无异常，双下肢无水肿",
}

# ---------------- LLM 调用（OpenAI 兼容） ----------------
def _llm_chat(messages, max_tokens=600, timeout=60):
    base = os.environ["LLM_API_BASE"].rstrip("/")
    req = urllib.request.Request(
        base + "/chat/completions",
        data=json.dumps({"model": os.environ.get("LLM_MODEL", "Deepseek-v4-flash"),
                         "messages": messages, "max_tokens": max_tokens, "temperature": 0.2}).encode(),
        headers={"Content-Type": "application/json",
                 "Authorization": "Bearer " + os.environ["LLM_API_KEY"]})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        data = json.loads(r.read().decode())
    return data["choices"][0]["message"]["content"].strip()

def llm_available():
    return bool(os.environ.get("LLM_API_BASE") and os.environ.get("LLM_API_KEY"))

def _sanitize_views(views):
    """剔除 PII 列，仅保留医学数据（研发态云端调用脱敏要求）。"""
    exams = []
    for req in views.get("检查申请主表", []):
        no = str(req.get("EXAM_NO"))
        for it in views.get("检查申请项目", []):
            if str(it.get("EXAM_NO")) == no:
                imp = next((str(r.get("IMPRESSION")).strip() for r in views.get("检查结果", [])
                            if str(r.get("EXAM_NO")) == no and str(r.get("IMPRESSION")).strip()), "")
                d = req.get("EXAM_DATE_TIME")
                import datetime as _dt
                ds = (_dt.datetime(1899, 12, 30) + _dt.timedelta(days=float(d))).strftime("%Y-%m-%d") if d else ""
                if imp:
                    exams.append(f"- {ds} {it.get('EXAM_ITEM')}：{imp[:120]}")
                break
    labs = []
    for r in views.get("检验结果", []):
        if str(r.get("ABNORMAL_INDICATOR")).strip():
            labs.append(f"- {r.get('REPORT_ITEM_NAME')} {r.get('RESULT')} {r.get('UNITS') or ''}（参考 {str(r.get('PRINT_CONTEXT'))[:20]}）")
    diags = []
    for req in views.get("检查申请主表", []) + views.get("检验申请主表", []):
        for key in ("CLIN_DIAG", "RELEVANT_CLINIC_DIAG"):
            v = str(req.get(key, "") if key in req else "").strip()
            if v and v not in diags and not v.startswith("常规"):
                diags.append(v)
    return {"exams": exams[:15], "abnormal_labs": labs[:20], "clinic_diags": diags[:8]}

def _llm_diagnose(diags):
    if not diags: return None
    prompt = ("以下是某患者住院期间检验/检查申请单上的临床诊断片段（可能重复、含前缀或口语）：\n"
              + "\n".join("- " + d for d in diags)
              + "\n\n请归纳为一份规范化诊断列表：合并同义项、去掉\"常规诊断：\"等前缀、按主次排序，"
                "输出格式为每行\"1.诊断名\"。只依据上述材料，不得新增任何诊断。只输出列表本身。")
    try:
        out = _llm_chat([{"role": "system", "content": "你是严谨的病案科医生，只做归纳改写，绝不编造。"},
                         {"role": "user", "content": prompt}])
        if re.search(r"\d+\.", out): return out
    except Exception:
        return None
    return None

def _llm_aux_report(exams, labs):
    if not exams and not labs: return None
    prompt = "以下是某患者的检查报告与异常检验结果：\n【检查报告】\n" + "\n".join(exams)
    if labs: prompt += "\n【异常检验】\n" + "\n".join(labs)
    prompt += ("\n\n请把它们整理为病历《辅助检查结果》栏的规范文本：每条一行，格式\"YYYY-MM-DD 项目：结论\"，"
               "检验结果合并为一行\"YYYY-MM-DD 检验：异常项=值(参考范围)；…\"。只依据给定材料，不得编造。只输出文本。")
    try:
        out = _llm_chat([{"role": "system", "content": "你是严谨的病案科医生，只做格式整理与归纳，绝不编造。"},
                         {"role": "user", "content": prompt}], max_tokens=800)
        if len(out) > 20: return out
    except Exception:
        return None
    return None

# ---------------- 主生成器（评测契约） ----------------
def generate(gold_fields, views, anchor="入院", doc_type="入院记录"):
    from evaluate_v0 import generate_v0
    gen = generate_v0(gold_fields, views, anchor)
    sanitized = _sanitize_views(views)
    if llm_available():
        d = _llm_diagnose(sanitized["clinic_diags"])
        if d:
            gen["初步诊断"] = d
            gen["出院诊断"] = d
        aux = _llm_aux_report(sanitized["exams"], sanitized["abnormal_labs"])
        if aux:
            gen["辅助检查结果"] = aux
            gen["辅助检查"] = aux
    # 模板常规（灰）：只填 gold 模板中存在的标签（标签=模板结构，非答案）
    gold_labels = {f["label"] for f in gold_fields}
    for label, default in PE_DEFAULTS.items():
        if label in gold_labels and label not in gen:
            gen[label] = default
    return gen
