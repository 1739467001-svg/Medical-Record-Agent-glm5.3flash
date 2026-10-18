# -*- coding: utf-8 -*-
"""热词规则全语料审计器（风险#3 防线 · 双向核查）
====================================================
热词规则与白名单是"人工判定 + 实测回归"攒出来的，随语料增长会累积两类新风险：

  ① **漏放误伤**（规则把正确的话改错，如 提个→体格 撞上"给你提个早下手术"）
     —— 判据：该处未受白名单保护（规则生效），且**参照稿里存在原错词**。
  ② **过度拦截**（白名单把本该修正的错写保护住了，如"提个检查"该修没修）
     —— 判据：该处被白名单拦下，且**参照稿里存在正确词**。

本工具把语料库里所有转写产物与参照稿拉齐，对**每一条规则 × 每一处出现**逐点判定，
输出「命中清单（带上下文）＋双向风险信号＋白名单建议」，作为发版前的规则体检关卡。

用法：
    python3 tools/hotword_audit.py                     # 全语料审计 → 报告 + JSON
    python3 tools/hotword_audit.py --roots 病历资料/门诊 病历资料/住院
    python3 tools/hotword_audit.py --fail-on-risk      # 发现未受保护风险信号则退出码 1（CI 关卡）
    python3 tools/hotword_audit.py --suggest           # 额外输出可直接抄进白名单的建议条目

数据安全：只读 git 忽略区内的转写产物与参照稿（不复制音频），报告同样落忽略区。
判定口径说明：参照稿按"整篇字面包含"判定（不做位置对齐）——存在即视为该词在真实语境
合法使用，属保守判据（宁可多报人工复核，不漏报误伤）。
"""
import argparse, glob, json, os, re, sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "mcp-server"))
import asr_mcp_server as mcp  # noqa: E402

CORPUS_DIR = os.path.join(REPO, "病历资料", "ASR语料库")
DEFAULT_ROOTS = ["病历资料"]
DRAFT_LINE_RE = re.compile(r"^\[[^]]*\]\s*(.+)$")
# 参照稿候选文件名（人工校订的理解稿/标注真值）
REF_NAME_HINTS = ("对照理解稿", "标注.json", "事实清单.json")


def _rel(p):
    return os.path.relpath(p, REPO)


def gather_transcripts(roots):
    """收集语料库内全部转写产物文本：评测明细 JSON / 转写草稿 txt / 转写稿 json。"""
    items = []
    for root in roots:
        base = os.path.join(REPO, root)
        for pat, kind in (("**/*.md.json", "评测明细"), ("**/转写草稿/*.txt", "转写草稿"),
                          ("**/转写稿/*.json", "转写稿"), ("**/原始ASR_*.json", "原始ASR")):
            for f in glob.glob(os.path.join(base, pat), recursive=True):
                if os.path.basename(f).startswith("INDEX") or "候选热词" in f:
                    continue
                try:
                    if f.endswith(".json"):
                        d = json.load(open(f, encoding="utf-8"))
                        texts = []
                        if isinstance(d, list):
                            for e in d:
                                if isinstance(e, dict):
                                    # 每条只取一个版本：优先纠错前原文（热词规则的真实输入），
                                    # 否则取纠错后文本——两者同取会把同一处命中重复计数
                                    texts.append(e.get("text_raw") or e.get("text") or "")
                        elif isinstance(d, dict) and "turns" in d:      # 标注真值不是转写产物
                            continue
                        elif isinstance(d, dict):
                            texts = [d.get("text_raw") or d.get("text") or ""]
                        for t in texts:
                            if t and len(t) >= 8:
                                items.append({"file": _rel(f), "kind": kind, "text": t})
                    else:
                        lines = [m.group(1) for m in
                                 (DRAFT_LINE_RE.match(ln) for ln in open(f, encoding="utf-8"))
                                 if m]
                        t = "".join(lines)
                        if len(t) >= 8:
                            items.append({"file": _rel(f), "kind": kind, "text": t})
                except Exception as e:
                    print(f"  ! 跳过 {_rel(f)}：{e}", file=sys.stderr)
    return items


