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
  const navMap = { workbench: "工作台", manual: "手册", about: "说明" };
  const navEl = [...document.querySelectorAll("[data-nav]")].find(n => n.textContent === navMap[view]);
  if (navEl) navEl.classList.add("active");
  window.scrollTo(0, 0);
  if (view === "patient" && a) return viewPatient(a);
  if (view === "adm" && a && b) return viewAdmission(a, +b);
  if (view === "fieldmap") return viewFieldmap();
  if (view === "manual") return viewManual();
  if (view === "about") return viewAbout();
  viewWorkbench();
}
window.addEventListener("hashchange", route);

/* ================================================================ */
/* 工作台                                                            */
/* ================================================================ */
function viewWorkbench(){
  const totalDocs = S.patients.reduce((n, p) => n + p.admissions.reduce((m, a) => m + a.doc_count, 0), 0);
  const totalAdm = S.patients.reduce((n, p) => n + p.admissions.length, 0);
  $app.innerHTML = `
    <div class="notice">⚠ <b>演示说明</b>&nbsp;本工作台为项目演示环境：患者已脱敏（患者A / 患者B），数据源自院方提供的真实住院病历（金标准）；"录音转写"为按真实入院记录重构的模拟对话，"AI 生成"内容取自金标准文书以演示确认流程。正式环境中草稿由多智能体实时生成。</div>
    <div class="page-head">
      <div class="page-title">工作台<small>王医生 · 普外科 · 演示数据集</small></div>
      <div style="display:flex;gap:8px"><button class="btn" onclick="location.hash='#/manual'">📖 医生操作手册</button><button class="btn" onclick="location.hash='#/about'">了解系统边界</button></div>
    </div>
    <div class="sec-head" style="padding-left:2px;font-size:17px;border:none;background:none;cursor:default">今日书写任务</div>
    <div class="task-list">
      <div class="card task-row t-warn">
        <div class="t-icon">🖊</div>
        <div class="t-main">
          <div class="t-title">患者A · 入院记录（急性阑尾炎）— AI 草稿已生成，待四色确认</div>
          <div class="t-sub">法定时限：入院 24h 内完成 · 剩余 13h · 27 轮问诊对话已转写</div>
        </div>
        <button class="btn primary" onclick="location.hash='#/adm/DA0001/1'">核对草稿 →</button>
      </div>
      <div class="card task-row t-dim">
        <div class="t-icon">📝</div>
        <div class="t-main">
          <div class="t-title">患者A · 首次病程记录 — 待书写</div>
          <div class="t-sub">法定时限：入院 8h 内 · AI 生成将于一期扩展开放（当前请手工书写）</div>
        </div>
        <span class="tag" style="background:var(--paper-2);color:var(--ink-3)">二期开放</span>
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
    <div class="stat-row">
      <div class="card stat"><span class="num">${S.patients.length}</span><span class="lab">在院患者（脱敏）</span></div>
      <div class="card stat b"><span class="num">${totalAdm}</span><span class="lab">住院记录</span><span class="sub">2023-11 ~ 2026-09</span></div>
      <div class="card stat g"><span class="num">${totalDocs}</span><span class="lab">累计病历文书</span><span class="sub">入院 / 病程 / 手术 / 出院 等</span></div>
      <div class="card stat y"><span class="num">1</span><span class="lab">待确认草稿</span><span class="sub">患者A · 入院记录</span></div>
    </div>
    <div class="sec-head" style="padding-left:2px;font-size:17px;border:none;background:none;cursor:default;margin-bottom:12px">我的患者</div>
    <div class="pt-grid">
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
    try { S.flow[key] = JSON.parse(localStorage.getItem("mra_flow_" + key)) || null; } catch (e) {}
    if (!S.flow[key] || typeof S.flow[key] !== "object")
      S.flow[key] = { step: 1, revealed: 0, transcriptDone: false, agentN: 0, edits: {}, editLog: [], archived: false };
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

/* ---------------- Step1 问诊录音 ---------------- */
function renderStepRecord(body, key, adm){
  const st = flowState(key);
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
        <div class="dialect-note">🎙 <b>方言识别</b>：系统实时将<b>山东方言</b>转写为普通话文本，自动区分<b>医生 / 患者</b>角色；药品名、检查名已加入医疗热词。演示环境为按真实入院记录重构的模拟对话。</div>
        <div style="font-size:11px;color:var(--ink-3);margin-top:8px;line-height:1.7">演示模式：无需真实说话，点击「开始录音」后将自动播放一段模拟问诊，用以展示"对话 → 病历字段"的完整链路。正式版本步骤为真实麦克风录音 + 方言识别。</div>
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
      rec = true; orb.classList.add("rec"); wave.classList.add("on");
      stateEl.textContent = "正在录音 · 方言实时转写中";
      cta.innerHTML = "■&nbsp; 结束问诊"; cta.classList.remove("primary");
      const t = setInterval(() => { sec++; timeEl.textContent = `00:${String(sec).padStart(2, "0")}`; }, 1000);
      S.timers.push(t);
      startStreaming(tp, key);
    } else {
      rec = false; orb.classList.remove("rec"); wave.classList.remove("on");
      stateEl.textContent = "录音已结束";
      cta.disabled = true;
      clearTimers();
      finishTranscript(tp, key);
    }
  };
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
  const md = S.dialogue.map(t => `**${t.role}**：${t.text}`).join("  \n");
  tp.innerHTML = S.dialogue.map(t => `
    <div class="ts-line ${t.role === "医生" ? "doctor" : "patient"}" style="animation:none;opacity:1;transform:none">
      <div class="who">${t.role}</div><div class="ts-bubble">${esc(t.text)}</div>
    </div>`).join("") + `
    <div class="md-doc">
      <div class="md-title">📄 MD 转写过程稿 · 已挂载至住院时间线</div>
      <div class="md-body">问诊记录_DA0001_202609021040.md&nbsp;&nbsp;·&nbsp;&nbsp;27 轮对话 · ${S.dialogue.reduce((n, t) => n + t.text.length, 0)} 字 · 医生/患者角色已分离 · 全文可溯源</div>
      <div style="margin-top:14px;display:flex;gap:10px">
        <button class="btn primary big" id="gen-cta">⚙ 生成入院记录草稿（多智能体）</button>
        <button class="btn big" onclick="location.hash='#/adm/DA0001/1';viewAdmission('DA0001',1)">重听问诊</button>
      </div>
    </div>`;
  document.getElementById("gen-cta").onclick = () => { const st2 = flowState(key); st2.step = 2; persistFlow(key); viewAdmission("DA0001", 1); };
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
function renderStepGenerate(body, key, adm){
  const p = getPatient("DA0001");
  const admRec = p.admissions[0];
  const admissionDoc = admRec.docs.find(d => d.type === "入院记录");
  const fields = admissionDoc.fields;
  const labeled = fields.filter(f => f.label);
  const cCount = { blue: 0, green: 0, gray: 0, yellow: 0 };
  labeled.forEach(f => cCount[colorOf(f)]++);
  // 质控：同名字段取值冲突
  const byLabel = {};
  labeled.forEach(f => { (byLabel[f.label] = byLabel[f.label] || []).push(String(f.value).trim()); });
  const conflicts = Object.entries(byLabel).filter(([l, vs]) => new Set(vs.filter(v => v)).size > 1).length;
  const his = admRec.his || {};
  const outs = [
    `从转写稿抽取 ${new Set(S.dialogue.flatMap(t => t.mapsTo)).size} 类医学要素：主诉、现病史、既往史、个人史、婚育史、家族史…每项携带对话出处`,
    `HIS 拉取（VISIT_ID 过滤）：异常检验 ${(his.abnormal_labs || []).length} 项 · 检查报告 ${(his.exams || []).length} 份 · 医嘱 ${(his.orders || []).length} 条 · 生命体征 ${(his.vitals || []).length} 类`,
    `命中本院同类病历 12 份（急性阑尾炎）· 《急性阑尾炎诊疗规范》· 本院写作风格样本 61 份`,
    `按入院记录模板生成 ${labeled.length} 个字段：绿 ${cCount.green}（对话提炼）· 蓝 ${cCount.blue}（系统带入）· 灰 ${cCount.gray}（规范所见）· 黄 ${cCount.yellow}（待补）`,
    conflicts ? `发现 ${conflicts} 组同名字段取值冲突（如"过敏史"），已标注 ⚠ 请医生裁定；无臆造项，缺失字段一律留空` : `完整性 / 逻辑一致性 / 受控词表校验通过；无臆造项，缺失字段一律留空（黄色）`,
    `${labeled.length} 个内容已映射至 XTextDocument 模板节点（含受控下拉 ${labeled.filter(f => f.dropdown).length} 项），生成可归档数据包`,
  ];
  body.innerHTML = `
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
  let i = 0;
  const total = AGENTS.length;
  const run = () => {
    if (i >= total){
      document.getElementById("gen-bar").style.width = "100%";
      document.getElementById("gen-done").innerHTML = `<button class="btn primary big" id="to-confirm">进入四色确认 →</button>`;
      document.getElementById("to-confirm").onclick = () => { flowState(key).step = 3; persistFlow(key); viewAdmission("DA0001", 1); };
      return;
    }
    const card = document.getElementById(`ag-${i}`);
    card.classList.add("run");
    document.getElementById(`ag-st-${i}`).textContent = "运行中…";
    document.getElementById("gen-bar").style.width = `${Math.round(((i + 0.5) / total) * 100)}%`;
    S.timers.push(setTimeout(() => {
      card.classList.remove("run"); card.classList.add("ok");
      document.getElementById(`ag-st-${i}`).textContent = "✓ 完成";
      document.getElementById(`ag-out-${i}`).textContent = outs[i];
      i++; document.getElementById("gen-bar").style.width = `${Math.round((i / total) * 100)}%`;
      S.timers.push(setTimeout(run, 240));
    }, 900));
  };
  later(run, 300);
}

