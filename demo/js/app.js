/* ============================================================
   AI 病历智能体 · 医生工作台（演示版）
   零依赖单页应用：工作台 / 患者全景 / 住院工作区（录音→生成→四色确认→归档）/ 字段地图
   ============================================================ */
"use strict";

const $app = document.getElementById("app");
const S = {
  patients: [], dialogue: [], fieldmap: null,
  flow: {},          // flowKey -> {step, revealed, transcriptDone, agentN, edits:{}, archived:false}
  timers: [],
  user: null,        // 当前登录用户（api/auth/me 返回）；null = 未登录
};

/* ---------------- 工具 ---------------- */
const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const flowKey = (code, idx) => `${code}_${idx}`;
function clearTimers(){ S.timers.forEach(t => clearInterval(t) || clearTimeout(t)); S.timers = []; }
function later(fn, ms){ const t = setTimeout(fn, ms); S.timers.push(t); }
function fmtTs(ts){ if(!ts || ts.length !== 8) return ts || "—"; return `${ts.slice(0,4)}-${ts.slice(4,6)}-${ts.slice(6,8)}`; }
function daysBetween(a, b){ if(!a || !b) return "—"; return Math.max(1, Math.round((new Date(b) - new Date(a)) / 864e5)); }

/* ---------------- 字段分类：四色与分区 ---------------- */
const GREEN = new Set(["主诉","主要症状","症状","持续时间","数字","时间单位","现病史","发病情况","诱因","主要症状特点及其发展变化","伴随症状","上呼吸道症状","消化道症状","泌尿系症状","发病以来诊治经过及结果","发病以来一般情况","精神状态","食欲","睡眠","小便情况","大便情况","体重","与本次疾病无紧密关系的其他疾病情况","病史","既往史","疾病史（含外伤）","一般健康状况标志","健康状况","心血管病史","其他病史","肝炎结核病史","手术外伤输血","手术外伤史","过敏史","预防接种史","个人史","居住地","地址","接触史","疫区接触史","特殊地区居住史","有毒物质接触史","生活习惯、烟酒史","吸烟史","饮酒史","冶游史","婚育史","婚姻史","婚姻史(知识库)","生育史","生育史(知识库)","月经史","月经史：","月经量","月经颜色","月经相关症状","家族史","家族健康状况","父母","兄弟姐妹","有无遗传倾向疾病","病史陈述者姓名","病史陈述者","陈述者与患者关系的代码","查房记录","初步诊断","入院诊断","出院诊断","诊断依据","鉴别诊断","诊疗计划","诊疗经过","出院情况","出院医嘱","手术经过","术前诊断","术中诊断"]);
const PE = new Set(["发育","营养","表情","面容","神志","体位","配合检查","色泽","肝掌蜘蛛痣","全身浅表淋巴结","头颅异常","眼睑水肿","结膜","巩膜","角膜","瞳孔","对光反射","外耳道","乳突","鼻","鼻窦","口唇","口腔粘膜","齿龈","咽部粘膜","扁桃体","颈部","颈","颈动脉","颈静脉","气管","肝颈静脉回流征","甲状腺","甲状腺异常","胸廓","胸骨叩痛","呼吸运动","呼吸规整","肋间隙","语颤","胸膜摩擦感","叩诊","呼吸音","双侧","干湿性罗音","有无","心前区隆起","心律","心包摩擦音","腹外形","腹壁静脉曲张","腹部紧张度","压痛反跳痛","包块","肝脏","肠鸣音","直肠肛门","肛门生殖器","脊柱","脊柱畸形","四肢","专科情况","老中青","性别","起病","护理级别","Padua评分"]);
const VITALS = new Set(["体征","体温","脉搏","呼吸","收缩压","舒张压"]);

function colorOf(f){
  const label = f.label || "";
  if (!String(f.value || "").trim()) return "yellow";
  if (/签名/.test(label)) return "yellow";
  if (GREEN.has(label)) return "green";
  if (label === "记录时间" || label === "辅助检查结果" || label === "辅助检查" || VITALS.has(label)) return "blue";
  if (f.binding === "Patient") return "blue";
  if (PE.has(label) || label.startsWith("体格检查")) return "gray";
  return "green";
}
function sectionOf(f){
  const l = f.label || "";
  if (!l || l === "记录时间" || /陈述者/.test(l)) return "基本信息";
  if (l === "主诉" || l === "主要症状") return "主诉";
  if (["现病史","发病情况","诱因","主要症状特点及其发展变化","伴随症状","上呼吸道症状","消化道症状","泌尿系症状","发病以来诊治经过及结果","发病以来一般情况","精神状态","食欲","睡眠","小便情况","大便情况","体重","与本次疾病无紧密关系的其他疾病情况","病史","数字","时间单位","症状","持续时间"].includes(l)) return "现病史";
  if (/既往|疾病史|健康状况|心血管病史|其他病史|肝炎|手术外伤|过敏|预防接种/.test(l)) return "既往史";
  if (/个人史|居住地|地址|接触史|疫区|特殊地区|有毒物质|生活习惯|吸烟|饮酒|发病时间|冶游/.test(l)) return "个人史";
  if (/婚育|婚姻|生育|月经/.test(l)) return "婚育史";
  if (/家族|父母|兄弟姐妹|遗传/.test(l)) return "家族史";
  if (VITALS.has(l)) return "生命体征";
  if (PE.has(l) || l.startsWith("体格检查")) return "体格检查";
  if (/辅助检查/.test(l)) return "辅助检查";
  if (/诊断/.test(l)) return "初步诊断";
  if (/签名/.test(l)) return "签名";
  return "其他";
}
const SECTION_ORDER = ["基本信息","主诉","现病史","既往史","个人史","婚育史","家族史","生命体征","体格检查","辅助检查","初步诊断","签名","其他"];
const COLOR_META = {
  blue:   { name: "蓝 · 系统带入", dot: "blue",  tip: "来自 HIS / 结构化数据，默认可信，抽查即可" },
  green:  { name: "绿 · 对话提炼", dot: "green", tip: "由问诊对话/口述生成，可溯源到转写原文" },
  gray:   { name: "灰 · 规范所见", dot: "gray",  tip: "模板规范项 + 查体所见合成，请医生过目核定" },
  yellow: { name: "黄 · 缺失待补", dot: "yellow",tip: "对话未获取、系统无法判定——留空提示补充，严禁臆造" },
};

/* ---------------- 数据获取 ---------------- */
async function loadData(){
  const [p, d, f] = await Promise.all([
    fetch("data/patients.json").then(r => r.json()),
    fetch("data/dialogue.json").then(r => r.json()),
    fetch("data/fieldmap.json").then(r => r.json()),
  ]);
  S.patients = p; S.dialogue = d; S.fieldmap = f;
}
const getPatient = code => S.patients.find(p => p.code === code);
const getAdm = (code, idx) => { const p = getPatient(code); return p && p.admissions[idx - 1]; };

/* ---------------- 路由 ---------------- */
function route(){
  clearTimers();
  const hash = location.hash || "#/workbench";
  const parts = hash.slice(2).split("/");
  const [view, a, b] = parts;
  document.querySelectorAll("[data-nav]").forEach(n => n.classList.remove("active"));
  const navMap = { workbench: "工作台", review: "审签", manual: "手册", about: "说明" };
  const navEl = [...document.querySelectorAll("[data-nav]")].find(n => n.textContent === navMap[view]);
  if (navEl) navEl.classList.add("active");
  window.scrollTo(0, 0);
  if (view === "patient" && a) return viewPatient(a);
  if (view === "adm" && a && b) return viewAdmission(a, +b);
  if (view === "login" || view === "register") return viewAuth(view);
  if (view === "fieldmap") return viewFieldmap();
  if (view === "manual") return viewManual();
  if (view === "about") return viewAbout();
  if (view === "review") return viewReview();
  viewWorkbench();
}
window.addEventListener("hashchange", route);

/* ================================================================ */
/* 工作台                                                            */
/* ================================================================ */
function viewWorkbench(){
  const totalDocs = S.patients.reduce((n, p) => n + p.admissions.reduce((m, a) => m + a.doc_count, 0), 0);
  const totalAdm = S.patients.reduce((n, p) => n + p.admissions.length, 0);
  let flowSaved = null;
  try { flowSaved = JSON.parse(localStorage.getItem("mra_flow_DA0001_1")); } catch (e) {}
  const editsDone = flowSaved ? Object.keys(flowSaved.edits || {}).length : 0;
  const flowDone = flowSaved && flowSaved.step === 4;
  const task1Title = flowDone ? "患者A · 入院记录（急性阑尾炎）— 已归档（演示）"
    : editsDone > 0 ? `患者A · 入院记录（急性阑尾炎）— 核对中，已修改 ${editsDone} 处`
    : "患者A · 入院记录（急性阑尾炎）— AI 草稿已生成，待四色确认";
  const task1Btn = flowDone ? "查看归档结果 →" : "继续核对 →";
  $app.innerHTML = `
    <div class="notice">⚠ <b>演示说明</b>&nbsp;本工作台为项目演示环境：患者已脱敏（患者A / 患者B），数据源自院方提供的真实住院病历（金标准）；"录音转写"为按真实入院记录重构的模拟对话，"AI 生成"内容取自金标准文书以演示确认流程。正式环境中草稿由多智能体实时生成。</div>
    <div class="page-head">
      <div class="page-title">工作台<small>${S.user ? esc(S.user.name) + (S.user.department ? " · " + esc(S.user.department) : "") : "王医生"} · 演示数据集</small></div>
      <div style="display:flex;gap:8px"><button class="btn" onclick="startTour(true)">▶ 新手引导</button><button class="btn" onclick="location.hash='#/manual'">📖 医生操作手册</button><button class="btn" onclick="location.hash='#/about'">了解系统边界</button></div>
    </div>
    <div class="sec-head" style="padding-left:2px;font-size:17px;border:none;background:none;cursor:default">今日书写任务</div>
    <div class="task-list" id="tour-tasks">
      <div class="card task-row t-warn">
        <div class="t-icon">🖊</div>
        <div class="t-main">
          <div class="t-title">${task1Title}</div>
          <div class="t-sub">法定时限：入院 24h 内完成 · 剩余 13h · 27 轮问诊对话已转写${editsDone > 0 && !flowDone ? " · 修改已自动留痕" : ""}</div>
        </div>
        <button class="btn primary" onclick="location.hash='#/adm/DA0001/1'">${task1Btn}</button>
      </div>
      <div class="card task-row">
        <div class="t-icon" style="background:var(--primary-soft)">📝</div>
        <div class="t-main">
          <div class="t-title">患者A · 首次病程记录 — AI 生成已开放</div>
          <div class="t-sub">法定时限：入院 8h 内 · 病例特点 / 诊断依据 / 鉴别诊断（知识辅助）/ 诊疗计划</div>
        </div>
        <button class="btn primary" onclick="startFlowDoc('首次病程记录')">生成草稿 →</button>
      </div>
      <div class="card task-row t-dim">
        <div class="t-icon">🩺</div>
        <div class="t-main">
          <div class="t-title">患者A · 手术记录 — 待书写（术后追记）</div>
          <div class="t-sub">法定时限：术后 24h 内 · AI 生成将于一期扩展开放（当前请手工书写）</div>
        </div>
        <span class="tag" style="background:var(--paper-2);color:var(--ink-3)">二期开放</span>
      </div>
      <div class="card task-row" style="opacity:.75">
        <div class="t-icon" style="background:var(--green-bg)">✓</div>
        <div class="t-main">
          <div class="t-title">历史参考：患者B · 社区获得性肺炎住院全流程已归档（15 份文书）</div>
          <div class="t-sub">可作为 AI 写作风格学习样本 · 点击患者卡片查看全景视图</div>
        </div>
      </div>
    </div>
    <div class="stat-row" id="tour-stats">
      <div class="card stat"><span class="num">${S.patients.length}</span><span class="lab">在院患者（脱敏）</span></div>
      <div class="card stat b"><span class="num">${totalAdm}</span><span class="lab">住院记录</span><span class="sub">2023-11 ~ 2026-09</span></div>
      <div class="card stat g"><span class="num">${totalDocs}</span><span class="lab">累计病历文书</span><span class="sub">入院 / 病程 / 手术 / 出院 等</span></div>
      <div class="card stat y"><span class="num">1</span><span class="lab">待确认草稿</span><span class="sub">患者A · 入院记录</span></div>
    </div>
    <div class="sec-head" style="padding-left:2px;font-size:17px;border:none;background:none;cursor:default;margin-bottom:12px">我的患者</div>
    <div class="pt-grid" id="tour-patients">
      ${S.patients.map((p, pi) => `
        <div class="card pt-card" onclick="location.hash='#/patient/${p.code}'">
          <div class="pt-top">
            <div class="pt-avatar">${p.display.slice(-1)}</div>
            <div>
              <div class="pt-name">${p.display} <span class="tag" style="background:var(--paper-2);color:var(--ink-3)">${p.code}</span></div>
              <div class="pt-meta">${patientMeta(p)}</div>
            </div>
          </div>
          <div class="adm-list">
            ${p.admissions.map((a, i) => `
              <div class="adm-item" onclick="event.stopPropagation();location.hash='#/adm/${p.code}/${i + 1}'">
                <span class="idx">${i + 1}</span>
                <span class="dis">${esc(a.disease || "住院记录")}</span>
                <span class="meta">${fmtTs(a.admit_ts)} · ${a.doc_count} 份文书</span>
                <span class="go">进入 →</span>
              </div>`).join("")}
          </div>
          ${p.code === "DA0001" ? `<div style="margin-top:14px"><button class="btn primary big" onclick="event.stopPropagation();location.hash='#/adm/DA0001/1'">▶ 开始入院问诊（演示全流程）</button></div>` : `<div style="margin-top:14px;font-size:12px;color:var(--ink-3)">全景视图 · 点击住院记录查看文书流（交互流程演示配置于患者A）</div>`}
        </div>`).join("")}
    </div>`;
}
function patientMeta(p){
  const sexes = { DA0001: "男 · 23岁 · 普外科", DA0002: "女 · 58岁 · 多科室" };
  return sexes[p.code] || "";
}