def gather_references(roots):
    """收集参照稿（人工校订/标注真值）文本，用于双向判据。"""
    refs = []
    for root in roots:
        base = os.path.join(REPO, root)
        for f in glob.glob(os.path.join(base, "**", "*"), recursive=True):
            name = os.path.basename(f)
            if not any(h in name for h in REF_NAME_HINTS):
                continue
            try:
                if f.endswith(".json"):
                    d = json.load(open(f, encoding="utf-8"))
                    if isinstance(d, dict) and "turns" in d:
                        t = "".join(str(x.get("text") or "") for x in d["turns"])
                    elif isinstance(d, dict):
                        t = json.dumps(d, ensure_ascii=False)
                    else:
                        t = json.dumps(d, ensure_ascii=False)
                else:
                    t = open(f, encoding="utf-8").read()
                refs.append({"file": _rel(f), "text": t})
            except Exception:
                continue
    return refs


def _find_occurrences(text, wrong):
    """返回该词在文本中的全部起始位置。"""
    out, i = [], text.find(wrong)
    while i != -1:
        out.append(i)
        i = text.find(wrong, i + 1)
    return out


def audit_text(text, source, rules, guards, refs):
    """对单篇文本逐规则逐出现点判定，返回命中记录列表。

    每条记录：规则、位置、是否被白名单拦下、上下文、以及"是否命中参照判据"。
    """
    hits = []
    for wrong, right in rules.items():
        for pos in _find_occurrences(text, wrong):
            ctx_lo, ctx_hi = max(0, pos - 12), min(len(text), pos + len(wrong) + 12)
            ctx = text[ctx_lo:ctx_hi]
            guarded = False
            prots = guards.get(wrong) or []
            if prots:
                # 与 _replace_guarded 同口径：保护短语前缀对齐 或 命中前后 6 字窗口包含
                if any(p.startswith(wrong) and text.startswith(p, pos) for p in prots):
                    guarded = True
                else:
                    w_lo, w_hi = max(0, pos - 6), min(len(text), pos + len(wrong) + 6)
                    if any(p in text[w_lo:w_hi] for p in prots):
                        guarded = True
            ref_has_wrong = [r["file"] for r in refs if wrong in r["text"]]
            ref_has_right = [r["file"] for r in refs if right in r["text"]]
            hits.append({
                "rule": f"{wrong}→{right}", "wrong": wrong, "right": right,
                "len": len(wrong), "source": source, "pos": pos, "context": ctx,
                "guarded": guarded, "short_rule": len(wrong) <= 2,
                "ref_has_wrong": ref_has_wrong, "ref_has_right": ref_has_right,
            })
    return hits


def classify(hits):
    """双向风险判定：漏放误伤（规则生效但参照含原错词）/ 过度拦截（被拦但参照含正确词）。"""
    miss = [h for h in hits if not h["guarded"] and h["ref_has_wrong"]]
    over = [h for h in hits if h["guarded"] and h["ref_has_right"] and not h["ref_has_wrong"]]
    return miss, over


def review_list(hits, guards):
    """未登记白名单的高风险短规则（≤2 字且有生效命中）→ 逐条附上下文，供人工判读。

    注意：短规则**未登记白名单本身不一定有问题**——仅当错词形在汉语里也是合法词/合法
    搭配时才需要保护（如"提个"见于"给你提个醒"）；若错词形并非合法词（如"顿疼""恶信"），
    无保护是正常状态。故此处只列证据不做自动结论，人工按上下文判断。
    """
    out = {}
    for h in hits:
        if h["short_rule"] and not h["guarded"] and h["wrong"] not in guards:
            out.setdefault(h["wrong"], {"right": h["right"], "contexts": []})
            if len(out[h["wrong"]]["contexts"]) < 5:
                out[h["wrong"]]["contexts"].append(f"{_short(h['source'])}: …{h['context']}…")
    return out


