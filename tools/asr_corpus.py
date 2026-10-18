# -*- coding: utf-8 -*-
"""方言 ASR 语料库管理器（数据飞轮第②阶段）
==============================================
每接入一例真实录音案例，一条命令完成：引擎转写 →（有参照稿则）CER/事实对比 →
报告落盘案例目录 → 语料库索引归集 → 复选/微调门槛进度。无参照稿的音频先入库
存"引擎草稿"，供人工按 evaluation/asr_testset/标注规范.md 校订成参照稿——这是
语料库扩容的主路径（当前瓶颈：音频多、参照稿少）。

用法：
    # 有参照稿：完整评测（报告 + 明细 JSON + 索引），自动跑全部已就绪引擎
    python3 tools/asr_corpus.py --add 病历资料/住院/<姓名> --whisper-model small --llm-polish

    # 无参照稿：只落引擎草稿（供标注）
    python3 tools/asr_corpus.py --add 病历资料/门诊/1.m4a

    python3 tools/asr_corpus.py --list     # 案例清单
    python3 tools/asr_corpus.py --stats    # 累计统计与门槛进度（复选 5 例 / 微调 50 小时）

数据安全（约法三章第 1 条）：语料库只落 git 忽略区（缺省 病历资料/ASR语料库/），
索引仅存路径与指标、不复制音频；启动时强制 git check-ignore 校验。
"""
import argparse, json, os, subprocess, sys, time

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "mcp-server"))
sys.path.insert(0, os.path.join(REPO, "evaluation"))

CORPUS_DIR = os.path.join(REPO, "病历资料", "ASR语料库")
INDEX_PATH = os.path.join(CORPUS_DIR, "索引.json")
DRAFT_DIR = os.path.join(CORPUS_DIR, "转写草稿")
AUDIO_EXT = (".wav", ".m4a", ".mp3")
FINETUNE_HOURS = 50.0  # 方言微调立项门槛（ASR 评测报告 §四：50+ 小时标注音频）
SELECT_CASES = 5       # 引擎复选门槛（5~10 例）


def fail(msg):
    print(f"❌ {msg}", file=sys.stderr)
    sys.exit(1)


def ensure_ignored(path):
    """数据安全强制项：语料库目录必须被 git 忽略（真实录音与转写稿永不入库）。"""
    p = os.path.abspath(path)
    r = subprocess.run(["git", "check-ignore", os.path.relpath(p, REPO)],
                       cwd=REPO, capture_output=True, text=True)
    if r.returncode != 0:
        fail(f"{p} 未被 .gitignore 忽略——语料库必须在忽略区（如 病历资料/ 下）")


def rel(p):
    return os.path.relpath(p, REPO) if p else None


def load_index():
    if os.path.exists(INDEX_PATH):
        return json.load(open(INDEX_PATH, encoding="utf-8"))
    return {"version": 1, "cases": []}


def save_index(idx):
    os.makedirs(CORPUS_DIR, exist_ok=True)
    json.dump(idx, open(INDEX_PATH, "w", encoding="utf-8"), ensure_ascii=False, indent=1)


def _pick_audio(case_dir):
    cands = []
    for sub in ("录音", ""):
        d = os.path.join(case_dir, sub) if sub else case_dir
        if os.path.isdir(d):
            cands += [os.path.join(d, f) for f in sorted(os.listdir(d))
                      if f.lower().endswith(AUDIO_EXT) and not f.startswith(".")]
    if not cands:
        return None
    pool = [c for c in cands if c.lower().endswith(".wav")] or cands
    for c in pool:  # 多段时优先"入院记录"主录音
        if "入院" in os.path.basename(c):
            return c
    return pool[0]


def _pick_ref(case_dir, audio):
    stem = os.path.splitext(os.path.basename(audio))[0]
    for name in ("对照理解稿.txt", "对照理解稿_人工校读.md", f"ref_{stem}.txt", f"{stem}_ref.txt"):
        p = os.path.join(case_dir, name)
        if os.path.exists(p):
            return p
    return None


def discover(target, ref_arg):
    p = os.path.abspath(target)
    if os.path.isfile(p):
        audio, case_dir = p, os.path.dirname(p)
    elif os.path.isdir(p):
        case_dir = p
        audio = _pick_audio(p)
        if not audio:
            fail(f"{p} 下未找到音频（{AUDIO_EXT}）")
    else:
        fail(f"路径不存在：{p}")
    ref = os.path.abspath(ref_arg) if ref_arg else _pick_ref(case_dir, audio)
    if ref and not os.path.exists(ref):
        fail(f"参照稿不存在：{ref}")
    case_id = os.path.splitext(rel(audio))[0]
    return case_id, case_dir, audio, ref


def ready_engines():
    import asr_mcp_server as mcp
    return [n for n, e in mcp.ENGINES.items() if n != "mock" and e.configured()]


