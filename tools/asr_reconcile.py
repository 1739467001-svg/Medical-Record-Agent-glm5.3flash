# -*- coding: utf-8 -*-
"""双引擎交叉校订稿生成器（标注加速器）
==========================================
两个独立引擎（缺省 whisper + SenseVoice）同题转写 → 字符级对齐 → 只把**分歧点**列成
带时间戳的听辨清单：人工标注从"逐字听写"变成"分歧仲裁"（一致处直接采信），参照稿
产出效率可提升数倍——当前数据飞轮的唯一瓶颈就是标注（见 docs/自建ASR选型与实施路径.md）。

用法：
    python3 tools/asr_reconcile.py 病历资料/门诊/1.m4a --whisper-model small
    python3 tools/asr_reconcile.py <案例目录>        # 自动发现音频
    # 产物：病历资料/ASR语料库/校订稿/<case_id>.md（git 忽略区）

口径说明：缺省**不启用** LLM 纠错（校订稿要引擎原声，不掺 LLM 改写）；热词修正默认开。
校订完成后把定稿另存为案例目录 对照理解稿.txt，再跑 asr_corpus.py --add 即并入评测口径。
"""
import argparse, difflib, os, re, sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "mcp-server"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import asr_mcp_server as mcp  # noqa: E402
import asr_corpus as corpus  # noqa: E402

CLEAN_RE = re.compile(r"[，。？！、\s,.?!——；;：:]")
# 语气词：对齐时双侧同去——whisper 倾向丢弃语气词、SenseVoice 如实保留（门诊实测一致率
# 被伪分歧压低到 40% 级），去语气词后对齐才反映"内容分歧"；附录正文仍保留原样
FILLER_RE = re.compile(r"[嗯哦呃啊哎唉呀哈嘛吧呢噢]")


def clean_chars(text):
    """清洗（去标点空白+语气词）后字符列表——对齐与时间映射共用同一口径。"""
    return [c for c in text if not CLEAN_RE.match(c) and not FILLER_RE.match(c)]


def build_time_map(segments):
    """(清洗后字符累积边界, 各段起始秒)：把字符位置映射回时间戳。"""
    bounds, times = [], []
    n = 0
    for s in segments or []:
        n += len(clean_chars(str(s.get("text") or "")))
        bounds.append(n)
        times.append(s.get("start"))
    return bounds, times


def find_time(idx, bounds, times):
    for i, b in enumerate(bounds):
        if idx < b:
            return times[i]
    return times[-1] if times else None


def fmt_ts(sec):
    if sec is None:
        return "—"
    return f"{int(sec // 60):02d}:{int(sec % 60):02d}"


