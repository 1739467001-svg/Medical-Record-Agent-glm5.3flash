# -*- coding: utf-8 -*-
"""彩排音频合成（录音链路全真彩排 · P2-O）：脱敏对话稿 → 双声 TTS → 16kHz 单声道 PCM WAV。
====================================================================
数据安全：内容取自 demo/data/dialogue.json（按金标准入院记录重构的脱敏模拟对话，
患者=患者A/患者B，无任何真实患者信息）；音频只写入 git 忽略目录（启动即校验）。
用途：在真实录音到位前，把「录音 → 落位 → 转写 → 生成 → 评测」全链路预演一遍；
真实录音到位后同一套流程直接替换音频即可（即插即跑）。

用法（macOS，零三方依赖）：
    python3 tools/make_rehearsal_audio.py                    # 默认 病历资料/彩排录音/
    python3 tools/make_rehearsal_audio.py --dir 病历资料/录音 --patients DA0001,DA0002
说明：
- 每位患者合成 1 个入院问诊 WAV：多轮拼接，医生/患者用不同声部（Eddy/Grandma），
  轮间留 0.6s 静音模拟自然对话节律；
- 同时落盘 对话稿标注.json（每轮 {start,end,text,role}）——转写字错率对齐的"标准答案"。
"""
import argparse, json, os, subprocess, sys, tempfile, wave

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# 标准声部（已实测可出声）：macOS「新奇声部」(Eddy/Grandma 等) 列表里可见但未安装会静音，
# 运行时 probe_voice 拦截；医生/患者用不同音色（均为普通话，声部区分即可，不追求拟真）
DOC_VOICE, PAT_VOICE = "Meijia", "Tingting"
TURN_GAP_SEC = 0.6
TARGET_RATE, TARGET_CH = 16000, 1

def probe_voice(voice):
    """声部可用性探测：合成测试句，时长 <0.5s 视为静音（未安装的声部）。"""
    r = subprocess.run(["say", "-v", voice, "-o", "/tmp/mra_voice_probe.aiff", "语音探测测试"],
                       capture_output=True, text=True)
    info = subprocess.run(["afinfo", "/tmp/mra_voice_probe.aiff"], capture_output=True, text=True)
    for line in info.stdout.splitlines():
        if "estimated duration" in line:
            if float(line.split()[2]) < 0.5:
                raise RuntimeError(f"声部 {voice} 未安装/输出静音——请在 系统设置→语音 中下载，"
                                   f"或改用其他已装声部（实测可用：Tingting/Meijia/Sinji）")
            return
    raise RuntimeError(f"声部 {voice} 探测失败（afinfo 无时长输出）")

def ensure_ignored(path):
    p = os.path.abspath(path)
    os.makedirs(p, exist_ok=True)
    r = subprocess.run(["git", "check-ignore", os.path.relpath(p, REPO)],
                       cwd=REPO, capture_output=True, text=True)
    if r.returncode != 0:
        print(f"❌ 目标目录未被 .gitignore 忽略（{p}）——彩排音频永不入库", file=sys.stderr)
        sys.exit(1)
    return p

def tts_aiff(text, voice, rate_words_per_min=180):
    """say → AIFF 临时文件（rate 控制语速：默认偏慢更像问诊）。返回路径。"""
    fd, out = tempfile.mkstemp(suffix=".aiff"); os.close(fd)
    r = subprocess.run(["say", "-v", voice, "-r", str(rate_words_per_min), "-o", out, text],
                       capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"say 失败: {r.stderr[:200]}")
    return out

def aiff_to_pcm(path):
    """AIFF 任意采样率 → (pcm_bytes(int16), rate, channels)。用 afconvert 转 wav 再读最稳。"""
    fd, wav = tempfile.mkstemp(suffix=".wav"); os.close(fd)
    r = subprocess.run(["afconvert", "-f", "WAVE", "-d", "LEI16", path, wav],
                       capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"afconvert 失败: {r.stderr[:200]}")
    try:
        with wave.open(wav, "rb") as w:
            return w.readframes(w.getnframes()), w.getframerate(), w.getnchannels()
    finally:
        os.remove(wav)

