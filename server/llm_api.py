# -*- coding: utf-8 -*-
"""
AI 病历智能体 · 服务端 LLM API（Demo 用）
=========================================
职责：为前端"AI 病例总结"提供实时生成服务（PRD F9 患者画像/病例总结）。
安全设计：
  - 密钥只存在服务器环境变量（.llm_env，chmod 600，不入 Git）
  - 仅接受 {code, admission} 白名单入参，提示词固定在服务端，不接受自由文本（防注入/防滥用）
  - 数据源为 demo/data/patients.json（已脱敏），满足研发态云端调用合规（PRD Q10）
  - 进程内限流（默认 6 次/分钟/IP）
运行：python3 llm_api.py（端口 8090，仅 127.0.0.1 经 nginx /mra/api/ 反代暴露）
"""
import json, os, sys, time, urllib.request, collections, base64, tempfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

_SERVER_DIR = os.path.dirname(os.path.abspath(__file__))
_REPO_DIR = os.path.dirname(_SERVER_DIR)
sys.path.insert(0, _SERVER_DIR)
# mcp-server 引擎适配器路径探测：本地仓库相对路径 / 容器挂载路径 / 显式环境变量；
# 全部缺失时 ASR 功能优雅降级（其余功能不受影响）
_MCP_CANDIDATES = [
    os.environ.get("MRA_MCP_PATH", ""),
    os.path.join(_REPO_DIR, "mcp-server"),
    "/mcp-server",
]
for _p in _MCP_CANDIDATES:
    if _p and os.path.isfile(os.path.join(_p, "asr_mcp_server.py")):
        sys.path.insert(0, _p)
        break
from agents import run_pipeline, aggregator  # noqa: E402
import auth  # noqa: E402  登录/注册/会话（server/auth.py）
import records  # noqa: E402  归档与审签流（server/records.py，P2-G）
try:
    import asr_mcp_server  # noqa: E402  可插拔 ASR 引擎适配器（P2-E：mock/xfyun/aliyun/sensetime/local-whisper）
except ImportError:
    asr_mcp_server = None  # 适配器不可用：/asr/* 返回 503，服务其余功能正常

DATA_PATH = os.environ.get("MRA_DATA_PATH", os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "demo", "data", "patients.json"))
PORT = int(os.environ.get("MRA_LLM_PORT", "8090"))
RATE_LIMIT = int(os.environ.get("MRA_RATE_LIMIT", "6"))
RATE_WINDOW = 60
AUTH_RATE_LIMIT = int(os.environ.get("MRA_AUTH_RATE_LIMIT", "20"))
STATIC_DIR = os.environ.get("MRA_STATIC_DIR", "")  # 研发态静态托管；生产由 nginx 提供，勿设

_CONTENT_TYPES = {".html": "text/html; charset=utf-8", ".css": "text/css; charset=utf-8",
                  ".js": "text/javascript; charset=utf-8", ".json": "application/json; charset=utf-8",
                  ".svg": "image/svg+xml", ".png": "image/png", ".ico": "image/x-icon"}

_patients = json.load(open(DATA_PATH, encoding="utf-8"))
_hits = collections.deque()
_auth_hits = collections.deque()
_asr_hits = collections.deque()
ASR_RATE_LIMIT = int(os.environ.get("MRA_ASR_RATE_LIMIT", "3"))
ASR_MAX_AUDIO_BYTES = 30 * 1024 * 1024  # base64 前原始音频上限

# 真实引擎 = 配置了真实密钥/模型的引擎（mock 与占位适配器不算）
_ASR_REAL_ENGINES = ("xfyun", "aliyun", "sensetime", "local-whisper")

def asr_status():
    """ASR 引擎就绪状态（前端据此切换 真实录音/演示回放）。适配器不可用时优雅降级。"""
    if asr_mcp_server is None:
        return {"ok": True, "engine": "none", "real": False, "ready": False,
                "note": "引擎适配器不可用（未挂载 mcp-server）",
                "demo_hint": "录音步骤为演示回放模式"}
    default = os.environ.get("ASR_DEFAULT_ENGINE", "mock")
    eng = asr_mcp_server.ENGINES.get(default)
    real = default in _ASR_REAL_ENGINES and bool(eng and eng.configured())
    return {"ok": True, "engine": default, "real": real,
            "ready": bool(eng and eng.configured()),
            "note": (eng.status().get("note") if eng else "未知引擎"),
            "demo_hint": "未配置真实引擎密钥（ASR_* 环境变量），录音步骤为演示回放模式" if not real else ""}

