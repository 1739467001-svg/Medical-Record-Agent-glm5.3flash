# -*- coding: utf-8 -*-
"""
一键回归（PRD 12.2-6 双周回归自动化，P2-H）
====================================================================
一键完成：三类文书 × 多智能体真实评测 → 报告落盘（reports/，md+json）→
与上期回归报告 diff（按文书类型对比指标与字段级命中/未命中变化）→ diff 结论写回新报告。

用法：
    python3 run_regression.py                     # 全流程（需 .llm_env 中的 LLM 密钥）
    python3 run_regression.py --base 病历资料/住院 --types 入院记录,首次病程记录
    python3 run_regression.py --no-run            # 只对最近一期已有报告做 diff

产物：
    evaluation/reports/评测报告_multiagent_YYYY-MM-DD.md   （含"与上期回归对比"一节）
    evaluation/reports/评测报告_multiagent_YYYY-MM-DD.json（机器可读，供下期 diff）
"""
import os, sys, re, json, glob, argparse, datetime

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

REPORTS_DIR = os.path.join(HERE, "reports")

def load_llm_env():
    """evaluation/.llm_env → os.environ（一键运行免手动 source）。"""
    p = os.path.join(HERE, ".llm_env")
    if os.path.exists(p):
        for line in open(p, encoding="utf-8"):
            m = re.match(r'\s*export\s+([A-Z_]+)="(.*)"\s*$', line)
            if m:
                os.environ.setdefault(m.group(1), m.group(2))

# ---------------- 报告读取（json 本期/历史通用；md 兼容历史基线） ----------------
def _parse_md_report(path):
    """解析 evaluate_v0 生成的 md 报告 → 与 json 同构的指标 dict。
    字段级 hitset/missset 按类型归因：单类型报告精确归属；多类型报告无法拆分（标记 _shared，diff 时跳过字段级）。"""
    text = open(path, encoding="utf-8").read()
    m = re.search(r"日期：(\d{4}-\d{2}-\d{2})", text)
    date = m.group(1) if m else ""
    groups = {}
    cur = None
    for line in text.splitlines():
        hm = re.match(r"## (\S+)分住院次明细", line.strip())
        if hm:
            cur = hm.group(1); groups.setdefault(cur, {"rows": []}); continue
        rm = re.match(r"\|\s*(患者[A-Z])\s*\|\s*(\d+)\s*\|\s*(\d+)\s*\|\s*(\d+)\s*\|\s*(\d+)%\s*\|\s*(\d+)%\s*\|\s*(\d+)%\s*\|\s*(\d+)/(\d+)/(\d+)/(\d+)\s*\|", line)
        if rm and cur:
            code, G, F, H, cov, acc, grn, b, g, gr, y = rm.groups()
            groups[cur]["rows"].append({
                "code": code, "G": int(G), "F": int(F), "H": int(H),
                "coverage": int(cov) / 100, "accuracy": int(acc) / 100, "green_ratio": int(grn) / 100,
                "hit_labels": [], "miss_labels": []})

    # 字段级命中/未命中列表（"初步诊断(×11)、主诉(×10)"）
    hitset, missset = {}, {}
    mh = re.search(r"###\s*生成器命中的字段[^\n]*\n+([^\n]+)", text)
    mm = re.search(r"###\s*生成器尝试但未命中的字段[^\n]*\n+([^\n]+)", text)
    for rx, tgt in ((mh, hitset), (mm, missset)):
        if rx:
            for label, cnt in re.findall(r"([^、\s()]+)\(×(\d+)\)", rx.group(1)):
                tgt[label] = int(cnt)
    for t, gd in groups.items():
        rows = gd["rows"]
        if rows:
            n = len(rows)
            gd["coverage"] = sum(r["coverage"] for r in rows) / n
            gd["accuracy"] = sum(r["accuracy"] for r in rows) / n
            gd["green_ratio"] = sum(r["green_ratio"] for r in rows) / n
            gd["n"] = n
    fieldsets = {}
    if len(groups) == 1:
        only = next(iter(groups))
        fieldsets[only] = {"hitset": hitset, "missset": missset}
    elif groups:
        for t in groups:
            fieldsets[t] = {"hitset": {}, "missset": {}, "_shared": True}
    return {"date": date, "source": os.path.basename(path), "groups": groups, "fieldsets": fieldsets}