def suggest_guards(hits):
    """为**误伤证据**生成白名单候选：只取「规则生效 且 参照稿保留了原错词形」的点
    （即 classify 判定的漏放误伤）。参照稿已改写为正确词的位置（真错写）恰恰**不该**
    保护——早期版本从全部命中取候选，会把"平时踢个挺好"（应修）也提为保护，造成过度拦截。
    函数自身强制该口径（不只靠调用方传入 miss），避免误用。"""
    cand = {}
    for h in hits:
        w, ctx = h["wrong"], h["context"]
        if not h["short_rule"] or h["guarded"] or not h["ref_has_wrong"]:
            continue
        i = ctx.find(w)
        if i < 0:
            continue
        pre = ctx[max(0, i - 2):i]
        post = ctx[i + len(w):i + len(w) + 2]
        for c in {pre + w, w + post, pre + w + post}:
            if 2 <= len(c) <= 6 and not any(ch in c for ch in "，。？！、,."):
                cand.setdefault(w, {})
                cand[w][c] = cand[w].get(c, 0) + 1
    return {w: sorted(d, key=lambda k: -d[k])[:4] for w, d in cand.items()}


def main():
    ap = argparse.ArgumentParser(description="热词规则全语料审计（风险#3 双向核查）")
    ap.add_argument("--roots", nargs="*", default=DEFAULT_ROOTS, help="扫描根目录（缺省 病历资料）")
    ap.add_argument("--out", default="", help="报告路径（缺省 病历资料/ASR语料库/热词规则审计.md）")
    ap.add_argument("--json", default="", help="明细 JSON 路径（缺省 同报告 .json）")
    ap.add_argument("--fail-on-risk", action="store_true", help="存在未受保护的风险信号时退出码 1")
    ap.add_argument("--suggest", action="store_true", help="输出白名单建议条目")
    a = ap.parse_args()

    rules = mcp._load_hotwords()
    guards = mcp._load_hotword_guards()
    if not rules:
        print("❌ 热词表为空，无法审计", file=sys.stderr)
        return 2
    items = gather_transcripts(a.roots)
    refs = gather_references(a.roots)
    print(f"扫描转写产物 {len(items)} 份 / 参照稿 {len(refs)} 份 / 规则 {len(rules)} 条 / 白名单 {len(guards)} 条")

    all_hits = []
    for it in items:
        all_hits += audit_text(it["text"], it["file"], rules, guards, refs)
    miss, over = classify(all_hits)
    applied = [h for h in all_hits if not h["guarded"]]
    blocked = [h for h in all_hits if h["guarded"]]
    review = review_list(all_hits, guards)
    sug = suggest_guards(miss) if a.suggest else {}
    risky = sorted(review)

    lines = ["# 热词规则全语料审计（风险#3 双向核查）", "",
             f"- 扫描范围：{', '.join(a.roots)}　转写产物 **{len(items)}** 份　参照稿 **{len(refs)}** 份",
             f"- 规则 **{len(rules)}** 条　白名单 **{len(guards)}** 条　命中点 **{len(all_hits)}** 处"
             f"（生效 {len(applied)} / 白名单拦下 {len(blocked)}）",
             "- 判据（保守）：**漏放误伤**＝规则生效处参照稿里存在原错词；"
             "**过度拦截**＝被白名单拦下处参照稿里存在正确词。参照按整篇字面包含判定，不定位。", ""]
    lines += ["## 一、风险信号", "",
              f"- 🔴 **漏放误伤**：{len(miss)} 处" + ("（需补白名单或收紧规则）" if miss else "（无）"),
              f"- 🟡 **过度拦截**：{len(over)} 处" + ("（白名单需放宽或核对该规则）" if over else "（无）"), ""]
    for title, group in (("漏放误伤", miss), ("过度拦截", over)):
        if not group:
            continue
        lines += [f"### {title}（{len(group)} 处）", "",
                  "| 规则 | 来源 | 上下文 | 参照稿 |", "|---|---|---|---|"]
        for h in group[:40]:
            refs_note = "含原错词 " + "、".join(_short(r) for r in h["ref_has_wrong"]) if h["ref_has_wrong"] \
                else ("含正确词 " + "、".join(_short(r) for r in h["ref_has_right"]) if h["ref_has_right"] else "—")
            lines.append(f"| `{h['rule']}` | {_short(h['source'])} | …{h['context'].replace('|','｜')}… | {refs_note} |")
        lines.append("")
    lines += ["## 二、生效命中清单（规则确实改到了字）", ""]
    if applied:
        lines += ["| 规则 | 来源 | 上下文 |", "|---|---|---|"]
        for h in applied[:60]:
            lines.append(f"| `{h['rule']}` | {_short(h['source'])} | …{h['context'].replace('|','｜')}… |")
        if len(applied) > 60:
            lines.append(f"| … | 其余 {len(applied) - 60} 处见明细 JSON | |")
    else:
        lines.append("（无）")
    lines += ["", "## 三、白名单拦截清单（规则被守住没动）", ""]
    if blocked:
        lines += ["| 规则 | 来源 | 上下文 |", "|---|---|---|"]
        for h in blocked[:40]:
            lines.append(f"| `{h['rule']}` | {_short(h['source'])} | …{h['context'].replace('|','｜')}… |")
    else:
        lines.append("（无）")
    lines += ["", "## 四、待判读：未登记白名单的高风险短规则", "",
              "> 说明：短规则未登记白名单**不等于有问题**——仅当错词形在汉语里也是合法词/搭配时才需保护"
              "（如「提个」见于「提个醒」）；若错词形并非常用词（如「顿疼」「恶信」），无保护属正常。"
              "以下只列证据，人工按上下文判断是否补白名单。", ""]
    if review:
        for w, info in review.items():
            lines += [f"**`{w}` → {info['right']}**（{len(info['contexts'])} 处采样子集）", ""]
            for c in info["contexts"]:
                lines.append(f"- {c.replace('|','｜')}")
            lines.append("")
    else:
        lines += ["（无）", ""]
    lines += ["## 五、下一步", "",
              "- 漏放误伤 → 为该规则补保护短语；过度拦截 → 检查白名单是否过宽（窗口 6 字）",
              "- 上面待判读项：确属「错词形也是合法词」的，补白名单；否则留档不动作", ""]
    if sug:
        lines += ["## 六、白名单补充建议（人工复核后抄入 medical_hotwords.json）", "", "```json"]
        lines += [json.dumps(sug, ensure_ascii=False, indent=1)]
        lines += ["```", ""]

    out = a.out or os.path.join(CORPUS_DIR, "热词规则审计.md")
    json_out = a.json or (out[:-3] + ".json" if out.endswith(".md") else out + ".json")
    os.makedirs(os.path.dirname(out), exist_ok=True)
    open(out, "w", encoding="utf-8").write("\n".join(lines) + "\n")
    json.dump({"hits": all_hits, "miss": miss, "over": over, "review": review,
               "suggest": sug, "risky_rules": risky, "n_items": len(items), "n_refs": len(refs)},
              open(json_out, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(f"✓ 审计报告 {_rel(out)}（漏放 {len(miss)} / 过度拦截 {len(over)} / 命中 {len(all_hits)}"
          f" / 待判读短规则 {len(risky)}）")
    if a.fail_on_risk and miss:
        print(f"❌ --fail-on-risk：发现漏放误伤 {len(miss)} 处", file=sys.stderr)
        return 1
    return 0


def _short(path, n=42):
    return path if len(path) <= n else "…" + path[-n:]


if __name__ == "__main__":
    sys.exit(main() or 0)