SYSTEM = "你是医院的病案科医生，负责撰写规范、克制、专业的中文病例总结。只依据给定材料，绝不编造任何症状、诊断或数值；材料不足的地方写\"（材料未提供）\"。"

def build_prompt(code, adm_idx):
    p = next((x for x in _patients if x["code"] == code), None)
    if not p: return None, "未知患者代号"
    adm = p["admissions"][adm_idx - 1] if 0 < adm_idx <= len(p["admissions"]) else None
    if not adm: return None, "住院次不存在"
    docs = "；".join(f"{d['type']}《{d['title']}》" for d in adm["docs"])
    his = adm.get("his") or {}
    labs = "；".join(f"{l.get('item')}={l.get('result')}{l.get('units') or ''}(参考{l.get('ref')})" for l in (his.get("abnormal_labs") or [])[:8]) or "无异常检验记录"
    exams = "；".join(e.get("impression", "")[:80] for e in (his.get("exams") or [])[:5]) or "无检查报告"
    orders = "；".join(o.get("text", "") for o in (his.get("orders") or [])[:10]) or "无医嘱记录"
    vitals = "；".join(f"{v['name']} {v['last']}{v.get('unit','')}" for v in (his.get("vitals") or [])[:6]) or "无体征记录"
    prompt = (
        f"患者代号 {code}，第 {adm_idx} 次住院，病种：{adm.get('disease')}，"
        f"入院 {adm.get('admit_ts')}，出院 {adm.get('discharge_ts')}。\n"
        f"【文书清单】{docs}\n【医嘱（节选）】{orders}\n【异常检验】{labs}\n【检查印象】{exams}\n【最近生命体征】{vitals}\n\n"
        "请输出该次住院的病例总结，固定五个小节，每节 1~3 句：\n"
        "1. 入院主诉与诊断\n2. 主要治疗措施\n3. 异常检验结果汇总\n4. 住院经过要点\n5. 综合分析（含随访建议）\n"
        "直接输出五节内容，不要开场白。")
    return prompt, None

def call_llm(prompt):
    body = json.dumps({"model": os.environ.get("LLM_MODEL", "Deepseek-v4-flash"),
                       "messages": [{"role": "system", "content": SYSTEM}, {"role": "user", "content": prompt}],
                       "max_tokens": 700, "temperature": 0.3}).encode()
    req = urllib.request.Request(os.environ["LLM_API_BASE"].rstrip("/") + "/chat/completions", data=body,
                                 headers={"Content-Type": "application/json",
                                          "Authorization": "Bearer " + os.environ["LLM_API_KEY"]})
    with urllib.request.urlopen(req, timeout=60) as r:
        data = json.loads(r.read().decode())
    return data["choices"][0]["message"]["content"].strip()

