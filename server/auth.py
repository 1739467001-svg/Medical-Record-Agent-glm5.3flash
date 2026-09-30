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
Cookie 注意：演示环境为 HTTP，故不带 Secure 属性；若上 HTTPS 部署请加上。
"""
import hashlib, json, os, re, secrets, sqlite3, sys, time
from datetime import datetime

SERVER_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(SERVER_DIR, "vendor"))

SESSION_COOKIE = "mra_session"
SESSION_TTL = 7 * 24 * 3600
PBKDF2_ROUNDS = 200_000
USERNAME_RE = re.compile(r"^[A-Za-z0-9_@.\-]{3,32}$")

MYSQL_CONF = {
    "host": os.environ.get("MRA_MYSQL_HOST", ""),
    "port": int(os.environ.get("MRA_MYSQL_PORT", "3306")),
    "user": os.environ.get("MRA_MYSQL_USER", ""),
    "password": os.environ.get("MRA_MYSQL_PASSWORD", ""),
    "database": os.environ.get("MRA_MYSQL_DB", "medical_record_agent"),
}
BACKEND = "mysql" if MYSQL_CONF["host"] else "sqlite"
SQLITE_PATH = os.path.join(SERVER_DIR, "local_dev.db")

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
            conn.close()
            _ACTIVE_SQLITE = cand
            if cand != SQLITE_PATH:
                print(f"[auth] server 目录不可写，SQLite 降级至 {cand}（容器重启数据清空；生产请配置 MySQL）", file=sys.stderr)
            return
        except sqlite3.OperationalError as e:
            last_err = e
    raise RuntimeError(f"SQLite 数据库无法创建（{last_err}）；请配置 MRA_MYSQL_* 使用 MySQL")

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
            "tour_done": bool(u.get("tour_done"))}

# ---------------- 校验 ----------------
def _validate_register(body):
    username = str(body.get("username") or "").strip()
    password = str(body.get("password") or "")
    name = str(body.get("name") or "").strip()
    dept = str(body.get("department") or "").strip()[:64]
    title = str(body.get("title") or "").strip()[:64]
    if not USERNAME_RE.match(username):
        return None, "用户名需为 3~32 位字母/数字/下划线（可用工号或手机号）"
    if len(password) < 6:
        return None, "密码至少 6 位"
    if not name or len(name) > 32:
        return None, "请填写真实姓名（32 字以内）"
    return {"username": username, "password": password, "name": name, "department": dept, "title": title}, None

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
        cur.execute(f"INSERT INTO users (username, pwd_hash, salt, name, department, title) "
                    f"VALUES ({q},{q},{q},{q},{q},{q})",
                    (data["username"], pwd_hash, salt, data["name"], data["department"], data["title"]))
        uid = cur.lastrowid
    finally:
        conn.close()
    token = _create_session(uid)
    user = {"id": uid, "username": data["username"], "name": data["name"],
            "department": data["department"], "title": data["title"], "tour_done": False}
    handler._json(200, {"ok": True, "user": user}, set_cookies=[_session_cookie(token)])

def do_login(handler, body):
    username = str(body.get("username") or "").strip()
    password = str(body.get("password") or "")
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
        return handler._json(401, {"error": "用户名或密码错误"})
    now = time.strftime("%Y-%m-%d %H:%M:%S")
    conn = _connect()
    try:
        q = "%s" if BACKEND == "mysql" else "?"
        conn.cursor().execute(f"UPDATE users SET last_login_at={q} WHERE id={q}", (now, u["id"]))
    finally:
        conn.close()
    token = _create_session(u["id"])
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