def _parse_json_report(path):
    """json 报告：字段级命中/未命中由逐行 hit_labels/miss_labels 精确聚合（按文书类型）。"""
    d = json.load(open(path, encoding="utf-8"))
    fieldsets = {}
    for t, gd in d.get("groups", {}).items():
        hs, ms = {}, {}
        for r in gd.get("rows", []):
            for l in r.get("hit_labels", []): hs[l] = hs.get(l, 0) + 1
            for l in r.get("miss_labels", []): ms[l] = ms.get(l, 0) + 1
        fieldsets[t] = {"hitset": hs, "missset": ms}
    return {"date": d.get("date", ""), "source": os.path.basename(path), "groups": d.get("groups", {}),
            "fieldsets": fieldsets}

def load_previous_reports(generator, exclude):
    """收集上期报告（根目录历史基线 + reports/ 历史），按文书类型合并（同类型取日期最新）。
    exclude：当期报告 md/json 路径集合，避免本期与自身对比。
    返回 (merged, sources)：merged[type] = 指标 + hitset/missset + _date/_source。"""
    pats = [os.path.join(HERE, f"评测报告_{generator}*.md"), os.path.join(HERE, f"评测报告_{generator}*.json"),
            os.path.join(REPORTS_DIR, f"评测报告_{generator}*.md"), os.path.join(REPORTS_DIR, f"评测报告_{generator}*.json")]
    excl = {os.path.abspath(p) for p in exclude}
    files = [f for f in glob.glob(pats[0]) + glob.glob(pats[1]) + glob.glob(pats[2]) + glob.glob(pats[3])
             if os.path.abspath(f) not in excl]
    merged, sources = {}, []
    for f in sorted(files):
        try:
            d = _parse_json_report(f) if f.endswith(".json") else _parse_md_report(f)
        except Exception as e:
            print(f"  （跳过无法解析的上期报告 {os.path.basename(f)}：{e}）")
            continue
        if d["source"] not in sources:
            sources.append(d["source"])
        for dtype, gd in d["groups"].items():
            prev = merged.get(dtype)
            if prev is None or (d["date"] or "") >= (prev.get("_date") or ""):
                fs = d["fieldsets"].get(dtype, {"hitset": {}, "missset": {}})
                merged[dtype] = {**gd, "_date": d["date"], "_source": d["source"],
                                 "hitset": fs.get("hitset", {}), "missset": fs.get("missset", {})}
    return merged, sources

# ---------------- diff 渲染（按文书类型对比） ----------------
def render_diff(cur, prev, sources):
    """cur/prev: {dtype: {coverage, accuracy, green_ratio, n, rows, hitset, missset}}。"""
    lines = []
    lines.append("## 与上期回归对比（PRD 12.2-6 双周回归）\n")
    lines.append("- 上期基线：" + ("、".join(sources) if sources else "（无上期数据，本期为首期回归基线）"))
    fmt = lambda x: f"{x*100:.1f}%"
    lines.append("\n### 指标对比（按文书类型）\n")
    lines.append("| 文书类型 | 覆盖率 | 准确率 | 录音依赖度 |")
    lines.append("|---|---|---|---|")
    for dtype in sorted(cur):
        c, p = cur[dtype], prev.get(dtype)
        if not p:
            lines.append(f"| {dtype} | {fmt(c['coverage'])}（首期） | {fmt(c['accuracy'])}（首期） | {fmt(c['green_ratio'])} |")
            continue
        def cell(key):
            dpp = (c[key] - p[key]) * 100
            arrow = "↑" if dpp > 0.05 else ("↓" if dpp < -0.05 else "→")
            return f"{fmt(p[key])} → {fmt(c[key])}（{dpp:+.1f}pp {arrow}）"
        lines.append(f"| {dtype} | {cell('coverage')} | {cell('accuracy')} | {cell('green_ratio')} |")
    lines.append("\n### 字段级变化（按文书类型）\n")
    any_change = False
    for dtype in sorted(cur):
        c, p = cur[dtype], prev.get(dtype)
        if not p:
            lines.append(f"- **{dtype}**：首期，无上期基线。")
            continue
        ch, cm = c.get("hitset", {}), c.get("missset", {})
        ph, pm = p.get("hitset", {}), p.get("missset", {})
        labels = sorted(set(ph) | set(pm) | set(ch) | set(cm))
        gained = [(l, ch.get(l, 0) - ph.get(l, 0)) for l in labels if ch.get(l, 0) > ph.get(l, 0)]
        lost = [(l, ch.get(l, 0) - ph.get(l, 0)) for l in labels if ch.get(l, 0) < ph.get(l, 0)]
        resolved = [(l, pm.get(l, 0) - cm.get(l, 0)) for l in labels if cm.get(l, 0) < pm.get(l, 0)]
        regressed = [(l, cm.get(l, 0) - pm.get(l, 0)) for l in labels if cm.get(l, 0) > pm.get(l, 0)]
        mt = (sum(cm.values()), sum(pm.values()))
        tail = "：" if (gained or resolved or regressed or lost) else "。"
        lines.append(f"- **{dtype}**：「尝试未命中」{mt[1]} → {mt[0]}（{mt[0]-mt[1]:+d}）{tail}")
        if gained:
            lines.append("  - 命中提升：" + "、".join(f"{l}(×{ph.get(l,0)}→×{ch[l]})" for l, _ in sorted(gained, key=lambda x: -x[1])))
        if resolved:
            lines.append("  - 未命中消解：" + "、".join(f"{l}(×{pm[l]}→×{cm.get(l,0)})" for l, _ in sorted(resolved, key=lambda x: -x[1])))
        if regressed:
            lines.append("  - ⚠ 新增未命中：" + "、".join(f"{l}(×{pm.get(l,0)}→×{cm[l]})" for l, _ in sorted(regressed, key=lambda x: -x[1])))
        if lost:
            lines.append("  - ⚠ 命中退化：" + "、".join(f"{l}(×{ph[l]}→×{ch.get(l,0)})" for l, _ in sorted(lost, key=lambda x: x[1])))
        if gained or resolved or regressed or lost:
            any_change = True
    if not any_change:
        lines.append("- 字段级命中无变化。")
    return lines

