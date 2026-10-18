# -*- coding: utf-8 -*-
"""分歧点听辨工作台（标注瓶颈的最后一公里）
================================================
`asr_reconcile.py` 把标注从"逐字听写"压缩成"分歧仲裁"，但仲裁仍是听 25 分钟音频。
本工具再削一层：**把每处分歧切成独立短音频片段**（±3 秒，含上下文），标注员点开片段
听 2~3 秒即可裁决，填一个字段就完成一处——总听音时长从 25 分钟降到 206 × 5 秒 ≈ 17 分钟，
且不再需要在长音频里来回拖拽定位。

三段式工作流：

    ① 拆片  python3 tools/asr_annotate.py --prepare 病历资料/ASR语料库/校订稿/xxx.md
              → 病历资料/ASR语料库/工作台/<案例>/ 下生成 clips/*.wav + 分歧点.md + 分歧点.json
    ② 仲裁  标注员打开 分歧点.md，在「校订」列填 A（采信引擎A）/ B（采信引擎B）/ 自定义文本
              也可只听 clips/ 里的片段，按序号对照
    ③ 成稿  python3 tools/asr_annotate.py --compile 病历资料/ASR语料库/工作台/<案例>
              → 拼出参照稿 ref_<音频名>.txt（引擎A 全文 + 逐处应用裁决），
                再跑 asr_corpus.py --add 即并入评测口径

口径说明：**一致处直接采信引擎A**（这是交叉校订法的前提，也是"只仲裁分歧"的含义）；
因此成稿质量取决于分歧点裁决质量，标注员无需重写全文。

数据安全：所有产物（含音频片段）落 git 忽略区 病历资料/ASR语料库/，不复制整段原始录音。
"""
import argparse, json, os, re, shutil, subprocess, sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "mcp-server"))
sys.path.insert(0, os.path.join(REPO, "tools"))
import asr_reconcile as R  # noqa: E402

CORPUS_DIR = os.path.join(REPO, "病历资料", "ASR语料库")
WORKBENCH_DIR = os.path.join(CORPUS_DIR, "工作台")
TS_RE = re.compile(r"^\[(\d+):(\d+)-")
SEG_LINE_RE = re.compile(r"^\[(\d+):(\d+)-(\d+):(\d+)\]\s*(.*)$")


def fail(msg):
    print(f"❌ {msg}", file=sys.stderr)
    sys.exit(1)


def _rel(p):
    return os.path.relpath(p, REPO)


def _sec(mm, ss):
    return int(mm) * 60 + int(ss)


def parse_draft(md_path):
    """解析校订稿：引擎名、案例 id、A 分段（带时间戳）、B 全文、以及原报告的分歧数。"""
    md = open(md_path, encoding="utf-8").read()
    m = re.search(r"# 双引擎交叉校订稿：(.+)", md)
    case_id = m.group(1).strip() if m else os.path.splitext(os.path.basename(md_path))[0]
    eng_a = re.search(r"引擎A（时间轴基准）：`([^`]+)`", md)
    eng_b = re.search(r"引擎B：`([^`]+)`", md)
    reported = re.search(r"分歧点：\*\*(\d+) 处\*\*", md)
    if "## 附录：引擎A 全文" not in md or "## 附录：引擎B" not in md:
        fail(f"{_rel(md_path)} 缺少附录全文，无法解析（请用最新版 asr_reconcile.py 重新生成）")
    a_sec = md.split("## 附录：引擎A 全文")[1].split("## 附录：引擎B")[0]
    b_sec = md.split("## 附录：引擎B", 1)[1]
    b_text = b_sec.split("\n", 2)[-1].strip() if b_sec.count("\n") >= 2 else ""
    segs = []
    for ln in a_sec.splitlines():
        m2 = SEG_LINE_RE.match(ln.strip())
        if m2:
            segs.append({"start": _sec(m2.group(1), m2.group(2)),
                         "end": _sec(m2.group(3), m2.group(4)), "text": m2.group(5)})
    if not segs:
        fail("未解析到引擎A 分段（附录格式异常）")
    return {"case_id": case_id, "md_path": md_path,
            "engine_a": eng_a.group(1) if eng_a else "A",
            "engine_b": eng_b.group(1) if eng_b else "B",
            "reported": int(reported.group(1)) if reported else None,
            "segments": segs, "a_text": "".join(s["text"] for s in segs), "b_text": b_text}


