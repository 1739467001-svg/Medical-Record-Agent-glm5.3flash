# -*- coding: utf-8 -*-
"""演示前预检（院方演示/彩排前 10 分钟跑一次，一条命令）
====================================================================
背景：2026-10-23 公网实测发现 LLM 密钥 401 失效，而 /health 只查环境变量
是否设置、不探测密钥——页面显示"AI 服务在线"，生成却静默降级为纯规则
字段（绿 0）。若演示前没抓住，现场会把降级输出当成功讲。

检查项（默认打公网 http://60.205.204.162/mra/，--local 打本地 8090）：
  [1] 站点可达            首页 200 且标题正确
  [2] 上传入口在位        app.js 含 rec-upload-btn（张老师 10.20 要求的功能）
  [3] 患者F 案例在位      patients.json 含 DA0006（首例真实录音案例）
  [4] LLM 真实可用       最小 transcript 打 generate-draft，流式读事件，
                         出现"失败已降级/HTTP Error 401"即判密钥失效
  [5] 生成链路端到端     同一探针里六智能体事件齐全（extractor→mapper）
                         且有 final 事件（fields+stats.llm_ok）
  [6] 演示材料就绪       --material 指定路径存在（默认彩排录音目录净版转写稿）

用法：
  python3 tools/preflight.py                 # 公网全项
  python3 tools/preflight.py --local         # 本地（需先起服务）
  python3 tools/preflight.py --url <base>    # 自定义 base（如 http://host/mra/）

退出码：任一检查失败为 1（可直接挂到演示前检查清单里）。
零第三方依赖（标准库 urllib）。
"""
import argparse, base64, json, os, sys, urllib.request, urllib.error

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_URL = "http://60.205.204.162/mra/"
MATERIAL = os.path.join(REPO, "病历资料", "彩排录音", "patient01_转写稿_纠错后.txt")
TIMEOUT = 60

ok_all = True


def head(title):
    print(f"\n=== {title} ===")


def report(name, ok, detail=""):
    global ok_all
    ok_all = ok_all and ok
    print(("  ✓ " if ok else "  ✗ ") + name + (f" — {detail}" if detail else ""))


def get(base, path, timeout=20, auth=None):
    if not path.startswith("/"):
        path = "/" + path
    req = urllib.request.Request(base.rstrip("/") + path, method="GET")
    if auth:
        req.add_header("Authorization", "Basic " + base64.b64encode(auth.encode()).decode())
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.status, r.read().decode("utf-8", "replace")


def events_show_llm_failure(events):
    """从 generate-draft 的 NDJSON 事件判定 LLM 是否失效（密钥 401/未配置/超时）。
    依据与 orchestrator.llm_ok_from_trace 同源：extractor 唯一降级路径即 LLM
    调用失败；writer 在 LLM 失败/未配置时告警。retriever 语料为空等正常降级不算。"""
    return any(("失败已降级" in (t.get("summary") or "")) or
               any("LLM" in w for w in (t.get("warnings") or []))
               for t in events)


def probe_generate(base, auth=None):
    """最小 transcript 打生成接口，流式读 NDJSON 事件。
    返回 (events, llm_ok, err_text)。降级/401 都如实反映。"""
    body = json.dumps({
        "code": "DA0001", "admission": 1, "doc": "入院记录",
        "transcript": "医生：哪里不舒服\n患者：测试演示预检。\n医生：多久了\n患者：一天。"}).encode()
    headers = {"Content-Type": "application/json"}
    if auth:
        headers["Authorization"] = "Basic " + base64.b64encode(auth.encode()).decode()
    req = urllib.request.Request(base.rstrip("/") + "/api/generate-draft", data=body,
                                 headers=headers, method="POST")
    events, err_text, llm_ok = [], "", None
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            buf = b""
            while True:
                chunk = r.read(4096)
                if not chunk:
                    break
                buf += chunk
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        ev = json.loads(line)
                    except Exception:
                        continue
                    events.append(ev)
                    if ev.get("event") == "final":
                        llm_ok = (ev.get("stats") or {}).get("llm_ok")
                    if ev.get("event") == "error":
                        err_text = ev.get("error", "")
    except urllib.error.HTTPError as e:
        err_text = f"HTTP {e.code}: {e.read().decode('utf-8', 'replace')[:120]}"
    except Exception as e:
        err_text = f"{type(e).__name__}: {e}"
    return events, llm_ok, err_text