/* ================================================================ */
/* 患者全景                                                          */
/* ================================================================ */
function viewPatient(code){
  const p = getPatient(code);
  if (!p) return viewWorkbench();
  $app.innerHTML = `
    <div class="card pv-head">
      <div class="pv-avatar">${p.display.slice(-1)}</div>
      <div style="flex:1">
        <div class="pv-title">${p.display} <span class="tag" style="background:var(--paper-2);color:var(--ink-3)">${p.code} · 已脱敏</span></div>
        <div class="pv-sub">
          <span class="tag" style="background:var(--primary-soft);color:var(--primary-deep)">${p.admissions.length} 次住院</span>
          <span class="tag" style="background:var(--primary-soft);color:var(--primary-deep)">${p.admissions.reduce((n,a)=>n+a.doc_count,0)} 份文书</span>
          <span class="tag" style="background:var(--paper-2);color:var(--ink-2)">患者全景视图 · 跨住院次聚合</span>
        </div>
      </div>
      <button class="btn" onclick="history.back()">← 返回</button>
    </div>
    <div class="tl">
      ${p.admissions.map((a, i) => admissionTimeline(p, a, i)).join("")}
    </div>`;
}
function admissionTimeline(p, a, i){
  const typeCount = {};
  a.docs.forEach(d => typeCount[d.type] = (typeCount[d.type] || 0) + 1);
  const his = a.his;
  return `
    <div class="tl-item">
      <div class="tl-date">${fmtTs(a.admit_ts)} ${a.discharge_ts ? "→ " + fmtTs(a.discharge_ts) : ""} · 住院 ${daysBetween(fmtTs(a.admit_ts), fmtTs(a.discharge_ts))} 天</div>
      <div class="card tl-card">
        <div class="tl-head">
          <span class="tl-disease">第 ${i + 1} 次 · ${esc(a.disease || "住院记录")}</span>
          <span class="tag" style="background:var(--paper-2);color:var(--ink-2)">VISIT_ID ${a.visit_id ?? "—"}</span>
          <button class="btn" style="margin-left:auto" onclick="event.stopPropagation();showAISummary('${p.code}',${i + 1})">⚡ AI 病例总结</button>
          ${p.code === "DA0001" ? `<button class="btn primary" onclick="location.hash='#/adm/${p.code}/${i + 1}'">进入住院工作区 →</button>`
            : `<span style="font-size:11.5px;color:var(--ink-3)">只读视图 · 点击文书查看</span>`}
        </div>
        <div class="doc-chips">
          ${Object.entries(typeCount).map(([t, n]) => `<span class="doc-chip"><b>${n}</b> ${t}</span>`).join("")}
        </div>
        ${his ? hisBoxes(his) : ""}
        <div class="doc-chips" style="margin-top:12px;border-top:1px dashed var(--line);padding-top:10px">
          ${a.docs.map((d, di) => `<span class="doc-chip" style="cursor:pointer" onclick="openDoc('${p.code}',${p.admissions.indexOf(a)},${di})">${esc(d.type)}·${esc(d.title || "")}<b style="margin-left:4px">${d.ts ? fmtTs(d.ts).slice(5) : ""}</b></span>`).join("")}
        </div>
      </div>
    </div>`;
}
function hisBoxes(his){
  const labs = (his.abnormal_labs || []).slice(0, 4);
  const exams = (his.exams || []).slice(0, 2);
  const vits = (his.vitals || []).filter(v => /体温|脉搏|血压|呼吸|血氧/.test(v.name)).slice(0, 5);
  if (!labs.length && !exams.length && !vits.length) return "";
  return `
    <div class="his-band">
      <div class="his-box"><div class="h"><span>异常检验（HIS 自动标记）</span><span>🔴</span></div>
        ${labs.length ? labs.map(l => `<div class="his-line"><span class="n">${esc(l.item)}</span><span class="val warn">${esc(l.result)}${l.units ? " " + esc(l.units) : ""} ${l.ref ? `<i style="color:var(--ink-3);font-style:normal">(${esc(l.ref)})</i>` : ""}</span></div>`).join("") : `<div class="his-line"><span class="n">本次住院无异常检验标记</span></div>`}
      </div>
      <div class="his-box"><div class="h"><span>检查报告印象</span><span>🩻</span></div>
        ${exams.length ? exams.map(e => `<div class="his-line"><span class="n" style="flex:1">${esc(e.impression)}</span></div>`).join("") : `<div class="his-line"><span class="n">—</span></div>`}
      </div>
      <div class="his-box"><div class="h"><span>生命体征汇总（区间 / 最近）</span><span>💓</span></div>
        ${vits.map(v => `<div class="his-line"><span class="n">${esc(v.name)}</span><span class="val">${v.min === v.max ? v.last : v.min + "~" + v.max} → ${v.last} ${esc(v.unit || "")}</span></div>`).join("")}
      </div>
    </div>`;
}

/* ---------------- 文书弹窗（只读） ---------------- */
function openDoc(code, admIdx, docIdx){
  const adm = getAdm(code, admIdx + 1);
  const d = adm.docs[docIdx];
  const rows = d.fields.map((f, i) => {
    const c = colorOf(f);
    const coded = f.dropdown && /^\d+$/.test(String(f.value).trim());
    return `<div class="f-row c-${c}">
      <div class="lb"><span class="cdot ${c}"></span>${esc(f.label || "（基本项）")}${f.dropdown ? ' <span class="tag" style="background:var(--paper-2);color:var(--ink-3)">受控</span>' : ""}</div>
      <div class="vv">${coded ? `<span class="mono" style="font-family:var(--mono);color:var(--ink-3)">〔受控码〕${esc(f.value)}</span>` : esc(f.value) || '<span style="color:var(--yellow)">（空 · 待补）</span>'}</div>
      <div class="ops"></div>
    </div>`;
  }).join("");
  document.getElementById("modal-root").innerHTML = `
    <div class="modal-mask" onclick="if(event.target===this)closeModal()">
      <div class="modal">
        <div class="modal-head"><span class="t">${esc(d.type)} · ${esc(d.title || "")}</span>
          <span class="tag" style="background:var(--paper-2);color:var(--ink-3)">${d.ts ? fmtTs(d.ts) : ""} · ${d.fields.length} 字段 · 已脱敏</span>
          <span class="x" onclick="closeModal()">✕</span></div>
        <div class="modal-body">${rows}</div>
      </div>
    </div>`;
}
function closeModal(){ document.getElementById("modal-root").innerHTML = ""; }

/* ================================================================ */
/* 住院工作区（核心流程）                                            */
/* ================================================================ */
function flowState(key){
  if (!S.flow[key]){
    let saved = null;
    try { saved = JSON.parse(localStorage.getItem("mra_flow_" + key)); } catch (e) {}
    // 以默认结构打底合并，旧版本/不完整存档缺字段时不致渲染报错
    S.flow[key] = { step: 1, revealed: 0, transcriptDone: false, agentN: 0, edits: {}, editLog: [], archived: false, filter: "all",
                    ...(saved && typeof saved === "object" ? saved : {}) };
  }
  return S.flow[key];
}
function persistFlow(key){
  try { localStorage.setItem("mra_flow_" + key, JSON.stringify(S.flow[key])); } catch (e) {}
}
function viewAdmission(code, idx){
  const p = getPatient(code), adm = getAdm(code, idx);
  if (!adm) return viewWorkbench();
  const key = flowKey(code, idx), st = flowState(key);
  const editable = p.code === "DA0001";
  const steps = ["问诊录音", "AI 生成草稿", "四色确认", "归档"];
  const stepBar = editable ? `
    <div class="ws-steps">
      ${steps.map((s, i) => `<div class="ws-step ${st.step === i + 1 ? "cur" : st.step > i + 1 ? "done" : ""}"><span class="n">${st.step > i + 1 ? "✓" : i + 1}</span>${s}</div>`).join("")}
    </div>` : `
    <div class="notice">本住院为<b>只读全景视图</b>（演示交互流程配置于患者A 的住院记录）。您可查看文书流与 HIS 汇总，点击文书查看字段内容。</div>`;
  $app.innerHTML = `
    <div class="card ws-head">
      <div style="display:flex;align-items:center;gap:14px">
        <button class="btn ghost" onclick="location.hash='#/patient/${code}'">←</button>
        <div>
          <div class="page-title" style="font-size:22px">${p.display} · 第 ${idx} 次住院<small>${esc(adm.disease || "")}</small></div>
          <div style="font-size:12px;color:var(--ink-3);margin-top:4px">${fmtTs(adm.admit_ts)} 入院 · ${adm.doc_count} 份文书 · 病案号 ${code}</div>
        </div>
      </div>
      <div style="display:flex;gap:8px">
        <span class="tag" style="background:var(--yellow-bg);color:var(--yellow)">法定时限：入院记录 24h 内完成</span>
        <span class="tag" style="background:var(--primary-soft);color:var(--primary-deep)">剩余 13h</span>
      </div>
    </div>
    ${stepBar}
    <div id="step-body"></div>`;

  if (editable){
    const chips = adm.docs.map((d, di) =>
      `<span class="doc-chip" style="cursor:pointer" onclick="openDoc('${code}',${idx - 1},${di})">${esc(d.type)}·${esc((d.title || "").slice(0, 14))}</span>`).join("");
    document.getElementById("step-body").insertAdjacentHTML("beforebegin",
      `<div class="card" style="padding:10px 16px;margin-bottom:14px;display:flex;gap:10px;align-items:center;flex-wrap:wrap">
        <span style="font-size:12px;color:var(--ink-3);white-space:nowrap">本次住院文书（${adm.doc_count} 份，点击调阅）</span>
        <div class="doc-chips" style="margin:0">${chips}</div>
      </div>`);
  }
  const body = document.getElementById("step-body");
  if (!editable) return renderAdmReadonly(body, p, adm, idx);
  if (st.step === 1) renderStepRecord(body, key, adm);
  else if (st.step === 2) renderStepGenerate(body, key, adm);
  else if (st.step === 3) renderStepConfirm(body, key, adm);
  else renderStepArchive(body, key, adm);
}

function renderAdmReadonly(body, p, adm, admIdx){
  body.innerHTML = `
    <div class="card" style="padding:18px 22px;margin-bottom:16px">
      <div class="md-title">文书流（${adm.docs.length} 份）</div>
      <div class="doc-chips">
        ${adm.docs.map((d, di) => `<span class="doc-chip" style="cursor:pointer" onclick="openDoc('${p.code}',${admIdx - 1},${di})">${esc(d.type)}·${esc(d.title || "")}<b style="margin-left:4px">${d.ts ? fmtTs(d.ts).slice(5) : ""}</b></span>`).join("")}
      </div>
    </div>
    ${adm.his ? `<div class="card" style="padding:18px 22px">${hisBoxes(adm.his)}</div>` : ""}`;
}

/* ---------------- Step1 问诊录音（P2-E：真实引擎就绪时走真实录音，否则演示回放） ---------------- */
function renderStepRecord(body, key, adm){
  const st = flowState(key);
  const real = !!(S.asr && S.asr.real);
  if (!S.asr){
    // 引擎状态未取回：先按演示模式渲染，状态到达后（用户尚未开始录音时）按真实模式重渲染
    fetch("api/asr/status").then(r => r.ok ? r.json() : null).then(d => {
      if (d && d.ok && !S.asr){
        S.asr = d;
        const cta = document.getElementById("rec-cta");
        if (cta && !cta.disabled && flowState(key).step === 1) renderStepRecord(body, key, adm);
      }
    }).catch(() => {});
  }
  body.innerHTML = `
    <div class="rec-grid">
      <div class="card rec-panel">
        <div class="rec-orb" id="rec-orb">
          <svg viewBox="0 0 24 24"><path d="M12 15a3.5 3.5 0 0 0 3.5-3.5V6a3.5 3.5 0 1 0-7 0v5.5A3.5 3.5 0 0 0 12 15zm5.5-3.5a5.5 5.5 0 0 1-11 0H4.8a7.2 7.2 0 0 0 6.3 7.1V21h1.8v-2.4a7.2 7.2 0 0 0 6.3-7.1h-1.7z"/></svg>
        </div>
        <div class="rec-time" id="rec-time">00:00</div>
        <div class="rec-state" id="rec-state">点击开始问诊录音</div>
        <div class="wave" id="wave">${"<i></i>".repeat(24)}</div>
        <button class="btn big primary" id="rec-cta" style="margin-top:10px;width:100%">●&nbsp; 开始录音</button>
        <div class="dialect-note">${real
          ? `🎙 <b>真实引擎已就绪</b>：${esc(S.asr.engine)} · 麦克风采集 16kHz PCM，结束后整段上传转写${esc(S.asr.note ? "（" + S.asr.note + "）" : "")}`
          : `🎙 <b>方言识别</b>：系统实时将<b>山东方言</b>转写为普通话文本，自动区分<b>医生 / 患者</b>角色；药品名、检查名已加入医疗热词。演示环境为按真实入院记录重构的模拟对话。`}</div>
        <div style="font-size:11px;color:var(--ink-3);margin-top:8px;line-height:1.7">${real
          ? `真实模式：点击「开始录音」将请求<b>麦克风权限</b>并真实采集音频，结束后由服务端引擎转写（角色分离能力以引擎为准，无角色信息时可在过程稿中人工标注）。`
          : `演示模式：无需真实说话，点击「开始录音」后将自动播放一段模拟问诊，用以展示"对话 → 病历字段"的完整链路。配置 ASR_* 引擎密钥后本页自动切换为真实录音。`}</div>
      </div>
      <div class="card transcript" id="transcript">
        <div style="text-align:center;color:var(--ink-3);font-size:13px;padding:60px 0">等待录音开始…<br><span style="font-size:11.5px">转写内容将实时显示在此处，并自动挂载为 MD 过程稿</span></div>
      </div>
    </div>`;

  const orb = document.getElementById("rec-orb"), wave = document.getElementById("wave"),
        timeEl = document.getElementById("rec-time"), stateEl = document.getElementById("rec-state"),
        cta = document.getElementById("rec-cta"), tp = document.getElementById("transcript");
  let rec = false, sec = 0;
  cta.onclick = () => {
    if (!rec){
      if (real){
        startRealRecording(wave).then(ok => {
          if (!ok){
            stateEl.textContent = "无法访问麦克风（需授权，且本地环境为 localhost 或 HTTPS）";
            window._startDemoReplay && window._startDemoReplay();
            return;
          }
          rec = true; orb.classList.add("rec"); wave.classList.add("on");
          stateEl.textContent = "正在录音 · 真实采集中（结束后转写）";
          cta.innerHTML = "■&nbsp; 结束问诊"; cta.classList.remove("primary");
          const t = setInterval(() => { sec++; timeEl.textContent = `${String(Math.floor(sec/60)).padStart(2,"0")}:${String(sec%60).padStart(2,"0")}`; }, 1000);
          S.timers.push(t);
        });
      } else {
        rec = true; orb.classList.add("rec"); wave.classList.add("on");
        stateEl.textContent = "正在录音 · 方言实时转写中";
        cta.innerHTML = "■&nbsp; 结束问诊"; cta.classList.remove("primary");
        const t = setInterval(() => { sec++; timeEl.textContent = `00:${String(sec).padStart(2, "0")}`; }, 1000);
        S.timers.push(t);
        window._startDemoReplay && window._startDemoReplay();
      }
    } else {
      rec = false; orb.classList.remove("rec"); wave.classList.remove("on");
      stateEl.textContent = real ? "录音已结束 · 正在转写…" : "录音已结束";
      cta.disabled = true;
      clearTimers();
      if (real){
        const b64 = stopRealRecording();
        transcribeReal(tp, key, b64, sec, () => { cta.disabled = false; });
      } else {
        finishTranscript(tp, key);
      }
    }
  };
  // 演示回放（demo 模式入口 & 真实模式降级路径）
  window._startDemoReplay = () => {
    st.transcriptTurns = null; st.transcriptMeta = null;
    startStreaming(tp, key);
  };
}
/* 真实录音（P2-E 前置）：getUserMedia + AudioContext(16kHz) 采 PCM，停止后前端打 WAV 上传。
   选 16k 单声道直出是为了让服务端引擎免 ffmpeg 直通（生产容器为 alpine 无转码工具）。 */
