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


if __name__ == "__main__":
    unittest.main()