def _kind(case_id):
    """真实案例 vs 合成回归用例（彩排录音为 TTS 合成+精确标注，只做回归不计入门槛）。"""
    return "合成回归" if "彩排" in case_id else "真实案例"


def audio_duration(audio):
    """时长（秒）：先试 16k 转码产物 WAV 头（引擎转写后已缓存），失败回退原 WAV 头。"""
    import asr_mcp_server as mcp
    try:
        wav = mcp.prepare_audio(audio)
        return mcp.wav_duration_sec(wav) or None
    except Exception:
        return None


def run_bench(audio, ref, engines, llm_polish, out_md):
    """有参照稿：子进程调评测脚本（报告 + 明细 JSON），返回 metrics 字典。"""
    cmd = [sys.executable, os.path.join(REPO, "evaluation", "asr_engine_bench.py"),
           audio, "--ref", ref, "--engines", ",".join(engines), "--out", out_md]
    if llm_polish:
        cmd.append("--llm-polish")
    r = subprocess.run(cmd, cwd=REPO, capture_output=True, text=True)
    if r.returncode != 0:
        fail(f"评测脚本失败：{(r.stderr or r.stdout)[-400:]}")
    print(r.stdout)
    metrics = {}
    jp = out_md + ".json"
    if os.path.exists(jp):
        for item in json.load(open(jp, encoding="utf-8")):
            metrics[item["engine"]] = {k: item.get(k) for k in
                                       ("cer", "net_cer", "chars", "hits", "total", "n_segments")}
    return metrics


def run_drafts(audio, engines, case_id, llm_polish):
    """无参照稿：逐引擎落转写草稿（供人工标注），返回 {engine: 草稿路径} 与时长。"""
    import asr_mcp_server as mcp
    os.makedirs(DRAFT_DIR, exist_ok=True)
    drafts, dur = {}, None
    for name in engines:
        t0 = time.time()
        try:
            t = mcp.pick_engine(name).transcribe(audio, speakers=True)
            t = mcp.postprocess(t, correct=True, llm_polish=llm_polish)
        except Exception as e:
            print(f"  ⚠ {name} 转写失败：{str(e)[:120]}")
            continue
        segs = t.segments or []
        dur = dur or (segs[-1].get("end") if segs and segs[-1].get("end") else None)
        path = os.path.join(DRAFT_DIR, f"{case_id.replace('/', '_')}__{name}.txt")
        with open(path, "w", encoding="utf-8") as f:
            f.write(f"# 引擎草稿（{name}，{time.strftime('%Y-%m-%d %H:%M')}）——待人工按标注规范校订\n")
            for s in segs:
                st, en = s.get("start"), s.get("end")
                ts = f"[{st:.0f}-{en:.0f}s] " if st is not None and en is not None else ""
                f.write(f"{ts}{s.get('text', '')}\n")
        drafts[name] = rel(path)
        print(f"  ✓ {name} 草稿：{rel(path)}（{len(segs)} 段，{time.time() - t0:.0f}s）")
    return drafts, dur


def cmd_add(args):
    case_id, case_dir, audio, ref = discover(args.add, args.ref)
    ensure_ignored(CORPUS_DIR)
    engines = ready_engines() if args.engines == "auto" else \
        [e.strip() for e in args.engines.split(",") if e.strip()]
    if not engines:
        fail("无就绪引擎（funasr 需 pip install funasr torch torchaudio；local-whisper 需 --whisper-model）")
    print(f"案例 {case_id}\n音频 {rel(audio)}\n参照 {rel(ref) if ref else '（无 → 落草稿待标注）'}\n引擎 {engines}")

    metrics, drafts, run = {}, {}, {"at": time.strftime("%Y-%m-%d %H:%M"),
                                    "engines": engines, "llm_polish": bool(args.llm_polish),
                                    "models": {k: os.environ[k] for k in
                                               ("ASR_LOCAL_MODEL", "ASR_FUNASR_MODEL") if os.environ.get(k)}}
    if ref:
        out_md = os.path.join(case_dir, "引擎对比_语料库%s.md" % ("_LLM纠错" if args.llm_polish else ""))
        metrics = run_bench(audio, ref, engines, args.llm_polish, out_md)
        run["report"] = rel(out_md)
    else:
        drafts, _ = run_drafts(audio, engines, case_id, args.llm_polish)
        run["drafts"] = drafts

    dur = audio_duration(audio)
    idx = load_index()
    rec = next((c for c in idx["cases"] if c["id"] == case_id), None)
    if rec is None:
        rec = {"id": case_id, "audio": rel(audio), "case_dir": rel(case_dir),
               "kind": _kind(case_id),
               "ref": rel(ref) if ref else None, "audio_sec": dur,
               "status": "已评测" if ref else "待标注", "runs": []}
        idx["cases"].append(rec)
    rec.update({"audio": rel(audio), "ref": rel(ref) if ref else rec.get("ref"),
                "audio_sec": dur or rec.get("audio_sec"),
                "status": "已评测" if ref else "待标注"})
    run["metrics"] = metrics
    rec["runs"].append(run)
    save_index(idx)
    print(f"\n✓ 已归集语料库（{rel(INDEX_PATH)}）——累计统计：")
    print_stats(idx)