def main():
    ap = argparse.ArgumentParser(description="院方演示前预检")
    ap.add_argument("--local", action="store_true", help="检查本地 http://127.0.0.1:8090/")
    ap.add_argument("--url", default=None, help="自定义 base URL")
    ap.add_argument("--material", default=MATERIAL, help="演示上传材料路径")
    ap.add_argument("--auth", default=os.environ.get("MRA_HTTP_AUTH", "mra:MRA-demo-2026"),
                    help="公网门禁 Basic Auth 凭证 user:pass（公网 2026-10-29 起有门禁；本地不需要）")
    args = ap.parse_args()
    base = args.url or ("http://127.0.0.1:8090/" if args.local else DEFAULT_URL)
    auth = None if args.local else args.auth
    print(f"演示前预检 · 目标 {base}" + ("（带门禁凭证）" if auth else ""))

    head("[1] 站点可达")
    try:
        st, html = get(base, "", auth=auth)
        report("首页 200 且标题正确", st == 200 and "AI 病历智能体" in html, f"status={st}")
    except urllib.error.HTTPError as e:
        if e.code == 401:
            report("首页可达", False, "401 门禁拒绝——公网已加 Basic Auth，请传 --auth user:pass（默认 mra:MRA-demo-2026）")
        else:
            report("首页可达", False, str(e))
    except Exception as e:
        report("首页可达", False, str(e))

    head("[2] 上传入口在位（张老师 10.20 要求）")
    try:
        st, js = get(base, "js/app.js", auth=auth)
        report("app.js 含上传入口", "rec-upload-btn" in js and "handleUpload" in js)
    except Exception as e:
        report("app.js 可取回", False, str(e))

    head("[3] 患者F 真实案例在位")
    try:
        st, pj = get(base, "data/patients.json", auth=auth)
        codes = [p.get("code") for p in json.loads(pj)]
        report("patients.json 含 DA0006", "DA0006" in codes, f"患者 {codes}")
    except Exception as e:
        report("patients.json 可取回", False, str(e))

    head("[4] LLM 真实可用（核心：/health 只查环境变量，不探测密钥）")
    events, llm_ok, err_text = probe_generate(base, auth)
    degraded = events_show_llm_failure(events)
    if err_text and not events:
        report("生成接口可调", False, err_text[:120])
    elif degraded or llm_ok is False:
        detail = "密钥失效/未配置——生成将降级为纯规则字段（绿 0），演示前必须换钥"
        first = next((t.get("summary") for t in events if "失败已降级" in (t.get("summary") or "")), "")
        report("LLM 真实可用", False, detail + (f"（{first[:60]}）" if first else ""))
    elif llm_ok is True:
        report("LLM 真实可用", True, "final 事件 llm_ok=true（真实生成，非降级）")
    else:
        report("LLM 状态可判定", False, "未拿到 final 事件，无法判定")

    head("[5] 生成链路端到端")
    if events:
        names = [t.get("agent") for t in events if t.get("event") == "agent_done"]
        expect = ["extractor", "aggregator", "retriever", "writer", "qc", "mapper"]
        report("六智能体事件齐全", all(n in names for n in expect), f"{names}")
        report("final 事件（fields+stats）", any(e.get("event") == "final" for e in events))
    else:
        report("生成事件", False, "无事件（接口不可达或超时）")

    head("[6] 演示材料就绪")
    report("净版转写稿存在", os.path.exists(args.material), args.material)

    print("\n" + ("✅ 全部通过，可以演示" if ok_all else "❌ 存在红项——按上面提示处理后再演示"))
    return 0 if ok_all else 1


if __name__ == "__main__":
    sys.exit(main())
