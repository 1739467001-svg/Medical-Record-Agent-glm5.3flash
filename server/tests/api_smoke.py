# -*- coding: utf-8 -*-
"""API 冒烟测试（HTTP 层端到端，unittest 之外的独立脚本）。
====================================================================
与单元测试的分工：单测覆盖业务逻辑（do_* 函数直调），本脚本覆盖 **HTTP 层**——
真实起服务进程，验证路由分发、/api/ 前缀剥离、Cookie 会话、限流、LLM 未配置
降级（503）、静态托管与角色权限的完整链路。

用法（零依赖，标准库）：
    python3 server/tests/api_smoke.py            # 随机端口起服务 → 全断言 → 退出码 0/1

隔离：MRA_SQLITE_PATH 指向临时库，不触碰研发 local_dev.db；
LLM 环境变量显式清除——顺带验证"LLM 未配置"的降级路径。
"""
import json, os, socket, subprocess, sys, tempfile, time, urllib.request, http.cookiejar

SERVER_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REPO_DIR = os.path.dirname(SERVER_DIR)

RESULTS = []
def check(name, ok, detail=""):
    RESULTS.append((name, ok, detail))
    print(("  ✓ " if ok else "  ✗ ") + name + (f" — {detail}" if detail and not ok else ""))

def free_port():
    s = socket.socket(); s.bind(("127.0.0.1", 0)); port = s.getsockname()[1]; s.close()
    return port

