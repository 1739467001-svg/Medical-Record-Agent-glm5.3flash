# HIS 数据接口说明（8 表数据字典 + 对接规范）

> 适用：院方信息科向本系统供数 / P3 对接期双方开发对照。版本随 8 表结构演进，字段清单由
> `tools/gen_fieldmap_xlsx.py`（字段地图）与本文共同构成"接口三件套"（另含 `02_HIS对接需求函`）。

## 一、数据供给现状（2026-10 实测）

院方按患者级导出 8 张 SQL 直查表（xls），患者目录样例结构：

```
病历资料/住院/<患者姓名>/
├── 医嘱.xls            # orders          按患者_id 导出全量
├── 检查申请主表.xls     # exam_master     申请单（含 CLIN_DIAG 临床诊断）
├── 检查申请项目.xls     # exam_items      申请项目明细（EXAM_NO 关联）
├── 检查结果.xls        # exam_report     报告所见/印象（DESCRIPTION/IMPRESSION）
├── 检验申请主表.xls     # lab_test_master 申请单（含 SPECIMEN 标本类型）
├── 检验申请项目.xls     # lab_test_items  项目明细（TEST_NO 关联）
├── 检验结果.xls        # lab_result      逐项结果（含 ABNORMAL_INDICATOR/参考范围）
├── 生命体征.xls        # vital_signs_rec  全程体征（含 入院/出院/手术 事件行）
└── <患者ID>_<姓名>(<导出时间>).zip    # 金标准 XML 文书（XTextDocument）
```

每张表第一个 sheet 为 `SQL Results`（数据行），第二个 sheet `SQL Statement` 留存导出查询语句（可追溯性：数据按什么条件导出）。

## 二、8 表数据字典（系统消费的核心字段）

### 1. orders（医嘱）
| 字段 | 用途 | 说明 |
|---|---|---|
| ORDER_TEXT | 生成素材 | 医嘱正文（药名/剂量/用法内嵌） |
| DOSAGE / DOSAGE_UNITS / ADMINISTRATION / FREQUENCY | 生成素材 | 剂量/单位/途径/频次 |
| START_DATE_TIME / STOP_DATE_TIME | 住院窗口过滤 + 文书锚点截断 | Excel 序列日期 |
| REPEAT_INDICATOR | 长期/临时区分 | 1=长期 |
| ORDERING_DEPT / DOCTOR / NURSE | 留痕参考 | 供数侧自行脱敏或保留（系统入库前扫描） |

### 2. exam_master（检查申请主表）
| 字段 | 用途 |
|---|---|
| EXAM_NO | 与 exam_items/exam_report 关联的主键 |
| CLIN_DIAG / RELEVANT_CLINIC_DIAG | **初步诊断规范改写的数据源**（P2-H 闭环） |
| EXAM_DATE_TIME / REQ_DATE_TIME | 报告时间排序 / 锚点截断 |
| NAME / SEX / DATE_OF_BIRTH | 登记信息回填（脱敏后） |

### 3. exam_items（检查申请项目）：EXAM_NO → EXAM_ITEM 检查项目名。
### 4. exam_report（检查结果）：EXAM_NO → DESCRIPTION（所见）、IMPRESSION（印象，直接进"辅助检查"素材）、IS_ABNORMAL。

### 5. lab_test_master（检验申请主表）：TEST_NO 主键；SPECIMEN 标本类型；AGE/SEX 随申请单时点（案例接入探测用最近行）。
### 6. lab_test_items（检验申请项目）：TEST_NO → ITEM_NAME 项目名。
### 7. lab_result（检验结果）
| 字段 | 用途 |
|---|---|
| REPORT_ITEM_NAME / RESULT / UNITS | **异常检验素材**（辅助检查字段） |
| ABNORMAL_INDICATOR | H/L 异常标记（空=正常不取） |
| PRINT_CONTEXT | 参考范围（随值展示） |
| RESULT_DATE_TIME | 锚点截断（术后复查不进术前文书） |

### 8. vital_signs_rec（生命体征）
| 字段 | 用途 |
|---|---|
| VITAL_SIGNS / VITAL_SIGNS_CVALUES / UNITS | 体征名/值/单位（入院锚点 ±24h 取最近） |
| TIME_POINT | 事件时刻；VITAL_SIGNS='入院/出院/手术' 行作锚点事件 |
| RECORDING_DATE | 日期序列（TIME_POINT 优先） |

## 三、时间语义（对接关键，实测沉淀）

1. **Excel 序列日期**：全表日期为 1899-12-30 基准的浮点序列数（含小数天=时刻），系统统一解析。
2. **患者级导出含历史住院**：表内可能混有该患者既往住院数据行（实测患者F 含 2022 年行）——对接时按 `PATIENT_ID + VISIT_ID` 或住院窗口过滤，本系统侧已按"文书锚点 ±3 天 + 锚点时刻截断（DOC_CUTOFF）"双重过滤。
3. **文书锚点时刻**：入院记录/首次病程记录写于入院当时，素材截断于锚点次日 0 点（术后病理/复查绝不进入术前文书——时间穿越防线，单测覆盖）；出院记录覆盖全程不截断。

## 四、归档回写包（系统 → 院方，两步走的第一步）

四色确认 + 上级医师审签后的归档包（`/records/export`，格式 `mra-archive-v1`）：

| 字段 | 内容 |
|---|---|
| record_no | 归档编号 AR-YYYYMMDD-NNNN |
| patient_code / admission_idx / doc_type / disease | 定位信息 |
| fields | 四色确认后的字段终值（label/value/binding/source/basis） |
| qc / edits | 质控报告 / 医生修改留痕（改前改后，F8 闭环） |
| **xml** | **完整 XTextDocument 文书**（P3 新增）：真实模板原位回填产物，可直接由院内编辑器打开；模板取自金标准 zip（同病种优先），旧患者值 0 残留（数据节点级断言） |

> 直写路径（第二步）：等院方/EMR 厂商提供写接口后，`xml` 字段即直写载荷；当前过渡期按
> "文件导入"落地。回填器实现：`server/template_backfill.py`（对位策略：Patient 绑定路径 >
> ToolTip/邻近标签 > 前缀匹配；防串患者三原则见模块头注释）。

## 五、录音数据接口（并行走廊）

命名约定：`患者编号_入院日8位_文书类型.wav`（16kHz/单声道/PCM）；mp3/m4a 原始格式先转
WAV（`ffmpeg -ac 1 -ar 16000`）。转写输出 `转写稿/<同名>.txt|json`（引擎/分段/热词修正留痕）。
真实录音与转写稿**永不入 Git**（gitignore 红线 + asr_ingest 启动强制校验）。

## 六、给信息科的导出要求清单（复演示排期用）

1. 8 表按患者级导出（保持现有 SQL 与列名不变，含 SQL Statement sheet）；
2. 金标准文书 zip 按住院次打包（现有命名 `<患者ID>_<姓名>(<时间>).zip` 保持）；
3. 新录音按命名约定落 `录音/` 子目录；
4. 涉及患者标识的列（NAME/PATIENT_ID/电话/证件）系统侧入库前自动扫描脱敏，导出侧无需预处理。