async function startRealRecording(wave){
  try{
    if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) return false;
    const stream = await navigator.mediaDevices.getUserMedia({
      audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true } });
    window._recStream = stream;
    let ctx;
    try { ctx = new (window.AudioContext || window.webkitAudioContext)({ sampleRate: 16000 }); }
    catch (e) { ctx = new (window.AudioContext || window.webkitAudioContext)(); }
    window._recCtx = ctx;
    const src = ctx.createMediaStreamSource(stream);
    const analyser = ctx.createAnalyser(); analyser.fftSize = 64;
    src.connect(analyser);
    const volArr = new Uint8Array(analyser.frequencyBinCount);
    window._volTimer = setInterval(() => {
      analyser.getByteFrequencyData(volArr);
      wave.querySelectorAll("i").forEach((b, i) => {
        b.style.height = Math.max(4, (volArr[i % volArr.length] / 255) * 30) + "px";
      });
    }, 120);
    const proc = ctx.createScriptProcessor(4096, 1, 1);
    window._pcmChunks = [];
    proc.onaudioprocess = e => { window._pcmChunks.push(new Float32Array(e.inputBuffer.getChannelData(0))); };
    src.connect(proc); proc.connect(ctx.destination);
    window._recProc = proc;
    window._recRate = ctx.sampleRate;
    return true;
  } catch (e) { return false; }
}
function stopRealRecording(){
  try { if (window._recProc) window._recProc.disconnect(); } catch (e) {}
  try { if (window._volTimer) clearInterval(window._volTimer); } catch (e) {}
  try { if (window._recStream) window._recStream.getTracks().forEach(t => t.stop()); } catch (e) {}
  try { if (window._recCtx) window._recCtx.close(); } catch (e) {}
  return buildWavBase64(window._pcmChunks || [], window._recRate || 16000);
}
function buildWavBase64(chunks, rate){
  let n = 0; chunks.forEach(c => n += c.length);
  const buf = new ArrayBuffer(44 + n * 2), v = new DataView(buf);
  const ws = (o, s) => { for (let i = 0; i < s.length; i++) v.setUint8(o + i, s.charCodeAt(i)); };
  ws(0, "RIFF"); v.setUint32(4, 36 + n * 2, true); ws(8, "WAVE"); ws(12, "fmt ");
  v.setUint32(16, 16, true); v.setUint16(20, 1, true); v.setUint16(22, 1, true);
  v.setUint32(24, rate, true); v.setUint32(28, rate * 2, true); v.setUint16(32, 2, true); v.setUint16(34, 16, true);
  ws(36, "data"); v.setUint32(40, n * 2, true);
  let off = 44;
  chunks.forEach(c => { for (let i = 0; i < c.length; i++, off += 2) {
    const s = Math.max(-1, Math.min(1, c[i]));
    v.setInt16(off, s < 0 ? s * 0x8000 : s * 0x7fff, true);
  } });
  const bytes = new Uint8Array(buf); let bin = "";
  for (let i = 0; i < bytes.length; i += 0x8000) bin += String.fromCharCode.apply(null, bytes.subarray(i, i + 0x8000));
  return btoa(bin);
}
async function transcribeReal(tp, key, wavB64, durSec, onFail){
  const st = flowState(key);
  tp.innerHTML = `<div style="text-align:center;color:var(--ink-3);font-size:13px;padding:60px 0">⏳ 正在上传音频并转写<span class="caret"></span><br><span style="font-size:11.5px">引擎 ${esc((S.asr || {}).engine || "")} · 录音时长 ${durSec}s · 整段文件识别</span></div>`;
  try{
    const r = await fetch("api/asr/transcribe", { method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ audio_base64: wavB64, filename: `问诊_DA0001_${Date.now()}`, duration_sec: durSec }) });
    const d = await r.json().catch(() => ({}));
    if (!r.ok || !d.ok) throw new Error(d.error || `AI 服务返回 ${r.status}`);
    st.transcriptTurns = (d.segments || []).map(s => ({
      role: s.speaker === "患者" ? "患者" : (s.speaker === "医生" ? "医生" : "对话"), text: s.text }));
    st.transcriptMeta = { engine: d.engine, duration_sec: d.duration_sec || durSec,
                          mock: !!d.mock, warnings: d.warnings || [], transcribed_at: d.transcribed_at };
    st.transcriptDone = true;
    st.revealed = (st.transcriptTurns || []).length;
    renderTranscriptResult(tp, key);
  } catch (e) {
    tp.innerHTML = `<div class="notice">⚠ 转写失败：${esc(e.message || e)}<br>
      <span style="font-size:11.5px">可重试录音；或先用演示回放继续体验后续流程（演示数据会明确标注，不会进入真实归档依据）。</span><br>
      <button class="btn" style="margin-top:10px" onclick="window._startDemoReplay && window._startDemoReplay()">使用演示回放</button></div>`;
    if (onFail) onFail();
  }
}
function transcriptTurns(key){
  const st = flowState(key);
  return st.transcriptTurns && st.transcriptTurns.length ? st.transcriptTurns : S.dialogue;
}
function startStreaming(tp, key){
  const st = flowState(key);
  tp.innerHTML = "";
  let i = 0;
  const next = () => {
    if (i >= S.dialogue.length) return;
    const turn = S.dialogue[i++];
    st.revealed = i;
    const line = document.createElement("div");
    line.className = `ts-line ${turn.role === "医生" ? "doctor" : "patient"}`;
    line.innerHTML = `
      <div class="who">${turn.role}</div>
      <div class="ts-bubble">
        ${turn.role === "患者" ? `<div class="dialect">🗣 原声（山东口音）→ 已转写为普通话</div><span class="txt"></span><span class="caret"></span>` : `<span class="txt"></span><span class="caret"></span>`}
      </div>`;
    tp.appendChild(line);
    tp.scrollTop = tp.scrollHeight;
    const txtEl = line.querySelector(".txt"), caret = line.querySelector(".caret");
    const text = turn.text; let c = 0;
    const typeT = setInterval(() => {
      c += 2; txtEl.textContent = text.slice(0, c);
      tp.scrollTop = tp.scrollHeight;
      if (c >= text.length){ clearInterval(typeT); caret.remove(); }
    }, 24);
    S.timers.push(typeT);
    S.timers.push(setTimeout(next, text.length * 24 + 500));
  };
  next();
}
function finishTranscript(tp, key){
  const st = flowState(key);
  st.transcriptDone = true;
  renderTranscriptResult(tp, key);
}
function renderTranscriptResult(tp, key){
  const st = flowState(key);
  const turns = transcriptTurns(key);
  const meta = st.transcriptMeta;
  const mdText = transcriptText(key);
  tp.innerHTML = turns.map(t => `
    <div class="ts-line ${t.role === "医生" ? "doctor" : (t.role === "患者" ? "patient" : "")}" style="animation:none;opacity:1;transform:none">
      <div class="who">${esc(t.role)}</div><div class="ts-bubble">${esc(t.text)}</div>
    </div>`).join("") + `
    <div class="md-doc">
      <div class="md-title">📄 MD 转写过程稿 · 已挂载至住院时间线${st.transcriptEdit !== undefined ? ' <span class="tag" style="background:var(--green-bg);color:var(--green)">✓ 已人工修正</span>' : ''}${meta ? ` <span class="tag" style="background:var(--blue-bg);color:var(--blue)">引擎 ${esc(meta.engine)}${meta.mock ? " · 模拟输出" : ""}</span>` : ''}</div>
      <div class="md-body">问诊记录_DA0001_202609021040.md&nbsp;&nbsp;·&nbsp;&nbsp;${turns.length} 轮对话 · ${st.transcriptEdit !== undefined ? st.transcriptEdit.length : mdText.length - turns.length * 3} 字${meta ? ` · ${esc(meta.engine)}${meta.duration_sec ? " · " + meta.duration_sec + "s" : ""}` : " · 医生/患者角色已分离"} · 全文可溯源</div>
      ${(meta && (meta.warnings || []).length) ? `<div style="font-size:11px;color:var(--yellow);margin-top:6px">⚠ ${esc(meta.warnings.join("；"))}</div>` : ""}
      <div style="margin-top:14px;display:flex;gap:10px;flex-wrap:wrap">
        <button class="btn primary big" id="gen-cta">⚙ 生成入院记录草稿（多智能体）</button>
        <button class="btn big" id="edit-ts">✎ 修正过程稿</button>
      </div>
    </div>`;
  document.getElementById("gen-cta").onclick = () => { const st2 = flowState(key); st2.step = 2; persistFlow(key); viewAdmission("DA0001", 1); };
  document.getElementById("edit-ts").onclick = () => editTranscript(tp, key);
  const mdEl = tp.querySelector(".md-doc");
  if (mdEl) mdEl.scrollIntoView({ behavior: "smooth", block: "center" });
}
/* 过程稿统一文本（"医生：…\n患者：…"）：生成请求与编辑框共用同一数据源 */
function transcriptText(key){
  const st = flowState(key);
  if (st.transcriptEdit !== undefined) return st.transcriptEdit;
  return transcriptTurns(key).map(t => t.role + "：" + t.text).join("\n");
}
function editTranscript(tp, key){
  const st = flowState(key);
  const cur = transcriptText(key);
  const zone = tp.querySelector(".md-doc");
  zone.innerHTML = `
    <div class="md-title">✎ 修正转写过程稿（原稿留痕，修正将标记并参与生成）</div>
    <textarea id="ts-edit" style="width:100%;min-height:220px;font-family:var(--sans);font-size:12.5px;line-height:1.9;border:1.5px solid var(--primary);border-radius:9px;padding:10px 12px;box-sizing:border-box">${esc(cur)}</textarea>
    <div style="margin-top:8px;display:flex;gap:8px">
      <button class="mini-btn" style="background:var(--primary);border-color:var(--primary);color:#fff" id="ts-save">保存修正</button>
      <button class="mini-btn" id="ts-cancel">取消</button>
    </div>`;
  document.getElementById("ts-cancel").onclick = () => finishTranscript(tp, key);
  document.getElementById("ts-save").onclick = () => {
    st.transcriptEdit = document.getElementById("ts-edit").value;
    persistFlow(key);
    finishTranscript(tp, key);
  };
}

