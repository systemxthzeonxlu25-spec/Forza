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

  // 顶部导航 + 右侧数据状态小点（每 2 秒查一次 /api/status）
  function nav(active) {
    const el = document.createElement("nav");
    el.className = "topnav";
    el.innerHTML =
      `<span class="brand">Forza 遥测</span>` +
      PAGES.map((p) => `<a href="${p.href}" class="${p.key === active ? "active" : ""}">${p.label}</a>`).join("") +
      `<span class="spacer"></span><span class="muted" id="nav-status"><span class="status-dot"></span>无数据</span>`;
    document.body.prepend(el);
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

  window.Forza = { fmtLap, fmtDelta, carClass, DRIVETRAIN, api, nav, qs, onLapsChanged, watchStatus, checkLapsChanged };
})();
