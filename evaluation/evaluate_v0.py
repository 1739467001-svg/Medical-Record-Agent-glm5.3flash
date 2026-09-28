# -*- coding: utf-8 -*-
"""
住院病历生成评测框架 v0 —— 金标准反推评测（PRD 7.1 / 12.2-1,12.2-6）
====================================================================
方法：以医生真实书写完成的《入院记录》为金标准（输出端），用规则生成器 v0
仅凭 HIS 结构化数据（无录音状态）生成草稿，逐字段对照打分。

指标（PRD 7.2）：
  覆盖率 = 命中字段数 / 金标准已填字段数
  准确率 = 命中字段数 / 生成器填写字段数
  对话依赖度 = 金标准中"对话提炼"类(绿)已填字段 / 金标准已填字段数
  —— 对话依赖度即"无录音时的生成天花板"，是录音补采（D1）的量化依据。

生成器接口：generate(fields_gold, his) -> {label: value}
后续接入 LLM 生成器只需实现同一接口，评测框架与指标不变。

用法：/usr/bin/python3 evaluate_v0.py [病历资料目录]
输出：评测报告_v0.md（无 PII：仅指标、字段标签与患者代号）
"""
import os, re, sys, glob, json, zipfile, difflib, datetime, tempfile
import xml.etree.ElementTree as ET
import xlrd

XSI = '{http://www.w3.org/2001/XMLSchema-instance}type'
PATIENT_CODE = {"张俊杰": "患者A", "荣玉民": "患者B", "刘倩倩": "患者C", "麻玉兰": "患者D", "闫秋荣": "患者E"}
NUMERIC_LABELS = {"体温", "脉搏", "呼吸", "收缩压", "舒张压", "数字"}

# ---------------- 字段分类（与 Demo 前端 colorOf 同一套规则） ----------------
GREEN = set("主诉 主要症状 症状 持续时间 时间单位 现病史 发病情况 诱因 主要症状特点及其发展变化 伴随症状 上呼吸道症状 消化道症状 泌尿系症状 发病以来诊治经过及结果 发病以来一般情况 精神状态 食欲 睡眠 小便情况 大便情况 体重 与本次疾病无紧密关系的其他疾病情况 病史 既往史 疾病史（含外伤） 一般健康状况标志 健康状况 心血管病史 其他病史 肝炎结核病史 手术外伤输血 手术外伤史 过敏史 预防接种史 个人史 居住地 地址 接触史 疫区接触史 特殊地区居住史 有毒物质接触史 生活习惯、烟酒史 吸烟史 饮酒史 冶游史 婚育史 婚姻史 婚姻史(知识库) 生育史 生育史(知识库) 月经史 月经史： 月经量 月经颜色 月经相关症状 家族史 家族健康状况 父母 兄弟姐妹 有无遗传倾向疾病 病史陈述者姓名 病史陈述者 陈述者与患者关系的代码 查房记录 初步诊断 入院诊断 出院诊断 诊断依据 鉴别诊断 诊疗计划 诊疗经过 出院情况 出院医嘱 手术经过 术前诊断 术中诊断".split())
PE = set("发育 营养 表情 面容 神志 体位 配合检查 色泽 肝掌蜘蛛痣 全身浅表淋巴结 头颅异常 眼睑水肿 结膜 巩膜 角膜 瞳孔 对光反射 外耳道 乳突 鼻 鼻窦 口唇 口腔粘膜 齿龈 咽部粘膜 扁桃体 颈部 颈 颈动脉 颈静脉 气管 肝颈静脉回流征 甲状腺 甲状腺异常 胸廓 胸骨叩痛 呼吸运动 呼吸规整 肋间隙 语颤 胸膜摩擦感 叩诊 呼吸音 双侧 干湿性罗音 有无 心前区隆起 心律 心包摩擦音 腹外形 腹壁静脉曲张 腹部紧张度 压痛反跳痛 包块 肝脏 肠鸣音 直肠肛门 肛门生殖器 脊柱 脊柱畸形 四肢 专科情况 老中青 性别 起病 护理级别 Padua评分".split())
VITALS = set("体征 体温 脉搏 呼吸 收缩压 舒张压".split())

def color_of(f):
    label = f["label"]
    if not str(f["value"]).strip(): return "yellow"
    if "签名" in label: return "yellow"
    if label in GREEN: return "green"
    if label in ("记录时间", "辅助检查结果", "辅助检查") or label in VITALS: return "blue"
    if f["binding"] == "Patient": return "blue"
    if label in PE or label.startswith("体格检查"): return "gray"
    return "green"