/* ---------------- Step2 多智能体生成 ---------------- */
const AGENTS = [
  { icon: "🔍", name: "信息抽取智能体" },
  { icon: "🗃", name: "数据汇聚智能体" },
  { icon: "📚", name: "知识检索智能体" },
  { icon: "✍️", name: "病历生成智能体" },
  { icon: "🛡", name: "质控校验智能体" },
  { icon: "🧩", name: "字段映射智能体" },
];
const AGENT_KEYS = ["extractor", "aggregator", "retriever", "writer", "qc", "mapper"];
const FLOW_DOCS = ["入院记录", "首次病程记录"];
const DOC_META = {
  "入院记录": { sub: "160 字段 · 法定时限 24h", icon: "📋" },
  "首次病程记录": { sub: "病例特点/诊断依据/鉴别诊断/诊疗计划 · 法定时限 8h", icon: "📝" },
};
// 分文书草稿缓存（兼容旧版单 agentResult 存档：视为入院记录结果）
function getAgentResult(st, doc){
  if (st.agentResults && st.agentResults[doc]) return st.agentResults[doc];
  if (doc === "入院记录" && st.agentResult) return st.agentResult;
  return null;
}
function setAgentResult(st, doc, res){
  st.agentResults = st.agentResults || {};
  st.agentResults[doc] = res;
}
function renderStepGenerate(body, key, adm){
  const st = flowState(key);
  const curDoc = st.currentDoc || "入院记录";
  const cached = getAgentResult(st, curDoc);
  if (cached){                    // 已有真草稿（重进页面直接显示）
    renderAgentResult(body, key, cached, curDoc);
    return;
  }
  const meta = DOC_META[curDoc];
  body.innerHTML = `
    <div class="doc-tabs">
      ${FLOW_DOCS.map(d => `<button class="doc-tab ${d === curDoc ? "on" : ""}" onclick="setFlowDoc('${d}')">${DOC_META[d].icon} ${d}<small>${DOC_META[d].sub}</small></button>`).join("")}
    </div>
    <div class="notice">⚡ <b>真实多智能体生成 · ${esc(curDoc)}</b>&nbsp;六个智能体正按流水线真实运行（DeepSeek 驱动，每个字段带来源与依据）；约 15~40 秒。${curDoc === "首次病程记录" ? "鉴别诊断/诊疗计划为知识辅助（灰源），须医生核定。" : "无录音模式下绿字段有限——这正是录音补采的意义。"}</div>
    <div class="gen-progress"><i id="gen-bar"></i></div>
    <div class="ag-grid">
      ${AGENTS.map((a, i) => `
        <div class="card ag-card" id="ag-${i}">
          <div class="bar"></div>
          <div class="ag-icon">${a.icon}</div>
          <div class="ag-name">${a.name}<span class="ag-status" id="ag-st-${i}">等待</span></div>
          <div class="ag-out" id="ag-out-${i}">—</div>
        </div>`).join("")}
    </div>
    <div class="gen-done-cta" id="gen-done"></div>`;
  // 流式消费 NDJSON（真实六智能体事件；服务端错误如实展示，绝不本地伪造智能体动画）
  // P2-E：转写过程稿随请求上传（此前演示链路未传——"对话→病历字段"实为断链，已修复）
  fetch("api/generate-draft", { method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ code: "DA0001", admission: 1, doc: curDoc, transcript: transcriptText(key) }) })
    .then(resp => {
      if (!resp.ok){
        return resp.json().catch(() => ({})).then(d => {
          throw new Error(d.error || `AI 服务返回 ${resp.status}`);
        });
      }
      const reader = resp.body.getReader(), dec = new TextDecoder();
      let buf = "";
      const pump = () => reader.read().then(({ done, value }) => {
        if (done) return;
        buf += dec.decode(value, { stream: true });
        let nl;
        while ((nl = buf.indexOf("\n")) >= 0){
          const line = buf.slice(0, nl).trim(); buf = buf.slice(nl + 1);
          if (!line) continue;
          try { handleAgentEvent(JSON.parse(line), key); } catch (e) {}
        }
        return pump();
      });
      return pump();
    })
    .catch(err => {
      const done = document.getElementById("gen-done");
      const msg = err && err.message ? err.message : "无法连接 AI 服务（本地静态环境无此接口）";
      if (done) done.innerHTML = `<div class="notice">⚠ ${esc(msg)}。线上部署（已配置 DeepSeek）：http://60.205.204.162/mra/</div>`;
    });
}
function setFlowDoc(doc){
  const st = flowState(flowKey("DA0001", 1));
  st.currentDoc = doc; persistFlow(st.key || flowKey("DA0001", 1));
  viewAdmission("DA0001", 1);
}
function startFlowDoc(doc){
  const key = flowKey("DA0001", 1), st = flowState(key);
  st.currentDoc = doc;
  st.step = 2;  // 从工作台直达生成步骤（已有草稿会直接显示结果页）
  persistFlow(key);
  location.hash = "#/adm/DA0001/1";
}
function handleAgentEvent(ev, key){
  const st = flowState(key);
  if (ev.doc && (st.currentDoc || "入院记录") !== ev.doc) return;  // 串流保护
  if (ev.event === "agent_start"){
    const i = AGENT_KEYS.indexOf(ev.agent);
    if (i >= 0){
      const card = document.getElementById(`ag-${i}`);
      if (card){ card.classList.add("run"); document.getElementById(`ag-st-${i}`).textContent = "运行中…"; }
      const bar = document.getElementById("gen-bar");
      if (bar) bar.style.width = `${Math.round((i / AGENT_KEYS.length) * 100)}%`;
    }
  } else if (ev.event === "agent_done"){
    const i = AGENT_KEYS.indexOf(ev.agent);
    if (i >= 0){
      const card = document.getElementById(`ag-${i}`);
      if (card){ card.classList.remove("run"); card.classList.add("ok"); }
      const stEl = document.getElementById(`ag-st-${i}`);
      if (stEl) stEl.textContent = `✓ ${(ev.ms / 1000).toFixed(1)}s`;
      const out = document.getElementById(`ag-out-${i}`);
      if (out){
        out.textContent = ev.summary;
        if (ev.warnings && ev.warnings.length)
          out.insertAdjacentHTML("beforeend", `<div style="margin-top:6px;color:var(--yellow);font-size:11px">⚠ ${esc(ev.warnings[0]).slice(0, 88)}…</div>`);
      }
      const bar = document.getElementById("gen-bar");
      if (bar) bar.style.width = `${Math.round(((i + 1) / AGENT_KEYS.length) * 100)}%`;
    }
  } else if (ev.event === "final"){
    const doc = ev.doc || st.currentDoc || "入院记录";
    setAgentResult(st, doc, { fields: ev.fields, stats: ev.stats });
    st.currentDoc = doc;
    persistFlow(key);
    renderAgentResult(document.getElementById("step-body"), key, getAgentResult(st, doc), doc);
  } else if (ev.event === "error"){
    const done = document.getElementById("gen-done");
    if (done) done.innerHTML = `<div class="notice">⚠ 生成失败：${esc(ev.error)}</div>`;
  }
}
function renderAgentResult(body, key, result, doc){
  const s = result.stats;
  const docName = doc || "入院记录";
  body.innerHTML = `
    <div class="gen-progress"><i style="width:100%"></i></div>
    <div class="ag-grid">
      ${AGENTS.map((a, i) => {
        const tr = (result.trace || [])[i];
        return `<div class="card ag-card ok"><div class="bar"></div>
          <div class="ag-icon">${a.icon}</div>
          <div class="ag-name">${a.name}<span class="ag-status">${tr ? "✓ " + (tr.ms / 1000).toFixed(1) + "s" : "✓"}</span></div>
          <div class="ag-out">${tr ? esc(tr.summary) : "—"}</div></div>`;
      }).join("")}
    </div>
    <div class="card" style="padding:14px 20px;margin-bottom:16px">
      <b style="font-family:var(--serif)">生成统计（真实流水线 · ${esc(docName)}）</b>
      <div style="font-size:12.5px;color:var(--ink-2);margin-top:6px;line-height:1.9">
        真实草稿 ${s.passed_fields} 字段（蓝 ${s.by_source.blue} / 绿 ${s.by_source.green} / 灰 ${s.by_source.gray}）·
        总耗时 ${(s.total_ms / 1000).toFixed(1)}s · 降级智能体：${s.degraded_agents.length ? esc(s.degraded_agents.join("、")) : "无"}
      </div>
    </div>
    <div class="gen-done-cta">
      <div style="display:flex;gap:10px;flex-wrap:wrap;justify-content:center">
        <button class="btn primary big" id="to-confirm">进入四色确认（真草稿）→</button>
        <button class="btn big" id="regen">↻ 重新生成</button>
      </div>
    </div>`;
  document.getElementById("to-confirm").onclick = () => { flowState(key).step = 3; persistFlow(key); viewAdmission("DA0001", 1); };
  document.getElementById("regen").onclick = () => {
    const st2 = flowState(key);
    if (st2.agentResults) delete st2.agentResults[docName];
    if (docName === "入院记录") st2.agentResult = null;
    persistFlow(key);
    renderStepGenerate(document.getElementById("step-body"), key, getAdm("DA0001", 1));
  };
}

