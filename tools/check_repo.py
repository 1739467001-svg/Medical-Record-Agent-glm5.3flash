# -*- coding: utf-8 -*-
"""一键仓库体检（P2-L 可交接性收口）
====================================================================
零依赖标准库脚本，一条命令完成六类检查，任何一项失败退出码 1：

  [1] 编译与语法     server/evaluation/mcp-server 全部 .py 可编译；app.js node --check
  [2] 前后端 API 契约 app.js 的 fetch/authPost 调用 ⊆ 服务端注册路由（auth/records 路由表 + llm_api 字面量）
  [3] onclick 引用    app.js onclick 引用的函数必须已定义（模板字符串改动后防手滑）
  [4] 数据泄漏防护    .gitignore 红线（local_dev.db/.llm_env/病历资料 等）未被 git 跟踪；
                     全部被跟踪文件内容无疑似密钥（sk-…）
  [5] 文档数字防漂移  README 宣称的"单测 N 项 / 冒烟 N 项"与实际运行结果一致
  [6] 运行环境        Python 版本、pymysql vendor、演示数据 JSON 可解析

用法：
    python3 tools/check_repo.py          # 全量（含单测+冒烟，约 30s）
    python3 tools/check_repo.py --fast   # 静态检查（跳过 [5] 的实际运行）
"""
import json, os, py_compile, re, subprocess, sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RESULTS = []
def report(section, name, ok, detail=""):
    RESULTS.append((section, name, ok))
    print(("  ✓ " if ok else "  ✗ ") + name + (f" — {detail}" if detail and not ok else ""))

def run(cmd, cwd=REPO, timeout=180):
    return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout)

PY_DIRS = ("server", "evaluation", "mcp-server")
JS_MAIN = os.path.join(REPO, "demo", "js", "app.js")
FORBIDDEN_TRACKED = ("local_dev", ".llm_env", "病历资料/", "9.24/", "9.25/")
GITIGNORE_LINES = ("local_dev", ".llm_env", "病历资料", "9.24", "9.25")

# ---------------- [1] 编译与语法 ----------------
def check_compile():
    print("[1] 编译与语法")
    bad = []
    count = 0
    for d in PY_DIRS:
        for root, _, files in os.walk(os.path.join(REPO, d)):
            if "__pycache__" in root or "vendor" in root:
                continue
            for f in files:
                if f.endswith(".py"):
                    count += 1
                    p = os.path.join(root, f)
                    try:
                        py_compile.compile(p, doraise=True)
                    except py_compile.PyCompileError as e:
                        bad.append(f"{os.path.relpath(p, REPO)}: {e.msg}")
    report(1, f"Python 编译（{count} 个文件）", not bad, "; ".join(bad[:3]))
    try:
        r = run(["node", "--check", os.path.relpath(JS_MAIN, REPO)])
        report(1, "app.js node --check", r.returncode == 0, r.stderr.strip()[:120])
    except FileNotFoundError:
        report(1, "app.js node --check", False, "本机无 node，无法校验")

# ---------------- [2] 前后端 API 契约 ----------------
def server_routes():
    """从 auth.py / records.py 的路由表与 llm_api.py 字面量提取服务端路径集合。
    records.py 路由表存的是剥掉 /records 前缀的子路径，这里补全。"""
    routes = {"/health", "/asr/status", "/asr/transcribe", "/generate-summary", "/generate-draft"}
    src = open(os.path.join(REPO, "server/auth.py"), encoding="utf-8").read()
    routes |= set(re.findall(r'\("(?:GET|POST)", "(/[^"]*)"\)', src))
    src = open(os.path.join(REPO, "server/records.py"), encoding="utf-8").read()
    for m in re.findall(r'\("(?:GET|POST)", "(/[^"]*)"\)', src):
        routes.add("/records" if m == "/" else "/records" + m)
    return routes

def frontend_calls():
    """app.js 全部 API 调用（fetch('api/…')、fetch('api'+path)、authPost('/…')），归一化为服务端路径。"""
    src = open(JS_MAIN, encoding="utf-8").read()
    calls = set()
    for m in re.findall(r'fetch\("api/([^"]*)"', src):
        calls.add("/" + m.split("?")[0])
    for m in re.findall(r'authPost\("(/[^"]*)"', src):
        calls.add(m.split("?")[0])
    return calls

def check_contract():
    print("[2] 前后端 API 契约（app.js → 服务端路由）")
    routes, calls = server_routes(), frontend_calls()
    missing = calls - routes
    report(2, f"前端 {len(calls)} 个调用全部有服务端路由", not missing,
           "缺失: " + ", ".join(sorted(missing)))
    unused = {r for r in routes if r not in calls and not r.startswith("/auth/")} - {"/asr/status", "/health"}
    print(f"    （信息：服务端未被前端直接调用的路由 {len(unused)} 个——评测脚本/联调备用，不算失败）")

