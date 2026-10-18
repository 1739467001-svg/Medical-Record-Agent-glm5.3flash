# -*- coding: utf-8 -*-
"""
ASR MCP Server（语音识别模型上下文协议服务器）
=================================================
为「东阿县人民医院 AI 病历智能体」提供统一的语音识别入口：
上层（Demo 录音步骤 / P1 多智能体生成器）只面向 MCP 工具与统一转写格式，
底层引擎（讯飞 / 阿里云 / 商汤 / 本地 Whisper / Mock）可插拔、密钥后补。

协议：MCP stdio 传输（换行分隔 JSON-RPC 2.0），零第三方依赖，兼容 Python 3.9+。
引擎依赖按需安装（仅启用对应引擎时才 import）：websocket-client、requests、faster-whisper。

配置（环境变量，未配置的引擎自动标记为未就绪）：
  ASR_DEFAULT_ENGINE      默认引擎（缺省 mock）
  ASR_XFYUN_APP_ID        讯飞实时语音转写 RTASR 应用 ID
  ASR_XFYUN_API_KEY       讯飞 RTASR APIKey
  ASR_ALIYUN_ACCESS_KEY_ID / ASR_ALIYUN_ACCESS_KEY_SECRET / ASR_ALIYUN_APP_KEY
                          阿里云智能语音交互·录音文件识别
  ASR_SENSE_API_BASE / ASR_SENSE_API_KEY
                          商汤（接口资料待商汤侧确认，当前为占位适配器）
  ASR_LOCAL_MODEL         本地模型路径或名称（faster-whisper，私有化研发用）
  ASR_LOCAL_DEVICE        cpu/cuda（缺省 cpu）；ASR_LOCAL_COMPUTE 缺省按设备 int8/float16
  ASR_LOCAL_BEAM          解码波束（缺省 5=引擎默认；患者F 单例 A/B：beam5 66.5% / beam1 70.2%，事实 12/12 vs 11/12）
  ASR_FUNASR_MODEL        本地 FunASR 模型（缺省 iic/SenseVoiceSmall；可切 SeACo-Paraformer 热词模型）
  ASR_FUNASR_DEVICE       FunASR 设备（cpu/cuda:0，缺省 cpu）
  ASR_FUNASR_HOTWORD      热词串覆盖（缺省取医疗热词表正确词；仅上下文偏置模型生效）
  ASR_FUNASR_CHUNK_S      超长段切分上限秒（缺省 SenseVoice 15 / Paraformer 系 10）
  ASR_FUNASR_PUNC=0       关闭 Paraformer 标点模型（缺省开）
  ASR_FUNASR_VAD=0        关闭 VAD 长音频切分（缺省开）

工具：
  asr_engines()                              查看引擎清单与就绪状态
  asr_prepare_audio(audio_path, out_dir?)    音频预处理 → 16kHz 单声道 WAV（ffmpeg/afconvert）
  asr_transcribe(audio_path, engine?, speakers?)  转写，返回统一 Transcript JSON

统一 Transcript 格式（对齐 PRD F2：角色分离 + 医疗热词 + 过程稿溯源）：
  {
    "engine": "xfyun", "audio": "...", "duration_sec": 152.3,
    "text": "全文…", "language": "zh",
    "segments": [{"start": 0.0, "end": 4.2, "speaker": "医生", "text": "…"}, …],
    "mock": false, "warnings": ["…"]
  }
"""
import os, re, sys, io, json, base64, hashlib, hmac, struct, time, urllib.parse, importlib.util, contextlib
from typing import Optional

# ---------------- 音频预处理 ----------------

def is_16k_mono_wav(path: str) -> bool:
    """已是 16kHz 单声道 16bit PCM WAV 时各引擎可直用，无需转码（生产容器无 ffmpeg 的前置）。"""
    try:
        with open(path, "rb") as f:
            head = f.read(44)
        if head[:4] != b"RIFF" or head[8:12] != b"WAVE" or head[12:16] != b"fmt ":
            return False
        fmt, ch = struct.unpack("<H", head[20:22])[0], struct.unpack("<H", head[22:24])[0]
        rate = struct.unpack("<I", head[24:28])[0]
        bits = struct.unpack("<H", head[34:36])[0]
        return fmt == 1 and ch == 1 and rate == 16000 and bits == 16
    except Exception:
        return False

def prepare_audio(audio_path: str, out_dir: str = "") -> str:
    """转 16kHz 单声道 16bit PCM WAV（讯飞/多数引擎要求）。优先 ffmpeg，macOS 回退 afconvert；
    输入已是目标格式时原样返回（前端 MediaRecorder 侧直接打包 16k WAV 的场景）。"""
    audio_path = os.path.abspath(audio_path)
    if not os.path.exists(audio_path):
        raise FileNotFoundError(audio_path)
    if is_16k_mono_wav(audio_path):
        return audio_path
    out_dir = os.path.abspath(out_dir) if out_dir else os.path.dirname(audio_path)
    os.makedirs(out_dir, exist_ok=True)
    stem = os.path.splitext(os.path.basename(audio_path))[0]
    out = os.path.join(out_dir, f"{stem}_16k.wav")
    if os.path.exists(out) and os.path.getmtime(out) >= os.path.getmtime(audio_path):
        return out
    import subprocess
    if shutil_which("ffmpeg"):
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", audio_path,
                        "-ar", "16000", "-ac", "1", "-sample_fmt", "s16", out], check=True)
    elif shutil_which("afconvert"):
        subprocess.run(["afconvert", "-f", "WAVE", "-d", "LEI16@16000", "-c", "1", audio_path, out], check=True)
    else:
        raise RuntimeError("需要 ffmpeg 或 macOS afconvert 之一做音频转码")
    return out