# ---------------- 数据解析 ----------------
def fix_zip_name(name):
    try: return name.encode('cp437').decode('gbk')
    except Exception: return name

def parse_doc_fields(path):
    root = ET.parse(path).getroot()
    fields = []
    for inp in root.iter('Element'):
        if inp.get(XSI) != 'XInputField': continue
        bg = inp.find('BackgroundText')
        label = ''.join(bg.itertext()).strip() if bg is not None else ''
        ds = inp.find('.//DataSource')
        binding = ''.join(ds.itertext()).strip() if ds is not None else ''
        iv = inp.find('InnerValue'); val = inp.find('Value')
        value = ''.join(iv.itertext()).strip() if iv is not None else (''.join(val.itertext()).strip() if val is not None else '')
        if label: fields.append({"label": label, "binding": binding, "value": value})
    return fields

def excel_date(serial):
    try:
        return datetime.datetime(1899, 12, 30) + datetime.timedelta(days=float(serial))
    except Exception: return None

def sheet_rows(path):
    wb = xlrd.open_workbook(path); sh = wb.sheet_by_index(0)
    hdr = [str(sh.cell_value(0, c)).strip() for c in range(sh.ncols)]
    for r in range(1, sh.nrows):
        yield {h: sh.cell_value(r, i) for i, h in enumerate(hdr)}

def discover_admissions(base_dir, workdir):
    """解压全部 zip，返回 [(患者代号, 患者目录, 入院记录xml路径)]"""
    results = []
    for folder, code in PATIENT_CODE.items():
        pdir = os.path.join(base_dir, folder)
        if not os.path.isdir(pdir): continue
        zips = sorted(glob.glob(os.path.join(pdir, "*.zip")))
        for zi, zp in enumerate(zips):
            outdir = os.path.join(workdir, folder, f"stay{zi+1}")
            os.makedirs(outdir, exist_ok=True)
            with zipfile.ZipFile(zp) as zf:
                for n in zf.namelist():
                    fixed = fix_zip_name(n)
                    with open(os.path.join(outdir, fixed), 'wb') as f:
                        f.write(zf.read(n))
            for x in glob.glob(os.path.join(outdir, "入院记录_*.xml")):
                results.append((code, pdir, x))
    return results

# ---------------- 生成器 v0（规则版：仅用 HIS 结构化数据，无录音/无 LLM） ----------------
def load_his_views(pdir):
    views = {}
    for name in ("检验申请主表", "检查申请主表", "检查申请项目", "检查结果", "生命体征"):
        p = os.path.join(pdir, name + ".xls")
        views[name] = list(sheet_rows(p)) if os.path.exists(p) else []
    return views

def generate_v0(gold_fields, views):
    """规则生成器：只填 HIS 可推导字段；对话依赖字段一律留空（黄）。"""
    gen = {}
    # 入院锚点：优先用生命体征表中的"入院"事件行（HIS 天然提供），否则退回 gold 基本信息时间
    admit_dt = None
    for v in views["生命体征"]:
        if str(v.get("VITAL_SIGNS")).strip() == "入院":
            admit_dt = excel_date(v.get("TIME_POINT"))
            if admit_dt: break
    if admit_dt is None:
        for f in gold_fields:
            m = re.match(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}", str(f["value"]))
            if f["binding"] == "Patient" and m:
                admit_dt = datetime.datetime.strptime(m.group(0), "%Y-%m-%d %H:%M:%S")
                break
    # 生命体征：取入院前后 24h 内距入院时刻最近的一条
    win = [v for v in views["生命体征"]
           if admit_dt and (d := excel_date(v.get("TIME_POINT"))) and abs((d - admit_dt).total_seconds()) <= 86400]
    pick = {}
    for v in win:
        d = excel_date(v.get("TIME_POINT"))
        name, val = str(v.get("VITAL_SIGNS")), str(v.get("VITAL_SIGNS_CVALUES")).strip()
        if not val: continue
        if name not in pick or abs((d - admit_dt).total_seconds()) < pick[name][0]:
            pick[name] = (abs((d - admit_dt).total_seconds()), val)
    mapping = {"腋下体温": "体温", "脉搏": "脉搏", "呼吸": "呼吸"}
    for src, dst in mapping.items():
        if src in pick: gen[dst] = pick[src][1]
    if "血压" in pick and "/" in pick["血压"][1]:
        hi, _, lo = pick["血压"][1].partition("/")
        gen["收缩压"], gen["舒张压"] = hi.strip(), lo.strip()
    # 辅助检查结果：检查申请 + 结果印象拼接
    item_by_no = {}
    for it in views["检查申请项目"]:
        item_by_no.setdefault(str(it.get("EXAM_NO")), str(it.get("EXAM_ITEM")))
    lines = []
    for req in views["检查申请主表"]:
        no = str(req.get("EXAM_NO"))
        imp_rows = [r for r in views["检查结果"] if str(r.get("EXAM_NO")) == no and str(r.get("IMPRESSION")).strip()]
        if not imp_rows: continue
        dt = excel_date(req.get("EXAM_DATE_TIME"))
        ds = dt.strftime("%Y-%m-%d") if dt else ""
        item = item_by_no.get(no, str(req.get("EXAM_SUB_CLASS")))
        lines.append(f"{ds}  {item}：{imp_rows[0]['IMPRESSION'].strip()}")
    if lines: gen["辅助检查结果"] = "\n".join(lines)
    # 初步诊断：申请单上的临床诊断去重编号
    diags = []
    for req in views["检查申请主表"] + views["检验申请主表"]:
        for key in ("CLIN_DIAG", "RELEVANT_CLINIC_DIAG"):
            d = str(req.get(key, "")).strip()
            if d and d not in diags and not d.startswith("常规"):
                diags.append(d)
    if diags: gen["初步诊断"] = "\n".join(f"{i+1}.{d}" for i, d in enumerate(diags[:5]))
    return gen

