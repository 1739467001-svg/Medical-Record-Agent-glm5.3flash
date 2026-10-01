# -*- coding: utf-8 -*-
"""
AI 病历智能体 · 用户认证与会话（登录 / 注册 / 新手引导标记）
==========================================================
存储（二选一，启动时自动判定）：
  - 生产：MySQL（PyMySQL，纯 Python，已 vendor 于 server/vendor/）
      连接参数来自环境变量 MRA_MYSQL_HOST / MRA_MYSQL_PORT / MRA_MYSQL_USER /
      MRA_MYSQL_PASSWORD / MRA_MYSQL_DB（建议写入 .llm_env，chmod 600，不入 Git）
  - 本地研发：未配置 MRA_MYSQL_HOST 时降级 SQLite（server/local_dev.db），仅研发用，不入 Git
安全设计：
  - 密码 PBKDF2-HMAC-SHA256（200k 轮）+ 每用户 16 字节随机盐，库中不存明文
  - 会话令牌 32 字节随机（secrets），HttpOnly Cookie，7 天有效，服务端可注销
  - 全部查询参数化，防 SQL 注入；输入做长度/格式白名单校验
  - 登录防爆破：15 分钟窗口内连续失败 5 次锁定 15 分钟（内存态，单进程部署）
  - 审计留痕（P2-K）：关键操作只追加写入 audit_log（audit_log 函数），失败不阻断业务
Cookie 注意：演示环境为 HTTP，故不带 Secure 属性；若上 HTTPS 部署请加上。
"""
import hashlib, json, os, re, secrets, sqlite3, sys, threading, time
from datetime import datetime

SERVER_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(SERVER_DIR, "vendor"))

SESSION_COOKIE = "mra_session"
SESSION_TTL = 7 * 24 * 3600
PBKDF2_ROUNDS = 200_000
USERNAME_RE = re.compile(r"^[A-Za-z0-9_@.\-]{3,32}$")

# 系统角色（P2-G 多角色与审签流，PRD 2.1 责任矩阵）：
#   resident  住院/管床医生 —— 书写与确认提交（默认）
#   attending 上级/查房医师 —— 审签（签发/退回），签名责任方
#   qc        质控科（科室主任/医务科）—— 病历抽查
ROLES = ("resident", "attending", "qc")
ROLE_NAMES = {"resident": "住院医师", "attending": "上级医师", "qc": "质控科"}

MYSQL_CONF = {
    "host": os.environ.get("MRA_MYSQL_HOST", ""),
    "port": int(os.environ.get("MRA_MYSQL_PORT", "3306")),
    "user": os.environ.get("MRA_MYSQL_USER", ""),
    "password": os.environ.get("MRA_MYSQL_PASSWORD", ""),
    "database": os.environ.get("MRA_MYSQL_DB", "medical_record_agent"),
}
BACKEND = "mysql" if MYSQL_CONF["host"] else "sqlite"
# SQLite 路径：默认 server/local_dev.db；可用 MRA_SQLITE_PATH 重定向（冒烟测试/沙箱隔离用）
SQLITE_PATH = os.environ.get("MRA_SQLITE_PATH") or os.path.join(SERVER_DIR, "local_dev.db")