def shutil_which(cmd):
    for d in os.environ.get("PATH", "").split(os.pathsep):
        p = os.path.join(d, cmd)
        if os.path.isfile(p) and os.access(p, os.X_OK):
            return p
    return None

def wav_duration_sec(path):
    """读取 WAV 头估算时长（数据块大小/字节率）。"""
    with open(path, "rb") as f:
        head = f.read(12)
        if head[:4] != b"RIFF" or head[8:12] != b"WAVE":
            return 0.0
        while True:
            ch = f.read(8)
            if len(ch) < 8: return 0.0
            cid, size = ch[:4], struct.unpack("<I", ch[4:8])[0]
            if cid == b"fmt ":
                fmt = f.read(size); byte_rate = struct.unpack("<I", fmt[8:12])[0]
            elif cid == b"data":
                if byte_rate: return round(size / byte_rate, 2)
                return 0.0
            else:
                f.seek(size + (size & 1), 1)  # 相对跳过块内容（缺 whence=1 会跳到绝对偏移，带 LIST 块的 WAV 全算错）

# ---------------- 引擎适配层 ----------------
class Transcript:
    def __init__(self, engine, audio, text, segments, mock=False, warnings=None):
        self.engine, self.audio, self.text = engine, audio, text
        self.segments = segments
        self.mock, self.warnings = mock, warnings or []
        self.text_raw = text  # LLM 纠错前原文（双版本留痕；correct_hotwords 不改此字段）
        self.hotwords_applied = []  # 热词修正清单（"错→对×N"），热词生长语料（harvest 用）
        self.corrections = []       # LLM 方言纠错对 [{before,after}]，同上
    def to_dict(self):
        return {"engine": self.engine, "audio": self.audio,
                "duration_sec": wav_duration_sec(self.audio) if self.audio.lower().endswith(".wav") else None,
                "text": self.text, "text_raw": self.text_raw, "language": "zh",
                "segments": self.segments, "mock": self.mock, "warnings": self.warnings,
                "hotwords_applied": self.hotwords_applied, "corrections": self.corrections}

# ---------------- 医疗热词后处理（PRD D4：同音听错纠正，保守短语规则） ----------------
_HOTWORDS_CACHE = None

def _load_hotwords():
    """加载热词表：mcp-server/medical_hotwords.json（种子）或 MRA_MEDICAL_HOTWORDS 指定的扩展文件。"""
    global _HOTWORDS_CACHE
    if _HOTWORDS_CACHE is not None:
        return _HOTWORDS_CACHE
    path = os.environ.get("MRA_MEDICAL_HOTWORDS") or \
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "medical_hotwords.json")
    try:
        data = json.load(open(path, encoding="utf-8"))
        _HOTWORDS_CACHE = {str(k): str(v) for k, v in
                           (data.get("规则（勿改本键）") or data).items() if not str(k).startswith("_") and not str(k).startswith("规则")}
    except Exception as e:
        _HOTWORDS_CACHE = {}
        print(f"[asr] 热词表加载失败（空表继续）: {e}", file=sys.stderr)
    return _HOTWORDS_CACHE

_PROMPT_CACHE = None

def _medical_prompt():
    """本地引擎的领域提示（initial_prompt）：引导声学模型偏向医患问诊语域。
    实测依据（患者F 首例真实方言录音，三轮 A/B）：
      - 短主题式提示（~45字）最优："消炎针"直接识别正确、标点完整、无首尾幻觉；
      - 长词表提示（>100字）反而诱发首尾幻觉（"七十年前"/"字幕by…"），不采用；
      - 无提示时出现重复循环（同句×7），配合 condition_on_previous_text=False 消除。
    按案例定制：case_ingest 可用申请单诊断构造 ASR_LOCAL_PROMPT（如含"阑尾炎"时识别最佳）。"""
    global _PROMPT_CACHE
    if _PROMPT_CACHE is not None:
        return _PROMPT_CACHE
    override = os.environ.get("ASR_LOCAL_PROMPT")
    if override:
        _PROMPT_CACHE = override[:220]
        return _PROMPT_CACHE
    _PROMPT_CACHE = ("以下是医患问诊对话，含山东方言口音，涉及腹痛、发热、恶心呕吐、"
                     "消炎针、输液、手术、住院。")
    return _PROMPT_CACHE

def correct_hotwords(transcript):
    """对 Transcript 应用热词修正（全文+分段同步，warnings 留痕）。返回 (transcript, 修正清单)。
    设计约束：只修 ASR 听错（同音字），不做规范用语改写——保持绿字段可溯源到医生实际说出的话。"""
    rules = _load_hotwords()
    if not rules:
        return transcript, []
    applied = []
    text = transcript.text
    for wrong, right in sorted(rules.items(), key=lambda kv: -len(kv[0])):
        n = text.count(wrong)
        if n:
            text = text.replace(wrong, right)
            applied.append(f"{wrong}→{right}×{n}")
    if applied:
        transcript.text = text
        for seg in transcript.segments:
            t = str(seg.get("text", ""))
            for wrong, right in sorted(rules.items(), key=lambda kv: -len(kv[0])):
                t = t.replace(wrong, right)
            seg["text"] = t
        transcript.warnings = (transcript.warnings or []) + ["医疗热词修正：" + "；".join(applied)]
    return transcript, applied


class BaseEngine:
    name = "base"
    def configured(self): raise NotImplementedError
    def status(self): raise NotImplementedError
    def transcribe(self, audio_path, speakers=True, **opts) -> Transcript:
        raise NotImplementedError