# ---------------- 对比与指标 ----------------
def norm(v):
    s = str(v).strip().strip("{}").replace("\r", "").replace(" ", "")
    return s

def match(gen_val, gold_val, label):
    g, go = norm(gen_val), norm(gold_val)
    if not g or not go: return 0.0
    if label in NUMERIC_LABELS:
        try:
            a, b = float(re.sub(r"[^\d.]", "", g) or "nan"), float(re.sub(r"[^\d.]", "", go) or "nan")
            if a == b: return 1.0
            if b != 0 and abs(a - b) / abs(b) <= 0.05: return 1.0
            return 0.0
        except Exception: return 0.0
    r = difflib.SequenceMatcher(None, g, go).ratio()
    if g in go or go in g: return 1.0
    return 1.0 if r >= 0.6 else 0.0

def evaluate_one(code, gold_fields, views):
    labeled = [f for f in gold_fields if f["label"]]
    by_label_gold = {}
    for f in labeled:
        if str(f["value"]).strip():
            by_label_gold.setdefault(f["label"], []).append(f)
    cls = {}
    for label in by_label_gold:
        cls[label] = color_of({"label": label, "binding": by_label_gold[label][0]["binding"], "value": "x"})
    gen = generate_v0(gold_fields, views)
    filled = hit = 0
    hit_labels, miss_labels = [], []
    for label, fs in by_label_gold.items():
        if label not in gen: continue
        filled += len(fs)
        h = sum(1 for f in fs if match(gen[label], f["value"], label) > 0)
        hit += h
        (hit_labels if h else miss_labels).append(label)
    total_gold = sum(len(fs) for fs in by_label_gold.values())
    green_cnt = sum(len(fs) for l, fs in by_label_gold.items() if cls[l] == "green")
    return {
        "code": code, "G": total_gold, "F": filled, "H": hit,
        "coverage": hit / total_gold if total_gold else 0,
        "accuracy": hit / filled if filled else 0,
        "green_ratio": green_cnt / total_gold if total_gold else 0,
        "gen_filled": len(gen), "hit_labels": hit_labels, "miss_labels": miss_labels,
        "cls_counts": {c: sum(1 for l in by_label_gold if cls[l] == c) for c in ("blue", "green", "gray", "yellow")},
    }

