# -*- coding: utf-8 -*-
"""
住院病历生成评测框架 v0 —— 金标准反推评测（PRD 7.1 / 12.2-1,12.2-6）
====================================================================
方法：以医生真实书写完成的《入院记录》《出院记录》为金标准（输出端），用规则生成器 v0
仅凭 HIS 结构化数据（无录音状态）生成草稿，逐字段对照打分。

指标（PRD 7.2）：
  覆盖率 = 命中字段数 / 金标准已填字段数
  准确率 = 命中字段数 / 生成器填写字段数
  对话依赖度 = 金标准中"对话提炼"类(绿)已填字段 / 金标准已填字段数
  —— 对话依赖度即"无录音时的生成天花板"，是录音补采（D1）的量化依据。

生成器接口：generate_v0(gold_fields, views, anchor) -> {label: value}
后续接入 LLM 生成器只需实现同一契约，评测框架与指标不变。

用法：python3 evaluate_v0.py [病历资料目录]
输出：评测报告_v0.md（无 PII：仅指标、字段标签与患者代号）
"""
import os, re, sys, glob, json, zipfile, difflib, datetime, tempfile
import xml.etree.ElementTree as ET
import xlrd

XSI = '{http://www.w3.org/2001/XMLSchema-instance}type'
# 患者代号映射（数据安全红线：真实姓名不入 Git，2026-10-03 修复）——
# 从 git 忽略文件 病历资料/患者代号映射.json 读取，格式 {"真实姓名": "患者A", ...}；
# 缺失时按目录名字典序兜底映射 患者A/B/C…（顺序可能与既往报告不一致，会告警）。
_PATIENT_CODE = None

def _patient_code_map():
    global _PATIENT_CODE
    if _PATIENT_CODE is not None:
        return _PATIENT_CODE
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    mfile = os.path.join(root, "病历资料", "患者代号映射.json")
    base = os.path.join(root, "病历资料", "住院")
    if os.path.exists(mfile):
        _PATIENT_CODE = json.load(open(mfile, encoding="utf-8"))
    else:
        folders = sorted(d for d in os.listdir(base)
                         if os.path.isdir(os.path.join(base, d))) if os.path.isdir(base) else []
        _PATIENT_CODE = {f: f"患者{chr(ord('A') + i)}" for i, f in enumerate(folders)}
        if folders:
            print(f"[evaluate_v0] ⚠ 未找到 患者代号映射.json，按目录序兜底映射（可能影响与既往报告的对齐）",
                  file=sys.stderr)
    return _PATIENT_CODE
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

def discover_docs(base_dir, workdir, types=("入院记录", "出院记录", "首次病程记录")):
    """解压全部 zip，返回 [(患者代号, 患者目录, 文书类型, 文书xml路径)]"""
    fname_map = {"入院记录": "入院记录_*.xml", "出院记录": "出院记录_*.xml",
                 "首次病程记录": "病程记录_首次病程记录_*.xml"}
    results = []
    for folder, code in _patient_code_map().items():
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
            for dtype in types:
                for x in glob.glob(os.path.join(outdir, fname_map[dtype])):
                    results.append((code, pdir, dtype, x))
    return results

def load_his_views(pdir):
    views = {}
    for name in ("检验申请主表", "检查申请主表", "检查申请项目", "检查结果", "生命体征"):
        p = os.path.join(pdir, name + ".xls")
        views[name] = list(sheet_rows(p)) if os.path.exists(p) else []
    return views