# 转写质量守卫（引擎无关）：重复循环与繁体输出是 whisper 类模型在噪声/难例音频上的
# 典型崩溃形态（门诊实测：嘈杂门诊音频上同句循环+繁体字，无提示词也复现）
_TRAD_HINTS = "為麼這現來個沒對後語說樣時開關們過還聽講讓東區將與萬號體點從無發長門問題醫藥護檢結報"

def quality_warnings(text):
    import collections
    warns = []
    if len(text) >= 8:
        grams = collections.Counter(text[i:i + 4] for i in range(len(text) - 4))
        top = grams.most_common(1)[0][1] if grams else 0
        if top >= 3:
            warns.append(f"⚠ 疑似重复循环（4-gram 最高重复 {top} 次）——本段转写质量存疑，建议复听")
    trad = sum(text.count(c) for c in _TRAD_HINTS)
    if trad >= 3 and trad / max(len(text), 1) > 0.02:
        warns.append("⚠ 疑似繁体输出（难例音频退化形态）——本段转写质量存疑，建议复听")
    return warns

class MockEngine(BaseEngine):
    """离线演示引擎：返回结构完整的模拟转写（角色分离），用于链路联调与 Demo，无任何外呼。"""
    name = "mock"
    def configured(self): return True
    def status(self): return {"ready": True, "note": "模拟引擎：仅返回结构示例，用于联调"}
    def transcribe(self, audio_path, speakers=True, **opts):
        demo = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "demo", "data", "dialogue.json")
        segs = []
        if os.path.exists(demo):
            turns = json.load(open(demo, encoding="utf-8"))
            t = 0.0
            for i, turn in enumerate(turns[:8]):
                dur = round(min(9.0, max(2.0, len(turn["text"]) * 0.22)), 2)
                segs.append({"start": round(t, 2), "end": round(t + dur, 2),
                             "speaker": turn["role"] if speakers else "未知", "text": turn["text"]})
                t += dur + 0.4
        else:
            segs = [{"start": 0.0, "end": 3.0, "speaker": "医生" if speakers else "未知", "text": "（模拟引擎：未找到演示对话数据）"}]
        return Transcript("mock", audio_path, "".join(s["text"] for s in segs), segs, mock=True,
                          warnings=["模拟引擎输出，不可用于任何评测或归档场景"])

class XfyunEngine(BaseEngine):
    """讯飞语音识别（可插拔两种接口形态，密钥后补即用；签名/分片以官方文档为准，联调时核对）：

    A. RTASR 实时语音转写（wss://rtasr.xfyun.cn/v1/ws）——流式 WebSocket，适合长录音与实时字幕。
       鉴权：appid + ts + signa（HMAC-SHA256(base64(md5(apiKey)), appid+ts) 再 base64+urlencode）。
       发送：裸音频分片 1280B/40ms，间隔 40ms，结束发 {"end": true}。
       返回：action=result 的 data 内按 seg_id 分段（type=2 为最终结果）。
    B. SFASR 一句话/短语音（ws://sfasr 或 HTTPS api）——短问诊片段备用。
    选择：ASR_XFYUN_MODE=rtasr|sfasr（默认 rtasr）。热词：ASR_XFYUN_HOTWORDS（名称，
    讯飞控制台预制热词表；未配置时退化为本地热词后处理 correct_hotwords）。
    角色分离：RTASR 多说话人需订正版/企业版（返回 rl 内 spk 字段）；基础版无角色，
    segments.speaker 标"未分离"由后处理标注（与本地 whisper 同策略）。"""
    name = "xfyun"
    def configured(self): return bool(os.environ.get("ASR_XFYUN_APP_ID") and os.environ.get("ASR_XFYUN_API_KEY"))
    def status(self):
        return {"ready": self.configured(),
                "note": f"讯飞{'RTASR 实时转写' if os.environ.get('ASR_XFYUN_MODE', 'rtasr') == 'rtasr' else '短语音'}："
                        "WebSocket 流式、医疗热词（控制台热词表或本地后处理）、多说话人需订正版",
                "env": ["ASR_XFYUN_APP_ID", "ASR_XFYUN_API_KEY",
                        "ASR_XFYUN_MODE(=rtasr|sfasr)", "ASR_XFYUN_HOTWORDS(热词表名,可选)"],
                "deps": "pip install websocket-client"}
    def _signa(self, app_id, api_key, ts):
        base_string = str(app_id) + str(ts)
        md5 = hashlib.md5(api_key.encode()).digest()
        base64_md5 = base64.b64encode(md5).decode()
        signa = base64.b64encode(hmac.new(base64_md5.encode(), base_string.encode(), hashlib.sha256).digest()).decode()
        return urllib.parse.quote(signa)
    def transcribe(self, audio_path, speakers=True, **opts):
        if not self.configured():
            raise RuntimeError("讯飞引擎未配置：请设置 ASR_XFYUN_APP_ID / ASR_XFYUN_API_KEY")
        try:
            import websocket  # websocket-client
        except ImportError:
            raise RuntimeError("缺少依赖：pip install websocket-client")
        mode = os.environ.get("ASR_XFYUN_MODE", "rtasr")
        if mode == "sfasr":
            raise RuntimeError("sfasr 短语音模式待接口文档到位后实现（当前可用 rtasr）")
        wav = prepare_audio(audio_path)
        app_id, api_key = os.environ["ASR_XFYUN_APP_ID"], os.environ["ASR_XFYUN_API_KEY"]
        ts = int(time.time())
        url = f"wss://rtasr.xfyun.cn/v1/ws?appid={app_id}&ts={ts}&signa={self._signa(app_id, api_key, ts)}"
        hw = os.environ.get("ASR_XFYUN_HOTWORDS", "")
        if hw:
            # 热词表名走 URL 参数（讯飞 RTASR 约定，联调核对）；未订正版会被服务端忽略
            url += f"&hotword={urllib.parse.quote(hw)}"
        frames = open(wav, "rb").read()
        piece = 1280  # 40ms @16kHz 16bit 单声道
        order = {}    # seg_id -> text（RTASR 按 seg_id 分段返回）
        spoke = {}    # seg_id -> spk（订正版多说话人；基础版无此字段）
        done_seg = set()
        # 超时兜底：假签名/网络异常时讯飞可能长时间不应答，连接与读超时各 15s
        ws = websocket.create_connection(url, timeout=15)
        try:
            for i in range(0, len(frames), piece):
                ws.send(frames[i:i + piece])
                time.sleep(0.04)
            ws.send(b'{"end": true}')
            idle = 0
            while True:
                try:
                    raw = ws.recv()
                except Exception:
                    break
                if not raw:
                    break
                idle = 0
                try:
                    msg = json.loads(raw)
                except (ValueError, TypeError):
                    continue
                if msg.get("action") == "error" or (msg.get("code") not in (None, 0, "0")):
                    raise RuntimeError(f"讯飞返回错误: {msg}")
                if msg.get("action") == "result":
                    data = json.loads(msg.get("data") or "{}")
                    seg_id = data.get("seg_id")
                    # ws 段内 cw 数组取每个候选首位（首候选=最优）；rl 支持多说话人 spk 字段
                    text, spk = "", None
                    for ri, rl in enumerate(data.get("rl", [])):
                        text += "".join(w.get("cw", [{}])[0].get("w", "") for w in rl.get("ws", []))
                        if rl.get("spk") is not None and spk is None:
                            spk = rl.get("spk")
                    if seg_id is not None and text:
                        order[int(seg_id)] = text
                        if spk is not None:
                            spoke[int(seg_id)] = spk
                    if str(data.get("type")) in ("2", 2):
                        done_seg.add(seg_id)
                    # 终止：收到全部最终段或服务端关闭（"type=2 为最终结果"，联调核对）
                    if done_seg and len(done_seg) >= len(order):
                        break
        finally:
            ws.close()
        texts = [order[k] for k in sorted(order)]
        has_spk = bool(spoke)
        segs = []
        for k in sorted(order):
            segs.append({"start": None, "end": None,
                         "speaker": (f"说话人{spoke[k]+1}" if has_spk and speakers else ("未分离" if speakers else "-")),
                         "text": order[k]})
        warns = []
        if not has_spk:
            warns.append("RTASR 基础版不带说话人分离：角色由后处理标注（订正版返回 spk 字段可直接用）")
        if hw:
            warns.append(f"已请求讯飞热词表「{hw}」（生效需控制台预制；本地热词后处理兜底仍会执行）")
        return Transcript("xfyun", audio_path, "".join(texts), segs, warnings=warns)

