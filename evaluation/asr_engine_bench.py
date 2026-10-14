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
import argparse, importlib.util, os, sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "mcp-server"))
import asr_mcp_server as mcp  # noqa: E402

# 患者F 校准的 12 项关键医学事实（评测口径与 docs/ASR识别质量评测_患者F.md 一致）
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


def clean(s):
    import re
    return re.sub(r"[，。？！、\s,.?!——；;：:]", "", s)


def lev(a, b):
    prev = list(range(len(b) + 1))
    for i in range(1, len(a) + 1):
        cur = [i] + [0] * len(b)
        for j in range(1, len(b) + 1):
            cur[j] = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (a[i - 1] != b[j - 1]))
        prev = cur
    return prev[len(b)]


def eval_text(hyp, ref):
    H, R = clean(hyp), clean(ref)
    cer = lev(R, H) / len(R) if R else 0.0
    hits = sum(1 for _, keys in FACTS if any(k in hyp for k in keys))
    return cer, hits, len(FACTS)


def main():
    ap = argparse.ArgumentParser(description="ASR 引擎对比评测（CER + 事实保留率）")
    ap.add_argument("audio", help="音频文件（建议 16k WAV，否则自动预处理）")
    ap.add_argument("--ref", required=True, help="参照理解稿（纯文本，案例目录）")
    ap.add_argument("--engines", default="local-whisper", help="逗号分隔引擎名")
    ap.add_argument("--llm-polish", action="store_true", help="评测 LLM 方言纠错级联效果")
    ap.add_argument("--out", default="", help="报告输出路径（缺省打印 stdout）")
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

    ref = open(a.ref, encoding="utf-8").read()
    rows = []
    for name in [e.strip() for e in a.engines.split(",") if e.strip()]:
        try:
            t = mcp.pick_engine(name).transcribe(a.audio, speakers=True)
            t = mcp.postprocess(t, correct=True, llm_polish=a.llm_polish)
            d = t.to_dict()
            cer, hits, total = eval_text(d["text"], ref)
            rows.append((name, d["engine"], cer, hits, total, len(d.get("segments") or []), None))
            if d.get("text_raw") and d["text_raw"] != d["text"]:
                cer0, hits0, _, _, _ = (*eval_text(d["text_raw"], ref), 0, 0)
                rows.append((name + "·raw(纠错前)", d["engine"], cer0, hits0, total,
                             len(d.get("segments") or []), "原转写（LLM 纠错前基线）"))
        except Exception as e:
            rows.append((name, "-", None, 0, len(FACTS), 0, str(e)[:120]))

    lines = ["# ASR 引擎对比评测", "",
             f"- 音频：`{a.audio}`　参照：`{a.ref}`（{len(clean(ref))} 字）　LLM 纠错：{'开' if a.llm_polish else '关'}",
             "- 口径：CER=编辑距离/参照字数（去标点）；事实保留=12 项关键医学事实字面命中数",
             f"- 基线（2026-10-14 患者F）：local-whisper small 热词修正后 CER 65.1%、事实 12/12", "",
             "| 引擎 | CER | 事实保留 | 分段 | 备注 |", "|---|---|---|---|---|"]
    for disp, eng, cer, hits, total, nseg, note in rows:
        cer_s = f"{cer*100:.1f}%" if cer is not None else "—"
        lines.append(f"| {disp} | {cer_s} | {hits}/{total} | {nseg} | {note or ''} |")
    out = "\n".join(lines) + "\n"
    if a.out:
        open(a.out, "w", encoding="utf-8").write(out)
        print(f"✓ 报告已写入 {a.out}")
    print(out)


if __name__ == "__main__":
    main()