/* ---------------- Step3 四色确认 ---------------- */
function draftToDoc(result, admRec, docName){
  // 真草稿 → 确认页 doc 结构（金标准 doc 作模板兜底：字段标签集与下拉信息）
  const gold = admRec.docs.find(d => d.type === docName)
    || admRec.docs.find(d => d.type === "病程记录" && (d.title || "").includes(docName))
    || admRec.docs.find(d => d.type === "入院记录");
  const byLabel = {};
  result.fields.forEach(f => byLabel[f.label] = f);
  const fields = gold.fields.map((f, i) => {
    const hit = f.label && byLabel[f.label];
    return hit ? { label: f.label, value: hit.value, binding: (hit.binding === "auto" ? f.binding : hit.binding) || f.binding,
                   dropdown: f.dropdown, _src: hit.source, _basis: hit.basis, _i: i }
               : { ...f, _i: i, _src: null };
  });
  // LLM 生成但模板中不存在的字段（如首程五小节）追加展示
  result.fields.forEach(f => {
    if (f.label && !fields.some(x => x.label === f.label))
      fields.push({ label: f.label, value: f.value, binding: f.binding, _src: f.source, _basis: f.basis, _i: fields.length, _new: true });
  });
  return { type: docName, title: gold.title, ts: gold.ts, fields, _real: true };
}
function renderStepConfirm(body, key, adm){
  const st = flowState(key);
  const admRec = getPatient("DA0001").admissions[0];
  const curDoc = st.currentDoc || "入院记录";
  const res = getAgentResult(st, curDoc);
  const doc = res ? draftToDoc(res, admRec, curDoc)
    : admRec.docs.find(d => d.type === curDoc)
      || admRec.docs.find(d => d.type === "病程记录" && (d.title || "").includes(curDoc))
      || admRec.docs.find(d => d.type === "入院记录");
  const qc = res && res.stats ? res.stats.qc : null;
  const labeled = doc.fields.map((f, i) => ({ ...f, _i: i })).filter(f => f.label);
  // 冲突检测
  const byLabel = {};
  labeled.forEach(f => (byLabel[f.label] = byLabel[f.label] || []).push(f));
  const conflictLabels = new Set(Object.entries(byLabel).filter(([l, fs]) => new Set(fs.map(f => String(f.value).trim()).filter(v => v)).size > 1).map(([l]) => l));

  const sections = [];
  SECTION_ORDER.forEach(sn => {
    const fs = labeled.filter(f => sectionOf(f) === sn);
    if (fs.length) sections.push({ name: sn, fields: fs });
  });
  const cCount = { blue: 0, green: 0, gray: 0, yellow: 0 };
  labeled.forEach(f => cCount[colorOf(f)]++);
  const filters = [
    ["all", "全部字段"], ["review", "待核对（黄+绿）"], ["yellow", "仅缺失（黄）"],
  ];
  const keep = c => st.filter === "review" ? (c === "green" || c === "yellow")
    : st.filter === "yellow" ? c === "yellow" : true;

  body.innerHTML = `
    ${curDoc !== "入院记录" ? `<div class="notice" style="margin-bottom:14px">📝 当前核对文书：<b>${esc(curDoc)}</b>${res ? "" : "（AI 草稿未生成，以下为金标准演示值）"}</div>` : ""}
    ${qc ? `<div class="qc-report">
      <div class="qc-head" onclick="const d=document.getElementById('qc-detail');d.style.display=d.style.display==='none'?'block':'none'">
        🛡 <b>质控校验报告</b>：${esc(qc.summary)}<span class="qc-arrow">▾ 明细</span></div>
      <div class="qc-detail" id="qc-detail" style="display:none">
        ${qc.blocked.length ? qc.blocked.map(b => `<div class="qc-item bad">⛔ <b>${esc(b.label)}</b> — ${esc(b.reason)}</div>`).join("") : `<div class="qc-item ok">✓ 无拦截修正项</div>`}
        ${(qc.missing || []).map(m => `<div class="qc-item warn">⚠ 缺失必填：${esc(m)}（AI 未生成，严禁臆造——请医生补录或手工书写）</div>`).join("")}
        ${(qc.warnings || []).map(w => `<div class="qc-item warn">⚠ ${esc(w)}</div>`).join("")}
      </div>
    </div>` : ""}
    <div class="legend">
      ${Object.entries(COLOR_META).map(([k, m]) => `<span><span class="cdot ${m.dot}"></span>${m.name} —— ${m.tip}</span>`).join("")}
    </div>
    <div class="confirm-stats sticky-stats">
      ${filters.map(([k, name]) => `<div class="cs-item ${st.filter === k ? "filter-on" : ""}" style="cursor:pointer" onclick="setConfirmFilter('${k}')">${name}</div>`).join("")}
      <div class="cs-item clickable" onclick="toggleAllSections(true)">展开全部</div>
      <div class="cs-item clickable" onclick="toggleAllSections(false)">收起全部</div>
      ${Object.entries(COLOR_META).map(([k, m]) => `<div class="cs-item clickable" onclick="jumpToColor('${k}')"><span class="dot ${m.dot}"></span>${m.name}<b>${cCount[k]}</b></div>`).join("")}
      <div class="cs-item" style="color:var(--yellow);border-color:var(--yellow-line);background:var(--yellow-bg)">⚠ ${conflictLabels.size} 组取值冲突 · 点颜色定位字段</div>
    </div>
    <div id="sec-list">
    ${sections.map(sec => {
      const vis = sec.fields.filter(f => keep(colorOf(f)));
      if (!vis.length) return "";
      return `
      <div class="card sec-block open" data-sec="${esc(sec.name)}">
        <div class="sec-head" onclick="this.parentElement.classList.toggle('open')">
          ${esc(sec.name)}<span class="cnt">${vis.length} 项</span><span class="arrow">▶</span>
        </div>
        <div class="sec-body">
          ${vis.map(f => fieldRow(f, st, conflictLabels)).join("")}
        </div>
      </div>`;
    }).join("") || `<div class="card" style="padding:30px;text-align:center;color:var(--green);font-size:14px">✓ 该筛选下没有需要处理的项目</div>`}
    </div>
    <div class="confirm-footer">
      <span style="font-size:12px;color:var(--ink-3)">
        修改留痕：<b id="editlog-count">${st.editLog.length}</b> 处（您的每次修改将记录改前/改后并回流用于模型优化）
        ${st.editLog.length ? `<button class="mini-btn" style="margin-left:6px" onclick="toggleEditLog()">查看留痕</button>` : ""}
      </span>
      <div style="display:flex;gap:10px">
        <button class="btn big" onclick="exportDraftXML()">导出草稿 XML</button>
        <button class="btn primary big" id="confirm-btn">✓ 医生确认无误，提交归档</button>
      </div>
    </div>
    <div id="editlog-panel"></div>`;

  window._confirmCtx = { body, key, adm };
  document.getElementById("confirm-btn").onclick = () => {
    const doctor = window.prompt("请输入确认医生姓名（将作为签章写入留痕与回执）：", st.confirmedBy || "王医生");
    if (!doctor) return;
    const miss = cCount.yellow;
    const real = S.user && !S.user.demo;
    const tip = real
      ? "签章医生：" + doctor + "\n确认将本份《" + doc.type + "》提交归档？" + (miss ? `\n\n⚠ 尚有 ${miss} 项缺失待补（黄色），归档后将按院内流程补录。` : "") + "\n\n提交后进入审签流程：上级医师签发 / 退回。"
      : "签章医生：" + doctor + "\n确认将本份《入院记录》草稿归档至院内系统？" + (miss ? `\n\n⚠ 尚有 ${miss} 项缺失待补（黄色），归档后将按院内流程补录。` : "") + "\n（演示环境为模拟归档）";
    if (window.confirm(tip)){
      submitArchive(key, doc, doctor, real);
    }
  };
  window._draftDoc = doc;
  window._draftEdits = st.edits;
}
/* P2-G 真归档：登录用户提交归档包至服务端（审签流入口）；失败降级为演示归档 */
async function submitArchive(key, doc, doctor, real){
  const st = flowState(key);
  if (real){
    const admRec = getPatient("DA0001").admissions[0];
    const agentRes = getAgentResult(st, doc.type);
    const payload = {
      code: "DA0001", admission: 1, doc: doc.type, disease: (admRec.disease || "").replace(/（.*）$/, ""),
      fields: doc.fields,
      qc: (agentRes && agentRes.stats && agentRes.stats.qc) || {},
      edits: st.editLog || [],
      xml: buildXML(doc)
    };
    try{
      const r = await fetch("api/records/submit", { method: "POST",
        headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload) });
      const d = await r.json().catch(() => ({}));
      if (r.ok && d.ok){
        const st2 = flowState(key);
        st2.step = 4; st2.confirmedBy = doctor; st2.archive = d; persistFlow(key);
        viewAdmission("DA0001", 1);
        return;
      }
      alert("归档服务返回错误：" + (d.error || "未知") + "（本次降级为演示归档）");
    } catch (e) {
      alert("归档服务不可达（" + e.message + "），本次降级为演示归档");
    }
  }
  const st2 = flowState(key); st2.step = 4; st2.confirmedBy = doctor; persistFlow(key); viewAdmission("DA0001", 1);
}
function setConfirmFilter(k){
  const st = flowState("DA0001_1"); st.filter = k; persistFlow("DA0001_1");
  const ctx = window._confirmCtx; if (ctx) renderStepConfirm(ctx.body, ctx.key, ctx.adm);
}
function toggleAllSections(open){
  document.querySelectorAll(".sec-block").forEach(s => s.classList.toggle("open", open));
}
function jumpToColor(color){
  const el = document.querySelector(`.f-row[data-color="${color}"]`);
  if (!el){ alert("该颜色下没有字段（或在当前筛选中被隐藏）"); return; }
  el.scrollIntoView({ behavior: "smooth", block: "center" });
  el.classList.remove("flash"); void el.offsetWidth; el.classList.add("flash");
}
function fieldRow(f, st, conflictLabels){
  const c = f._src || colorOf(f);
  const fid = `${f._i}`;
  const val = st.edits[fid] !== undefined ? st.edits[fid] : f.value;
  const coded = f.dropdown && /^\d+$/.test(String(val).trim());
  const empty = !String(val).trim();
  // 溯源：从对话中找到出现该字段标签的轮次
  const srcTurns = S.dialogue.filter(t => t.mapsTo.includes(f.label));
  return `<div class="f-row c-${c}" id="frow-${fid}" data-color="${c}">
    <div class="lb"><span class="cdot ${c}"></span>${esc(f.label)}${f.dropdown ? ' <span class="tag" style="background:var(--paper-2);color:var(--ink-3)">受控</span>' : ""}</div>
    <div>
      ${empty ? `<div class="vv" style="color:var(--yellow)">（缺失 · 请补充或核实后填写）</div>`
        : coded ? `<div class="vv"><span style="font-family:var(--mono);color:var(--ink-3)">〔受控码〕</span>${esc(val)}</div>`
        : `<div class="vv" id="fv-${fid}">${esc(val)}</div>`}
      <div id="edit-zone-${fid}"></div>
      <div id="src-${fid}"></div>
      ${f._basis ? `<div style="margin-top:5px;font-size:11px;color:var(--ink-3)">📎 依据：${esc(f._basis)}</div>` : ""}
      ${conflictLabels.has(f.label) ? `<div style="margin-top:6px;font-size:11.5px;color:var(--yellow)">⚠ 质控提示：模板内同名字段存在不同取值（嵌套模板分支），请医生裁定保留哪项</div>` : ""}
    </div>
    <div class="ops">
      ${srcTurns.length ? `<button class="mini-btn" onclick="toggleSrc('${fid}',${esc(JSON.stringify(srcTurns.map(t => S.dialogue.indexOf(t))))})">溯源</button>` : ""}
      ${!empty && !coded ? `<button class="mini-btn" onclick="editField('${fid}')">修改</button>` : ""}
      ${empty ? `<button class="mini-btn warn" onclick="editField('${fid}')">补录</button>` : ""}
    </div>
  </div>`;
}
function toggleSrc(fid, turnIdxs){
  const box = document.getElementById(`src-${fid}`);
  if (box.innerHTML){ box.innerHTML = ""; return; }
  const idxs = JSON.parse(turnIdxs);
  box.innerHTML = `<div class="src-pop">${idxs.map(i => {
    const t = S.dialogue[i];
    return `<div class="who">转写稿 · ${t.role}（录音 00:${String(3 + i * 7).padStart(2, "0")}）</div>${esc(t.text)}`;
  }).join('<div style="height:6px"></div>')}</div>`;
}
function toggleEditLog(){
  const panel = document.getElementById("editlog-panel");
  if (panel.innerHTML){ panel.innerHTML = ""; return; }
  const st = flowState("DA0001_1");
  panel.innerHTML = `<div class="card" style="padding:14px 18px;margin-top:14px">
    <div class="md-title">修改留痕（${st.editLog.length} 条 · 随归档包存档，用于审计与模型反馈）</div>
    ${st.editLog.map((e, i) => `
      <div style="display:grid;grid-template-columns:34px 130px 1fr 1fr 70px;gap:10px;padding:7px 4px;border-bottom:1px dashed var(--line);font-size:12px;align-items:start">
        <span style="color:var(--ink-3)">${i + 1}</span>
        <span style="font-weight:600">${esc(e.field)}</span>
        <span style="color:var(--ink-3)">改前：${esc(e.before.slice(0, 60)) || "（空）"}</span>
        <span style="color:var(--green)">改后：${esc(e.after.slice(0, 60))}</span>
        <span style="color:var(--ink-3);font-family:var(--mono);font-size:10.5px">${esc(e.time)} ${esc(e.doctor)}</span>
      </div>`).join("")}
  </div>`;
  panel.scrollIntoView({ behavior: "smooth", block: "nearest" });
}
function exportEditLog(){
  const st = flowState("DA0001_1");
  const payload = { document: "入院记录_DA0001_20260902", confirmedBy: "王医生（住院医师）",
    exportedAt: new Date().toISOString(), editLog: st.editLog };
  const blob = new Blob([JSON.stringify(payload, null, 1)], { type: "application/json" });
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = "修改留痕_DA0001_入院记录.json";
  a.click(); URL.revokeObjectURL(a.href);
}
function editField(fid){
  const doc = window._draftDoc, edits = window._draftEdits;
  const f = doc.fields[+fid];
  const zone = document.getElementById(`edit-zone-${fid}`);
  if (zone.innerHTML){ zone.innerHTML = ""; return; }
  zone.innerHTML = `<textarea id="ta-${fid}">${esc(edits[fid] !== undefined ? edits[fid] : f.value)}</textarea>
    <div style="margin-top:6px;display:flex;gap:8px">
      <button class="mini-btn" style="background:var(--primary);border-color:var(--primary);color:#fff" onclick="saveEdit('${fid}')">保存修改</button>
      <button class="mini-btn" onclick="document.getElementById('edit-zone-${fid}').innerHTML=''">取消</button>
    </div>`;
}
function saveEdit(fid){
  const ta = document.getElementById(`ta-${fid}`);
  const doc = window._draftDoc, edits = window._draftEdits;
  const before = edits[fid] !== undefined ? edits[fid] : doc.fields[+fid].value;
  const after = ta.value;
  edits[fid] = after;
  // F8 反馈闭环：修改留痕（改前/改后/字段/时间），归档时可下载
  const st = flowState("DA0001_1");
  st.editLog.push({
    field: doc.fields[+fid].label || "（基本项）",
    before: String(before), after: String(after),
    time: new Date().toLocaleTimeString("zh-CN", { hour12: false }),
    doctor: "王医生",
  });
  persistFlow("DA0001_1");
  const vv = document.getElementById(`fv-${fid}`);
  if (vv) vv.textContent = after;
  document.getElementById(`edit-zone-${fid}`).innerHTML = "";
  const lb = document.querySelector(`#frow-${fid} .lb`);
  if (lb && !lb.querySelector(".edited-chip"))
    lb.insertAdjacentHTML("beforeend", '<span class="tag edited-chip" style="background:var(--green-bg);color:var(--green)">✓ 已更新</span>');
  const n = document.getElementById("editlog-count");
  if (n) n.textContent = st.editLog.length;
}

/* ---------------- Step4 归档 ---------------- */
function renderStepArchive(body, key, adm){
  const admRec = getPatient("DA0001").admissions[0];
  const curDoc = flowState(key).currentDoc || "入院记录";
  const doc = window._draftDoc
    || (getAgentResult(flowState(key), curDoc) ? draftToDoc(getAgentResult(flowState(key), curDoc), admRec, curDoc) : null)
    || admRec.docs.find(d => d.type === curDoc)
    || admRec.docs.find(d => d.type === "病程记录" && (d.title || "").includes(curDoc))
    || admRec.docs.find(d => d.type === "入院记录");
  const st = flowState(key);
  const labeled = doc.fields.filter(f => f.label);
  const xml = buildXML(doc);
  body.innerHTML = `
    <div class="arch-grid">
      <div class="card" style="padding:20px 24px">
        <div class="md-title" style="font-size:16px">归档预览 · XTextDocument（字段回填包 · ${esc(doc.type)}）</div>
        <div style="font-size:12px;color:var(--ink-3);margin-bottom:12px">按院内模板 DataSource/BindingPath 绑定规范回填 · 共 ${labeled.length} 个字段 · ${Object.keys(st.edits).length} 处医生修改已生效</div>
        <div class="xml-view">${esc(xml)}</div>
        <div style="margin-top:14px;display:flex;gap:10px">
          <button class="btn" onclick="downloadXML()">⬇ 下载归档包（模拟）</button>
          <button class="btn" onclick="exportEditLog()">⬇ 下载修改留痕（${st.editLog.length} 条）</button>
          <button class="btn ghost" onclick="flowState('${key}').step=3;viewAdmission('DA0001',1)">← 返回修改</button>
        </div>
      </div>
      <div class="card arch-receipt">
        <div class="receipt-check"><svg viewBox="0 0 24 24"><path d="M4 12.5l5 5L20 6.5"/></svg></div>
        ${st.archive ? `
        <div style="font-family:var(--serif);font-size:20px;font-weight:700;color:var(--green);margin-bottom:4px">已提交归档 · 待上级医师审签</div>
        <div style="font-size:12px;color:var(--ink-3);margin-bottom:16px">已入审签流：上级医师签发 / 退回 → 质控科抽查（P2-G）</div>
        <div class="receipt-line"><span>归档编号</span><b>${esc(st.archive.record_no || "—")}</b></div>
        <div class="receipt-line"><span>文书类型</span><b>${esc(doc.type)} · ${esc(admRec.disease || "").replace(/（.*）$/, "")}</b></div>
        <div class="receipt-line"><span>确认医生（签章）</span><b>${esc((st.confirmedBy || "王医生") + "（住院医师）")}</b></div>
        <div class="receipt-line"><span>当前状态</span><b>${esc(st.archive.status_name || "待上级医师审签")}</b></div>
        <div class="receipt-line"><span>医生修改</span><b>${st.editLog.length} 处（留痕已随包存档）</b></div>
        <div class="receipt-line"><span>留痕</span><b>生成/修改/确认/审签 全程可追溯</b></div>
        <div style="margin-top:18px;display:flex;gap:10px;flex-wrap:wrap;justify-content:center">
          <button class="btn primary big" onclick="location.hash='#/review'">查看审签进度 →</button>
          <button class="btn big" onclick="location.hash='#/workbench'">返回工作台</button>
        </div>` : `
        <div style="font-family:var(--serif);font-size:20px;font-weight:700;color:var(--green);margin-bottom:4px">已归档（模拟）</div>
        <div style="font-size:12px;color:var(--ink-3);margin-bottom:16px">登录账号提交可进入真实审签流（上级医师审签 + 质控科抽查）</div>
        <div class="receipt-line"><span>归档编号</span><b>AR-20260902-0047</b></div>
        <div class="receipt-line"><span>文书类型</span><b>入院记录 · 慢性阑尾炎</b></div>
        <div class="receipt-line"><span>确认医生（签章）</span><b>${esc((flowState(key).confirmedBy || "王医生") + "（住院医师）")}</b></div>
        <div class="receipt-line"><span>生成方式</span><b>多智能体草稿 + 医生确认</b></div>
        <div class="receipt-line"><span>医生修改</span><b>${st.editLog.length} 处（留痕已随包存档）</b></div>
        <div class="receipt-line"><span>留痕</span><b>生成/修改/确认 全程可追溯</b></div>
        <div style="margin-top:18px;display:flex;gap:10px;flex-wrap:wrap;justify-content:center">
          <button class="btn primary big" onclick="location.hash='#/workbench'">返回工作台</button>
          <button class="btn big" onclick="location.hash='#/patient/DA0001'">在患者全景中查看</button>
        </div>`}
      </div>
    </div>`;
}
function buildXML(doc){
  const lines = [];
  lines.push(`<?xml version="1.0" encoding="UTF-8"?>`);
  lines.push(`<XTextDocument EditorVersionString="1.2020.8.24">`);
  lines.push(`  <ContentReadonly>False</ContentReadonly>`);
  lines.push(`  <XElements>`);
  doc.fields.filter(f => f.label).forEach(f => {
    const v = window._draftEdits && window._draftEdits[doc.fields.indexOf(f)] !== undefined
      ? window._draftEdits[doc.fields.indexOf(f)] : f.value;
    if (!String(v).trim()) return;
    lines.push(`    <Element xsi:type="XInputField">`);
    lines.push(`      <BackgroundText>${f.label}</BackgroundText>`);
    if (f.binding) lines.push(`      <ValueBinding><DataSource>${f.binding}</DataSource></ValueBinding>`);
    lines.push(`      <Value>${v}</Value>`);
    lines.push(`    </Element>`);
  });
  lines.push(`  </XElements>`);
  lines.push(`</XTextDocument>`);
  return lines.join("\n");
}
function downloadXML(){
  const xml = buildXML(window._draftDoc);
  const blob = new Blob([xml], { type: "application/xml" });
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = "入院记录_DA0001_20260902_草稿.xml";
  a.click(); URL.revokeObjectURL(a.href);
}
function exportDraftXML(){ downloadXML(); }