class AliyunEngine(BaseEngine):
    """阿里云 智能语音交互·录音文件识别（HTTP 提交+轮询，自带说话人分离）。密钥就绪后接入。"""
    name = "aliyun"
    def configured(self):
        return all(os.environ.get(k) for k in
                   ("ASR_ALIYUN_ACCESS_KEY_ID", "ASR_ALIYUN_ACCESS_KEY_SECRET", "ASR_ALIYUN_APP_KEY"))
    def status(self):
        return {"ready": self.configured(),
                "note": "阿里云录音文件识别：REST 提交+轮询，支持说话人分离（diarization）",
                "env": ["ASR_ALIYUN_ACCESS_KEY_ID", "ASR_ALIYUN_ACCESS_KEY_SECRET", "ASR_ALIYUN_APP_KEY"],
                "limitation": "要求音频可通过 URL 访问（OSS），接入时需配套院内文件上传通道"}
    def transcribe(self, audio_path, speakers=True, **opts):
        if not self.configured():
            raise RuntimeError("阿里云引擎未配置：请设置 ASR_ALIYUN_ACCESS_KEY_ID/SECRET/APP_KEY")
        try:
            import requests
        except ImportError:
            raise RuntimeError("缺少依赖：pip install requests")
        raise RuntimeError("阿里云适配器骨架：Token 获取与任务提交需在拿到密钥后按官方 SDK 联调实现（本文件 status.note 已列要点）")

class SenseTimeEngine(BaseEngine):
    """商汤语音识别（占位适配器）：对外公开的文件转写接口资料待商汤侧确认，接口位已预留。"""
    name = "sensetime"
    def configured(self): return bool(os.environ.get("ASR_SENSE_API_BASE") and os.environ.get("ASR_SENSE_API_KEY"))
    def status(self):
        return {"ready": False, "note": "占位适配器：待商汤接口资料确认后按 BaseEngine.transcribe 契约实现",
                "env": ["ASR_SENSE_API_BASE", "ASR_SENSE_API_KEY"]}
    def transcribe(self, audio_path, speakers=True, **opts):
        raise RuntimeError("商汤适配器未实现：接口资料确认后填充（契约见 BaseEngine）")