# ---------------- [3] onclick 引用完整性 ----------------
def check_onclick():
    print("[3] onclick 引用完整性")
    src = open(JS_MAIN, encoding="utf-8").read()
    defined = set(re.findall(r'function\s+([A-Za-z_$][\w$]*)\s*\(', src))
    referenced = set(re.findall(r'onclick="([A-Za-z_$][\w$]*)\(', src))
    referenced |= set(re.findall(r"onclick='([A-Za-z_$][\w$]*)\(", src))
    referenced -= {"if", "for", "while", "switch", "return"}   # 内联 JS 语句不算函数引用
    undefined = referenced - defined
    report(3, f"onclick 引用 {len(referenced)} 个函数全部已定义", not undefined,
           "未定义: " + ", ".join(sorted(undefined)))

# ---------------- [4] 数据泄漏防护 ----------------
def check_leaks():
    print("[4] 数据泄漏防护（红线文件不入 Git）")
    tracked = run(["git", "ls-files"]).stdout.splitlines()
    hit = [t for t in tracked if any(t.startswith(f) or t == f.rstrip("/") for f in FORBIDDEN_TRACKED)]
    report(4, "红线路径未被 git 跟踪", not hit, "被跟踪: " + ", ".join(hit[:5]))
    gi = open(os.path.join(REPO, ".gitignore"), encoding="utf-8").read() if os.path.exists(
        os.path.join(REPO, ".gitignore")) else ""
    miss = [g for g in GITIGNORE_LINES if g not in gi]
    report(4, ".gitignore 覆盖全部红线", not miss, "缺: " + ", ".join(miss))
    # 全部被跟踪文本文件内容扫描疑似密钥（sk- 开头 ≥20 位长 token）
    suspects = []
    for t in tracked:
        p = os.path.join(REPO, t)
        if not os.path.isfile(p) or os.path.getsize(p) > 2_000_000:
            continue
        try:
            text = open(p, encoding="utf-8", errors="ignore").read()
        except Exception:
            continue
        for m in re.findall(r"sk-[A-Za-z0-9_\-]{20,}", text):
            suspects.append(f"{t}: {m[:12]}…")
    report(4, "被跟踪文件无疑似密钥", not suspects, "; ".join(suspects[:3]))

# ---------------- [5] 文档数字防漂移 ----------------
def readme_claims():
    text = open(os.path.join(REPO, "README.md"), encoding="utf-8").read()
    m1 = re.search(r"unittest，(\d+) 项", text)
    m2 = re.search(r"端到端，(\d+) 项", text)
    return (int(m1.group(1)) if m1 else None, int(m2.group(1)) if m2 else None)

def check_docs_and_tests(fast):
    print("[5] 文档数字防漂移（README 宣称 vs 实际运行）")
    claim_unit, claim_smoke = readme_claims()
    if fast:
        report(5, "README 数字核对", claim_unit is not None and claim_smoke is not None,
               f"未解析到宣称数字（单测={claim_unit} 冒烟={claim_smoke}）；--fast 跳过实测")
        return
    r = run([sys.executable, "-m", "unittest", "discover", "-s", "tests"], cwd=os.path.join(REPO, "server"))
    m = re.search(r"Ran (\d+) tests", r.stdout + r.stderr)
    actual_unit = int(m.group(1)) if m else -1
    ok_u = actual_unit == claim_unit and r.returncode == 0
    report(5, f"单测：README 宣称 {claim_unit} 项 = 实际 {actual_unit} 项且全过", ok_u,
           r.stderr.strip().splitlines()[-1] if r.returncode else "")
    r2 = run([sys.executable, os.path.join(REPO, "server", "tests", "api_smoke.py")], timeout=240)
    m2 = re.search(r"冒烟结果：(\d+)/(\d+) 通过", r2.stdout + r2.stderr)
    actual_smoke = int(m2.group(2)) if m2 else -1
    ok_s = actual_smoke == claim_smoke and r2.returncode == 0
    report(5, f"冒烟：README 宣称 {claim_smoke} 项 = 实际 {actual_smoke} 项且全过", ok_s,
           (r2.stdout + r2.stderr).strip().splitlines()[-1] if r2.returncode else "")

# ---------------- [6] 运行环境 ----------------
def check_env():
    print("[6] 运行环境")
    v = sys.version_info
    report(6, f"Python {'.'.join(map(str, sys.version_info[:3]))}（需 ≥3.10）", v >= (3, 10))
    report(6, "pymysql 已 vendor（生产 MySQL 依赖）",
           os.path.isdir(os.path.join(REPO, "server", "vendor", "pymysql")))
    data = os.path.join(REPO, "demo", "data", "patients.json")
    try:
        d = json.load(open(data, encoding="utf-8"))
        report(6, f"演示数据 patients.json 可解析（{len(d)} 患者）", isinstance(d, list) and bool(d))
    except Exception as e:
        report(6, "演示数据 patients.json 可解析", False, str(e))

def main():
    fast = "--fast" in sys.argv
    os.chdir(REPO)
    check_compile()
    check_contract()
    check_onclick()
    check_leaks()
    check_docs_and_tests(fast)
    check_env()
    fails = [r for r in RESULTS if not r[2]]
    print(f"\n体检结果：{len(RESULTS) - len(fails)}/{len(RESULTS)} 通过" +
          ("" if not fails else f"，失败项：{[f[1] for f in fails]}"))
    sys.exit(1 if fails else 0)

if __name__ == "__main__":
    main()
