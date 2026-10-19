// 圈速对比页：圈列表（筛选 / 改名 / 备注 / 删除）+ 两圈对比（曲线、逐弯分析、赛道图）。
// 数据：/api/tracks、/api/cars、/api/laps、/api/compare（计算全在后端 forza/analysis.py）。
// 约定：A = 参考圈（--accent 蓝），B = 要分析的圈（--accent-2 橙）；时间差 = B − A，正数 = B 慢。
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
    renaming: null,                   // "track" | "car"
  };

  // ------------------------------------------------------------------ 加载
  async function load(opts = {}) {
    try {
      const [tracks, cars, laps] = await Promise.all([
        F.api("/api/tracks"), F.api("/api/cars"), F.api("/api/laps?limit=5000"),
      ]);
      S.tracks = tracks; S.cars = cars; S.laps = laps;
    } catch (e) {
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
    if (opts.initial) applyUrl();
    pickFilters();
    renderFilters();
    refreshLaps();
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

  function applyUrl() {
    const a = Number(F.qs("a")), b = Number(F.qs("b"));
    const lapA = S.laps.find((l) => l.id === a);
    if (lapA) {
      S.trackId = lapA.track_id;
      S.carKey = carKeyOf(lapA);
      S.a = a;
      if (S.laps.some((l) => l.id === b)) S.b = b;
      return;
    }
    const t = Number(F.qs("track"));
    if (S.tracks.some((x) => x.id === t)) S.trackId = t;
    const c = F.qs("car");
    if (c) S.carKey = c;
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
  function visibleLaps() {
    const ls = lapsOfTrackCar();
    return S.completeOnly ? ls.filter((l) => l.is_complete) : ls;
  }

  function syncUrl() {
    const p = new URLSearchParams();
    if (S.trackId != null) p.set("track", S.trackId);
    if (S.carKey) p.set("car", S.carKey);
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
    $("#complete-only").checked = S.completeOnly;
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
      S.carKey = null; S.a = S.b = null;
      pickFilters(); renderFilters(); refreshLaps();
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
      S.carKey = e.target.value; S.a = S.b = null;
      refreshLaps();
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
    const done = () => { S.renaming = null; renderFilters(); };
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
        await load();
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

  // 筛选 / 数据变了之后：补齐默认选择 → 重画列表 → 更新网址 → 对比
  function refreshLaps() {
    chooseDefaults();
    renderTable();
    syncUrl();
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
      await load();
    } else if (act === "complete") {
      try {
        await F.api(`/api/laps/${id}`, { method: "PATCH", body: { is_complete: !lap.is_complete } });
      } catch (err) { alert(`修改失败：${err.message}`); }
      S.cmpKey = null;   // 最快圈可能变了，重新对比
      await load();
    } else if (e.target.closest(".note-text")) {
      editNote(tr.querySelector(".c-note"), lap);
    }
  });

  function editNote(td, lap) {
    td.innerHTML = `<input class="note-input" maxlength="200" value="${esc(lap.note || "")}" placeholder="比如：T3 刹车早了">`;
    const input = td.querySelector("input");
    input.focus();
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
      renderTable();
    };
    input.onkeydown = (e) => { if (e.key === "Enter") finish(true); if (e.key === "Escape") finish(false); };
    input.onblur = () => finish(true);
  }

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
    const key = `${S.a}-${S.b}`;
    if (key === S.cmpKey && S.cmp) return;
    const seq = ++S.cmpSeq;
    showCmpState(`<span class="spinner"></span>正在对比…`, false, true);
    try {
      const res = await F.api(`/api/compare?a=${S.a}&b=${S.b}`);
      if (seq !== S.cmpSeq) return;           // 期间又换了选择，丢掉旧结果
      S.cmp = res; S.cmpKey = key;
      if (!res.segments.some((s) => s.name === S.selSeg)) S.selSeg = null;
      renderCompare();
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

  function renderCompare() {
    const res = S.cmp;
    $("#cmp-state").hidden = true;
    $("#cmp-head").hidden = false;
    $("#cmp-body").hidden = false;
    $("#cmp-body").style.opacity = "";
    renderHead(res);
    renderSegList();
    buildCharts(res);
    renderMap();
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
      return `<div class="seg-row ${S.selSeg === s.name ? "on" : ""}" data-name="${esc(s.name)}" title="${esc(s.explanation)}">
        <div class="seg-name">${esc(s.kind === "start" ? "起点" : s.name)}<small>${dir}</small></div>
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
  document.querySelectorAll(".seg-sort button").forEach((b) => {
    b.onclick = () => {
      S.segSort = b.dataset.sort;
      document.querySelectorAll(".seg-sort button").forEach((x) => x.classList.toggle("on", x === b));
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
            if (x < left || x > left + width || x - lastX < 22 * dpr) continue;
            lastX = x;
            ctx.fillStyle = s.name === S.selSeg ? COL.text : COL.muted;
            ctx.fillText(s.name, x, top + 3 * dpr);
          }
          ctx.restore();
        }] : [],
      },
    };
  }

  function buildCharts(res) {
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
    if (sel) selectSeg(sel.name); else onSetScale(S.charts[0], "x");
  }

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
      `<span><b class="seg">${Math.round(d)} m</b>${seg ? ` · ${esc(seg.kind === "start" ? "起点直道" : seg.name)}` : ""}</span>`,
      `<span>Δt <b class="${dt > 0 ? "loss" : dt < 0 ? "gain" : ""}">${F.fmtDelta(dt, 3)}</b></span>`,
      `<span>车速 <b class="va">${v1(A.speed[idx])}</b>/<b class="vb">${v1(B.speed[idx])}</b></span>`,
      `<span>油门 <b class="va">${pct(A.throttle[idx])}</b>/<b class="vb">${pct(B.throttle[idx])}</b>%</span>`,
      `<span>刹车 <b class="va">${pct(A.brake[idx])}</b>/<b class="vb">${pct(B.brake[idx])}</b>%</span>`,
      `<span>挡 <b class="va">${A.gear[idx] ?? "-"}</b>/<b class="vb">${B.gear[idx] ?? "-"}</b></span>`,
      `<span>转速 <b class="va">${v1(A.rpm[idx])}</b>/<b class="vb">${v1(B.rpm[idx])}</b></span>`,
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
      return;
    }
    cv.hidden = false;
    const m = res.map;
    const dpr = DPR();
    const w = Math.max(200, wrap.clientWidth);
    const xs = m.x, zs = m.z;
    const minX = Math.min(...xs), maxX = Math.max(...xs), minZ = Math.min(...zs), maxZ = Math.max(...zs);
    const pad = 26;
    const spanX = Math.max(1, maxX - minX), spanZ = Math.max(1, maxZ - minZ);
    const hMax = window.innerWidth > 980 ? Math.min(w * 0.95, 360) : Math.min(w * 1.05, 420);
    const h = Math.round(Math.min(Math.max((w - 2 * pad) * spanZ / spanX + 2 * pad, 180), hMax));
    const k = Math.min((w - 2 * pad) / spanX, (h - 2 * pad) / spanZ);
    const ox = (w - spanX * k) / 2, oz = (h - spanZ * k) / 2;
    // 俯视图：x 向右，z 向上（canvas 的 y 向下，所以取反）。这样画出来的左右弯和实际一致
    MAP.proj = (x, z) => [ox + (x - minX) * k, h - (oz + (z - minZ) * k)];
    MAP.pts = xs.map((x, i) => MAP.proj(x, zs[i]));
    MAP.w = w; MAP.h = h;
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
    const path = (i0, i1) => {
      ctx.beginPath(); ctx.moveTo(P[i0][0], P[i0][1]);
      for (let i = i0 + 1; i <= i1; i++) ctx.lineTo(P[i][0], P[i][1]);
    };
    // 路面底色
    path(0, n - 1); ctx.strokeStyle = COL.panel2; ctx.lineWidth = 11; ctx.stroke();
    // 当前曲线放大的范围 / 选中的弯：白色描边
    const xr = S.charts[0]?.scales.x;
    const L = res.length_m;
    if (xr && (xr.min > 1 || xr.max < L - 1)) {
      const i0 = idxOfDist(m.dist, xr.min), i1 = idxOfDist(m.dist, xr.max);
      if (i1 > i0) { path(i0, i1); ctx.strokeStyle = "rgba(255,255,255,0.55)"; ctx.lineWidth = 12; ctx.stroke(); }
    }
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
      const tw = ctx.measureText(c.name).width + 8;
      const on = c.name === S.selSeg;
      ctx.fillStyle = on ? COL.text : "rgba(13,15,18,0.82)";
      roundRect(ctx, lx - tw / 2, ly - 9, tw, 18, 5); ctx.fill();
      ctx.fillStyle = on ? "#0d0f12" : COL.text;
      ctx.fillText(c.name, lx, ly + 0.5);
    }
    drawMapOverlay();
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
    const [px, py] = MAP.pts[Math.min(MAP.pts.length - 1, idxOfDist(S.cmp.map.dist, d))];
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.beginPath(); ctx.arc(px, py, 7, 0, Math.PI * 2);
    ctx.fillStyle = COL.text; ctx.fill();
    ctx.lineWidth = 3; ctx.strokeStyle = "#0d0f12"; ctx.stroke();
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
