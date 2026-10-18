# -*- coding: utf-8 -*-
"""语音增强前端 A/B 实验台（选型文档 §五.4「评估语音增强前端」）
==============================================================
门诊嘈杂音频上模型退化明显（whisper 幻觉/同句循环、一致率跌到 41%），
候选对策之一是**识别前先做降噪/归一化**。本工具把同一音频的「原始」与
「ffmpeg 增强」两版喂给**同一引擎**，量化三件事：

  ① 字符一致率（两版转写差多少 —— 差得越多说明增强对识别影响越大）
  ② 质量告警变化（重复循环/繁体输出是否消退）
  ③ 与既有引擎草稿的一致率（作为**弱参照**：草稿也是模型产物，只说明"偏离程度"，
     不能证明谁更准）

⚠ 口径声明：**没有人工参照稿时无法判定优劣**——本工具只给"变化量"与"告警信号"，
是否采纳需人工听辨或待参照稿到位后复核。增强效果需在标注语料上复验才算定论。

用法：
    python3 tools/asr_denoise_ab.py 病历资料/门诊/1.m4a --engine funasr
    python3 tools/asr_denoise_ab.py <音频> --filters "highpass=f=80,afftdn=nf=-25,loudnorm=I=-20"
    python3 tools/asr_denoise_ab.py <音频> --preset mild|standard|strong

依赖：ffmpeg（增强）+ 所选引擎依赖；产物落 /tmp，不入库（实验性）。
"""
import argparse, difflib, json, os, re, subprocess, sys, tempfile, time

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "mcp-server"))
import asr_mcp_server as mcp  # noqa: E402

CLEAN_RE = re.compile(r"[，。？！、\s,.?!——；;：:]")
# 语气词：双侧同去（whisper 丢语气词、SenseVoice 全保留，同去后一致率才反映内容差异）
FILLER_RE = re.compile(r"[嗯哦呃啊哎唉呀哈嘛吧呢噢]")

PRESETS = {
    # 轻：仅高通去低频轰鸣（空调/桌面震动）
    "mild": "highpass=f=80",
    # 标准：高通 + FFT 降噪（噪声底 -25dB）+ EBU R128 响度归一
    "standard": "highpass=f=80,afftdn=nf=-25,loudnorm=I=-20:TP=-2:LRA=11",
    # 强：标准之上加非局部均值降噪（更激进，易引入"水声"伪影）
    "strong": "highpass=f=80,afftdn=nf=-30,anlmdn=s=0.00002,loudnorm=I=-20:TP=-2:LRA=11",
}


def chars(text):
    return [c for c in text if not CLEAN_RE.match(c) and not FILLER_RE.match(c)]


def agree(a, b):
    """去标点语气词后的字符一致率（与 asr_reconcile 同口径）。"""
    ca, cb = chars(a), chars(b)
    if not ca or not cb:
        return 0.0
    ops = difflib.SequenceMatcher(None, ca, cb, autojunk=False).get_opcodes()
    matched = sum(i2 - i1 for tag, i1, i2, j1, j2 in ops if tag == "equal")
    return matched / max(len(ca), len(cb))


def make_wav(src, dst, filters):
    """ffmpeg 转 16k 单声道 PCM WAV（可选增强滤镜链）。"""
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", src]
    if filters:
        cmd += ["-af", filters]
    cmd += ["-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", dst]
    t0 = time.time()
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"ffmpeg 失败：{r.stderr.strip()[:300]}")
    return time.time() - t0


def levels(path):
    """mean/max 音量（dB），用于确认增强没有削顶或整体压扁。"""
    r = subprocess.run(["ffmpeg", "-hide_banner", "-i", path, "-af", "volumedetect",
                        "-f", "null", "-"], capture_output=True, text=True)
    out = {}
    for k in ("mean_volume", "max_volume"):
        m = re.search(rf"{k}: (-?[\d.]+) dB", r.stderr)
        if m:
            out[k] = float(m.group(1))
    return out


