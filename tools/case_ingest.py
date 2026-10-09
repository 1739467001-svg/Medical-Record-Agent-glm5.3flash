# -*- coding: utf-8 -*-
"""真实案例接入传送带（患者F 首例沉淀，迎接第 2、3 个案例）
====================================================================
把「一例真实数据」（HIS 8 表 xls + 录音转写稿）变成平台可用的脱敏演示数据：

    案例目录（git 忽略）                     平台数据（入 Git，全脱敏）
    病历资料/住院/<真实姓名>/                demo/data/patients.json   ← 追加住院次条目
    ├── 医嘱.xls 等 8 张 HIS 表       ──►    demo/data/dialogue_<code>.json ← 真实对话（角色标注）
    └── 录音/转写稿/*.txt（asr_ingest 产物）  <案例目录>/接入报告.md    ← 留在忽略区（含真实值）

流程：8 表加载（复用 evaluate_v0.load_his_views）→ 住院窗口过滤（复用 generate_multiagent）
→ 自动探测 入院/出院/科室/诊断 → 构建 his 节 + 模板骨架文书（复用模板患者的字段标签，值清空）
→ 转写稿角色标注（LLM 可用时；否则疑问句启发式）→ 红线扫描（真实姓名/住院号/手机号 0 命中才落盘）。

用法：
    # 先转写（asr_ingest），再接入：
    python3 tools/case_ingest.py 病历资料/住院/<姓名> --code DA0006
    python3 tools/case_ingest.py 病历资料/住院/<姓名> --code DA0006 --dry-run   # 只出报告不落盘

数据安全（约法三章第 1 条）：
  - 案例目录必须被 git 忽略（启动强制校验，与 asr_ingest 同规则）；
  - 输出到 demo/data 前逐 token 红线扫描：患者姓名/医护姓名/PATIENT_ID/手机号/证件号，
    任一命中即拒绝写入（医护按"姓+医生/护士"改写后重扫）；
  - 患者代号从 git 忽略的 病历资料/患者代号映射.json 读取，缺失则报错（不发明代号）。
"""
import argparse, datetime, json, os, re, subprocess, sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "evaluation"))
sys.path.insert(0, os.path.join(REPO, "server"))

import evaluate_v0  # noqa: E402  load_his_views / excel_date / _patient_code_map
import generate_multiagent  # noqa: E402  _filter_views_by_stay（住院窗口过滤，P2-H）

DEMO_DATA = os.path.join(REPO, "demo", "data")
PATIENTS_JSON = os.path.join(DEMO_DATA, "patients.json")
# 骨架模板：字段标签/绑定路径/受控标记来自医院 EMR 模板（与具体患者无关），值一律清空
TEMPLATE_CODE_DEFAULT = "DA0001"
SKELETON_DOCS = ("入院记录", "首次病程记录", "手术记录", "出院记录")
# 医护姓名列（与 deid_tools/deidentify.py 同一套口径）
STAFF_NAME_COLS = ("DOCTOR", "STOP_DOCTOR", "DOCTOR_USER", "CANCEL_DOCTOR", "STOP_DOCTOR_USER",
                   "REQ_PHYSICIAN", "CONFIRM_DOCT", "ORDERING_PROVIDER", "REPORTER",
                   "NURSE", "STOP_NURSE", "NURSE_USER", "STOP_NURSE_USER", "PROCESSING_NURSE",
                   "STOP_PROCESSING_NURSE", "CANCEL_NURSE", "REQ_NURSE", "REPORT_NURSE",
                   "SPCM_SAMPLE_NURSE", "TECHNICIAN", "EXAM_OPERATOR", "APPOINT_OPERATOR")
SYMPTOM_DIAGS = ("腹痛", "发热", "头晕", "恶心", "呕吐", "胸痛", "咳嗽")  # disease 展示时剔除症状条目


def fail(msg):
    print(f"❌ {msg}", file=sys.stderr)
    sys.exit(1)


def ensure_ignored(path):
    """案例目录必须被 git 忽略（真实数据永不入库）。"""
    p = os.path.abspath(path)
    if not os.path.isdir(p):
        fail(f"案例目录不存在：{p}")
    r = subprocess.run(["git", "check-ignore", os.path.relpath(p, REPO)],
                       cwd=REPO, capture_output=True, text=True)
    if r.returncode != 0:
        fail(f"目录未被 .gitignore 忽略（{p}）——真实案例数据永不入库")
    return p