/* ================================================================ */
/* 审签工作台（P2-G 多角色与审签流）                                  */
/* 住院医师：本人文书 + 审签/退回状态；上级医师：签发/退回；质控科：抽查  */
/* ================================================================ */
const RV_STATUS = {
  submitted: { name: "待审签", bg: "var(--yellow-bg)", fg: "var(--yellow)", line: "var(--yellow-line)" },
  signed:    { name: "已审签", bg: "var(--green-bg)", fg: "var(--green)", line: "var(--green-line)" },
  rejected:  { name: "已退回", bg: "#fdeceb", fg: "var(--red)", line: "#f2c4bd" }
};
const RV_SPOT = { ok: { name: "抽查合格", fg: "var(--green)" }, issue: { name: "抽查缺陷", fg: "var(--red)" } };
let rvFilter = "";
let rvView = "list";   // 审签工作台视图：list=文书列表 / edits=修改回流（F8）

function viewReview(){
  if (!S.user){ location.hash = "#/login"; return; }
  const role = S.user.demo ? null : (S.user.role || "resident");
  const head = S.user.demo
    ? `<div class="notice" style="margin-top:18px">您处于<b>演示模式</b>——审签工作台需医生账号。请退出后登录，或分别注册「上级医师」「质控科」角色体验完整审签流（归档提交也需账号）。</div>`
    : "";
  const tabs = ["", "submitted", "signed", "rejected"].map(s =>
    `<a class="rv-tab ${rvView === "list" && rvFilter === s ? "on" : ""}" onclick="rvSetFilter('${s}')">${s ? RV_STATUS[s].name : "全部"} </a>`).join("")
    + `<a class="rv-tab ${rvView === "edits" ? "on" : ""}" onclick="rvSetView('edits')">🔄 修改回流</a>`;
  $app.innerHTML = `
    <div class="page-head">
      <div class="page-title">审签工作台<small>${
        S.user.demo ? "演示模式（需登录）" :
        role === "attending" ? "上级医师 · 签名责任方：签发或退回（PRD 2.1 责任矩阵）" :
        role === "qc" ? "质控科 · 病历抽查：合格 / 缺陷标记" :
        "住院医师 · 我的归档与审签进度（退回原因在此回流）"}</small></div>
    </div>
    ${head}
    <div id="rv-body" style="${S.user.demo ? "display:none" : ""}">
      <div class="rv-tabs">${tabs}</div>
      ${rvView === "edits" ? `
      <div id="rv-edits" style="text-align:center;padding:28px;color:var(--ink-3)">加载修改回流…</div>` : `
      <div id="rv-stats" class="rv-stats"></div>
      <div class="card" style="padding:0;overflow:hidden">
        <table class="fm rv-table">
          <thead><tr><th>归档编号</th><th>文书 / 病种</th><th>书写医生</th><th>字段（黄/总）</th><th>审签状态</th><th>抽查</th><th>提交时间</th><th style="width:90px">操作</th></tr></thead>
          <tbody id="rv-rows"><tr><td colspan="8" style="text-align:center;padding:28px;color:var(--ink-3)">加载中…</td></tr></tbody>
        </table>
      </div>`}
    </div>`;
  if (!S.user.demo){
    if (rvView === "edits") loadReviewEdits();
    else loadReviewList();
  }
}
function rvSetFilter(s){ rvFilter = s; rvView = "list"; viewReview(); }
function rvSetView(v){ rvView = v; viewReview(); }

/* 修改回流（F8 数据闭环）：医生修改留痕聚合 → 字段热点 + 四色来源 → 模型反馈 */
const RV_COLOR = {
  green: { dot: "green", note: "对话提炼被改多 → 抽取/提示词待优化" },
  blue:  { dot: "blue",  note: "HIS 带入被改多 → 映射规则待校准" },
  gray:  { dot: "gray",  note: "模板常规被改多 → 规范默认待核对" },
  yellow:{ dot: "yellow",note: "待补字段被填写 → 录音依赖/补录流程" },
  unknown:{ dot: "gray", note: "字段未在模板中（新增/改标签）" }
};
async function loadReviewEdits(){
  const box = document.getElementById("rv-edits");
  if (!box) return;
  try{
    const r = await fetch("api/records/edits");
    if (r.status === 401){ S.user = null; renderUserArea(); location.hash = "#/login"; return; }
    const d = await r.json();
    if (!d.ok) throw new Error(d.error || "加载失败");
    const t = d.totals || {}, bc = d.by_color || {}, note = d.color_note || {};
    const hot = d.hot || [], recent = d.recent || [];
    const colorChips = ["green","blue","gray","yellow","unknown"].filter(c => bc[c])
      .map(c => `<span class="tag" style="background:var(--paper-2);color:var(--ink-2)"><span class="cdot ${RV_COLOR[c].dot}"></span>${({green:"绿·对话",blue:"蓝·HIS",gray:"灰·模板",yellow:"黄·待补",unknown:"未在模板"})[c]} <b>${bc[c]}</b><small style="color:var(--ink-3)">（${esc(note[c] || RV_COLOR[c].note)}）</small></span>`).join("");
    box.innerHTML = `
      <div class="rv-stats">
        <span class="tag" style="background:var(--blue-bg);color:var(--blue);border:1px solid var(--blue-line)">修改总次数 <b>${t.edits || 0}</b></span>
        <span class="tag" style="background:var(--paper-2);color:var(--ink-2)">涉及归档 <b>${t.archives_with_edits || 0}</b></span>
        <span class="tag" style="background:var(--paper-2);color:var(--ink-2)">涉及字段 <b>${t.fields_touched || 0}</b></span>
        <button class="mini-btn" style="margin-left:auto" onclick="downloadEditsPack()">⬇ 下载修改回流包（模型反馈用）</button>
      </div>
      ${colorChips ? `<div class="rv-stats">${colorChips}</div>` : ""}
      <div class="card" style="padding:0;overflow:hidden;margin-bottom:14px">
        <table class="fm rv-table">
          <thead><tr><th>字段热点</th><th>来源</th><th>修改次数</th><th>典型改前 → 改后</th></tr></thead>
          <tbody>${hot.length ? hot.map(h => `
            <tr>
              <td><b>${esc(h.label)}</b></td>
              <td><span class="cdot ${RV_COLOR[h.color]?.dot || "gray"}"></span> ${({green:"绿·对话",blue:"蓝·HIS",gray:"灰·模板",yellow:"黄·待补",unknown:"未在模板"})[h.color] || h.color}</td>
              <td><b>${h.count}</b></td>
              <td style="color:var(--ink-2)">${h.samples && h.samples.length ? h.samples.slice(0, 1).map(sm =>
                `<span style="color:var(--red)">「${esc(sm.before || "（空）")}」</span> → <span style="color:var(--green)">「${esc(sm.after || "（空）")}」</span>
                 <span style="color:var(--ink-3);font-size:10.5px">（${esc(sm.record_no)} · ${esc((sm.at || "").slice(0, 16))}）</span>`).join("") : "—"}</td>
            </tr>`).join("") : `<tr><td colspan="4" style="text-align:center;padding:28px;color:var(--ink-3)">暂无修改留痕——医生在「四色确认」步骤的每次修改（改前/改后/字段/时间）会随归档包入库并聚合在此</td></tr>`}</tbody>
        </table>
      </div>
      <div class="md-title" style="font-size:14px;margin:2px 0 8px">最近修改流（跨归档 · 按时间倒序）</div>
      <div class="card" style="padding:12px 16px">${recent.length ? recent.map(e => `
        <div style="font-size:12.5px;line-height:1.9;color:var(--ink-2)">
          <span class="mono" style="color:var(--ink-3)">${esc(e.record_no)}</span> · ${esc(e.doc_type)} · <b>${esc(e.field)}</b>：
          <span style="color:var(--red)">「${esc(e.before || "（空）")}」</span>→<span style="color:var(--green)">「${esc(e.after || "（空）")}」</span>
          <span style="color:var(--ink-3)">· ${esc(e.doctor || "")} · ${esc((e.at || "").slice(0, 16))}</span>
        </div>`).join("") : `<span style="color:var(--ink-3);font-size:12.5px">（暂无）</span>`}</div>
      <div class="notice" style="margin-top:14px">🔁 <b>回流用途</b>：修改热点是模型迭代的直接依据——绿字段热点指向抽取/提示词优化，蓝字段热点指向 HIS 映射校准，灰字段热点指向模板常规核对；随双周回归对照热点是否收敛（PRD F8 数据闭环）。</div>`;
  } catch (e) {
    box.innerHTML = `<div class="notice">⚠ ${esc(e.message || e)}</div>`;
  }
}
async function downloadEditsPack(){
  try{
    const r = await fetch("api/records/edits");
    const d = await r.json();
    const blob = new Blob([JSON.stringify(d, null, 1)], { type: "application/json" });
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = `修改回流包_模型反馈_${new Date().toISOString().slice(0, 10)}.json`;
    a.click(); URL.revokeObjectURL(a.href);
  } catch (e) { alert("下载失败：" + (e.message || e)); }
}
async function loadReviewList(){
  const body = document.getElementById("rv-body");
  if (!body) return;
  try{
    const r = await fetch("api/records" + (rvFilter ? "?status=" + rvFilter : ""));
    if (r.status === 401){ S.user = null; renderUserArea(); location.hash = "#/login"; return; }
    const d = await r.json();
    if (!d.ok) throw new Error(d.error || "加载失败");
    const st = d.stats || {};
    document.getElementById("rv-stats").innerHTML = `
      <span class="tag" style="background:var(--yellow-bg);color:var(--yellow);border:1px solid var(--yellow-line)">待审签 ${st.submitted || 0}</span>
      <span class="tag" style="background:var(--green-bg);color:var(--green);border:1px solid var(--green-line)">已审签 ${st.signed || 0}</span>
      <span class="tag" style="background:#fdeceb;color:var(--red);border:1px solid #f2c4bd">已退回 ${st.rejected || 0}</span>
      <span class="tag" style="background:var(--paper-2);color:var(--ink-2)">抽查合格 ${(d.spot || {}).ok || 0} · 缺陷 ${(d.spot || {}).issue || 0}</span>
      ${isReviewer(S.user) ? "" : `<span class="tag" style="background:var(--blue-bg);color:var(--blue);border:1px solid var(--blue-line)">仅显示本人文书</span>`}`;
    const rows = d.records || [];
    document.getElementById("rv-rows").innerHTML = rows.length ? rows.map(r => `
      <tr>
        <td class="mono">${esc(r.record_no)}</td>
        <td><b>${esc(r.doc_type)}</b><span style="color:var(--ink-3)"> · ${esc(r.disease || "—")}</span></td>
        <td>${esc(r.resident_name)}</td>
        <td>${r.yellow_cnt ? `<b style="color:var(--yellow)">${r.yellow_cnt}</b>` : "0"} / ${r.total_cnt}</td>
        <td><span class="tag" style="background:${RV_STATUS[r.status].bg};color:${RV_STATUS[r.status].fg};border:1px solid ${RV_STATUS[r.status].line}">${esc(RV_STATUS[r.status].name)}</span>
            ${r.status !== "submitted" && r.signed_name ? `<div style="font-size:10.5px;color:var(--ink-3);margin-top:2px">${esc(r.signed_name)}</div>` : ""}</td>
        <td style="color:${RV_SPOT[r.spot_result] ? RV_SPOT[r.spot_result].fg : "var(--ink-3)"}">${esc(RV_SPOT[r.spot_result] ? RV_SPOT[r.spot_result].name : "未抽查")}</td>
        <td style="color:var(--ink-3)">${esc((r.created_at || "").slice(0, 16))}</td>
        <td><button class="mini-btn" onclick="openReviewDetail(${r.id})">查看</button></td>
      </tr>`).join("")
      : `<tr><td colspan="8" style="text-align:center;padding:28px;color:var(--ink-3)">暂无文书——住院医师在「四色确认」步骤提交归档后出现在这里</td></tr>`;
  } catch (e) {
    const el = document.getElementById("rv-rows");
    if (el) el.innerHTML = `<tr><td colspan="8" style="text-align:center;padding:28px;color:var(--red)">${esc(e.message)}</td></tr>`;
  }
}
async function openReviewDetail(id){
  let d;
  try{
    const r = await fetch("api/records/detail?id=" + id);
    d = await r.json();
    if (!r.ok || !d.ok) throw new Error(d.error || "加载失败");
  } catch (e) { return alert("加载详情失败：" + e.message); }
  const rec = d.record;
  const colorCnt = { blue: 0, green: 0, gray: 0, yellow: 0 };
  (rec.fields || []).forEach(f => { colorCnt[colorOf(f)]++; });
  const fieldRows = (rec.fields || []).filter(f => f.label).slice(0, 80).map(f => {
    const c = colorOf(f);
    return `<div class="f-row c-${c}">
      <div class="lb"><span class="cdot ${c}"></span>${esc(f.label)}</div>
      <div class="vv">${esc(String(f.value || "").slice(0, 120)) || '<span style="color:var(--yellow)">（空 · 待补）</span>'}</div>
      <div class="ops"></div>
    </div>`;
  }).join("") || '<div style="padding:12px;color:var(--ink-3)">（无字段数据）</div>';
  const qc = rec.qc || {};
  const canSign = !S.user.demo && S.user.role === "attending" && rec.status === "submitted";
  const canSpot = !S.user.demo && S.user.role === "qc";
  document.getElementById("modal-root").innerHTML = `
    <div class="modal-mask" onclick="if(event.target===this)closeModal()">
      <div class="modal" style="max-width:860px">
        <div class="modal-head"><span class="t">${esc(rec.doc_type)} · ${esc(rec.disease || "")}</span>
          <span class="tag" style="background:${RV_STATUS[rec.status].bg};color:${RV_STATUS[rec.status].fg};border:1px solid ${RV_STATUS[rec.status].line}">${esc(RV_STATUS[rec.status].name)}</span>
          <span class="tag" style="background:var(--paper-2);color:var(--ink-3)">${esc(rec.record_no)}</span>
          <span class="x" onclick="closeModal()">✕</span></div>
        <div class="modal-body">
          <div class="rv-meta">
            <span>书写医生：<b>${esc(rec.resident_name)}</b></span>
            <span>提交时间：<b>${esc((rec.created_at || "").slice(0, 16))}</b></span>
            <span>字段：<b>${rec.total_cnt}</b>（黄 ${rec.yellow_cnt}）</span>
            <span>四色：🔵${colorCnt.blue} 🟢${colorCnt.green} ⚪${colorCnt.gray} 🟡${colorCnt.yellow}</span>
            <span>抽查：<b style="color:${RV_SPOT[rec.spot_result] ? RV_SPOT[rec.spot_result].fg : "var(--ink-3)"}">${esc(RV_SPOT[rec.spot_result] ? RV_SPOT[rec.spot_result].name : "未抽查")}</b>${rec.spot_by ? `（${esc(rec.spot_by)}）` : ""}</span>
          </div>
          ${rec.status === "rejected" ? `<div class="notice">↩ <b>上级医师退回原因：</b>${esc(rec.sign_comment || "（未填写）")}<br><span style="color:var(--ink-3)">修改后请在「四色确认」步骤重新提交，将生成新的归档编号。</span></div>` : ""}
          ${rec.status === "signed" && rec.sign_comment ? `<div class="src-pop" style="margin-bottom:14px">✓ <b>审签意见：</b>${esc(rec.sign_comment)}——${esc(rec.signed_name)}</div>` : ""}
          ${rec.spot_result && rec.spot_comment ? `<div class="src-pop" style="margin-bottom:14px">🔍 <b>抽查意见：</b>${esc(rec.spot_comment)}——${esc(rec.spot_by || "质控科")}</div>` : ""}
          ${qc.summary ? `<div class="rv-qc"><b>机器质控结论：</b>${esc(qc.summary)}${(qc.warnings || []).length ? "<br>⚠ " + qc.warnings.map(w => esc(w)).join("<br>⚠ ") : ""}${(qc.missing || []).length ? `<br>缺失待补：${qc.missing.map(m => esc(m)).join("、")}` : ""}</div>` : ""}
          ${(rec.edits || []).length ? `<div class="rv-qc">✎ <b>医生修改留痕 ${rec.edits.length} 处</b>：${rec.edits.slice(0, 5).map(e => `${esc(e.field || "")}「${esc(String(e.before || "").slice(0, 20))}」→「${esc(String(e.after || "").slice(0, 20))}」`).join("；")}${rec.edits.length > 5 ? " …" : ""}</div>` : ""}
          <div class="md-title" style="font-size:14px;margin:6px 0 2px">字段明细（四色）</div>
          ${fieldRows}
          <div style="display:flex;gap:10px;margin-top:14px;flex-wrap:wrap">
            ${canSign ? `<button class="btn primary" onclick="reviewAction(${rec.id}, 'sign')">✓ 签发（上级医师签章）</button>
            <button class="btn" style="border-color:var(--yellow-line);color:var(--yellow)" onclick="reviewAction(${rec.id}, 'reject')">↩ 退回修改</button>` : ""}
            ${canSpot ? `<button class="btn primary" onclick="spotAction(${rec.id}, 'ok')">🔍 抽查合格</button>
            <button class="btn" style="border-color:#f2c4bd;color:var(--red)" onclick="spotAction(${rec.id}, 'issue')">🔍 标记缺陷</button>` : ""}
            <button class="btn ghost" onclick="closeModal()">关闭</button>
          </div>
        </div>
      </div>
    </div>`;
}
async function reviewAction(id, action){
  let comment = "";
  if (action === "reject"){
    comment = window.prompt("退回原因（将回流给书写医生，必填）：") || "";
    if (!comment.trim()) return;
  }
  const r = await fetch("api/records/review", { method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ id, action, comment }) });
  const d = await r.json().catch(() => ({}));
  if (!r.ok || !d.ok) return alert("审签失败：" + (d.error || "未知错误"));
  closeModal();
  alert(action === "sign" ? "✓ 已签发（" + (d.signed_name || "") + "）" : "↩ 已退回，原因已回流给书写医生");
  loadReviewList();
}
async function spotAction(id, result){
  let comment = "";
  if (result === "issue"){
    comment = window.prompt("缺陷描述（将存档供书写医生整改）：") || "";
    if (!comment.trim()) return;
  }
  const r = await fetch("api/records/spotcheck", { method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ id, result, comment }) });
  const d = await r.json().catch(() => ({}));
  if (!r.ok || !d.ok) return alert("抽查失败：" + (d.error || "未知错误"));
  closeModal();
  alert(result === "ok" ? "✓ 已标记抽查合格" : "🔍 已标记抽查缺陷");
  loadReviewList();
}

