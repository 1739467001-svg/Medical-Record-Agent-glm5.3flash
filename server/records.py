# -*- coding: utf-8 -*-
"""
归档与审签流（P2-G 多角色与审签流，PRD 2.1 责任矩阵）
====================================================================
流程状态机（替代演示版"模拟归档"，登录用户走真库存储）：
  住院医师 四色确认 → 提交归档（submitted 待审签）
  上级医师 审签     → 签发（signed）/ 退回（rejected，原因回流住院医师修改后重新提交）
  质控科   抽查     → 合格（ok）/ 缺陷（issue），独立标记不改变审签状态
角色（auth.ROLES）：resident 住院医师（书写与确认，默认）/ attending 上级医师（审签）/ qc 质控科（抽查）。
存储：与 auth 同连接层（生产 MySQL / 研发 SQLite 自动降级）；首次建表时播种演示数据，
保证上级医师/质控科登录即可看到审签工作台的真实效果。
"""
import json, time, sys
import auth

STATUS_NAMES = {"submitted": "待上级医师审签", "signed": "已审签", "rejected": "已退回"}
SPOT_NAMES = {"ok": "抽查合格", "issue": "抽查缺陷", "": "未抽查"}
Q = "%s" if auth.BACKEND == "mysql" else "?"


def init_db():
    ddl_mysql = """CREATE TABLE IF NOT EXISTS archives (
        id INT AUTO_INCREMENT PRIMARY KEY,
        record_no VARCHAR(32) NOT NULL UNIQUE,
        patient_code VARCHAR(16) NOT NULL,
        admission_idx INT NOT NULL DEFAULT 1,
        doc_type VARCHAR(32) NOT NULL,
        disease VARCHAR(64) DEFAULT '',
        resident_id INT NOT NULL,
        resident_name VARCHAR(64) NOT NULL,
        fields_json MEDIUMTEXT NOT NULL,
        qc_json MEDIUMTEXT,
        edits_json MEDIUMTEXT,
        xml_text MEDIUMTEXT,
        total_cnt INT NOT NULL DEFAULT 0,
        yellow_cnt INT NOT NULL DEFAULT 0,
        status VARCHAR(16) NOT NULL DEFAULT 'submitted',
        signed_name VARCHAR(64), signed_at DATETIME NULL, sign_comment VARCHAR(512) DEFAULT '',
        spot_name VARCHAR(64), spot_at DATETIME NULL,
        spot_result VARCHAR(16) DEFAULT '', spot_comment VARCHAR(512) DEFAULT '',
        created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
        KEY idx_status (status), KEY idx_rid (resident_id)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4"""
    ddl_sqlite = """CREATE TABLE IF NOT EXISTS archives (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        record_no TEXT NOT NULL UNIQUE,
        patient_code TEXT NOT NULL,
        admission_idx INTEGER NOT NULL DEFAULT 1,
        doc_type TEXT NOT NULL,
        disease TEXT DEFAULT '',
        resident_id INTEGER NOT NULL,
        resident_name TEXT NOT NULL,
        fields_json TEXT NOT NULL,
        qc_json TEXT, edits_json TEXT, xml_text TEXT,
        total_cnt INTEGER NOT NULL DEFAULT 0,
        yellow_cnt INTEGER NOT NULL DEFAULT 0,
        status TEXT NOT NULL DEFAULT 'submitted',
        signed_name TEXT, signed_at TEXT, sign_comment TEXT DEFAULT '',
        spot_name TEXT, spot_at TEXT, spot_result TEXT DEFAULT '', spot_comment TEXT DEFAULT '',
        created_at TEXT DEFAULT CURRENT_TIMESTAMP)"""
    conn = auth._connect()
    try:
        cur = conn.cursor()
        cur.execute(ddl_mysql if auth.BACKEND == "mysql" else ddl_sqlite)
        cur.execute(f"SELECT COUNT(*) AS n FROM archives")
        if cur.fetchone()["n"] == 0:
            _seed(cur)
    finally:
        conn.close()