def load_llm_env():
    """加载 evaluation/.llm_env（export K=V 或 K=V），已存在的环境变量优先。"""
    p = os.path.join(REPO, "evaluation", ".llm_env")
    if not os.path.exists(p):
        return
    for line in open(p, encoding="utf-8"):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        line = re.sub(r"^export\s+", "", line)
        if "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def xdate(row, *cols):
    for c in cols:
        d = evaluate_v0.excel_date(row.get(c))
        if d:
            return d
    return None


def collect_staff_names(views):
    """收集医护真实姓名（红线扫描用），返回 {姓名: 角色}。"""
    names = {}
    for tbl, rows in views.items():
        for r in rows:
            for col in STAFF_NAME_COLS:
                v = str(r.get(col, "")).strip()
                if 2 <= len(v) <= 4 and re.fullmatch(r"[\u4e00-\u9fa5]+", v):
                    role = "护士" if "NURSE" in col else "医生"
                    names.setdefault(v, role)
    return names


def detect_facts(views_raw):
    """自动探测案例事实：入院/出院时间、科室、诊断、患者基本信息。"""
    facts = {}
    vitals = views_raw.get("生命体征", [])
    admits = [xdate(v, "TIME_POINT", "RECORDING_DATE") for v in vitals
              if str(v.get("VITAL_SIGNS")).strip() == "入院"]
    admits = [a for a in admits if a]
    facts["admit_dt"] = max(admits) if admits else None
    orders = views_raw.get("医嘱", [])
    disch = [xdate(o, "START_DATE_TIME", "ENTER_DATE_TIME") for o in orders
             if "出院" in str(o.get("ORDER_TEXT", ""))]
    disch = [d for d in disch if d]
    facts["discharge_dt"] = max(disch) if disch else None
    m = next((re.search(r"按(.{2,8}?)护理常规", str(o.get("ORDER_TEXT", ""))) for o in orders
              if "护理常规" in str(o.get("ORDER_TEXT", ""))), None)
    facts["dept"] = m.group(1) if m else ""
    facts["has_surgery"] = any("手术" in str(o.get("ORDER_TEXT", "")) and
                               ("通知" in str(o.get("ORDER_TEXT", "")) or "切除" in str(o.get("ORDER_TEXT", "")))
                               for o in orders)
    diags = []
    for tbl in ("检查申请主表", "检验申请主表"):
        for r in views_raw.get(tbl, []):
            for key in ("CLIN_DIAG", "RELEVANT_CLINIC_DIAG"):
                d = str(r.get(key, "")).strip()
                if d and d not in diags:
                    diags.append(d)
    facts["clinic_diags"] = diags
    main = [d for d in diags if not any(s in d for s in SYMPTOM_DIAGS)]
    facts["disease"] = (main[0] if main else (diags[0] if diags else "")) + ("（手术）" if facts["has_surgery"] else "")
    # 患者基本信息：检验申请主表最近一行（AGE/SEX 随住院更新）
    labs = views_raw.get("检验申请主表", [])
    latest = max(labs, key=lambda r: (r.get("REQUESTED_DATE_TIME") or 0)) if labs else {}
    facts["sex"] = str(latest.get("SEX", "")).strip()
    age = latest.get("AGE")
    facts["age"] = f"{int(age)}岁" if age else ""
    pids = {str(r.get("PATIENT_ID")).strip() for tbl in ("医嘱", "检验申请主表", "检查申请主表")
            for r in views_raw.get(tbl, []) if str(r.get("PATIENT_ID", "")).strip()}
    facts["patient_ids"] = sorted(pids)
    names = {str(r.get("NAME")).strip() for tbl in ("检验申请主表", "检查申请主表")
             for r in views_raw.get(tbl, []) if str(r.get("NAME", "")).strip()}
    facts["patient_names"] = sorted(names)
    return facts


