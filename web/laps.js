// 圈速对比页：圈列表（筛选 / 改名 / 备注 / 删除 / 调校版本）+ 分段 / 理论最佳 + 两圈对比（曲线、逐弯分析、赛道图）。
// 数据：/api/tracks、/api/cars、/api/laps、/api/sectors、/api/compare（计算全在后端 forza/analysis.py、forza/sectors.py）。
// 约定：A = 参考圈（--accent 蓝），B = 要分析的圈（--accent-2 橙）；时间差 = B − A，正数 = B 慢。
// 深链：/laps.html?b=圈&at=米（at 是 B 圈自己的距离；不给 a 时参考圈 = 后端 default_reference），也支持 a=&b=&at=。
// 新圈落盘（Forza.onLapsChanged）时自动重拉列表；正在改名 / 写备注时先记下，改完再刷新。
(function () {
  "use strict";
  const F = window.Forza;
  F.nav("laps");

  // ------------------------------------------------------------------ 小工具
  const $ = (sel, root = document) => root.querySelector(sel);
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const rootCss = getComputedStyle(document.documentElement);
  const cssVar = (name, fb) => rootCss.getPropertyValue(name).trim() || fb;
  const COL = {
    a: cssVar("--accent", "#4ea1ff"),
    b: cssVar("--accent-2", "#ff9f43"),
    loss: cssVar("--bad", "#ff5c5c"),
    gain: cssVar("--gain", "#2ec4b6"),
    neutral: cssVar("--neutral", "#5d6470"),
    text: cssVar("--text", "#e8eaed"),
    muted: cssVar("--muted", "#8b929c"),
    line: cssVar("--line", "#2c313a"),
    panel2: cssVar("--panel-2", "#20242b"),
  };
  const GRID = "rgba(255,255,255,0.06)";
  const DPR = () => window.devicePixelRatio || 1;

  function hexRgb(hex) {
    const h = hex.replace("#", "");
    const n = parseInt(h.length === 3 ? h.split("").map((c) => c + c).join("") : h, 16);
    return [(n >> 16) & 255, (n >> 8) & 255, n & 255];
  }
  function alpha(hex, a) {
    const [r, g, b] = hexRgb(hex);
    return `rgba(${r},${g},${b},${a})`;
  }
  function mix(c1, c2, t) {
    const a = hexRgb(c1), b = hexRgb(c2);
    return `rgb(${a.map((v, i) => Math.round(v + (b[i] - v) * t)).join(",")})`;
  }
  const fmtDate = (s) => (s ? s.slice(5, 16).replace("T", " ") : "");
  const carKeyOf = (o) => `${o.game}:${o.car_ordinal}`;

  // ------------------------------------------------------------------ 状态
  const S = {
    tracks: [], cars: [], laps: [],
    trackId: null, carKey: null, completeOnly: false,
    a: null, b: null,                 // 选中的圈 id
    cmp: null, cmpKey: null,          // 当前对比结果
    cmpSeq: 0,
    charts: [], hoverIdx: null,
    selSeg: null,                     // 选中的段名（T3 / 起点）
    segSort: "loss",
    renaming: null,                   // "track" | "car" | "sector"
    editingNote: null,                // 正在写备注的圈 id
    reloadPending: null,              // 改名 / 写备注期间来了新圈：改完再刷新
    setupFilter: "",                  // "" = 全部版本，"none" = 没标记版本，其它 = setup_id
    setupNames: {},                   // setup_id → 最新名字（来自 /api/setups，拿不到就用圈里记的）
    setupsKey: null,
    focusAt: null,                    // 深链 at（B 圈自己的距离），对比出来后放大过去
    focusMark: null,                  // 曲线上标出的深链位置（对比网格上的距离）
    pendingSector: null,              // 从分段表点过来：对比出来后选中这一段
    extraSel: loadExtraSel(),         // 勾选的额外曲线（"offset" = 走线偏移，其它 = /api/compare 的 channels）
    extraOptions: null,
    sec: null, secKey: null, secSeq: 0, secView: "table",
    secRename: null,                  // 正在改名的弯（编号）
  };

  function loadExtraSel() {
    try {
      const v = JSON.parse(localStorage.getItem("forza.laps.extra") || "null");
      if (Array.isArray(v)) return new Set(v);
    } catch (_) { /* 隐私模式等：用默认 */ }
    return new Set(["offset"]);
  }
  function saveExtraSel() {
    try { localStorage.setItem("forza.laps.extra", JSON.stringify([...S.extraSel])); } catch (_) { /* 忽略 */ }
  }

  // ------------------------------------------------------------------ 加载
  async function load(opts = {}) {
    try {
      const [tracks, cars, laps] = await Promise.all([
        F.api("/api/tracks"), F.api("/api/cars"), F.api("/api/laps?limit=5000"),
      ]);
      S.tracks = tracks; S.cars = cars; S.laps = laps;
    } catch (e) {
      if (opts.quiet && S.laps.length) return;     // 自动刷新失败：保留当前页面，下次再试
      showPageState("error", e.message);
      return;
    }
    if (!S.laps.length) {
      showPageState("empty");
      return;
    }
    $("#page-state").hidden = true;
    $("#filters").hidden = false;
    $("#laps-card").hidden = false;
    if (opts.initial) await applyUrl();
    pickFilters();
    if (opts.selectB != null) applyNewLap(opts.selectB);
    renderFilters();
    refreshLaps({ sectors: opts.sectors });
    loadSetupNames();
  }

  function showPageState(kind, msg) {
    for (const id of ["#filters", "#laps-card", "#compare"]) $(id).hidden = true;
    const el = $("#page-state");
    el.hidden = false;
    el.classList.toggle("error", kind === "error");
    if (kind === "error") {
      el.innerHTML = `<h2>读取圈速数据失败</h2><p class="bad">${esc(msg)}</p>
        <p class="muted">确认 <code>python -m forza serve</code> 还在运行，然后重试。</p>
        <button class="primary" id="retry">重试</button>`;
      $("#retry").onclick = () => { el.innerHTML = `<span class="spinner"></span>加载中…`; load({ initial: true }); };
    } else {
      el.innerHTML = `<h2>还没有圈速记录</h2>
        <p>圈是自动记下来的，不用手动操作：</p>
        <ol>
          <li>运行 <code>python -m forza serve</code>，在游戏设置的「HUD / 游戏玩法」页打开 <b>Data Out</b>，
              IP 填这台电脑的局域网 IP（同一台电脑可填 127.0.0.1），端口填 <b>5555</b>（启动时终端里会打印）。</li>
          <li>进比赛、计时赛或自由练习跑圈：每过一次起终点线，上一圈就自动存档；
              Horizon 的冲刺赛跑完整段存一条。第一圈（起步圈）、倒带过的圈会打上标记。</li>
          <li>回到这个页面刷新，就能看到每圈的圈速，并挑两圈叠在一起对比。</li>
        </ol>
        <p class="muted">没开游戏也能先试：<code>python -m forza fake -o samples/demo.frec</code> 生成假数据，
          再 <code>python -m forza import samples/demo.frec</code> 导入，然后刷新本页。</p>`;
    }
  }

  // 网址参数：a / b / at（深链）或 track / car / setup（刷新后保持筛选）
  async function applyUrl() {
    const a = Number(F.qs("a")), b = Number(F.qs("b"));
    const at = parseFloat(F.qs("at"));
    const lapA = S.laps.find((l) => l.id === a);
    const lapB = S.laps.find((l) => l.id === b);
    if (lapB) {
      // 深链（诊断页“在对比页查看”）：以 B 为准；没给 a（或 a 不对）就用后端的默认参考圈
      S.trackId = lapB.track_id;
      S.carKey = carKeyOf(lapB);
      S.b = b;
      if (lapA && a !== b && lapA.track_id === lapB.track_id && carKeyOf(lapA) === carKeyOf(lapB)) {
        S.a = a;
      } else {
        try {
          const r = await F.api(`/api/laps/${b}/reference`);
          if (r.reference) S.a = r.reference.id;
        } catch (_) { /* 拿不到：下面 chooseDefaults 会挑一圈 */ }
      }
      if (Number.isFinite(at) && at >= 0) S.focusAt = at;
      return;
    }
    if (lapA) {
      S.trackId = lapA.track_id;
      S.carKey = carKeyOf(lapA);
      S.a = a;
      return;
    }
    const t = Number(F.qs("track"));
    if (S.tracks.some((x) => x.id === t)) S.trackId = t;
    const c = F.qs("car");
    if (c) S.carKey = c;
    const su = F.qs("setup");
    if (su) S.setupFilter = su;
  }

  // ------------------------------------------------------------------ 新圈落盘：自动刷新
  // B 原来是“最新一圈”（列表里最新的那圈，或最新的完整圈）就自动换成新圈
  function bIsLatest() {
    if (S.b == null) return false;
    const ls = visibleLaps();
    const latest = ls[0]?.id;
    const latestComplete = ls.find((l) => l.is_complete)?.id;
    return S.b === latest || S.b === latestComplete;
  }

  function applyNewLap(id) {
    const nl = S.laps.find((l) => l.id === id);
    if (!nl || nl.track_id !== S.trackId || carKeyOf(nl) !== S.carKey || !nl.is_complete) return;
    if (!visibleLaps().some((l) => l.id === id)) return;
    if (S.a === id) return;
    S.b = id;
  }

  async function autoReload(info) {
    S.reloadPending = null;
    const follow = bIsLatest();
    await load({ quiet: true, sectors: true, selectB: follow ? info?.last_saved_lap_id : null });
    const nl = S.laps.find((l) => l.id === info?.last_saved_lap_id);
    if (nl) {
      const n = nl.kind === "sprint" ? "冲刺" : `第 ${nl.lap_number} 圈`;
      toast(`新存了一圈：${n} ${F.fmtLap(nl.lap_time)}${S.b === nl.id ? "，已设为 B" : ""}`);
    }
  }

  // 改名 / 写备注结束时调用：期间来过新圈就补一次刷新
  function flushPendingReload() {
    if (S.reloadPending && !S.renaming && S.editingNote == null) autoReload(S.reloadPending);
  }

  F.onLapsChanged((info) => {
    if (S.renaming || S.editingNote != null) { S.reloadPending = info; return; }
    autoReload(info);
  });

  let toastTimer = null;
  function toast(msg) {
    const el = $("#toast");
    el.textContent = msg;
    el.hidden = false;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => { el.hidden = true; }, 5000);
  }

  // 没选或选的已不存在：赛道 / 车默认用最新一圈的
  function pickFilters() {
    const latest = S.laps[0];
    if (!S.laps.some((l) => l.track_id === S.trackId)) S.trackId = latest.track_id;
    const onTrack = S.laps.filter((l) => l.track_id === S.trackId);
    if (!onTrack.some((l) => carKeyOf(l) === S.carKey)) S.carKey = carKeyOf(onTrack[0]);
  }

  function lapsOfTrackCar() {
    return S.laps.filter((l) => l.track_id === S.trackId && carKeyOf(l) === S.carKey);
  }
  function setupMatch(l) {
    const f = S.setupFilter;
    if (!f) return true;
    if (f === "none") return l.setup_id == null;
    return String(l.setup_id) === f;
  }
  function visibleLaps() {
    const ls = lapsOfTrackCar().filter(setupMatch);
    return S.completeOnly ? ls.filter((l) => l.is_complete) : ls;
  }

  function syncUrl() {
    const p = new URLSearchParams();
    if (S.trackId != null) p.set("track", S.trackId);
    if (S.carKey) p.set("car", S.carKey);
    if (S.setupFilter) p.set("setup", S.setupFilter);
    if (S.a) p.set("a", S.a);
    if (S.b) p.set("b", S.b);
    history.replaceState(null, "", `${location.pathname}?${p}`);
  }

  // ------------------------------------------------------------------ 筛选（含改名）
  function trackName(t) { return t ? (t.name || `赛道 ${t.id}`) : "未知赛道"; }
  function carLabel(c) {
    if (!c) return "未知车辆";
    const fam = (c.game || "").startsWith("fh") ? "fh" : "fm";
    const cls = c.car_class != null ? `${F.carClass(fam, c.car_class)} ${c.car_pi ?? ""}`.trim() : "";
    return `${c.name || `车 #${c.car_ordinal}`}${cls ? ` · ${cls}` : ""}`;
  }
  const curTrack = () => S.tracks.find((t) => t.id === S.trackId);
  const curCar = () => S.cars.find((c) => carKeyOf(c) === S.carKey);

  function renderFilters() {
    renderTrackCtl();
    renderCarCtl();
    renderSetupCtl();
    $("#complete-only").checked = S.completeOnly;
  }

  // ---- 调校版本（setup_id / setup_name 存在圈的 meta 里；名字以 /api/setups 为准，拿不到就用圈里记的）
  function setupName(id, fallback) {
    return S.setupNames[id] || fallback || `版本 #${id}`;
  }

  function setupsOfCar() {
    const m = new Map();
    let none = 0;
    for (const l of lapsOfTrackCar()) {
      if (l.setup_id == null) { none++; continue; }
      const k = String(l.setup_id);
      const e = m.get(k) || { id: k, name: l.setup_name, n: 0, last: 0 };
      e.n++; e.last = Math.max(e.last, l.id);
      if (!e.name && l.setup_name) e.name = l.setup_name;
      m.set(k, e);
    }
    return { list: [...m.values()].sort((x, y) => y.last - x.last), none };
  }

  function renderSetupCtl() {
    const { list, none } = setupsOfCar();
    const grp = $("#setup-group");
    if (!list.length) {               // 这台车还没记过调校版本：不显示这个筛选
      grp.hidden = true;
      if (S.setupFilter) S.setupFilter = "";
      return;
    }
    grp.hidden = false;
    if (S.setupFilter && S.setupFilter !== "none" && !list.some((e) => e.id === S.setupFilter)) S.setupFilter = "";
    const opts = [`<option value="">全部版本（${lapsOfTrackCar().length} 圈）</option>`]
      .concat(list.map((e) => `<option value="${esc(e.id)}" ${e.id === S.setupFilter ? "selected" : ""}>${esc(setupName(e.id, e.name))}（${e.n} 圈）</option>`));
    if (none) opts.push(`<option value="none" ${S.setupFilter === "none" ? "selected" : ""}>没标记版本（${none} 圈）</option>`);
    $("#setup-ctl").innerHTML = `<select id="setup-sel" aria-label="调校版本">${opts.join("")}</select>`;
    $("#setup-sel").onchange = (e) => {
      S.setupFilter = e.target.value;
      S.a = S.b = null;
      refreshLaps();
    };
  }

  async function loadSetupNames() {
    const car = curCar() || (S.carKey && { game: S.carKey.split(":")[0], car_ordinal: S.carKey.split(":")[1] });
    if (!car || !lapsOfTrackCar().some((l) => l.setup_id != null)) return;
    const key = carKeyOf(car);
    if (S.setupsKey === key) return;
    S.setupsKey = key;
    try {
      const list = await F.api(`/api/setups?game=${encodeURIComponent(car.game)}&car_ordinal=${car.car_ordinal}`);
      if (!Array.isArray(list)) return;
      for (const x of list) S.setupNames[x.id] = x.name;
      renderSetupCtl();
      renderTable();
    } catch (_) { /* 调校版本接口不可用：只用圈里记的名字 */ }
  }

  function renderTrackCtl() {
    const el = $("#track-ctl");
    if (S.renaming === "track") return renderRename(el, "track", trackName(curTrack()));
    const ids = [...new Set(S.laps.map((l) => l.track_id))];
    const opts = ids.map((id) => {
      const t = S.tracks.find((x) => x.id === id);
      const n = S.laps.filter((l) => l.track_id === id).length;
      return `<option value="${id}" ${id === S.trackId ? "selected" : ""}>${esc(trackName(t))}（${n} 圈）</option>`;
    });
    el.innerHTML = `<select id="track-sel" aria-label="赛道">${opts.join("")}</select>
      <button class="small-btn" id="track-rename" title="给这条赛道起个名字">改名</button>`;
    $("#track-sel").onchange = (e) => {
      S.trackId = Number(e.target.value);
      S.carKey = null; S.a = S.b = null; S.setupFilter = "";
      pickFilters(); renderFilters(); refreshLaps(); loadSetupNames();
    };
    $("#track-rename").onclick = () => { S.renaming = "track"; renderTrackCtl(); };
  }

  function renderCarCtl() {
    const el = $("#car-ctl");
    if (S.renaming === "car") return renderRename(el, "car", curCar()?.name || "");
    const keys = [...new Set(S.laps.filter((l) => l.track_id === S.trackId).map(carKeyOf))];
    const opts = keys.map((k) => {
      const c = S.cars.find((x) => carKeyOf(x) === k) ||
        { game: k.split(":")[0], car_ordinal: Number(k.split(":")[1]) };
      return `<option value="${esc(k)}" ${k === S.carKey ? "selected" : ""}>${esc(carLabel(c))}</option>`;
    });
    el.innerHTML = `<select id="car-sel" aria-label="车辆">${opts.join("")}</select>
      <button class="small-btn" id="car-rename" title="给这辆车起个名字（游戏只给编号）">改名</button>`;
    $("#car-sel").onchange = (e) => {
      S.carKey = e.target.value; S.a = S.b = null; S.setupFilter = "";
      renderSetupCtl();
      refreshLaps();
      loadSetupNames();
    };
    $("#car-rename").onclick = () => { S.renaming = "car"; renderCarCtl(); };
  }

  function renderRename(el, what, value) {
    el.innerHTML = `<input id="rename-input" maxlength="60" value="${esc(value)}"
        placeholder="${what === "track" ? "赛道名字" : "车的名字"}">
      <button class="small-btn primary" id="rename-ok">保存</button>
      <button class="small-btn" id="rename-cancel">取消</button>`;
    const input = $("#rename-input");
    input.focus(); input.select();
    const done = () => { S.renaming = null; renderFilters(); flushPendingReload(); };
    const save = async () => {
      const name = input.value.trim();
      if (!name) { input.focus(); return; }
      try {
        if (what === "track") {
          await F.api(`/api/tracks/${S.trackId}`, { method: "PATCH", body: { name } });
        } else {
          const [game, ord] = S.carKey.split(":");
          await F.api(`/api/cars/${encodeURIComponent(game)}/${ord}`, { method: "PATCH", body: { name } });
        }
        S.renaming = null;
        S.reloadPending = null;
        await load({ sectors: true });
      } catch (e) { alert(`改名失败：${e.message}`); }
    };
    $("#rename-ok").onclick = save;
    $("#rename-cancel").onclick = done;
    input.onkeydown = (e) => { if (e.key === "Enter") save(); if (e.key === "Escape") done(); };
  }

  $("#complete-only").onchange = (e) => { S.completeOnly = e.target.checked; refreshLaps(); };

  // ------------------------------------------------------------------ 圈列表
  // 默认选择：B = 最新的完整圈，A = 最快圈（B 本身最快时 A = 第二快）
  function chooseDefaults() {
    const ls = visibleLaps();
    const ids = new Set(ls.map((l) => l.id));
    if (S.a && !ids.has(S.a)) S.a = null;
    if (S.b && !ids.has(S.b)) S.b = null;
    if (S.a && S.b) return;
    const complete = ls.filter((l) => l.is_complete);
    const byTime = [...complete].sort((x, y) => x.lap_time - y.lap_time);
    if (!S.b) S.b = (complete[0] || ls[0])?.id ?? null;
    if (!S.a) S.a = (byTime.find((l) => l.id !== S.b) || ls.find((l) => l.id !== S.b))?.id ?? null;
  }

  // 筛选 / 数据变了之后：补齐默认选择 → 重画列表 → 更新网址 → 分段表 → 对比
  function refreshLaps(opts = {}) {
    chooseDefaults();
    renderTable();
    syncUrl();
    loadSectors(!!opts.sectors);
    runCompare();
  }

  function flagBadges(l) {
    const out = [];
    if (l.kind === "sprint") out.push(`<span class="badge sprint" title="Horizon 冲刺赛：整段算一条记录">冲刺</span>`);
    if (!l.is_complete) out.push(`<span class="badge incomplete" title="没跑完 / 被标记为不完整，不参与最快圈">不完整</span>`);
    const f = l.flags || [];
    if (f.includes("first_lap")) out.push(`<span class="badge" title="比赛的第一圈（常常是站立起步，比飞驰圈慢）">第一圈</span>`);
    if (f.includes("rewind")) out.push(`<span class="badge rewind" title="这圈中途倒过带">倒带</span>`);
    if (f.includes("abandoned")) out.push(`<span class="badge incomplete" title="冲刺赛中途退出">放弃</span>`);
    if (f.includes("dist_integrated")) out.push(`<span class="badge" title="游戏给的距离不可靠，改用车速积分算距离">距离估算</span>`);
    if (f.includes("partial_start")) out.push(`<span class="badge incomplete" title="采集开始时这一圈已经在进行，缺少前段数据">中途接入</span>`);
    if (f.includes("finish_inferred")) out.push(`<span class="badge" title="根据计时和距离估计完赛，尚未由游戏完赛画面核实">完赛推定</span>`);
    if (f.includes("interrupted")) out.push(`<span class="badge incomplete" title="重开或切换事件前的记录，不参与最快圈">中断</span>`);
    if (f.includes("rewound_after_save")) out.push(`<span class="badge incomplete" title="落盘后倒带跨回本圈，原成绩已失效">倒带失效</span>`);
    if (l.setup_id != null) {
      const nm = setupName(l.setup_id, l.setup_name);
      out.push(`<span class="badge setup" title="调校版本：${esc(nm)}">${esc(nm)}</span>`);
    }
    return out.join("");
  }

  function renderTable() {
    const tb = $("#lap-table tbody");
    const ls = visibleLaps();
    const all = lapsOfTrackCar();
    $("#lap-count").textContent = S.completeOnly && ls.length !== all.length
      ? `${ls.length} / ${all.length} 圈` : `${ls.length} 圈`;
    if (!ls.length) {
      tb.innerHTML = `<tr class="empty-row"><td colspan="9">没有符合条件的圈${S.completeOnly ? "（取消“只看完整圈”试试）" : ""}</td></tr>`;
      return;
    }
    tb.innerHTML = ls.map((l) => {
      const cls = [l.id === S.a ? "row-a" : "", l.id === S.b ? "row-b" : ""].join(" ");
      const delta = l.delta_to_best == null || l.is_best ? "" : F.fmtDelta(l.delta_to_best);
      const num = l.kind === "sprint" ? "—" : l.lap_number;
      const note = l.note
        ? `<span class="note-text" title="点击修改备注">${esc(l.note)}</span>`
        : `<span class="note-text placeholder" title="点击添加备注">添加备注</span>`;
      return `<tr data-id="${l.id}" class="${cls}">
        <td class="c-pick"><label class="pick pick-a" title="设为 A（参考圈）"><input type="radio" name="pick-a" value="${l.id}" ${l.id === S.a ? "checked" : ""}><span>A</span></label></td>
        <td class="c-pick"><label class="pick pick-b" title="设为 B（要分析的圈）"><input type="radio" name="pick-b" value="${l.id}" ${l.id === S.b ? "checked" : ""}><span>B</span></label></td>
        <td class="c-num">${num}</td>
        <td class="c-time">${F.fmtLap(l.lap_time)} ${l.is_best ? `<span class="badge best" title="这条赛道 + 这辆车的最快完整圈">★ 最快</span>` : ""}</td>
        <td class="c-delta">${delta}</td>
        <td class="c-date">${fmtDate(l.created_at)}</td>
        <td class="c-flags">${flagBadges(l)}</td>
        <td class="c-note">${note}</td>
        <td class="c-act">
          <a href="/tuning.html?lap=${l.id}" title="用这圈的数据做调车诊断">诊断</a>
          <button class="link" data-act="complete" title="${l.is_complete ? "不让这圈参与最快圈 / 默认对比" : "重新算作完整圈"}">${l.is_complete ? "标为不完整" : "标为完整"}</button>
          <button class="link danger" data-act="delete" title="删除这一圈">删除</button>
        </td>
      </tr>`;
    }).join("");
  }

  $("#lap-table").addEventListener("change", (e) => {
    const t = e.target;
    if (t.name !== "pick-a" && t.name !== "pick-b") return;
    const id = Number(t.value);
    if (t.name === "pick-a") { if (id === S.b) S.b = S.a; S.a = id; }
    else { if (id === S.a) S.a = S.b; S.b = id; }
    renderTable(); syncUrl(); runCompare();
    if (S.sec && S.secView === "matrix") renderSectors();     // 逐圈表里标着 A / B
  });

  $("#lap-table").addEventListener("click", async (e) => {
    const tr = e.target.closest("tr[data-id]");
    if (!tr) return;
    const id = Number(tr.dataset.id);
    const lap = S.laps.find((l) => l.id === id);
    const act = e.target.closest("[data-act]")?.dataset.act;
    if (act === "delete") {
      const what = lap.kind === "sprint" ? "这条冲刺记录" : `第 ${lap.lap_number} 圈`;
      if (!confirm(`删除${what}（${F.fmtLap(lap.lap_time)}）？删除后不能恢复。`)) return;
      try { await F.api(`/api/laps/${id}`, { method: "DELETE" }); } catch (err) { alert(`删除失败：${err.message}`); }
      if (S.a === id) S.a = null;
      if (S.b === id) S.b = null;
      await load({ sectors: true });
    } else if (act === "complete") {
      try {
        await F.api(`/api/laps/${id}`, { method: "PATCH", body: { is_complete: !lap.is_complete } });
      } catch (err) { alert(`修改失败：${err.message}`); }
      S.cmpKey = null;   // 最快圈可能变了，重新对比
      await load({ sectors: true });
    } else if (e.target.closest(".note-text")) {
      editNote(tr.querySelector(".c-note"), lap);
    }
  });

  function editNote(td, lap) {
    td.innerHTML = `<input class="note-input" maxlength="200" value="${esc(lap.note || "")}" placeholder="比如：T3 刹车早了">`;
    const input = td.querySelector("input");
    input.focus();
    S.editingNote = lap.id;
    let done = false;
    const finish = async (save) => {
      if (done) return;
      done = true;
      if (save && input.value.trim() !== (lap.note || "")) {
        try {
          const m = await F.api(`/api/laps/${lap.id}`, { method: "PATCH", body: { note: input.value.trim() } });
          lap.note = m.note;
        } catch (err) { alert(`保存备注失败：${err.message}`); }
      }
      S.editingNote = null;
      renderTable();
      flushPendingReload();
    };
    input.onkeydown = (e) => { if (e.key === "Enter") finish(true); if (e.key === "Escape") finish(false); };
    input.onblur = () => finish(true);
  }

  // ------------------------------------------------------------------ 分段 / 理论最佳（/api/sectors）
  function secQuery() {
    const [game, ord] = (S.carKey || ":").split(":");
    const p = new URLSearchParams({ track_id: S.trackId, car_ordinal: ord, game });
    if (S.setupFilter) p.set("setup_id", S.setupFilter);
    return p.toString();
  }

  async function loadSectors(force = false) {
    const card = $("#sectors-card");
    if (S.trackId == null || !S.carKey) { card.hidden = true; return; }
    const key = secQuery();
    if (!force && key === S.secKey && S.sec) return;
    const seq = ++S.secSeq;
    try {
      const res = await F.api(`/api/sectors?${key}`);
      if (seq !== S.secSeq) return;
      S.sec = res; S.secKey = key;
      renderSectors();
    } catch (e) {
      if (seq !== S.secSeq) return;
      S.sec = null; S.secKey = key;
      // 404 = 还没有干净圈（接口在）；其它错误（接口不存在 / 出错）：整块藏起来，不影响页面其它部分
      if (/干净|弯道/.test(e.message)) {
        card.hidden = false;
        $("#sec-sub").textContent = "";
        $("#sec-body").innerHTML = `<div class="muted sec-empty">${esc(e.message)}</div>`;
      } else {
        card.hidden = true;
      }
    }
  }

  const fmtS = (v, d = 3) => (v == null ? "—" : v.toFixed(d));
  const lapName = (r) => (r.kind === "sprint" ? `冲刺 #${r.id}` : `第 ${r.lap_number} 圈`);

  function renderSectors() {
    const res = S.sec;
    const card = $("#sectors-card");
    card.hidden = false;
    document.querySelectorAll("#sectors-card [data-view]").forEach((b) => b.classList.toggle("on", b.dataset.view === S.secView));
    const srcLap = S.laps.find((l) => l.id === res.layout.source_lap_id);
    $("#sec-sub").textContent = `弯道表来自${srcLap ? lapName(srcLap) : `圈 #${res.layout.source_lap_id}`} · ${res.segments.length} 段`;
    const used = res.laps.filter((r) => r.used);
    const excl = res.laps.filter((r) => !r.used);
    const th = res.theoretical_best;
    const worst = res.consistency.most_inconsistent;
    const summary = `<div class="sec-summary">
        <div class="sec-stat"><span class="muted small">理论最佳（各段最快之和）</span>
          <span class="big mono">${th ? F.fmtLap(th.time_s) : "--:--.---"}</span>
          <span class="small">${th ? `比最快圈（${lapName(used.find((r) => r.id === th.fastest_lap_id) || { lap_number: th.fastest_lap_number })} ${F.fmtLap(th.fastest_lap_time)}）快 <b class="gain-t">${th.gain_s.toFixed(3)} s</b>` : "还没有足够的干净圈"}</span></div>
        <div class="sec-stat"><span class="muted small">最不稳的段</span>
          <span class="big">${worst ? `${esc(worst.name)} <span class="mono">±${worst.std_s.toFixed(2)} s</span>` : "—"}</span>
          <span class="small muted">${esc(worst ? "每圈用时波动最大，先练这里" : res.consistency.summary)}</span></div>
        <div class="sec-stat"><span class="muted small">参与统计</span>
          <span class="big">${used.length} 圈</span>
          <span class="small muted">${excl.length ? `排除 ${excl.length} 圈：${esc(excl.slice(0, 4).map((r) => `${lapName(r)}（${r.reason}）`).join("、"))}${excl.length > 4 ? "…" : ""}` : "没有排除的圈"}</span></div>
      </div>`;
    const body = S.secView === "matrix" ? secMatrix(res, used) : secTable(res);
    $("#sec-body").innerHTML = summary + body +
      `<div class="muted small sec-note">${esc(res.notes.join("；"))}。点“来自”的圈号：把那圈设为 B，跳到下面对比这一段。</div>`;
  }

  function secTable(res) {
    const stds = res.segments.map((s) => s.std_s ?? 0);
    const maxStd = Math.max(0.05, ...stds);
    const worst = res.consistency.most_inconsistent?.code;
    const rows = res.segments.map((s) => {
      const dir = s.direction === "right" ? "右" : s.direction === "left" ? "左" : "";
      const nameCell = S.renaming === "sector" && S.secRename === s.code
        ? `<input class="sec-name-input" maxlength="30" value="${esc(s.name)}" data-code="${esc(s.code)}" aria-label="弯名">`
        : `<button class="link sec-name" data-rename="${esc(s.code)}" title="点击改名（编号 ${esc(s.code)} 不变）">${esc(s.name)}</button>${s.name !== s.code ? `<small class="muted">${esc(s.code)}</small>` : ""}`;
      const from = s.best_lap_id != null
        ? `<button class="link" data-jump="${s.best_lap_id}" data-code="${esc(s.code)}" title="把这圈设为 B，对比这一段">${esc(lapName({ kind: S.laps.find((l) => l.id === s.best_lap_id)?.kind, lap_number: s.best_lap_number, id: s.best_lap_id }))}</button>`
        : "—";
      const bar = s.std_s == null ? "" : `<span class="bar"><i style="width:${Math.min(100, s.std_s / maxStd * 100).toFixed(0)}%"></i></span>`;
      const disp = [s.brake_std_m != null ? `刹车点 ±${s.brake_std_m.toFixed(0)} m` : "", s.min_speed_std_kmh != null ? `弯心速度 ±${s.min_speed_std_kmh.toFixed(1)} km/h` : ""].filter(Boolean).join(" · ");
      return `<tr class="${s.code === worst ? "worst" : ""}">
        <td class="c-seg">${nameCell}<small class="muted dir">${dir}</small></td>
        <td class="c-range muted">${Math.round(s.start_m)}–${Math.round(s.end_m)} m</td>
        <td class="c-time">${fmtS(s.best_s)}</td>
        <td class="c-from">${from}</td>
        <td class="c-med">${fmtS(s.median_s)}</td>
        <td class="c-std"><span class="mono">${s.std_s == null ? "—" : `±${s.std_s.toFixed(3)}`}</span>${bar}${disp ? `<small class="muted disp">${esc(disp)}</small>` : ""}</td>
        <td class="c-n muted">${s.n}${s.outliers ? `<small title="偏离中位数 > 20% 的不计入">（剔除 ${s.outliers}）</small>` : ""}</td>
      </tr>`;
    }).join("");
    return `<div class="table-wrap sec-wrap"><table class="sec-table">
      <thead><tr><th>段</th><th class="c-range">范围</th><th>最快 s</th><th>来自</th><th class="c-med">中位数</th><th>波动（标准差）</th><th class="c-n">圈数</th></tr></thead>
      <tbody>${rows}</tbody></table></div>`;
  }

  // 逐圈 × 逐段：每格 = 比这段最快慢多少；0 = 这段的最快（理论最佳就是这些格拼起来的）
  function secMatrix(res, used) {
    if (!used.length) return `<div class="muted sec-empty">没有参与统计的圈</div>`;
    const all = used.flatMap((r) => r.delta_to_best.filter((v, j) => v != null && !r.outlier[j]));
    const maxD = Math.max(0.1, ...all);
    const head = res.segments.map((s) => `<th title="${esc(s.code)}">${esc(s.name)}</th>`).join("");
    const rows = used.map((r) => {
      const cells = r.delta_to_best.map((v, j) => {
        if (r.outlier[j]) return `<td class="out" title="偏离这段中位数超过 20%，不计入">${fmtS(r.times[j], 2)}</td>`;
        if (v == null) return `<td>—</td>`;
        const best = v <= 0.0005;
        const t = Math.min(1, v / maxD);
        const bg = best ? alpha(COL.gain, 0.28) : t < 0.08 ? "transparent" : alpha(COL.loss, 0.12 + 0.5 * t);
        return `<td class="${best ? "best" : ""}" style="background:${bg}" title="${esc(res.segments[j].name)}：${fmtS(r.times[j])} s">${best ? "最快" : `+${v.toFixed(2)}`}</td>`;
      }).join("");
      return `<tr class="${r.id === S.b ? "row-b" : r.id === S.a ? "row-a" : ""}"><th><button class="link" data-jump="${r.id}" title="把这圈设为 B">${esc(lapName(r))}</button><small class="muted mono">${F.fmtLap(r.lap_time)}</small></th>${cells}</tr>`;
    }).join("");
    return `<div class="table-wrap sec-wrap"><table class="sec-matrix"><thead><tr><th>圈</th>${head}</tr></thead><tbody>${rows}</tbody></table></div>`;
  }

  // 点“来自第 N 圈”：设为 B（如果它正是 A，就和 B 对调），对比出来后选中这一段
  function jumpToLap(id, code) {
    if (id !== S.b) {
      if (id === S.a) S.a = S.b;
      S.b = id;
      if (!visibleLaps().some((l) => l.id === id)) { S.completeOnly = false; S.setupFilter = ""; renderFilters(); }
    }
    S.pendingSector = code || null;
    chooseDefaults();
    renderTable(); syncUrl();
    if (S.sec && S.secView === "matrix") renderSectors();
    $("#compare").scrollIntoView({ behavior: "smooth", block: "start" });
    if (S.cmpKey === cmpKeyNow() && S.cmp) applyPendingSector(); else runCompare();
  }

  $("#sec-body").addEventListener("click", (e) => {
    const j = e.target.closest("[data-jump]");
    if (j) return jumpToLap(Number(j.dataset.jump), j.dataset.code);
    const r = e.target.closest("[data-rename]");
    if (r) {
      S.renaming = "sector"; S.secRename = r.dataset.rename;
      renderSectors();
      const input = $(".sec-name-input");
      if (input) { input.focus(); input.select(); }
    }
  });
  $("#sec-body").addEventListener("keydown", (e) => {
    const input = e.target.closest(".sec-name-input");
    if (!input) return;
    if (e.key === "Enter") saveSectorName(input);
    if (e.key === "Escape") { S.renaming = null; renderSectors(); flushPendingReload(); }
  });
  $("#sec-body").addEventListener("focusout", (e) => {
    const input = e.target.closest(".sec-name-input");
    if (input && S.renaming === "sector") saveSectorName(input);
  });

  async function saveSectorName(input) {
    if (S.renaming !== "sector") return;
    S.renaming = null;
    const code = input.dataset.code, name = input.value.trim();
    const old = S.sec?.segments.find((s) => s.code === code)?.name;
    if (name !== old) {
      try {
        await F.api(`/api/sectors/${S.trackId}/corners/${encodeURIComponent(code)}`, { method: "PATCH", body: { name } });
        S.cmpKey = null;            // 逐弯分析里的名字也跟着变
        await loadSectors(true);
        runCompare();
      } catch (err) { alert(`改名失败：${err.message}`); renderSectors(); }
    } else {
      renderSectors();
    }
    flushPendingReload();
  }

  document.querySelectorAll("#sectors-card [data-view]").forEach((b) => {
    b.onclick = () => { S.secView = b.dataset.view; if (S.sec) renderSectors(); };
  });

  $("#sec-rebuild").onclick = async () => {
    if (!confirm("用这条赛道最快的干净圈重新识别弯道？\n弯道编号可能会变；改过的弯名会按位置自动沿用。")) return;
    try {
      const r = await F.api(`/api/sectors/${S.trackId}/rebuild`, { method: "POST", body: {} });
      const kept = r.kept_names.map((k) => k.name).join("、");
      toast(`弯道表已重算：${r.layout.corners.length} 个弯${kept ? `，沿用了改过的名字：${kept}` : ""}`);
      S.cmpKey = null;
      await loadSectors(true);
      runCompare();
    } catch (err) { alert(`重算失败：${err.message}`); }
  };

  // ------------------------------------------------------------------ 对比
  async function runCompare() {
    const sec = $("#compare");
    const ls = visibleLaps();
    sec.hidden = false;
    if (lapsOfTrackCar().length < 2) {
      return showCmpState(`这条赛道 + 这辆车目前只有 ${lapsOfTrackCar().length} 圈，至少要两圈才能对比。再跑一圈就有了。`);
    }
    if (!S.a || !S.b || ls.length < 2) {
      return showCmpState("在上面的列表里各选一圈 A 和 B 就能对比。");
    }
    const key = cmpKeyNow();
    if (key === S.cmpKey && S.cmp) return;
    const sameLaps = S.cmp && S.cmpKey && S.cmpKey.split("|")[0] === key.split("|")[0];
    const seq = ++S.cmpSeq;
    showCmpState(`<span class="spinner"></span>正在对比…`, false, true);
    try {
      const ch = extraChannels();
      const res = await F.api(`/api/compare?a=${S.a}&b=${S.b}${ch.length ? `&channels=${ch.join(",")}` : ""}`);
      if (seq !== S.cmpSeq) return;           // 期间又换了选择，丢掉旧结果
      const keepZoom = sameLaps ? S.charts[0]?.scales.x : null;   // 只是多勾了曲线：保持放大范围
      S.cmp = res; S.cmpKey = key;
      if (!sameLaps) S.focusMark = null;
      S.extraOptions = res.extra_options || S.extraOptions;
      if (!res.segments.some((s) => s.name === S.selSeg)) S.selSeg = null;
      renderCompare(keepZoom);
    } catch (e) {
      if (seq !== S.cmpSeq) return;
      console.error(e);
      S.cmp = null; S.cmpKey = null;
      showCmpState(`对比失败：${esc(e.message)} <button class="small-btn" id="cmp-retry">重试</button>`, true);
      $("#cmp-retry").onclick = () => runCompare();
    }
  }

  function showCmpState(html, isError = false, keepOld = false) {
    const st = $("#cmp-state");
    st.hidden = false;
    st.classList.toggle("error", isError);
    st.innerHTML = html;
    const showOld = keepOld && S.cmp;
    $("#cmp-head").hidden = !showOld;
    $("#cmp-body").hidden = !showOld;
    if (showOld) $("#cmp-body").style.opacity = 0.5;
  }

  function cmpKeyNow() { return `${S.a}-${S.b}|${extraChannels().join(",")}`; }
  // 勾选的、需要后端返回的额外曲线（走线偏移总是返回，不用请求）
  function extraChannels() { return [...S.extraSel].filter((k) => k !== "offset").sort(); }

  function renderCompare(keepZoom = null) {
    const res = S.cmp;
    $("#cmp-state").hidden = true;
    $("#cmp-head").hidden = false;
    $("#cmp-body").hidden = false;
    $("#cmp-body").style.opacity = "";
    renderHead(res);
    renderSegList();
    buildCharts(res, keepZoom);
    renderMap();
    renderPicker();
    // 深链 at=（B 圈自己的距离）：乘 scale 换到对比网格，放大 ±150 m 并标出来
    if (S.focusAt != null && res.other.id === S.b) {
      const xm = S.focusAt * res.scale;
      S.focusAt = null;
      S.focusMark = xm;
      zoomX(Math.max(0, xm - 150), Math.min(res.length_m, xm + 150));
      $("#compare").scrollIntoView({ block: "start" });
    }
    applyPendingSector();
  }

  function applyPendingSector() {
    const code = S.pendingSector;
    if (!code || !S.cmp) return;
    S.pendingSector = null;
    if (segByName(code)) selectSeg(code);
  }

  function lapTitle(m) {
    if (!m) return "";
    const n = m.kind === "sprint" ? "冲刺" : `第 ${m.lap_number} 圈`;
    return `${n} · <span class="mono">${F.fmtLap(m.lap_time)}</span>`;
  }

  function renderHead(res) {
    const t = res.totals;
    const total = t.lap_time_delta_s ?? t.delta_t_end_s;
    const cls = Math.abs(total) < 0.0005 ? "" : total > 0 ? "loss" : "gain";
    const verdict = Math.abs(total) < 0.0005 ? "一样快" : total > 0 ? "B 比 A 慢" : "B 比 A 快";
    const bestA = S.laps.find((l) => l.id === res.ref.id)?.is_best;
    const bestB = S.laps.find((l) => l.id === res.other.id)?.is_best;
    const warn = res.warnings.length
      ? `<ul class="warnings">${res.warnings.map((w) => `<li>${esc(w)}</li>`).join("")}</ul>` : "";
    $("#cmp-head").innerHTML = `
      <div class="lap-chips">
        <div class="lap-chip"><span class="sw" style="background:${COL.a}"></span><b>A</b>
          <span>${lapTitle(res.ref)}</span><span class="who">参考圈${bestA ? " · 最快" : ""}</span></div>
        <div class="lap-chip"><span class="sw" style="background:${COL.b}"></span><b>B</b>
          <span>${lapTitle(res.other)}</span><span class="who">要分析的圈${bestB ? " · 最快" : ""}</span></div>
      </div>
      <div class="cmp-total"><span class="big ${cls}">${F.fmtDelta(total)} s</span><span class="muted small">${verdict}</span></div>
      <div class="cmp-summary"><div class="sentence">B ${esc(t.summary)}</div>${warn}</div>
      <button class="small-btn" id="swap-ab" title="把 A、B 对调">⇄ 对调 A / B</button>`;
    $("#swap-ab").onclick = () => {
      [S.a, S.b] = [S.b, S.a];
      renderTable(); syncUrl(); runCompare();
      if (S.sec && S.secView === "matrix") renderSectors();
    };
  }

  // ------------------------------------------------------------------ 逐弯列表
  function segNums(s) {
    if (s.kind === "start") {
      return `<span class="nw">过线车速 <span class="va">${Math.round(s.line_speed_ref_kmh ?? 0)}</span> / <span class="vb">${Math.round(s.line_speed_other_kmh ?? 0)}</span> km/h</span>`;
    }
    const pair = (a, b, unit) => `<span class="va">${a ?? "—"}</span> / <span class="vb">${b ?? "—"}</span>${unit}`;
    const grp = (html) => `<span class="nw">${html}</span>`;
    const r0 = (v) => (v == null ? null : Math.round(v));
    return [
      grp(`刹车点 ${pair(r0(s.brake_ref_m), r0(s.brake_other_m), " m")}`),
      grp(`最低速 ${pair(r0(s.min_speed_ref_kmh), r0(s.min_speed_other_kmh), " km/h")}`),
      grp(`全油门 ${pair(r0(s.full_throttle_ref_m), r0(s.full_throttle_other_m), " m")}`),
    ].join("");
  }

  function renderSegList() {
    const res = S.cmp;
    let segs = res.segments.slice();
    if (S.segSort === "loss") segs.sort((x, y) => y.time_delta_s - x.time_delta_s);
    const maxAbs = Math.max(0.05, ...segs.map((s) => Math.abs(s.time_delta_s)));
    $("#seg-list").innerHTML = segs.map((s) => {
      const dt = s.time_delta_s;
      const flat = Math.abs(dt) < 0.03;
      const cls = flat ? "flat" : dt > 0 ? "loss" : "gain";
      // 解释句 = “T3：慢 0.52 s。刹车点早 24 m，……”；名字和时间差在左右两栏，中间只放后半句
      const body = s.clauses.length ? s.clauses.join("，")
        : (flat ? "基本持平" : s.explanation.split("。").slice(1).join("。") || s.explanation);
      const dir = s.kind === "start" ? "直道" : s.direction === "right" ? "右弯" : "左弯";
      const label = s.kind === "start" ? "起点" : (s.label || s.name);
      return `<div class="seg-row ${S.selSeg === s.name ? "on" : ""}" data-name="${esc(s.name)}" title="${esc(s.explanation)}">
        <div class="seg-name">${esc(label)}<small>${dir}${label !== s.name && s.kind !== "start" ? ` · ${esc(s.name)}` : ""}</small></div>
        <div><div class="seg-text ${flat && !s.clauses.length ? "flat" : ""}">${esc(body)}</div>
             <div class="seg-nums">${segNums(s)}</div></div>
        <div class="seg-dt ${cls}">${F.fmtDelta(dt, 2)}<span class="bar"><i style="width:${Math.min(100, Math.abs(dt) / maxAbs * 100).toFixed(0)}%"></i></span></div>
      </div>`;
    }).join("");
  }

  $("#seg-list").addEventListener("click", (e) => {
    const row = e.target.closest(".seg-row");
    if (!row) return;
    selectSeg(S.selSeg === row.dataset.name ? null : row.dataset.name);
  });
  document.querySelectorAll(".seg-card .seg-sort button").forEach((b) => {
    b.onclick = () => {
      S.segSort = b.dataset.sort;
      document.querySelectorAll(".seg-card .seg-sort button").forEach((x) => x.classList.toggle("on", x === b));
      if (S.cmp) renderSegList();
    };
  });

  function segByName(name) { return S.cmp?.segments.find((s) => s.name === name) || null; }
  function segAt(d) {
    const segs = S.cmp?.segments || [];
    let hit = null;
    for (const s of segs) if (s.seg_start_m <= d) hit = s;
    return hit;
  }

  // 选中一段：曲线放大到这段，赛道图高亮
  function selectSeg(name) {
    S.selSeg = name;
    document.querySelectorAll(".seg-row").forEach((r) => r.classList.toggle("on", r.dataset.name === name));
    const s = segByName(name);
    if (s) {
      const pad = Math.max(10, (s.seg_end_m - s.seg_start_m) * 0.04);
      zoomX(Math.max(0, s.seg_start_m - pad), Math.min(S.cmp.length_m, s.seg_end_m + pad));
    } else {
      zoomX(null, null);
    }
  }

  // ------------------------------------------------------------------ 曲线（uPlot）
  let syncingScale = false;

  function destroyCharts() {
    S.charts.forEach((u) => u.destroy());
    S.charts = [];
    $("#charts").innerHTML = "";
  }

  function zoomX(min, max) {
    if (!S.charts.length || !S.cmp) return;
    const x = S.cmp.channels.dist;
    S.charts[0].setScale("x", { min: min ?? x[0], max: max ?? x[x.length - 1] });
  }

  function onSetScale(u, key) {
    if (key !== "x" || syncingScale) return;
    syncingScale = true;
    const { min, max } = u.scales.x;
    S.charts.forEach((o) => { if (o !== u) o.setScale("x", { min, max }); });
    syncingScale = false;
    const x = S.cmp.channels.dist;
    const full = min <= x[0] + 1 && max >= x[x.length - 1] - 1;
    $("#zoom-label").textContent = full ? "全圈" : `${Math.round(min)}–${Math.round(max)} m`;
    $("#zoom-reset").hidden = full;
    if (full && S.selSeg) {
      S.selSeg = null;
      document.querySelectorAll(".seg-row.on").forEach((r) => r.classList.remove("on"));
    }
    renderMap();
  }

  function onSetCursor(u) {
    const idx = u.cursor.idx;
    if (idx === S.hoverIdx) return;
    S.hoverIdx = idx;
    updateReadout(idx);
    drawMapOverlay();
  }

  // 弯道底色 + 选中段高亮 + 弯名标签（标签只画在 labels=true 的图上）
  function cornerPlugin(labels) {
    return {
      hooks: {
        drawSeries: [(u, si) => {
          // 深链位置：竖虚线（每张图画一次，挂在最后一条曲线之后）
          if (S.focusMark == null || si !== u.series.length - 1) return;
          const x = u.valToPos(S.focusMark, "x", true);
          const { left, top, width, height } = u.bbox;
          if (x < left || x > left + width) return;
          const { ctx } = u;
          ctx.save();
          ctx.strokeStyle = COL.text; ctx.globalAlpha = 0.7;
          ctx.setLineDash([4 * DPR(), 4 * DPR()]); ctx.lineWidth = DPR();
          ctx.beginPath(); ctx.moveTo(x, top); ctx.lineTo(x, top + height); ctx.stroke();
          ctx.restore();
        }],
        drawClear: [(u) => {
          const res = S.cmp;
          if (!res) return;
          const { ctx } = u;
          const { left, top, width, height } = u.bbox;
          ctx.save();
          ctx.beginPath(); ctx.rect(left, top, width, height); ctx.clip();
          const sel = segByName(S.selSeg);
          if (sel) {
            const x0 = u.valToPos(sel.seg_start_m, "x", true), x1 = u.valToPos(sel.seg_end_m, "x", true);
            ctx.fillStyle = alpha(COL.a, 0.07);
            ctx.fillRect(x0, top, x1 - x0, height);
          }
          ctx.fillStyle = "rgba(255,255,255,0.05)";
          for (const s of res.segments) {
            if (s.kind !== "corner") continue;
            const x0 = u.valToPos(s.start_m, "x", true), x1 = u.valToPos(s.end_m, "x", true);
            ctx.fillRect(x0, top, Math.max(1, x1 - x0), height);
          }
          ctx.restore();
        }],
        draw: labels ? [(u) => {
          const res = S.cmp;
          if (!res) return;
          const { ctx } = u;
          const { left, top, width } = u.bbox;
          const dpr = DPR();
          ctx.save();
          ctx.font = `${600} ${11 * dpr}px system-ui, sans-serif`;
          ctx.textAlign = "center";
          ctx.textBaseline = "top";
          let lastX = -1e9;
          for (const s of res.segments) {
            if (s.kind !== "corner") continue;
            const x = u.valToPos(s.apex_m, "x", true);
            const lb = s.label || s.name;
            const need = Math.max(22, ctx.measureText(lb).width / dpr + 8) * dpr;
            if (x < left || x > left + width || x - lastX < need) continue;
            lastX = x;
            ctx.fillStyle = s.name === S.selSeg ? COL.text : COL.muted;
            ctx.fillText(lb, x, top + 3 * dpr);
          }
          ctx.restore();
        }] : [],
      },
    };
  }

  function buildCharts(res, keepZoom = null) {
    destroyCharts();
    if (typeof uPlot === "undefined") {
      $("#charts").innerHTML = `<div class="cmp-state error">曲线库（/vendor/uPlot.iife.min.js）没加载成功，刷新试试。</div>`;
      return;
    }
    const ch = res.channels;
    const x = ch.dist;
    const A = ch.ref, B = ch.other;
    const scale100 = (arr, k = 100) => arr.map((v) => (v == null ? null : v * k));
    // 0 以上（B 慢）红、0 以下（B 快）青：用一条曲线 + 在 y=0 处硬切换的竖直渐变
    const zeroSplit = (pos, neg) => (u) => {
      const { top, height } = u.bbox;
      const y0 = u.valToPos(0, "y", true);
      const t = Math.min(1, Math.max(0, (y0 - top) / Math.max(1, height)));
      const g = u.ctx.createLinearGradient(0, top, 0, top + height);
      g.addColorStop(0, pos); g.addColorStop(t, pos); g.addColorStop(t, neg); g.addColorStop(1, neg);
      return g;
    };
    const line = (color, extra = {}) => ({ stroke: color, width: 1.75, points: { show: false }, ...extra });
    const stepped = uPlot.paths.stepped({ align: 1 });
    const steerMax = Math.max(10, ...A.steer.concat(B.steer).map((v) => Math.abs(v ?? 0) * 100));
    const gears = A.gear.concat(B.gear).filter((v) => v != null && v > 0);
    const gearMax = gears.length ? Math.max(...gears) : 6;
    const gearMin = gears.length ? Math.min(...gears) : 1;

    const specs = [
      {
        title: "累计时间差 Δt（B − A，向上 = B 慢）", h: 130, labels: true,
        data: [x, ch.delta_t],
        series: [line(COL.loss, {
          stroke: zeroSplit(COL.loss, COL.gain),
          fill: zeroSplit(alpha(COL.loss, 0.22), alpha(COL.gain, 0.22)),
          fillTo: 0,
        })],
        points: { fill: () => COL.text, stroke: () => COL.text },
        range: (u, mn, mx) => {
          const lo = Math.min(mn ?? 0, 0), hi = Math.max(mx ?? 0, 0);
          const span = Math.max(hi - lo, 0.1);
          return [lo - span * 0.08, hi + span * 0.25];     // 上面多留一截给弯名
        },
        fmt: (v) => (v > 0 ? "+" : "") + v.toFixed(Math.abs(v) < 1 ? 2 : 1),
      },
      {
        title: "车速 km/h", h: 190, labels: true,
        data: [x, A.speed, B.speed], series: [line(COL.a), line(COL.b)],
        range: (u, mn, mx) => [Math.max(0, mn - (mx - mn) * 0.05), mx + (mx - mn) * 0.16 + 1],
        fmt: (v) => v.toFixed(0),
      },
      {
        title: "油门 ↑ / 刹车 ↓（%）", h: 150,
        data: [x, scale100(A.throttle), scale100(B.throttle), scale100(A.brake, -100), scale100(B.brake, -100)],
        series: [line(COL.a), line(COL.b), line(COL.a), line(COL.b)],
        range: () => [-105, 105],
        fmt: (v) => Math.abs(v).toFixed(0),
      },
      {
        title: "方向（%，向上 = 右）", h: 100, space: 18,
        data: [x, scale100(A.steer), scale100(B.steer)], series: [line(COL.a), line(COL.b)],
        range: () => [-steerMax * 1.1, steerMax * 1.1],
        fmt: (v) => v.toFixed(0),
      },
      ...extraSpecs(res, line),
      {
        title: "挡位", h: 100, xAxis: true,
        splits: () => Array.from({ length: gearMax - gearMin + 1 }, (_, i) => gearMin + i)
          .filter((g) => gearMax - gearMin <= 5 || g % 2),
        data: [x, A.gear, B.gear],
        series: [line(COL.a, { paths: stepped }), line(COL.b, { paths: stepped })],
        range: () => [gearMin - 0.5, gearMax + 0.5],
        fmt: (v) => (Number.isInteger(v) ? String(v) : ""),
      },
    ];

    const host = $("#charts");
    const width = Math.max(260, host.clientWidth);
    const font = `12px system-ui, sans-serif`;
    for (const sp of specs) {
      const wrap = document.createElement("div");
      wrap.innerHTML = `<div class="chart-title">${esc(sp.title)}</div>`;
      host.appendChild(wrap);
      const opts = {
        width, height: sp.h + (sp.xAxis ? 26 : 0),
        legend: { show: false },
        cursor: {
          sync: { key: "forza-cmp", setSeries: false },
          y: false,                                   // 只要竖线：几张图共用一个距离光标
          drag: { x: true, y: false, setScale: true },
          points: { size: 7, ...(sp.points || {}) },
        },
        select: { show: true },
        scales: { x: { time: false }, y: sp.range ? { range: sp.range } : {} },
        axes: [
          {
            stroke: COL.muted, font, grid: { stroke: GRID, width: 1 }, ticks: { show: false },
            size: sp.xAxis ? 30 : 4,
            values: sp.xAxis ? (u, vals) => vals.map((v) => `${v} m`) : (u, vals) => vals.map(() => ""),
          },
          {
            stroke: COL.muted, font, size: 46, grid: { stroke: GRID, width: 1 }, ticks: { show: false },
            values: (u, vals) => vals.map(sp.fmt),
            ...(sp.splits ? { splits: sp.splits } : {}),
            ...(sp.space ? { space: sp.space } : {}),
          },
        ],
        series: [{}, ...sp.series],
        plugins: [cornerPlugin(sp.labels)],
        hooks: { setScale: [onSetScale], setCursor: [onSetCursor] },
      };
      const u = new uPlot(opts, sp.data, wrap);
      u._h = opts.height;
      S.charts.push(u);
    }
    // 图例（曲线自己不带图例，统一放上面一行）
    $("#chart-legend").innerHTML =
      `<span class="item"><span class="sw" style="background:${COL.a}"></span>A ${lapTitle(res.ref)}</span>` +
      `<span class="item"><span class="sw" style="background:${COL.b}"></span>B ${lapTitle(res.other)}</span>`;
    S.hoverIdx = null;
    updateReadout(null);
    const sel = segByName(S.selSeg);
    if (keepZoom && keepZoom.min != null) S.charts[0].setScale("x", { min: keepZoom.min, max: keepZoom.max });
    else if (sel) selectSeg(sel.name); else onSetScale(S.charts[0], "x");
  }

  // 走线偏移 + 勾选的额外曲线。A = 蓝、B = 橙；同一张图里两条子曲线用实线 / 虚线区分
  function extraSpecs(res, line) {
    const ch = res.channels, x = ch.dist;
    const A = ch.ref, B = ch.other;
    const out = [];
    const dash = { dash: [5, 4] };
    const symRange = (minSpan) => (u, mn, mx) => {
      const m = Math.max(minSpan, Math.abs(mn ?? 0), Math.abs(mx ?? 0)) * 1.1;
      return [-m, m];
    };
    const padRange = (u, mn, mx) => {
      if (mn == null || mx == null) return [0, 1];
      const p = Math.max((mx - mn) * 0.08, 0.05);
      return [mn - p, mx + p];
    };
    if (S.extraSel.has("offset") && ch.lateral_offset) {
      out.push({
        title: "走线偏移：B 相对 A 的横向距离（m，向上 = B 更靠右）", h: 100, space: 20,
        data: [x, ch.lateral_offset], series: [line(COL.b, { fill: alpha(COL.b, 0.12), fillTo: 0 })],
        range: symRange(1), fmt: (v) => v.toFixed(Math.abs(v) < 10 ? 1 : 0),
      });
    }
    for (const opt of res.extra_options || []) {
      const e = res.extra[opt.key];
      if (!e || !S.extraSel.has(opt.key)) continue;
      const names = e.series.map((s) => s.name);
      const fmt2 = (v) => v.toFixed(Math.abs(v) < 10 ? 2 : 0);
      if (opt.key === "tire_temp" || opt.key === "susp") {
        // 四轮：前轴一张、后轴一张，实线 = 左、虚线 = 右
        for (const [axle, l, r] of [["前轴", names[0], names[1]], ["后轴", names[2], names[3]]]) {
          out.push({
            title: `${e.title} · ${axle}（实线 = 左，虚线 = 右）`, h: 110, space: 22,
            data: [x, A[l], A[r], B[l], B[r]],
            series: [line(COL.a), line(COL.a, dash), line(COL.b), line(COL.b, dash)],
            range: padRange, fmt: opt.key === "susp" ? (v) => v.toFixed(2) : (v) => v.toFixed(0),
          });
        }
      } else if (names.length === 2) {
        out.push({
          title: `${e.title}（实线 = ${e.series[0].label}，虚线 = ${e.series[1].label}）`, h: 110, space: 22,
          data: [x, A[names[0]], A[names[1]], B[names[0]], B[names[1]]],
          series: [line(COL.a), line(COL.a, dash), line(COL.b), line(COL.b, dash)],
          range: padRange, fmt: fmt2,
        });
      } else {
        out.push({
          title: `${e.title}${e.unit && !e.title.includes(e.unit) ? `（${e.unit}）` : ""}`, h: 110, space: 22,
          data: [x, A[names[0]], B[names[0]]], series: [line(COL.a), line(COL.b)],
          range: opt.key === "g_lat" || opt.key === "slip_ratio" ? symRange(0.5) : padRange, fmt: fmt2,
        });
      }
    }
    return out;
  }

  // ---- “更多曲线”选择框
  const EXTRA_FALLBACK = [
    { key: "g_lat", title: "横向 G" }, { key: "g_comb", title: "合成 G" },
    { key: "slip_angle", title: "前后轴平均 |侧偏角|" }, { key: "slip_ratio", title: "驱动轮滑移率" },
    { key: "tire_temp", title: "胎温" }, { key: "susp", title: "悬挂行程" },
  ];
  function renderPicker() {
    const opts = [{ key: "offset", title: "走线偏移（B 相对 A 的横向距离）" }]
      .concat((S.extraOptions || EXTRA_FALLBACK).map((o) => ({ key: o.key, title: o.title.replace(/（.*$/, "") })));
    $("#extra-picker").innerHTML = opts.map((o) =>
      `<label class="chk"><input type="checkbox" value="${esc(o.key)}" ${S.extraSel.has(o.key) ? "checked" : ""}> ${esc(o.title)}</label>`).join("") +
      `<div class="muted small">勾选后在曲线区多画这几张图（胎温、悬挂按前 / 后轴分两张）</div>`;
    const n = S.extraSel.size;
    $("#extra-btn").textContent = `更多曲线${n ? `（${n}）` : ""} ▾`;
  }
  $("#extra-btn").onclick = (e) => {
    e.stopPropagation();
    const pk = $("#extra-picker");
    if (pk.hidden) renderPicker();
    pk.hidden = !pk.hidden;
    $("#extra-btn").setAttribute("aria-expanded", String(!pk.hidden));
  };
  document.addEventListener("click", (e) => {
    const pk = $("#extra-picker");
    if (!pk.hidden && !e.target.closest(".picker-wrap")) {
      pk.hidden = true;
      $("#extra-btn").setAttribute("aria-expanded", "false");
    }
  });
  $("#extra-picker").addEventListener("change", (e) => {
    const k = e.target.value;
    if (e.target.checked) S.extraSel.add(k); else S.extraSel.delete(k);
    saveExtraSel();
    renderPicker();
    if (!S.cmp) return;
    if (k === "offset") {                     // 走线偏移已经在结果里，不用重新请求
      const keep = S.charts[0]?.scales.x;
      buildCharts(S.cmp, keep);
    } else {
      runCompare();
    }
  });

  $("#zoom-reset").onclick = () => selectSeg(null);

  function updateReadout(idx) {
    const el = $("#readout");
    const res = S.cmp;
    if (!res) { el.innerHTML = ""; return; }
    if (idx == null) {
      el.innerHTML = `<span>把鼠标移到曲线或赛道图上看每个位置的数值</span>`;
      return;
    }
    const ch = res.channels, A = ch.ref, B = ch.other;
    const d = ch.dist[idx];
    const seg = segAt(d);
    const dt = ch.delta_t[idx];
    const pct = (v) => (v == null ? "-" : Math.round(v * 100));
    const v1 = (v) => (v == null ? "-" : v.toFixed(0));
    el.innerHTML = [
      `<span><b class="seg">${Math.round(d)} m</b>${seg ? ` · ${esc(seg.kind === "start" ? "起点直道" : (seg.label || seg.name))}` : ""}</span>`,
      `<span>Δt <b class="${dt > 0 ? "loss" : dt < 0 ? "gain" : ""}">${F.fmtDelta(dt, 3)}</b></span>`,
      `<span>车速 <b class="va">${v1(A.speed[idx])}</b>/<b class="vb">${v1(B.speed[idx])}</b></span>`,
      `<span>油门 <b class="va">${pct(A.throttle[idx])}</b>/<b class="vb">${pct(B.throttle[idx])}</b>%</span>`,
      `<span>刹车 <b class="va">${pct(A.brake[idx])}</b>/<b class="vb">${pct(B.brake[idx])}</b>%</span>`,
      `<span>挡 <b class="va">${A.gear[idx] ?? "-"}</b>/<b class="vb">${B.gear[idx] ?? "-"}</b></span>`,
      `<span>转速 <b class="va">${v1(A.rpm[idx])}</b>/<b class="vb">${v1(B.rpm[idx])}</b></span>`,
      ch.lateral_offset && ch.lateral_offset[idx] != null
        ? `<span>走线 <b class="vb">${ch.lateral_offset[idx] > 0 ? "右" : "左"} ${Math.abs(ch.lateral_offset[idx]).toFixed(1)} m</b></span>` : "",
    ].join("");
  }

  // 容器宽度变化（窗口缩放、手机转屏）时重排曲线和赛道图
  let resizeTimer = null;
  new ResizeObserver(() => {
    clearTimeout(resizeTimer);
    resizeTimer = setTimeout(() => {
      const w = Math.max(260, $("#charts").clientWidth);
      S.charts.forEach((u) => { if (u.width !== w) u.setSize({ width: w, height: u._h }); });
      renderMap();
    }, 80);
  }).observe($("#cmp-body"));

  // ------------------------------------------------------------------ 赛道图（canvas）
  const MAP = { canvas: $("#map"), base: document.createElement("canvas"), pts: null, proj: null, scaleMax: 1 };

  // 丢时间速率 → 颜色：0 附近灰，越丢越红，越赚越青
  function rateColor(r) {
    if (r == null) return COL.neutral;
    const t = Math.max(-1, Math.min(1, r / MAP.scaleMax));
    if (Math.abs(t) < 0.12) return COL.neutral;
    return mix(COL.neutral, t > 0 ? COL.loss : COL.gain, Math.min(1, (Math.abs(t) - 0.12) / 0.7));
  }

  // 全圈：A 的走线按丢 / 赚时间着色；曲线放大到一段时：地图也放大，画 A、B 两条走线 + 刹车点 / 转向点 / 弯心 / 全油门点
  function renderMap() {
    const res = S.cmp;
    const cv = MAP.canvas;
    const wrap = cv.parentElement;
    const old = wrap.querySelector(".map-empty");
    if (old) old.remove();
    if (!res || !res.map) {
      cv.hidden = true;
      MAP.pts = null;
      if (res) wrap.insertAdjacentHTML("beforeend", `<div class="map-empty">这圈没有坐标数据，画不出赛道图</div>`);
      setMapMode(false);
      return;
    }
    cv.hidden = false;
    const m = res.map;
    const dpr = DPR();
    const w = Math.max(200, wrap.clientWidth);
    const xr = S.charts[0]?.scales.x;
    const gx = res.channels.dist;
    const zoomed = !!(xr && xr.min != null && (xr.min > gx[0] + 1 || xr.max < gx[gx.length - 1] - 1));
    const hasB = Array.isArray(m.x_b) && Array.isArray(m.z_b);
    const zoomIdx = zoomed ? [idxOfDist(m.dist, xr.min), idxOfDist(m.dist, xr.max)] : null;
    // 视野范围：全圈 = 整条赛道；放大 = 这一段两条走线的外框（至少 60 m 见方）
    let xs = m.x, zs = m.z;
    if (zoomed) {
      const [i0, i1] = zoomIdx;
      xs = m.x.slice(i0, i1 + 1); zs = m.z.slice(i0, i1 + 1);
      if (hasB) { xs = xs.concat(m.x_b.slice(i0, i1 + 1)); zs = zs.concat(m.z_b.slice(i0, i1 + 1)); }
    }
    let minX = Math.min(...xs), maxX = Math.max(...xs), minZ = Math.min(...zs), maxZ = Math.max(...zs);
    if (zoomed) {
      const cx0 = (minX + maxX) / 2, cz0 = (minZ + maxZ) / 2;
      minX = Math.min(minX, cx0 - 30); maxX = Math.max(maxX, cx0 + 30);
      minZ = Math.min(minZ, cz0 - 30); maxZ = Math.max(maxZ, cz0 + 30);
    }
    const pad = 26;
    const spanX = Math.max(1, maxX - minX), spanZ = Math.max(1, maxZ - minZ);
    const hMax = window.innerWidth > 980 ? Math.min(w * 0.95, 360) : Math.min(w * 1.05, 420);
    const h = Math.round(Math.min(Math.max((w - 2 * pad) * spanZ / spanX + 2 * pad, 180), hMax));
    const k = Math.min((w - 2 * pad) / spanX, (h - 2 * pad) / spanZ);
    const ox = (w - spanX * k) / 2, oz = (h - spanZ * k) / 2;
    // 俯视图：x 向右，z 向上（canvas 的 y 向下，所以取反）。这样画出来的左右弯和实际一致
    MAP.proj = (x, z) => [ox + (x - minX) * k, h - (oz + (z - minZ) * k)];
    MAP.pts = m.x.map((x, i) => MAP.proj(x, m.z[i]));
    MAP.ptsB = hasB ? m.x_b.map((x, i) => MAP.proj(x, m.z_b[i])) : null;
    MAP.w = w; MAP.h = h; MAP.zoomed = zoomed;
    // 颜色刻度：98% 分位（至少 0.08 s/100 m，免得两圈几乎一样时噪声被放大成大红大绿）
    const abs = m.loss_rate.map((v) => Math.abs(v ?? 0)).sort((a, b) => a - b);
    MAP.scaleMax = Math.max(0.08, abs[Math.floor(abs.length * 0.98)] || 0);

    for (const c of [cv, MAP.base]) {
      c.width = Math.round(w * dpr); c.height = Math.round(h * dpr);
    }
    cv.style.height = `${h}px`;
    const ctx = MAP.base.getContext("2d");
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, w, h);
    ctx.lineCap = "round"; ctx.lineJoin = "round";
    const P = MAP.pts, n = P.length;
    const path = (pts, i0, i1) => {
      ctx.beginPath(); ctx.moveTo(pts[i0][0], pts[i0][1]);
      for (let i = i0 + 1; i <= i1; i++) ctx.lineTo(pts[i][0], pts[i][1]);
    };
    setMapMode(zoomed);
    if (zoomed) {
      // 放大：路面底色（按 1 m ≈ 几像素画出路宽感）+ A、B 两条走线 + 标记点
      path(P, 0, n - 1); ctx.strokeStyle = COL.panel2; ctx.lineWidth = Math.max(11, Math.min(40, 12 * k)); ctx.stroke();
      const [i0, i1] = zoomIdx;
      const lo = Math.max(0, i0 - 3), hi = Math.min(n - 1, i1 + 3);
      // A 粗一点画在下面、B 细一点叠在上面：两条线重合时两种颜色都看得见
      ctx.lineWidth = 4.5;
      path(P, lo, hi); ctx.strokeStyle = COL.a; ctx.stroke();
      if (MAP.ptsB) { ctx.lineWidth = 2; path(MAP.ptsB, lo, hi); ctx.strokeStyle = COL.b; ctx.stroke(); }
      drawMarkers(ctx, res, xr.min, xr.max);
      $("#map-sub").textContent = `${Math.round(xr.min)}–${Math.round(xr.max)} m：蓝 = A、橙 = B 的走线`;
      drawMapOverlay();
      return;
    }
    $("#map-sub").textContent = "颜色 = B 在这里丢 / 赚时间";
    // 路面底色
    path(P, 0, n - 1); ctx.strokeStyle = COL.panel2; ctx.lineWidth = 11; ctx.stroke();
    // 按丢 / 赚时间着色
    ctx.lineWidth = 5;
    for (let i = 0; i < n - 1; i++) {
      ctx.beginPath(); ctx.moveTo(P[i][0], P[i][1]); ctx.lineTo(P[i + 1][0], P[i + 1][1]);
      ctx.strokeStyle = rateColor(m.loss_rate[i]); ctx.stroke();
    }
    // 起终点：短横线 + 行驶方向小箭头
    if (n > 3) {
      const [sx, sy] = P[0], [nx, ny] = P[Math.min(3, n - 1)];
      const ang = Math.atan2(ny - sy, nx - sx);
      ctx.save(); ctx.translate(sx, sy); ctx.rotate(ang);
      ctx.strokeStyle = COL.text; ctx.lineWidth = 3;
      ctx.beginPath(); ctx.moveTo(0, -9); ctx.lineTo(0, 9); ctx.stroke();
      ctx.fillStyle = COL.text;
      ctx.beginPath(); ctx.moveTo(18, 0); ctx.lineTo(10, -5); ctx.lineTo(10, 5); ctx.closePath(); ctx.fill();
      ctx.restore();
    }
    // 弯名：从赛道中心往外推一点，免得压在线上
    const cx = w / 2, cy = h / 2;
    ctx.font = "600 12px system-ui, sans-serif";
    ctx.textAlign = "center"; ctx.textBaseline = "middle";
    for (const c of m.corners) {
      const [px, py] = MAP.proj(c.x, c.z);
      let dx = px - cx, dy = py - cy;
      const dl = Math.hypot(dx, dy) || 1;
      dx /= dl; dy /= dl;
      const lx = px + dx * 16, ly = py + dy * 16;
      const lb = c.label || c.name;
      const tw = ctx.measureText(lb).width + 8;
      const on = c.name === S.selSeg;
      ctx.fillStyle = on ? COL.text : "rgba(13,15,18,0.82)";
      roundRect(ctx, lx - tw / 2, ly - 9, tw, 18, 5); ctx.fill();
      ctx.fillStyle = on ? "#0d0f12" : COL.text;
      ctx.fillText(lb, lx, ly + 0.5);
    }
    drawMapOverlay();
  }

  function setMapMode(zoomed) {
    $("#map-legend-full").hidden = zoomed;
    $("#map-legend-zoom").hidden = !zoomed;
  }

  // 沿走线插值出某个距离处的屏幕坐标（side = "a" | "b"）
  function mapPosAt(side, d) {
    const m = S.cmp.map, pts = side === "b" && MAP.ptsB ? MAP.ptsB : MAP.pts;
    const i = idxOfDist(m.dist, d);
    if (i <= 0) return pts[0];
    const d0 = m.dist[i - 1], d1 = m.dist[i];
    const t = d1 > d0 ? Math.min(1, Math.max(0, (d - d0) / (d1 - d0))) : 0;
    return [pts[i - 1][0] + (pts[i][0] - pts[i - 1][0]) * t, pts[i - 1][1] + (pts[i][1] - pts[i - 1][1]) * t];
  }

  // 放大范围里每个弯：两圈各自的 刹车点 ● / 转向点 ▲ / 弯心（最低速）◆ / 全油门 ■
  function drawMarkers(ctx, res, lo, hi) {
    const kinds = [["brake", "circle"], ["turn_in", "tri"], ["min_speed", "diamond"], ["full_throttle", "square"]];
    for (const s of res.segments) {
      if (s.kind !== "corner" || s.seg_end_m < lo || s.seg_start_m > hi) continue;
      // A 的标记大一号画在下面，B 的叠在上面：位置相同时也能看到两个
      for (const [side, col, r] of [["ref", COL.a, 8], ["other", COL.b, 5.5]]) {
        if (side === "other" && !MAP.ptsB) continue;
        for (const [k, shape] of kinds) {
          const d = s[`${k}_${side}_m`];
          if (d == null || d < lo || d > hi) continue;
          const [px, py] = mapPosAt(side === "ref" ? "a" : "b", d);
          shapePath(ctx, shape, px, py, r);
          ctx.fillStyle = col; ctx.fill();
          ctx.lineWidth = 1.5; ctx.strokeStyle = "#0d0f12"; ctx.stroke();
        }
      }
    }
  }

  function shapePath(ctx, shape, x, y, r) {
    ctx.beginPath();
    if (shape === "circle") ctx.arc(x, y, r * 0.85, 0, Math.PI * 2);
    else if (shape === "tri") { ctx.moveTo(x, y - r); ctx.lineTo(x + r, y + r * 0.8); ctx.lineTo(x - r, y + r * 0.8); ctx.closePath(); }
    else if (shape === "diamond") { ctx.moveTo(x, y - r); ctx.lineTo(x + r, y); ctx.lineTo(x, y + r); ctx.lineTo(x - r, y); ctx.closePath(); }
    else ctx.rect(x - r * 0.75, y - r * 0.75, r * 1.5, r * 1.5);
  }

  function roundRect(ctx, x, y, w, h, r) {
    ctx.beginPath();
    ctx.moveTo(x + r, y); ctx.arcTo(x + w, y, x + w, y + h, r); ctx.arcTo(x + w, y + h, x, y + h, r);
    ctx.arcTo(x, y + h, x, y, r); ctx.arcTo(x, y, x + w, y, r); ctx.closePath();
  }

  function idxOfDist(arr, d) {
    let lo = 0, hi = arr.length - 1;
    while (lo < hi) { const mid = (lo + hi) >> 1; if (arr[mid] < d) lo = mid + 1; else hi = mid; }
    return lo;
  }

  // 底图 + 当前光标位置（曲线上悬停的那个距离）
  function drawMapOverlay() {
    if (!MAP.pts) return;
    const cv = MAP.canvas, dpr = DPR();
    const ctx = cv.getContext("2d");
    ctx.setTransform(1, 0, 0, 1, 0, 0);
    ctx.clearRect(0, 0, cv.width, cv.height);
    ctx.drawImage(MAP.base, 0, 0);
    const idx = S.hoverIdx;
    if (idx == null || !S.cmp) return;
    const d = S.cmp.channels.dist[idx];
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    const dots = MAP.zoomed && MAP.ptsB ? [["a", COL.a], ["b", COL.b]] : [["a", COL.text]];
    for (const [side, col] of dots) {
      const [px, py] = mapPosAt(side, d);
      ctx.beginPath(); ctx.arc(px, py, MAP.zoomed ? 5 : 7, 0, Math.PI * 2);
      ctx.fillStyle = col; ctx.fill();
      ctx.lineWidth = 3; ctx.strokeStyle = "#0d0f12"; ctx.stroke();
    }
  }

  // 赛道图上悬停：曲线光标跟着走；点击：选中最近的弯
  function nearestMapIdx(ev) {
    if (!MAP.pts) return null;
    const r = MAP.canvas.getBoundingClientRect();
    const mx = ev.clientX - r.left, my = ev.clientY - r.top;
    let best = -1, bd = 24 * 24;
    MAP.pts.forEach(([x, y], i) => {
      const dd = (x - mx) ** 2 + (y - my) ** 2;
      if (dd < bd) { bd = dd; best = i; }
    });
    return best < 0 ? null : best;
  }
  MAP.canvas.addEventListener("mousemove", (ev) => {
    const i = nearestMapIdx(ev);
    if (i == null || !S.charts.length) return;
    const d = S.cmp.map.dist[i];
    for (const u of S.charts) {
      const { min, max } = u.scales.x;
      if (d < min || d > max) return;
      u.setCursor({ left: u.valToPos(d, "x"), top: 10 });
    }
  });
  MAP.canvas.addEventListener("mouseleave", () => {
    S.charts.forEach((u) => u.setCursor({ left: -10, top: -10 }));
  });
  MAP.canvas.addEventListener("click", (ev) => {
    const i = nearestMapIdx(ev);
    if (i == null) return;
    const seg = segAt(S.cmp.map.dist[i]);
    if (seg) selectSeg(S.selSeg === seg.name ? null : seg.name);
  });

  // ------------------------------------------------------------------ 启动
  $("#page-state").hidden = false;
  $("#page-state").innerHTML = `<span class="spinner"></span>加载中…`;
  load({ initial: true });
})();
