# -*- coding: utf-8 -*-
"""auth 单元测试：角色白名单 / _public_user / SQLite 老库 role 列迁移。"""
import os, sys, sqlite3, tempfile, unittest

SERVER_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, SERVER_DIR)
import auth


class TestValidateRegister(unittest.TestCase):
    def test_role_whitelist(self):
        data, err = auth._validate_register(
            {"username": "doc01", "password": "123456", "name": "张三", "role": "hacker"})
        self.assertIsNone(err)
        self.assertEqual(data["role"], "resident")   # 非法角色回退默认
        data, _ = auth._validate_register(
            {"username": "doc01", "password": "123456", "name": "张三", "role": "attending"})
        self.assertEqual(data["role"], "attending")

    def test_username_rules(self):
        _, err = auth._validate_register({"username": "ab", "password": "123456", "name": "张三"})
        self.assertIn("用户名", err)
        _, err = auth._validate_register({"username": "ok01", "password": "123", "name": "张三"})
        self.assertIn("密码", err)

    def test_public_user_carries_role(self):
        u = {"id": 1, "username": "x", "name": "x", "role": "qc"}
        pub = auth._public_user(u)
        self.assertEqual(pub["role"], "qc")
        self.assertEqual(pub["role_name"], "质控科")
        # 老库行无 role 键（迁移前的会话恢复场景）→ 缺省 resident
        pub2 = auth._public_user({"id": 2, "username": "y", "name": "y"})
        self.assertEqual(pub2["role"], "resident")
        self.assertEqual(pub2["role_name"], "住院医师")


class TestSqliteRoleMigration(unittest.TestCase):
    """P2-G 老库迁移：无 role 列的旧 users 表 → 补列且存量用户默认 resident。"""

    def test_migration_adds_role_column(self):
        tmp = tempfile.mkdtemp(prefix="mra_mig_")
        db = os.path.join(tmp, "old.db")
        conn = sqlite3.connect(db)
        # 旧版表结构（无 role 列，P2-G 之前的形态）
        conn.execute(
            "CREATE TABLE users (id INTEGER PRIMARY KEY AUTOINCREMENT, username TEXT UNIQUE NOT NULL,"
            " pwd_hash TEXT NOT NULL, salt TEXT NOT NULL, name TEXT NOT NULL, department TEXT DEFAULT '',"
            " title TEXT DEFAULT '', tour_done INTEGER DEFAULT 0,"
            " created_at TEXT DEFAULT CURRENT_TIMESTAMP, last_login_at TEXT)")
        conn.execute("INSERT INTO users (username, pwd_hash, salt, name) VALUES ('old001','h','s','老用户')")
        conn.commit()
        cur = conn.cursor()
        auth._migrate_role_column(cur, mysql=False)
        cur.execute("PRAGMA table_info(users)")
        cols = [r[1] for r in cur.fetchall()]
        self.assertIn("role", cols)
        cur.execute("SELECT role FROM users WHERE username='old001'")
        self.assertEqual(cur.fetchone()[0], "resident")
        # 幂等：重复迁移不报错不重复加列
        auth._migrate_role_column(cur, mysql=False)
        cur.execute("PRAGMA table_info(users)")
        self.assertEqual([r[1] for r in cur.fetchall()].count("role"), 1)
        conn.close()


class TestLoginGateAndAudit(unittest.TestCase):
    """P2-K：登录防爆破（连续失败锁定）+ 关键操作审计留痕。"""

    def setUp(self):
        self._old_active = auth._ACTIVE_SQLITE   # 用例结束恢复，避免污染其他模块的临时库
        tmp = tempfile.mkdtemp(prefix="mra_gate_")
        auth._ACTIVE_SQLITE = os.path.join(tmp, "t.db")
        conn = sqlite3.connect(auth._ACTIVE_SQLITE)
        cur = conn.cursor()
        for ddl in auth._DDL_SQLITE:
            cur.execute(ddl)
        auth._migrate_role_column(cur, mysql=False)   # role 列由迁移补齐（与 init_db 一致）
        conn.commit()
        conn.close()
        auth._login_state.clear()

    def tearDown(self):
        auth._ACTIVE_SQLITE = self._old_active
        auth._login_state.clear()

    def _attempt(self, username, password):
        class H:
            def __init__(self): self.resp = None
            def _json(self, code, obj, set_cookies=None): self.resp = (code, obj)
        h = H()
        auth.do_login(h, {"username": username, "password": password})
        return h.resp

    def _audit_count(self, action):
        conn = sqlite3.connect(auth._ACTIVE_SQLITE)
        n = conn.execute("SELECT COUNT(*) FROM audit_log WHERE action=?", (action,)).fetchone()[0]
        conn.close()
        return n

    def test_lockout_after_max_fails(self):
        for i in range(auth.LOGIN_MAX_FAILS):
            code, _ = self._attempt("ghost_user", "wrong")
            self.assertEqual(code, 401)   # 锁定前一律 401（不暴露账号存在性）
        code, obj = self._attempt("ghost_user", "wrong")
        self.assertEqual(code, 429)       # 达到阈值后锁定
        self.assertIn("锁定", obj["error"])
        # 解除锁定后恢复（401 而非 429）
        auth._login_gate_ok("ghost_user")
        code, _ = self._attempt("ghost_user", "wrong")
        self.assertEqual(code, 401)

    def test_audit_rows_written(self):
        self._attempt("ghost_user", "wrong")           # 登录失败 → login_fail
        class H:
            def __init__(self): self.resp = None
            def _json(self, code, obj, set_cookies=None): self.resp = (code, obj)
        h = H()
        auth.do_register(h, {"username": "audit_doc", "password": "123456", "name": "审医生"})
        self.assertEqual(h.resp[0], 200)               # 注册成功
        self._attempt("audit_doc", "123456")           # 登录成功 → login
        self.assertGreaterEqual(self._audit_count("login_fail"), 1)
        self.assertGreaterEqual(self._audit_count("register"), 1)
        self.assertGreaterEqual(self._audit_count("login"), 1)
        # 审计只追加：字段不含口令明文
        conn = sqlite3.connect(auth._ACTIVE_SQLITE)
        row = conn.execute("SELECT detail FROM audit_log WHERE action='register'").fetchone()
        conn.close()
        self.assertNotIn("123456", (row[0] or ""))


if __name__ == "__main__":
    unittest.main()
