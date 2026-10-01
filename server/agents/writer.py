# -*- coding: utf-8 -*-
"""病历生成智能体（LLM，PRD 4.3.1）：要素+数据集+知识 → 草稿字段（四色来源元数据）。
铁律：材料未提及一律不产出该字段（黄=留空），受控码字段（纯数字值域）绝不猜测。
"""
import time
from .common import llm_available, llm_json, sanitize, DraftField, trace_step

def _dataset_text(ds, doc_type):
    lines = [f"住院锚点：{ds.get('anchor_dt') or '未识别'}"]
    if ds.get("vitals"): lines.append("生命体征：" + "；".join(f"{k}={v}" for k, v in ds["vitals"].items()))
    if ds.get("exams"):
        lines.append("检查报告：")
        lines += [f"  - {e['date']} {e['item']}：{e['impression']}" for e in ds["exams"][:10]]
    if ds.get("abnormal_labs"):
        lines.append("异常检验：" + "；".join(
            f"{l['item']}={l['result']}{l['units']}(参考{l['ref'] or '无'})" for l in ds["abnormal_labs"][:15]))
    if ds.get("orders"):
        lines.append("医嘱（节选）：" + "；".join(o["text"] for o in ds["orders"][:15]))
    if ds.get("clinic_diags"):
        lines.append("申请单临床诊断线索：" + "；".join(ds["clinic_diags"][:6]))
    return "\n".join(lines)

def _elements_text(elements):
    if not elements: return "（无对话素材——无录音模式）"
    by = {}
    for e in elements:
        by.setdefault(e["section"], []).append(f"{e['item']}={e['text']}")
    return "\n".join(f"【{sec}】" + "；".join(v) for sec, v in by.items())