# ---------------- 主流程 ----------------
def main():
    ap = argparse.ArgumentParser(description="一键三类文书评测 + 与上期报告 diff（PRD 12.2-6）")
    ap.add_argument("--base", default=os.path.join(os.path.dirname(HERE), "病历资料", "住院"))
    ap.add_argument("--generator", choices=["multiagent", "llm", "rules"], default="multiagent")
    ap.add_argument("--types", default="入院记录,出院记录,首次病程记录")
    ap.add_argument("--out", default=None, help="报告文件名（默认 评测报告_<generator>_<日期>.md）")
    ap.add_argument("--no-run", action="store_true", help="不重新生成，只 diff 最近一期已有报告")
    a = ap.parse_args()

    os.makedirs(REPORTS_DIR, exist_ok=True)
    today = datetime.date.today().isoformat()
    out_name = a.out or f"评测报告_{a.generator}_{today}.md"
    out_path = os.path.join(REPORTS_DIR, out_name)

    if not a.no_run:
        load_llm_env()
        import evaluate_v0
        types = tuple(t for t in a.types.split(",") if t)
        print(f"[1/3] 运行 {a.generator} 评测：{types} …（LLM 版约每份文书 2 次调用）")
        evaluate_v0.main(a.base, generator=a.generator, types=types, out=out_path)
        print(f"[2/3] 报告已写入 {out_path}")
    else:
        print("[1/3] --no-run：跳过生成")

    # 本期指标：优先刚生成的 json；--no-run 时取 reports/ 里最新一份
    cur_json = os.path.splitext(out_path)[0] + ".json"
    if not os.path.exists(cur_json):
        cands = sorted(glob.glob(os.path.join(REPORTS_DIR, f"评测报告_{a.generator}*.json")))
        if not cands:
            print("没有可对比的报告"); return
        cur_json = cands[-1]
    cur = _parse_json_report(cur_json)
    for dtype, fs in cur["fieldsets"].items():
        if dtype in cur["groups"]:
            cur["groups"][dtype]["hitset"] = fs.get("hitset", {})
            cur["groups"][dtype]["missset"] = fs.get("missset", {})
    prev, sources = load_previous_reports(a.generator, exclude={cur_json, os.path.splitext(cur_json)[0] + ".md"})
    if not prev:
        print("[3/3] 无上期报告可比——本期成为回归基线")
        return
    diff_lines = render_diff(cur["groups"], prev, sources)
    print("[3/3] 与上期对比：")
    for l in diff_lines:
        if l.startswith(("##", "###", "-", "|", "  -")):
            print("   " + l)
    # diff 结论写回报告 md
    md_path = os.path.splitext(cur_json)[0] + ".md"
    if os.path.exists(md_path):
        with open(md_path, "a", encoding="utf-8") as f:
            f.write("\n" + "\n".join(diff_lines) + "\n")
        print(f"   diff 已写回：{md_path}")

if __name__ == "__main__":
    main()
