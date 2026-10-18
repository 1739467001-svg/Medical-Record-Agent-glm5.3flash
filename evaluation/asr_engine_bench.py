# -*- coding: utf-8 -*-
"""ASR 引擎对比评测（新引擎密钥到位后即插即测，重复利用患者F 评测口径）
====================================================================
同一音频 × 多引擎（或同引擎 ± LLM 纠错）→ CER（编辑距离）+ 关键医学事实保留率，
输出 Markdown 对比表。基线（2026-10-14，患者F 真实方言录音）：
  local-whisper small（热词修正后）CER 65.1%、事实可辨认 12/12、语义恢复 10/12。

用法（评测产物只落 git 忽略目录）：
    python3 evaluation/asr_engine_bench.py 病历资料/住院/<姓名>/录音/DA0006_20261002_入院记录.wav \
        --ref 病历资料/住院/<姓名>/对照理解稿.txt \
        --engines local-whisper [--llm-polish] [--out <案例目录>/引擎对比.md]

参照稿：人工校读理解稿（纯文本，每轮一句），存案例目录；引擎结果先过统一后处理
（热词修正；--llm-polish 时加 LLM 方言纠错）再评测——与生产链路完全同构。
"""
import argparse, importlib.util, json, os, re, sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "mcp-server"))
import asr_mcp_server as mcp  # noqa: E402

# 关键医学事实清单：缺省用患者F 校准的 12 项（与 docs/ASR识别质量评测_患者F.md 一致）；
# 其他案例用 --facts <json> 或参照稿同目录 事实清单.json 覆盖（[["名称",["关键词",…]],…]
# 或 {"名称":["关键词"]}）——多案例语料库必须逐案提供，否则事实列无意义。
FACTS = [
    ("起病=昨天下午", ("昨天下午", "昨天")),
    ("院外消炎针", ("消炎针", "消炎药")),
    ("治疗后无缓解", ("无缓解", "没见好", "不缓解", "厉害")),
    ("呕吐2-3次", ("吐", "呕吐")),
    ("无发热", ("发烧", "发热")),
    ("既往无类似发作", ("痛过", "没痛过", "既往")),
    ("无药物过敏", ("过敏", "药")),
    ("近两天未饮酒", ("喝酒", "饮酒")),
    ("育有2个孩子", ("孩子", "两个")),
    ("平素体格尚可", ("体格", "平时")),
    ("腹部压痛查体", ("疼", "压痛")),
    ("医生告知手术", ("手术",)),
]


def load_facts(path):
    data = json.load(open(path, encoding="utf-8"))
    if isinstance(data, dict):
        return [(k, tuple(v)) for k, v in data.items() if not str(k).startswith("_")]
    return [(str(n), tuple(ks)) for n, ks in data]


def clean(s):
    import re
    return re.sub(r"[，。？！、\s,.?!——；;：:]", "", s)


# 语气词（净CER 口径：双侧同去——患者F 实测教训：SenseVoice 如实转写语气词，
# 字数近参照稿两倍，标准 CER 把"忠实"误判为"错误"；清理型转写则天然占便宜）
FILLER_RE = re.compile(r"[嗯哦呃啊哎唉呀哈嘛吧呢噢]")


def clean_net(s):
    return FILLER_RE.sub("", clean(s))


def lev(a, b):
    prev = list(range(len(b) + 1))
    for i in range(1, len(a) + 1):
        cur = [i] + [0] * len(b)
        for j in range(1, len(b) + 1):
            cur[j] = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (a[i - 1] != b[j - 1]))
        prev = cur
    return prev[len(b)]


def eval_text(hyp, ref):
    """返回 {cer, net_cer, chars, hits, total}：cer=标准口径；net_cer=去语气词口径。"""
    H, R = clean(hyp), clean(ref)
    cer = lev(R, H) / len(R) if R else 0.0
    Hn, Rn = FILLER_RE.sub("", H), FILLER_RE.sub("", R)
    net = lev(Rn, Hn) / len(Rn) if Rn else 0.0
    hits = sum(1 for _, keys in FACTS if any(k in hyp for k in keys))
    return {"cer": cer, "net_cer": net, "chars": len(H), "hits": hits, "total": len(FACTS)}


