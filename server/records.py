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

# 审计动作中文名（P2-K 审计留痕；与 auth.audit_log 写入的 action 对应）
AUDIT_ACTION_NAMES = {
    "login": "登录", "login_fail": "登录失败", "register": "注册",
    "archive_submit": "提交归档", "review_sign": "审签·签发", "review_reject": "审签·退回",
    "spotcheck": "质控抽查", "generate_draft": "生成草稿", "generate_summary": "生成摘要",
    "asr_transcribe": "录音转写", "export_archive": "导出归档包",
}


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
    """演示种子数据：让上级医师/质控科首次进入审签工作台即有真实可操作内容；
    修改留痕样例供「修改回流」视图（F8 数据闭环）演示。"""
    demo_edits = [
        {"field": "主诉", "before": "右下腹疼痛1月余", "after": "右下腹部疼痛1个月",
         "time": "2026-09-29 10:12:00", "doctor": "王医生（演示）"},
        {"field": "肝掌蜘蛛痣", "before": "无", "after": "无肝掌、蜘蛛痣",
         "time": "2026-09-29 10:14:00", "doctor": "王医生（演示）"},
    ]
    demo_fields = [
        {"label": "主诉", "value": "右下腹部疼痛1个月", "source": "green"},
        {"label": "现病史", "value": "患者于1个月前无明显诱因出现右下腹腹痛，为持续性钝痛……（演示数据节选）", "source": "green"},
        {"label": "体温", "value": "36.8", "source": "blue"},
        {"label": "脉搏", "value": "78", "source": "blue"},
        {"label": "发育", "value": "正常", "source": "gray"},
        {"label": "肝掌蜘蛛痣", "value": "无肝掌、蜘蛛痣", "source": "gray"},
        {"label": "初步诊断", "value": "1.慢性阑尾炎", "source": "green"},
    ]
    now = time.strftime("%Y-%m-%d %H:%M:%S")
    rows = [
        ("AR-20260929-0001", "DA0001", 1, "入院记录", "急性阑尾炎", 0, "王医生（演示）",
         "signed", "李主任（演示）", "病例特点归纳完整，诊断依据充分，同意签发。", "合格", "甲级病历", now, demo_edits),
        ("AR-20260930-0002", "DA0001", 1, "首次病程记录", "急性阑尾炎", 0, "王医生（演示）",
         "submitted", None, "", "", "", now,
         [{"field": "体温", "before": "36.5", "after": "36.8", "time": "2026-09-30 09:40:00", "doctor": "王医生（演示）"}]),
    ]
    for no, code, idx, doc, dis, rid, rname, st, sname, scomment, sres, scomment2, created, edits in rows:
        total, yellow = len(demo_fields), 0
        cur.execute(f"INSERT INTO archives (record_no, patient_code, admission_idx, doc_type, disease, "
                    f"resident_id, resident_name, fields_json, qc_json, edits_json, xml_text, total_cnt, yellow_cnt, "
                    f"status, signed_name, signed_at, sign_comment, spot_name, spot_at, spot_result, spot_comment, created_at) "
                    f"VALUES ({Q},{Q},{Q},{Q},{Q},{Q},{Q},{Q},{Q},{Q},{Q},{Q},{Q},{Q},{Q},{Q},{Q},{Q},{Q},{Q},{Q},{Q})",
                    (no, code, idx, doc, dis, rid, rname, json.dumps(demo_fields, ensure_ascii=False),
                     json.dumps({"summary": "通过 34 / 拦截修正 0 / 缺失待补 1 / 复核提示 2", "blocked": [], "missing": ["医生签名（须医生手工签章）"], "warnings": []}, ensure_ascii=False),
                     json.dumps(edits, ensure_ascii=False), "", total, yellow, st, sname, now if sname else None, scomment,
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


# ---------------- 修改回流分析（F8 数据闭环：医生修改留痕 → 模型反馈） ----------------
# 四色分类与前端 colorOf 同规则（领域标签集为稳定数据，内置避免跨目录依赖）
_GREEN_SET = set("主诉 主要症状 症状 持续时间 时间单位 现病史 发病情况 诱因 主要症状特点及其发展变化 伴随症状 上呼吸道症状 消化道症状 泌尿系症状 发病以来诊治经过及结果 发病以来一般情况 精神状态 食欲 睡眠 小便情况 大便情况 体重 与本次疾病无紧密关系的其他疾病情况 病史 既往史 疾病史（含外伤） 一般健康状况标志 健康状况 心血管病史 其他病史 肝炎结核病史 手术外伤输血 手术外伤史 过敏史 预防接种史 个人史 居住地 地址 接触史 疫区接触史 特殊地区居住史 有毒物质接触史 生活习惯、烟酒史 吸烟史 饮酒史 冶游史 婚育史 婚姻史 生育史 月经史 月经量 月经颜色 月经相关症状 家族史 家族健康状况 父母 兄弟姐妹 有无遗传倾向疾病 病史陈述者姓名 病史陈述者 查房记录 初步诊断 入院诊断 出院诊断 诊断依据 鉴别诊断 诊疗计划 诊疗经过 出院情况 出院医嘱 手术经过 术前诊断 术中诊断".split())
_PE_SET = set("发育 营养 表情 面容 神志 体位 配合检查 色泽 肝掌蜘蛛痣 全身浅表淋巴结 头颅异常 眼睑水肿 结膜 巩膜 角膜 瞳孔 对光反射 外耳道 乳突 鼻 鼻窦 口唇 口腔粘膜 齿龈 咽部粘膜 扁桃体 颈部 颈 颈动脉 颈静脉 气管 肝颈静脉回流征 甲状腺 甲状腺异常 胸廓 胸骨叩痛 呼吸运动 呼吸规整 肋间隙 语颤 胸膜摩擦感 叩诊 呼吸音 干湿性罗音 心前区隆起 心律 心包摩擦音 腹外形 腹壁静脉曲张 腹部紧张度 压痛反跳痛 包块 肝脏 肠鸣音 直肠肛门 肛门生殖器 脊柱 脊柱畸形 四肢 专科情况 老中青 起病 护理级别 Padua评分".split())
_VITALS_SET = set("体温 脉搏 呼吸 收缩压 舒张压".split())

def field_color(label, binding="", value=""):
    """与前端 colorOf 同规则：黄=空值/签名，绿=对话提炼，蓝=HIS 带入，灰=体格检查常规。"""
    label = str(label or "")
    if not str(value or "").strip() or "签名" in label:
        return "yellow"
    if label in _GREEN_SET:
        return "green"
    if label in ("记录时间", "辅助检查结果", "辅助检查") or label in _VITALS_SET:
        return "blue"
    if str(binding or "") == "Patient":
        return "blue"
    if label in _PE_SET or label.startswith("体格检查"):
        return "gray"
    return "green"

_COLOR_NOTE = {
    "green": "对话提炼被改多 → 抽取/提示词待优化",
    "blue": "HIS 带入被改多 → 映射规则待校准",
    "gray": "模板常规被改多 → 规范默认待核对",
    "yellow": "待补字段被填写 → 录音依赖/补录流程",
    "unknown": "字段未在模板中（新增/改标签）",
}

def do_edits(handler, u, qs):
    """修改回流聚合（F8）：医生修改留痕 → 字段热点 + 四色来源 + 改前改后样例，用于模型反馈。"""
    where, args = "", []
    if u.get("role") not in ("attending", "qc"):
        where = f"WHERE resident_id={Q}"
        args = [u["id"]]
    conn = auth._connect()
    try:
        cur = conn.cursor()
        cur.execute(f"SELECT * FROM archives {where} ORDER BY id DESC LIMIT 200", tuple(args))
        rows = auth._rows(cur)
    finally:
        conn.close()
    hot, by_color, recent, archives_with_edits, total_edits = {}, {}, [], 0, 0
    for r in rows:
        try:
            edits = json.loads(r["edits_json"] or "[]")
        except Exception:
            edits = []
        if not edits:
            continue
        archives_with_edits += 1
        total_edits += len(edits)
        try:
            fields = json.loads(r["fields_json"] or "[]")
        except Exception:
            fields = []
        binding_by_label = {str(f.get("label") or ""): (str(f.get("binding") or ""), f.get("value")) for f in fields if f.get("label")}
        for e in edits:
            label = str(e.get("field") or "（未命名字段）")
            binding, fval = binding_by_label.get(label, ("", None))
            color = field_color(label, binding, fval if fval is not None else e.get("after"))
            by_color[color] = by_color.get(color, 0) + 1
            h = hot.setdefault(label, {"label": label, "count": 0, "color": color, "samples": []})
            h["count"] += 1
            if len(h["samples"]) < 2:
                h["samples"].append({"before": str(e.get("before") or "")[:80], "after": str(e.get("after") or "")[:80],
                                     "record_no": r["record_no"], "doc_type": r["doc_type"],
                                     "at": str(e.get("time") or r["created_at"] or "")})
            recent.append({"record_no": r["record_no"], "doc_type": r["doc_type"], "field": label,
                           "before": str(e.get("before") or "")[:60], "after": str(e.get("after") or "")[:60],
                           "doctor": str(e.get("doctor") or ""), "at": str(e.get("time") or "")})
    hot_list = sorted(hot.values(), key=lambda x: -x["count"])[:12]
    recent = sorted(recent, key=lambda x: x["at"] or "", reverse=True)[:10]
    handler._json(200, {
        "ok": True, "role": u.get("role") or "resident",
        "totals": {"archives_with_edits": archives_with_edits, "edits": total_edits, "fields_touched": len(hot)},
        "by_color": by_color, "color_note": _COLOR_NOTE,
        "hot": hot_list, "recent": recent,
    })


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
        ("GET", "/edits"): lambda: do_edits(handler, u, _qs(handler.path)),
        ("GET", "/audit"): lambda: do_audit(handler, u, _qs(handler.path)),
        ("GET", "/export"): lambda: do_export(handler, u, _qs(handler.path)),
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
    auth.audit_log(u, "archive_submit", rec_no,
                   f"{rec['doc']}·{rec['disease'] or '—'}·字段 {rec['total']}（黄 {rec['yellow']}）")
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
        if rows:
            cur.execute(f"SELECT ts, username, name, role, action, detail FROM audit_log "
                        f"WHERE target={Q} ORDER BY id DESC LIMIT 20", (rows[0]["record_no"],))
            audit_rows = auth._rows(cur)
        else:
            audit_rows = []
    finally:
        conn.close()
    if not rows:
        return handler._json(404, {"error": "文书不存在"})
    r = rows[0]
    if u.get("role") not in ("attending", "qc") and r["resident_id"] != u["id"]:
        return handler._json(403, {"error": "无权查看他人文书"})
    d = _row_brief(r)
    for a in audit_rows:
        a["action_name"] = AUDIT_ACTION_NAMES.get(a["action"], a["action"])
    d["audit"] = audit_rows
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


def do_export(handler, u, qs):
    """归档包导出（P3 HIS 回写对接前置）：完整 JSON 包（元数据+四色字段+QC+修改留痕+XML 回填），
    以附件下载。权限同详情：attending/qc 全量，resident 仅本人。调用写审计。"""
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
        return handler._json(403, {"error": "无权导出他人文书"})
    try:
        fields = json.loads(r["fields_json"])
    except Exception:
        fields = []
    try:
        qc = json.loads(r["qc_json"] or "{}")
    except Exception:
        qc = {}
    try:
        edits = json.loads(r["edits_json"] or "[]")
    except Exception:
        edits = []
    package = {
        "format": "mra-archive-v1",   # HIS 对接层按此版本号解析
        "record_no": r["record_no"], "patient_code": r["patient_code"],
        "admission_idx": r["admission_idx"], "doc_type": r["doc_type"],
        "disease": r["disease"] or "", "resident_name": r["resident_name"],
        "status": r["status"], "created_at": str(r["created_at"] or ""),
        "signed_name": r["signed_name"] or "", "signed_at": str(r["signed_at"] or ""),
        "sign_comment": r["sign_comment"] or "",
        "spot_result": r["spot_result"] or "", "spot_name": r["spot_name"] or "",
        "spot_comment": r["spot_comment"] or "",
        "total_cnt": r["total_cnt"], "yellow_cnt": r["yellow_cnt"],
        "fields": fields, "qc": qc, "edits": edits,
        "xml": r["xml_text"] or "",
    }
    payload = json.dumps(package, ensure_ascii=False, indent=2).encode("utf-8")
    auth.audit_log(u, "export_archive", r["record_no"], f"{r['doc_type']}·归档包导出（HIS 对接前置）")
    handler.send_response(200)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Disposition", f'attachment; filename="{r["record_no"]}.json"')
    handler.send_header("Content-Length", str(len(payload)))
    handler.end_headers()
    handler.wfile.write(payload)


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
        cur.execute(f"SELECT status, record_no FROM archives WHERE id={Q}", (rid,))
        rows = auth._rows(cur)
        if not rows:
            return handler._json(404, {"error": "文书不存在"})
        if rows[0]["status"] != "submitted":
            return handler._json(409, {"error": f"该文书当前状态为「{STATUS_NAMES.get(rows[0]['status'])}」，不可重复审签"})
        rec_no = rows[0]["record_no"]
        name = (u.get("name") or u["username"]) + "（上级医师审签）"
        cur.execute(f"UPDATE archives SET status={Q}, signed_name={Q}, signed_at={Q}, sign_comment={Q} WHERE id={Q}",
                    (status, name, _now(), comment, rid))
    finally:
        conn.close()
    auth.audit_log(u, "review_sign" if action == "sign" else "review_reject", rec_no,
                   comment[:200] or ("签发" if action == "sign" else "退回（未填原因）"))
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
        cur.execute(f"SELECT record_no FROM archives WHERE id={Q}", (rid,))
        rows = auth._rows(cur)
        if not rows:
            return handler._json(404, {"error": "文书不存在"})
        rec_no = rows[0]["record_no"]
        name = (u.get("name") or u["username"]) + "（质控科抽查）"
        cur.execute(f"UPDATE archives SET spot_result={Q}, spot_comment={Q}, spot_name={Q}, spot_at={Q} WHERE id={Q}",
                    (result, comment, name, _now(), rid))
    finally:
        conn.close()
    auth.audit_log(u, "spotcheck", rec_no, f"{SPOT_NAMES[result]}{('·' + comment[:160]) if comment else ''}")
    handler._json(200, {"ok": True, "id": rid, "spot_result": result, "spot_name_str": SPOT_NAMES[result]})


def do_audit(handler, u, qs):
    """审计日志查询（P2-K）：仅质控科。支持 action 筛选与 limit（默认 100，上限 500）。"""
    if u.get("role") != "qc":
        return handler._json(403, {"error": "审计日志仅质控科可查（医务合规职能）"})
    try:
        limit = min(max(int(qs.get("limit") or 100), 1), 500)
    except ValueError:
        limit = 100
    action = (qs.get("action") or "").strip()[:32]
    where, args = "", []
    if action:
        where = f"WHERE action={Q}"
        args.append(action)
    conn = auth._connect()
    try:
        cur = conn.cursor()
        cur.execute(f"SELECT * FROM audit_log {where} ORDER BY id DESC LIMIT {limit}", tuple(args))
        rows = auth._rows(cur)
    finally:
        conn.close()
    for r in rows:
        r["action_name"] = AUDIT_ACTION_NAMES.get(r["action"], r["action"])
    handler._json(200, {"ok": True, "records": rows, "action_names": AUDIT_ACTION_NAMES})


try:
    init_db()
except Exception as e:
    print(f"[records] 初始化失败（审签功能降级不可用）: {e}", file=sys.stderr)