# ---------------- 主流程 ----------------
def main(base_dir):
    out_dir = os.path.dirname(os.path.abspath(__file__))
    with tempfile.TemporaryDirectory() as workdir:
        adm = discover_admissions(base_dir, workdir)
        rows = []
        for code, pdir, xml in sorted(adm):
            gold = parse_doc_fields(xml)
            views = load_his_views(pdir)
            rows.append(evaluate_one(code, gold, views))
    n = len(rows)
    def avg(k): return sum(r[k] for r in rows) / n if n else 0
    lines = []
    lines.append("# 住院病历生成评测报告 v0（金标准反推 · 无录音基线）\n")
    lines.append(f"- 日期：{datetime.date.today().isoformat()}　|　文书：《入院记录》× {n} 份住院次　|　生成器：规则版 v0（仅 HIS 结构化数据，无录音、无 LLM）")
    lines.append("- 方法：PRD 7.1 金标准对照法。医生真实书写的入院记录为金标准，规则生成器从 HIS 视图（生命体征/检查申请与结果/临床诊断）生成草稿，逐字段对照。文本相似度 ≥0.6 记命中，数值按 5% 容差。\n")
    lines.append("## 一、核心结论\n")
    lines.append(f"1. **无录音状态下，规则生成器只能覆盖入院记录金标准内容的 {avg('coverage')*100:.1f}%**（跨 {n} 份均值）——缺口的主体是对话来源字段（主诉、现病史、既往史等）。")
    lines.append(f"2. **金标准中对录音的依赖度为 {avg('green_ratio')*100:.1f}%**：这些字段只可能来自医患对话（出生地、既往史、发病经过等），结构化数据里不存在。这正是 PRD 风险 D1（录音补采）的量化依据：**不补录音，入院记录生成上限约 {100-avg('green_ratio')*100:.0f}%，无法达到验收阈值（PRD 7.2 建议覆盖率 ≥90%）**。")
    lines.append(f"3. 生成器已填字段的准确率为 {avg('accuracy')*100:.1f}%：生命体征类基本命中；'初步诊断'（申请单临床诊断 vs 医生书写的规范诊断）与'辅助检查结果'（多报告拼接格式差异）命中不稳定——这类字段需要 LLM 的归纳改写能力，是 P1 多智能体核心的验证重点。\n")
    lines.append("## 二、分住院次明细\n")
    lines.append("| 患者代号 | 金标准已填 G | 生成填写 F | 命中 H | 覆盖率 H/G | 准确率 H/F | 录音依赖度(绿/G) | 分类(蓝/绿/灰/黄) |")
    lines.append("|---|---|---|---|---|---|---|---|")
    for r in rows:
        c = r["cls_counts"]
        lines.append(f"| {r['code']} | {r['G']} | {r['F']} | {r['H']} | {r['coverage']*100:.0f}% | {r['accuracy']*100:.0f}% | {r['green_ratio']*100:.0f}% | {c['blue']}/{c['green']}/{c['gray']}/{c['yellow']} |")
    lines.append(f"| **均值** | — | — | — | **{avg('coverage')*100:.1f}%** | **{avg('accuracy')*100:.1f}%** | **{avg('green_ratio')*100:.1f}%** | — |\n")
    hitset, missset = {}, {}
    for r in rows:
        for l in r["hit_labels"]: hitset[l] = hitset.get(l, 0) + 1
        for l in r["miss_labels"]: missset[l] = missset.get(l, 0) + 1
    lines.append("## 三、字段级分析\n")
    lines.append("### 规则生成器稳定命中的字段（HIS 可推导）\n")
    lines.append("、".join(f"{k}(×{v})" for k, v in sorted(hitset.items(), key=lambda x: -x[1])) or "（无）")
    lines.append("\n\n### 生成器尝试但未命中的字段（需 LLM 归纳改写或更细规则）\n")
    lines.append("、".join(f"{k}(×{v})" for k, v in sorted(missset.items(), key=lambda x: -x[1])) or "（无）")
    lines.append("\n\n### 完全依赖录音的字段类别（生成器未尝试）\n")
    lines.append("主诉、现病史全部子项、既往史全部子项、个人史、婚育史、家族史——即入院记录的主体叙述内容。\n")
    lines.append("## 四、下一步\n")
    lines.append("1. **录音补采到位后**：以真实对话录音重跑本框架（生成器换 ASR+LLM 链路），形成完整端到端基线。")
    lines.append("2. **P1 接入 LLM 生成器**：实现与 `generate_v0` 同签名的 `generate_llm()`，本框架指标与报告结构不变，双周回归（PRD 12.2-6）。")
    lines.append("3. **扩展文书类型**：先出院记录（HIS 汇总占比更高、预计无录音覆盖率更好），再首次病程。")
    lines.append("4. 阈值判断：当前基线距 PRD 7.2 建议验收值（覆盖率≥90%、可用率≥80%）的差距 = 录音依赖度 + LLM 改写能力两部分，前者是数据问题（D1），后者是一期核心研发内容。")
    report = "\n".join(lines)
    out = os.path.join(out_dir, "评测报告_v0.md")
    with open(out, "w", encoding="utf-8") as f:
        f.write(report)
    print("REPORT_WRITTEN:", out)
    print(f"汇总：n={n} 覆盖率={avg('coverage')*100:.1f}% 准确率={avg('accuracy')*100:.1f}% 录音依赖度={avg('green_ratio')*100:.1f}%")

if __name__ == "__main__":
    base = sys.argv[1] if len(sys.argv) > 1 else os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "病历资料", "住院")
    main(base)