def run_engine(wav, engine, model_env):
    """单次转写 + 统一后处理（热词修正开、LLM 纠错关——要引擎原声）。"""
    t0 = time.time()
    tr = mcp.pick_engine(engine).transcribe(wav, speakers=True)
    tr = mcp.postprocess(tr, correct=True, llm_polish=False)
    return tr, time.time() - t0


def main():
    ap = argparse.ArgumentParser(description="语音增强前端 A/B 实验台")
    ap.add_argument("audio", help="待测音频（m4a/mp3/wav 均可）")
    ap.add_argument("--engine", default="funasr", help="引擎名（缺省 funasr）")
    ap.add_argument("--preset", default="standard", choices=sorted(PRESETS), help="增强强度档")
    ap.add_argument("--filters", default="", help="自定义 ffmpeg 滤镜链（覆盖 --preset）")
    ap.add_argument("--funasr-model", default="", help="设置 ASR_FUNASR_MODEL")
    ap.add_argument("--whisper-model", default="", help="设置 ASR_LOCAL_MODEL")
    ap.add_argument("--draft", default="", help="既有引擎草稿 txt（弱参照，可选）")
    ap.add_argument("--out", default="", help="结果 JSON 路径（缺省 /tmp/asr_denoise_ab.json）")
    a = ap.parse_args()
    if a.funasr_model:
        os.environ["ASR_FUNASR_MODEL"] = a.funasr_model
    if a.whisper_model:
        os.environ["ASR_LOCAL_MODEL"] = a.whisper_model

    filters = a.filters or PRESETS[a.preset]
    tmp = tempfile.mkdtemp(prefix="asr_enh_")
    raw, enh = os.path.join(tmp, "raw.wav"), os.path.join(tmp, "enh.wav")
    make_wav(a.audio, raw, "")
    make_wav(a.audio, enh, filters)
    print(f"增强链：{filters}")
    print(f"电平　原始 {levels(raw)}　增强 {levels(enh)}")

    res = {"audio": a.audio, "engine": a.engine, "filters": filters,
           "levels": {"raw": levels(raw), "enh": levels(enh)}, "runs": {}}
    for label, wav in (("原始", raw), ("增强", enh)):
        tr, dt = run_engine(wav, a.engine, None)
        qw = mcp.quality_warnings(tr.text)
        res["runs"][label] = {"text": tr.text, "sec": round(dt, 1), "chars": len(tr.text),
                              "segments": len(tr.segments), "warnings": qw,
                              "hotwords_applied": tr.hotwords_applied}
        print(f"[{label}] {dt:.1f}s　{len(tr.text)} 字　分段 {len(tr.segments)}　"
              f"告警：{qw or '无'}")

    agr = agree(res["runs"]["原始"]["text"], res["runs"]["增强"]["text"])
    res["agreement_raw_vs_enh"] = round(agr, 4)
    print(f"\n原始 × 增强 字符一致率：**{agr*100:.1f}%**"
          f"（越低说明增强改动越大，需人工听辨判定优劣）")
    if a.draft and os.path.exists(a.draft):
        lines = open(a.draft, encoding="utf-8").read().splitlines()
        draft = "".join(m.group(1) for m in
                        (re.match(r"^\[[^]]*\]\s*(.+)$", ln) for ln in lines) if m)
        for label in res["runs"]:
            ar = agree(res["runs"][label]["text"], draft)
            res["runs"][label]["agree_vs_draft"] = round(ar, 4)
            print(f"{label} × 既有草稿 一致率（弱参照，非真值）：{ar*100:.1f}%")
    out = a.out or "/tmp/asr_denoise_ab.json"
    json.dump(res, open(out, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(f"\n✓ 结果 {out}")
    print("⚠ 无人工参照稿时不能判定优劣；采纳前需在标注语料上复验（见 "
          "docs/自建ASR选型与实施路径.md §五.4）")


if __name__ == "__main__":
    main()
