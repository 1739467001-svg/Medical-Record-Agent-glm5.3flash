# -*- coding: utf-8 -*-
"""评测趋势汇总（P2-M）：解析 evaluation/reports/ 下历次 multiagent 评测 JSON，
生成跨期趋势表（覆盖率/准确率/较上期变化），供双周汇报与模型迭代对照。

用法（零依赖，生成 reports/评测趋势汇总.md）：
    python3 evaluation/trend_report.py
"""
import json, os, re, sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPORTS = os.path.join(HERE, "reports")
OUT = os.path.join(REPORTS, "评测趋势汇总.md")
TYPES = ("入院记录", "首次病程记录", "出院记录")

def load_reports():
    """按日期升序加载历次 评测报告_multiagent_*.json（同日去重取最后）。"""
    files = {}
    for f in os.listdir(REPORTS):
        m = re.match(r"评测报告_multiagent_(.+?)\.json$", f)
        if m:
            files[m.group(1)] = os.path.join(REPORTS, f)   # 同期覆盖 → 取字典序最后
    out = []
    for day in sorted(files):
        try:
            d = json.load(open(files[day], encoding="utf-8"))
            out.append({"date": d.get("date", day), "gen": d.get("gen_name", d.get("generator", "")),
                        "groups": d.get("groups", {})})
        except Exception as e:
            print(f"⚠ 跳过无法解析的报告 {f}: {e}", file=sys.stderr)
    return out

def pct(x):
    return f"{x * 100:.1f}%" if isinstance(x, (int, float)) else "—"

def main():
    reports = load_reports()
    if not reports:
        print("暂无评测报告 JSON（evaluation/reports/评测报告_multiagent_*.json）")
        return
    lines = ["# 评测趋势汇总（历次 multiagent 评测对照）", "",
             f"- 报告期数：{len(reports)}（{reports[0]['date']} → {reports[-1]['date']}）",
             "- 生成方式：`python3 evaluation/trend_report.py`（每次 run_regression 后重跑刷新本页）", ""]
    for t in TYPES:
        lines.append(f"## {t}")
        have = [r for r in reports if t in r["groups"]]
        if not have:
            lines.append("\n（暂无数据）\n")
            continue
        lines += ["", "| 日期 | 覆盖率 | 准确率 | 较上期 | 样本 n |", "|---|---|---|---|---|"]
        prev = None
        for r in have:
            g = r["groups"][t]
            cov, acc = g.get("coverage"), g.get("accuracy")
            if prev is None:
                delta = "基线"
            else:
                dc = (cov - prev[0]) * 100 if (cov is not None and prev[0] is not None) else 0
                da = (acc - prev[1]) * 100 if (acc is not None and prev[1] is not None) else 0
                delta = f"覆盖 {dc:+.1f}pp · 准确 {da:+.1f}pp"
            lines.append(f"| {r['date']} | {pct(cov)} | {pct(acc)} | {delta} | {g.get('n', '—')} |")
            prev = (cov, acc)
        lines.append("")
    lines += ["---", "", "*附：指标口径见 PRD 7.1（coverage=金标准字段命中占比，accuracy=命中字段中"
              " 文本匹配≥0.6 的占比）；「较上期」对比同文书类型上一期。*"]
    with open(OUT, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    print(f"✓ 趋势汇总已生成：evaluation/reports/评测趋势汇总.md（{len(reports)} 期）")

if __name__ == "__main__":
    main()