/* ================================================================ */
/* 字段地图                                                          */
/* ================================================================ */
function viewFieldmap(filterText = ""){
  const fm = S.fieldmap;
  const rows = fm.fields.filter(f => !filterText || f.label.includes(filterText) || Object.keys(f.bindings).some(b => b.includes(filterText)));
  $app.innerHTML = `
    <div class="page-head">
      <div class="page-title">字段地图 v0<small>自动提取自 ${fm.totalDocs} 份真实文书 · ${fm.fields.length} 个字段标签 · 供院方确认（PRD Q2）</small></div>
    </div>
    <div class="fm-toolbar">
      <input id="fm-q" placeholder="搜索字段标签或绑定路径，如：主诉 / JWS / A01" value="${esc(filterText)}">
      <span class="tag" style="background:var(--primary-soft);color:var(--primary-deep);align-self:center">命中 ${rows.length} 项</span>
    </div>
    <div class="card fm-wrap">
      <table class="fm">
        <thead><tr><th style="width:220px">字段标签（BackgroundText）</th><th>绑定路径（DataSource）</th><th style="width:90px">受控下拉</th><th style="width:340px">出现于文书类型</th></tr></thead>
        <tbody>
          ${rows.map(f => `<tr>
            <td><b>${esc(f.label)}</b></td>
            <td>${Object.entries(f.bindings).slice(0, 6).map(([b, n]) => `<span class="mono">${esc(b)}</span><span style="color:var(--ink-3)"> ×${n}</span>`).join("，") || '<span style="color:var(--ink-3)">（无绑定 · 自由文本）</span>'}</td>
            <td>${f.dropdown ? '<span class="tag" style="background:var(--yellow-bg);color:var(--yellow)">含下拉</span>' : '<span style="color:var(--ink-3)">—</span>'}</td>
            <td style="color:var(--ink-2)">${f.docTypes.join(" · ")}</td>
          </tr>`).join("")}
        </tbody>
      </table>
    </div>`;
  document.getElementById("fm-q").oninput = e => {
    const v = e.target.value;
    clearTimeout(window._fmT);
    window._fmT = setTimeout(() => viewFieldmap(v), 250);
  };
  const q = document.getElementById("fm-q"); q.focus(); q.setSelectionRange(q.value.length, q.value.length);
}

/* ================================================================ */
/* 操作手册页                                                        */
/* ================================================================ */
function viewManual(){
  $app.innerHTML = `
  <div class="about-prose">
    <div class="page-title" style="margin-bottom:14px">医生操作手册<small>3 分钟上手 · 面向住院/管床医生</small></div>
    <div class="card kcard">
      <h2>一、它帮你做什么</h2>
      <p>把你<b>对病人说的、问的、查的</b>变成规范的住院病历草稿：你负责看病和说话，系统负责听、转写、组织成文，你最后核对签认。它不替你做诊断——<b>归档前必须经你确认</b>。</p>
      <h2>二、一次完整的使用（以入院记录为例）</h2>
      <ul>
        <li><b>第 1 步 · 选择病人</b>：工作台 → 我的患者 → 进入本次住院。系统已自动从 HIS 带入基本信息、检验检查、医嘱。</li>
        <li><b>第 2 步 · 问诊录音</b>：点击「开始录音」，像平常一样问诊。山东方言实时转成普通话文字，系统自动区分你和病人说的话。</li>
        <li><b>第 3 步 · AI 生成</b>：问诊结束点「结束问诊」，六个智能体自动工作（抽取你的话 → 汇总检验检查 → 检索诊疗规范 → 按本院模板写草稿 → 自查 → 对准模板字段），约十几秒。</li>
        <li><b>第 4 步 · 四色核对（核心）</b>：优先看<b>黄色</b>（缺什么补什么）和<b>绿色</b>（点「溯源」对照对话原文，不是你的意思就改）。要改点「修改」，每处修改自动留痕。</li>
        <li><b>第 5 步 · 确认归档</b>：点「医生确认无误，提交归档」→ 进入院内系统。中途关掉页面不会丢，回来接着做。</li>
      </ul>
      <h2>三、四种颜色怎么读</h2>
      <ul>
        <li>🔵 <b>蓝 · 系统带入</b>：来自 HIS 的数据（姓名、检验值等），抽查即可。</li>
        <li>🟢 <b>绿 · 对话提炼</b>：从你与病人的对话中提炼（主诉、现病史等），<b>点「溯源」对照录音原文</b>。</li>
        <li>⚪ <b>灰 · 规范所见</b>：按模板常规合成的查体所见与鉴别诊断，请过目核定。</li>
        <li>🟡 <b>黄 · 缺失待补</b>：对话里没提到、系统不敢写的——<b>宁缺毋造</b>，需要你补录。</li>
      </ul>
      <h2>四、常见问题</h2>
      <ul>
        <li><b>病人说方言听不准怎么办？</b>转写稿可手动修改后再生成；录音原声保留，可回放定位。</li>
        <li><b>写错了算谁的？</b>AI 只出草稿；确认前的修改都会记录改前改后；归档后走院内修改流程。责任主体始终是医生。</li>
        <li><b>数据安全吗？</b>患者数据加密存储、私有化部署不出院；生成、修改、确认、归档全程留痕可审计。</li>
      </ul>
      <div style="margin-top:16px"><button class="btn primary big" onclick="location.hash='#/workbench'">← 返回工作台开始使用</button></div>
    </div>
  </div>`;
}

/* ================================================================ */
/* 说明页                                                            */
/* ================================================================ */
function viewAbout(){
  $app.innerHTML = `
  <div class="about-prose">
    <div class="page-title" style="margin-bottom:6px">系统说明与演示边界</div>
    <div class="card kcard">
      <h2>这是什么</h2>
      <p>「AI 病历智能体」医生端网页工作台的<b>演示版（Demo）</b>，对应项目规划一期 P2 里程碑"端到端链路打通"。目标：让医生在 3 分钟内理解系统如何工作——<b>录音 → 方言转写 → 多智能体生成 → 四色确认 → 归档</b>。</p>
      <h2>演示边界（务必知悉）</h2>
      <ul>
        <li><b>数据</b>：源自院方提供的真实住院病历（金标准），已全部脱敏（患者A / 患者B；医生护士姓名、电话、详址已脱敏）。</li>
        <li><b>录音转写</b>：为按真实入院记录重构的模拟对话；正式环境接入方言 ASR 引擎（选型进行中）。</li>
        <li><b>AI 生成</b>：患者A 流程中的草稿由<b>真实六智能体流水线</b>现场生成（DeepSeek 驱动，逐字段携带来源/依据/置信度，QC 智能体拦截臆造）；无录音模式下绿字段有限属真实能力边界。</li>
        <li><b>归档</b>：模拟流程；正式环境经对接层回写院内电子病历系统（回写通路正与院方确认）。</li>
      </ul>
      <h2>四色确认（产品核心）</h2>
      <p>蓝 = 系统带入（HIS 数据）· 绿 = 对话提炼（可溯源到转写原文）· 灰 = 规范所见与知识辅助 · 黄 = 缺失待补（<b>严禁臆造，宁缺毋造</b>）。医生重点核对黄色与绿色，确认后归档，责任主体始终是医生。</p>
      <h2>安全与合规</h2>
      <p>全流程留痕（生成/修改/确认/归档记录操作人与时间）· 医生修改回流用于模型优化（数据闭环沉淀于院内）· 正式环境私有化部署，核心模型与数据不出院。</p>
    </div>
  </div>`;
}

