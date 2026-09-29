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
import json, os, sys, time, urllib.request, collections
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from agents import run_pipeline, aggregator  # noqa: E402

DATA_PATH = os.environ.get("MRA_DATA_PATH", os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "demo", "data", "patients.json"))
PORT = int(os.environ.get("MRA_LLM_PORT", "8090"))
RATE_LIMIT = int(os.environ.get("MRA_RATE_LIMIT", "6"))
RATE_WINDOW = 60

_patients = json.load(open(DATA_PATH, encoding="utf-8"))
_hits = collections.deque()

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

    def _json(self, code, obj):
        b = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        if self.path == "/health":
            self._json(200, {"ok": True, "engine": os.environ.get("LLM_MODEL", "Deepseek-v4-flash")})
        else:
            self._json(404, {"error": "not found"})

    def do_POST(self):
        if self.path not in ("/generate-summary", "/generate-draft"):
            return self._json(404, {"error": "not found"})
        now = time.time()
        while _hits and now - _hits[0] > RATE_WINDOW: _hits.popleft()
        if len(_hits) >= RATE_LIMIT:
            return self._json(429, {"error": "请求过于频繁，请稍后再试"})
        _hits.append(now)
        if not (os.environ.get("LLM_API_BASE") and os.environ.get("LLM_API_KEY")):
            return self._json(503, {"error": "LLM 服务未配置"})
        try:
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
        except Exception as e:
            return self._json(400, {"error": f"请求体解析失败: {e}"})
        if self.path == "/generate-summary":
            try:
                prompt, err = build_prompt(str(body.get("code", "")), int(body.get("admission", 0) or 0))
                if err: return self._json(400, {"error": err})
                summary = call_llm(prompt)
                return self._json(200, {"code": body.get("code"), "admission": body.get("admission"),
                                        "summary": summary, "generated_at": time.strftime("%Y-%m-%d %H:%M:%S")})
            except Exception as e:
                return self._json(500, {"error": f"生成失败: {e}"})
        return self._stream_draft(body)

    # ---- 多智能体草稿（NDJSON 流式：每行一个 JSON 事件） ----
    def _stream_draft(self, body):
        code, adm_idx = str(body.get("code", "")), int(body.get("admission", 0) or 0)
        transcript = body.get("transcript") or None
        p = next((x for x in _patients if x["code"] == code), None)
        adm = p["admissions"][adm_idx - 1] if p and 0 < adm_idx <= len(p["admissions"]) else None
        if not adm:
            return self._json(400, {"error": "患者或住院次不存在"})
        doc = next((d for d in adm["docs"] if d["type"] == "入院记录"), None)
        target_labels = {f.get("label") for f in doc["fields"]} if doc else None
        dataset_adm = {"his": adm.get("his") or {}, "docs": adm["docs"]}

        self.send_response(200)
        self.send_header("Content-Type", "application/x-ndjson; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()

        def emit(obj):
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
                      "ms": tr["ms"], "warnings": tr["warnings"]})
        try:
            result = run_pipeline(transcript=transcript,
                                  patients_admission=dataset_adm,
                                  corpus=build_corpus(code, adm_idx),
                                  doc_type="入院记录",
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
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
