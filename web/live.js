// 实时面板：WebSocket 收数据 → requestAnimationFrame 只渲染最新一条。
// 消息格式见 forza/live.py 的模块说明。
(function () {
  "use strict";

  // ======================= 可调参数 =======================
  // 胎温颜色阈值（°C）。只是通用的参考值，不是 Forza 官方数字；不同轮胎配方差别很大，按自己的车调。
  const TIRE_COLD_C = 60;       // 低于这个：冷（蓝）
  const TIRE_OPT_LOW_C = 75;    // 理想区间下限：从这里开始是绿色
  const TIRE_OPT_HIGH_C = 100;  // 理想区间上限：超过开始变黄
  const TIRE_HOT_C = 115;       // 过热：红
  const SHIFT_WARN = 0.92;      // 转速 ≥ 最高转速 × 92%：转速条变黄
  const SHIFT_NOW = 0.97;       // ≥ 97%：红色闪烁，该换挡了
  const SUSP_BOTTOM = 0.95;     // 归一化悬挂行程 ≥ 0.95：快压到底了（标红）
  const SLIP_LIMIT = 1.0;       // 滑移率 / 侧偏角 / 综合滑移 |x| > 1：失去抓地（标红）
  const G_TRAIL_S = 1.5;        // G 球拖尾长度（秒）
  const G_RANGE = 2.0;          // G 球最外圈代表多少 G
  const DELTA_BAR_S = 2.0;      // 差距条满格代表多少秒
  const STALE_S = 2.5;          // 这么久没收到游戏的包：显示“数据中断”
  const WS_SILENT_S = 4.5;      // WebSocket 这么久一条消息都没有：认为连接已死，主动重连
  const RECONNECT_MS = [500, 1000, 2000, 4000, 8000];   // 重连退避
  const KMH_TO_MPH = 0.621371;
  const STORE_KEY = "forza.live.v1";                    // 本机设置（localStorage）

  const WHEELS = [
    { key: "fl", name: "左前", side: "left" },
    { key: "fr", name: "右前", side: "right" },
    { key: "rl", name: "左后", side: "left" },
    { key: "rr", name: "右后", side: "right" },
  ];

  // ======================= 本机设置 =======================
  const DEFAULTS = { unit: "kmh", hz: 20, awakeHintDone: false, dismissed: {} };
  const settings = loadSettings();

  function loadSettings() {
    try {
      const s = JSON.parse(localStorage.getItem(STORE_KEY) || "{}");
      return { ...DEFAULTS, ...s, dismissed: { ...(s.dismissed || {}) } };
    } catch (_) {
      return { ...DEFAULTS, dismissed: {} };
    }
  }
  function saveSettings() {
    try { localStorage.setItem(STORE_KEY, JSON.stringify(settings)); } catch (_) { /* 隐私模式等：不保存也能用 */ }
  }

  // ======================= DOM =======================
  Forza.nav("live");
  const $ = (id) => document.getElementById(id);
  const body = document.body;
  const E = {
    rpmFill: $("rpm-fill"), rpmTicks: $("rpm-ticks"), rpmVal: $("rpm-val"), rpmRow: $("w-rpm"),
    gear: $("gear"), gearBox: $("gear-box"), speed: $("speed"), speedUnit: $("speed-unit"), speedBox: $("speed-box"),
    brake: $("brake-fill"), thr: $("thr-fill"), steer: $("steer-fill"),
    delta: $("delta"), deltaFill: $("delta-fill"), deltaRef: $("delta-ref"),
    lapCur: $("lap-cur"), lapLast: $("lap-last"), lapBest: $("lap-best"), lapPred: $("lap-pred"),
    lapNum: $("lap-num"), lapPos: $("lap-pos"),
    fuel: $("x-fuel"), power: $("x-power"), torque: $("x-torque"), boost: $("x-boost"),
    gball: $("gball"), statusLine: $("status-line"),
    banners: $("banners"), centerMsg: $("center-msg"),
    overlay: $("overlay"), ovTitle: $("ov-title"), ovSub: $("ov-sub"), ovSetup: $("ov-setup"),
    ovPort: $("ov-port"), ovLanIp: $("ov-lanip"), ovNote: $("ov-note"),
    menu: $("menu"), toast: $("toast"),
  };

  // 轮胎卡片：按车上的位置摆（左前 右前 / 左后 右后），悬挂条在靠车身中线的一侧
  const T = WHEELS.map((w) => {
    const el = document.createElement("div");
    el.className = `tire ${w.side}`;
    el.innerHTML =
      `<div class="t-main">` +
      `<div class="t-temp dash-only"><span class="t-name">${w.name}</span><b class="mono">--</b></div>` +
      `<div class="t-row sr"><span>滑移</span><b class="mono">--</b></div>` +
      `<div class="t-row sa"><span>侧偏</span><b class="mono">--</b></div>` +
      `<div class="t-row wear" hidden><span>磨损</span><b class="mono">--</b></div>` +
      `</div><div class="t-susp" title="悬挂行程"><i></i></div>`;
    $("tires").appendChild(el);
    return {
      el,
      temp: el.querySelector(".t-temp"), tempV: el.querySelector(".t-temp b"),
      sr: el.querySelector(".sr"), srV: el.querySelector(".sr b"),
      sa: el.querySelector(".sa"), saV: el.querySelector(".sa b"),
      wear: el.querySelector(".wear"), wearV: el.querySelector(".wear b"),
      susp: el.querySelector(".t-susp"), suspFill: el.querySelector(".t-susp i"),
    };
  });

  // 只在值变化时才写 DOM（textContent / style / class），保持每帧开销很小
  function setText(el, s) {
    if (el._t !== s) { el._t = s; el.textContent = s; }
  }
  function setHTML(el, s) {
    if (el._h !== s) { el._h = s; el.innerHTML = s; }
  }
  function setTf(el, s) {
    if (el._tf !== s) { el._tf = s; el.style.transform = s; }
  }
  function setBg(el, s) {
    if (el._bg !== s) { el._bg = s; el.style.background = s; }
  }
  function setCls(el, cls, on) {
    on = !!on;
    const k = "_c_" + cls;
    if (el[k] !== on) { el[k] = on; el.classList.toggle(cls, on); }
  }
  function setHidden(el, h) {
    h = !!h;
    if (el._hid !== h) { el._hid = h; el.hidden = h; }
  }
  const clamp = (x, a, b) => Math.min(b, Math.max(a, x));
  const num = (x) => typeof x === "number" && isFinite(x);
  const signed = (x, d = 2) => (num(x) ? (x < 0 ? "−" : "") + Math.abs(x).toFixed(d) : "--");
  const esc = (s) => String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));

  // ======================= 颜色 =======================
  // 胎温 → 颜色：冷蓝 → 理想绿 → 偏热黄 → 过热红
  const C_COLD = [59, 130, 246], C_OK = [46, 204, 113], C_WARM = [245, 197, 66], C_HOT = [255, 77, 77];
  const mix = (a, b, t) => a.map((v, i) => Math.round(v + (b[i] - v) * t));
  function tireColor(c) {
    let rgb;
    if (c <= TIRE_COLD_C) rgb = C_COLD;
    else if (c < TIRE_OPT_LOW_C) rgb = mix(C_COLD, C_OK, (c - TIRE_COLD_C) / (TIRE_OPT_LOW_C - TIRE_COLD_C));
    else if (c <= TIRE_OPT_HIGH_C) rgb = C_OK;
    else {
      const mid = (TIRE_OPT_HIGH_C + TIRE_HOT_C) / 2;
      if (c < mid) rgb = mix(C_OK, C_WARM, (c - TIRE_OPT_HIGH_C) / (mid - TIRE_OPT_HIGH_C));
      else if (c < TIRE_HOT_C) rgb = mix(C_WARM, C_HOT, (c - mid) / (TIRE_HOT_C - mid));
      else rgb = C_HOT;
    }
    return `rgb(${rgb[0]},${rgb[1]},${rgb[2]})`;
  }

  // ======================= 状态 =======================
  let ws = null;
  let wsState = "connecting";      // connecting | open | closed
  let retry = 0, retryTimer = null, nextRetryAt = 0;
  let lastMsgAt = 0;
  let latest = null;               // 最近一条 frame 消息（含 frame / lap）
  let status = null;               // 最近的 status 块
  let dirty = true;                // 有新消息还没渲染
  const gTrail = [];               // G 球拖尾：[时间 ms, g_lat, g_long]

  // ======================= WebSocket =======================
  function connect() {
    clearTimeout(retryTimer);
    wsState = "connecting";
    const proto = location.protocol === "https:" ? "wss:" : "ws:";
    let sock;
    try {
      sock = new WebSocket(`${proto}//${location.host}/ws/live?hz=${settings.hz}`);
    } catch (_) {
      drop(null);
      return;
    }
    ws = sock;
    sock.onopen = () => {
      if (ws !== sock) return;
      wsState = "open";
      retry = 0;
      lastMsgAt = performance.now();
      dirty = true;
    };
    sock.onmessage = (ev) => { if (ws === sock) onMessage(ev.data); };
    sock.onclose = sock.onerror = () => drop(sock);
  }

  function drop(sock) {
    if (sock && ws !== sock) return;     // onerror + onclose 会先后触发，只处理一次
    if (sock) { try { sock.close(); } catch (_) { /* 忽略 */ } }
    ws = null;
    wsState = "closed";
    dirty = true;
    const delay = RECONNECT_MS[Math.min(retry, RECONNECT_MS.length - 1)];
    retry++;
    nextRetryAt = performance.now() + delay;
    clearTimeout(retryTimer);
    retryTimer = setTimeout(connect, delay);
  }

  function reconnectNow() {
    if (ws) { const s = ws; ws = null; try { s.close(); } catch (_) { /* 忽略 */ } }
    retry = 0;
    connect();
  }

  function onMessage(text) {
    lastMsgAt = performance.now();
    let msg;
    try { msg = JSON.parse(text); } catch (_) { return; }
    if (msg.status) status = msg.status;
    if (msg.type === "frame" && msg.frame) {
      latest = msg;
      const f = msg.frame;
      if (num(f.g_lat) && num(f.g_long)) gTrail.push([lastMsgAt, f.g_lat, f.g_long]);
    }
    dirty = true;
  }

  // ======================= 渲染 =======================
  let lastStateAt = 0;
  function loop(now) {
    requestAnimationFrame(loop);
    // 看门狗：服务器每秒至少发一条 status，太久没消息说明连接已经死了（手机切 Wi-Fi 等）
    if (wsState === "open" && now - lastMsgAt > WS_SILENT_S * 1000) drop(ws);
    const stateDue = now - lastStateAt > 250;
    if (!dirty && !stateDue) return;
    renderState(now);
    lastStateAt = now;
    if (dirty && latest) renderFrame(latest, now);
    dirty = false;
  }

  function fmtSpeed(kmh) {
    if (!num(kmh)) return "--";
    return String(Math.round(settings.unit === "mph" ? kmh * KMH_TO_MPH : kmh));
  }

  function renderFrame(msg, now) {
    const f = msg.frame, lap = msg.lap;

    // 转速条 + 换挡灯
    const maxr = f.max_rpm || 0;
    const frac = maxr > 0 && num(f.rpm) ? clamp(f.rpm / maxr, 0, 1) : 0;
    setTf(E.rpmFill, `scaleX(${frac.toFixed(3)})`);
    setText(E.rpmVal, num(f.rpm) ? String(Math.round(f.rpm)) : "--");
    if (maxr > 0 && E.rpmTicks._max !== maxr) {
      E.rpmTicks._max = maxr;
      E.rpmTicks.style.setProperty("--tick", `${(1000 / maxr) * 100}%`);
    }
    setCls(body, "rpm-warn", frac >= SHIFT_WARN && frac < SHIFT_NOW);
    setCls(body, "rpm-shift", frac >= SHIFT_NOW);

    // 档位 / 车速
    const gear = f.gear && f.gear !== "-" ? f.gear : "";
    setText(E.gear, gear || "无档位");
    setCls(E.gearBox, "na", !gear);
    setText(E.speed, fmtSpeed(f.speed_kmh));
    setText(E.speedUnit, (settings.unit === "mph" ? "mph" : "km/h") + (f.speed_src === "vel" ? " ≈" : ""));

    // 踏板 / 方向
    setTf(E.thr, `scaleY(${num(f.throttle) ? f.throttle.toFixed(3) : 0})`);
    setTf(E.brake, `scaleY(${num(f.brake) ? f.brake.toFixed(3) : 0})`);
    setTf(E.steer, `scaleX(${num(f.steer) ? clamp(f.steer, -1, 1).toFixed(3) : 0})`);

    // 轮胎
    for (let i = 0; i < 4; i++) {
      const t = T[i];
      const tc = f.tire_temp_c ? f.tire_temp_c[i] : null;
      if (num(tc)) {
        setHTML(t.tempV, `${Math.round(tc)}<small>°</small>`);
        setBg(t.temp, tireColor(tc));
        setCls(t.temp, "na", false);
      } else {
        setText(t.tempV, "--");
        setBg(t.temp, "");
        setCls(t.temp, "na", true);
      }
      const sr = f.slip_ratio ? f.slip_ratio[i] : null;
      const sa = f.slip_angle ? f.slip_angle[i] : null;
      const cs = f.comb_slip ? f.comb_slip[i] : null;
      setText(t.srV, signed(sr));
      setText(t.saV, signed(sa));
      setCls(t.sr, "lost", num(sr) && Math.abs(sr) > SLIP_LIMIT);
      setCls(t.sa, "lost", num(sa) && Math.abs(sa) > SLIP_LIMIT);
      setCls(t.el, "lost", num(cs) && Math.abs(cs) > SLIP_LIMIT);
      const s = f.susp ? f.susp[i] : null;
      setTf(t.suspFill, `scaleY(${num(s) ? clamp(s, 0, 1).toFixed(3) : 0})`);
      setCls(t.susp, "bottom", num(s) && s >= SUSP_BOTTOM);
      const w = f.tire_wear ? f.tire_wear[i] : null;
      setHidden(t.wear, !num(w));
      if (num(w)) setText(t.wearV, `${(w * 100).toFixed(w < 0.1 ? 1 : 0)}%`);
    }

    // 圈速 + 实时差距
    renderLap(f, lap);

    // 其它
    setText(E.fuel, num(f.fuel) ? `${Math.round(f.fuel * 100)}%` : "--");
    setText(E.power, num(f.power_kw) ? `${Math.round(f.power_kw)} kW` : "--");
    setText(E.torque, num(f.torque) ? `${Math.round(f.torque)} N·m` : "--");
    setText(E.boost, num(f.boost) ? f.boost.toFixed(1) : "--");

    drawGBall(f, now);
  }

  function renderLap(f, lap) {
    if (!lap) {
      setText(E.delta, "--");
      setCls(E.delta, "good", false); setCls(E.delta, "bad", false);
      setTf(E.deltaFill, "scaleX(0)");
      for (const el of [E.lapCur, E.lapLast, E.lapBest, E.lapPred]) setText(el, Forza.fmtLap(null));
      setText(E.lapNum, f.has_dash ? "第 - 圈" : "Sled 格式没有圈速");
      setText(E.lapPos, "");
      setText(E.deltaRef, "");
      return;
    }
    const d = lap.delta;
    setText(E.delta, num(d) ? Forza.fmtDelta(d, 2) : "--");
    setCls(E.delta, "good", num(d) && d < 0);
    setCls(E.delta, "bad", num(d) && d > 0);
    setTf(E.deltaFill, `scaleX(${num(d) ? clamp(d / DELTA_BAR_S, -1, 1).toFixed(3) : 0})`);
    setCls(E.deltaFill, "good", num(d) && d < 0);
    setCls(E.deltaFill, "bad", num(d) && d > 0);
    setText(E.deltaRef, num(lap.ref_time) ? `对比 ${Forza.fmtLap(lap.ref_time)}` : "");
    setText(E.lapCur, Forza.fmtLap(lap.time));
    setText(E.lapLast, Forza.fmtLap(lap.last));
    setText(E.lapBest, Forza.fmtLap(lap.best));
    setText(E.lapPred, Forza.fmtLap(lap.predicted));
    let label;
    if (!lap.active && !num(lap.race_time)) label = "没有比赛计时";   // FH 自由漫游
    else label = `第 ${num(lap.number) ? lap.number + 1 : "-"} 圈`;
    setText(E.lapNum, label);
    setText(E.lapPos, num(lap.position) && lap.position > 0 ? `P${lap.position}` : "");
  }

  // ---------- G 球（摩擦圆）：横向 = 左右，纵向：上 = 加速，下 = 刹车 ----------
  const gctx = E.gball.getContext("2d");
  let gSize = 0, gDpr = 1;
  function sizeGBall() {
    const r = E.gball.getBoundingClientRect();
    const dpr = window.devicePixelRatio || 1;
    const s = Math.max(10, Math.floor(Math.min(r.width, r.height)));
    if (s !== gSize || dpr !== gDpr) {
      gSize = s; gDpr = dpr;
      E.gball.width = Math.round(s * dpr);
      E.gball.height = Math.round(s * dpr);
      dirty = true;
    }
  }
  if (window.ResizeObserver) new ResizeObserver(sizeGBall).observe(E.gball);
  window.addEventListener("resize", sizeGBall);

  function drawGBall(f, now) {
    if (!gSize) sizeGBall();
    const ctx = gctx, s = gSize;
    ctx.setTransform(gDpr, 0, 0, gDpr, 0, 0);
    ctx.clearRect(0, 0, s, s);
    const c = s / 2, R = s / 2 - 2;
    const k = R / G_RANGE;
    // 刻度圈：0.5 / 1 / 1.5 / 2 G（1 G 那圈亮一点）
    for (let g = 0.5; g <= G_RANGE + 1e-6; g += 0.5) {
      ctx.beginPath();
      ctx.arc(c, c, g * k, 0, Math.PI * 2);
      ctx.strokeStyle = g === 1 ? "#4a5260" : "#2a2f37";
      ctx.lineWidth = g === 1 ? 1.5 : 1;
      ctx.stroke();
    }
    ctx.beginPath();
    ctx.moveTo(c - R, c); ctx.lineTo(c + R, c);
    ctx.moveTo(c, c - R); ctx.lineTo(c, c + R);
    ctx.strokeStyle = "#2a2f37"; ctx.lineWidth = 1; ctx.stroke();
    const fs = Math.max(9, Math.round(s * 0.075));
    ctx.font = `${fs}px system-ui, sans-serif`;
    ctx.fillStyle = "#6b7280";
    ctx.textAlign = "center";
    ctx.textBaseline = "top";
    ctx.fillText("加速", c, 3);
    ctx.textBaseline = "bottom";
    ctx.fillText("刹车", c, s - 2);
    ctx.textAlign = "left";
    ctx.fillText("1G", c + k + 2, c - 1);

    // 拖尾
    const cutoff = now - G_TRAIL_S * 1000;
    while (gTrail.length && gTrail[0][0] < cutoff) gTrail.shift();
    const pt = (gl, gn) => {
      let x = gl, y = gn;
      const m = Math.hypot(x, y);
      if (m > G_RANGE) { x *= G_RANGE / m; y *= G_RANGE / m; }
      return [c + x * k, c - y * k];
    };
    ctx.lineCap = "round";
    ctx.lineWidth = Math.max(2, s * 0.018);
    for (let i = 1; i < gTrail.length; i++) {
      const a = (gTrail[i][0] - cutoff) / (G_TRAIL_S * 1000);
      const [x0, y0] = pt(gTrail[i - 1][1], gTrail[i - 1][2]);
      const [x1, y1] = pt(gTrail[i][1], gTrail[i][2]);
      ctx.strokeStyle = `rgba(78,161,255,${(a * 0.8).toFixed(2)})`;
      ctx.beginPath(); ctx.moveTo(x0, y0); ctx.lineTo(x1, y1); ctx.stroke();
    }
    // 当前点 + 数值
    if (num(f.g_lat) && num(f.g_long)) {
      const [x, y] = pt(f.g_lat, f.g_long);
      ctx.beginPath();
      ctx.arc(x, y, Math.max(4, s * 0.045), 0, Math.PI * 2);
      ctx.fillStyle = "#e8eaed";
      ctx.fill();
      const tot = Math.hypot(f.g_lat, f.g_long);
      ctx.fillStyle = "#c8ccd2";
      ctx.textAlign = "left"; ctx.textBaseline = "top";
      ctx.font = `600 ${Math.round(fs * 1.15)}px ui-monospace, Consolas, monospace`;
      ctx.fillText(tot.toFixed(2), 2, 2);
    }
  }

  // ---------- 连接 / 数据状态 ----------
  function dataMode() {
    if (wsState !== "open") return wsState === "connecting" && retry === 0 ? "connecting" : "wsdown";
    if (!status || status.since_last_packet === null || status.since_last_packet === undefined) return "nodata";
    if (status.since_last_packet > STALE_S) return latest ? "stale" : "nodata";
    if (latest && !latest.frame.is_race_on) return "paused";
    return "live";
  }

  let curMode = "";
  function renderState(now) {
    const mode = dataMode();
    if (mode !== curMode) {
      body.classList.remove(`mode-${curMode}`);
      body.classList.add(`mode-${mode}`);
      curMode = mode;
    }
    setCls(body, "fmt-sled", !!latest && !latest.frame.has_dash);

    // 遮罩：未连接服务 / 没有收到数据
    if (mode === "wsdown" || mode === "connecting") {
      setHidden(E.overlay, false);
      setText(E.ovTitle, mode === "connecting" ? "正在连接…" : "未连接服务");
      const left = Math.max(0, Math.ceil((nextRetryAt - now) / 1000));
      setText(E.ovSub, mode === "connecting" ? "" :
        (wsState === "connecting" ? "正在重连…" : `${left} 秒后重连（第 ${retry} 次）`) +
        "。确认电脑上的 Forza 遥测程序还开着，手机和电脑连的是同一个 Wi-Fi。");
      setHidden(E.ovSetup, true);
    } else if (mode === "nodata") {
      setHidden(E.overlay, false);
      setText(E.ovTitle, "没有收到数据");
      const age = status && num(status.since_last_packet) ? `（已经 ${Math.round(status.since_last_packet)} 秒没收到包）` : "";
      setText(E.ovSub, `服务已连接，但还没收到游戏发来的数据${age}。游戏只在真正开车时才发。`);
      setHidden(E.ovSetup, false);
      if (status) {
        setText(E.ovPort, String(status.udp_port ?? "--"));
        const ips = status.lan_ips || [];
        setText(E.ovLanIp, ips.length ? `，不行就填本机局域网 IP ${ips.join(" / ")}` : "");
        setText(E.ovNote, "Forza Horizon 6 端口要避开 5200–5300；FM7 不能填 127.0.0.1，要填局域网 IP。");
      }
    } else {
      setHidden(E.overlay, true);
    }

    // 中间大字：暂停 / 数据中断
    if (mode === "paused") {
      setHidden(E.centerMsg, false);
      setHTML(E.centerMsg, "暂停 / 菜单中<small>回到赛道上就会继续刷新</small>");
    } else if (mode === "stale") {
      setHidden(E.centerMsg, false);
      setHTML(E.centerMsg, `${Math.round(status.since_last_packet)} 秒没收到数据` +
        `<small>游戏暂停 / 退出比赛时会停止发包（FH6 在菜单里完全不发）</small>`);
    } else {
      setHidden(E.centerMsg, true);
    }

    renderBanners();
    renderStatusLine(mode);
  }

  function renderStatusLine(mode) {
    const parts = [];
    if (mode === "wsdown" || mode === "connecting") parts.push("服务未连接");
    if (mode === "nodata" && status) parts.push(`等待游戏数据（UDP 端口 ${status.udp_port}）`);
    if (status && status.game_name) parts.push(status.game_name);
    if (status && status.format_label) parts.push(status.format_label);
    if (status && num(status.pps) && mode !== "nodata") parts.push(`${Math.round(status.pps)} 包/秒`);
    if (latest && latest.frame.car) {
      const car = latest.frame.car;
      if (car.pi) {
        parts.push(`${Forza.carClass(latest.frame.family, car.class)} ${car.pi} ${Forza.DRIVETRAIN[car.drivetrain] || ""}`.trim());
      }
    }
    if (status && status.replay) parts.push("回放中");
    if (status && status.recording) parts.push("录制中");
    setText(E.statusLine, parts.join(" · ") || "等待数据…");
  }

  // ---------- 横幅：解码警告 / Sled 提示 / 防息屏提示 ----------
  let awakeHint = false;
  let awakeHintUntil = 0;          // 防息屏提示只出现一次（本机记住），12 秒后自动收起
  function showAwakeHintOnce() {
    awakeHint = true;
    if (settings.awakeHintDone) return;
    settings.awakeHintDone = true;
    saveSettings();
    awakeHintUntil = performance.now() + 12000;
  }
  function renderBanners() {
    const items = [];
    if (status && status.warnings) {
      for (const w of status.warnings) items.push({ cls: "warn", text: w, id: "w:" + w });
    }
    if (latest && !latest.frame.has_dash) {
      items.push({
        cls: "info", id: "sled",
        text: "Forza Motorsport 里 Packet Format 请选 Car Dash。当前是 Sled 格式：没有档位、油门刹车、胎温和圈速，车速是估算的",
      });
    }
    if (awakeHint && awakeHintUntil > performance.now()) {
      items.push({
        cls: "info", id: "awake",
        text: "这个页面没法阻止手机息屏（局域网 http 页面一般都不行）：建议把手机的自动锁屏时间调长（设置 → 显示 → 自动锁定 / 休眠）",
      });
    }
    const shown = items.filter((it) => !settings.dismissed[it.id] || it.id === "sled");
    const key = shown.map((it) => it.id).join("|");
    if (E.banners._k === key) return;
    E.banners._k = key;
    E.banners.innerHTML = shown.map((it) =>
      `<div class="banner ${it.cls}" data-id="${esc(it.id)}"><span class="txt">${esc(it.text)}</span>` +
      (it.id === "sled" ? "" : `<button class="x" aria-label="关闭">×</button>`) + `</div>`).join("");
  }
  E.banners.addEventListener("click", (ev) => {
    const b = ev.target.closest(".banner");
    if (!b) return;
    if (ev.target.closest(".x")) {
      const id = b.dataset.id;
      if (id === "awake") awakeHintUntil = 0;
      else settings.dismissed[id] = 1;      // 同一条警告关掉后不再显示（本机记住）
      saveSettings();
      E.banners._k = null;
      renderBanners();
    } else {
      b.classList.toggle("open");          // 点文字展开 / 收起长提示
    }
  });

  // ======================= 全屏 / 防息屏 =======================
  function fsElement() {
    return document.fullscreenElement || document.webkitFullscreenElement || null;
  }
  function toggleFullscreen() {
    const d = document, el = d.documentElement;
    if (fsElement()) {
      (d.exitFullscreen || d.webkitExitFullscreen || (() => {})).call(d);
      return;
    }
    try {
      if (el.requestFullscreen) {
        const p = el.requestFullscreen({ navigationUI: "hide" });
        if (p && p.catch) p.catch(() => toast("全屏被浏览器拒绝了"));
      } else if (el.webkitRequestFullscreen) {
        el.webkitRequestFullscreen();
      } else {
        toast("这个浏览器不支持网页全屏。iPhone：点分享 → 添加到主屏幕，从主屏幕打开就没有地址栏");
      }
    } catch (_) {
      toast("全屏失败");
    }
    requestWakeLock();
  }
  function onFsChange() {
    setCls(body, "is-fs", !!fsElement());
  }
  document.addEventListener("fullscreenchange", onFsChange);
  document.addEventListener("webkitfullscreenchange", onFsChange);
  $("btn-fs").addEventListener("click", toggleFullscreen);

  let wakeLock = null;
  async function requestWakeLock() {
    if (!("wakeLock" in navigator) || !window.isSecureContext) {
      showAwakeHintOnce();           // 局域网 http 一般走到这里
      setText($("set-wake"), "不可用（需要 https）");
      return;
    }
    if (wakeLock || document.visibilityState !== "visible") return;
    try {
      wakeLock = await navigator.wakeLock.request("screen");
      setText($("set-wake"), "已开启");
      wakeLock.addEventListener("release", () => { wakeLock = null; setText($("set-wake"), "已释放"); });
    } catch (_) {
      showAwakeHintOnce();
      setText($("set-wake"), "被拒绝");
    }
  }
  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState !== "visible") return;
    requestWakeLock();               // 切回来时防息屏锁会被系统释放，重新申请
    if (!ws) reconnectNow();         // 手机锁屏回来：立刻重连，不等退避
    dirty = true;
  });
  // 有的浏览器要求用户点过页面才能申请
  document.addEventListener("click", () => { if (!wakeLock) requestWakeLock(); }, { once: true });

  // ======================= 菜单 / 设置 =======================
  function openMenu(open) {
    setHidden(E.menu, !open);
    if (open) syncSettingsUI();
  }
  function syncSettingsUI() {
    for (const b of document.querySelectorAll("#set-unit button")) b.classList.toggle("on", b.dataset.v === settings.unit);
    for (const b of document.querySelectorAll("#set-hz button")) b.classList.toggle("on", Number(b.dataset.v) === Number(settings.hz));
  }
  function setUnit(u) {
    settings.unit = u === "mph" ? "mph" : "kmh";
    saveSettings();
    syncSettingsUI();
    dirty = true;
  }
  $("btn-menu").addEventListener("click", () => openMenu(true));
  $("menu-close").addEventListener("click", () => openMenu(false));
  E.menu.addEventListener("click", (ev) => { if (ev.target === E.menu) openMenu(false); });
  $("set-unit").addEventListener("click", (ev) => {
    const b = ev.target.closest("button");
    if (b) setUnit(b.dataset.v);
  });
  $("set-hz").addEventListener("click", (ev) => {
    const b = ev.target.closest("button");
    if (!b) return;
    settings.hz = Number(b.dataset.v);
    saveSettings();
    syncSettingsUI();
    reconnectNow();                  // 推送频率是连接参数，重连生效
  });
  // 点车速直接切换单位
  E.speedBox.addEventListener("click", () => {
    setUnit(settings.unit === "mph" ? "kmh" : "mph");
    toast(settings.unit === "mph" ? "车速单位：mph" : "车速单位：km/h");
  });

  let toastTimer = null;
  function toast(text) {
    E.toast.textContent = text;
    E.toast.hidden = false;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => { E.toast.hidden = true; }, 3500);
  }

  // ======================= 启动 =======================
  setText(E.speedUnit, settings.unit === "mph" ? "mph" : "km/h");
  requestWakeLock();
  connect();
  requestAnimationFrame(loop);
})();