# ---------------- 生成器 v0（规则版：仅用 HIS 结构化数据，无录音/无 LLM） ----------------
def generate_v0(gold_fields, views, anchor="入院", doc_type="入院记录", stay_dt=None):
    """规则生成器：只填 HIS 可推导字段；对话依赖字段一律留空。
    anchor：入院记录取"入院"事件行、出院记录取"出院"事件行作为体征时间锚点。
    stay_dt：文书名日期（本次住院锚点），规则版不使用（多智能体版用于住院次窗口过滤）。"""
    gen = {}
    anchor_dt = None
    for v in views["生命体征"]:
        if str(v.get("VITAL_SIGNS")).strip() == anchor:
            anchor_dt = excel_date(v.get("TIME_POINT"))
            if anchor_dt: break
    if anchor_dt is None:
        for f in gold_fields:
            m = re.match(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}", str(f["value"]))
            if f["binding"] == "Patient" and m:
                anchor_dt = datetime.datetime.strptime(m.group(0), "%Y-%m-%d %H:%M:%S")
                break
    # 生命体征：取锚点前后 24h 内距锚点最近的一条
    win = []
    if anchor_dt:
        for v in views["生命体征"]:
            d = excel_date(v.get("TIME_POINT"))
            if d and abs((d - anchor_dt).total_seconds()) <= 86400:
                win.append(v)
    pick = {}
    for v in win:
        d = excel_date(v.get("TIME_POINT"))
        name, val = str(v.get("VITAL_SIGNS")), str(v.get("VITAL_SIGNS_CVALUES")).strip()
        if not val or name not in ("腋下体温", "脉搏", "呼吸", "血压"): continue
        dist = abs((d - anchor_dt).total_seconds()) if d else 1e18
        if name not in pick or dist < pick[name][0]:
            pick[name] = (dist, val)
    for src, dst in (("腋下体温", "体温"), ("脉搏", "脉搏"), ("呼吸", "呼吸")):
        if src in pick: gen[dst] = pick[src][1]
    if "血压" in pick and "/" in pick["血压"][1]:
        hi, _, lo = pick["血压"][1].partition("/")
        gen["收缩压"], gen["舒张压"] = hi.strip(), lo.strip()
    # 辅助检查：检查申请 + 结果印象拼接
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
    if "辅助检查" not in gen:
        gen["辅助检查"] = gen.get("辅助检查结果", "")
    # 诊断：申请单临床诊断去重编号
    diags = []
    for req in views["检查申请主表"] + views["检验申请主表"]:
        for key in ("CLIN_DIAG", "RELEVANT_CLINIC_DIAG"):
            d = str(req.get(key, "") if key in req else "").strip()
            if d and d not in diags and not d.startswith("常规"):
                diags.append(d)
    if diags:
        gen["初步诊断"] = "\n".join(f"{i+1}.{d}" for i, d in enumerate(diags[:5]))
        gen["出院诊断"] = gen["初步诊断"]
    return gen

# ---------------- 对比与指标 ----------------
def norm(v):
    return str(v).strip().strip("{}").replace("\r", "").replace(" ", "")

def match(gen_val, gold_val, label):
    g, go = norm(gen_val), norm(gold_val)
    if not g or not go: return 0.0
    if label in NUMERIC_LABELS:
        try:
            a = float(re.sub(r"[^\d.]", "", g) or "nan")
            b = float(re.sub(r"[^\d.]", "", go) or "nan")
            if a == b: return 1.0
            if b != 0 and abs(a - b) / abs(b) <= 0.05: return 1.0
            return 0.0
        except Exception: return 0.0
    if g in go or go in g: return 1.0
    return 1.0 if difflib.SequenceMatcher(None, g, go).ratio() >= 0.6 else 0.0

def evaluate_one(code, gold_fields, views, anchor="入院", doc_type="入院记录", gen_fn=None, stay_dt=None):
    gen_fn = gen_fn or generate_v0
    labeled = [f for f in gold_fields if f["label"]]
    by_label_gold = {}
    for f in labeled:
        if str(f["value"]).strip():
            by_label_gold.setdefault(f["label"], []).append(f)
    cls = {label: color_of({"label": label, "binding": fs[0]["binding"], "value": "x"})
           for label, fs in by_label_gold.items()}
    try:
        gen = gen_fn(gold_fields, views, anchor=anchor, doc_type=doc_type, stay_dt=stay_dt)
    except TypeError:  # 旧契约生成器（无 stay_dt 形参）
        gen = gen_fn(gold_fields, views, anchor=anchor, doc_type=doc_type)
    filled = hit = 0
    hit_labels, miss_labels = [], []
    for label, fs in by_label_gold.items():
        if label not in gen or not str(gen[label]).strip(): continue
        filled += len(fs)
        h = sum(1 for f in fs if match(gen[label], f["value"], label) > 0)
        hit += h
        (hit_labels if h else miss_labels).append(label)
    total_gold = sum(len(fs) for fs in by_label_gold.values())
    green_cnt = sum(len(fs) for l, fs in by_label_gold.items() if cls[l] == "green")
    return {
        "code": code, "doc_type": doc_type, "G": total_gold, "F": filled, "H": hit,
        "coverage": hit / total_gold if total_gold else 0,
        "accuracy": hit / filled if filled else 0,
        "green_ratio": green_cnt / total_gold if total_gold else 0,
        "hit_labels": hit_labels, "miss_labels": miss_labels,
        "cls_counts": {c: sum(1 for l in by_label_gold if cls[l] == c) for c in ("blue", "green", "gray", "yellow")},
    }