def main():
    tmp = tempfile.mkdtemp(prefix="mra_smoke_")
    port = free_port()
    env = dict(os.environ)
    for k in ("LLM_API_BASE", "LLM_API_KEY", "ASR_DEFAULT_ENGINE", "ASR_XFYUN_APP_ID", "ASR_XFYUN_API_KEY"):
        env.pop(k, None)   # 显式无 LLM/真实 ASR：验证降级路径
    env.update({"MRA_LLM_PORT": str(port), "MRA_SQLITE_PATH": os.path.join(tmp, "smoke.db"),
                "MRA_STATIC_DIR": os.path.join(REPO_DIR, "demo"),
                "MRA_DATA_PATH": os.path.join(REPO_DIR, "demo", "data", "patients.json")})
    proc = subprocess.Popen([sys.executable, os.path.join(SERVER_DIR, "llm_api.py")], env=env,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    base = f"http://127.0.0.1:{port}"
    jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))

    def req(method, path, body=None):
        url = path if path.startswith("http") else base + path
        data = json.dumps(body).encode() if body is not None else None
        r = urllib.request.Request(url, data=data, method=method,
                                   headers={"Content-Type": "application/json"} if body is not None else {})
        try:
            with opener.open(r, timeout=30) as resp:
                return resp.status, json.loads(resp.read().decode() or "{}")
        except urllib.error.HTTPError as e:
            try:
                return e.code, json.loads(e.read().decode() or "{}")
            except Exception:
                return e.code, {}

    try:
        # 等服务就绪
        for _ in range(40):
            try:
                code, d = req("GET", base + "/health")
                if code == 200: break
            except Exception: pass
            time.sleep(0.25)
        else:
            raise RuntimeError("服务未在 10s 内就绪")

        print("[1] 基础与静态")
        code, d = req("GET", "/api/health")   # /api/ 前缀剥离
        check("健康检查（/api/ 前缀剥离）", code == 200 and d.get("ok") and d.get("auth") == "sqlite")
        with opener.open(base + "/index.html", timeout=10) as r:
            check("静态托管 index 内容", r.status == 200 and "AI 病历智能体" in r.read().decode())

        print("[2] ASR 状态（无真实引擎）")
        code, d = req("GET", "/asr/status")
        check("asr/status 演示模式", code == 200 and d.get("real") is False and d.get("engine") == "mock")

        print("[3] 认证与角色")
        for uname, role in (("smoke_att", "attending"), ("smoke_res", "resident"), ("smoke_qc", "qc")):
            code, d = req("POST", "/auth/register", {"username": uname, "password": "smoke123",
                                                     "name": "冒烟" + uname[-3:], "role": role})
            check(f"注册 {uname}（{role}）", code == 200 and d.get("user", {}).get("role") == role)
        code, d = req("GET", "/auth/me")
        check("会话恢复 /auth/me（最后注册者）", code == 200 and d.get("user", {}).get("username") == "smoke_qc")

        print("[4] 归档与审签（HTTP 层）")
        code, d = req("POST", "/records/submit", {"code": "DA0001", "admission": 1, "doc": "入院记录",
                      "disease": "急性阑尾炎", "fields": [{"label": "主诉", "value": "右下腹痛1天", "binding": "A01.03"}],
                      "qc": {}, "edits": [{"field": "主诉", "before": "腹痛", "after": "右下腹痛1天",
                                           "time": "10:00:00", "doctor": "冒烟"}], "xml": ""})
        check("住院医师提交归档", code == 200 and d.get("ok") and "AR-" in d.get("record_no", ""))
        rid = d.get("id")
        # 切到质控 cookie 演示越权：先 logout 再用另一个账号？opener 共享 jar——直接注册新会话覆盖
        # 质控登录（覆盖会话）
        req("POST", "/auth/logout")
        req("POST", "/auth/login", {"username": "smoke_qc", "password": "smoke123"})
        code, d = req("POST", "/records/review", {"id": rid, "action": "sign"})
        check("质控越权审签 → 403", code == 403)
        code, d = req("POST", "/records/spotcheck", {"id": rid, "result": "ok"})
        check("质控抽查合格", code == 200 and d.get("spot_result") == "ok")
        req("POST", "/auth/logout")
        req("POST", "/auth/login", {"username": "smoke_att", "password": "smoke123"})
        code, d = req("POST", "/records/review", {"id": rid, "action": "sign", "comment": "同意"})
        check("上级医师签发", code == 200 and d.get("status") == "signed")
        code, d = req("POST", "/records/review", {"id": rid, "action": "reject", "comment": "再试"})
        check("重复审签 → 409", code == 409)

        print("[5] 列表/详情/修改回流")
        code, d = req("GET", "/records")
        check("上级医师列表含已签发", code == 200 and any(r["id"] == rid and r["status"] == "signed" for r in d.get("records", [])))
        code, d = req("GET", f"/records/detail?id={rid}")
        check("详情含抽查结果", code == 200 and d["record"].get("spot_result") == "ok")
        code, d = req("GET", "/records/edits")
        hot = {x["label"]: x["color"] for x in d.get("hot", [])}
        check("修改回流聚合（主诉=绿）", code == 200 and hot.get("主诉") == "green")

        print("[6] 审计留痕（P2-K）")
        req("POST", "/auth/logout")
        req("POST", "/auth/login", {"username": "smoke_res", "password": "smoke123"})
        code, d = req("GET", "/records/audit")
        check("住院医师查审计 → 403", code == 403)
        req("POST", "/auth/logout")
        req("POST", "/auth/login", {"username": "smoke_qc", "password": "smoke123"})
        code, d = req("GET", "/records/audit?limit=200")
        acts = {r["action"] for r in d.get("records", [])}
        check("质控查审计含注册/登录/归档/审签/抽查", code == 200 and
              {"register", "login", "archive_submit", "review_sign", "spotcheck"} <= acts)
        code, d = req("GET", f"/records/detail?id={rid}")
        check("详情附操作留痕时间线", code == 200 and len(d["record"].get("audit") or []) >= 2)

        print("[7] 未配置 LLM 的降级与未登录拦截")
        code, d = req("POST", "/generate-summary", {"code": "DA0001", "admission": 1})
        check("generate-summary → 503（LLM 未配置）", code == 503)
        code, d = req("POST", "/generate-draft", {"code": "DA0001", "admission": 1, "doc": "入院记录"})
        check("generate-draft → 503（LLM 未配置）", code == 503)
        jar.clear()
        code, d = req("GET", "/records")
        check("未登录访问 records → 401", code == 401)

    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except Exception:
            proc.kill()

    fails = [r for r in RESULTS if not r[1]]
    print(f"\n冒烟结果：{len(RESULTS) - len(fails)}/{len(RESULTS)} 通过" + ("" if not fails else f"，失败 {len(fails)} 项"))
    sys.exit(1 if fails else 0)

if __name__ == "__main__":
    main()