def merge_clusters(opcodes, max_gap=4, max_span=60):
    """相邻非 equal 操作按小间隔合并成分歧簇，并把超长簇按比例切成 ≤max_span 的小行
    （低一致率音频会产生巨型 diff，不切分则表格不可用）。返回 (a0,a1,b0,b1) 列表。"""
    out, cur = [], None
    for tag, i1, i2, j1, j2 in opcodes:
        if tag == "equal":
            continue
        if cur and (i1 - cur[1]) <= max_gap and (j1 - cur[3]) <= max_gap:
            cur = (cur[0], max(cur[1], i2), cur[2], max(cur[3], j2))
        else:
            if cur:
                out.append(cur)
            cur = (i1, i2, j1, j2)
    if cur:
        out.append(cur)
    pieces = []
    for a0, a1, b0, b1 in out:
        if (a1 - a0) + (b1 - b0) < 2:
            continue
        span = max(a1 - a0, b1 - b0)
        k = max(1, (span + max_span - 1) // max_span)
        for t in range(k):
            pieces.append((a0 + (a1 - a0) * t // k, a0 + (a1 - a0) * (t + 1) // k,
                           b0 + (b1 - b0) * t // k, b0 + (b1 - b0) * (t + 1) // k))
    return pieces


def marked(chars, i1, i2, pad=8):
    """带【分歧标记】的上下文片段（供人工听辨）。"""
    s, e = max(0, i1 - pad), min(len(chars), i2 + pad)
    seg = "".join(chars[s:i1]) + "【" + "".join(chars[i1:i2]) + "】" + "".join(chars[i2:e])
    return ("…" if s > 0 else "") + seg + ("…" if e < len(chars) else "")


def cell(s):
    return s.replace("|", "｜")


def main():
    ap = argparse.ArgumentParser(description="双引擎交叉校订稿生成器（标注加速器）")
    ap.add_argument("target", help="音频文件或案例目录")
    ap.add_argument("--engines", default="local-whisper,funasr", help="两个引擎（A,B：A 为时间轴基准）")
    ap.add_argument("--whisper-model", default="", help="设置 ASR_LOCAL_MODEL（如 small）")
    ap.add_argument("--funasr-model", default="", help="设置 ASR_FUNASR_MODEL（如 SeACo 模型 ID）")
    ap.add_argument("--llm-polish", action="store_true", help="启用 LLM 纠错（缺省关：校订稿要引擎原声）")
    ap.add_argument("--out", default="", help="输出路径（缺省 病历资料/ASR语料库/校订稿/<案例>.md）")
    a = ap.parse_args()
    if a.whisper_model:
        os.environ["ASR_LOCAL_MODEL"] = a.whisper_model
    if a.funasr_model:
        os.environ["ASR_FUNASR_MODEL"] = a.funasr_model

    case_id, _, audio, _ = corpus.discover(a.target, None)
    specs = [e.strip() for e in a.engines.split(",") if e.strip()]
    if len(specs) < 2:
        corpus.fail("需要两个引擎（--engines A,B）")
    MODEL_ENV = {"funasr": "ASR_FUNASR_MODEL", "local-whisper": "ASR_LOCAL_MODEL"}
    texts, segs_of, order = {}, {}, []
    for spec in specs:
        name, label = spec, spec
        if ":" in spec:  # 支持"引擎:模型ID"规格（同族双模型交叉，如 SenseVoice vs SeACo）
            name, model = spec.split(":", 1)
            os.environ[MODEL_ENV.get(name, "ASR_FUNASR_MODEL")] = model
            label = f"{name}({model.split('/')[-1][:28]})"
        t = mcp.pick_engine(name).transcribe(audio, speakers=True)
        t = mcp.postprocess(t, correct=True, llm_polish=a.llm_polish)
        texts[label] = t.text
        segs_of[label] = t.segments
        order.append(label)
        print(f"  ✓ {label}: {len(t.text)} 字")
    A, B = order[0], order[1]
    ca, cb = clean_chars(texts[A]), clean_chars(texts[B])
    ops = difflib.SequenceMatcher(None, ca, cb, autojunk=False).get_opcodes()
    matched = sum(i2 - i1 for tag, i1, i2, j1, j2 in ops if tag == "equal")
    sim = matched / max(len(ca), len(cb), 1)
    clusters = merge_clusters(ops)
    bounds, times = build_time_map(segs_of[A])

    lines = [f"# 双引擎交叉校订稿：{case_id}", "",
             f"- 引擎A（时间轴基准）：`{A}`　引擎B：`{B}`　LLM 纠错：{'开' if a.llm_polish else '关（引擎原声）'}",
             f"- 字符一致率：**{sim*100:.1f}%**（A {len(ca)} 字 / B {len(cb)} 字）　分歧点：**{len(clusters)} 处**",
             "- 用法：只需在分歧点听原音裁决（一致处直接采信）→ 填「校订」列 → 定稿另存案例目录"
             " `对照理解稿.txt` → `python3 tools/asr_corpus.py --add <案例>` 并入评测口径", ""]
    if sim < 0.6:
        lines += ["- ⚠ **两引擎一致性低（<60%）**：该音频对模型较难（噪声/方言/重叠说话），"
                  "本稿仅用于定位难点，建议整段精听而非只仲裁分歧点", ""]
    MAX_ROWS = 200
    if len(clusters) > MAX_ROWS:
        lines += [f"- （分歧点 {len(clusters)} 处，表格只列前 {MAX_ROWS} 处，其余见附录全文）", ""]
        clusters = clusters[:MAX_ROWS]
    lines += ["| # | 时间 | 引擎A（听辨基准） | 引擎B | 校订（人工填写） |", "|---|---|---|---|---|"]
    for k, (i1, i2, j1, j2) in enumerate(clusters, 1):
        lines.append(f"| {k} | {fmt_ts(find_time(i1, bounds, times))} | {cell(marked(ca, i1, i2))} "
                     f"| {cell(marked(cb, j1, j2))} |  |")
    lines += ["", f"## 附录：引擎A 全文（{A}，带时间戳）", ""]
    for s in segs_of[A]:
        lines.append(f"[{fmt_ts(s.get('start'))}-{fmt_ts(s.get('end'))}] {s.get('text','')}")
    lines += ["", f"## 附录：引擎B 全文（{B}）", "", texts[B]]

    out = a.out or os.path.join(corpus.CORPUS_DIR, "校订稿",
                                case_id.replace("/", "_") + ".md")
    corpus.ensure_ignored(corpus.CORPUS_DIR)
    os.makedirs(os.path.dirname(out), exist_ok=True)
    open(out, "w", encoding="utf-8").write("\n".join(lines) + "\n")
    print(f"✓ 校订稿已写入 {corpus.rel(out)}（{len(clusters)} 处分歧，一致率 {sim*100:.1f}%）")


if __name__ == "__main__":
    main()
