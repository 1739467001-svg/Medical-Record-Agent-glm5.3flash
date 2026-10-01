# -*- coding: utf-8 -*-
"""录音数据批量落位校验与转写（录音 Runbook 第 1-3 步自动化，P2-M）
====================================================================
录音数据到位前即可用；数据到位后一条命令完成落位校验 → 批量转写。

数据安全（约法三章第 1 条）：
  - 本脚本只允许在 **git 已忽略** 的目录（如 病历资料/录音/）内读写——
    启动时强制校验 `git check-ignore`，不忽略则拒绝运行；
  - 转写稿派生自真实录音（含患者信息），同样只写入该目录内，绝不入 Git。

用法：
    # 第 1 步·落位校验（无需任何密钥）：格式/命名/可读性 → 落位报告.md
    python3 tools/asr_ingest.py 病历资料/录音 --validate

    # 第 2-3 步·批量转写（需 .llm_env 中 ASR_* 密钥已配置）
    python3 tools/asr_ingest.py 病历资料/录音 --transcribe --engine xfyun

命名约定（校验时不符合仅告警不拒绝）：
    患者编号_住院日期8位_文书类型.wav   例：DA0001_20231108_入院记录.wav
    文件名须含 8 位日期；文书类型关键字：入院/首程/首次病程/病程/出院/手术
"""
import argparse, json, os, re, struct, subprocess, sys, time

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DOC_HINTS = ("入院", "首程", "首次病程", "病程", "出院", "手术")
DATE_RE = re.compile(r"20\d{6}")

def fail(msg):
    print(f"❌ {msg}", file=sys.stderr)
    sys.exit(1)

def ensure_ignored(path):
    """数据安全强制项：目标目录必须被 git 忽略（真实录音/转写稿永不入库）。"""
    p = os.path.abspath(path)
    if not os.path.isdir(p):
        fail(f"目录不存在：{p}（请先把录音按命名约定放入该目录）")
    r = subprocess.run(["git", "check-ignore", os.path.relpath(p, REPO)],
                       cwd=REPO, capture_output=True, text=True)
    if r.returncode != 0:
        fail(f"目录未被 .gitignore 忽略（{p}）——真实录音与转写稿永不入库，"
             f"请将数据放入 病历资料/ 等已忽略目录，或在 .gitignore 增加对应规则后重试")
    return p

# ---------------- WAV 头解析（与 mcp-server is_16k_mono_wav 同判定，独立实现免依赖） ----------------
def wav_info(path):
    """返回 (ok, detail, duration_sec)。仅解析 RIFF/fmt 块，不读数据块。"""
    try:
        with open(path, "rb") as f:
            head = f.read(12)
            if len(head) < 12 or head[:4] != b"RIFF" or head[8:12] != b"WAVE":
                return False, "非 WAV(RIFF) 文件", 0
            data_bytes = 0
            while True:
                hdr = f.read(8)
                if len(hdr) < 8:
                    break
                cid, size = hdr[:4], struct.unpack("<I", hdr[4:8])[0]
                if cid == b"fmt ":
                    fmt = f.read(size)
                    audio_fmt, ch, rate = struct.unpack("<HHI", fmt[:8])
                elif cid == b"data":
                    data_bytes = size
                    break
                else:
                    f.seek(size, 1)
            if audio_fmt != 1:
                return False, f"非 PCM 编码（format={audio_fmt}），需先转 16kHz 单声道 PCM", 0
            if ch != 1 or rate != 16000:
                return False, f"{rate}Hz/{ch}ch —— 非目标格式 16kHz/单声道（ASR 直通要求）", 0
            return True, "16kHz 单声道 PCM", data_bytes / (16000 * 2)
    except Exception as e:
        return False, f"文件不可读: {e}", 0

def scan(audio_dir):
    files = sorted(f for f in os.listdir(audio_dir)
                   if f.lower().endswith((".wav", ".mp3", ".m4a")) and not f.startswith("."))
    rows, warns = [], []
    for name in files:
        p = os.path.join(audio_dir, name)
        ext = name.rsplit(".", 1)[-1].lower()
        row = {"file": name, "date": "", "doc_hint": "", "ok": "", "detail": "", "sec": 0}
        dates = DATE_RE.findall(name)
        row["date"] = dates[0] if dates else ""
        if not dates:
            warns.append(f"{name}：文件名无 8 位日期（约定：患者编号_日期_文书类型.wav）")
        hint = next((h for h in DOC_HINTS if h in name), "")
        row["doc_hint"] = hint
        if not hint:
            warns.append(f"{name}：文件名未含文书类型关键字（{'/'.join(DOC_HINTS[:3])}…）")
        if ext == "wav":
            ok, detail, sec = wav_info(p)
            row.update(ok=("✓" if ok else "✗"), detail=detail, sec=round(sec, 1))
            if not ok:
                warns.append(f"{name}：{detail}")
        else:
            row.update(ok="—", detail=f"{ext} 原始格式，转写前由引擎/人工转 16k WAV", sec=0)
        rows.append(row)
    return rows, warns