class LocalWhisperEngine(BaseEngine):
    """本地 faster-whisper（私有化研发基线：数据不出机，作为云端引擎的对照与兜底）。
    档位与环境变量：ASR_LOCAL_MODEL（small/medium/large-v3-turbo/模型路径）、
    ASR_LOCAL_DEVICE（cpu/cuda，缺省 cpu）、ASR_LOCAL_COMPUTE（缺省按设备取 int8/float16）、
    ASR_LOCAL_BEAM（缺省 5=引擎默认；患者F 单例 A/B 实测 beam5 优于 beam1，且旧报告
    "贪心最优"的说法与代码实际（未显式传参=beam5）不符，以复测为准）。"""
    name = "local-whisper"
    def configured(self): return bool(os.environ.get("ASR_LOCAL_MODEL"))
    def status(self):
        return {"ready": self.configured(),
                "note": f"faster-whisper 本地转写（{os.environ.get('ASR_LOCAL_MODEL') or '未配置模型'}/"
                        f"{os.environ.get('ASR_LOCAL_DEVICE', 'cpu')}）：无外呼、适合研发期与私有化对照；"
                        f"波束缺省 5（ASR_LOCAL_BEAM 可调）；不带医生/患者角色分离（后处理区分）",
                "env": ["ASR_LOCAL_MODEL", "ASR_LOCAL_DEVICE", "ASR_LOCAL_COMPUTE", "ASR_LOCAL_BEAM"],
                "deps": "pip install faster-whisper"}
    @staticmethod
    def _wav_to_float32(path):
        """16kHz 单声道 PCM WAV 直读为 float32（标准库 wave，免 PyAV/ffmpeg）。
        非 16k/立体声用 audioop 重采样混单；非 WAV 或非 PCM 交给 PyAV 兜底。"""
        import wave, audioop
        try:
            with wave.open(path, "rb") as w:
                if w.getcomptype() != "NONE":
                    raise wave.Error("compressed wav")
                rate, ch, sw = w.getframerate(), w.getnchannels(), w.getsampwidth()
                raw = w.readframes(w.getnframes())
        except (wave.Error, EOFError):
            return None, None
        if sw != 2:
            return None, None
        if ch != 1:
            raw = audioop.tomono(raw, 2, 0.5, 0.5)
        if rate != 16000:
            raw, _ = audioop.ratecv(raw, 2, 1, rate, 16000, None)
        import numpy as np
        return np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0, 16000

    def transcribe(self, audio_path, speakers=True, **opts):
        if not self.configured():
            raise RuntimeError("本地引擎未配置：请设置 ASR_LOCAL_MODEL（如 base / medium / 模型路径）")
        try:
            from faster_whisper import WhisperModel
        except ImportError:
            raise RuntimeError("缺少依赖：pip install faster-whisper")
        model_name = os.environ["ASR_LOCAL_MODEL"]
        device = os.environ.get("ASR_LOCAL_DEVICE", "cpu")
        compute = os.environ.get("ASR_LOCAL_COMPUTE", "int8" if device == "cpu" else "float16")
        try:
            model = WhisperModel(model_name, device=device, compute_type=compute)
        except Exception:
            # 模型已缓存但 HF 网络抖动（实测 MaxRetryError）→ 离线加载重试；
            # 国内网络可另设 HF_ENDPOINT=https://hf-mirror.com 走镜像
            model = WhisperModel(model_name, device=device, compute_type=compute,
                                 local_files_only=True)
        # WAV 直读优先（解除 PyAV metadata_errors 不兼容与 ffmpeg 依赖）；其他格式回退 PyAV
        pcm, rate = self._wav_to_float32(audio_path)
        kw = dict(language="zh", vad_filter=True,
                  initial_prompt=_medical_prompt(),
                  beam_size=int(os.environ.get("ASR_LOCAL_BEAM", "5")),
                  # 真实方言录音实测（患者F）：条件于上文会诱发重复循环（同句×7），关闭后消除
                  condition_on_previous_text=False)
        src = pcm if pcm is not None else audio_path
        seg_iter, info = model.transcribe(src, **kw)
        segs = [{"start": round(s.start, 2), "end": round(s.end, 2), "speaker": "未分离" if speakers else "-", "text": s.text.strip()}
                for s in seg_iter]
        text = "".join(s["text"] for s in segs)
        return Transcript("local-whisper", audio_path, text, segs,
                          warnings=["本地模型对山东方言准确率待评测；角色分离需后处理"] + quality_warnings(text))

# ---------------- 本地 FunASR（自建/私有化：达摩院开源，方言与医疗热词增强） ----------------
_FUNASR_DEFAULT_MODEL = "iic/SenseVoiceSmall"
_FUNASR_MODELS = {}  # (model_id|"fsmn-vad", device) -> AutoModel：长驻进程内复用，避免每请求重载

def _funasr_hotwords():
    """热词串：ASR_FUNASR_HOTWORD 显式指定优先；否则取医疗热词表正确词。
    解码期上下文偏置与 correct_hotwords 事后替换互补——偏置修"听不见音"（声学层），
    替换修"听得见但写错字"（文本层）；仅 SeACo/contextual 系模型支持 hotword。"""
    explicit = os.environ.get("ASR_FUNASR_HOTWORD")
    if explicit is not None:
        return explicit.strip()
    return " ".join(sorted(set(_load_hotwords().values())))

@contextlib.contextmanager
def _quiet_stdout():
    """funasr 在 import 与推理时向 stdout 打版本号/进度（实测 "funasr version: 1.4.16."
    直接污染 MCP stdio 协议流，客户端会把它当 JSON 响应读），统一重定向到 stderr。"""
    with contextlib.redirect_stdout(sys.stderr):
        yield