def resample_to_16k_mono(pcm, rate, ch):
    """极简重采样（线性插值）+ 混单声道：彩排够用，避免引 numpy。"""
    import audioop  # deprecated in 3.13 但 3.12 可用
    if ch != 1:
        pcm = audioop.tomono(pcm, 2, 0.5, 0.5)
    if rate != TARGET_RATE:
        pcm, _ = audioop.ratecv(pcm, 2, 1, rate, TARGET_RATE, None)
    return pcm

def synth_dialogue(turns, out_wav, out_label):
    """多轮 TTS 拼接 → 16k 单声道 WAV + 对齐标注。返回总时长（秒）。"""
    gap = b"\x00\x00" * int(TURN_GAP_SEC * TARGET_RATE)
    pcm_all, marks, cursor = [], [], 0
    for i, t in enumerate(turns):
        voice = DOC_VOICE if t["role"] == "医生" else PAT_VOICE
        aiff = tts_aiff(t["text"], voice)
        try:
            pcm, rate, ch = aiff_to_pcm(aiff)
            pcm = resample_to_16k_mono(pcm, rate, ch)
        finally:
            os.remove(aiff)
        n_sec = len(pcm) / (TARGET_RATE * 2)
        marks.append({"role": t["role"], "text": t["text"],
                      "start": round(cursor, 2), "end": round(cursor + n_sec, 2)})
        pcm_all.append(pcm)
        cursor += n_sec + TURN_GAP_SEC
        pcm_all.append(gap)
        print(f"  [{i+1}/{len(turns)}] {t['role']} {n_sec:.1f}s 「{t['text'][:18]}…」")
    with wave.open(out_wav, "wb") as w:
        w.setnchannels(TARGET_CH); w.setsampwidth(2); w.setframerate(TARGET_RATE)
        w.writeframes(b"".join(pcm_all))
    with open(out_label, "w", encoding="utf-8") as f:
        json.dump({"wav": os.path.basename(out_wav), "rate": TARGET_RATE,
                   "turns": marks}, f, ensure_ascii=False, indent=1)
    return cursor

def main():
    ap = argparse.ArgumentParser(description="彩排音频合成（脱敏对话稿 → 双声 16kHz WAV）")
    ap.add_argument("--dir", default="病历资料/彩排录音", help="输出目录（须被 git 忽略）")
    ap.add_argument("--patients", default="DA0001", help="合成哪些患者（当前对话稿为患者A）")
    args = ap.parse_args()
    out_dir = ensure_ignored(args.dir)
    probe_voice(DOC_VOICE)
    probe_voice(PAT_VOICE)
    print(f"✓ 声部探测通过：医生={DOC_VOICE} 患者={PAT_VOICE}")
    dialogue = json.load(open(os.path.join(REPO, "demo", "data", "dialogue.json"), encoding="utf-8"))
    date_map = {"DA0001": "20231108"}   # 与金标准住院日期对齐（住院次窗口过滤用）
    for code in [c.strip() for c in args.patients.split(",") if c.strip()]:
        name = f"{code}_{date_map.get(code, '20231108')}_入院记录.wav"
        print(f"▶ 合成 {name}（{len(dialogue)} 轮，医生={DOC_VOICE} 患者={PAT_VOICE}）")
        total = synth_dialogue(dialogue, os.path.join(out_dir, name),
                               os.path.join(out_dir, name.replace(".wav", ".标注.json")))
        print(f"  ✓ 完成，总时长 {total/60:.1f} 分钟\n")
    print(f"✓ 全部完成 → {os.path.relpath(out_dir, REPO)}（含 标注.json 标准答案）")

if __name__ == "__main__":
    main()