def cmd_list(args):
    idx = load_index()
    if not idx["cases"]:
        print("语料库为空。用 --add <案例目录|音频> 归集第一条。")
        return
    print("| 案例 | 类型 | 状态 | 时长 | 参照 | 评测次数 |")
    print("|---|---|---|---|---|---|")
    for c in idx["cases"]:
        sec = c.get("audio_sec")
        print(f"| {c['id']} | {c.get('kind', '真实案例')} | {c['status']} | {sec:.0f}s | {'有' if c.get('ref') else '—'} | {len(c.get('runs', []))} |")


def print_stats(idx):
    cases = idx["cases"]
    real = [c for c in cases if c.get("kind") != "合成回归"]
    synth = [c for c in cases if c.get("kind") == "合成回归"]
    done_real = [c for c in real if c["status"] == "已评测"]
    done_all = [c for c in cases if c["status"] == "已评测"]
    total_h = sum((c.get("audio_sec") or 0) for c in real) / 3600.0
    synth_min = sum((c.get("audio_sec") or 0) for c in synth) / 60.0
    print(f"- 真实案例：{len(real)}（已评测 {len(done_real)} / 待标注 {len(real) - len(done_real)}）"
          f"　复选门槛 {SELECT_CASES} 例 → 进度 {min(len(done_real), SELECT_CASES)}/{SELECT_CASES}")
    print(f"- 真实音频总量：{total_h:.2f} 小时　微调门槛 {FINETUNE_HOURS:.0f} 小时 → 进度 {total_h / FINETUNE_HOURS * 100:.1f}%"
          + (f"　（另合成回归 {synth_min:.0f} 分钟，不计入门槛）" if synth else ""))
    per = {}
    for c in done_all:
        runs = c.get("runs") or []
        if not runs:  # 只取每案例最近一次归集（历史 run 保留在索引里可追溯）
            continue
        for eng, m in (runs[-1].get("metrics") or {}).items():
            if m.get("cer") is None:
                continue
            per.setdefault(eng, []).append(m)
    if per:
        print("- 各引擎在已评测案例上的均值（最近归集口径；跨案例均值仅作趋势参考——案例难度差异大，"
              "真实方言 vs 合成清晰音频可差一个数量级）：")
        for eng, ms in sorted(per.items()):
            n = len(ms)
            cer = sum(m["cer"] for m in ms) / n
            net = sum(m["net_cer"] for m in ms) / n
            hits = sum(m["hits"] for m in ms)
            tot = sum(m["total"] for m in ms)
            print(f"    {eng}: CER {cer*100:.1f}% / 净CER {net*100:.1f}%（{n} 例）事实 {hits}/{tot}")
    print("- 下一步：待标注音频按 evaluation/asr_testset/标注规范.md 校订出参照稿后，"
          "重跑 `--add <案例>` 即并入评测口径。")


def cmd_stats(args):
    idx = load_index()
    print("# ASR 语料库统计（git 忽略区）")
    print_stats(idx)


def main():
    ap = argparse.ArgumentParser(description="方言 ASR 语料库管理器（数据飞轮第②阶段）")
    ap.add_argument("--add", metavar="案例目录|音频文件", help="归集案例（有参照稿→评测；无→草稿待标注）")
    ap.add_argument("--ref", help="显式指定参照稿路径（缺省自动发现 对照理解稿.txt / ref_<stem>.txt）")
    ap.add_argument("--engines", default="auto", help="auto（全部已就绪引擎）或逗号分隔引擎名")
    ap.add_argument("--whisper-model", help="设置 ASR_LOCAL_MODEL（如 small / large-v3-turbo）")
    ap.add_argument("--funasr-model", help="设置 ASR_FUNASR_MODEL（如 SeACo 模型 ID）")
    ap.add_argument("--llm-polish", action="store_true", help="启用 LLM 方言纠错级联")
    ap.add_argument("--list", action="store_true", help="案例清单")
    ap.add_argument("--stats", action="store_true", help="累计统计与门槛进度")
    args = ap.parse_args()
    if args.whisper_model:
        os.environ["ASR_LOCAL_MODEL"] = args.whisper_model
    if args.funasr_model:
        os.environ["ASR_FUNASR_MODEL"] = args.funasr_model
    if args.add:
        cmd_add(args)
    elif args.list:
        cmd_list(args)
    elif args.stats:
        cmd_stats(args)
    else:
        ap.print_help()


if __name__ == "__main__":
    main()
