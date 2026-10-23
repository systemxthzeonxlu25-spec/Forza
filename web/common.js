// 全站公用：顶部导航、格式化工具、API 请求。各页面通过 window.Forza 使用。
(function () {
  "use strict";

  const PAGES = [
    { href: "/", label: "实时面板", key: "live" },
    { href: "/laps.html", label: "圈速对比", key: "laps" },
    { href: "/tuning.html", label: "调车诊断", key: "tuning" },
    { href: "/car.html", label: "车辆调校", key: "car" },
    { href: "/connect.html", label: "连接手机", key: "connect" },
  ];

  // 车辆等级：不同系列编号不一样（仅用于显示，拿不准时显示数字）
  const CLASS_NAMES = {
    fh: ["D", "C", "B", "A", "S1", "S2", "X"],
    fm: ["D", "C", "B", "A", "S", "R", "P", "X"],
  };
  const DRIVETRAIN = ["FWD", "RWD", "AWD"];

  function fmtLap(s) {
    if (s === null || s === undefined || !isFinite(s) || s <= 0) return "--:--.---";
    const m = Math.floor(s / 60);
    const sec = s - m * 60;
    return `${m}:${sec.toFixed(3).padStart(6, "0")}`;
  }

  function fmtDelta(s, digits = 3) {
    if (s === null || s === undefined || !isFinite(s)) return "--";
    return (s >= 0 ? "+" : "−") + Math.abs(s).toFixed(digits);
  }

  // 把任意值转成可以安全拼进 innerHTML 的文本（& < > " ' 都转义）。
  // 页面里凡是用户能改的内容（车名、赛道名、备注、调校版本名……）拼 HTML 前都要过一遍它。
  const ESC = { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" };
  function esc(s) {
    return String(s ?? "").replace(/[&<>"']/g, (c) => ESC[c]);
  }

  // 读 CSS 变量（主题色等，定义在 common.css 的 :root 里）；没定义时用 fallback。
  // 画布（canvas / uPlot）的颜色只能在 JS 里给，所以各页面用它从 CSS 取色，换主题只改 CSS。
  function cssVar(name, fallback = "") {
    return getComputedStyle(document.documentElement).getPropertyValue(name).trim() || fallback;
  }

  function carClass(family, cls) {
    const names = CLASS_NAMES[family] || [];
    return names[cls] ?? String(cls);
  }

  async function api(path, opts = {}) {
    const init = { ...opts };
    if (init.body && typeof init.body !== "string") {
      init.body = JSON.stringify(init.body);
      init.headers = { "Content-Type": "application/json", ...(init.headers || {}) };
    }
    const r = await fetch(path, init);
    if (!r.ok) {
      let msg = `${r.status} ${r.statusText}`;
      try { msg = (await r.json()).detail || msg; } catch (_) { /* 忽略 */ }
      throw new Error(msg);
    }
    return r.status === 204 ? null : r.json();
  }


  // ---------------- 页首插画：粗线稿 + 平涂色块（线条用 SVG 位移滤镜做出手绘的抖动） ----------------
  // 颜色写死（不跟主题变）：墨色线条 + 象牙白块，底色是 Clay / Sky / Olive / Oat 之一。
  const INK = "#141413", IVORY = "#faf9f5";
  const wobble = (id) =>
    `<filter id="${id}" x="-5%" y="-5%" width="110%" height="110%"><feTurbulence type="fractalNoise" baseFrequency="0.03" numOctaves="2" seed="7"/>` +
    `<feDisplacementMap in="SourceGraphic" scale="4" xChannelSelector="R" yChannelSelector="G"/></filter>`;
  const stroke = `fill="none" stroke="${INK}" stroke-width="5.5" stroke-linecap="round" stroke-linejoin="round"`;
  const ILLUS = {
    // 对比：一块象牙白的 L 形，旁边一块方形正要拼上去
    laps: (id) => `<svg viewBox="0 0 250 150" role="img" aria-label="两块拼图"><defs>${wobble(id)}</defs>
      <rect width="250" height="150" fill="#d97757"/>
      <path d="M118 38L171 36L170 104L81 106L83 71L118 70Z" fill="${IVORY}"/>
      <g filter="url(#${id})"><rect x="62" y="30" width="44" height="40" ${stroke}/></g></svg>`,
    // 诊断：象牙白底板上的节点图
    tuning: (id) => `<svg viewBox="0 0 250 150" role="img" aria-label="节点图"><defs>${wobble(id)}</defs>
      <rect width="250" height="150" fill="#6a9bcc"/>
      <path d="M42 31L207 29L208 121L43 123Z" fill="${IVORY}"/>
      <g filter="url(#${id})">
        <g ${stroke} stroke-width="5">
          <path d="M125 76L113 42M125 76L152 50M125 76L88 66M125 76L96 98M125 76L130 112M125 76L166 92"/>
        </g>
        <g fill="${IVORY}" stroke="${INK}" stroke-width="5">
          <circle cx="125" cy="76" r="9"/><circle cx="112" cy="40" r="6"/><circle cx="154" cy="48" r="6"/>
          <circle cx="86" cy="66" r="6"/><circle cx="94" cy="100" r="6"/><circle cx="130" cy="114" r="6"/><circle cx="168" cy="93" r="6"/>
        </g>
      </g></svg>`,
    // 车辆：半圆仪表盘
    car: (id) => `<svg viewBox="0 0 250 150" role="img" aria-label="仪表盘"><defs>${wobble(id)}</defs>
      <rect width="250" height="150" fill="#788c5d"/>
      <path d="M52 112A73 73 0 0 1 198 112Z" fill="${IVORY}"/>
      <g filter="url(#${id})">
        <path d="M52 112A73 73 0 0 1 198 112" ${stroke}/>
        <path d="M125 112L158 66" ${stroke} stroke-width="7"/>
        <circle cx="125" cy="112" r="9" fill="${INK}"/>
        <g ${stroke} stroke-width="4"><path d="M66 90L76 96M88 66L95 75M125 48V60M162 56L156 66M186 86L176 92"/></g>
        <path d="M34 118H216" ${stroke}/>
      </g></svg>`,
    // 连接：手机 + 信号弧
    connect: (id) => `<svg viewBox="0 0 250 150" role="img" aria-label="手机和信号"><defs>${wobble(id)}</defs>
      <rect width="250" height="150" fill="#e3dacc"/>
      <rect x="90" y="22" width="62" height="110" rx="12" fill="${IVORY}"/>
      <g filter="url(#${id})">
        <rect x="90" y="22" width="62" height="110" rx="12" ${stroke}/>
        <path d="M112 38H130" ${stroke}/><circle cx="121" cy="118" r="4" fill="${INK}"/>
        <path d="M172 60C184 66 184 86 172 92" ${stroke}/><path d="M186 46C206 58 206 94 186 106" ${stroke}/>
        <path d="M70 60C58 66 58 86 70 92" ${stroke}/><path d="M56 46C36 58 36 94 56 106" ${stroke}/>
      </g></svg>`,
  };
  const HERO = {
    laps:   { title: "圈速对比", sub: "选两圈，看差在哪个弯、哪一段，理论最佳圈能快多少。" },
    tuning: { title: "调车诊断", sub: "每条结论都附实测数据和判断依据；数据不够时直接说“数据不足”。" },
    car:    { title: "车辆调校", sub: "调校版本对比、实测功率曲线、各挡传动比和换挡点、改终传预测。" },
    connect:{ title: "连接手机", sub: "手机和电脑连同一个 Wi-Fi，扫码就能把实时面板放到手机上。" },
  };
  function hero(key) {
    const h = HERO[key], main = document.querySelector("main.page");
    if (!h || !main || main.querySelector(".hero")) return;
    const el = document.createElement("header");
    el.className = "hero";
    el.innerHTML = `<div class="hero-text"><h1>${h.title}</h1><p>${h.sub}</p></div>` +
      // 插画纯装饰：对读屏软件隐藏，标题和说明文字已经在左边了
      `<div class="hero-art" aria-hidden="true">${ILLUS[key]("wob-" + key)}</div>`;
    main.prepend(el);
  }

  // 顶部导航 + 右侧数据状态小点（每 2 秒查一次 /api/status）
  function nav(active) {
    const el = document.createElement("nav");
    el.className = "topnav";
    el.innerHTML =
      `<span class="brand">Forza 遥测</span>` +
      PAGES.map((p) => `<a href="${p.href}" class="${p.key === active ? "active" : ""}">${p.label}</a>`).join("") +
      `<span class="spacer"></span><span class="muted" id="nav-status"><span class="status-dot"></span>无数据</span>`;
    document.body.prepend(el);
    if (!document.body.classList.contains("live-page")) addThemeButton(el);   // 实时面板固定深色
    hero(active);
    const st = el.querySelector("#nav-status");
    async function poll() {
      try {
        const s = await api("/api/status");
        checkLapsChanged(s);
        const age = s.since_last_packet;
        const dot = age === null ? "" : age < 2 ? "on" : "stale";
        const txt = age === null ? "无数据" : age < 2 ? `${s.game_name || ""} ${Math.round(s.pps)} 包/秒` : `${Math.round(age)} 秒没收到包`;
        st.innerHTML = `<span class="status-dot ${dot}"></span>${txt}`;
      } catch (_) {
        st.innerHTML = `<span class="status-dot"></span>服务未连接`;
      }
    }
    poll();
    setInterval(poll, 2000);
  }

  // 主题切换：跟随系统 → 浅色 → 深色 → 跟随系统。选择存在 localStorage，theme.js 在 <head> 里恢复。
  const THEME_LABEL = { auto: "跟随系统", light: "浅色", dark: "深色" };
  function addThemeButton(nav) {
    const btn = document.createElement("button");
    btn.className = "theme-btn";
    const cur = () => document.documentElement.getAttribute("data-theme") || "auto";
    const paint = () => { btn.textContent = THEME_LABEL[cur()]; btn.title = "切换主题"; };
    btn.addEventListener("click", () => {
      const next = { auto: "light", light: "dark", dark: "auto" }[cur()];
      if (next === "auto") document.documentElement.removeAttribute("data-theme");
      else document.documentElement.setAttribute("data-theme", next);
      try { next === "auto" ? localStorage.removeItem("forza-theme") : localStorage.setItem("forza-theme", next); } catch (_) { /* 忽略 */ }
      paint();
      location.reload();     // 图表（canvas / uPlot）的颜色在页面加载时取一次，重载最省事
    });
    paint();
    nav.appendChild(btn);
  }

  // 新圈落盘广播：saved_count 变大时派发 window 事件 "forza:laps-changed"（第一次轮询只记录、不派发）。
  // 页面里用 Forza.onLapsChanged(fn) 订阅；fn 收到 {saved_count, last_saved_lap_id}。
  let lastSaved = null;
  function checkLapsChanged(s) {
    if (typeof s.saved_count !== "number") return;
    if (lastSaved !== null && s.saved_count > lastSaved) {
      window.dispatchEvent(new CustomEvent("forza:laps-changed", {
        detail: { saved_count: s.saved_count, last_saved_lap_id: s.last_saved_lap_id },
      }));
    }
    lastSaved = s.saved_count;
  }

  function onLapsChanged(fn) {
    window.addEventListener("forza:laps-changed", (e) => fn(e.detail));
  }

  // 没有导航栏的页面（例如全屏实时面板）也可以单独开启轮询
  function watchStatus(intervalMs = 2000) {
    const tick = async () => {
      try { checkLapsChanged(await api("/api/status")); } catch (_) { /* 忽略 */ }
    };
    tick();
    return setInterval(tick, intervalMs);
  }

  function qs(name) {
    return new URLSearchParams(location.search).get(name);
  }

  window.Forza = { fmtLap, fmtDelta, carClass, DRIVETRAIN, api, esc, cssVar, nav, qs, onLapsChanged, watchStatus, checkLapsChanged };
})();
