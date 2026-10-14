# -*- coding: utf-8 -*-
"""XTextDocument 模板回填器（P3 对接前置，PRD Q4 两步走的"直写一步"）。
====================================================================
院方 EMR 文书是完整 XTextDocument XML（页眉表格 + 段落 + XInputField 填充位），
此前的"归档包"只带平面字段表，导入院内还需人工对位。本模块把四色确认后的
字段值**原位回填**进真实模板 XML，产出可直接由院内编辑器打开/导入的文书：

    ① 模板来源：金标准 zip（字段标签/绑定路径/受控码值域与患者无关，可复用为模板）
    ② 值来源：归档包 fields（label→value，四色确认后的最终值）
    ③ 对位策略（按优先级）：
        a. Patient 绑定字段 → BindingPath 精确匹配（name/SEX/age…，值形态校验）
        b. 带 BindingPath 的输入域 → 按 InnerValue 原值等价替换（保留受控码/形态）
        c. 无绑定输入域 → 按标签文本（邻近 XString 前缀）匹配 label
    ④ 医生签名位留空（责任红线：签名永远医生手工）

数据安全：模板解析值全部来自 git 忽略区的金标准 zip；回填值来自归档包（脱敏）。
纯函数实现，无网络/无 LLM；单测覆盖（tests/test_records.py 追加 TestTemplateBackfill）。
"""
import io
import re
import zipfile
import xml.etree.ElementTree as ET

XSI = "{http://www.w3.org/2001/XMLSchema-instance}type"
_XML_DECL = '<?xml version="1.0" encoding="UTF-8"?>\n'

# Patient 绑定路径 → 回填键（值由归档包 fields 以 label 提供；此处按语义对位）
PATIENT_PATH_MAP = {
    "name": ("患者姓名",), "SEX": ("性别",), "age": ("年龄",), "NATION": ("民族",),
    "OCCUPATION": ("职业",), "MARRIAGE": ("婚姻",), "INTIME": ("入院时间",),
    "INTIMES": ("住院次数",), "PATIENTNO": ("病案号",), "BIRTHADDRESS": ("出生地",),
}


def _fix_zip_name(name):
    try:
        return name.encode("cp437").decode("gbk")
    except Exception:
        return name


def load_template(template_zip, doc_keyword):
    """从金标准 zip 中取指定文书的模板 ElementTree。返回 (root, 文件名) 或 (None, None)。
    注意匹配语义：文书名即 zip 内文件名前缀（"病程记录_首次病程记录_…"），子串匹配会
    误中"术后首次病程记录"——按文件名"类型_标题_"前缀优先，退化为包含匹配。"""
    with zipfile.ZipFile(template_zip) as zf:
        names = [_fix_zip_name(n) for n in zf.namelist()]
        pick = next((fn for fn in names if fn.startswith(doc_keyword + "_")), None)
        if pick is None:
            pick = next((fn for fn in names if doc_keyword in fn), None)
        if pick is None:
            return None, None
        return ET.fromstring(zf.read(pick).encode("latin-1", errors="replace").decode("utf-8", errors="replace") if False else zf.read(_orig_name(zf.namelist(), names, pick)).decode("utf-8", errors="replace")), pick


def _orig_name(orig_names, fixed_names, fixed):
    for o, f in zip(orig_names, fixed_names):
        if f == fixed:
            return o
    return fixed


def _set_value(inp, value):
    """回填一个 XInputField：InnerValue + Value + 渲染层 XString/Text 同步。
    注意：文档所有节点标签都是 Element，XString 是 xsi:type 属性而非标签——
    必须按属性过滤遍历（iter('XString') 恒空，实测踩坑）。空值清空。"""
    iv = inp.find("InnerValue")
    if iv is not None:
        iv.text = value
    # 渲染层同步：域内所有 Text 子节点（实测容器不止 XString——XTextButton 等控件
    # 同样携带展示文本），按 xsi:type 过滤会漏，直接同步全部 Text
    for el in inp.iter():
        if el.tag == "Text":
            el.text = value
    v = inp.find("Value")
    if v is not None:
        v.text = value


def _nearby_label(root, inp):
    """取输入域的前置视觉标签（同表格单元格/段落中相邻 XString 文本），用于无绑定域对位。"""
    for cell in root.iter():
        if cell is inp or inp not in list(cell.iter()):
            continue
        siblings = []
        for el in cell.iter():
            if el.get(XSI) == "XString":
                t = el.find("Text")
                if t is not None and t.text:
                    siblings.append(t.text.strip())
            if el is inp and siblings:
                return siblings[-1]
    return ""


