# -*- coding: utf-8 -*-
"""数据汇聚智能体（纯代码，PRD 4.3.1）：HIS 原始视图 → 标准化 HisDataset。
双源适配：
  A. from_his_views(views_rows, anchor) —— 评测/院内直连 HIS（xls 行字典，结构同 evaluate_v0.load_his_views）
  B. from_patients_json(admission)     —— 服务端 Demo（patients.json 中已聚合的 his 节）
职责锚点：VISIT 过滤、Excel 日期归一、参考范围伴随、异常标记——即 PRD DQ1~DQ3 的清洗落点。
"""
import datetime
from .common import HisDataset

def _excel_date(serial):
    try:
        return datetime.datetime(1899, 12, 30) + datetime.timedelta(days=float(serial))
    except Exception:
        return None

def _fmt(dt):
    return dt.strftime("%Y-%m-%d") if dt else ""

def from_his_views(views, anchor_word="入院"):
    """views: {检验申请主表:[row], 检查申请主表:[row], 检查申请项目:[row], 检查结果:[row], 生命体征:[row]}"""
    ds = HisDataset(vitals={}, exams=[], abnormal_labs=[], orders=[], clinic_diags=[], anchor_dt=None)
    vital_rows = views.get("生命体征", [])
    # 锚点：生命体征"入院/出院"事件行
    anchor_dt = None
    for v in vital_rows:
        if str(v.get("VITAL_SIGNS")).strip() == anchor_word:
            anchor_dt = _excel_date(v.get("TIME_POINT"))
            if anchor_dt: break
    ds["anchor_dt"] = _fmt(anchor_dt) if anchor_dt else None
    # 体征：锚点 ±24h 内取最近
    pick = {}
    for v in vital_rows:
        d = _excel_date(v.get("TIME_POINT"))
        name, val = str(v.get("VITAL_SIGNS")), str(v.get("VITAL_SIGNS_CVALUES")).strip()
        if not val or name in ("入院", "出院", "手术", "转入"): continue
        if anchor_dt and d:
            if abs((d - anchor_dt).total_seconds()) > 86400: continue
            dist = abs((d - anchor_dt).total_seconds())
        else:
            dist = 9e18
        if name not in pick or dist < pick[name][0]:
            pick[name] = (dist, val)
    ds["vitals"] = {k: v[1] for k, v in pick.items()}
    # 检查：申请 × 项目 × 结果印象 联接
    item_by_no = {}
    for it in views.get("检查申请项目", []):
        item_by_no.setdefault(str(it.get("EXAM_NO")), str(it.get("EXAM_ITEM")))
    seen = set()
    for req in views.get("检查申请主表", []):
        no = str(req.get("EXAM_NO"))
        imp = next((str(r.get("IMPRESSION")).strip() for r in views.get("检查结果", [])
                    if str(r.get("EXAM_NO")) == no and str(r.get("IMPRESSION")).strip()), "")
        if not imp or no in seen: continue
        seen.add(no)
        ds["exams"].append({"date": _fmt(_excel_date(req.get("EXAM_DATE_TIME"))),
                            "item": item_by_no.get(no, str(req.get("EXAM_SUB_CLASS"))), "impression": imp[:160]})
    # 异常检验（含参考范围）
    for r in views.get("检验结果", []):
        if str(r.get("ABNORMAL_INDICATOR")).strip():
            ds["abnormal_labs"].append({"item": str(r.get("REPORT_ITEM_NAME")), "result": str(r.get("RESULT")),
                                        "units": str(r.get("UNITS") or ""), "ref": str(r.get("PRINT_CONTEXT"))[:24]})
    # 医嘱（药类/处置类，节选）
    for o in views.get("医嘱", []):
        txt = str(o.get("ORDER_TEXT")).strip()
        if txt:
            ds["orders"].append({"text": txt,
                                 "dose": (str(o.get("DOSAGE")) + str(o.get("DOSAGE_UNITS") or "")).rstrip(".0"),
                                 "route": str(o.get("ADMINISTRATION") or "")})
    # 申请单临床诊断（去重去前缀）
    for req in views.get("检查申请主表", []) + views.get("检验申请主表", []):
        for key in ("CLIN_DIAG", "RELEVANT_CLINIC_DIAG"):
            d = str(req.get(key, "") if key in req else "").strip()
            if d and d not in ds["clinic_diags"] and not d.startswith("常规"):
                ds["clinic_diags"].append(d)
    ds["orders"] = ds["orders"][:40]
    ds["abnormal_labs"] = ds["abnormal_labs"][:24]
    return ds

def from_patients_json(admission):
    """服务端适配：patients.json 的 his 节 → 同构 HisDataset。"""
    his = admission.get("his") or {}
    ds = HisDataset(vitals={}, exams=[], abnormal_labs=[], orders=[], clinic_diags=[], anchor_dt=None)
    for v in his.get("vitals") or []:
        ds["vitals"][v.get("name", "")] = str(v.get("last", ""))
    ds["exams"] = [{"date": "", "item": "检查", "impression": e.get("impression", "")[:160]}
                   for e in his.get("exams") or []]
    ds["abnormal_labs"] = [{"item": l.get("item", ""), "result": l.get("result", ""),
                            "units": l.get("units", ""), "ref": l.get("ref", "")}
                           for l in his.get("abnormal_labs") or []]
    ds["orders"] = [{"text": o.get("text", ""), "dose": "", "route": ""} for o in his.get("orders") or []]
    return ds
