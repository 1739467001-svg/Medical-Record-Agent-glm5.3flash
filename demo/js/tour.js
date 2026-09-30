/* ============================================================
   AI 病历智能体 · 新手引导（游戏式聚光灯分步引导，零依赖）
   用法：Tour.start(steps, onDone)
     steps: [{ sel: "CSS选择器", title: "标题", body: "HTML 说明" }, ...]
   机制：全屏遮罩拦截点击 + 目标元素外圈"聚光灯"（超大 box-shadow 挖洞）
        + 跟随提示卡（自动上/下避让、视口内夹取）+ ←/→/Enter/Esc 键盘操作
   ============================================================ */
"use strict";

const Tour = {
  steps: [], idx: 0, onDone: null,

  start(steps, onDone){
    this.steps = steps || [];
    this.idx = 0;
    this.onDone = onDone || null;
    this.destroy();
    if (!this.steps.length) return;
    const root = document.createElement("div");
    root.id = "tour-root";
    root.innerHTML =
      '<div class="tour-mask"></div>' +
      '<div class="tour-spot"></div>' +
      '<div class="tour-tip">' +
        '<div class="tour-tip-head"><span class="tour-no"></span>' +
          '<span class="tour-skip" onclick="Tour.skip()">跳过引导 ✕</span></div>' +
        '<div class="tour-title"></div>' +
        '<div class="tour-body"></div>' +
        '<div class="tour-dots"></div>' +
        '<div class="tour-btns">' +
          '<button class="btn" id="tour-prev">上一步</button>' +
          '<button class="btn primary" id="tour-next">下一步 →</button>' +
        '</div>' +
      '</div>';
    document.body.appendChild(root);
    this.root = root;
    this.spot = root.querySelector(".tour-spot");
    this.tip = root.querySelector(".tour-tip");
    root.querySelector("#tour-prev").addEventListener("click", () => this.prev());
    root.querySelector("#tour-next").addEventListener("click", () => this.next());
    this._onResize = () => this.place();
    this._onScroll = () => this.place();
    this._onKey = e => {
      if (e.key === "Escape") this.skip();
      else if (e.key === "ArrowRight" || e.key === "Enter") this.next();
      else if (e.key === "ArrowLeft") this.prev();
    };
    window.addEventListener("resize", this._onResize);
    window.addEventListener("scroll", this._onScroll, true);
    document.addEventListener("keydown", this._onKey);
    this.show(0);
  },

  show(i){
    if (i < 0 || i >= this.steps.length) return this.finish();
    this.idx = i;
    const st = this.steps[i];
    const el = st.sel ? document.querySelector(st.sel) : null;
    this.root.querySelector(".tour-no").textContent = `第 ${i + 1} / ${this.steps.length} 步`;
    this.root.querySelector(".tour-title").textContent = st.title;
    this.root.querySelector(".tour-body").innerHTML = st.body;
    this.root.querySelector(".tour-dots").innerHTML =
      this.steps.map((_, k) => `<i class="${k === i ? "on" : ""}"></i>`).join("");
    this.root.querySelector("#tour-prev").disabled = i === 0;
    this.root.querySelector("#tour-next").textContent =
      i === this.steps.length - 1 ? "完成，开始使用 →" : "下一步 →";
    document.querySelectorAll(".tour-hl").forEach(n => n.classList.remove("tour-hl"));
    this.tip.style.visibility = "hidden";   // 切换步骤先隐藏，滚动定位后再显示，避免跳动
    if (el){
      el.classList.add("tour-hl");
      el.scrollIntoView({ block: "center", behavior: "smooth" });
      setTimeout(() => this.place(), 380);   // 平滑滚动到位后校准一次
    }
    this.place();
  },

  place(){
    const st = this.steps[this.idx];
    const el = st && st.sel ? document.querySelector(st.sel) : null;
    if (!el){ this.spot.style.opacity = "0"; return; }
    this.spot.style.opacity = "1";
    const r = el.getBoundingClientRect(), pad = 8;
    this.spot.style.left = (r.left - pad) + "px";
    this.spot.style.top = (r.top - pad) + "px";
    this.spot.style.width = (r.width + pad * 2) + "px";
    this.spot.style.height = (r.height + pad * 2) + "px";
    // 提示卡：优先放目标下方，空间不足放上方；水平方向夹取在视口内
    const tw = Math.min(400, window.innerWidth - 24);
    this.tip.style.width = tw + "px";
    const th = this.tip.offsetHeight;
    let top;
    if (r.bottom + 16 + th < window.innerHeight - 12) top = r.bottom + 16;
    else if (r.top - 16 - th > 76) top = r.top - 16 - th;
    else top = Math.max(76, window.innerHeight - th - 16);
    const left = Math.max(12, Math.min(r.left + r.width / 2 - tw / 2, window.innerWidth - tw - 12));
    this.tip.style.top = top + "px";
    this.tip.style.left = left + "px";
    this.tip.style.visibility = "visible";
  },

  next(){ this.show(this.idx + 1); },
  prev(){ this.show(this.idx - 1); },
  skip(){ this.finish(); },
  finish(){
    this.destroy();
    const cb = this.onDone;
    this.onDone = null;
    if (cb) cb();
  },
  destroy(){
    const root = document.getElementById("tour-root");
    if (root) root.remove();
    this.root = null;
    document.querySelectorAll(".tour-hl").forEach(n => n.classList.remove("tour-hl"));
    if (this._onResize){
      window.removeEventListener("resize", this._onResize);
      window.removeEventListener("scroll", this._onScroll, true);
      document.removeEventListener("keydown", this._onKey);
      this._onResize = this._onScroll = this._onKey = null;
    }
  },
};
window.Tour = Tour;