class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        sys_err = getattr(__import__("sys"), "stderr")
        print("[llm-api]", fmt % args, file=sys_err)

    def _json(self, code, obj, set_cookies=None):
        b = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(b)))
        for c in (set_cookies or []):
            self.send_header("Set-Cookie", c)
        self.end_headers()
        self.wfile.write(b)

    @property
    def cookies(self):
        if not hasattr(self, "_cookies"):
            self._cookies = {}
            for part in (self.headers.get("Cookie") or "").split(";"):
                if "=" in part:
                    k, v = part.split("=", 1)
                    self._cookies[k.strip()] = v.strip()
        return self._cookies

    def _rate_ok(self, hits, limit):
        now = time.time()
        while hits and now - hits[0] > RATE_WINDOW: hits.popleft()
        if len(hits) >= limit: return False
        hits.append(now)
        return True

    def _read_body(self):
        try:
            return json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
        except Exception as e:
            return {"__error__": str(e)}

    def _static(self, path):
        from urllib.parse import urlsplit, unquote
        name = unquote(urlsplit(path).path) or "/"
        if name == "/": name = "/index.html"
        fp = os.path.realpath(os.path.join(STATIC_DIR, name.lstrip("/")))
        root = os.path.realpath(STATIC_DIR)
        if fp != root and not fp.startswith(root + os.sep):
            return self._json(403, {"error": "forbidden"})
        if not os.path.isfile(fp):
            return self._json(404, {"error": "not found"})
        with open(fp, "rb") as f: data = f.read()
        self.send_response(200)
        self.send_header("Content-Type", _CONTENT_TYPES.get(os.path.splitext(fp)[1].lower(), "application/octet-stream"))
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path.startswith("/api/"):
            path = path[4:]  # 开发静态托管对齐生产 nginx：/mra/api/* → /*
        if path == "/health":
            return self._json(200, {"ok": True, "engine": os.environ.get("LLM_MODEL", "Deepseek-v4-flash"),
                                    "auth": auth.BACKEND})
        if path == "/asr/status":
            return self._json(200, asr_status())
        if path.startswith("/auth/"):
            return auth.handle(self, {}, path)
        if path == "/records" or path.startswith("/records/"):
            return records.handle(self, {}, path)
        if STATIC_DIR:
            return self._static(path)
        self._json(404, {"error": "not found"})

    def do_POST(self):
        path = self.path.split("?", 1)[0]
        if path.startswith("/api/"):
            path = path[4:]  # 开发静态托管对齐生产 nginx：/mra/api/* → /*
        if path.startswith("/auth/"):
            if not self._rate_ok(_auth_hits, AUTH_RATE_LIMIT):
                return self._json(429, {"error": "请求过于频繁，请稍后再试"})
            body = self._read_body()
            if "__error__" in body:
                return self._json(400, {"error": f"请求体解析失败: {body['__error__']}"})
            return auth.handle(self, body, path)
        if path.startswith("/records/"):
            if not self._rate_ok(_auth_hits, AUTH_RATE_LIMIT):
                return self._json(429, {"error": "请求过于频繁，请稍后再试"})
            body = self._read_body()
            if "__error__" in body:
                return self._json(400, {"error": f"请求体解析失败: {body['__error__']}"})
            return records.handle(self, body, path)
        if path == "/asr/transcribe":
            return self._asr_transcribe()
        if path not in ("/generate-summary", "/generate-draft"):
            return self._json(404, {"error": "not found"})
        if not self._rate_ok(_hits, RATE_LIMIT):
            return self._json(429, {"error": "请求过于频繁，请稍后再试"})
        if not (os.environ.get("LLM_API_BASE") and os.environ.get("LLM_API_KEY")):
            return self._json(503, {"error": "LLM 服务未配置"})
        try:
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
        except Exception as e:
            return self._json(400, {"error": f"请求体解析失败: {e}"})
        if path == "/generate-summary":
            try:
                prompt, err = build_prompt(str(body.get("code", "")), int(body.get("admission", 0) or 0))
                if err: return self._json(400, {"error": err})
                summary = call_llm(prompt)
                return self._json(200, {"code": body.get("code"), "admission": body.get("admission"),
                                        "summary": summary, "generated_at": time.strftime("%Y-%m-%d %H:%M:%S")})
            except Exception as e:
                return self._json(500, {"error": f"生成失败: {e}"})
        return self._stream_draft(body)

    # ---- ASR 转写（P2-E 前置：音频上传 → 可插拔引擎 → 统一 Transcript） ----
    def _asr_transcribe(self):
        if not self._rate_ok(_asr_hits, ASR_RATE_LIMIT):
            return self._json(429, {"error": "转写请求过于频繁（每分钟 3 次），请稍后再试"})
        st = asr_status()
        if asr_mcp_server is None:
            return self._json(503, {"error": "ASR 引擎适配器不可用（服务端未挂载 mcp-server）"})
        if not st["real"]:
            return self._json(503, {"error": "真实 ASR 引擎未配置（当前录音页为演示回放模式）。" + (st["demo_hint"] or "")})
        # 合规：真实引擎外呼仅限登录医生账号（演示模式不触发云端转写，PRD Q10）
        u = auth._user_by_token(self.cookies.get(auth.SESSION_COOKIE))
        if not u:
            return self._json(401, {"error": "真实转写需登录医生账号"})
        try:
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
        except Exception as e:
            return self._json(400, {"error": f"请求体解析失败: {e}"})
        audio_b64 = str(body.get("audio_base64") or "")
        if not audio_b64:
            return self._json(400, {"error": "缺少 audio_base64"})
        try:
            raw = base64.b64decode(audio_b64)
        except Exception as e:
            return self._json(400, {"error": f"音频解码失败: {e}"})
        if len(raw) > ASR_MAX_AUDIO_BYTES:
            return self._json(413, {"error": "音频过大（>30MB），请分段录音"})
        filename = os.path.splitext(os.path.basename(str(body.get("filename") or "recording")))[0].replace(" ", "_")[:64] or "recording"
        fd, tmp = tempfile.mkstemp(prefix=f"mra_asr_{filename}_", suffix=".wav")
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(raw)
            engine = asr_mcp_server.pick_engine(None)
            t = engine.transcribe(tmp, speakers=bool(body.get("speakers", True)))
            d = t.to_dict()
            return self._json(200, {"ok": True, "engine": d["engine"], "text": d["text"],
                                    "segments": d["segments"], "duration_sec": d.get("duration_sec"),
                                    "mock": bool(d.get("mock")), "warnings": d.get("warnings") or [],
                                    "transcribed_at": time.strftime("%Y-%m-%d %H:%M:%S")})
        except Exception as e:
            return self._json(502, {"error": f"转写失败：{e}"})
        finally:
            try: os.remove(tmp)
            except OSError: pass

    # ---- 多智能体草稿（NDJSON 流式：每行一个 JSON 事件） ----
    def _stream_draft(self, body):
        code, adm_idx = str(body.get("code", "")), int(body.get("admission", 0) or 0)
        doc_type = str(body.get("doc") or "入院记录")  # 目标文书：入院记录 / 首次病程记录 …
        transcript = body.get("transcript") or None
        p = next((x for x in _patients if x["code"] == code), None)
        adm = p["admissions"][adm_idx - 1] if p and 0 < adm_idx <= len(p["admissions"]) else None
        if not adm:
            return self._json(400, {"error": "患者或住院次不存在"})
        # 文书定位：type 精确匹配 → 病程记录类按标题匹配（如"首次病程记录"）
        doc = next((d for d in adm["docs"] if d["type"] == doc_type), None) \
            or next((d for d in adm["docs"] if d["type"] == "病程记录" and doc_type in d.get("title", "")), None)
        if not doc:
            return self._json(400, {"error": f"该住院次无《{doc_type}》文书"})
        target_labels = {f.get("label") for f in doc["fields"]} if doc else None
        dataset_adm = {"his": adm.get("his") or {}, "docs": adm["docs"]}

        self.send_response(200)
        self.send_header("Content-Type", "application/x-ndjson; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()

        def emit(obj):
            obj["doc"] = doc_type  # 每个事件携带目标文书，前端按文书分发
            try:
                self.wfile.write((json.dumps(obj, ensure_ascii=False) + "\n").encode())
                self.wfile.flush()
            except Exception:
                raise ConnectionError("client closed")

        def on_step(agent, status, tr, partial):
            if status == "run":
                emit({"event": "agent_start", "agent": agent})
            elif status == "done" and tr:
                emit({"event": "agent_done", "agent": agent, "summary": tr["summary"],
                      "ms": tr["ms"], "warnings": tr.get("warnings") or []})
        try:
            result = run_pipeline(transcript=transcript,
                                  patients_admission=dataset_adm,
                                  corpus=build_corpus(code, adm_idx),
                                  doc_type=doc_type,
                                  target_labels=target_labels,
                                  on_step=on_step)
            emit({"event": "final", "fields": result["fields"],
                  "stats": result["stats"], "trace": result["trace"]})
        except ConnectionError:
            pass
        except Exception as e:
            try: emit({"event": "error", "error": str(e)})
            except Exception: pass

def build_corpus(code, adm_idx):
    """知识检索语料：该患者其余住院次 + 另一患者的文书（全部脱敏数据）。"""
    docs = []
    for p in _patients:
        for a in p["admissions"]:
            if p["code"] == code and a is not None:
                docs += a["docs"]
    from agents import retriever
    return retriever.build_corpus_from_fields(docs)

if __name__ == "__main__":
    print(f"[llm-api] listening 0.0.0.0:{PORT}, data={DATA_PATH}")
    print(f"[llm-api] auth backend: {auth.BACKEND}" + (f" -> {auth.MYSQL_CONF['host']}" if auth.BACKEND == "mysql" else f" (dev sqlite: {auth.SQLITE_PATH})"))
    if STATIC_DIR: print(f"[llm-api] static serving (dev): {STATIC_DIR}")
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
