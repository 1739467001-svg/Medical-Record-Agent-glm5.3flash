# -*- coding: utf-8 -*-
"""字段映射智能体（纯代码，PRD 4.3.1）：质控后草稿 → 模板填充包。
对准字段地图（label→binding，来自字段地图 v0/模板），产出：
  {label: {value, source, basis, confidence, binding}} + 映射报告（未匹配/受控待确认）
"""
import re, time
from .common import trace_step

# 绑定路径表（字段地图 v0 实测高频绑定；评测/服务端共用）
BINDINGS = {
    "主诉": "DL_ZS_FY", "现病史": "field1", "既往史": "JWS", "个人史": "field40",
    "婚育史": "A01.07", "家族史": "field16", "体温": "", "脉搏": "", "呼吸": "",
    "收缩压": "", "舒张压": "", "辅助检查结果": "", "初步诊断": "", "出院诊断": "",
    "发育": "field25", "营养": "field25", "神志": "A01.17", "配合检查": "field25",
}
_DEFAULT = "auto"

def map_fields(draft, target_labels=None):
    """target_labels：模板标签集。产出 (mapped, trace)。
    mapped 为 list[dict]；未在模板中的字段不丢弃，标记 binding='(模板外)' 由调用方决定。"""
    t0 = time.time()
    mapped, out_of_template, coded = [], [], []
    for f in draft:
        label = f["label"]
        entry = dict(f)
        if target_labels is not None and label not in target_labels:
            entry["binding"] = "(模板外)"
            out_of_template.append(label)
        else:
            entry["binding"] = BINDINGS.get(label, _DEFAULT)
        if re.match(r"^\s*\d+(\.\d+)?\s*$", str(f["value"])):
            coded.append(label)
        mapped.append(entry)
    summary = (f"映射 {len(mapped)} 字段至模板绑定"
               + (f"；模板外 {len(out_of_template)}：{'、'.join(out_of_template[:6])}" if out_of_template else "")
               + (f"；受控值待院方词表 {len(coded)}" if coded else ""))
    return mapped, trace_step("mapper", (time.time() - t0) * 1000, summary)
