# -*- coding: utf-8 -*-
"""热词表自动生长工具（Phase ③ 上下文偏置 · 纠错对沉淀）
============================================================
扫描语料库/评测报告里的转写产物，沉淀两类信号：

  ① **热词命中**（`hotwords_applied`：错→对×N）——现有规则有效性的直接证据（按族统计）；
  ② **LLM 方言纠错对**（`corrections`：before→after）——未入表的高频讹变候选。

聚合去重后输出候选清单（人工审核），审核通过（`approve: true`）后 `--merge` 写入
`mcp-server/medical_hotwords.json`（自动备份；跳过已存在/超长/自相矛盾项）。

用法：
    python3 tools/hotword_harvest.py --scan 病历资料/ASR语料库 病历资料/住院 病历资料/门诊
    python3 tools/hotword_harvest.py --scan <目录...> --out 病历资料/ASR语料库/候选热词.md
    python3 tools/hotword_harvest.py --merge 病历资料/ASR语料库/候选热词.json

数据安全：只读转写产物（git 忽略区），候选清单默认落语料库目录；不复制音频。
"""
import argparse, glob, json, os, re, shutil, sys, time

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HOTWORDS = os.path.join(REPO, "mcp-server", "medical_hotwords.json")
HIT_RE = re.compile(r"^(.*?)→(.*?)×(\d+)$")


def load_rules():
    d = json.load(open(HOTWORDS, encoding="utf-8"))
    return d.get("规则（勿改本键）", d)


def scan_reports(dirs):
    """找评测报告明细 JSON（<报告>.md.json）。"""
    files = []
    for d in dirs:
        files += glob.glob(os.path.join(d, "**", "*.md.json"), recursive=True)
    return sorted(set(files))


def harvest(files):
    hits, corr = {}, {}  # key -> {"count": n, "sources": set()}
    for f in files:
        try:
            data = json.load(open(f, encoding="utf-8"))
        except Exception:
            continue
        if not isinstance(data, list):
            continue
        for item in data:
            if not isinstance(item, dict):
                continue
            for h in item.get("hotwords_applied") or []:
                m = HIT_RE.match(str(h))
                if m:
                    key = f"{m.group(1)}→{m.group(2)}"
                    e = hits.setdefault(key, {"count": 0, "sources": set()})
                    e["count"] += int(m.group(3))
                    e["sources"].add(f)
            for c in item.get("corrections") or []:
                if not isinstance(c, dict):
                    continue
                b, a = str(c.get("before") or "").strip(), str(c.get("after") or "").strip()
                if b and a and b != a:
                    e = corr.setdefault(f"{b}→{a}", {"count": 0, "sources": set(), "before": b, "after": a})
                    e["count"] += 1
                    e["sources"].add(f)
    return hits, corr


def main():
    ap = argparse.ArgumentParser(description="热词表自动生长（纠错对沉淀）")
    ap.add_argument("--scan", nargs="+", metavar="目录", help="扫描目录（递归找 *.md.json 评测明细）")
    ap.add_argument("--out", default="", help="候选清单输出路径（缺省 病历资料/ASR语料库/候选热词.md）")
    ap.add_argument("--merge", metavar="候选JSON", help="合并审核通过的候选（approve: true）进热词表")
    a = ap.parse_args()

    if a.merge:
        cand = json.load(open(a.merge, encoding="utf-8"))
        rules = load_rules()
        add = {}
        if isinstance(cand, dict) and "corrections" in cand:   # 本工具产出的候选清单
            items = cand["corrections"]
        elif isinstance(cand, list):                           # 纯 corrections 列表
            items = cand
        elif isinstance(cand, dict):                           # 纯 {错: 对} 映射
            items = []
            add = {k: v for k, v in cand.items() if isinstance(v, str)}
        else:
            items = []
        for c in items:
            if isinstance(c, dict) and c.get("approve") and c.get("before") and c.get("after"):
                add[c["before"]] = c["after"]
        added = {k: v for k, v in add.items() if k not in rules}
        if not added:
            print("没有待合并项（approve 为 true 且未与现有规则重复）。")
            return
        bak = HOTWORDS + f".bak-{time.strftime('%Y%m%d%H%M%S')}"
        shutil.copy(HOTWORDS, bak)
        doc = json.load(open(HOTWORDS, encoding="utf-8"))
        key = "规则（勿改本键）" if "规则（勿改本键）" in doc else None
        target = doc[key] if key else doc
        target.update(added)
        json.dump(doc, open(HOTWORDS, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
        print(f"✓ 已合并 {len(added)} 条规则（备份：{os.path.basename(bak)}）")
        for k, v in sorted(added.items()):
            print(f"    {k} → {v}")
        return

    if not a.scan:
        ap.print_help()
        return
    files = scan_reports(a.scan)
    hits, corr = harvest(files)
    rules = load_rules()
    cand = [{"before": v["before"], "after": v["after"], "count": v["count"],
             "sources": len(v["sources"]), "approve": False}
            for k, v in corr.items() if v["before"] not in rules
            and len(v["before"]) <= 8 and len(v["after"]) <= 8]
    cand.sort(key=lambda c: (-c["count"], c["before"]))

    out = a.out or os.path.join(REPO, "病历资料", "ASR语料库", "候选热词.md")
    lines = ["# 热词/纠错候选清单（自动沉淀，待人工审核）", "",
             f"- 扫描评测明细：{len(files)} 份　现有规则：{len(rules)} 条",
             "- 审核方式：在候选 JSON 里给通过的项标 `\"approve\": true`，再跑 "
             "`python3 tools/hotword_harvest.py --merge <候选JSON>`", "",
             "## 一、热词命中（现有规则有效性证据）", "",
             "| 规则 | 命中次数 | 来源报告数 |", "|---|---|---|"]
    for k, v in sorted(hits.items(), key=lambda kv: -kv[1]["count"]):
        lines.append(f"| {k} | {v['count']} | {len(v['sources'])} |")
    lines += ["", "## 二、纠错对候选（未入表，按频次排序）", "",
              "| 原文 | 建议纠正 | 次数 | 来源报告数 | 审核 |", "|---|---|---|---|---|"]
    for c in cand:
        lines.append(f"| {c['before']} | {c['after']} | {c['count']} | {c['sources']} |  |")
    os.makedirs(os.path.dirname(out), exist_ok=True)
    open(out, "w", encoding="utf-8").write("\n".join(lines) + "\n")
    json.dump({"hotword_hits": {k: v["count"] for k, v in hits.items()}, "corrections": cand},
              open(os.path.splitext(out)[0] + ".json", "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)
    print(f"✓ 候选清单：{os.path.relpath(out, REPO)}（热词命中 {len(hits)} 条 / 纠错候选 {len(cand)} 条）")
    print(f"  候选 JSON：{os.path.relpath(os.path.splitext(out)[0] + '.json', REPO)}")


if __name__ == "__main__":
    main()