# 模板常规（灰）：writer 直接按规范所见产出，qc 复核
GRAY_DEFAULTS = {
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
VITAL_MAP = {"腋下体温": "体温", "体温": "体温", "脉搏": "脉搏", "呼吸": "呼吸",
             "血压": None, "收缩压": "收缩压", "舒张压": "舒张压"}

def generate(elements, dataset, knowledge, doc_type="入院记录", target_labels=None):
    """返回 (draft_fields, trace)。target_labels：模板字段标签集（来自字段地图，非答案）。
    LLM 只负责 4 类字段：主诉、现病史叙述、初步/出院诊断、辅助检查结果；
    其余蓝/灰由规则精准产出。"""
    t0 = time.time()
    draft = []
    warnings = []
    is_first_course = doc_type == "首次病程记录"
    # 首程两种形态：粗粒度段落模板（target_labels 含"病例特点"，如 demo 数据）→ 五小节分支；
    # 细粒度字段模板（真实金标准：老中青/主诉/…，55+ 字段）→ 与入院记录相同的字段还原路径
    para_mode = is_first_course and ((not target_labels) or ("病例特点" in target_labels))
    # ---- 蓝：HIS 直填（粗粒度段落首程：体征写入病例特点正文，不单独成字段） ----
    if not para_mode:
        vit = dataset.get("vitals", {})
        for src, dst in VITAL_MAP.items():
            if dst and src in vit and dst not in [d["label"] for d in draft]:
                if dst == "体温" or dst not in [d["label"] for d in draft]:
                    draft.append(DraftField(label=dst, value=str(vit[src]), source="blue",
                                            basis=f"HIS 生命体征（{src}）", confidence=0.95))
        if "血压" in vit and "/" in str(vit["血压"]):
            hi, _, lo = str(vit["血压"]).partition("/")
            draft += [DraftField(label="收缩压", value=hi.strip(), source="blue", basis="HIS 生命体征（血压）", confidence=0.95),
                      DraftField(label="舒张压", value=lo.strip(), source="blue", basis="HIS 生命体征（血压）", confidence=0.95)]
    # ---- 灰：模板常规（严格按 target_labels 注入，首程模板无体格检查项时自动为空） ----
    for label, value in GRAY_DEFAULTS.items():
        if target_labels is None or label in target_labels:
            draft.append(DraftField(label=label, value=value, source="gray",
                                    basis="模板常规所见（须医生核对）", confidence=0.8))
    # ---- 绿/蓝/灰：LLM 归纳生成 ----
    if llm_available():
        if para_mode:
            # 段落型首程（PRD 153）：固定五小节；鉴别诊断/诊疗计划为知识辅助（灰源），医生过目核定
            instruct = (
                "固定输出五个小节：病例特点（人口学概括+主诉现病史要点+查体要点+辅助检查摘要）；"
                "初步诊断（依据申请单临床诊断线索与检查印象归纳）；诊断依据（逐条对应病例特点）；"
                "鉴别诊断（2~3 个鉴别点，结合【写作规范要点】，source 用 gray）；"
                "诊疗计划（护理级别、检查安排、治疗措施，结合规范要点，source 用 gray）。\n"
                "输出 JSON：{\"fields\":[{\"label\":\"病例特点|初步诊断|诊断依据|鉴别诊断|诊疗计划\","
                "\"value\":\"完整段落\",\"source\":\"green|gray\",\"basis\":\"依据摘要\",\"confidence\":0~1}]}"
                " value 为完整病历正文段落（不是短语）。只输出 JSON。")
        else:
            instruct = (
                "从【对话要素】中逐一还原以下字段（每个要素必须被使用，不得遗漏可支撑的字段）："
                "主诉；现病史（按 发病情况→症状特点→伴随症状→诊治经过→一般情况 组织成段）；"
                "既往史（健康/疾病/手术外伤/过敏/接种逐项，答'无'的也写）；个人史；婚育史；家族史。\n"
                "再从【结构化数据】还原：初步诊断/出院诊断（依据申请单临床诊断线索与检查印象归纳）；辅助检查结果（按日期逐条整理）。\n"
                "输出 JSON：{\"fields\":[{\"label\":\"字段名\",\"value\":\"规范文本\",\"source\":\"green|blue\","
                "\"basis\":\"依据摘要（引用要素/数据来源）\",\"confidence\":0~1}]}\n"
                "要求：value 为完整病历正文表述（不是短语）；主诉=症状+持续时间；"
                "要素为'无'的子项按'无××'规范表述写入对应节；没有材料支撑的字段不输出。只输出 JSON。")
        messages = [
            {"role": "system", "content":
                "你是本院病历书写智能体。铁律：只依据给定的【对话要素】与【结构化数据】书写，材料未提及的字段一律不输出；"
                "受控下拉字段（值域为编号）绝不猜测。语气为本院病历规范用语。输出纯 JSON。"},
            {"role": "user", "content": (
                f"目标文书：{doc_type}\n\n【对话要素】\n{sanitize(_elements_text(elements))[:2500]}\n\n"
                f"【结构化数据】\n{sanitize(_dataset_text(dataset, doc_type))[:2200]}\n\n"
                f"【写作规范要点】\n" + "\n".join("- " + k for k in knowledge) + "\n\n" + instruct)},
        ]
        try:
            data = llm_json(messages, max_tokens=3500)
            for f in data.get("fields", []):
                label, value = str(f.get("label", "")).strip(), str(f.get("value", "")).strip()
                if not label or not value: continue
                if re_match_coded(value):  # LLM 幻觉出纯编号 → 拦截
                    warnings.append(f"writer 输出疑似受控码被拦截：{label}")
                    continue
                src = f.get("source")
                if src not in ("green", "blue", "gray"): src = "green"
                draft.append(DraftField(label=label, value=value[:2000],
                                        source=src,
                                        basis=str(f.get("basis", ""))[:120],
                                        confidence=float(f.get("confidence", 0.75))))
        except Exception as e:
            warnings.append(f"LLM 生成失败已降级（仅规则字段）：{e}")
    else:
        warnings.append("LLM 未配置：仅输出规则字段")
    seen, uniq = set(), []
    for d in draft:
        if d["label"] in seen: continue
        seen.add(d["label"]); uniq.append(d)
    return uniq, trace_step("writer", (time.time() - t0) * 1000,
                            f"生成 {len(uniq)} 个草稿字段（蓝{sum(1 for d in uniq if d['source']=='blue')}"
                            f"/绿{sum(1 for d in uniq if d['source']=='green')}"
                            f"/灰{sum(1 for d in uniq if d['source']=='gray')}）", warnings)

_CODED = None
def re_match_coded(value):
    global _CODED
    if _CODED is None:
        import re as _re
        _CODED = _re.compile(r"^\s*\d+(\.\d+)?\s*$")
    return bool(_CODED.match(str(value)))
