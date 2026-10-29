# -*- coding: utf-8 -*-
"""一键仓库体检（P2-L 可交接性收口）
====================================================================
零依赖标准库脚本，一条命令完成六类检查，任何一项失败退出码 1：

  [1] 编译与语法     server/evaluation/mcp-server 全部 .py 可编译；app.js node --check
  [2] 前后端 API 契约 app.js 的 fetch/authPost 调用 ⊆ 服务端注册路由（auth/records 路由表 + llm_api 字面量）
  [3] onclick 引用    app.js onclick 引用的函数必须已定义（模板字符串改动后防手滑）
  [4] 数据泄漏防护    .gitignore 红线（local_dev.db/.llm_env/病历资料 等）未被 git 跟踪；
                     全部被跟踪文件内容无疑似密钥（sk-…）；患者真名零入库（代号映射×被跟踪文本）
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
        js_test = os.path.join(REPO, "demo", "tests", "upload_parse.test.js")
        if os.path.exists(js_test):
            r2 = run(["node", os.path.relpath(js_test, REPO)])
            report(1, "上传解析 JS 回归（对话体/叙述体）", r2.returncode == 0, (r2.stdout + r2.stderr).strip()[:120])
    except FileNotFoundError:
        report(1, "app.js node --check", False, "本机无 node，无法校验")

# ---------------- [2] 前后端 API 契约 ----------------
def server_routes():
    """从 auth.py / records.py 的路由表与 llm_api.py 字面量提取服务端路径集合。
    records.py 路由表存的是剥掉 /records 前缀的子路径，这里补全。"""
    routes = {"/health", "/asr/status", "/asr/transcribe", "/generate-summary", "/generate-draft", "/deid-check"}
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
    # 真实姓名映射防回归：患者代号映射必须走 git 忽略文件，不允许硬编码进代码
    # （2026-10-03 发现 PATIENT_CODE 字典把真实姓名写进了已提交的 evaluate_v0.py）
    hard_coded = []
    for t in tracked:
        if not t.endswith(".py"):
            continue
        try:
            text = open(os.path.join(REPO, t), encoding="utf-8", errors="ignore").read(20000)
        except Exception:
            continue
        # 字面量中文键（{"某姓名": "患者A"}）才算硬编码；目录序推导的字典推导式是合法兜底
        if re.search(r'PATIENT_CODE\s*=\s*\{\s*["\'][\u4e00-\u9fff]+["\']\s*:', text):
            hard_coded.append(t)
    report(4, "患者姓名映射未硬编码（走 git 忽略文件）", not hard_coded, "; ".join(hard_coded[:3]))

    # 真实姓名零入库扫描（患者F 接入时发现存量泄漏后新增）：代号映射的真实姓名键
    # 不得出现在任何被跟踪文本文件里（含 demo 数据自由文本——既往脱敏只替换结构化列，
    # 曾漏掉出院医嘱正文里的医护真名）。映射文件本身在 git 忽略区，仅本地体检可用。
    map_file = os.path.join(REPO, "病历资料", "患者代号映射.json")
    real_names = []
    if os.path.exists(map_file):
        try:
            real_names = [k for k in json.load(open(map_file, encoding="utf-8"))
                          if isinstance(k, str) and len(k) >= 2]
        except Exception:
            real_names = []
    leaked = []
    if real_names:
        for t in tracked:
            if not t.endswith((".py", ".json", ".md", ".js", ".html", ".txt", ".yml", ".yaml", ".css", ".csv")):
                continue
            try:
                text = open(os.path.join(REPO, t), encoding="utf-8", errors="ignore").read()
            except Exception:
                continue
            for nm in real_names:
                if nm in text:
                    leaked.append(f"{t}:{nm}")
        report(4, f"患者真名零入库（映射 {len(real_names)} 名 × 被跟踪文本）", not leaked,
               "; ".join(leaked[:5]))

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

# ---------------- [7] GitHub 同步（三步制：改代码 → 更新开发日志 → 推 GitHub）----------------
def check_github_sync():
    """确认本地进度已同步到 GitHub。

    三种情形区别对待：
      · 不是 Git 仓库（如院内交付包——打包时刻意排除 .git）：本项**不适用**，如实说明并跳过；
      · 是 Git 仓库但未配 origin（临时导出、离线环境）：只提示，不判失败；
      · 配了 origin：则"本地领先未推送""工作区有未提交改动"都算**不通过**——
        这两条正是"忘了同步 GitHub"的两种表现，是三步制的自动守门。
    """
    print("[7] GitHub 同步状态")
    if run(["git", "rev-parse", "--git-dir"]).returncode != 0:
        report(7, "GitHub 同步检查", True,
               "本目录不是 Git 仓库（如院内交付包，打包时已排除 .git）——本项不适用")
        return
    r = run(["git", "remote", "get-url", "origin"])
    if r.returncode != 0:
        report(7, "GitHub 同步检查", True,
               "未配置 origin（离线/临时导出环境）——跳过；如需同步：git remote add origin <URL>")
        return
    url = r.stdout.strip()
    report(7, f"远端 origin 已配置：{url.split('//')[-1]}", True)

    branch = run(["git", "rev-parse", "--abbrev-ref", "HEAD"]).stdout.strip() or "main"
    # 不在此处 git fetch：体检要能在离线/院内网环境跑，网络等待会拖死它。
    # 领先性按**本地跟踪引用** origin/<branch> 判定——三步制里第 3 步就是 push，
    # push 会更新该引用，因此它准确反映"上次推送后本地是否又攒了新提交"。
    if run(["git", "rev-parse", "--verify", "--quiet", f"origin/{branch}"]).returncode != 0:
        report(7, "远端分支信息可用", True,
               f"本地无 origin/{branch} 跟踪引用（尚未 fetch/push 过）——跳过领先性比对")
    else:
        r = run(["git", "rev-list", "--count", f"origin/{branch}..{branch}"])
        ahead = int(r.stdout.strip() or 0)
        report(7, f"本地无未推送提交（领先 origin/{branch} 共 {ahead} 个）", ahead == 0,
               "" if ahead == 0 else f"有 {ahead} 个提交未推：git push origin {branch}")

    # 工作区未提交改动（含未跟踪文件）
    r = run(["git", "status", "--porcelain"])
    dirty = [ln for ln in r.stdout.splitlines() if ln.strip()]
    report(7, f"工作区已全部提交（{len(dirty)} 项未提交）", not dirty,
           "" if not dirty else "有未提交改动：git add -A && git commit -m '...'")


def main():
    fast = "--fast" in sys.argv
    os.chdir(REPO)
    check_compile()
    check_contract()
    check_onclick()
    check_leaks()
    check_docs_and_tests(fast)
    check_env()
    check_github_sync()
    fails = [r for r in RESULTS if not r[2]]
    print(f"\n体检结果：{len(RESULTS) - len(fails)}/{len(RESULTS)} 通过" +
          ("" if not fails else f"，失败项：{[f[1] for f in fails]}"))
    sys.exit(1 if fails else 0)

if __name__ == "__main__":
    main()