def _seed(cur):
    """演示种子数据：让上级医师/质控科首次进入审签工作台即有真实可操作内容。"""
    demo_fields = [
        {"label": "主诉", "value": "右下腹部疼痛1个月", "source": "green"},
        {"label": "现病史", "value": "患者于1个月前无明显诱因出现右下腹腹痛，为持续性钝痛……（演示数据节选）", "source": "green"},
        {"label": "体温", "value": "36.8", "source": "blue"},
        {"label": "脉搏", "value": "78", "source": "blue"},
        {"label": "发育", "value": "正常", "source": "gray"},
        {"label": "初步诊断", "value": "1.慢性阑尾炎", "source": "green"},
    ]
    now = time.strftime("%Y-%m-%d %H:%M:%S")
    rows = [
        ("AR-20260929-0001", "DA0001", 1, "入院记录", "急性阑尾炎", 0, "王医生（演示）",
         "signed", "李主任（演示）", "病例特点归纳完整，诊断依据充分，同意签发。", "合格", "甲级病历", now),
        ("AR-20260930-0002", "DA0001", 1, "首次病程记录", "急性阑尾炎", 0, "王医生（演示）",
         "submitted", None, "", "", "", now),
    ]
    for no, code, idx, doc, dis, rid, rname, st, sname, scomment, sres, scomment2, created in rows:
        total, yellow = len(demo_fields), 0
        cur.execute(f"INSERT INTO archives (record_no, patient_code, admission_idx, doc_type, disease, "
                    f"resident_id, resident_name, fields_json, qc_json, edits_json, xml_text, total_cnt, yellow_cnt, "
                    f"status, signed_name, signed_at, sign_comment, spot_name, spot_at, spot_result, spot_comment, created_at) "
                    f"VALUES ({Q},{Q},{Q},{Q},{Q},{Q},{Q},{Q},{Q},{Q},{Q},{Q},{Q},{Q},{Q},{Q},{Q},{Q},{Q},{Q},{Q},{Q})",
                    (no, code, idx, doc, dis, rid, rname, json.dumps(demo_fields, ensure_ascii=False),
                     json.dumps({"summary": "通过 34 / 拦截修正 0 / 缺失待补 1 / 复核提示 2", "blocked": [], "missing": ["医生签名（须医生手工签章）"], "warnings": []}, ensure_ascii=False),
                     "[]", "", total, yellow, st, sname, now if sname else None, scomment,
                     "质控科（演示）" if sres else None, now if sres else None, sres, scomment2, created))


def _now():
    return time.strftime("%Y-%m-%d %H:%M:%S")


def _current_user(handler):
    return auth._user_by_token(handler.cookies.get(auth.SESSION_COOKIE))


def _row_brief(r):
    return {"id": r["id"], "record_no": r["record_no"], "patient_code": r["patient_code"],
            "admission_idx": r["admission_idx"], "doc_type": r["doc_type"], "disease": r["disease"] or "",
            "resident_name": r["resident_name"], "status": r["status"], "status_name": STATUS_NAMES.get(r["status"], r["status"]),
            "total_cnt": r["total_cnt"], "yellow_cnt": r["yellow_cnt"],
            "signed_name": r["signed_name"] or "", "signed_at": str(r["signed_at"] or ""),
            "sign_comment": r["sign_comment"] or "",
            "spot_result": r["spot_result"] or "", "spot_name_str": SPOT_NAMES.get(r["spot_result"] or "", "未抽查"),
            "spot_comment": r["spot_comment"] or "", "created_at": str(r["created_at"] or "")}


# ---------------- 路由入口（由 llm_api.py 调用） ----------------
def _qs(handler_path):
    """从请求路径解析查询参数（无查询串时返回空 dict）。"""
    q = (handler_path or "").split("?", 1)[1] if "?" in (handler_path or "") else ""
    return dict(p.split("=", 1) for p in q.split("&") if "=" in p)