def rebuild_clusters(a_text, b_text):
    """用与校订稿同口径重算分歧簇（去标点+去语气词对齐）。"""
    import difflib
    ca, cb = R.clean_chars(a_text), R.clean_chars(b_text)
    ops = difflib.SequenceMatcher(None, ca, cb, autojunk=False).get_opcodes()
    clusters = R.merge_clusters(ops)
    matched = sum(i2 - i1 for tag, i1, i2, j1, j2 in ops if tag == "equal")
    sim = matched / max(len(ca), len(cb), 1)
    return clusters, ca, cb, sim


def raw_index_map(text):
    """清洗后位置 → 原文位置 的映射（供把分歧簇定位回原始字符区间）。"""
    return [i for i, c in enumerate(text)
            if not R.CLEAN_RE.match(c) and not R.FILLER_RE.match(c)], text


def span_in_raw(raw_map, i1, i2):
    """清洗区间 [i1,i2) → 原文区间 [lo,hi)（空区间退化为插入点）。"""
    if not raw_map:
        return 0, 0
    if i1 >= len(raw_map):
        return len(raw_map) - 1, len(raw_map) - 1
    lo = raw_map[i1]
    if i2 <= i1:                      # 纯插入（B 多字、A 无对应）
        return lo, lo
    hi = (raw_map[i2 - 1] + 1) if i2 - 1 < len(raw_map) else len(raw_map)
    return lo, hi


def find_clip_window(clusters):
    """为每个分歧簇算切片段的时间窗。

    关键：**段内按字符比例插值**，而不是直接取整段起止时间。
    实测教训——门诊分段较长（约 10 秒/段），若整段取时间戳，十几个分歧簇会全部落在
    同一段上、窗口彼此完全重叠，合并后片段几乎等于整段音频（听音量反而 100%+），
    碎片化毫无收益。按比例插值后每簇窗口收紧到 1~3 秒，片段才真正"短"。
    """
    segs = clusters["segments"]
    # 每段的清洗字符区间 [cs, ce) 与时间区间 [t0, t1]
    bounds, seg_spans = [], []
    n = 0
    for s in segs:
        seg_len = len(R.clean_chars(s["text"]))
        cs, ce = n, n + seg_len
        n = ce
        bounds.append((cs, ce, s["start"], s["end"]))

    def t_at(idx, is_end=False):
        """清洗字符下标 → 时间（段内线性插值；越界收敛到边界）。"""
        if not bounds:
            return 0.0
        if idx < 0:
            idx = 0
        for cs, ce, t0, t1 in bounds:
            if idx < ce or (is_end and idx == ce):
                if ce == cs:                       # 空段：退化取段首
                    return t0
                span = ce - cs
                pos = min(max(idx - cs, 0), span)
                if is_end and pos == 0:
                    pos = min(span, 1)
                return t0 + (t1 - t0) * (pos / span)
        return bounds[-1][3]

    out = []
    for (i1, i2, j1, j2) in clusters["clusters"]:
        out.append((t_at(i1), t_at(max(i2, i1 + 1), is_end=True)))
    return out


CLIP_PAD = 2.0     # 片段前后各留 2 秒上下文（听清语速与咬字）
CLIP_MERGE_GAP = 1.5   # 相邻窗口间隔小于此值则合并成一个片段（避免片段互相重叠、反而更费时）
DENSE_RATIO = 0.9      # 听音量/音频时长超过此比例即视为"分歧密集"（碎片化省不下时间）