def build_his_node(views, facts):
    """过滤后的 HIS 视图 → demo his 节（格式与 patients.json 既有条目一致）。"""
    anchor = facts["admit_dt"]
    win = views  # 已按住院窗口过滤
    vitals = []
    pick = {}
    for v in win.get("生命体征", []):
        name = str(v.get("VITAL_SIGNS")).strip()
        val = str(v.get("VITAL_SIGNS_CVALUES")).strip()
        if not val or name in ("入院", "出院", "手术", "转入"):
            continue
        d = xdate(v, "TIME_POINT", "RECORDING_DATE")
        dist = abs((d - anchor).total_seconds()) if (d and anchor) else 9e18
        if name not in pick or dist < pick[name][0]:
            pick[name] = (dist, val, str(v.get("UNITS", "")).strip(),
                          d.strftime("%Y-%m-%d") if d else "")
    for name, (_, val, unit, date) in pick.items():
        vitals.append({"name": name, "last": val, "unit": unit, "date": date})
    exams, seen = [], set()
    imp_by_no = {}
    for r in win.get("检查结果", []):
        no = str(r.get("EXAM_NO"))
        imp = str(r.get("IMPRESSION", "")).strip()
        if imp and no not in imp_by_no:
            imp_by_no[no] = imp
    item_by_no = {}
    for it in win.get("检查申请项目", []):
        item_by_no.setdefault(str(it.get("EXAM_NO")), str(it.get("EXAM_ITEM")))
    for req in win.get("检查申请主表", []):
        no = str(req.get("EXAM_NO"))
        if no in seen or no not in imp_by_no:
            continue
        seen.add(no)
        d = xdate(req, "EXAM_DATE_TIME", "REQ_DATE_TIME")
        exams.append({"exam_no": "", "item": item_by_no.get(no, ""),
                      "impression": imp_by_no[no][:200],
                      "date": d.strftime("%Y-%m-%d") if d else ""})
    labs = []
    for r in win.get("检验结果", []):
        if not str(r.get("ABNORMAL_INDICATOR", "")).strip():
            continue
        d = xdate(r, "RESULT_DATE_TIME")
        labs.append({"item": str(r.get("REPORT_ITEM_NAME", "")).strip(),
                     "result": str(r.get("RESULT", "")).strip(),
                     "units": str(r.get("UNITS", "")).strip(),
                     "ref": str(r.get("PRINT_CONTEXT", "")).strip()[:24],
                     "date": d.strftime("%Y-%m-%d") if d else ""})
    orders = []
    for o in win.get("医嘱", []):
        txt = str(o.get("ORDER_TEXT", "")).strip()
        if not txt:
            continue
        d = xdate(o, "START_DATE_TIME", "ENTER_DATE_TIME")
        dose = (str(o.get("DOSAGE", "")).rstrip(".0") if o.get("DOSAGE") not in (None, "") else "")
        orders.append({"text": txt, "dose": (dose + str(o.get("DOSAGE_UNITS", "") or "")).strip(),
                       "route": str(o.get("ADMINISTRATION", "") or "").strip(),
                       "freq": str(o.get("FREQUENCY", "") or "").strip(),
                       "start": d.strftime("%Y-%m-%d") if d else ""})
    return {"abnormal_labs": labs[:60], "exams": exams, "orders": orders[:60], "vitals": vitals}


def build_skeleton_docs(template_adm, facts, code):
    """模板患者文书 → 骨架（标签/绑定/受控保留，值清空=黄色待生成）。
    Patient 绑定字段按值形态回填脱敏登记信息（姓名→代号、性别、年龄、入院时间、住院号掩码）。"""
    admit_ts = facts["admit_dt"]
    discharge_ts = facts["discharge_dt"] or admit_ts
    fill_pat = {}
    pid = (facts["patient_ids"] or [""])[0]
    masked_pid = (pid[:2] + "*" * max(0, len(pid) - 4) + pid[-2:]) if len(pid) >= 6 else "****"
    admit_str = admit_ts.strftime("%Y-%m-%d %H:%M:%S") if admit_ts else ""

    def map_patient_value(v):
        s = str(v).strip()
        if s in facts["patient_names"] or not s:
            return None
        if s in ("男", "女"):
            return facts["sex"] or None
        if re.fullmatch(r"\d+岁", s):
            return facts["age"] or None
        if re.fullmatch(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}", s):
            return admit_str or None  # 模板中的时间戳（入院/记录时间）→ 本案例入院时间
        if re.search(r"\*{2,}", s):
            return masked_pid  # 模板中的掩码住院号 → 本案例掩码住院号
        return None

    docs = []
    for t in template_adm.get("docs", []):
        dtype, title = t.get("type"), t.get("title", "")
        if dtype == "病程记录" and "首次病程记录" not in title:
            continue
        if dtype not in SKELETON_DOCS and not (dtype == "病程记录" and "首次病程记录" in title):
            continue
        fields = []
        for f in t.get("fields", []):
            nf = {"label": f.get("label", ""), "binding": f.get("binding", ""),
                  "dropdown": bool(f.get("dropdown")), "value": ""}
            if f.get("binding") == "Patient":
                mv = map_patient_value(f.get("value", ""))
                nf["value"] = mv if mv is not None else ""
            fields.append(nf)
        # Patient 绑定第一个空位 = 姓名 → 患者代号
        for nf in fields:
            if nf["binding"] == "Patient" and not nf["value"]:
                nf["value"] = fill_pat.setdefault("name", code)
                break
        ts = (admit_ts if dtype != "出院记录" else discharge_ts)
        docs.append({"type": dtype, "title": title or dtype,
                     "ts": ts.strftime("%Y%m%d") if ts else "", "fields": fields})
    order = {n: i for i, n in enumerate(SKELETON_DOCS)}
    docs.sort(key=lambda d: order.get(d["type"] if d["type"] != "病程记录" else "首次病程记录", 9))
    return docs