/* ---------------- Step3 四色确认 ---------------- */
function renderStepConfirm(body, key, adm){
  const st = flowState(key);
  const admRec = getPatient("DA0001").admissions[0];
  const doc = admRec.docs.find(d => d.type === "入院记录");
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

  body.innerHTML = `
    <div class="legend">
      ${Object.entries(COLOR_META).map(([k, m]) => `<span><span class="cdot ${m.dot}"></span>${m.name} —— ${m.tip}</span>`).join("")}
    </div>
    <div class="confirm-stats sticky-stats">
      ${Object.entries(COLOR_META).map(([k, m]) => `<div class="cs-item"><span class="dot ${m.dot}"></span>${m.name}<b>${cCount[k]}</b></div>`).join("")}
      <div class="cs-item" style="margin-left:auto;color:var(--yellow);border-color:#ecd9ae;background:var(--yellow-bg)">⚠ ${conflictLabels.size} 组字段取值冲突待裁定 · ${cCount.yellow} 项缺失待补</div>
    </div>
    <div id="sec-list">
    ${sections.map(sec => `
      <div class="card sec-block open" data-sec="${esc(sec.name)}">
        <div class="sec-head" onclick="this.parentElement.classList.toggle('open')">
          ${esc(sec.name)}<span class="cnt">${sec.fields.length} 项</span><span class="arrow">▶</span>
        </div>
        <div class="sec-body">
          ${sec.fields.map(f => fieldRow(f, st, conflictLabels)).join("")}
        </div>
      </div>`).join("")}
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

  document.getElementById("confirm-btn").onclick = () => {
    if (window.confirm("确认将本份《入院记录》草稿归档至院内系统？\n（演示环境为模拟归档）确认后责任签章记录为：王医生")){
      flowState(key).step = 4; persistFlow(key); viewAdmission("DA0001", 1);
    }
  };
  window._draftDoc = doc;
  window._draftEdits = st.edits;
}
function fieldRow(f, st, conflictLabels){
  const c = colorOf(f);
  const fid = `${f._i}`;
  const val = st.edits[fid] !== undefined ? st.edits[fid] : f.value;
  const coded = f.dropdown && /^\d+$/.test(String(val).trim());
  const empty = !String(val).trim();
  // 溯源：从对话中找到出现该字段标签的轮次
  const srcTurns = S.dialogue.filter(t => t.mapsTo.includes(f.label));
  return `<div class="f-row c-${c}" id="frow-${fid}">
    <div class="lb"><span class="cdot ${c}"></span>${esc(f.label)}${f.dropdown ? ' <span class="tag" style="background:var(--paper-2);color:var(--ink-3)">受控</span>' : ""}</div>
    <div>
      ${empty ? `<div class="vv" style="color:var(--yellow)">（缺失 · 请补充或核实后填写）</div>`
        : coded ? `<div class="vv"><span style="font-family:var(--mono);color:var(--ink-3)">〔受控码〕</span>${esc(val)}</div>`
        : `<div class="vv" id="fv-${fid}">${esc(val)}</div>`}
      <div id="edit-zone-${fid}"></div>
      <div id="src-${fid}"></div>
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
  const n = document.getElementById("editlog-count");
  if (n) n.textContent = st.editLog.length;
}