def handle(handler, body, path_override=None):
    """处理 /records/* 请求（全部要求登录）。返回 True 表示已处理。"""
    path = (path_override or handler.path).split("?", 1)[0]
    u = _current_user(handler)
    if not u:
        handler._json(401, {"error": "请先登录（审签工作台需医生账号）"})
        return True
    sub = path[len("/records"):] or "/"
    routes = {
        ("POST", "/submit"): lambda: do_submit(handler, body, u),
        ("GET", "/"): lambda: do_list(handler, u, _qs(handler.path)),
        ("GET", "/detail"): lambda: do_detail(handler, u, _qs(handler.path)),
        ("POST", "/review"): lambda: do_review(handler, body, u),
        ("POST", "/spotcheck"): lambda: do_spotcheck(handler, body, u),
    }
    fn = routes.get((handler.command, sub))
    if not fn:
        handler._json(404, {"error": "not found"})
        return True
    fn()
    return True


def do_submit(handler, body, u):
    """四色确认后真归档：住院医师（或任何登录医生）提交文书归档包。"""
    try:
        fields = body.get("fields") or []
        if not isinstance(fields, list) or not fields:
            return handler._json(400, {"error": "归档包缺少字段数据"})
        patient_code = str(body.get("code") or "").strip()[:16]
        doc_type = str(body.get("doc") or "").strip()[:32]
        if not patient_code or not doc_type:
            return handler._json(400, {"error": "缺少患者或文书类型"})
        rec = {
            "code": patient_code, "idx": int(body.get("admission") or 1),
            "doc": doc_type, "disease": str(body.get("disease") or "")[:64],
            "fields": json.dumps(fields, ensure_ascii=False),
            "qc": json.dumps(body.get("qc") or {}, ensure_ascii=False),
            "edits": json.dumps(body.get("edits") or [], ensure_ascii=False),
            "xml": str(body.get("xml") or "")[:200000],
            "total": len([f for f in fields if f.get("label")]),
            "yellow": len([f for f in fields if f.get("label") and not str(f.get("value") or "").strip()]),
        }
    except Exception as e:
        return handler._json(400, {"error": f"归档包解析失败: {e}"})
    day = time.strftime("%Y%m%d")
    conn = auth._connect()
    try:
        cur = conn.cursor()
        cur.execute(f"SELECT COUNT(*) AS n FROM archives WHERE record_no LIKE {Q}", (f"AR-{day}-%",))
        rec_no = f"AR-{day}-{cur.fetchone()['n'] + 1:04d}"
        cur.execute(f"INSERT INTO archives (record_no, patient_code, admission_idx, doc_type, disease, "
                    f"resident_id, resident_name, fields_json, qc_json, edits_json, xml_text, total_cnt, yellow_cnt, status, created_at) "
                    f"VALUES ({Q},{Q},{Q},{Q},{Q},{Q},{Q},{Q},{Q},{Q},{Q},{Q},{Q},'submitted',{Q})",
                    (rec_no, rec["code"], rec["idx"], rec["doc"], rec["disease"],
                     u["id"], u.get("name") or u["username"], rec["fields"], rec["qc"], rec["edits"], rec["xml"],
                     rec["total"], rec["yellow"], _now()))
        rid = cur.lastrowid
    finally:
        conn.close()
    handler._json(200, {"ok": True, "id": rid, "record_no": rec_no, "status": "submitted",
                        "status_name": STATUS_NAMES["submitted"],
                        "resident": u.get("name") or u["username"]})


def do_list(handler, u, qs):
    """审签工作台列表：住院医师看本人文书（含退回原因回流）；上级医师/质控科看全部。"""
    where, args = "", []
    if u.get("role") not in ("attending", "qc"):
        where = f"WHERE resident_id={Q}"
        args = [u["id"]]
    status = (qs.get("status") or "").strip()
    if status in STATUS_NAMES:
        where = (where + " AND " if where else "WHERE ") + f"status={Q}"
        args.append(status)
    conn = auth._connect()
    try:
        cur = conn.cursor()
        cur.execute(f"SELECT * FROM archives {where} ORDER BY id DESC LIMIT 100", tuple(args))
        rows = [_row_brief(r) for r in auth._rows(cur)]
    finally:
        conn.close()
    stats = {s: sum(1 for r in rows if r["status"] == s) for s in STATUS_NAMES}
    spot = {"ok": sum(1 for r in rows if r["spot_result"] == "ok"),
            "issue": sum(1 for r in rows if r["spot_result"] == "issue")}
    handler._json(200, {"ok": True, "role": u.get("role") or "resident", "records": rows,
                        "stats": stats, "spot": spot})