_DDL_MYSQL = [
    """CREATE TABLE IF NOT EXISTS users (
        id INT AUTO_INCREMENT PRIMARY KEY,
        username VARCHAR(32) NOT NULL UNIQUE,
        pwd_hash CHAR(64) NOT NULL,
        salt CHAR(32) NOT NULL,
        name VARCHAR(64) NOT NULL,
        department VARCHAR(64) NOT NULL DEFAULT '',
        title VARCHAR(64) NOT NULL DEFAULT '',
        tour_done TINYINT NOT NULL DEFAULT 0,
        created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
        last_login_at DATETIME NULL
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",
    """CREATE TABLE IF NOT EXISTS sessions (
        token CHAR(64) PRIMARY KEY,
        user_id INT NOT NULL,
        created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
        expires_at DATETIME NOT NULL,
        KEY idx_uid (user_id)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",
    """CREATE TABLE IF NOT EXISTS audit_log (
        id INT AUTO_INCREMENT PRIMARY KEY,
        ts DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
        user_id INT NULL,
        username VARCHAR(32) DEFAULT '',
        name VARCHAR(64) DEFAULT '',
        role VARCHAR(16) DEFAULT '',
        action VARCHAR(32) NOT NULL,
        target VARCHAR(128) DEFAULT '',
        detail VARCHAR(512) DEFAULT '',
        KEY idx_ts (ts), KEY idx_action (action)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",
]
_DDL_SQLITE = [
    """CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        username TEXT UNIQUE NOT NULL,
        pwd_hash TEXT NOT NULL, salt TEXT NOT NULL,
        name TEXT NOT NULL, department TEXT DEFAULT '', title TEXT DEFAULT '',
        tour_done INTEGER DEFAULT 0,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP, last_login_at TEXT)""",
    """CREATE TABLE IF NOT EXISTS sessions (
        token TEXT PRIMARY KEY, user_id INTEGER NOT NULL,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP, expires_at TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS audit_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts TEXT DEFAULT CURRENT_TIMESTAMP,
        user_id INTEGER,
        username TEXT DEFAULT '', name TEXT DEFAULT '', role TEXT DEFAULT '',
        action TEXT NOT NULL, target TEXT DEFAULT '', detail TEXT DEFAULT '')""",
]

_ACTIVE_SQLITE = SQLITE_PATH  # 实际生效的 SQLite 路径（目录不可写时自动降级到 /tmp）

def _connect():
    if BACKEND == "mysql":
        import pymysql
        return pymysql.connect(cursorclass=pymysql.cursors.DictCursor, autocommit=True, **MYSQL_CONF)
    conn = sqlite3.connect(_ACTIVE_SQLITE, timeout=10)
    conn.isolation_level = None  # autocommit，与 MySQL 分支 autocommit=True 语义一致
    conn.row_factory = sqlite3.Row
    return conn

def _rows(cur):
    return [dict(r) for r in cur.fetchall()]

def init_db():
    global _ACTIVE_SQLITE
    if BACKEND == "mysql":
        conn = _connect()
        try:
            cur = conn.cursor()
            for ddl in _DDL_MYSQL:
                cur.execute(ddl)
            _migrate_role_column(cur, mysql=True)
        finally:
            conn.close()
        return
    # SQLite：依次尝试配置路径与 /tmp 兜底（容器内挂载的 repo/ 可能为只读）
    last_err = None
    for cand in (SQLITE_PATH, "/tmp/mra_local_dev.db"):
        try:
            conn = sqlite3.connect(cand, timeout=10)
            conn.isolation_level = None
            cur = conn.cursor()
            for ddl in _DDL_SQLITE:
                cur.execute(ddl)
            _migrate_role_column(cur, mysql=False)
            conn.close()
            _ACTIVE_SQLITE = cand
            if cand != SQLITE_PATH:
                print(f"[auth] server 目录不可写，SQLite 降级至 {cand}（容器重启数据清空；生产请配置 MySQL）", file=sys.stderr)
            return
        except sqlite3.OperationalError as e:
            last_err = e
    raise RuntimeError(f"SQLite 数据库无法创建（{last_err}）；请配置 MRA_MYSQL_* 使用 MySQL")

def _migrate_role_column(cur, mysql):
    """P2-G 老库迁移：users 补 role 列（已存在则跳过）。"""
    if mysql:
        cur.execute("SELECT COUNT(*) AS n FROM information_schema.COLUMNS "
                    "WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME='users' AND COLUMN_NAME='role'")
        if cur.fetchone()["n"] == 0:
            cur.execute("ALTER TABLE users ADD COLUMN role VARCHAR(16) NOT NULL DEFAULT 'resident'")
    else:
        cur.execute("PRAGMA table_info(users)")
        if not any(r[1] == "role" for r in cur.fetchall()):
            cur.execute("ALTER TABLE users ADD COLUMN role TEXT NOT NULL DEFAULT 'resident'")

# ---------------- 密码 ----------------
def _hash_pwd(pwd, salt_hex):
    return hashlib.pbkdf2_hmac("sha256", pwd.encode("utf-8"), bytes.fromhex(salt_hex), PBKDF2_ROUNDS).hex()

def _new_pwd(pwd):
    salt = secrets.token_hex(16)
    return _hash_pwd(pwd, salt), salt

# ---------------- 会话 ----------------
def _create_session(user_id):
    token = secrets.token_hex(32)
    exp = datetime.utcfromtimestamp(time.time() + SESSION_TTL).strftime("%Y-%m-%d %H:%M:%S")
    conn = _connect()
    try:
        cur = conn.cursor()
        cur.execute("DELETE FROM sessions WHERE expires_at < %s" if BACKEND == "mysql"
                    else "DELETE FROM sessions WHERE expires_at < ?", (time.strftime("%Y-%m-%d %H:%M:%S"),))
        cur.execute("INSERT INTO sessions (token, user_id, expires_at) VALUES (%s,%s,%s)"
                    if BACKEND == "mysql" else "INSERT INTO sessions (token, user_id, expires_at) VALUES (?,?,?)",
                    (token, user_id, exp))
    finally:
        conn.close()
    return token

def _session_cookie(token):
    return f"{SESSION_COOKIE}={token}; Path=/; Max-Age={SESSION_TTL}; HttpOnly; SameSite=Lax"

def _clear_cookie():
    return f"{SESSION_COOKIE}=; Path=/; Max-Age=0; HttpOnly; SameSite=Lax"

def _user_by_token(token):
    if not token: return None
    conn = _connect()
    try:
        cur = conn.cursor()
        cur.execute("SELECT u.* FROM sessions s JOIN users u ON u.id=s.user_id "
                    "WHERE s.token=%s AND s.expires_at > %s" if BACKEND == "mysql"
                    else "SELECT u.* FROM sessions s JOIN users u ON u.id=s.user_id "
                         "WHERE s.token=? AND s.expires_at > ?",
                    (token, time.strftime("%Y-%m-%d %H:%M:%S")))
        rows = _rows(cur)
        return rows[0] if rows else None
    finally:
        conn.close()

def _public_user(u):
    return {"id": u["id"], "username": u["username"], "name": u["name"],
            "department": u.get("department") or "", "title": u.get("title") or "",
            "role": u.get("role") or "resident", "role_name": ROLE_NAMES.get(u.get("role") or "resident", "住院医师"),
            "tour_done": bool(u.get("tour_done"))}

# ---------------- 审计留痕（P2-K：只追加，失败不阻断业务主流程） ----------------
def audit_log(user, action, target="", detail=""):
    """关键操作审计写入。user 为登录用户 dict（可为 None=未登录事件，如登录失败）。
   绝不抛异常：审计属旁路，写失败仅告警，不影响登录/审签等主流程。"""
    try:
        u = user or {}
        conn = _connect()
        try:
            q = "%s" if BACKEND == "mysql" else "?"
            conn.cursor().execute(
                f"INSERT INTO audit_log (user_id, username, name, role, action, target, detail) "
                f"VALUES ({q},{q},{q},{q},{q},{q},{q})",
                (u.get("id"), str(u.get("username") or "")[:32], str(u.get("name") or "")[:64],
                 str(u.get("role") or "")[:16], str(action)[:32], str(target)[:128], str(detail)[:512]))
        finally:
            conn.close()
    except Exception as e:
        print(f"[auth] 审计写入失败（action={action}）: {e}", file=sys.stderr)

# ---------------- 登录防爆破（内存态：单进程部署；多副本部署需改为 DB 计数） ----------------
LOGIN_MAX_FAILS = 5
LOGIN_WINDOW_SECONDS = 900   # 失败计数窗口 15 分钟
LOGIN_LOCK_SECONDS = 900     # 触发后锁定 15 分钟
_login_state = {}
_login_gate = threading.Lock()

def _login_gate_check(username):
    """允许登录返回 (True, None)；锁定中返回 (False, 提示语)。"""
    with _login_gate:
        st = _login_state.get(username)
        if not st:
            return True, None
        now = time.time()
        if st.get("locked_until"):
            if now < st["locked_until"]:
                mins = int((st["locked_until"] - now) // 60) + 1
                return False, f"登录失败次数过多，账号已临时锁定，请约 {mins} 分钟后再试"
            _login_state.pop(username, None)   # 锁定期满自动解除
        elif now - st.get("first", now) > LOGIN_WINDOW_SECONDS:
            _login_state.pop(username, None)   # 失败窗口过期，重新计数
        return True, None

def _login_gate_fail(username):
    """记一次失败；返回 True 表示本次触发锁定。"""
    with _login_gate:
        now = time.time()
        st = _login_state.setdefault(username, {"fails": 0, "first": now})
        if now - st["first"] > LOGIN_WINDOW_SECONDS:
            st.update(fails=0, first=now)
        st["fails"] += 1
        if st["fails"] >= LOGIN_MAX_FAILS:
            st["locked_until"] = now + LOGIN_LOCK_SECONDS
            return True
        return False

def _login_gate_ok(username):
    with _login_gate:
        _login_state.pop(username, None)

# ---------------- 校验 ----------------
def _validate_register(body):
    username = str(body.get("username") or "").strip()
    password = str(body.get("password") or "")
    name = str(body.get("name") or "").strip()
    dept = str(body.get("department") or "").strip()[:64]
    title = str(body.get("title") or "").strip()[:64]
    role = str(body.get("role") or "resident").strip()
    if role not in ROLES:
        role = "resident"
    if not USERNAME_RE.match(username):
        return None, "用户名需为 3~32 位字母/数字/下划线（可用工号或手机号）"
    if len(password) < 6:
        return None, "密码至少 6 位"
    if not name or len(name) > 32:
        return None, "请填写真实姓名（32 字以内）"
    return {"username": username, "password": password, "name": name,
            "department": dept, "title": title, "role": role}, None

# ---------------- 路由入口（由 llm_api.py 调用） ----------------
def handle(handler, body, path_override=None):
    """处理 /auth/* 请求。handler 需提供 path/command/_json/cookies；返回 True 表示已处理。
    path_override：llm_api 开发静态托管剥离 /api/ 前缀后传入的干净路径。"""
    path = (path_override or handler.path).split("?", 1)[0]
    method = handler.command
    routes = {
        ("POST", "/auth/register"): do_register,
        ("POST", "/auth/login"): do_login,
        ("POST", "/auth/logout"): do_logout,
        ("POST", "/auth/tour-done"): do_tour_done,
        ("GET", "/auth/me"): do_me,
    }
    fn = routes.get((method, path))
    if not fn:
        handler._json(404, {"error": "not found"})
        return True
    fn(handler, body)
    return True

def do_register(handler, body):
    data, err = _validate_register(body)
    if err: return handler._json(400, {"error": err})
    pwd_hash, salt = _new_pwd(data["password"])
    conn = _connect()
    try:
        cur = conn.cursor()
        q = "%s" if BACKEND == "mysql" else "?"
        cur.execute(f"SELECT id FROM users WHERE username={q}", (data["username"],))
        if _rows(cur):
            return handler._json(409, {"error": "该用户名已被注册"})
        cur.execute(f"INSERT INTO users (username, pwd_hash, salt, name, department, title, role) "
                    f"VALUES ({q},{q},{q},{q},{q},{q},{q})",
                    (data["username"], pwd_hash, salt, data["name"], data["department"], data["title"], data["role"]))
        uid = cur.lastrowid
    finally:
        conn.close()
    token = _create_session(uid)
    user = {"id": uid, "username": data["username"], "name": data["name"],
            "department": data["department"], "title": data["title"],
            "role": data["role"], "role_name": ROLE_NAMES[data["role"]], "tour_done": False}
    audit_log(user, "register", target=data["username"], detail=f"角色 {ROLE_NAMES[data['role']]}")
    handler._json(200, {"ok": True, "user": user}, set_cookies=[_session_cookie(token)])

def do_login(handler, body):
    username = str(body.get("username") or "").strip()
    password = str(body.get("password") or "")
    if username:
        ok, err = _login_gate_check(username)
        if not ok:
            return handler._json(429, {"error": err})
    conn = _connect()
    try:
        q = "%s" if BACKEND == "mysql" else "?"
        cur = conn.cursor()
        cur.execute(f"SELECT * FROM users WHERE username={q}", (username,))
        rows = _rows(cur)
    finally:
        conn.close()
    u = rows[0] if rows else None
    if not u or _hash_pwd(password, u["salt"]) != u["pwd_hash"]:
        locked = _login_gate_fail(username) if username else False
        audit_log(None, "login_fail", target=username[:32],
                  detail="触发临时锁定" if locked else "用户名或口令不符")
        return handler._json(401, {"error": "用户名或密码错误"})
    _login_gate_ok(username)
    now = time.strftime("%Y-%m-%d %H:%M:%S")
    conn = _connect()
    try:
        q = "%s" if BACKEND == "mysql" else "?"
        conn.cursor().execute(f"UPDATE users SET last_login_at={q} WHERE id={q}", (now, u["id"]))
    finally:
        conn.close()
    token = _create_session(u["id"])
    audit_log(u, "login", target=username[:32])
    handler._json(200, {"ok": True, "user": _public_user(u)}, set_cookies=[_session_cookie(token)])

def do_logout(handler, body):
    token = handler.cookies.get(SESSION_COOKIE)
    if token:
        conn = _connect()
        try:
            q = "%s" if BACKEND == "mysql" else "?"
            conn.cursor().execute(f"DELETE FROM sessions WHERE token={q}", (token,))
        finally:
            conn.close()
    handler._json(200, {"ok": True}, set_cookies=[_clear_cookie()])

def do_me(handler, body):
    u = _user_by_token(handler.cookies.get(SESSION_COOKIE))
    if not u: return handler._json(401, {"error": "未登录"})
    handler._json(200, {"user": _public_user(u)})

def do_tour_done(handler, body):
    u = _user_by_token(handler.cookies.get(SESSION_COOKIE))
    if not u: return handler._json(401, {"error": "未登录"})
    conn = _connect()
    try:
        q = "%s" if BACKEND == "mysql" else "?"
        conn.cursor().execute(f"UPDATE users SET tour_done=1 WHERE id={q}", (u["id"],))
    finally:
        conn.close()
    handler._json(200, {"ok": True})

init_db()