class FunASREngine(BaseEngine):
    """本地 FunASR（阿里达摩院开源，数据不出机）——自建 ASR 主力：

    - 默认 iic/SenseVoiceSmall：非自回归、CPU 可跑，中文口音鲁棒性优于 whisper-small；
    - 可切 SeACo-Paraformer（ASR_FUNASR_MODEL 指向含 seaco/contextual 的模型）：
      医疗热词在解码期做上下文偏置，直接提升"消炎针/阑尾炎"类术语的听音正确率；
    - 分段策略（患者F 首例实测教训）：VAD 会把无长停顿的长对话并成 50s+ 一段，
      而本族模型在 >15~20s 长段上显著退化（SeACo 丢内容、SenseVoice 压缩掉字），
      故对超长段按能量最低点二次切分（ASR_FUNASR_CHUNK_S，缺省 SenseVoice 15s /
      Paraformer 系 10s），逐段转写并保留 VAD 时间戳进统一 Transcript；
    - Paraformer 系默认带标点（ct-punc，ASR_FUNASR_PUNC=0 可关）。
    模型经 ModelScope 缓存后离线可用；依赖：pip install funasr torch torchaudio。"""
    name = "funasr"
    def configured(self):
        try:
            return importlib.util.find_spec("funasr") is not None
        except (ImportError, ValueError):
            return False
    def status(self):
        model_id = os.environ.get("ASR_FUNASR_MODEL") or _FUNASR_DEFAULT_MODEL
        return {"ready": self.configured(),
                "note": f"本地 FunASR（{model_id}，{os.environ.get('ASR_FUNASR_DEVICE', 'cpu')}）："
                        "无外呼；VAD 分段+超长段能量切分；SeACo 系模型支持医疗热词解码期偏置",
                "env": ["ASR_FUNASR_MODEL", "ASR_FUNASR_DEVICE", "ASR_FUNASR_HOTWORD",
                        "ASR_FUNASR_CHUNK_S", "ASR_FUNASR_PUNC", "ASR_FUNASR_VAD"],
                "deps": "pip install funasr torch torchaudio"}
    @staticmethod
    def _clean(text):
        """清除富文本标签与表情符号（SenseVoice 情绪/事件标记非医疗内容），
        并清 CJK 字符间空格（FunASR 部分模型逐字间插空格；不动中英/数字边界）。"""
        t = re.sub(r"<\|[^|]*\|>", "", text)
        t = re.sub(r"[\U0001F000-\U0001FAFF\u2600-\u27BF\uFE0F\u200D]", "", t)
        return re.sub(r"(?<=[\u4e00-\u9fff])\s+(?=[\u4e00-\u9fff])", "", t).strip()
    def _build(self, model_id, device, vad=False):
        key = ("fsmn-vad", device) if vad else (model_id, device)
        if key not in _FUNASR_MODELS:
            with _quiet_stdout():  # funasr import 会往 stdout 打版本号，必须挡在协议流之外
                from funasr import AutoModel
                if vad:
                    _FUNASR_MODELS[key] = AutoModel(model="fsmn-vad", device=device, disable_update=True)
                else:
                    kw = {"model": model_id, "device": device, "disable_update": True}
                    if "sensevoice" not in model_id.lower() and os.environ.get("ASR_FUNASR_PUNC", "1") != "0":
                        kw["punc_model"] = "ct-punc"
                    _FUNASR_MODELS[key] = AutoModel(**kw)
        return _FUNASR_MODELS[key]
    @staticmethod
    def _split_long(pcm, seg, limit_ms):
        """超长段按能量最低点切分：在均匀切点 ±1.2s 内找 100ms 窗 RMS 最低处，
        避免在词中间硬切（VAD 无静音点时的兜底；患者F 实测 VAD 会把 55.9s 并成一段）。"""
        import numpy as np
        s, e = seg
        if e - s <= limit_ms:
            return [(s, e)]
        k = (e - s + limit_ms - 1) // limit_ms
        cuts = []
        for i in range(1, k):
            tgt = s + (e - s) * i // k
            lo, hi = max(s + 300, tgt - 1200), min(e - 300, tgt + 1200)
            best, best_r = tgt, None
            for c in range(lo, hi, 100):
                a, b = c * 16, (c + 100) * 16
                sub = pcm[a:b] if b <= len(pcm) else pcm[a:]
                r = float(np.sqrt((sub.astype("float64") ** 2).mean())) if len(sub) else 0.0
                if best_r is None or r < best_r:
                    best_r, best = r, c + 50
            cuts.append(best)
        pts = [s] + sorted(cuts) + [e]
        return [(pts[i], pts[i + 1]) for i in range(len(pts) - 1)]
    def transcribe(self, audio_path, speakers=True, **opts):
        if not self.configured():
            raise RuntimeError("缺少依赖：pip install funasr torch torchaudio")
        model_id = os.environ.get("ASR_FUNASR_MODEL") or _FUNASR_DEFAULT_MODEL
        device = os.environ.get("ASR_FUNASR_DEVICE", "cpu")
        lower = model_id.lower()
        is_sv = "sensevoice" in lower
        contextual = "seaco" in lower or "contextual" in lower
        wav = prepare_audio(audio_path)
        pcm, _ = LocalWhisperEngine._wav_to_float32(wav)
        if pcm is None:
            raise RuntimeError("FunASR 需要 16kHz 单声道 PCM WAV（prepare_audio 后仍不可读）")
        warns = []
        segs_ms = []
        if os.environ.get("ASR_FUNASR_VAD", "1") != "0":
            try:
                with _quiet_stdout():
                    rv = self._build(model_id, device, vad=True).generate(input=wav)
                segs_ms = (rv[0].get("value") or []) if rv else []
            except Exception as e:
                warns.append(f"VAD 切分失败，整段兜底：{str(e)[:80]}")
        if not segs_ms:
            segs_ms = [[0, len(pcm) // 16]]
        limit = int(float(os.environ.get("ASR_FUNASR_CHUNK_S", "15" if is_sv else "10")) * 1000)
        # 分段：超长段能量切分 + 相邻短段合并（合计 ≤limit 且间隔 ≤800ms）——减少调用次数
        chunks = []
        for s, e in [c for seg in segs_ms for c in self._split_long(pcm, seg, limit)]:
            if chunks and (e - chunks[-1][0]) <= limit and (s - chunks[-1][1]) <= 800:
                chunks[-1] = (chunks[-1][0], e)
            else:
                chunks.append((s, e))
        chunks = [(s, e) for s, e in chunks if e - s >= 100]  # <0.1s 碎片丢弃
        if not chunks:
            raise RuntimeError("FunASR 转写结果为空（音频过短或无有效语音）")
        audio_in = [pcm[int(s * 16):int(e * 16)] for s, e in chunks]
        hot = _funasr_hotwords() if contextual else ""
        if contextual and hot:
            warns.append(f"解码期热词偏置已启用（{len(hot.split())} 词）")
        model = self._build(model_id, device)

        def _cfg(inp, n):
            gen = {"input": inp, "batch_size_s": 60}
            if n > 1:
                gen["batch_size"] = n
            if is_sv:
                gen.update(language="zh", use_itn=True)
            elif hot:
                gen["hotword"] = hot
            return gen

        try:
            with _quiet_stdout():
                results = model.generate(**_cfg(audio_in, len(audio_in)))
        except Exception as e:
            # 批量失败（单段异常会拖垮整批）→ 逐段降级重试，保住可用性
            warns.append(f"批量转写失败，逐段降级：{str(e)[:80]}")
            results = []
            for one in audio_in:
                with _quiet_stdout():
                    results.append(model.generate(**_cfg(one, 1))[0])
        out = []
        for (s_ms, e_ms), r in zip(chunks, results):
            t = self._clean(str((r or {}).get("text") or ""))
            if not t:
                continue
            out.append({"start": round(s_ms / 1000.0, 2), "end": round(e_ms / 1000.0, 2),
                        "speaker": "未分离" if speakers else "-", "text": t})
        if not out:
            raise RuntimeError("FunASR 转写结果为空（音频过短或无有效语音）")
        warns.append("角色分离由后处理标注（本地引擎不含医生/患者分离）")
        text = "".join(s["text"] for s in out)
        return Transcript("funasr", audio_path, text, out, warnings=warns + quality_warnings(text))

ENGINES = {e.name: e for e in (MockEngine(), XfyunEngine(), AliyunEngine(), SenseTimeEngine(),
                               LocalWhisperEngine(), FunASREngine())}

def pick_engine(name: Optional[str]) -> BaseEngine:
    name = name or os.environ.get("ASR_DEFAULT_ENGINE") or "mock"
    if name not in ENGINES:
        raise ValueError(f"未知引擎 {name}，可用：{list(ENGINES)}")
    return ENGINES[name]

# ---------------- 转写后处理管线（引擎无关，新引擎到即享） ----------------
def postprocess(transcript, correct=True, llm_polish=False):
    """统一后处理：①热词同音修正（默认开，留痕）②可选 LLM 方言纠错级联（双版本留痕）。
    所有引擎（含云端）的转写结果都应过此管线再进抽取/生成——修正清单与原文随 Transcript
    下发，四色确认页可溯源到转写原话（绿字段红线：修正不改写语义）。"""
    if correct:
        transcript, applied = correct_hotwords(transcript)
        transcript.hotwords_applied = applied  # 热词命中清单（随 to_dict 下发，供 harvest 沉淀）
    if llm_polish and os.environ.get("LLM_API_BASE") and transcript.text:
        try:
            transcript = dialect_polish(transcript)
        except Exception as e:
            transcript.warnings.append(f"LLM 方言纠错降级（保留原转写）: {e}")
    return transcript

def dialect_polish(transcript):
    """LLM 方言同音纠错级联（患者F 评测结论 #4 的落地）：只修同音讹变不改语义，
    原文以 text_raw 保留在 Transcript 上（双版本留痕，溯源红线）。
    实测调优（患者F 第一轮：仅"只纠错"指令时模型几乎原样复读，0 处有效修正）：
    ①明确给出纠错示例对（做俩→昨天、观点有→没见好、有关→过敏、走些→做）；
    ②要求输出修正清单，便于人工核对与留痕；③声明"听不清处保留原字，禁止臆改"。"""
    import urllib.request
    prompt = (
        "你是山东西区方言医患对话的转写校对员。下面这份 ASR 转写稿含方言同音讹变，"
        "请逐处纠正为正确词，输出 JSON：{\"text\":\"纠错后全文\",\"corrections\":"
        "[{\"before\":\"原文片段\",\"after\":\"纠正后\"}]}。\n"
        "已知方言/同音讹变对照（供参考，不限于此）：做俩=昨天；观点有=没见好；"
        "把人厉害=疼得厉害；聊点多=七点多；通过=痛过（病史语境）；有关=过敏（药物语境）；"
        "走些手术/走手术=做手术；给它还疼=这儿还疼；瞪瞪吧=等等吧；手势力=化验；"
        "行李=难受/不舒服；条子=身体；小指=小针。\n"
        "规则：只纠同音讹变与口语粘连，不增删医学信息、不改数字；听不清处保留原字；"
        "修正逐处列入 corrections（原文片段须与稿中完全一致）。\n\n" + transcript.text[:5000])
    body = json.dumps({"model": os.environ.get("LLM_MODEL", "Deepseek-v4-flash"),
                       "messages": [{"role": "user", "content": prompt}],
                       "max_tokens": 4000, "temperature": 0.1}).encode()
    req = urllib.request.Request(os.environ["LLM_API_BASE"].rstrip("/") + "/chat/completions",
                                 data=body, headers={"Content-Type": "application/json",
                                                     "Authorization": "Bearer " + os.environ["LLM_API_KEY"]})
    with urllib.request.urlopen(req, timeout=60) as r:
        out = json.loads(r.read().decode())["choices"][0]["message"]["content"].strip()
    out = re.sub(r"^```(json)?|```$", "", out.strip(), flags=re.M).strip()
    try:
        data = json.loads(out)
        polished = str(data.get("text") or "").strip()
        corrections = data.get("corrections") or []
    except (ValueError, AttributeError):
        polished, corrections = out, []
    if not polished or len(polished) < len(transcript.text) * 0.5:
        transcript.warnings.append("LLM 纠错输出异常（长度骤减或为空），已放弃")
        return transcript
    transcript.text_raw = transcript.text                 # 双版本：原文保留可溯源
    transcript.text = polished
    transcript.corrections = corrections
    transcript.warnings.append(
        f"LLM 方言纠错已应用（{len(corrections)} 处；原文以 text_raw 保留，修正清单见 corrections）")
    return transcript

# ---------------- MCP stdio 协议（换行分隔 JSON-RPC 2.0） ----------------
SERVER_INFO = {"name": "donge-asr-mcp", "version": "0.1.0"}
TOOLS = [
    {"name": "asr_engines", "description": "列出可用语音识别引擎及就绪状态（密钥配置情况）",
     "inputSchema": {"type": "object", "properties": {}, "required": []}},
    {"name": "asr_prepare_audio", "description": "音频预处理：转 16kHz 单声道 WAV（各引擎通用前置）",
     "inputSchema": {"type": "object", "properties": {
         "audio_path": {"type": "string"}, "out_dir": {"type": "string"}}, "required": ["audio_path"]}},
    {"name": "asr_transcribe", "description": "语音转写：返回统一 Transcript（全文 + text_raw 原文 + 分段 + 说话人）。engine 可选 " + "/".join(ENGINES) + "，缺省取 ASR_DEFAULT_ENGINE。correct=热词修正（默认开）；llm_polish=LLM 方言纠错级联（默认关，原文以 text_raw 留痕）",
     "inputSchema": {"type": "object", "properties": {
         "audio_path": {"type": "string", "description": "音频文件路径（m4a/wav/mp3 等，自动预处理）"},
         "engine": {"type": "string"}, "speakers": {"type": "boolean", "default": True},
         "correct": {"type": "boolean", "default": True},
         "llm_polish": {"type": "boolean", "default": False}},
         "required": ["audio_path"]}},
    {"name": "asr_compare", "description": "多引擎同题对比评测：同一音频跑多个引擎，输出各引擎转写全文与段数（引擎选型/验收用；需各引擎密钥就绪）",
     "inputSchema": {"type": "object", "properties": {
         "audio_path": {"type": "string"},
         "engines": {"type": "array", "items": {"type": "string"},
                     "description": "对比引擎清单，缺省全部已配置引擎"},
         "llm_polish": {"type": "boolean", "default": False}},
         "required": ["audio_path"]}},
]

def handle(req):
    method = req.get("method")
    rid = req.get("id")
    if method == "initialize":
        return {"jsonrpc": "2.0", "id": rid, "result": {
            "protocolVersion": req.get("params", {}).get("protocolVersion", "2024-11-05"),
            "capabilities": {"tools": {}}, "serverInfo": SERVER_INFO}}
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": rid, "result": {"tools": TOOLS}}
    if method == "tools/call":
        params = req.get("params", {})
        name = params.get("name"); args = params.get("arguments", {}) or {}
        try:
            if name == "asr_engines":
                data = {n: e.status() for n, e in ENGINES.items()}
                data["_default"] = os.environ.get("ASR_DEFAULT_ENGINE", "mock")
            elif name == "asr_prepare_audio":
                data = {"wav_16k": prepare_audio(args["audio_path"], args.get("out_dir", ""))}
            elif name == "asr_transcribe":
                eng = pick_engine(args.get("engine"))
                t = eng.transcribe(args["audio_path"], speakers=bool(args.get("speakers", True)))
                # 统一后处理管线：热词修正（默认）+ 可选 LLM 方言纠错（llm_polish=1 时）
                t = postprocess(t, correct=bool(args.get("correct", True)),
                                llm_polish=bool(args.get("llm_polish", False)))
                data = t.to_dict()
            elif name == "asr_compare":
                want = args.get("engines") or [n for n, e in ENGINES.items()
                                               if n != "mock" and e.configured()]
                out = {}
                for n in want:
                    if n not in ENGINES:
                        out[n] = {"error": f"未知引擎 {n}"}
                        continue
                    try:
                        t = ENGINES[n].transcribe(args["audio_path"], speakers=True)
                        t = postprocess(t, correct=True, llm_polish=bool(args.get("llm_polish", False)))
                        d = t.to_dict()
                        out[n] = {"text": d["text"][:800], "n_segments": len(d["segments"]),
                                  "duration_sec": d["duration_sec"], "warnings": d["warnings"][:3]}
                    except Exception as e:
                        out[n] = {"error": str(e)[:200]}
                data = out
            else:
                raise ValueError(f"未知工具 {name}")
            return {"jsonrpc": "2.0", "id": rid, "result": {
                "content": [{"type": "text", "text": json.dumps(data, ensure_ascii=False, indent=1)}]}}
        except Exception as e:
            return {"jsonrpc": "2.0", "id": rid, "result": {
                "content": [{"type": "text", "text": f"ERROR: {e}"}], "isError": True}}
    if method == "ping":
        return {"jsonrpc": "2.0", "id": rid, "result": {}}
    if method and method.startswith("notifications/"):
        return None
    if rid is not None:
        return {"jsonrpc": "2.0", "id": rid, "error": {"code": -32601, "message": f"method 未实现: {method}"}}
    return None

def main():
    for line in sys.stdin:
        line = line.strip()
        if not line: continue
        try:
            req = json.loads(line)
        except json.JSONDecodeError:
            continue
        resp = handle(req)
        if resp is not None:
            sys.stdout.write(json.dumps(resp, ensure_ascii=False) + "\n")
            sys.stdout.flush()

if __name__ == "__main__":
    main()