/* ================================================================
   登录 / 注册 + 顶栏用户区 + 新手引导（首次登录自动播放）
   ================================================================ */
const DEMO_USER_KEY = "mra_demo_user";
const TOUR_KEY_DEMO = "mra_tour_demo";

function tourSteps(){
  return [
    { sel: "#tour-tasks", title: "① 今日书写任务", body: "AI 已按<b>六智能体流水线</b>生成入院记录草稿。点右侧「继续核对」进入逐字段核对；<b>法定时限倒计时</b>就在这一行——入院记录 24h 内必须完成。" },
    { sel: "#tour-patients", title: "② 我的患者 · 开始问诊", body: "点击患者卡片查看住院全景与历史文书。新患者从「▶ 开始入院问诊」进入：<b>问诊录音 → AI 生成草稿 → 四色确认 → 归档</b>，四步完成一份病历。" },
    { sel: "#tour-stats", title: "③ 工作量总览", body: "在院患者、住院记录、累计文书、待确认草稿一目了然，方便交班与自查。" },
    { sel: ".topnav", title: "④ 帮助与状态", body: "「手册」是医生操作手册，「说明」讲清系统能力边界（AI 只出草稿，医生确认后归档）。右上角圆点是 <b>AI 服务状态灯</b>——绿色表示在线。" },
    { sel: "#user-area", title: "⑤ 账号与引导",
      body: S.user && !S.user.demo
        ? "这里显示你的登录身份，可随时退出。本引导可从工作台右上角「▶ 新手引导」随时重看。"
        : "登录后你的身份会显示在这里；<b>首次登录会自动播放本引导</b>。现在点「下一步」完成引导，然后可点右上角「登录 / 注册」创建账号。" },
  ];
}
function demoUser(){
  try { return JSON.parse(localStorage.getItem(DEMO_USER_KEY)); } catch (e) { return null; }
}
const ROLE_NAMES_CN = { resident: "住院医师", attending: "上级医师", qc: "质控科" };
const roleName = u => (u && (u.role_name || ROLE_NAMES_CN[u.role])) || "住院医师";
const isReviewer = u => !!u && !u.demo && (u.role === "attending" || u.role === "qc");
function renderUserArea(){
  const el = document.getElementById("user-area");
  const nv = document.getElementById("nav-review");
  if (nv) nv.style.display = isReviewer(S.user) ? "" : "none";
  if (!el) return;
  if (S.user){
    const u = S.user;
    el.innerHTML = `<div style="display:flex;align-items:center;gap:10px">
      <div class="user-chip"><span class="avatar">${esc((u.name || u.username || "医").slice(0, 1))}</span><div><b>${esc(u.name || u.username)}</b><i>${esc(u.department || "")}${u.title ? " · " + esc(u.title) : ""}${u.demo ? "" : " · " + esc(roleName(u))}</i></div></div>
      <button class="btn ghost" style="color:rgba(238,247,250,.85);border-color:rgba(238,247,250,.35);padding:6px 12px" onclick="doLogout()">退出</button></div>`;
  } else {
    el.innerHTML = `<button class="btn primary" style="padding:7px 16px" onclick="location.hash='#/login'">登录 / 注册</button>`;
  }
}
async function authPost(path, data){
  const r = await fetch("api" + path, { method: "POST",
    headers: { "Content-Type": "application/json" }, body: JSON.stringify(data) });
  const d = await r.json().catch(() => ({}));
  return { ok: r.ok, d };
}
function viewAuth(mode){
  const isReg = mode === "register";
  document.querySelectorAll("[data-nav]").forEach(n => n.classList.remove("active"));
  $app.innerHTML = `
    <div class="auth-wrap">
      <div class="card auth-card">
        <div class="auth-brand"><div class="brand-mark">十</div>
          <div><div class="auth-h1">${isReg ? "注册医生账号" : "医生登录"}</div><div class="auth-sub">东阿县人民医院 · AI 病历智能体</div></div></div>
        <div class="auth-tabs">
          <a class="${isReg ? "" : "on"}" href="#/login">登 录</a>
          <a class="${isReg ? "on" : ""}" href="#/register">注 册</a>
        </div>
        <div id="auth-err" class="auth-err" style="display:none"></div>
        ${isReg ? `
        <label class="auth-lab">姓名<input id="au-name" maxlength="32" placeholder="真实姓名（病案签章用）"></label>
        <div class="auth-row">
          <label class="auth-lab">科室<select id="au-dept"><option>普外科</option><option>内科</option><option>外科</option><option>妇产科</option><option>儿科</option><option>急诊科</option><option>医务科（质控）</option><option>其他</option></select></label>
          <label class="auth-lab">职称<select id="au-title"><option>住院医师</option><option>主治医师</option><option>副主任医师</option><option>主任医师</option><option>护士</option><option>其他</option></select></label>
        </div>
        <label class="auth-lab">系统角色（对应病历责任矩阵）<select id="au-role">
          <option value="resident">住院医师 —— 书写、四色确认与提交归档</option>
          <option value="attending">上级医师 —— 病历审签（签发 / 退回）</option>
          <option value="qc">质控科 —— 病历抽查（合格 / 缺陷）</option>
        </select></label>` : ""}
        <label class="auth-lab">用户名（工号 / 手机号）<input id="au-user" maxlength="32" placeholder="3~32 位字母、数字或下划线"></label>
        <label class="auth-lab">密码<input id="au-pwd" type="password" maxlength="64" placeholder="至少 6 位" onkeydown="if(event.key==='Enter')submitAuth('${isReg ? "register" : "login"}')"></label>
        ${isReg ? `<label class="auth-lab">确认密码<input id="au-pwd2" type="password" maxlength="64" onkeydown="if(event.key==='Enter')submitAuth('register')"></label>` : ""}
        <button class="btn primary big" style="width:100%;justify-content:center;margin-top:6px" onclick="submitAuth('${isReg ? "register" : "login"}')">${isReg ? "注册并进入" : "登 录"}</button>
        <div class="auth-foot">
          <a href="#" onclick="enterDemo();return false">先逛逛，进入演示模式 →</a>
          <span>${isReg ? "已有账号？" : "没有账号？"}<a href="${isReg ? "#/login" : "#/register"}">${isReg ? "去登录" : "立即注册"}</a></span>
        </div>
      </div>
    </div>`;
  setTimeout(() => { const el = document.getElementById("au-user"); if (el) el.focus(); }, 60);
}
function authError(msg){
  const el = document.getElementById("auth-err");
  if (el){ el.textContent = "⚠ " + msg; el.style.display = "block"; }
}
async function submitAuth(mode){
  const err = document.getElementById("auth-err");
  if (err) err.style.display = "none";
  const val = id => { const el = document.getElementById(id); return el ? el.value.trim() : ""; };
  const username = val("au-user"), password = document.getElementById("au-pwd")?.value || "";
  if (mode === "register"){
    const name = val("au-name"), department = val("au-dept"), title = val("au-title"), role = val("au-role") || "resident";
    const pwd2 = document.getElementById("au-pwd2")?.value || "";
    if (password !== pwd2) return authError("两次输入的密码不一致");
    const { ok, d } = await authPost("/auth/register", { username, password, name, department, title, role });
    if (!ok) return authError(d.error || "注册失败，请稍后再试");
    S.user = d.user;
    afterAuth(true);
  } else {
    const { ok, d } = await authPost("/auth/login", { username, password });
    if (!ok) return authError(d.error || "登录失败，请稍后再试");
    S.user = d.user;
    afterAuth(false);
  }
}
function afterAuth(isNew){
  localStorage.removeItem(DEMO_USER_KEY);
  renderUserArea();
  location.hash = "#/workbench";
  if (isNew || !S.user.tour_done) setTimeout(maybeTour, 400);
}
async function doLogout(){
  try { await fetch("api/auth/logout", { method: "POST" }); } catch (e) {}
  S.user = null;
  renderUserArea();
  location.hash = "#/login";
}
function enterDemo(){
  localStorage.setItem(DEMO_USER_KEY, JSON.stringify({ name: "王医生", department: "普外科", title: "住院医师", role: "resident", role_name: "住院医师", demo: true }));
  S.user = demoUser();
  renderUserArea();
  location.hash = "#/workbench";
  setTimeout(maybeTour, 400);
}
function tourKey(){ return S.user && !S.user.demo ? "mra_tour_" + (S.user.username || "") : TOUR_KEY_DEMO; }
function maybeTour(){
  if (document.getElementById("tour-root")) return;
  if (S.user && S.user.demo){ if (localStorage.getItem(TOUR_KEY_DEMO) === "1") return; }
  else if (S.user && S.user.tour_done) return;
  Tour.start(tourSteps(), markTourDone);
}
function markTourDone(){
  localStorage.setItem(tourKey(), "1");
  if (S.user && !S.user.demo) fetch("api/auth/tour-done", { method: "POST" }).catch(() => {});
}
function startTour(replay){
  if (replay) localStorage.setItem(tourKey(), "0");
  Tour.start(tourSteps(), markTourDone);
}

/* ---------------- 启动 ---------------- */
(async function init(){
  await loadData();
  S.user = demoUser();          // 先按演示身份渲染，避免登录态闪变
  renderUserArea();
  route();
  // 会话恢复：服务端会话优先于演示身份；随后按需自动播放新手引导
  try {
    const r = await fetch("api/auth/me");
    if (r.ok){ const d = await r.json(); if (d && d.user){ S.user = d.user; renderUserArea(); } }
  } catch (e) {}
  const h = location.hash || "#/workbench";
  if (h === "#/" || h === "#/workbench") setTimeout(maybeTour, 700);
  // 顶栏 AI 服务状态灯（本地静态环境优雅降级为灰色）
  fetch("api/health").then(r => r.ok ? r.json() : null).then(d => {
    const el = document.getElementById("ai-status");
    if (el && d && d.ok){
      el.classList.add("on");
      el.title = "AI 服务在线 · " + (d.engine || "");
    } else if (el){
      el.title = "AI 服务离线（本地静态环境属正常）";
    }
  }).catch(() => { const el = document.getElementById("ai-status"); if (el) el.title = "AI 服务离线"; });
  // P2-E：ASR 引擎状态预取（录音页据此切换 真实录音 / 演示回放）
  fetch("api/asr/status").then(r => r.ok ? r.json() : null).then(d => { if (d && d.ok) S.asr = d; }).catch(() => {});
})();

/* ---------------- AI 病例总结（服务端 LLM 实时生成） ---------------- */
function showAISummary(code, idx){
  const adm = getAdm(code, idx);
  document.getElementById("modal-root").innerHTML = `
    <div class="modal-mask" onclick="if(event.target===this)closeModal()">
      <div class="modal">
        <div class="modal-head"><span class="t">⚡ AI 病例总结 · ${esc(getPatient(code).display)} 第 ${idx} 次住院</span>
          <span class="tag" style="background:var(--primary-soft);color:var(--primary-deep)">DeepSeek 实时生成 · 已脱敏数据</span>
          <span class="x" onclick="closeModal()">✕</span></div>
        <div class="modal-body" id="ai-sum-body">
          <div style="text-align:center;padding:46px 0;color:var(--ink-3);font-size:13px">
            <div class="wave on" style="height:26px">${"<i></i>".repeat(16)}</div>
            <div style="margin-top:10px">AI 正在阅读该次住院的文书流、医嘱、检验与检查报告…</div>
          </div>
        </div>
      </div>
    </div>`;
  fetch("api/generate-summary", { method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ code, admission: idx }) })
    .then(r => r.json().then(d => ({ ok: r.ok, d })))
    .then(({ ok, d }) => {
      const body = document.getElementById("ai-sum-body");
      if (!body) return;
      if (!ok || d.error){
        body.innerHTML = `<div class="notice" style="margin:10px 0 0">⚠ ${esc(d.error || "服务暂不可用")}</div>
          <p style="font-size:12.5px;color:var(--ink-3);line-height:1.8">注：AI 实时生成需云端部署环境（本地静态打开时不可用）。线上访问地址：http://60.205.204.162/mra/</p>`;
        return;
      }
      const html = d.summary.split(/\n/).filter(l => l.trim()).map(l =>
        l.trim().startsWith("1.") || l.trim().startsWith("2.") || l.trim().startsWith("3.") ||
        l.trim().startsWith("4.") || l.trim().startsWith("5.")
          ? `<p style="margin:10px 0 4px;font-weight:700;color:var(--primary-deep)">${esc(l.trim())}</p>`
          : `<p style="margin:0 0 6px;line-height:1.9">${esc(l.trim())}</p>`).join("");
      body.innerHTML = `<div class="md-title" style="font-size:14.5px">生成时间 ${esc(d.generated_at)} · AI 草稿仅供参考，须经医生核对</div>
        <div style="font-size:13.5px">${html}</div>
        <div style="margin-top:14px;display:flex;gap:8px">
          <button class="btn" onclick="copyAISummary()">复制文本</button>
        </div>
        <textarea id="ai-sum-raw" style="position:absolute;left:-9999px">${esc(d.summary)}</textarea>`;
    })
    .catch(() => {
      const body = document.getElementById("ai-sum-body");
      if (body) body.innerHTML = `<div class="notice" style="margin:10px 0 0">⚠ 无法连接 AI 服务（本地静态环境无此接口）。请访问线上部署：http://60.205.204.162/mra/</div>`;
    });
}
function copyAISummary(){
  const ta = document.getElementById("ai-sum-raw");
  ta.select();
  navigator.clipboard.writeText(ta.value).then(() => {
    alert("已复制到剪贴板");
  });
}
