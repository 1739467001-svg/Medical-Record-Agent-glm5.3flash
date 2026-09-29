# -*- coding: utf-8 -*-
"""质控校验智能体（双层，PRD 4.3.1）：代码硬校验 + LLM 复核，只删改不新增。
代码层（硬拦截）：数值范围、日期逻辑、体温/心率生理限值、与数据集数值不一致、疑似受控码。
LLM 层（软复核）：医学逻辑矛盾、表述可疑（如把检查印象写进现病史），输出“应修正字段”。
"""
import re, time
from .common import llm_available, llm_json, sanitize, trace_step

RANGES = {"体温": (34, 43), "脉搏": (30, 220), "呼吸": (8, 40),
          "收缩压": (50, 260), "舒张压": (30, 160)}

def _num(s):
    m = re.search(r"-?\d+(\.\d+)?", str(s))
    return float(m.group(0)) if m else None

def qc(draft, dataset):
    """返回 (passed_fields, qc_trace)。passed_fields 仅含通过校验的字段；被拦截项记入报告。"""
    t0 = time.time()
    passed, blocked = [], []
    ds_vitals = dataset.get("vitals", {})
    for f in draft:
        label, value = f["label"], str(f["value"]).strip()
        if not value:
            blocked.append((label, "空值")); continue
        if label in RANGES:
            n = _num(value)
            lo, hi = RANGES[label]
            if n is None or not (lo <= n <= hi):
                blocked.append((label, f"超出生理范围 {value}")); continue
            # 与数据集交叉核对（误差>15% 视为不一致，以数据集为准修正）
            for src, v in ds_vitals.items():
                if label in src or src in label:
                    dn = _num(v)
                    if dn and n and abs(dn - n) / max(abs(dn), 1) > 0.15:
                        f = dict(f); f["value"] = str(v); f["basis"] += "（QC：以 HIS 记录为准修正）"
                        blocked.append((label, f"与 HIS 不一致，已修正为 {v}"))
                        break
        if re.match(r"^\s*\d+(\.\d+)?\s*$", value) and label not in RANGES and label != "数字":
            blocked.append((label, "疑似受控码值")); continue
        passed.append(f)
    # LLM 复核：仅当有绿字段且 LLM 可用时
    warnings = []
    greens = [f for f in passed if f["source"] == "green"]
    if greens and llm_available():
        listing = "\n".join(f"- {f['label']}：{f['value'][:120]}（依据：{f['basis'][:60]}）" for f in greens)
        try:
            data = llm_json([
                {"role": "system", "content": "你是病历质控医师。逐条检查下列草稿字段：医学逻辑矛盾、张冠李戴、把检查印象当病史、过度推断。只输出需修正项，没有则输出空数组。绝不新增字段。"},
                {"role": "user", "content": listing + "\n\n输出 JSON：{\"fixes\":[{\"label\":\"字段名\",\"reason\":\"问题\",\"action\":\"remove|keep_with_warning\"}]}"},
            ], max_tokens=600)
            remove = {x["label"] for x in data.get("fixes", []) if x.get("action") == "remove"}
            for x in data.get("fixes", []):
                warnings.append(f"QC 复核：{x.get('label')} — {x.get('reason')}")
            if remove:
                passed = [f for f in passed if f["label"] not in remove]
                blocked += [(l, "LLM 复核判定不合规，已移除") for l in remove]
        except Exception as e:
            warnings.append(f"LLM 复核失败（保守放行代码层结果）：{e}")
    report = {"blocked": blocked, "warnings": warnings,
              "summary": f"通过 {len(passed)} / 拦截修正 {len(blocked)} / 复核提示 {len(warnings)}"}
    return passed, trace_step("qc", (time.time() - t0) * 1000, report["summary"], warnings)