# ---------------- 转写稿 → 角色标注对话 ----------------
def _load_transcript(case_dir, explicit=None):
    if explicit:
        return open(explicit, encoding="utf-8").read().strip()
    tdir = os.path.join(case_dir, "录音", "转写稿")
    if not os.path.isdir(tdir):
        return ""
    txts = sorted(f for f in os.listdir(tdir) if f.endswith(".txt"))
    return open(os.path.join(tdir, txts[-1]), encoding="utf-8").read().strip() if txts else ""


def _segments(case_dir):
    tdir = os.path.join(case_dir, "录音", "转写稿")
    if not os.path.isdir(tdir):
        return []
    js = sorted(f for f in os.listdir(tdir) if f.endswith(".json"))
    if not js:
        return []
    return json.load(open(os.path.join(tdir, js[-1]), encoding="utf-8")).get("segments", [])


_HEUR_DOC = re.compile(r"(吗|呢|\?|？|来$|我看|躺平|等着|先别|张嘴|深呼吸)")

def tag_roles_llm(segments, labels_vocab):
    """LLM 角色标注 + 轮次归并 + mapsTo 溯源标注（文本保持转写原样，不改写——绿字段可溯源）。"""
    from agents.common import llm_available, llm_json
    if not llm_available() or not segments:
        return None
    lines = "\n".join(f"[{i+1}] {s['text']}" for i, s in enumerate(segments))
    messages = [
        {"role": "system", "content": "你是医疗转写整理员。输出纯 JSON，不改写、不增删任何原文文字。"},
        {"role": "user", "content": (
            f"以下是医患问诊录音的 ASR 分段（含方言，[n]为段号）：\n{lines}\n\n"
            "任务：1) 判定每段说话人（医生/患者）；2) 同一说话人连续内容合并为一轮；"
            "**一个段号内若混有医生问和患者答（无说话人分离，常见），必须按语义拆成多轮**，"
            "拆分点逐字保留原文；3) 每轮标注 mapsTo：仅当该轮对话明确支撑某病历字段时才标"
            "（宁可少标不错标；医生告知手术安排≠手术外伤史，手术外伤史指患者既往手术经历），"
            f"从这个词表里选，可为空数组：{json.dumps(sorted(labels_vocab), ensure_ascii=False)}\n"
            "输出 JSON：{\"turns\":[{\"role\":\"医生|患者\",\"text\":\"逐字保留的原文（含ASR错字）\","
            "\"mapsTo\":[\"字段标签\"]}]}")},
    ]
    try:
        data = llm_json(messages, max_tokens=4000)
        turns = []
        for t in data.get("turns", []):
            if not str(t.get("text", "")).strip():
                continue
            turns.append({"role": t.get("role") if t.get("role") in ("医生", "患者") else "患者",
                          "text": str(t["text"]).strip(),
                          "mapsTo": [m for m in (t.get("mapsTo") or []) if m in labels_vocab]})
        return turns or None
    except Exception as e:
        print(f"  ⚠ LLM 角色标注失败（降级启发式）：{e}", file=sys.stderr)
        return None


def tag_roles_heuristic(segments):
    """启发式降级：疑问/查体指令句→医生，其余→患者；相邻同角色合并。"""
    turns = []
    for s in segments:
        role = "医生" if _HEUR_DOC.search(s["text"]) else "患者"
        if turns and turns[-1]["role"] == role:
            turns[-1]["text"] += s["text"]
        else:
            turns.append({"role": role, "text": s["text"], "mapsTo": []})
    return turns


