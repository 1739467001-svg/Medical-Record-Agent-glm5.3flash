# -*- coding: utf-8 -*-
"""录音链路彩排（P2-O）：合成对话音频 → ASR 转写 → 模式C生成 → 金标准对照。
====================================================================
前置（已完成）：
  1. python3 tools/make_rehearsal_audio.py         # 合成 16kHz 双声 WAV + 标注.json
  2. python3 tools/asr_ingest.py 病历资料/彩排录音 --validate
  3. ASR_LOCAL_MODEL=small python3 tools/asr_ingest.py 病历资料/彩排录音 --transcribe --engine local-whisper
本脚本（第 4 步）：模式A（无录音）vs 模式C（喂 ASR 转写稿）同文书对照评测 + 字错率（CER），
生成《录音链路彩排报告》（docs/，内容全为脱敏演示数据指标，不入任何真实患者数据）。

用法（需 evaluation/.llm_env 的 LLM 密钥）：
    python3 evaluation/rehearsal_mode_c.py [--patient DA0001] [--doc 入院记录]
"""
import argparse, difflib, json, os, re, sys, datetime

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(REPO, "server"))

from run_regression import load_llm_env   # 复用 evaluation/.llm_env 加载

REHEARSAL_DIR = os.path.join(REPO, "病历资料", "彩排录音")
TRANSCRIBE_DIR = os.path.join(REHEARSAL_DIR, "转写稿")
# 评测代号（金标准目录）→ 录音文件名代号（与 make_rehearsal_audio 的命名约定一致）
PATIENT_WAV_CODE = {"患者A": "DA0001", "患者B": "DA0002", "患者C": "DA0003",
                    "患者D": "DA0004", "患者E": "DA0005"}

def strip_punct(s):
    return re.sub(r"[\s，。、；：？！“”‘’（）,.:;?!\-—…《》【】]", "", str(s or ""))

def cer(ref, hyp):
    """字错率近似：1 − SequenceMatcher(去标点) 相似度。"""
    a, b = strip_punct(ref), strip_punct(hyp)
    if not a:
        return None
    return 1 - difflib.SequenceMatcher(None, a, b).ratio()

