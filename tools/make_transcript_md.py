# -*- coding: utf-8 -*-
"""真实录音问诊记录 Markdown 过程稿生成器（真实数据链路补全，PRD F3）。
====================================================================
把「转写稿 JSON（asr_ingest 产物）+ 角色标注对话（case_ingest 产物）」合成一份
医生可读、可修正、可挂住院时间线的 **Markdown 问诊过程稿**：

    # 问诊记录 · <患者代号>_<入院日>_<文书类型>
    > 元数据块（引擎/时长/热词修正/生成时间/数据安全声明）
    | 时间    | 角色 | 对话内容                | 关联字段（mapsTo） |
    | 00:00   | 患者 | 从昨天下午都开始痛……     | 主诉、现病史       |

对位逻辑：转写分段与角色轮次按序号一一对应（case_ingest 的角色标注即逐段标注，
实测两者同为 50 段/轮）；数量不齐时按累计文本长度贪心对齐并标记 ⚠。
输出两份：案例目录（git 忽略区，含真实值）与 demo/data（脱敏，供平台挂载）。

用法：
    python3 tools/make_transcript_md.py 病历资料/住院/<姓名> --code DA0006
"""
import argparse, datetime, json, os, subprocess, sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEMO_DATA = os.path.join(REPO, "demo", "data")


def fail(msg):
    print(f"❌ {msg}", file=sys.stderr)
    sys.exit(1)


def ensure_ignored(path):
    p = os.path.abspath(path)
    r = subprocess.run(["git", "check-ignore", os.path.relpath(p, REPO)],
                       cwd=REPO, capture_output=True, text=True)
    if r.returncode != 0:
        fail(f"目录未被 .gitignore 忽略（{p}）——真实转写数据永不入库")
    return p


def _mmss(sec):
    m, s = divmod(int(sec or 0), 60)
    return f"{m:02d}:{s:02d}"


def load_parts(case_dir, code):
    """转写 JSON（时间戳+热词修正文本）+ 脱敏对话 JSON（角色+mapsTo）。"""
    tdir = os.path.join(case_dir, "录音", "转写稿")
    tjs = sorted(f for f in os.listdir(tdir) if f.endswith(".json")) if os.path.isdir(tdir) else []
    if not tjs:
        fail(f"无转写稿 JSON（先跑 asr_ingest --transcribe）：{tdir}")
    tr = json.load(open(os.path.join(tdir, tjs[-1]), encoding="utf-8"))
    dpath = os.path.join(DEMO_DATA, f"dialogue_{code}.json")
    if not os.path.exists(dpath):
        fail(f"无角色标注对话（先跑 case_ingest）：{dpath}")
    dlg = json.load(open(dpath, encoding="utf-8"))
    return tr, dlg, tjs[-1]


def align_segments(segs, turns):
    """分段↔轮次对齐：优先按序号一一对应（case_ingest 逐段标注的常规形态）；
    数量不齐时按累计文本长度贪心合并，返回 [(seg, turn)]（缺侧为 None），并给出告警。"""
    warns = []
    if len(segs) == len(turns):
        return list(zip(segs, turns)), warns
    if len(segs) != len(turns):
        warns.append(f"分段 {len(segs)} 与对话轮 {len(turns)} 数量不一致——按文本长度贪心对齐，请人工核对")
    out = []
    si = ti = 0
    while si < len(segs) or ti < len(turns):
        s = segs[si] if si < len(segs) else None
        t = turns[ti] if ti < len(turns) else None
        if s is None or t is None:
            out.append((s, t))
            si += ti and ti + 1 or ti + 1
            ti += 1
            continue
        s_len, t_len = len(s.get("text", "")), len(t.get("text", ""))
        out.append((s, t))
        if s_len <= t_len * 1.3:
            si += 1
            ti += 1
        elif s_len > t_len * 1.3:
            si += 1          # 分段长：多条分段并一轮，下一轮继续吃当前 turn
        else:
            ti += 1
    return out, warns