def merge_windows(windows):
    """把相互重叠/紧邻的分歧窗口合并为较大的片段区间。

    必要性（实测教训）：门诊 5 段共 206 处分歧，若每处各切一个 ±3 秒片段，
    片段总时长 70.6 分钟——**远超原始音频 25 分钟**（相邻分歧的窗口大量重叠），
    标注反而更费时。合并后按"并集"切片段，总听音量才真正下降。
    返回 [(clip_start, clip_end, [cluster 下标...]), ...]（下标为 windows 中的序号）。
    """
    order = sorted(range(len(windows)), key=lambda i: windows[i][0])
    groups, cur = [], None
    for i in order:
        t0, t1 = windows[i]
        if cur and t0 <= cur[1] + CLIP_MERGE_GAP:
            cur[1] = max(cur[1], t1)
            cur[2].append(i)
        else:
            if cur:
                groups.append(cur)
            cur = [t0, t1, [i]]
    if cur:
        groups.append(cur)
    return groups


def _clip_ranges(windows):
    """合并后的片段区间与总听音量（秒）——用于核算"到底要听多久"。"""
    groups = merge_windows(windows)
    total = sum(g[1] - g[0] + 2 * CLIP_PAD for g in groups)
    return groups, total


def cut_clip(audio, start, end, dst):
    """切片段（缓存命中则跳过）。"""
    if os.path.exists(dst) and os.path.getsize(dst) > 1000:
        return False
    ss = max(0.0, start - CLIP_PAD)
    to = end + CLIP_PAD
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
           "-ss", f"{ss:.2f}", "-to", f"{to:.2f}", "-i", audio,
           "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", dst]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        return None
    return True