def cmd_validate(audio_dir):
    rows, warns = scan(audio_dir)
    n_ok = sum(1 for r in rows if r["ok"] == "✓")
    total_sec = sum(r["sec"] for r in rows)
    lines = ["# 录音落位报告", "", f"- 目录：{os.path.relpath(audio_dir, REPO)}",
             f"- 文件数：{len(rows)}（16kHz 单声道 PCM 达标 {n_ok}）",
             f"- 达标音频总时长：{total_sec/60:.1f} 分钟", ""]
    if rows:
        lines += ["| 文件 | 日期 | 文书 | 格式 | 详情 | 时长(s) |",
                  "|---|---|---|---|---|---|"]
        lines += [f"| {r['file']} | {r['date'] or '—'} | {r['doc_hint'] or '—'} "
                  f"| {r['ok']} | {r['detail']} | {r['sec'] or '—'} |" for r in rows]
    if warns:
        lines += ["", "## 命名/格式告警"] + [f"- {w}" for w in warns]
    lines += ["", "下一步：格式告警处理后，配置 ASR 密钥并执行 "
              f"`python3 tools/asr_ingest.py {os.path.relpath(audio_dir, REPO)} --transcribe`"]
    out = os.path.join(audio_dir, "落位报告.md")
    with open(out, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    print(f"✓ 扫描 {len(rows)} 个文件（格式达标 {n_ok}，告警 {len(warns)}）")
    print(f"✓ 报告已写入（git 已忽略目录内）：{os.path.relpath(out, REPO)}")
    for w in warns[:5]:
        print(f"  ⚠ {w}")

def _load_engine(engine_name):
    sys.path.insert(0, os.path.join(REPO, "mcp-server"))
    try:
        import asr_mcp_server
    except ImportError as e:
        fail(f"无法加载 mcp-server 引擎适配器: {e}")
    os.environ.setdefault("ASR_DEFAULT_ENGINE", engine_name)
    return asr_mcp_server.pick_engine(engine_name if engine_name != "default" else None)

def cmd_transcribe(audio_dir, engine_name):
    engine = _load_engine(engine_name)
    rows, _ = scan(audio_dir)
    targets = [r for r in rows if r["ok"] == "✓"]
    if not targets:
        fail("没有格式达标的 16kHz 单声道 PCM WAV 可转写——先按落位报告处理格式")
    out_dir = os.path.join(audio_dir, "转写稿")
    os.makedirs(out_dir, exist_ok=True)
    done, failed = 0, []
    for r in targets:
        src = os.path.join(audio_dir, r["file"])
        base = r["file"].rsplit(".", 1)[0]
        t0 = time.time()
        try:
            t = engine.transcribe(src, speakers=True)
            d = t.to_dict()
            with open(os.path.join(out_dir, base + ".txt"), "w", encoding="utf-8") as f:
                f.write(d.get("text") or "")
            with open(os.path.join(out_dir, base + ".json"), "w", encoding="utf-8") as f:
                json.dump(d, f, ensure_ascii=False, indent=1)
            done += 1
            print(f"  ✓ {r['file']} → {base}.txt（{d.get('engine')}，{time.time()-t0:.1f}s，"
                  f"{len(d.get('segments') or [])} 段）")
        except Exception as e:
            failed.append(f"{r['file']}: {e}")
            print(f"  ✗ {r['file']} — {e}")
    print(f"\n转写完成 {done}/{len(targets)}，输出目录：{os.path.relpath(out_dir, REPO)}")
    if failed:
        print("失败清单（已列出，可单独重试）：")
        for f_ in failed:
            print(f"  - {f_}")
        sys.exit(1)

def main():
    ap = argparse.ArgumentParser(description="录音批量落位校验与转写（数据安全：仅限 git 忽略目录）")
    ap.add_argument("dir", help="录音目录（须被 .gitignore 忽略，如 病历资料/录音）")
    ap.add_argument("--validate", action="store_true", help="落位校验（默认动作，无需密钥）")
    ap.add_argument("--transcribe", action="store_true", help="批量转写达标的 WAV（需 ASR_* 密钥）")
    ap.add_argument("--engine", default="default", help="引擎：default/xfyun/aliyun/sensetime/local-whisper")
    args = ap.parse_args()
    audio_dir = ensure_ignored(args.dir)
    if args.transcribe:
        cmd_transcribe(audio_dir, args.engine)
    else:
        cmd_validate(audio_dir)

if __name__ == "__main__":
    main()