/* ---------------- Step4 归档 ---------------- */
function renderStepArchive(body, key, adm){
  const doc = window._draftDoc || getPatient("DA0001").admissions[0].docs.find(d => d.type === "入院记录");
  const st = flowState(key);
  const labeled = doc.fields.filter(f => f.label);
  const xml = buildXML(doc);
  body.innerHTML = `
    <div class="arch-grid">
      <div class="card" style="padding:20px 24px">
        <div class="md-title" style="font-size:16px">归档预览 · XTextDocument（字段回填包）</div>
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
        <div style="font-family:var(--serif);font-size:20px;font-weight:700;color:var(--green);margin-bottom:4px">已归档（模拟）</div>
        <div style="font-size:12px;color:var(--ink-3);margin-bottom:16px">正式环境经对接层回写院内电子病历系统</div>
        <div class="receipt-line"><span>归档编号</span><b>AR-20260902-0047</b></div>
        <div class="receipt-line"><span>文书类型</span><b>入院记录 · 慢性阑尾炎</b></div>
        <div class="receipt-line"><span>确认医生</span><b>王医生（住院医师）</b></div>
        <div class="receipt-line"><span>生成方式</span><b>多智能体草稿 + 医生确认</b></div>
        <div class="receipt-line"><span>医生修改</span><b>${st.editLog.length} 处（留痕已随包存档）</b></div>
        <div class="receipt-line"><span>留痕</span><b>生成/修改/确认 全程可追溯</b></div>
        <div style="margin-top:18px"><button class="btn primary big" onclick="location.hash='#/workbench'">返回工作台</button></div>
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
        <li><b>AI 生成</b>：演示中的"草稿"取自金标准文书内容以呈现确认体验；正式环境由多智能体实时生成，并逐字段携带来源与置信度。</li>
        <li><b>归档</b>：模拟流程；正式环境经对接层回写院内电子病历系统（回写通路正与院方确认）。</li>
      </ul>
      <h2>四色确认（产品核心）</h2>
      <p>蓝 = 系统带入（HIS 数据）· 绿 = 对话提炼（可溯源到转写原文）· 灰 = 规范所见与知识辅助 · 黄 = 缺失待补（<b>严禁臆造，宁缺毋造</b>）。医生重点核对黄色与绿色，确认后归档，责任主体始终是医生。</p>
      <h2>安全与合规</h2>
      <p>全流程留痕（生成/修改/确认/归档记录操作人与时间）· 医生修改回流用于模型优化（数据闭环沉淀于院内）· 正式环境私有化部署，核心模型与数据不出院。</p>
    </div>
  </div>`;
}

/* ---------------- 启动 ---------------- */
(async function init(){
  await loadData();
  route();
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