def cmd_prepare(a):
    d = parse_draft(a.prepare)
    clusters, ca, cb, sim = rebuild_clusters(d["a_text"], d["b_text"])
    if d["reported"] is not None and d["reported"] != len(clusters):
        print(f"⚠ 重算分歧数 {len(clusters)} ≠ 报告 {d['reported']}（附录内容可能被改过，以重算为准）")
    # 定位音频：案例 id → 音频文件
    audio = a.audio or _guess_audio(d["case_id"])
    out_dir = os.path.join(WORKBENCH_DIR, d["case_id"].replace("/", "_"))
    clips_dir = os.path.join(out_dir, "clips")
    os.makedirs(clips_dir, exist_ok=True)

    raw_map, a_raw = raw_index_map(d["a_text"])
    windows = find_clip_window({**d, "clusters": clusters})
    # 相邻分歧的片段窗口大量重叠：按并集合并后切片段，否则总听音量反而超过原音频
    groups, listen_sec = _clip_ranges(windows)
    audio_sec = _audio_duration(audio) if audio else None
    dense = bool(audio_sec and listen_sec / audio_sec > DENSE_RATIO)
    items, group_clip = [], {}
    if not dense:
        for gi, (g0, g1, idxs) in enumerate(groups, 1):
            clip = os.path.join(clips_dir, f"{gi:03d}.wav")
            ok = cut_clip(audio, g0, g1, clip) if audio and os.path.exists(audio) else None
            group_clip[gi] = (_rel(clip) if ok else None, len(idxs))
    else:
        # 分歧密集：碎片化无收益（pad 开销把总时长推过原音频）——不切片段，
        # 分歧点清单退化为"该听哪几处"的锚点（每项带秒数，直接在原音频里跳转）
        groups = [[0.0, audio_sec or 0.0, list(range(len(windows)))]]
        listen_sec = audio_sec or 0.0
    cluster_group = {}
    for gi, (_, _, idxs) in enumerate(groups, 1):
        for ci in idxs:
            cluster_group[ci] = gi
    for gi, (g0, g1, idxs) in enumerate(groups, 1):
        clip = os.path.join(clips_dir, f"{gi:03d}.wav")
        ok = cut_clip(audio, g0, g1, clip) if audio and os.path.exists(audio) else None
        group_clip[gi] = (_rel(clip) if ok else None, len(idxs))
    for k, ((i1, i2, j1, j2), (t0, t1)) in enumerate(zip(clusters, windows), 1):
        lo, hi = span_in_raw(raw_map, i1, i2)
        gi = cluster_group[k - 1]
        items.append({
            "idx": k, "start": round(t0, 2), "end": round(t1, 2),
            "clip_group": None if dense else gi,
            "clip": None if dense else group_clip[gi][0],
            "at": R.fmt_ts(t0),          # 锚点秒数（密集模式直接跳转用）
            "a_span": [lo, hi], "a_text": a_raw[lo:hi],
            "b_text": "".join(cb[j1:j2]),
            "a_ctx": R.marked(ca, i1, i2), "b_ctx": R.marked(cb, j1, j2),
            "decision": "", "note": "",
        })
    meta = {"case_id": d["case_id"], "draft": _rel(d["md_path"]), "audio": _rel(audio) if audio else None,
            "engine_a": d["engine_a"], "engine_b": d["engine_b"],
            "sim": round(sim, 4), "n_clusters": len(items),
            "n_clips": len(groups), "clip_pad": CLIP_PAD,
            "listen_sec": round(listen_sec, 1), "audio_sec": round(audio_sec, 1) if audio_sec else None,
            "dense": dense, "a_text": a_raw,
            "clips_ok": sum(1 for i in items if i["clip"])}
    json.dump({"meta": meta, "items": items},
              open(os.path.join(out_dir, "分歧点.json"), "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)
    _write_md(os.path.join(out_dir, "分歧点.md"), meta, items, a_raw)
    print(f"✓ 工作台已生成 {_rel(out_dir)}")
    print(f"  分歧点 {len(items)} 处　一致率 {sim*100:.1f}%")
    if dense:
        print(f"  ⚠ 分歧密集（逐片段听合计 {listen_sec/60:.1f} 分钟 ≈ 原音频 {audio_sec/60:.1f} 分钟，"
              f"省不下时间）→ 已改用「行内标注意稿」：整段听一遍，遇〖#n〗标记填一个决策")
    else:
        print(f"  已切 {len(groups)} 个音频片段"
              f"（合计 {listen_sec/60:.1f} 分钟 vs 原音频 {audio_sec/60:.1f} 分钟，"
              f"压缩到 {listen_sec/audio_sec*100:.0f}%）" if audio else "  （缺音频，可 --audio 指定）")
    print(f"  下一步：打开 分歧点.md 填「校订」列（A / B / 自定义文本），或直接改行内标记 ⟦⟧")
    return out_dir


def _audio_duration(path):
    r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                        "-of", "csv=p=0", path], capture_output=True, text=True)
    try:
        return float(r.stdout.strip())
    except ValueError:
        return None


def _guess_audio(case_id):
    """案例 id（如 病历资料/门诊/5）→ 音频文件路径。"""
    for ext in (".wav", ".m4a", ".mp3"):
        p = os.path.join(REPO, case_id + ext)
        if os.path.exists(p):
            return p
    return None


def build_linear_script(a_raw, items, segs=None):
    """生成**行内标注意稿**：把每处分歧原位替换成带编号的标记，标注员边听边按序号裁决。

    密集场景（分歧多、碎片化省不下时间）下这比 206 个小片段实用得多——
    整段听一遍（=原音频时长），遇到标记就填一个决策，不必在播放器里反复定位。
    标记形如：〖#12 A=小眼点 B=小眼脸 ⟦校订⟧〗
    """
    out = a_raw
    for it in sorted(items, key=lambda x: -x["a_span"][0]):
        lo, hi = it["a_span"]
        a_txt = it["a_text"] or "∅"
        b_txt = it["b_text"] or "∅"
        out = out[:lo] + f"〖#{it['idx']} A={a_txt} B={b_txt} ⟦{it['decision'] or ''}⟧〗" + out[hi:]
    return out