def do_detail(handler, u, qs):
    try:
        rid = int(qs.get("id") or 0)
    except ValueError:
        return handler._json(400, {"error": "参数错误"})
    conn = auth._connect()
    try:
        cur = conn.cursor()
        cur.execute(f"SELECT * FROM archives WHERE id={Q}", (rid,))
        rows = auth._rows(cur)
    finally:
        conn.close()
    if not rows:
        return handler._json(404, {"error": "文书不存在"})
    r = rows[0]
    if u.get("role") not in ("attending", "qc") and r["resident_id"] != u["id"]:
        return handler._json(403, {"error": "无权查看他人文书"})
    d = _row_brief(r)
    try:
        d["fields"] = json.loads(r["fields_json"])
    except Exception:
        d["fields"] = []
    try:
        d["qc"] = json.loads(r["qc_json"] or "{}")
    except Exception:
        d["qc"] = {}
    try:
        d["edits"] = json.loads(r["edits_json"] or "[]")
    except Exception:
        d["edits"] = []
    d["spot_by"] = r["spot_name"] or ""
    handler._json(200, {"ok": True, "record": d})


def do_review(handler, body, u):
    """上级医师审签：签发 / 退回（退回原因必填，回流住院医师）。"""
    if u.get("role") != "attending":
        return handler._json(403, {"error": "仅上级医师可执行审签"})
    try:
        rid = int(body.get("id") or 0)
    except ValueError:
        return handler._json(400, {"error": "参数错误"})
    action = str(body.get("action") or "")
    comment = str(body.get("comment") or "").strip()[:512]
    if action not in ("sign", "reject"):
        return handler._json(400, {"error": "action 需为 sign 或 reject"})
    if action == "reject" and not comment:
        return handler._json(400, {"error": "退回必须填写原因（将回流给书写医生）"})
    status = "signed" if action == "sign" else "rejected"
    conn = auth._connect()
    try:
        cur = conn.cursor()
        cur.execute(f"SELECT status FROM archives WHERE id={Q}", (rid,))
        rows = auth._rows(cur)
        if not rows:
            return handler._json(404, {"error": "文书不存在"})
        if rows[0]["status"] != "submitted":
            return handler._json(409, {"error": f"该文书当前状态为「{STATUS_NAMES.get(rows[0]['status'])}」，不可重复审签"})
        name = (u.get("name") or u["username"]) + "（上级医师审签）"
        cur.execute(f"UPDATE archives SET status={Q}, signed_name={Q}, signed_at={Q}, sign_comment={Q} WHERE id={Q}",
                    (status, name, _now(), comment, rid))
    finally:
        conn.close()
    handler._json(200, {"ok": True, "id": rid, "status": status, "status_name": STATUS_NAMES[status],
                        "signed_name": name})


def do_spotcheck(handler, body, u):
    """质控科抽查：合格 / 缺陷（附意见），独立于审签状态。"""
    if u.get("role") != "qc":
        return handler._json(403, {"error": "仅质控科可执行抽查"})
    try:
        rid = int(body.get("id") or 0)
    except ValueError:
        return handler._json(400, {"error": "参数错误"})
    result = str(body.get("result") or "")
    comment = str(body.get("comment") or "").strip()[:512]
    if result not in ("ok", "issue"):
        return handler._json(400, {"error": "result 需为 ok（合格）或 issue（缺陷）"})
    conn = auth._connect()
    try:
        cur = conn.cursor()
        name = (u.get("name") or u["username"]) + "（质控科抽查）"
        cur.execute(f"UPDATE archives SET spot_result={Q}, spot_comment={Q}, spot_name={Q}, spot_at={Q} WHERE id={Q}",
                    (result, comment, name, _now(), rid))
    finally:
        conn.close()
    handler._json(200, {"ok": True, "id": rid, "spot_result": result, "spot_name_str": SPOT_NAMES[result]})


try:
    init_db()
except Exception as e:
    print(f"[records] 初始化失败（审签功能降级不可用）: {e}", file=sys.stderr)