def main():
    ap = argparse.ArgumentParser(description="ASR 引擎对比评测（CER + 事实保留率）")
    ap.add_argument("audio", help="音频文件（建议 16k WAV，否则自动预处理）")
    ap.add_argument("--ref", required=True, help="参照理解稿（纯文本，案例目录）")
    ap.add_argument("--engines", default="local-whisper", help="逗号分隔引擎名")
    ap.add_argument("--llm-polish", action="store_true", help="评测 LLM 方言纠错级联效果")
    ap.add_argument("--out", default="", help="报告输出路径（缺省打印 stdout）")
    ap.add_argument("--facts", default="", help="事实清单 JSON（缺省：参照稿同目录 事实清单.json；再缺省用患者F 12 项）")
    a = ap.parse_args()

    # 加载 LLM 环境（evaluation/.llm_env，--llm-polish 需要）
    envp = os.path.join(REPO, "evaluation", ".llm_env")
    if os.path.exists(envp):
        for line in open(envp, encoding="utf-8"):
            line = line.strip()
            if line.startswith("export "):
                line = line[7:]
            if "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k, v.strip('"').strip("'"))

    # 事实清单：显式 --facts 优先；否则找参照稿同目录 事实清单.json；再否则用内置患者F 12 项
    global FACTS
    facts_path = a.facts or os.path.join(os.path.dirname(os.path.abspath(a.ref)), "事实清单.json")
    if a.facts and not os.path.exists(a.facts):
        raise SystemExit(f"事实清单不存在：{a.facts}")
    if os.path.exists(facts_path):
        FACTS = load_facts(facts_path)
        print(f"（事实清单：{facts_path}，{len(FACTS)} 项）")

    ref = open(a.ref, encoding="utf-8").read()
    rows, details = [], []
    for name in [e.strip() for e in a.engines.split(",") if e.strip()]:
        try:
            t = mcp.pick_engine(name).transcribe(a.audio, speakers=True)
            t = mcp.postprocess(t, correct=True, llm_polish=a.llm_polish)
            d = t.to_dict()
            m = eval_text(d["text"], ref)
            nseg = len(d.get("segments") or [])
            rows.append((name, m["cer"], m["net_cer"], m["chars"], m["hits"], m["total"], nseg, None))
            details.append({"engine": name, **m, "n_segments": nseg, "text": d["text"],
                            "text_raw": d.get("text_raw"), "warnings": d.get("warnings"),
                            "hotwords_applied": d.get("hotwords_applied"),
                            "corrections": d.get("corrections")})
            if d.get("text_raw") and d["text_raw"] != d["text"]:
                m0 = eval_text(d["text_raw"], ref)
                rows.append((name + "·raw(纠错前)", m0["cer"], m0["net_cer"], m0["chars"],
                             m0["hits"], m0["total"], nseg, "原转写（热词/LLM 纠错前基线）"))
                details.append({"engine": name + "·raw(纠错前)", **m0, "text": d["text_raw"],
                                "warnings": ["纠错前原转写"]})
        except Exception as e:
            rows.append((name, None, None, 0, 0, len(FACTS), 0, str(e)[:120]))

    lines = ["# ASR 引擎对比评测", "",
             f"- 音频：`{a.audio}`　参照：`{a.ref}`（{len(clean(ref))} 字）　LLM 纠错：{'开' if a.llm_polish else '关'}",
             f"- 口径：CER=编辑距离/参照字数（去标点）；净CER=双侧再同去语气词（忠实转写不吃亏）；事实保留={len(FACTS)} 项关键医学事实字面命中数",
             "- 基线（2026-10-14 患者F）：local-whisper small 热词修正后 CER 65.1%、事实 12/12", "",
             "| 引擎 | CER | 净CER | 转写字数 | 事实保留 | 分段 | 备注 |", "|---|---|---|---|---|---|---|"]
    for disp, cer, net, chars, hits, total, nseg, note in rows:
        f2 = lambda v: f"{v*100:.1f}%" if v is not None else "—"
        lines.append(f"| {disp} | {f2(cer)} | {f2(net)} | {chars} | {hits}/{total} | {nseg} | {note or ''} |")
    out = "\n".join(lines) + "\n"
    if a.out:
        open(a.out, "w", encoding="utf-8").write(out)
        open(a.out + ".json", "w", encoding="utf-8").write(
            json.dumps(details, ensure_ascii=False, indent=1))
        print(f"✓ 报告已写入 {a.out}（明细含全文 → {a.out}.json，案例目录 git 忽略）")
    print(out)


if __name__ == "__main__":
    main()
