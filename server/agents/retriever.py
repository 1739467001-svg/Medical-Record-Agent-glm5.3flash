# -*- coding: utf-8 -*-
"""知识检索智能体（v1 纯代码，PRD 4.3.1）：从本院金标准文书库检索同类病例写法片段与常规参考。
v1 诚实边界：本地语料检索（BM3 风格关键词匹配），不外呼；二期可换向量检索/医学知识库。
语料双源：corpus 参数（评测=workdir XML；服务端=patients.json 文书）。
"""
import re

STYLE_LABELS = ("主诉", "现病史", "初步诊断", "诊断依据", "鉴别诊断")

def _norm(s):
    return re.sub(r"\s+", "", str(s or ""))

def build_corpus_from_fields(docs):
    """docs: [{type,title,fields:[{label,value}]}] → 语料条目列表"""
    corpus = []
    for d in docs:
        for f in d.get("fields", []):
            v = str(f.get("value") or "").strip()
            if f.get("label") in STYLE_LABELS and 8 <= len(v) <= 400:
                corpus.append({"doc_type": d.get("type"), "title": d.get("title"),
                               "label": f["label"], "text": v})
    return corpus

def retrieve(query, corpus, top_k=6):
    """关键词命中率打分检索：query（诊断/症状词）→ 同类写法片段。"""
    keys = [k for k in re.split(r"[、，,\s；;1-9．.（）()]+", _norm(query)) if len(k) >= 2][:8]
    scored = []
    for i, c in enumerate(corpus):
        blob = _norm(c["title"] + c["text"])
        hit = sum(1 for k in keys if k in blob)
        style_bonus = 1 if c["label"] in ("主诉", "现病史") else 0
        if hit > 0:
            scored.append((hit * 2 + style_bonus, i))
    scored.sort(reverse=True)
    return [corpus[i] for _, i in scored[:top_k]]

def knowledge_digest(diags, doc_type):
    """常规知识要点（v1：文书类型常识框架，供 writer 组织结构）。"""
    if doc_type == "入院记录":
        return ["主诉=症状+持续时间；现病史按 发病情况→主要症状特点→伴随症状→诊治经过→一般情况 组织",
                "既往史含：一般健康、疾病史、手术外伤输血史、过敏史、预防接种史",
                "个人史/婚育史/家族史分节陈述；阴性症状用'无'规范表述"]
    if doc_type == "首次病程记录":
        return ["病例特点=人口学+主诉现病史要点+查体+辅查", "诊断依据逐条对应诊断",
                "鉴别诊断每条：病名+鉴别点+本例不符之处", "诊疗计划：护理级别+检查+治疗+观察"]
    if doc_type == "出院记录":
        return ["入院情况/诊疗经过/出院情况三段叙述", "出院诊断与入院诊断呼应", "出院医嘱分条：用药+复诊+随访"]
    return []