# ---------------- 红线扫描 ----------------
def redline_scan(payload_str, facts, staff_names, folder_name):
    """输出前逐 token 扫描：真实姓名/住院号/手机号/证件号，命中即拒写。返回命中清单。"""
    hits = []
    tokens = set(facts["patient_names"]) | {folder_name} | set(facts["patient_ids"]) | set(staff_names)
    for t in tokens:
        t = str(t).strip()
        if t and len(t) >= 2 and t in payload_str:
            hits.append(t)
    for pat, name in ((r"1[3-9]\d{9}", "手机号"), (r"\d{17}[0-9Xx]", "证件号")):
        if re.search(pat, payload_str):
            hits.append(name)
    return hits


def deid_text(text, facts, staff_names, display):
    """文本脱敏：患者姓名→代号，医护姓名→姓+医生/护士。"""
    for n in facts["patient_names"]:
        text = text.replace(n, display)
    for n, role in staff_names.items():
        text = text.replace(n, f"{n[0]}{role}")
    return text


def main():
    ap = argparse.ArgumentParser(description="真实案例接入传送带（HIS 8 表 + 转写稿 → 脱敏 demo 数据）")
    ap.add_argument("case_dir", help="案例目录（git 忽略，如 病历资料/住院/<姓名>）")
    ap.add_argument("--code", required=True, help="平台患者编号（如 DA0006）")
    ap.add_argument("--admit", help="入院日 YYYYMMDD（缺省自动探测生命体征'入院'事件）")
    ap.add_argument("--discharge", help="出院日 YYYYMMDD（缺省自动探测医嘱'出院'）")
    ap.add_argument("--template", default=TEMPLATE_CODE_DEFAULT, help="骨架模板患者编号（默认 DA0001）")
    ap.add_argument("--transcript", help="转写稿路径（缺省取 案例目录/录音/转写稿/ 最新）")
    ap.add_argument("--no-llm", action="store_true", help="角色标注不用 LLM（启发式降级）")
    ap.add_argument("--dry-run", action="store_true", help="只出接入报告，不写 demo/data")
    args = ap.parse_args()

    case_dir = ensure_ignored(args.case_dir)
    folder_name = os.path.basename(os.path.normpath(case_dir))
    load_llm_env()

    mapping = evaluate_v0._patient_code_map()
    display = mapping.get(folder_name)
    if not display:
        fail(f"患者代号映射缺失：请在 病历资料/患者代号映射.json 增加 \"{folder_name}\": \"患者X\"（该文件不入 Git）")

    print(f"== 案例接入：{display}（{args.code}）==")
    views_raw = evaluate_v0.load_his_views(case_dir)
    n_rows = {k: len(v) for k, v in views_raw.items()}
    print(f"  HIS 8 表：{n_rows}")
    facts = detect_facts(views_raw)
    if args.admit:
        facts["admit_dt"] = datetime.datetime.strptime(args.admit, "%Y%m%d")
    if args.discharge:
        facts["discharge_dt"] = datetime.datetime.strptime(args.discharge, "%Y%m%d")
    if not facts["admit_dt"]:
        fail("未能探测入院时间（生命体征无'入院'事件行）——请用 --admit 指定")
    stay_date = facts["admit_dt"].date()
    disch_str = f"{facts['discharge_dt']:%Y-%m-%d}" if facts["discharge_dt"] else "未探测到"
    print(f"  入院 {facts['admit_dt']:%Y-%m-%d %H:%M} · 出院 {disch_str}"
          f" · 科室 {facts['dept'] or '?'} · 诊断线索 {facts['clinic_diags'][:4]}")

    # 住院窗口过滤（入院记录口径 ±3 天，与评测同规则）→ demo his 节
    views_win = generate_multiagent._filter_views_by_stay(views_raw, facts["admit_dt"], "入院记录")
    his = build_his_node(views_win, facts)
    print(f"  his 节：体征 {len(his['vitals'])} / 检查 {len(his['exams'])} / 异常检验 {len(his['abnormal_labs'])} / 医嘱 {len(his['orders'])}")

    # 骨架文书（模板字段标签，值清空）
    patients = json.load(open(PATIENTS_JSON, encoding="utf-8"))
    tpl = next((p for p in patients if p["code"] == args.template), None)
    if not tpl or not tpl["admissions"]:
        fail(f"模板患者 {args.template} 不在 patients.json")
    docs = build_skeleton_docs(tpl["admissions"][0], facts, display)
    print(f"  骨架文书：{[(d['type'], d['title'], len(d['fields'])) for d in docs]}")

    # 转写稿 → 角色标注对话
    labels_vocab = set()
    for d in docs:
        if d["type"] == "入院记录":
            labels_vocab |= {f["label"] for f in d["fields"] if f["label"]}
    labels_vocab = {l for l in labels_vocab if l in evaluate_v0.GREEN or l in ("辅助检查", "初步诊断")}
    segments = _segments(case_dir)
    turns = None if args.no_llm else tag_roles_llm(segments, labels_vocab)
    used_llm = turns is not None
    if turns is None:
        turns = tag_roles_heuristic(segments)
    print(f"  对话：{len(turns)} 轮（{'LLM 角色标注' if used_llm else '启发式降级'}）")

    # 脱敏 + 红线扫描
    staff_names = collect_staff_names(views_raw)
    entry = {"code": args.code, "display": display,
             "profile": f"{facts['sex']} · {facts['age']} · {facts['dept']}".strip(" ·"),
             "admissions": [{
                 "idx": 1,
                 "admit_ts": facts["admit_dt"].strftime("%Y%m%d"),
                 "discharge_ts": (facts["discharge_dt"] or facts["admit_dt"]).strftime("%Y%m%d"),
                 "visit_id": 1, "disease": facts["disease"], "doc_count": len(docs),
                 "interactive": True, "dialogue": f"dialogue_{args.code}.json",
                 "real_recording": True,
                 "docs": docs, "his": his}]}
    dialogue = [{"role": t["role"], "text": deid_text(t["text"], facts, staff_names, display),
                 "mapsTo": t["mapsTo"]} for t in turns]
    payload = json.dumps(entry, ensure_ascii=False) + json.dumps(dialogue, ensure_ascii=False)
    hits = redline_scan(payload, facts, staff_names, folder_name)
    if hits:
        fail(f"红线扫描命中，拒绝写入 demo/data：{hits}（请检查数据来源后重试）")
    print("  红线扫描：0 命中 ✓")

    # 接入报告（留在案例目录=git 忽略区，可含真实值供人工核对）
    report = [f"# 案例接入报告 · {folder_name} → {display}（{args.code}）", "",
              f"- 接入时间：{datetime.datetime.now():%Y-%m-%d %H:%M}",
              f"- 入院：{facts['admit_dt']:%Y-%m-%d %H:%M}" + (f" · 出院：{facts['discharge_dt']:%Y-%m-%d}" if facts["discharge_dt"] else ""),
              f"- 科室：{facts['dept'] or '?'} · 诊断线索：{'；'.join(facts['clinic_diags'][:6]) or '无'}",
              f"- HIS 行数：{n_rows}",
              f"- his 节：体征 {len(his['vitals'])} / 检查 {len(his['exams'])} / 异常检验 {len(his['abnormal_labs'])} / 医嘱 {len(his['orders'])}",
              f"- 对话：{len(turns)} 轮（{'LLM' if used_llm else '启发式'}角色标注）· 红线扫描 0 命中",
              f"- 骨架文书：{', '.join(d['type'] + (('(' + d['title'] + ')') if d['title'] != d['type'] else '') for d in docs)}",
              "", "## 对话（角色标注后，脱敏前原文——本文件不入 Git）", ""]
    report += [f"**{t['role']}**：{t['text']}" + (f"　`→ {t['mapsTo']}`" if t.get("mapsTo") else "")
               for t in turns]
    rpath = os.path.join(case_dir, "接入报告.md")
    with open(rpath, "w", encoding="utf-8") as f:
        f.write("\n".join(report) + "\n")
    print(f"  接入报告（git 忽略区）：{os.path.relpath(rpath, REPO)}")

    if args.dry_run:
        print("  --dry-run：未写入 demo/data")
        return

    # 落盘：patients.json 幂等替换 + dialogue_<code>.json
    patients = [p for p in patients if p["code"] != args.code] + [entry]
    patients.sort(key=lambda p: p["code"])
    with open(PATIENTS_JSON, "w", encoding="utf-8") as f:
        json.dump(patients, f, ensure_ascii=False, indent=1)
    dpath = os.path.join(DEMO_DATA, f"dialogue_{args.code}.json")
    with open(dpath, "w", encoding="utf-8") as f:
        json.dump(dialogue, f, ensure_ascii=False, indent=1)
    print(f"  ✓ 已写入 {os.path.relpath(PATIENTS_JSON, REPO)}（{len(patients)} 位患者）")
    print(f"  ✓ 已写入 {os.path.relpath(dpath, REPO)}（{len(dialogue)} 轮）")
    print("\n下一步：启动服务体验真实案例流程 → MRA_STATIC_DIR=\"$PWD/demo\" python3 server/llm_api.py")


if __name__ == "__main__":
    main()