def _write_md(path, meta, items, a_raw=""):
    dense = meta.get("dense")
    lines = [f"# 分歧点听辨工作台：{meta['case_id']}", "",
             f"- 引擎A（一致处采信基准）：`{meta['engine_a']}`　引擎B：`{meta['engine_b']}`　"
             f"字符一致率 **{meta['sim']*100:.1f}%**",
             f"- 分歧点 **{meta['n_clusters']} 处**　"
             + (f"⚠ 分歧密集（听音量占比 {meta.get('listen_sec',0)/max(meta.get('audio_sec') or 1,1)*100:.0f}%）"
                "——碎片化省不下时间，改用下方「行内标注意稿」整段听一遍即可"
                if dense else
                f"音频片段：clips/（{meta.get('n_clips', 0)} 个，每段含前后 {CLIP_PAD:.0f} 秒上下文）"),
             "- **填法**：在「校订」列填 `A`＝采信引擎A原文／`B`＝采信引擎B／或直接写正确文本；"
             "留空视同采信 A。备注列可选。", "",
             "| # | 时间 | 片段 | 引擎A | 引擎B | 校订 | 备注 |", "|---|---|---|---|---|---|---|"]
    for it in items:
        clip = os.path.basename(it["clip"]) if it.get("clip") else "—"
        lines.append(f"| {it['idx']} | {R.fmt_ts(it['start'])} | {clip} | {R.cell(it['a_ctx'])} "
                     f"| {R.cell(it['b_ctx'])} | {it['decision']} | {it['note']} |")
    if dense and a_raw:
        lines += ["", "## 行内标注意稿（密集模式：整段听一遍，遇〖标记〗填决策）", "",
                  "> 每处标记形如 `〖#序号 A=引擎A原文 B=引擎B文本 ⟦校订⟧〗`：听清后把 `⟦⟧` 里填上"
                  "「A」「B」或正确文本即可（填 A 或留空＝采信引擎A）；改完执行文末命令成稿"
                  "（标记外的内容＝一致处，已采信 A）。", "",
                  "```text", build_linear_script(a_raw, items), "```"]
    lines += ["", "## 成稿", "",
              "填完保存本文件后执行（也可用 分歧点.json 的 decision 字段）：", "",
              "```bash",
              f"python3 tools/asr_annotate.py --compile {WORKBENCH_DIR[len(REPO)+1:]}/{meta['case_id'].replace('/', '_')}",
              "```", ""]
    open(path, "w", encoding="utf-8").write("\n".join(lines) + "\n")


INLINE_RE = re.compile(r"〖#(\d+)\s+A=(.*?)\s+B=(.*?)\s+⟦(.*?)⟧〗")


