// 调车诊断页：选圈 → 调 /api/laps/{id}/diagnose（多圈：/api/diagnose?ids=）→ 问题卡片 + 赛道小地图。
// 地址栏参数：?lap=ID 单圈；?ids=1,2,3 多圈合并。
(function () {
  "use strict";
  const F = window.Forza;
  F.nav("tuning");

  const SEV = { alert: "重点", warn: "注意", info: "提示" };
  const SEV_ORDER = { info: 0, warn: 1, alert: 2 };
  const CONF = { high: "高", medium: "中", low: "低" };
  const CHECK = { found: "发现问题", ok: "正常", skipped: "没法判断" };

  const $ = (id) => document.getElementById(id);
  function el(tag, cls, text) {
    const e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text !== undefined && text !== null) e.textContent = text;
    return e;
  }
  function cssVar(name) {
    return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  }

  const state = {
    laps: null,       // /api/laps 的结果；拿不到时为 null（改用手动输入）
    result: null,     // 当前诊断结果
    map: null,        // {x, z, d}：地图坐标
    markers: [],      // 画出来的标记
    hl: null,         // 高亮的标记 key
    baseMsg: "",      // 常驻提示（比如圈列表拿不到），诊断时不清掉
  };

  // ================================================================ 选圈

  function showMsg(text, isErr) {
    const m = $("tn-msg");
    const t = text || state.baseMsg;
    m.hidden = !t;
    m.textContent = t || "";
    m.classList.toggle("err", !!(text && isErr));
  }

  function useManual(reason) {
    $("tn-pick-wrap").hidden = true;
    $("tn-manual-wrap").hidden = false;
    $("tn-multi-wrap").hidden = true;
    state.baseMsg = reason;
    showMsg("", false);
  }

  function lapText(l) {
    const parts = [`#${l.id}`];
    if (l.track_name) parts.push(l.track_name);
    if (l.lap_number !== null && l.lap_number !== undefined) parts.push(l.kind === "sprint" ? "冲刺" : `第 ${l.lap_number} 圈`);
    parts.push(F.fmtLap(l.lap_time));
    if (l.car_name) parts.push(l.car_name);
    else if (l.car_ordinal) parts.push(`车 ${l.car_ordinal}`);
    if (l.is_complete === false) parts.push("（不完整）");
    return parts.join("  ");
  }

  async function loadLaps() {
    try {
      const data = await F.api("/api/laps?limit=300");
      const arr = Array.isArray(data) ? data : data && (data.laps || data.items || data.data);
      if (!Array.isArray(arr)) throw new Error("返回格式不认识");
      state.laps = arr.filter((l) => l && l.id !== null && l.id !== undefined).sort((a, b) => b.id - a.id);
    } catch (e) {
      state.laps = null;
      useManual(`圈列表暂时拿不到（${e.message}），可以直接输入圈号。`);
      return;
    }
    const sel = $("tn-lap");
    sel.textContent = "";
    if (!state.laps.length) {
      sel.appendChild(el("option", "", "数据库里还没有圈"));
      showMsg("还没有圈数据：开游戏跑几圈，或者用 python -m forza fake 生成假数据再 import。", false);
      return;
    }
    for (const l of state.laps) {
      const o = el("option", "", lapText(l));
      o.value = String(l.id);
      sel.appendChild(o);
    }
  }

  // 选中的圈 + 同车同赛道、id 不大于它的最近 n-1 圈
  function recentIds(id, n) {
    const base = state.laps && state.laps.find((l) => l.id === id);
    if (!base) return [id];
    const same = state.laps
      .filter((l) => l.id <= id && l.track_id === base.track_id && l.car_ordinal === base.car_ordinal && l.game === base.game)
      .sort((a, b) => b.id - a.id)
      .slice(0, n)
      .map((l) => l.id);
    return same.sort((a, b) => a - b);
  }

  function parseIds(text) {
    return String(text || "").split(/[,，\s]+/).map((s) => parseInt(s, 10)).filter((x) => Number.isFinite(x) && x > 0);
  }

  function currentSelection() {
    if (!state.laps) return parseIds($("tn-manual").value);
    const id = parseInt($("tn-lap").value, 10);
    if (!Number.isFinite(id)) return [];
    if ($("tn-multi").checked) return recentIds(id, parseInt($("tn-multi-n").value, 10) || 3);
    return [id];
  }

  // ================================================================ 诊断

  async function run(ids) {
    if (!ids.length) {
      showMsg("先选一圈（或输入圈号）。", true);
      return;
    }
    const go = $("tn-go");
    go.disabled = true;
    go.textContent = "诊断中…";
    showMsg("", false);
    const url = ids.length === 1 ? `/api/laps/${ids[0]}/diagnose` : `/api/diagnose?ids=${ids.join(",")}`;
    try {
      const res = await F.api(url);
      state.result = res;
      history.replaceState(null, "", ids.length === 1 ? `?lap=${ids[0]}` : `?ids=${ids.join(",")}`);
      render(res);
      loadMap(res);
      if (ids.length > 1 && res.laps && res.laps.length < ids.length) showMsg("有的圈没找到，已跳过。", false);
    } catch (e) {
      clearResult();
      showMsg(`诊断失败：${e.message}`, true);
    } finally {
      go.disabled = false;
      go.textContent = "诊断";
    }
  }

  // ================================================================ 渲染

  // 诊断失败时把上一次的结果收起来，免得误以为是这一圈的
  function clearResult() {
    state.result = null;
    state.map = null;
    state.markers = [];
    $("tn-findings").textContent = "";
    for (const id of ["tn-head", "tn-checks", "tn-map-card", "tn-dq", "tn-na"]) $(id).hidden = true;
  }

  function render(res) {
    renderHead(res);
    renderFindings(res);
    renderChecks(res);
    renderList("tn-dq", "tn-dq-list", res.data_quality, (q) => {
      const li = el("li", q.level, q.text);
      return li;
    });
    renderList("tn-na", "tn-na-list", res.not_available, (x) => {
      const li = el("li");
      li.appendChild(el("b", "", x.item));
      li.appendChild(el("span", "", x.why));
      if (x.instead) li.appendChild(el("em", "", "可以这样看：" + x.instead));
      return li;
    });
  }

  function stat(k, v, small) {
    const d = el("div", "tn-stat");
    d.appendChild(el("div", "k", k));
    const vv = el("div", "v", v);
    if (small) vv.appendChild(el("small", "", " " + small));
    d.appendChild(vv);
    return d;
  }

  function renderHead(res) {
    const box = $("tn-head");
    box.hidden = false;
    box.textContent = "";
    const s = res.summary || {};
    const lap = res.lap || {};
    const t = el("div", "tn-head-title");
    t.appendChild(el("strong", "", res.mode === "multi" ? `${res.laps.length} 圈合并诊断` : res.lap_label || "这圈"));
    const sub = [lap.track_name, lap.car_name || (lap.car_ordinal ? `车 ${lap.car_ordinal}` : null)].filter(Boolean).join(" · ");
    if (sub) t.appendChild(el("span", "muted", sub));
    if (res.mode === "multi" && s.reference_lap) t.appendChild(el("span", "muted", `（概况数字取自 ${s.reference_lap}）`));
    if (s.samples) t.appendChild(el("span", "muted tn-small", `${s.samples} 个采样点${s.sample_hz ? ` · ${Math.round(s.sample_hz)} Hz` : ""}`));
    box.appendChild(t);

    const g = el("div", "tn-stats");
    g.appendChild(stat("圈时", F.fmtLap(s.lap_time_s)));
    g.appendChild(stat("驱动", s.drivetrain_label || "未知"));
    g.appendChild(stat("最高车速", s.max_speed_kmh ? `${Math.round(s.max_speed_kmh)}` : "--", s.max_speed_kmh ? "km/h" : ""));
    g.appendChild(stat("用到的最高挡", s.top_gear_used ? `${s.top_gear_used} 挡` : "--"));
    if (s.turn_share) {
      g.appendChild(stat("弯道", `${s.corner_count} 个`, `右 ${Math.round(s.turn_share.right * 100)}% / 左 ${Math.round(s.turn_share.left * 100)}%`));
    } else {
      g.appendChild(stat("弯道", s.corner_count ? `${s.corner_count} 个` : "--"));
    }
    if (s.tire_temps_c) {
      const d = el("div", "tn-stat");
      d.appendChild(el("div", "k", "热胎平均胎温 °C"));
      const tt = el("div", "tn-tires");
      const temps = s.tire_temps_c;
      const mx = Math.max(temps.fl, temps.fr, temps.rl, temps.rr);
      for (const [w, name] of [["fl", "左前"], ["fr", "右前"], ["rl", "左后"], ["rr", "右后"]]) {
        tt.appendChild(el("span", temps[w] === mx ? "hot" : "", `${name} ${temps[w].toFixed(1)}`));
      }
      d.appendChild(tt);
      g.appendChild(d);
    }
    box.appendChild(g);

    const c = el("div", "tn-counts");
    const counts = s.counts || {};
    for (const k of ["alert", "warn", "info"]) c.appendChild(el("span", `badge sev-${k}`, `${SEV[k]} ${counts[k] || 0}`));
    box.appendChild(c);

    if (res.mode === "multi" && res.laps) {
      const lw = el("div", "tn-laps");
      for (const l of res.laps) {
        const a = el("a", l.usable ? "" : "bad", `${l.label} ${F.fmtLap(l.lap_time_s)}${l.usable ? "" : "（数据不足）"}`);
        a.href = l.id ? `?lap=${l.id}` : "#";
        a.title = "只看这一圈";
        lw.appendChild(a);
      }
      box.appendChild(lw);
    }
  }

  function renderFindings(res) {
    const box = $("tn-findings");
    box.textContent = "";
    const fs = res.findings || [];
    if (!res.usable) {
      const c = el("div", "card tn-empty");
      c.appendChild(el("strong", "", "数据不足，没有下结论"));
      const errs = (res.data_quality || []).filter((q) => q.level !== "info").map((q) => q.text);
      c.appendChild(el("div", "muted", errs.join(" ") || "这圈数据太少，没法判断。"));
      box.appendChild(c);
      return;
    }
    if (!fs.length) {
      const c = el("div", "card tn-empty");
      c.appendChild(el("strong", "", "没发现明显问题"));
      c.appendChild(el("div", "muted", "“检查了什么”里列了每一项检查的实测范围。“没发现”不等于没问题——Data Out 看不到的东西见“Data Out 判断不了的”。"));
      box.appendChild(c);
      return;
    }
    fs.forEach((f, i) => box.appendChild(findingCard(f, i + 1)));
  }

  function findingCard(f, num) {
    const card = el("article", `card fd sev-${f.severity}`);
    card.id = `fd-${num}`;
    const head = el("div", "fd-head");
    head.appendChild(el("span", `fd-num sev-${f.severity}`, String(num)));
    head.appendChild(el("span", `badge sev-${f.severity}`, SEV[f.severity]));
    head.appendChild(el("span", "badge plain", f.category_label || f.category));
    head.appendChild(el("span", `badge conf-${f.confidence}`, `置信度 ${CONF[f.confidence] || f.confidence}`));
    if (f.repeat) head.appendChild(el("span", "badge plain", `${f.repeat.laps}/${f.repeat.of} 圈出现`));
    card.appendChild(head);
    card.appendChild(el("h3", "fd-title", f.title));
    card.appendChild(el("p", "fd-summary", f.summary));

    // 位置（可点：在地图上高亮）
    const locs = el("div", "fd-locs");
    (f.locations || []).forEach((l, j) => {
      const onMap = l.phase !== null && l.phase !== undefined;
      const text = (l.lap_label ? `${l.lap_label}：` : "") + (l.label || `${Math.round(l.dist_m)} m`);
      const b = el("button", "fd-loc" + (onMap ? "" : " static"), text);
      b.type = "button";
      if (onMap) {
        b.title = "在赛道图上标出";
        b.addEventListener("click", () => highlight(`${num}:${j}`, b));
      } else {
        b.title = "整圈统计，不在图上标";
      }
      b.dataset.key = `${num}:${j}`;
      locs.appendChild(b);
    });
    card.appendChild(locs);

    card.appendChild(el("h4", "", "证据"));
    const ev = el("ul", "fd-ev");
    for (const e of f.evidence || []) {
      const cls = e.startsWith("　") ? "sub" : e.startsWith("判定") ? "rule" : "";
      ev.appendChild(el("li", cls, e.replace(/^　/, "")));
    }
    card.appendChild(ev);

    const det = el("details", "fd-basis");
    det.appendChild(el("summary", "", "判断依据"));
    const dl = el("dl");
    const b = f.basis || {};
    for (const [k, v] of [["用到的数据通道", (b.channels || []).join("；")], ["规则", b.rule], ["为什么能说明问题", b.principle]]) {
      if (!v) continue;
      dl.appendChild(el("dt", "", k));
      dl.appendChild(el("dd", "", v));
    }
    det.appendChild(dl);
    card.appendChild(det);

    card.appendChild(el("h4", "", "可以试试（常见调整方向）"));
    const ol = el("ol", "fd-tips");
    for (const s of f.suggestions || []) ol.appendChild(el("li", "", s));
    card.appendChild(ol);
    if (f.caveat) card.appendChild(el("p", "muted fd-caveat", f.caveat));
    return card;
  }

  function renderChecks(res) {
    const sec = $("tn-checks");
    const ul = $("tn-check-list");
    ul.textContent = "";
    const cs = res.checks || [];
    sec.hidden = !cs.length;
    for (const c of cs) {
      const li = el("li");
      li.appendChild(el("span", `st st-${c.status}`, CHECK[c.status] || c.status));
      li.appendChild(el("span", "nm", c.name));
      const nt = el("span", "nt");
      String(c.note || "").split("\n").forEach((line, i) => {
        if (i) nt.appendChild(document.createElement("br"));
        nt.appendChild(document.createTextNode(line));
      });
      li.appendChild(nt);
      ul.appendChild(li);
    }
  }

  function renderList(secId, listId, items, make) {
    const sec = $(secId);
    const ul = $(listId);
    ul.textContent = "";
    sec.hidden = !(items && items.length);
    for (const it of items || []) ul.appendChild(make(it));
  }

  // ================================================================ 赛道小地图

  async function fetchOutline(lapId, samples) {
    // 优先用圈速模块的通用接口；不可用时退回诊断模块自己的降采样接口
    const step = Math.max(1, Math.ceil((samples || 3000) / 600));
    const tries = [
      `/api/laps/${lapId}?step=${step}&channels=pos_x,pos_z,dist`,
      `/api/tuning/outline/${lapId}`,
    ];
    for (const url of tries) {
      try {
        const data = await F.api(url);
        const ch = (data && (data.channels || data)) || {};
        if (Array.isArray(ch.pos_x) && Array.isArray(ch.pos_z) && Array.isArray(ch.dist) && ch.pos_x.length > 10) return ch;
      } catch (_) { /* 换下一个 */ }
    }
    return null;
  }

  async function loadMap(res) {
    const card = $("tn-map-card");
    const lapId = res.lap && res.lap.id;
    state.map = null;
    state.hl = null;
    if (!lapId || !res.usable) {
      card.hidden = true;
      return;
    }
    const ch = await fetchOutline(lapId, res.summary && res.summary.samples);
    if (state.result !== res) return;           // 期间又换了一圈
    const x = [], z = [], d = [];
    if (ch) {
      for (let i = 0; i < ch.pos_x.length; i++) {
        if (ch.pos_x[i] === null || ch.pos_z[i] === null || ch.dist[i] === null) continue;
        x.push(ch.pos_x[i]); z.push(ch.pos_z[i]); d.push(ch.dist[i]);
      }
    }
    const span = x.length ? Math.max(Math.max(...x) - Math.min(...x), Math.max(...z) - Math.min(...z)) : 0;
    card.hidden = false;
    if (span < 5) {
      $("tn-map").hidden = true;
      $("tn-map-note").textContent = "赛道图不可用：这圈没有坐标数据（或圈数据接口暂时不可用）。";
      return;
    }
    $("tn-map").hidden = false;
    $("tn-map-note").textContent = "数字 = 问题卡片的编号，色段 = 发生的路段；点数字跳到对应卡片。整圈性的问题（如胎温）不在图上标。";
    state.map = { x, z, d };
    drawMap();
  }

  // 按圈内距离插值出坐标
  function pointAt(dist) {
    const { x, z, d } = state.map;
    let lo = 0, hi = d.length - 1;
    if (dist <= d[0]) return [x[0], z[0]];
    if (dist >= d[hi]) return [x[hi], z[hi]];
    while (hi - lo > 1) {
      const mid = (lo + hi) >> 1;
      if (d[mid] <= dist) lo = mid; else hi = mid;
    }
    const f = d[hi] > d[lo] ? (dist - d[lo]) / (d[hi] - d[lo]) : 0;
    return [x[lo] + (x[hi] - x[lo]) * f, z[lo] + (z[hi] - z[lo]) * f];
  }

  function drawMap() {
    const res = state.result;
    if (!state.map || !res) return;
    const cv = $("tn-map");
    const box = $("tn-map-box");
    const w = box.clientWidth;
    if (w < 50) return;
    const h = Math.round(Math.max(220, Math.min(w * 0.78, 400)));
    const dpr = window.devicePixelRatio || 1;
    cv.width = Math.round(w * dpr);
    cv.height = Math.round(h * dpr);
    cv.style.height = `${h}px`;
    const ctx = cv.getContext("2d");
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, w, h);

    // 世界坐标 → 画布：x 向右，z（北）向上
    const { x, z } = state.map;
    const minX = Math.min(...x), maxX = Math.max(...x), minZ = Math.min(...z), maxZ = Math.max(...z);
    const pad = 26;
    const sc = Math.min((w - 2 * pad) / Math.max(maxX - minX, 1), (h - 2 * pad) / Math.max(maxZ - minZ, 1));
    const ox = (w - (maxX - minX) * sc) / 2, oy = (h - (maxZ - minZ) * sc) / 2;
    const P = (px, pz) => [ox + (px - minX) * sc, h - oy - (pz - minZ) * sc];
    const PD = (dist) => P(...pointAt(dist));

    const col = { line: cssVar("--line"), muted: cssVar("--muted"), text: cssVar("--text"), bg: cssVar("--bg"),
      alert: cssVar("--bad"), warn: cssVar("--warn"), info: cssVar("--accent") };

    // 赛道
    function path(i0, i1) {
      ctx.beginPath();
      for (let i = i0; i <= i1; i++) {
        const [px, py] = P(x[i], z[i]);
        if (i === i0) ctx.moveTo(px, py); else ctx.lineTo(px, py);
      }
    }
    ctx.lineJoin = "round";
    ctx.lineCap = "round";
    path(0, x.length - 1);
    ctx.strokeStyle = col.line;
    ctx.lineWidth = 7;
    ctx.stroke();
    ctx.strokeStyle = col.muted;
    ctx.lineWidth = 1.5;
    ctx.stroke();

    // 起点
    const [sx, sy] = P(x[0], z[0]);
    ctx.fillStyle = col.text;
    ctx.beginPath();
    ctx.arc(sx, sy, 4, 0, Math.PI * 2);
    ctx.fill();
    ctx.font = "11px system-ui, sans-serif";
    ctx.fillStyle = col.muted;
    ctx.textAlign = "left";
    ctx.textBaseline = "middle";
    ctx.fillText("起点", sx + 7, sy - 9);

    // 外法线方向（指向弯外），用来放弯道标签、错开重叠的标记
    function outward(dist) {
      const a = PD(dist - 25), b = PD(dist), c = PD(dist + 25);
      let nx = b[0] - (a[0] + c[0]) / 2, ny = b[1] - (a[1] + c[1]) / 2;
      let len = Math.hypot(nx, ny);
      if (len < 0.5) {               // 直道：取垂直于行驶方向
        nx = -(c[1] - a[1]); ny = c[0] - a[0];
        len = Math.hypot(nx, ny) || 1;
      }
      return [nx / len, ny / len];
    }

    // 弯道编号：放在弯外侧；和已放的标签挤在一起时再往外挪，实在不行放到弯内侧
    ctx.textAlign = "center";
    const labels = [];
    for (const c of res.corners || []) {
      const [px, py] = PD(c.apex_m);
      const [nx, ny] = outward(c.apex_m);
      let pos = null;
      for (const k of [15, 28, 41, -15, -28]) {
        const cand = [px + nx * k, py + ny * k];
        if (!labels.some((q) => Math.abs(q[0] - cand[0]) < 22 && Math.abs(q[1] - cand[1]) < 13)) { pos = cand; break; }
      }
      pos = pos || [px + nx * 15, py + ny * 15];
      labels.push(pos);
      ctx.fillStyle = col.muted;
      ctx.fillText(c.name, pos[0], pos[1]);
    }

    // 问题路段 + 编号标记
    const markers = [];
    const fs = res.findings || [];
    fs.forEach((f, fi) => {
      const num = fi + 1;
      const placed = [];
      (f.locations || []).forEach((l, j) => {
        if (l.phase === null || l.phase === undefined || l.dist_m === null) return;
        const d0 = l.dist_m, d1 = Math.max(l.end_m || d0, d0);
        const mid = (d0 + d1) / 2;
        if (placed.some((m) => Math.abs(m.mid - mid) < 40)) {   // 多圈同一位置只画一个
          placed.find((m) => Math.abs(m.mid - mid) < 40).keys.push(`${num}:${j}`);
          return;
        }
        const m = { num, sev: f.severity, title: f.title, label: l.label, d0, d1, mid, keys: [`${num}:${j}`] };
        placed.push(m);
        markers.push(m);
      });
    });
    // 路段色带（先画低严重度，严重的盖在上面）
    markers.sort((a, b) => SEV_ORDER[a.sev] - SEV_ORDER[b.sev]);
    for (const m of markers) {
      if (m.d1 - m.d0 < 3) continue;
      ctx.beginPath();
      const n = Math.max(2, Math.ceil((m.d1 - m.d0) / 4));
      for (let k = 0; k <= n; k++) {
        const [px, py] = PD(m.d0 + ((m.d1 - m.d0) * k) / n);
        if (k === 0) ctx.moveTo(px, py); else ctx.lineTo(px, py);
      }
      ctx.strokeStyle = col[m.sev];
      ctx.lineWidth = 5;
      ctx.stroke();
    }
    // 标记位置：放在路段中点，和已有标记太近就沿外法线往外挪
    const R = 9;
    for (const m of markers) {
      const [px, py] = PD(m.mid);
      const [nx, ny] = outward(m.mid);
      let cx = px, cy = py, k = 0;
      while (markers.some((o) => o !== m && o.cx !== undefined && Math.hypot(o.cx - cx, o.cy - cy) < 2 * R + 3) && k < 8) {
        k++;
        cx = px + nx * k * (2 * R + 3);
        cy = py + ny * k * (2 * R + 3);
      }
      m.cx = Math.min(Math.max(cx, R + 2), w - R - 2);
      m.cy = Math.min(Math.max(cy, R + 2), h - R - 2);
      m.ax = px; m.ay = py;
    }
    for (const m of markers) {                             // 挪开了的标记：先画所有引线，再画圆，免得引线压住别的标记
      if (Math.hypot(m.cx - m.ax, m.cy - m.ay) > 1) {
        ctx.beginPath();
        ctx.moveTo(m.ax, m.ay);
        ctx.lineTo(m.cx, m.cy);
        ctx.strokeStyle = col[m.sev];
        ctx.lineWidth = 1.5;
        ctx.stroke();
      }
    }
    for (const m of markers) {
      const on = m.keys.includes(state.hl);
      if (on) {
        ctx.beginPath();
        ctx.arc(m.cx, m.cy, R + 5, 0, Math.PI * 2);
        ctx.strokeStyle = col.text;
        ctx.lineWidth = 2;
        ctx.stroke();
      }
      ctx.beginPath();
      ctx.arc(m.cx, m.cy, R, 0, Math.PI * 2);
      ctx.fillStyle = col[m.sev];
      ctx.fill();
      ctx.strokeStyle = col.bg;                            // 2px 底色描边，重叠时分得清
      ctx.lineWidth = 2;
      ctx.stroke();
      ctx.fillStyle = "#0d0f12";
      ctx.font = "bold 11px system-ui, sans-serif";
      ctx.textAlign = "center";
      ctx.textBaseline = "middle";
      ctx.fillText(String(m.num), m.cx, m.cy + 0.5);
    }
    state.markers = markers;
  }

  function markerAt(ev) {
    const r = $("tn-map").getBoundingClientRect();
    const px = ev.clientX - r.left, py = ev.clientY - r.top;
    let best = null, bd = 16;                              // 命中范围比标记大
    for (const m of state.markers) {
      const dd = Math.hypot(m.cx - px, m.cy - py);
      if (dd < bd) { bd = dd; best = m; }
    }
    return { m: best, px, py };
  }

  function showTip(ev) {
    const tip = $("tn-tip");
    const { m, px, py } = markerAt(ev);
    $("tn-map").style.cursor = m ? "pointer" : "default";
    if (!m) { tip.hidden = true; return; }
    tip.textContent = "";
    tip.appendChild(el("b", "", `${m.num}. ${m.title}`));
    tip.appendChild(el("span", "muted", m.label || ""));
    tip.hidden = false;
    const bw = $("tn-map-box").clientWidth;
    const tw = tip.offsetWidth;
    tip.style.left = `${Math.min(Math.max(px - tw / 2, 0), bw - tw)}px`;
    tip.style.top = `${py + 16}px`;
  }

  function highlight(key, btn) {
    state.hl = state.hl === key ? null : key;
    document.querySelectorAll(".fd-loc.on").forEach((b) => b.classList.remove("on"));
    if (state.hl && btn) btn.classList.add("on");
    drawMap();
    const card = $("tn-map-card");
    if (state.hl && !card.hidden) {
      const r = card.getBoundingClientRect();
      if (r.bottom < 0 || r.top > window.innerHeight) card.scrollIntoView({ behavior: "smooth", block: "nearest" });
    }
  }

  function jumpToCard(m) {
    const card = $(`fd-${m.num}`);
    if (!card) return;
    card.scrollIntoView({ behavior: "smooth", block: "start" });
    card.classList.add("flash");
    setTimeout(() => card.classList.remove("flash"), 1200);
    state.hl = m.keys[0];
    document.querySelectorAll(".fd-loc.on").forEach((b) => b.classList.remove("on"));
    const b = card.querySelector(`.fd-loc[data-key="${m.keys[0]}"]`);
    if (b) b.classList.add("on");
    drawMap();
  }

  // ================================================================ 启动

  async function init() {
    const cv = $("tn-map");
    cv.addEventListener("pointermove", showTip);
    cv.addEventListener("pointerleave", () => { $("tn-tip").hidden = true; });
    cv.addEventListener("click", (ev) => { const { m } = markerAt(ev); if (m) jumpToCard(m); });
    let raf = 0;
    new ResizeObserver(() => { cancelAnimationFrame(raf); raf = requestAnimationFrame(drawMap); }).observe($("tn-map-box"));

    $("tn-form").addEventListener("submit", (ev) => {
      ev.preventDefault();
      run(currentSelection());
    });
    $("tn-lap").addEventListener("change", () => run(currentSelection()));
    $("tn-multi").addEventListener("change", () => run(currentSelection()));
    $("tn-multi-n").addEventListener("change", () => { if ($("tn-multi").checked) run(currentSelection()); });

    await loadLaps();
    const ids = parseIds(F.qs("ids"));
    const one = parseInt(F.qs("lap"), 10);
    let start = [];
    if (ids.length > 1) start = ids;
    else if (Number.isFinite(one)) start = [one];
    else if (ids.length === 1) start = ids;
    else if (state.laps && state.laps.length) start = [state.laps[0].id];   // 默认：最近一圈
    if (state.laps) {
      const sel = $("tn-lap");
      const want = String(start.length ? start[start.length - 1] : "");
      if (want && ![...sel.options].some((o) => o.value === want)) {
        const o = el("option", "", `#${want}`);
        o.value = want;
        sel.prepend(o);
      }
      if (want) sel.value = want;
      if (start.length > 1) {
        $("tn-multi").checked = true;
        const n = $("tn-multi-n");
        if (![...n.options].some((o) => o.value === String(start.length))) n.appendChild(el("option", "", String(start.length)));
        n.value = String(start.length);
      }
    } else if (start.length) {
      $("tn-manual").value = start.join(",");
    }
    if (start.length) run(start);
  }

  init();
})();
