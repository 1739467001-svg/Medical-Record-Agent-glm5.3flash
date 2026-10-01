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
import os, sys, io, json, base64, hashlib, hmac, struct, time, urllib.parse
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
                f.seek(size + (size & 1))

# ---------------- 引擎适配层 ----------------
class Transcript:
    def __init__(self, engine, audio, text, segments, mock=False, warnings=None):
        self.engine, self.audio, self.text = engine, audio, text
        self.segments = segments
        self.mock, self.warnings = mock, warnings or []
    def to_dict(self):
        return {"engine": self.engine, "audio": self.audio,
                "duration_sec": wav_duration_sec(self.audio) if self.audio.lower().endswith(".wav") else None,
                "text": self.text, "language": "zh",
                "segments": self.segments, "mock": self.mock, "warnings": self.warnings}

class BaseEngine:
    name = "base"
    def configured(self): raise NotImplementedError
    def status(self): raise NotImplementedError
    def transcribe(self, audio_path, speakers=True, **opts) -> Transcript:
        raise NotImplementedError

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
    """讯飞 实时语音转写 RTASR（流式 WebSocket，适合长录音）。密钥就绪后即可接入。
    签名与分片参数以讯飞官方文档为准，接入联调时核对（HTTPS 控制台申请 appid/api_key）。"""
    name = "xfyun"
    def configured(self): return bool(os.environ.get("ASR_XFYUN_APP_ID") and os.environ.get("ASR_XFYUN_API_KEY"))
    def status(self):
        return {"ready": self.configured(),
                "note": "讯飞实时语音转写 RTASR：WebSocket 流式、长录音、支持热词；需 websocket-client",
                "env": ["ASR_XFYUN_APP_ID", "ASR_XFYUN_API_KEY"]}
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
        wav = prepare_audio(audio_path)
        app_id, api_key = os.environ["ASR_XFYUN_APP_ID"], os.environ["ASR_XFYUN_API_KEY"]
        ts = int(time.time())
        url = f"wss://rtasr.xfyun.cn/v1/ws?appid={app_id}&ts={ts}&signa={self._signa(app_id, api_key, ts)}"
        # TODO(接入联调时核对)：①签名算法 ②结果消息的 action/code 约定 ③热词参数——以讯飞官方文档为准
        frames = open(wav, "rb").read()
        piece = 1280  # 40ms @16kHz 16bit 单声道
        order = {}  # seg_id -> text（RTASR 按 seg_id 分段返回）
        # 超时兜底：假签名/网络异常时讯飞可能长时间不应答，连接与读超时各 15s（联调实测）
        ws = websocket.create_connection(url, timeout=15)
        try:
            for i in range(0, len(frames), piece):
                ws.send(frames[i:i + piece])
                time.sleep(0.04)
            ws.send(b'{"end": true}')
            while True:
                raw = ws.recv()
                if not raw: break
                msg = json.loads(raw)
                if msg.get("action") == "error" or (msg.get("code") not in (None, 0, "0")):
                    raise RuntimeError(f"讯飞返回错误: {msg}")
                if msg.get("action") == "result":
                    data = json.loads(msg.get("data") or "{}")
                    seg_id = data.get("seg_id")
                    text = "".join(w["cw"][0]["w"] for rl in data.get("rl", []) for w in rl.get("ws", []))
                    if seg_id is not None and text:
                        order[int(seg_id)] = text
                    if data.get("type") in ("0", 0):  # 官方约定：最终结果标记，接入时核对
                        break
        finally:
            ws.close()
        texts = [order[k] for k in sorted(order)]
        segs = [{"start": None, "end": None, "speaker": "未分离" if speakers else "-", "text": t} for t in texts]
        return Transcript("xfyun", audio_path, "".join(texts), segs,
                          warnings=["RTASR 单声道不带角色信息：说话人分离由 P1 后处理模块实现（对齐 PRD F2）"])

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
    """本地 faster-whisper（私有化研发基线：数据不出机，作为云端引擎的对照与兜底）。"""
    name = "local-whisper"
    def configured(self): return bool(os.environ.get("ASR_LOCAL_MODEL"))
    def status(self):
        return {"ready": self.configured(),
                "note": "faster-whisper 本地转写：无外呼、适合研发期与私有化对照；不带医生/患者角色分离（后处理区分）",
                "env": ["ASR_LOCAL_MODEL"], "deps": "pip install faster-whisper"}
    def transcribe(self, audio_path, speakers=True, **opts):
        if not self.configured():
            raise RuntimeError("本地引擎未配置：请设置 ASR_LOCAL_MODEL（如 base / medium / 模型路径）")
        try:
            from faster_whisper import WhisperModel
        except ImportError:
            raise RuntimeError("缺少依赖：pip install faster-whisper")
        model = WhisperModel(os.environ["ASR_LOCAL_MODEL"], device="cpu", compute_type="int8")
        seg_iter, info = model.transcribe(audio_path, language="zh", vad_filter=True)
        segs = [{"start": round(s.start, 2), "end": round(s.end, 2), "speaker": "未分离" if speakers else "-", "text": s.text.strip()}
                for s in seg_iter]
        return Transcript("local-whisper", audio_path, "".join(s["text"] for s in segs), segs,
                          warnings=["本地模型对山东方言准确率待评测；角色分离需后处理"])

ENGINES = {e.name: e for e in (MockEngine(), XfyunEngine(), AliyunEngine(), SenseTimeEngine(), LocalWhisperEngine())}

def pick_engine(name: Optional[str]) -> BaseEngine:
    name = name or os.environ.get("ASR_DEFAULT_ENGINE") or "mock"
    if name not in ENGINES:
        raise ValueError(f"未知引擎 {name}，可用：{list(ENGINES)}")
    return ENGINES[name]

# ---------------- MCP stdio 协议（换行分隔 JSON-RPC 2.0） ----------------
SERVER_INFO = {"name": "donge-asr-mcp", "version": "0.1.0"}
TOOLS = [
    {"name": "asr_engines", "description": "列出可用语音识别引擎及就绪状态（密钥配置情况）",
     "inputSchema": {"type": "object", "properties": {}, "required": []}},
    {"name": "asr_prepare_audio", "description": "音频预处理：转 16kHz 单声道 WAV（各引擎通用前置）",
     "inputSchema": {"type": "object", "properties": {
         "audio_path": {"type": "string"}, "out_dir": {"type": "string"}}, "required": ["audio_path"]}},
    {"name": "asr_transcribe", "description": "语音转写：返回统一 Transcript（全文 + 分段 + 说话人）。engine 可选 mock/xfyun/aliyun/sensetime/local-whisper，缺省取 ASR_DEFAULT_ENGINE",
     "inputSchema": {"type": "object", "properties": {
         "audio_path": {"type": "string", "description": "音频文件路径（m4a/wav/mp3 等，自动预处理）"},
         "engine": {"type": "string"}, "speakers": {"type": "boolean", "default": True}},
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
                t = pick_engine(args.get("engine")).transcribe(
                    args["audio_path"], speakers=bool(args.get("speakers", True)))
                data = t.to_dict()
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
