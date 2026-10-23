/* 上传入口文本解析（parseTranscriptText）回归测试 —— 对应 server/tests/test_upload_parse.py
   张老师 10.20 要求的上传入口：若雨的转写稿两种形态都要吃下去——
   对话体（"医生：…/患者：…"）按行分轮；结构化叙述体（主诉/现病史…）按段作为素材。
   从 app.js 抽取函数本体执行（不依赖浏览器/DOM）。node demo/tests/upload_parse.test.js
   任何用例失败退出码 1。 */
"use strict";
const fs = require("fs"), path = require("path");
const APP = path.join(__dirname, "..", "js", "app.js");
const src = fs.readFileSync(APP, "utf8");
const m = src.match(/function parseTranscriptText\(text\)\{[\s\S]*?\n\}/);
if (!m) { console.error("✗ app.js 中未找到 parseTranscriptText"); process.exit(1); }
(0, eval)(m[0]); // 间接 eval：函数声明落到全局（严格模式下直接 eval 不会泄漏）
const parseTranscriptText = globalThis.parseTranscriptText;
if (typeof parseTranscriptText !== "function") { console.error("✗ parseTranscriptText 提取失败"); process.exit(1); }

let failed = 0;
const check = (name, cond, got) => {
  if (cond) { console.log("  ✓ " + name); }
  else { failed++; console.error("  ✗ " + name + " — " + JSON.stringify(got)); }
};

// 1. 对话体：按行分轮，角色归一（医生/患者）
const t1 = parseTranscriptText("医生：哪里不舒服？\n患者：肚子疼。\n医生：多久了？\n患者：昨天下午开始。");
check("对话体分轮数与角色", t1.length === 4 && t1[0].role === "医生" && t1[1].role === "患者", t1);
check("对话体内容完整", t1[3].text === "昨天下午开始。", t1);

// 2. 结构化叙述体：整段作为素材，角色"对话"（若雨 patient01自动生成入院记录.txt 形态）
const t2 = parseTranscriptText("主诉：腹痛3天\n\n现病史：患者3天前无明显诱因出现腹痛\n\n既往史：体健");
check("叙述体按段兜底", t2.length === 3 && t2.every(t => t.role === "对话"), t2);
check("叙述体段内容含小标题", t2[1].text.includes("现病史"), t2);

// 3. 混合 + CRLF + 空行
const t3 = parseTranscriptText("医生：你好\r\n\r\n患者：你好\r\n医生：哪不舒服\r\n患者：头疼");
check("CRLF/空行混合解析", t3.length === 4 && t3[0].role === "医生" && t3[3].role === "患者", t3);

// 4. 别名角色（医师/大夫/病人/家属）
const t4 = parseTranscriptText("大夫：哪疼\n病人：头疼\n家属：补一句");
check("角色别名归一", t4.length === 3 && t4[0].role === "医生" && t4[1].role === "患者" && t4[2].role === "患者", t4);

// 5. 空文本 → 零轮次（前端据此走"上传失败"提示，不静默空跑）
check("空文本零轮次", parseTranscriptText("").length === 0, []);

// 6. 全角/半角冒号均识别
const t6 = parseTranscriptText("医生:半角冒号\n患者：全角冒号");
check("半角冒号识别", t6.length === 2 && t6[0].text === "半角冒号", t6);

// 7. ASR 导出件元数据行跳过：# 注释头 / --- 分节线（若雨 paraformer 导出格式，合成样本）
const t7 = parseTranscriptText("# 标准录音 1.mp3\n# engine=DashScope hotwords=93\n# ts=2026-10-17 15:38:22\n\n--- ASR 原文 ---\n确。哦。他从昨天下午开始痛。\n\n--- 纠错后 ---\n确，他从昨天下午开始痛。");
check("元数据行不产轮次", t7.length === 2 && t7.every(t => t.role === "对话"), t7);
check("分节线内容不入素材", !t7.some(t => t.text.includes("---") || t.text.includes("engine=")), t7);

// 8. 对话体 + 元数据行混合（元数据夹在对话之间也被跳过）
const t8 = parseTranscriptText("# 转写稿\n医生：哪里不舒服\n# 自动标注\n患者：肚子疼");
check("元数据行夹在对话间跳过", t8.length === 2 && t8[0].role === "医生" && t8[1].role === "患者", t8);

if (failed) { console.error(`✗ ${failed} 个用例失败`); process.exit(1); }
console.log("✓ parseTranscriptText 全部用例通过");