def build_md(code, doc_type, tr, dlg, tr_file, admits=None):
    segs = tr.get("segments") or []
    pairs, warns = align_segments(segs, dlg)
    meta = {
        "引擎": tr.get("engine", "?"),
        "时长": f"{(tr.get('duration_sec') or 0):.0f}s（实录 {segs[-1]['end']:.0f}s）" if segs else "?",
        "热词修正": (tr.get("warnings") or []),
        "角色标注": "LLM（无说话人分离，按语义标注）",
        "生成时间": datetime.datetime.now().strftime("%Y-%m-%d %H:%M"),
    }
    lines = [f"# 问诊记录 · {code}_{doc_type}", ""]
    lines += ["> " + " · ".join(f"**{k}**：{v}" if not isinstance(v, list) else
                                f"**{k}**：{'; '.join(map(str, v))}" for k, v in meta.items())]
    lines.append(">")
    lines.append("> 本过程稿由真实问诊录音机器转写生成，含方言口音与同音错字（热词表已修正部分），"
                 "医生可在四色确认前修正过程稿并重新生成。全文可溯源，作为绿字段（对话提炼）的依据。")
    lines.append("")
    lines.append("| 时间 | 角色 | 对话内容 | 关联病历字段 |")
    lines.append("|---|---|---|---|")
    for seg, turn in pairs:
        ts = _mmss(seg.get("start", 0)) if seg else "—"
        role = turn.get("role", "?") if turn else "—"
        text = (turn or seg or {}).get("text", "").replace("\n", " ").replace("|", "\\|")
        maps = "、".join((turn or {}).get("mapsTo") or [])
        lines.append(f"| {ts} | {role} | {text} | {maps or ''} |")
    if warns:
        lines += ["", "## ⚠ 对齐告警"] + [f"- {w}" for w in warns]
    lines.append("")
    lines.append(f"*转写源文件：`{tr_file}` · 脱敏对话：`dialogue_{code}.json` · "
                 f"本稿随归档包存档（审计与溯源依据）*")
    return "\n".join(lines) + "\n"


def main():
    ap = argparse.ArgumentParser(description="真实录音 → Markdown 问诊过程稿")
    ap.add_argument("case_dir", help="案例目录（git 忽略，如 病历资料/住院/<姓名>）")
    ap.add_argument("--code", required=True, help="平台患者编号（如 DA0006）")
    ap.add_argument("--doc", default="入院记录", help="文书类型（默认入院记录）")
    args = ap.parse_args()
    case_dir = ensure_ignored(args.case_dir)
    tr, dlg, tr_file = load_parts(case_dir, args.code)
    md = build_md(args.code, args.doc, tr, dlg, tr_file)
    # ① 案例目录存档（含真实转写细节，git 忽略区）
    local = os.path.join(case_dir, "问诊记录_%s_%s.md" % (args.code, args.doc))
    open(local, "w", encoding="utf-8").write(md)
    print(f"✓ 过程稿（案例目录，git 忽略）：{os.path.relpath(local, REPO)}")
    # ② demo/data 版（脱敏对话渲染，供平台挂载展示）
    demo = os.path.join(DEMO_DATA, f"transcript_{args.code}_{args.doc}.md")
    open(demo, "w", encoding="utf-8").write(md)
    print(f"✓ 过程稿（平台数据）：{os.path.relpath(demo, REPO)}")
    # ③ patients.json 挂 transcript_md 引用（幂等）
    pj = os.path.join(DEMO_DATA, "patients.json")
    patients = json.load(open(pj, encoding="utf-8"))
    for p in patients:
        if p["code"] == args.code:
            for a in p["admissions"]:
                a["transcript_md"] = f"transcript_{args.code}_{args.doc}.md"
    json.dump(patients, open(pj, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(f"✓ patients.json 已挂 transcript_md 引用")


if __name__ == "__main__":
    main()