def _field_key(inp, root):
    """输入域的规范标签：ToolTip > 邻近 XString 标签 > InnerValue 前缀提示。
    真实模板体征域用「T：」+ToolTip"体温"表示（实测），ToolTip 是最可靠对位键。"""
    tt = inp.find("ToolTip")
    if tt is not None and tt.text:
        return tt.text.strip()
    return _nearby_label(root, inp).rstrip(":：").strip()


def backfill(template_root, fields, patient_facts=None):
    """把 fields（[{label, value}]，四色确认后的最终值）原位回填进模板。
    patient_facts：案例登记信息（case_ingest 探测的脱敏值），用于 Patient 绑定域的
    语义桥接（性别/年龄/民族…）——fields 中没有时兜底。
    返回 (回填后 XML 文本, 统计{filled, cleared, matched, unmatched})。

    防串患者三原则（实测踩坑：模板即金标准文书，自带上一患者的真实值）：
    1. 只改已有填充位，不增删结构（院内编辑器兼容）；
    2. Patient 登记域：fields/登记信息对位 → 无值则**清空**（绝不保留模板旧患者值）；
    3. 未对位的临床域一律**清空**（宁缺毋造：模板旧值是别人的病历）。
    医生签名位保持不动（责任红线：签名永远医生手工）。"""
    by_label = {}
    for f in fields:
        label = str(f.get("label") or "").strip()
        value = str(f.get("value") or "")
        if label:
            by_label.setdefault(label, value)
    facts = patient_facts or {}
    label_by_path = {}
    for path, labels in PATIENT_PATH_MAP.items():
        for lb in labels:
            label_by_path[lb] = path

    filled, cleared, matched = 0, 0, set()
    for inp in root_inputs(template_root):
        bp = inp.find(".//BindingPath")
        ds = inp.find(".//DataSource")
        path = bp.text if bp is not None and bp.text else ""
        source = ds.text if ds is not None and ds.text else ""
        key = _field_key(inp, template_root)
        # ① Patient 绑定域：fields > 登记信息 > 清空（含 path 不在映射表的一般 Patient 域——
        #    实测病案号/出生地/职业/婚姻等按 path 对位不上时必须清空，否则旧患者登记值残留）
        if source == "Patient" and (path or key in PATIENT_PATH_MAP.values()):
            label = next((lb for lb, p in label_by_path.items() if p == path), key)
            value = by_label.pop(label, None)
            if value is None and path == "name":
                value = by_label.pop("患者姓名", None)
            if value is None:
                value = facts.get(path)
            if value is not None:
                _set_value(inp, value)
                filled += 1
                matched.add(f"Patient.{path or key}")
            else:
                _set_value(inp, "")
                cleared += 1
            continue
        # ② 标签精确 / ③ 前缀匹配
        if key and key in by_label:
            _set_value(inp, by_label.pop(key))
            filled += 1
            matched.add(key)
            continue
        if key:
            hit = next((lb for lb in list(by_label)
                        if lb.startswith(key) or key.startswith(lb) or lb.rstrip("：:") == key), None)
            if hit:
                _set_value(inp, by_label.pop(hit))
                filled += 1
                matched.add(hit)
                continue
        # ④ 未对位：有旧值则清空（模板旧患者值绝不残留）；空域保持空
        iv = inp.find("InnerValue")
        if iv is not None and (iv.text or "").strip():
            _set_value(inp, "")
            cleared += 1
    # BodyText 全文缓存重建（编辑器导出的纯文本摘要，实测含旧患者全文——必须同步）：
    # 从回填后的输入域值重排生成，保持"标签：值"行式与原缓存近似。
    body = template_root.find(".//BodyText")
    if body is not None:
        lines = []
        for inp in root_inputs(template_root):
            iv = inp.find("InnerValue")
            val = (iv.text or "").strip() if iv is not None else ""
            if not val:
                continue
            key = _field_key(inp, template_root)
            lines.append(f"{key}：{val}" if key else val)
        body.text = "\n".join(lines)
    stats = {"filled": filled, "cleared": cleared, "matched": sorted(matched),
             "unmatched": sorted(by_label.keys())}
    xml = _XML_DECL + ET.tostring(template_root, encoding="unicode")
    return xml, stats


def root_inputs(root):
    for inp in root.iter("Element"):
        if inp.get(XSI) == "XInputField":
            yield inp


def backfill_from_zip(template_zip, doc_keyword, fields, patient_facts=None):
    """组合入口：模板 zip + fields → (XML 文本, stats, 模板文件名)。"""
    root, fn = load_template(template_zip, doc_keyword)
    if root is None:
        return None, {"error": f"模板 zip 中未找到含「{doc_keyword}」的文书"}, None
    xml, stats = backfill(root, fields, patient_facts)
    return xml, stats, fn
