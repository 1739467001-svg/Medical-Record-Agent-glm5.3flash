# -*- coding: utf-8 -*-
"""编排器（PRD 4.3.2）：调度六智能体按 DAG 执行，产出草稿 + 全程 trace。

DAG：extractor ∥ aggregator ∥ retriever（并行）→ writer → qc → mapper
回调：on_step(agent, status, trace|null, partial) 供服务端流式推送（NDJSON）。
"""
import time
from . import extractor, aggregator, retriever, writer, qc, mapper
from .common import trace_step

def run_pipeline(transcript=None, his_views=None, patients_admission=None,
                 corpus=None, doc_type="入院记录", target_labels=None, on_step=None):
    """参数四选二（his_views 评测直连 / patients_admission 服务端 Demo）。
    返回 {fields, trace[], stats}。异常逐智能体降级，永不整体崩（质控兜底哲学）。"""
    t_start = time.time()
    trace = []

    def report(agent, status, tr=None, partial=None):
        if on_step:
            try: on_step(agent, status, tr, partial)
            except Exception: pass

    # ---- 并行层（串行实现等价：三者互不依赖，代码耗时 << LLM） ----
    report("extractor", "run")
    elements, tr_e = extractor.extract(transcript, doc_type)
    trace.append(tr_e); report("extractor", "done", tr_e,
                               partial={"elements": len(elements)})

    report("aggregator", "run")
    t0 = time.time()
    if his_views is not None:
        dataset = aggregator.from_his_views(his_views)
    elif patients_admission is not None:
        dataset = aggregator.from_patients_json(patients_admission)
    else:
        dataset = aggregator.HisDataset(vitals={}, exams=[], abnormal_labs=[], orders=[], clinic_diags=[], anchor_dt=None)
    tr_a = trace_step("aggregator", (time.time() - t0) * 1000,
                      f"汇聚 {len(dataset['exams'])} 检查 / {len(dataset['abnormal_labs'])} 异常检验 / "
                      f"{len(dataset['orders'])} 医嘱 / {len(dataset['vitals'])} 体征 / 锚点 {dataset['anchor_dt'] or '—'}")
    trace.append(tr_a); report("aggregator", "done", tr_a)

    report("retriever", "run")
    t0 = time.time()
    docs_corpus = corpus or []
    hits = retriever.retrieve("；".join(dataset["clinic_diags"]) or doc_type, docs_corpus)
    knowledge = retriever.knowledge_digest(dataset["clinic_diags"], doc_type)
    tr_r = trace_step("retriever", (time.time() - t0) * 1000,
                      f"语料 {len(docs_corpus)} 条 → 命中同类写法 {len(hits)} 片段 + 规范要点 {len(knowledge)} 条"
                      + ("（语料为空：仅规范要点）" if not docs_corpus else ""))
    trace.append(tr_r); report("retriever", "done", tr_r)

    # ---- 串行层 ----
    report("writer", "run")
    draft, tr_w = writer.generate(elements, dataset, knowledge, doc_type, target_labels)
    trace.append(tr_w); report("writer", "done", tr_w, partial={"fields": len(draft)})

    report("qc", "run")
    passed, tr_q = qc.qc(draft, dataset)
    trace.append(tr_q); report("qc", "done", tr_q)

    report("mapper", "run")
    mapped, tr_m = mapper.map_fields(passed, target_labels)
    trace.append(tr_m); report("mapper", "done", tr_m, partial={"fields": len(mapped)})

    qc_report = next((t.get("report") for t in trace if t["agent"] == "qc" and t.get("report")), None)
    stats = {"total_ms": round((time.time() - t_start) * 1000),
             "elements": len(elements), "draft_fields": len(draft),
             "passed_fields": len(mapped),
             "by_source": {s: sum(1 for f in mapped if f["source"] == s) for s in ("blue", "green", "gray")},
             "degraded_agents": [t["agent"] for t in trace if t["status"] in ("degraded", "skipped")],
             "qc": qc_report}
    return {"fields": mapped, "trace": trace, "stats": stats}
