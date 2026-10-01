# evaluation/ · 评测与回归说明

## 目录结构
- `evaluate_v0.py`：金标准反推评测框架（PRD 7.1；`--generator rules|llm|multiagent`，`--types` 三类文书，`--out` 指定报告路径）
- `run_regression.py`：**一键双周回归**（三类文书评测 → reports/ 落盘 md+json → 与上期按文书类型 diff → 结论写回报告；`--no-run` 只 diff）
- `reports/`：回归报告存档（md + json 成对；json 供下期 diff）
- `asr_testset/`：ASR 方言测试集标注脚手架（含标注规范）
- 根目录 `评测报告_*.md`：**历史基线**（规则 v0 / 单 LLM / 多智能体模式 B 入院 / 模式 A 首程），保留作为对比锚点，日常回归产物一律进 `reports/`

## 当前基线（2026-10-01）
三类文书 36 份 × DeepSeek：覆盖率 入院 15.4% / 出院 11.3% / 首程 11.5%；录音依赖度 54.1% / 64.8% / 66.8%。
详见 `reports/评测报告_multiagent_2026-10-01.md` 与 `docs/P2_阶段交付总结_2026-10.md`。

## 用法
```bash
python3 run_regression.py                    # 需 .llm_env（LLM 密钥）
python3 run_regression.py --no-run           # 只与上期 diff
python3 evaluate_v0.py --generator rules     # 快速规则基线（无 LLM）
```