def read_decisions(bench_dir):
    """读裁决，三个来源按优先级叠加（后者覆盖前者，但都只认**非空**值）：

      ① 分歧点.json 的 decision 字段（结构化底稿）
      ② 分歧点.md 表格的「校订」列（标注员改表格）
      ③ 分歧点.md 行内标注意稿的 `⟦…⟧`（密集模式主用：边听边在正文里填）
    """
    pj = os.path.join(bench_dir, "分歧点.json")
    if not os.path.exists(pj):
        fail(f"{_rel(bench_dir)} 下无 分歧点.json（请先 --prepare）")
    data = json.load(open(pj, encoding="utf-8"))
    by_idx = {it["idx"]: it for it in data["items"]}
    pm = os.path.join(bench_dir, "分歧点.md")
    if os.path.exists(pm):
        md = open(pm, encoding="utf-8").read()
        # ② 表格列
        for ln in md.splitlines():
            cells = [c.strip() for c in ln.strip().strip("|").split("|")] if ln.strip().startswith("|") else []
            if len(cells) < 7 or not cells[0].isdigit():
                continue
            dec, note = cells[5], cells[6]
            it = by_idx.get(int(cells[0]))
            if it is not None and dec and dec != "校订":
                it["decision"], it["note"] = dec, note
        # ③ 行内标记（密集模式）。跳过文档里的**格式示例**（A=引擎A原文 / 决策字面"校订"），
        #    否则示例本身会被当成 #12 的真实裁决写进 JSON
        for m in INLINE_RE.finditer(md):
            idx, a_txt, dec = int(m.group(1)), m.group(2).strip(), m.group(4).strip()
            if a_txt == "引擎A原文" or dec in ("", "校订", "—"):
                continue
            it = by_idx.get(idx)
            if it is not None:
                it["decision"] = dec
        json.dump(data, open(pj, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    return data


def cmd_compile(a):
    data = read_decisions(os.path.abspath(a.compile))
    meta, items = data["meta"], data["items"]
    a_raw = meta["a_text"]
    decided = [it for it in items if it["decision"]]
    # 自后向前替换，避免前面替换影响后面区间下标
    out = a_raw
    applied = 0
    for it in sorted(items, key=lambda x: -x["a_span"][0]):
        dec = (it["decision"] or "").strip()
        if not dec:
            continue
        lo, hi = it["a_span"]
        rep = it["b_text"] if dec.upper() == "B" else ("" if dec.upper() == "A" else dec)
        if dec.upper() == "A":
            continue                      # 采信 A＝保持原文，无需替换
        out = out[:lo] + rep + out[hi:]
        applied += 1
    # 落参照稿：优先案例目录 ref_<音频名>.txt（同一目录多音频场景避免互相覆盖）
    if meta.get("audio"):
        stem = os.path.splitext(os.path.basename(meta["audio"]))[0]
        dst = os.path.join(REPO, os.path.dirname(meta["audio"]), f"ref_{stem}.txt")
    else:
        dst = os.path.join(os.path.abspath(a.compile), "对照理解稿.txt")
    if a.out:
        dst = a.out
    open(dst, "w", encoding="utf-8").write(out.strip() + "\n")
    remaining = [it["idx"] for it in items if not it["decision"]]
    print(f"✓ 参照稿已生成 {_rel(dst)}（{len(out)} 字，应用裁决 {applied}/{len(items)} 处）")
    if remaining:
        print(f"  未裁决 {len(remaining)} 处（沿用引擎A 原文）：{remaining[:12]}"
              f"{' …' if len(remaining) > 12 else ''}")
    print(f"  下一步：python3 tools/asr_corpus.py --add {os.path.dirname(meta['audio']) if meta.get('audio') else '<案例>'} --llm-polish")
    return dst


def cmd_status(a):
    if not os.path.isdir(WORKBENCH_DIR):
        print("（尚无工作台目录）")
        return
    for name in sorted(os.listdir(WORKBENCH_DIR)):
        pj = os.path.join(WORKBENCH_DIR, name, "分歧点.json")
        if not os.path.exists(pj):
            continue
        d = json.load(open(pj, encoding="utf-8"))
        n, done = len(d["items"]), sum(1 for i in d["items"] if i["decision"])
        bar = "█" * int(20 * done / max(n, 1)) + "·" * (20 - int(20 * done / max(n, 1)))
        print(f"  {name:28} {bar} {done}/{n}"
              f"{'  ✅可成稿' if done == n and n else ''}")


def main():
    ap = argparse.ArgumentParser(description="分歧点听辨工作台（标注瓶颈最后一公里）")
    ap.add_argument("--prepare", metavar="校订稿.md", help="① 拆片：生成工作台目录与音频片段")
    ap.add_argument("--compile", metavar="工作台目录", help="③ 成稿：据裁决拼出参照稿")
    ap.add_argument("--status", action="store_true", help="查看各案例裁决进度")
    ap.add_argument("--audio", default="", help="指定音频（缺省按案例 id 自动探测）")
    ap.add_argument("--out", default="", help="参照稿输出路径（缺省案例目录 ref_<音频名>.txt）")
    a = ap.parse_args()
    if a.status:
        return cmd_status(a)
    if a.prepare:
        return cmd_prepare(a)
    if a.compile:
        return cmd_compile(a)
    ap.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main() or 0)