# ---------------- 报告 ----------------
def main(base_dir, generator="rules", types=("入院记录", "出院记录", "首次病程记录"), out=None):
    out_dir = os.path.dirname(os.path.abspath(__file__))
    if generator == "llm":
        import generate_llm
        gen_fn = generate_llm.generate
        gen_name = "LLM 版 v1（规则打底 + DeepSeek 归纳 + 模板常规）"
    elif generator == "multiagent":
        import generate_multiagent
        gen_fn = generate_multiagent.generate
        gen_name = "多智能体 v1（六智能体流水线：extractor/aggregator/retriever/writer/qc/mapper，模式 " + os.environ.get("MULTIAGENT_MODE", "A") + "）"
    else:
        gen_fn = generate_v0
        gen_name = "规则版 v0（仅 HIS 结构化数据，无录音、无 LLM）"
    with tempfile.TemporaryDirectory() as workdir:
        docs = discover_docs(base_dir, workdir, types)
        groups = {}
        for code, pdir, dtype, xml in sorted(docs):
            gold = parse_doc_fields(xml)
            views = load_his_views(pdir)
            anchor = "入院"
            # 住院次锚点：文书名中的 8 位日期（入院/首程=入院日，出院记录=出院日）
            dates = re.findall(r"20\d{6}", os.path.basename(xml))
            stay_dt = (datetime.datetime.strptime(dates[0], "%Y%m%d") if dates else None)
            groups.setdefault(dtype, []).append(
                evaluate_one(code, gold, views, anchor=anchor, doc_type=dtype, gen_fn=gen_fn, stay_dt=stay_dt))

    def avg(k, rows): return sum(r[k] for r in rows) / len(rows) if rows else 0
    lines = []
    lines.append("# 住院病历生成评测报告 v0（金标准反推 · 无录音基线）\n")
    n_adm, n_dis = len(groups.get("入院记录", [])), len(groups.get("出院记录", []))
    lines.append(f"- 日期：{datetime.date.today().isoformat()}　|　文书：" + " + ".join(f"{t} × {len(groups[t])}" for t in groups if groups[t]) + f"　|　生成器：{gen_name}")
    lines.append("- 方法：PRD 7.1 金标准对照法。医生真实书写的文书为金标准，生成器从 HIS 视图（生命体征/检查申请与结果/临床诊断）生成草稿，逐字段对照。文本相似度 ≥0.6 记命中，数值按 5% 容差。LLM 版另含模板常规所见（灰），须医生逐项核对。")
    if generator == "multiagent":
        lines.append("- 住院次限定与规范用语改写（P2-H）：HIS 视图为患者级导出，评测适配层按文书名日期窗口过滤（入院/首程 ±3 天、出院 ±14 天）只保留本次住院数据行，窗口空表原样保留；诊断字段由申请单临床诊断规范改写产出（生成端改写，QC 回流去\"？\"/ICD尾缀），无录音时不编造病史类字段。\n")
    lines.append("## 一、核心结论\n")
    if n_adm:
        grn_adm = avg("green_ratio", groups["入院记录"])
        lines.append(f"1. **入院记录：录音依赖度 {grn_adm*100:.1f}%**（主诉/现病史/既往史/个人史/家族史只存在于医患对话），无录音基线覆盖率 {avg('coverage', groups['入院记录'])*100:.1f}%——**不补录音，入院记录生成上限约 {100-grn_adm*100:.0f}%，无法达到验收阈值（PRD 7.2 建议覆盖率 ≥90%）**。这是录音补采（PRD 风险 D1）的量化依据。")
    if n_dis:
        grn_dis = avg("green_ratio", groups["出院记录"])
        lines.append(f"2. **出院记录：录音依赖度 {grn_dis*100:.1f}%（高于入院记录），无录音基线覆盖率 {avg('coverage', groups['出院记录'])*100:.1f}%**——诊疗经过、出入院情况等叙述性内容同样出自医生口述，说明**单纯增加结构化数据无法绕开录音依赖，医患/医护对话数据是全部文书类型的共同先决条件**，录音补采（D1）的优先级进一步提升。")
    if n_adm and n_dis:
        lines.append(f"3. 已填字段准确率：入院 {avg('accuracy', groups['入院记录'])*100:.1f}% / 出院 {avg('accuracy', groups['出院记录'])*100:.1f}%。生命体征类基本命中；'初步/出院诊断'经 P2-H 规范用语改写（申请单诊断直填改写 + QC 回流）命中率已显著提升，剩余未命中主要是申请单未载明的诊断（医生口述/临床推理，录音依赖）与'辅助检查'多报告拼接格式。\n")
    for dtype, rows in groups.items():
        lines.append(f"## {dtype}分住院次明细\n")
        lines.append("| 患者代号 | 金标准已填 G | 生成填写 F | 命中 H | 覆盖率 H/G | 准确率 H/F | 录音依赖度(绿/G) | 分类(蓝/绿/灰/黄) |")
        lines.append("|---|---|---|---|---|---|---|---|")
        for r in rows:
            c = r["cls_counts"]
            lines.append(f"| {r['code']} | {r['G']} | {r['F']} | {r['H']} | {r['coverage']*100:.0f}% | {r['accuracy']*100:.0f}% | {r['green_ratio']*100:.0f}% | {c['blue']}/{c['green']}/{c['gray']}/{c['yellow']} |")
        lines.append(f"| **均值** | — | — | — | **{avg('coverage', rows)*100:.1f}%** | **{avg('accuracy', rows)*100:.1f}%** | **{avg('green_ratio', rows)*100:.1f}%** | — |\n")
    hitset, missset = {}, {}
    for rows in groups.values():
        for r in rows:
            for l in r["hit_labels"]: hitset[l] = hitset.get(l, 0) + 1
            for l in r["miss_labels"]: missset[l] = missset.get(l, 0) + 1
    lines.append("## 字段级分析\n")
    lines.append("### 生成器命中的字段（HIS 可推导，跨全部文书）\n")
    lines.append("、".join(f"{k}(×{v})" for k, v in sorted(hitset.items(), key=lambda x: -x[1])) or "（无）")
    lines.append("\n### 生成器尝试但未命中的字段（需更强归纳能力或更细规则）\n")
    lines.append("、".join(f"{k}(×{v})" for k, v in sorted(missset.items(), key=lambda x: -x[1])) or "（无）")
    lines.append("\n### 完全依赖录音的字段类别（生成器未尝试）\n")
    lines.append("入院记录：主诉、现病史全部子项、既往史全部子项、个人史、婚育史、家族史；出院记录：入院情况叙述、诊疗经过、出院情况。\n")
    lines.append("## 下一步\n")
    lines.append("1. **录音补采到位后**：以真实对话录音重跑本框架（生成器换 ASR+LLM 链路），形成完整端到端基线。")
    lines.append("2. **LLM 生成器**：`generate_llm()` 已实现（规则打底 + DeepSeek 归纳 + 模板常规），用 `--generator llm` 运行；双周回归（PRD 12.2-6）。")
    lines.append("3. **扩展文书类型**：下一步首次病程记录（鉴别诊断等知识辅助字段是 LLM 价值点）。")
    lines.append("4. 阈值判断：距 PRD 7.2 建议验收值（覆盖率≥90%、可用率≥80%）的差距 = 录音依赖度 + LLM 改写能力两部分，前者是数据问题（D1），后者是一期核心研发内容。")
    report = "\n".join(lines)
    out = os.path.join(out_dir, out or f"评测报告_{generator}.md")
    with open(out, "w", encoding="utf-8") as f:
        f.write(report)
    # 机器可读指标（供回归自动化 diff，PRD 12.2-6）：与报告同名的 .json
    payload = {
        "date": datetime.date.today().isoformat(), "generator": generator, "gen_name": gen_name,
        "types": {t: len(rows) for t, rows in groups.items()},
        "groups": {t: {"coverage": avg("coverage", rows), "accuracy": avg("accuracy", rows),
                       "green_ratio": avg("green_ratio", rows), "n": len(rows),
                       "rows": [{k: r[k] for k in ("code", "G", "F", "H", "coverage", "accuracy",
                                                   "green_ratio", "hit_labels", "miss_labels")} for r in rows]}
                   for t, rows in groups.items()},
        "hitset": hitset, "missset": missset,
    }
    with open(os.path.splitext(out)[0] + ".json", "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=1)
    print("REPORT_WRITTEN:", out)
    for dtype, rows in groups.items():
        print(f"  {dtype}: n={len(rows)} 覆盖率={avg('coverage', rows)*100:.1f}% 准确率={avg('accuracy', rows)*100:.1f}% 录音依赖度={avg('green_ratio', rows)*100:.1f}%")

if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("base", nargs="?", default=os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "病历资料", "住院"))
    ap.add_argument("--generator", choices=["rules", "llm", "multiagent"], default="rules")
    ap.add_argument("--types", default="入院记录,出院记录,首次病程记录")
    ap.add_argument("--out", default=None, help="输出报告文件名（默认 评测报告_<generator>.md）")
    a = ap.parse_args()
    main(a.base, generator=a.generator, types=tuple(t for t in a.types.split(",") if t), out=a.out)
