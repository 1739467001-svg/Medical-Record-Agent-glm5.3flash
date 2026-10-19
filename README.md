# AI 病历智能体系统（东阿县人民医院 · 高级定制版）

以"医生口述 / 医患对话录音"为入口，依托方言语音识别与多智能体协作，自动生成符合医院电子病历模板（XTextDocument/XML）规范的住院病历草稿，经医生四色确认后归档至院内系统。

> 数据安全声明：本仓库**不含任何真实患者数据**。院方提供的原始病历、录音与内部会议材料已通过 `.gitignore` 永久排除；仓库内所有演示/评测数据均为脱敏再构（患者→患者A/B/C/D/E，医护→姓+医生/护士，电话/住址/院内ID→掩码），并以自动化断言校验无泄漏。责任约定：AI 仅生成草稿，医生确认后方可归档，责任主体始终是医生。

## 仓库结构

| 目录/文件 | 内容 | 状态 |
|---|---|---|
| **`docs/开发日志.md`** | **开发日志（进度沉淀主文档）**：逐日记录每次修改的时间/commit/内容，每次改动按"更新日志→落盘→推送 GitHub"三步走；汇报与仓库对账的单一事实源 | 持续更新 |
| `PRD_AI病历智能体系统_v1.0.md` | 项目开发需求文档（总指南）：需求对齐、数据盘点、功能需求、评测指标、里程碑、风险、待确认问题清单 | v1.0 |
| `AI病历智能体系统建设实施规划方案1.0.docx` | 对院方规划方案（合作洽谈稿，需求总纲） | v1.0 |
| `demo/` | 医生端网页工作台 Demo：工作台任务视角 → 问诊录音（真实引擎就绪即切真实模式）→ **真实六智能体流水线生成**（流式展示）→ 四色确认 → 真库归档 → **上级医师审签 / 质控科抽查**；含打印/存 PDF、台账 CSV 导出、脱敏演示数据与字段地图；**交互流程已平台化（数据驱动）**——患者F 为首例真实录音案例（真实方言问诊录音→本地转写→接入），新案例经传送带接入后前端零改动 | 可运行 |
| `server/` | **P1 多智能体核心**：六智能体 + orchestrator（DAG+trace）；LLM API（流式 NDJSON）；**认证服务 auth.py**（三角色，生产 MySQL / 研发 SQLite 降级，PBKDF2 200k 轮）+ **归档审签 records.py**（真库归档/审签状态机/质控抽查/修改回流聚合）+ **单元测试 54 项**（tests/） | 已上线 |
| `evaluation/` | 金标准反推评测框架 + 三类文书基线报告 + **一键回归自动化**（run_regression.py，md+json 双格式落盘、与上期 diff）；HIS 视图 **8 表全量加载**（医嘱/检验结果已接入生成端）；ASR 方言测试集标注脚手架 | 回归已自动化 |
| `mcp-server/` | ASR MCP 服务器：统一语音识别接口，讯飞/阿里云/商汤/**本地自建（FunASR / faster-whisper）**可插拔引擎；**录音页真实链路已打通**（/asr/status 探测 + /asr/transcribe 转写，密钥后补即用，演示回放保留为降级）；**自建引擎落地**：FunASR（SenseVoice/SeACo 热词偏置）零外呼可跑，分段策略经首例真实方言录音实测修正（VAD+超长段能量切分）；热词变体族持续生长 | 自建引擎可用，等云端密钥 |
| `tools/` | 运维与接入工具：**case_ingest.py 真实案例接入传送带**（HIS 8 表 + 录音转写稿 → 脱敏演示数据，红线扫描强制）、asr_ingest.py 录音落位/批量转写、**asr_corpus.py 方言 ASR 语料库**（引擎对比/标注草稿/门槛进度，数据飞轮）、**asr_reconcile.py 双引擎交叉校订稿**（分歧点听辨清单，标注加速）、**hotword_harvest.py 热词表自动生长**（纠错对沉淀→候选清单→审核合并）、**hotword_audit.py 热词规则全语料审计**（双向核查误伤/过度拦截，发版关卡）、**asr_denoise_ab.py 语音增强前端实验台**（门诊噪声场景 A/B）、**asr_annotate.py 分歧点听辨工作台**（拆片/仲裁/成稿，标注加速）、check_repo.py 一键仓库体检、make_rehearsal_audio.py 彩排音频合成 | 可用 |
| `deploy/` | 部署编排：云服务器版（docker-compose.server.yml/update/backup）+ **私有化交付套件 `deploy/offline/`**（make_delivery.sh 一键打包、install.sh 院内一键安装、verify.sh 验收自检）；交付文档见 `docs/私有化交付/`（01 部署说明书 / 02 验收报告模板 / **03 交付形态说明**——面向院方的交付形态与边界说明） | 可用 |

## 快速开始

```bash
# 医生端 Demo（零依赖静态站）
cd demo && python3 -m http.server 8765
# 打开 http://127.0.0.1:8765 —— 患者A「开始入院问诊」体验全流程（未登录=演示模式）

# 完整体验（登录/注册 + AI 服务 + 新手引导；本地无 MySQL 自动降级 SQLite）
MRA_STATIC_DIR="$PWD/demo" python3 server/llm_api.py
# 打开 http://127.0.0.1:8090 —— 注册医生账号 → 首次登录自动播放五步新手引导

# 评测基线（需院方原始数据包，本地运行、不入库）
python3 evaluation/evaluate_v0.py

# 一键双周回归（PRD 12.2-6）：三类文书 × 多智能体真实评测 + 与上期报告 diff，
# 报告落盘 evaluation/reports/（md+json），diff 结论写回报告（需 .llm_env 中的 LLM 密钥）
python3 evaluation/run_regression.py

# ASR MCP 服务器自测（mock 引擎，无需密钥）
python3 mcp-server/test_client.py

# 后端单元测试（零依赖 unittest，100 项：规范用语改写/无素材拦截/QC 回流/
# 审签状态机/角色权限/修改回流聚合/老库迁移/登录防爆破/审计留痕/归档包导出/医疗热词/WAV直读/
# 方言变体族热词/热词白名单防线/热词规则审计双向判据/语音增强A-B口径/标注工作台坐标映射/
# 领域提示边界/
# 案例接入传送带/时间穿越截断/XTextDocument 模板回填；临时 SQLite，不触碰研发数据）
cd server && python3 -m unittest discover -s tests

# API 冒烟测试（HTTP 层端到端，22 项：起真实服务进程 → 路由/前缀剥离/会话/
# 审签流/归档包导出/角色越权/限流/LLM 未配置降级/审计查询；临时库隔离）
python3 server/tests/api_smoke.py

# 一键仓库体检（P2-L，14 项：Python 编译/JS 语法/前后端 API 契约/onclick 引用/
# 红线文件不入 Git/疑似密钥扫描/患者真名零入库/README 数字与实测一致/运行环境；--fast 跳过实测）
python3 tools/check_repo.py

# 录音数据接入（P2-M，录音 Runbook 第 1-3 步自动化；仅限 git 忽略目录，真实录音/转写稿永不入库）
python3 tools/asr_ingest.py 病历资料/录音 --validate      # 落位校验：格式/命名 → 落位报告.md（无需密钥）
python3 tools/asr_ingest.py 病历资料/录音 --transcribe    # 批量转写（需 .llm_env 中 ASR_* 密钥）

# 自建 ASR 语料库（数据飞轮第②阶段：引擎对比 + 标注草稿 + 门槛进度；详见 docs/自建ASR选型与实施路径.md）
python3 tools/asr_corpus.py --add 病历资料/住院/<姓名> --whisper-model small --llm-polish  # 有参照稿→完整评测并归集
python3 tools/asr_corpus.py --add 病历资料/门诊/1.m4a --llm-polish                        # 无参照稿→落引擎草稿待标注
python3 tools/asr_corpus.py --stats   # 累计：案例数/音频小时数/各引擎均值/复选 5 例与微调 50h 门槛进度
python3 tools/asr_reconcile.py 病历资料/门诊/1.m4a --whisper-model small   # 双引擎交叉校订稿：只列分歧点，标注从"逐字听写"变"分歧仲裁"
python3 tools/hotword_harvest.py --scan 病历资料   # 热词表自动生长：纠错对沉淀→候选清单（审核后 --merge 合并）
python3 tools/hotword_audit.py --suggest --fail-on-risk  # 热词规则全语料审计：双向核查误伤/过度拦截（发版关卡）

# 语音增强前端实验台（门诊噪声场景：原始 vs ffmpeg 降噪 同引擎 A/B，含多档预设）
python3 tools/asr_denoise_ab.py 病历资料/门诊/1.m4a --engine funasr --preset standard

# 分歧点听辨工作台（标注瓶颈最后一公里：拆片 → 仲裁 → 成稿）
python3 tools/asr_annotate.py --prepare 病历资料/ASR语料库/校订稿/<案例>.md   # 生成工作台 + 音频片段
python3 tools/asr_annotate.py --status                                       # 各案例裁决进度
python3 tools/asr_annotate.py --compile 病历资料/ASR语料库/工作台/<案例>      # 据裁决拼出参照稿

# 评测趋势汇总（P2-M）：历次回归报告 → 覆盖率/准确率跨期对照表（双周汇报用）
python3 evaluation/trend_report.py

# 录音链路彩排（P2-O，无真实录音预演全链路；音频/转写稿只落 git 忽略目录）
python3 tools/make_rehearsal_audio.py                       # ① 合成双声问诊音频（macOS，脱敏对话稿，声部自动探测）
ASR_LOCAL_MODEL=small python3 tools/asr_ingest.py 病历资料/彩排录音 --transcribe --engine local-whisper  # ② 本地转写（数据不出机）
python3 evaluation/rehearsal_mode_c.py                      # ③ 模式A/C 对照评测 + 字错率 → docs/录音链路彩排报告.md

# 真实案例接入传送带（首例患者F 已跑通；第 2、3 个案例同此六步，详见 docs/真实案例接入报告_患者F.md 第七节）
# ① 案例目录（git 忽略）：病历资料/住院/<姓名>/ 放 8 张 HIS 表 xls；录音放 录音/ 子目录（mp3 先转 16k 单声道 WAV）
# ② 患者代号映射.json 增加 "<姓名>": "患者X"（不入 Git）
# ③ 落位校验 + 本地转写（建议 ASR_LOCAL_PROMPT 带上申请单诊断，实测最优）
ASR_LOCAL_MODEL=small python3 tools/asr_ingest.py "病历资料/住院/<姓名>/录音" --transcribe --engine local-whisper
# ④ 接入（自动探测入院/出院/科室/诊断；LLM 角色标注；红线扫描 0 命中才落盘 demo/data）
python3 tools/case_ingest.py "病历资料/住院/<姓名>" --code DA00NN --dry-run   # 先核对，再去掉 --dry-run
# ⑤ Markdown 问诊过程稿（时间戳×角色×关联字段表；挂载 admission.transcript_md，前端可查看）
python3 tools/make_transcript_md.py "病历资料/住院/<姓名>" --code DA00NN
# ⑥ 平台验证：工作台任务卡 → 录音回放（真实转写）→ 过程稿(MD) → 生成 → 四色确认 → 归档

# 演示动线与降级预案（院方汇报用，docs/）
# docs/演示脚本_5分钟.md —— 预检清单 → 5 分钟动线 → 量化结论 → 降级预案 → Q&A

# 对院方推进三件套（P2-N：催收 D1 录音/D2 接口/D3 字段确认，docs/对院方推进/）
# 01_录音知情同意书模板_v0.1.md（含录音技术规范，待医务科审定）
# 02_HIS对接需求函_v0.1.md（两步走方案 + 资料清单，转信息科/EMR 厂商）
# 03_字段地图v0_确认表.xlsx（238 字段×四色预建议×院方勾选列；生成器 tools/gen_fieldmap_xlsx.py）

# 私有化交付包（三甲规格：Docker 内网部署 + B/S 访问，docs/私有化交付/01_说明书）
bash deploy/offline/make_delivery.sh v1.0              # 一键打包（联调用 --skip-images）
# 产物 dist/mra-delivery-v1.0.tar.gz：离线镜像 + install.sh 院内一键安装
# + verify.sh 验收自检 + 红线扫描（患者数据/密钥零进入）+ SHA256SUMS 全包校验
# 院内安装：sudo bash install.sh [--with-mysql]；验收：bash verify.sh --deployed
```

## 核心设计（详见 PRD）

- **四色确认**：蓝=系统带入 / 绿=对话提炼（可溯源到转写原文）/ 灰=规范所见 / 黄=缺失待补——严禁臆造，宁缺毋造。
- **规范用语改写（生成→质控→回流闭环）**：诊断字段由申请单诊断规范改写产出（去"？"/ICD 尾缀、剔除症状条目、保留病名原文），writer 生成端改写 + QC 质控端回流互为兜底；无录音时病史类字段（主诉/现病史等）硬拦截不编造。
- **六智能体协作**：信息抽取 / 数据汇聚 / 知识检索 / 病历生成 / 质控校验 / 字段映射，由编排器按文书类型调度，每个生成字段携带来源与置信度。
- **医生账号与新手引导**：登录/注册（工号/手机号 + PBKDF2 加盐口令，数据存 MySQL，会话 HttpOnly Cookie 7 天）；**系统角色**对应病历责任矩阵——住院医师（书写确认）/ 上级医师（审签：签发/退回）/ 质控科（抽查：合格/缺陷）；**首次登录自动播放五步聚光灯引导**（书写任务→患者→统计→帮助→账号），可随时从工作台「▶ 新手引导」重看；演示模式无需账号。
- **审签工作流**：四色确认提交后真库归档（归档编号 AR-YYYYMMDD-NNNN）→ 上级医师签发/退回（原因回流书写医生）→ 质控科抽查标记；审签工作台按角色呈现（住院医师=本人进度，上级医师=签发/退回，质控科=抽查+审计日志页签）。
- **合规留痕与安全（P2-K）**：登录/注册/草稿生成/提交归档/审签/抽查/录音转写全部只追加写入 audit_log（未登录的演示运行不入库；写失败不阻断业务）；质控科可在审签工作台「🛡 审计日志」按时间倒序查阅，每份文书详情附本文书留痕时间线；登录防爆破——15 分钟窗口内连续失败 5 次锁定 15 分钟。
- **修改回流数据闭环（F8）**：医生每次修改（改前/改后/字段/时间）随归档入库，聚合为字段热点 × 四色来源——绿热点指向抽取优化、蓝热点指向 HIS 映射校准、灰热点指向模板默认核对，随双周回归对照收敛。
- **导出**：病历「打印/存 PDF」（专用文书版式，浏览器原生存 PDF）；审签/质控台账 CSV（Excel 直开）；修改回流包 JSON（模型反馈）。
- **量化结论（无录音基线，2026-10-01 三类文书 36 份实测）**：录音依赖度 入院 54.1% / 出院 64.8% / 首程 66.8%——不补采录音，覆盖率无法达到验收阈值（PRD 7.2 ≥90%）。这是录音补采（PRD 风险 D1）的量化依据。
- **首例真实录音案例实测（患者F，2026-10-09）**：93.7s 真实方言术前问诊 + HIS 8 表 → 本地转写（热词修正 5 处）→ 传送带接入 → 六智能体生成。模式A（无录音）26 字段绿 0 vs 模式C（真实录音）32 字段**绿 6**（主诉/现病史/既往史/个人史/婚育史/家族史整体解锁，主诉"腹痛1天"与录音完全一致）；QC 对 LLM 过度推断的复核提示全部命中要害。录音依赖度理论在真实数据上首次兑现。详见 `docs/真实案例接入报告_患者F.md`。

## 阶段状态（P3 自研前置完成）

**阶段交付总结见 `docs/P3_阶段交付总结_2026-10.md`**（数据全齐后冲刺：时间穿越防线 / 8 表接入新基线 / 归档 XML 模板原位回填 / 患者F A/C 对照）与 `docs/P2_阶段交付总结_2026-10.md`（功能全景 / 部署 / Runbook）；**首例真实录音案例见 `docs/真实案例接入报告_患者F.md`**；**HIS 对接契约见 `docs/HIS数据接口说明_8表字典_v1.0.md`**。剩余 P3 正式项：院方写接口文档 + 云端 ASR 密钥 + 院内试点排期。