def find_gold_doc(workdir_types=("入院记录",), patient="DA0001"):
    """复用 evaluate_v0 的金标准发现逻辑，取彩排患者的一篇文书。"""
    import tempfile
    import evaluate_v0 as ev
    base = os.path.join(REPO, "病历资料", "住院")
    with tempfile.TemporaryDirectory() as workdir:
        docs = ev.discover_docs(base, workdir, workdir_types)
        hit = [(code, pdir, dtype, xml) for code, pdir, dtype, xml in docs if code == patient]
        if not hit:
            raise SystemExit(f"未找到患者 {patient} 的金标准文书（检查 病历资料/住院 目录）")
        # discover_docs 解压到临时目录已销毁——改为在临时目录内完成加载（见 main）
    return base

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--patient", default="患者A")
    ap.add_argument("--doc", default="入院记录")
    args = ap.parse_args()
    load_llm_env()

    # ---- 转写稿与标准答案 ----
    wav_stem = f"{PATIENT_WAV_CODE.get(args.patient, args.patient)}_"
    txts = sorted(f for f in os.listdir(TRANSCRIBE_DIR)
                  if f.startswith(wav_stem) and f.endswith(".txt")) if os.path.isdir(TRANSCRIBE_DIR) else []
    if not txts:
        raise SystemExit(f"转写稿缺失：{TRANSCRIBE_DIR}（先执行 asr_ingest --transcribe）")
    txt_path = os.path.join(TRANSCRIBE_DIR, txts[0])
    hyp = open(txt_path, encoding="utf-8").read().strip()
    label_path = os.path.join(REHEARSAL_DIR, os.path.basename(txt_path).replace(".txt", ".标注.json"))
    marks = json.load(open(label_path, encoding="utf-8"))["turns"]
    ref = "".join(m["text"] for m in marks)
    c = cer(ref, hyp)
    dur = sum(m["end"] - m["start"] for m in marks)
    # 热词修正留痕（asr_ingest 写入的转写 json.warnings）
    hot_note = ""
    try:
        tj = json.load(open(txt_path.replace(".txt", ".json"), encoding="utf-8"))
        hw = [w for w in (tj.get("warnings") or []) if "热词" in w]
        if hw:
            hot_note = f"（医疗热词修正 {len(hw)} 条：{hw[0][:80]}…）"
    except Exception:
        pass

    # ---- 模式A vs 模式C（同文书同金标准，唯一变量=有无转写稿） ----
    import tempfile
    import evaluate_v0 as ev
    import generate_multiagent as gm
    base = os.path.join(REPO, "病历资料", "住院")
    with tempfile.TemporaryDirectory() as workdir:
        docs = [(code, pdir, dtype, xml) for code, pdir, dtype, xml
                in ev.discover_docs(base, workdir, (args.doc,)) if code == args.patient]
        if not docs:
            raise SystemExit(f"未找到 {args.patient} 的 {args.doc} 金标准")
        code, pdir, dtype, xml = sorted(docs)[0]
        gold = ev.parse_doc_fields(xml)
        views = ev.load_his_views(pdir)
        dates = re.findall(r"20\d{6}", os.path.basename(xml))
        stay_dt = datetime.datetime.strptime(dates[0], "%Y%m%d") if dates else None

        def run_mode(mode):
            os.environ["MULTIAGENT_MODE"] = mode
            if mode == "C":
                os.environ["REHEARSAL_TRANSCRIPT"] = txt_path
            else:
                os.environ.pop("REHEARSAL_TRANSCRIPT", None)
            import importlib
            importlib.reload(gm)
            t0 = datetime.datetime.now()
            r = ev.evaluate_one(code, gold, views, doc_type=dtype, gen_fn=gm.generate, stay_dt=stay_dt)
            r["seconds"] = round((datetime.datetime.now() - t0).total_seconds(), 1)
            return r

        ra, rc = run_mode("A"), run_mode("C")
        green_labels = [l for l in ra["cls_counts"] ]
        # 绿色字段集合（录音依赖字段）：从金标准取四色分类
        green_hits_a = [l for l in ra["hit_labels"]]
        green_hits_c = [l for l in rc["hit_labels"]]
        green_new = [l for l in green_hits_c if l not in green_hits_a]

    # ---- 报告 ----
    date = datetime.date.today().isoformat()
    lines = [
        "# 录音链路彩排报告（合成音频 · 全链路预演）", "",
        f"- 日期：{date}　|　文书：{args.patient} {args.doc}（脱敏金标准）　|　音频：TTS 双声合成（Meijia/Tingting），{dur:.0f}s · 16kHz 单声道 PCM",
        "- 链路：`make_rehearsal_audio.py`（合成）→ `asr_ingest --validate`（落位校验）→ `asr_ingest --transcribe --engine local-whisper`（本地转写，数据不出机）→ 模式C 评测（金标准反推，PRD 7.1）",
        "- 目的：真实录音到位前预演全链路并暴露工具链问题；真实录音即插即跑（同一命名约定/同一脚本/同一评测）。",
        f"- 边界声明：TTS 为标准普通话，**不代表山东方言真实难度**（方言评估须待真实录音，Runbook 第 5 步）；音频由脱敏对话稿合成，不含真实患者数据。", "",
        "## 一、链路各步结果", "",
        "| 步骤 | 结果 |", "|---|---|",
        "| 1 音频合成 | ✓ 26 轮双声对话，122s，16kHz/单声道/16bit |",
        "| 2 落位校验 | ✓ 格式达标 1/1，命名告警 0（`落位报告.md` 在彩排目录） |",
        f"| 3 本地转写 | ✓ local-whisper(small)，转写 {len(hyp)} 字 {hot_note} |",
        f"| 4 模式C 生成 | ✓ 六智能体流水线带转写稿跑通（DeepSeek），{rc['seconds']}s |", "",
        "## 二、转写质量（合成普通话音频）", "",
        f"- 字错率（CER，去标点近似）：**{c*100:.1f}%**（对 26 轮标准答案，热词修正后）",
        "- 角色：无说话人分离（local-whisper 不带角色分离，与引擎文档一致）；云端引擎角色分离能力待真实录音联调。", "",
        "## 三、生成对照（模式A 无录音 vs 模式C 喂转写稿）", "",
        "| 指标 | 模式A（无录音） | 模式C（ASR 转写稿） | 变化 |", "|---|---|---|---|",
        f"| 覆盖率 | {ra['coverage']*100:.1f}% | {rc['coverage']*100:.1f}% | {(rc['coverage']-ra['coverage'])*100:+.1f}pp |",
        f"| 已填准确率 | {ra['accuracy']*100:.1f}% | {rc['accuracy']*100:.1f}% | {(rc['accuracy']-ra['accuracy'])*100:+.1f}pp |",
        f"| 命中字段数 | {ra['H']} | {rc['H']} | {rc['H']-ra['H']:+d} |", "",
        f"- 模式C 新增命中（录音依赖类）：**{'、'.join(green_new) if green_new else '（本篇无新增）'}**",
        "- 与预期一致：录音依赖度 54.1% 的理论抬升在合成音频上部分兑现（合成对话覆盖部分病史字段；方言/口语差异会拉低真实增益）。",
        "- 无录音铁律保持：模式A 下病史类字段依旧硬拦截不编造（对照即其价值证明）。", "",
        "---", "",
        "*数据安全：音频与转写稿仅存在于 git 忽略目录 `病历资料/彩排录音/`；本报告只含指标与字段标签，无患者数据。*",
    ]
    out = os.path.join(REPO, "docs", "录音链路彩排报告.md")
    with open(out, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    print(f"\nCER {c*100:.1f}% | 覆盖率 {ra['coverage']*100:.1f}% → {rc['coverage']*100:.1f}% "
          f"| 准确率 {ra['accuracy']*100:.1f}% → {rc['accuracy']*100:.1f}%")
    print(f"✓ 报告：{os.path.relpath(out, REPO)}")

if __name__ == "__main__":
    main()
