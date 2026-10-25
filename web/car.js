// 车辆调校页：调校版本（列表 / 新建 / 改名 / 设为当前 / 版本对比）+ 实测功率曲线 + 各挡传动比与换挡点 + 改终传预测。
// 数据：/api/cars、/api/setups*（forza/setups.py）、/api/powertrain*（forza/powertrain.py）。
// 约定：版本 A = 参考（--accent 蓝），B = 要看的版本（--accent-2 橙）；Δ = B − A，负数 = B 快。
// 网址参数 ?game=&car= 选车；新圈落盘（Forza.onLapsChanged）时自动刷新版本统计和动力总成数据。
(function () {
  "use strict";
  const F = window.Forza;
  F.nav("car");

  // ------------------------------------------------------------------ 小工具
  const $ = (sel, root = document) => root.querySelector(sel);
  const { esc, cssVar, getJSON } = F;   // 公用的 HTML 转义 / 读 CSS 变量 / 不抛异常的请求（见 common.js）
  const COL = {
    a: cssVar("--accent", "#4ea1ff"),
    b: cssVar("--accent-2", "#ff9f43"),
    text: cssVar("--text", "#e8eaed"),
    muted: cssVar("--muted", "#8b929c"),
    line: cssVar("--line", "#2c313a"),
    panel: cssVar("--panel", "#171a1f"),
  };
  const GRID = cssVar("--grid", "rgba(255,255,255,0.06)");
  const num = (v) => typeof v === "number" && isFinite(v);
  const UNTAGGED_ID = 0;          // 版本对比里代表“没记版本的圈”（和 forza/setups.py 的 UNTAGGED_ID 一致）
  const fmtS = (v, d = 3) => (num(v) ? v.toFixed(d) : "--");
  const fmtInt = (v) => (num(v) ? String(Math.round(v)) : "--");
  const fmtDate = (s) => (s ? s.slice(5, 16).replace("T", " ") : "");
  const carKey = (c) => (c ? `${c.game}:${c.car_ordinal}` : "");
  const show = (el, on) => { el.hidden = !on; };
  function store(key, val) {
    try {
      if (val === undefined) return JSON.parse(localStorage.getItem(key) || "null");
      localStorage.setItem(key, JSON.stringify(val));
    } catch (_) { /* 隐私模式等：不记也行 */ }
    return null;
  }
  const detail = (r) => (r.data && typeof r.data.detail === "string" ? r.data.detail
    : r.status ? `出错了（${r.status}）` : "服务未连接");

  // ------------------------------------------------------------------ 状态
  const S = {
    cars: [], car: null,
    setups: [], setupsOk: false, current: null, hints: [],
    editing: null,            // 正在改名的版本 id
    pt: null, pi: null, charts: [],
    cmp: null, cmpSeq: 0, ptSeq: 0,
  };
  const q = () => `game=${encodeURIComponent(S.car.game)}&car_ordinal=${S.car.car_ordinal}`;

  // ------------------------------------------------------------------ 选车
  function carLabel(c) {
    const name = c.name || `车 #${c.car_ordinal}`;
    const fam = String(c.game || "").startsWith("fh") ? "fh" : "fm";
    const cls = c.car_class != null ? `${F.carClass(fam, c.car_class)} ` : "";
    return `${name}（${c.game} · ${cls}${c.car_pi || ""}）`;
  }

  async function init() {
    const r = await getJSON("/api/cars");
    if (!r.ok) return fatal(`读不到车辆列表：${detail(r)}`);
    S.cars = r.data || [];
    if (!S.cars.length) {
      return fatal("还没有存过圈。开游戏跑几圈（或者用 python -m forza import 导入录像），这里就会出现车辆和调校数据。", false);
    }
    let pick = null;
    const g = F.qs("game"), o = F.qs("car");
    if (g && o) pick = S.cars.find((c) => c.game === g && String(c.car_ordinal) === o);
    if (!pick) {      // 实时那台车 → 最近一圈的车 → 列表第一台
      const cur = await getJSON("/api/setups/current");
      const lc = cur.ok && cur.data && cur.data.car;
      if (lc) pick = S.cars.find((c) => c.game === lc.game && c.car_ordinal === lc.car_ordinal);
    }
    if (!pick) {
      const l = await getJSON("/api/laps?limit=1");
      const m = l.ok && l.data && l.data[0];
      if (m) pick = S.cars.find((c) => c.game === m.game && c.car_ordinal === m.car_ordinal);
    }
    pick = pick || S.cars[0];
    const sel = $("#car-sel");
    sel.innerHTML = S.cars.map((c) => `<option value="${esc(carKey(c))}">${esc(carLabel(c))}</option>`).join("");
    sel.value = carKey(pick);
    sel.onchange = () => selectCar(S.cars.find((c) => carKey(c) === sel.value));
    selectCar(pick);
  }

  function fatal(msg, isErr = true) {
    const el = $("#car-msg");
    el.textContent = msg;
    el.classList.toggle("err", isErr);
    show(el, true);
    show($("#car-body"), false);
    $("#car-sel").innerHTML = `<option value="">（没有）</option>`;
  }

  function selectCar(c) {
    if (!c) return;
    S.car = c;
    S.pi = null;
    S.cmp = null;
    S.editing = null;
    show($("#car-body"), true);
    show($("#car-msg"), false);
    const dt = F.DRIVETRAIN[c.drivetrain] || "";
    $("#car-info").textContent = [dt, c.num_cylinders ? `${c.num_cylinders} 缸` : "", c.first_seen ? `第一次出现 ${fmtDate(c.first_seen)}` : ""]
      .filter(Boolean).join(" · ");
    const p = new URLSearchParams(location.search);
    p.set("game", c.game);
    p.set("car", c.car_ordinal);
    history.replaceState(null, "", `${location.pathname}?${p}`);
    $("#cmp-out").hidden = true;
    loadSetups();
    loadPower();
    loadFinalInputs();
  }

  // ------------------------------------------------------------------ 调校版本
  async function loadSetups() {
    const key = carKey(S.car);
    const [r, cur] = await Promise.all([getJSON(`/api/setups?${q()}`), getJSON(`/api/setups/current?${q()}`)]);
    if (key !== carKey(S.car)) return;
    S.setupsOk = r.ok && Array.isArray(r.data);
    show($("#sec-setups"), S.setupsOk);
    if (!S.setupsOk) { show($("#sec-compare"), false); return; }
    S.setups = r.data;
    S.current = cur.ok && cur.data ? cur.data.setup : null;
    S.hints = cur.ok && cur.data ? cur.data.hints || [] : [];
    renderSetups();
    renderCompareForm();
  }

  function renderSetups() {
    const cur = S.setups.find((s) => s.is_current);
    $("#setup-cur").textContent = cur ? `当前：${cur.name}` : "还没有记录版本";
    const host = $("#setup-list");
    // 当前版本那一行已经带了同样的提示，就不在顶上重复
    const hints = S.hints.filter((h) => !(cur && cur.hint === h)).map((h) => `<p class="hint">${esc(h)}</p>`).join("");
    if (!S.setups.length) {
      host.innerHTML = hints + `<div class="empty">这台车还没有记录过调校版本。点右上角“记录调校改动”，填个名字（例如“基准”），之后存的圈就都归到这个版本。
        实时面板菜单里也能记。</div>`;
      return;
    }
    host.innerHTML = hints + S.setups.map(setupRow).join("");
    host.querySelectorAll("[data-act]").forEach((b) => { b.onclick = () => setupAction(b.dataset.act, Number(b.dataset.id)); });
    const inp = host.querySelector(".setup-edit input[name=name]");
    if (inp) {
      inp.focus();
      host.querySelectorAll(".setup-edit input").forEach((el) => {
        el.onkeydown = (e) => {
          if (e.key === "Enter") { e.preventDefault(); setupAction("save", S.editing); }
          if (e.key === "Escape") setupAction("cancel", S.editing);
        };
      });
    }
  }

  function setupRow(s) {
    const editing = S.editing === s.id;
    const track = !s.track_name ? "" : /^赛道/.test(s.track_name) ? esc(s.track_name) : `赛道 ${esc(s.track_name)}`;
    const more = (s.tracks || []).length > 1 ? `（共 ${s.tracks.length} 条赛道，统计取圈数最多的）` : "";
    const stats = [
      `圈数 <b>${s.lap_count}</b>`,
      `完整飞驰圈 <b>${s.flying_laps}</b>${s.outlier_laps ? `<span class="muted">（另有 ${s.outlier_laps} 圈失误圈不计）</span>` : ""}`,
      `最快 <b>${F.fmtLap(s.best_time)}</b>`,
      `中位数 <b>${F.fmtLap(s.median_time)}</b>`,
      `波动 <b>${num(s.std_time) ? `±${s.std_time.toFixed(3)} s` : "--"}</b>`,
      track + more,
      `记于 ${esc(fmtDate(s.created_at))}`,
    ].filter(Boolean).map((x) => `<span>${x}</span>`).join("");
    const body = editing
      ? `<div class="setup-edit">
           <input name="name" maxlength="60" value="${esc(s.name)}" aria-label="版本名">
           <input name="note" maxlength="300" value="${esc(s.note || "")}" placeholder="改了什么（可不填）" aria-label="说明">
           <button type="button" class="primary small-btn" data-act="save" data-id="${s.id}">保存</button>
           <button type="button" class="small-btn" data-act="cancel" data-id="${s.id}">取消</button>
           <span class="car-msg err" id="edit-msg" hidden></span>
         </div>`
      : `<div class="setup-stats">${stats}</div>`;
    return `<div class="setup-row ${s.is_current ? "current" : ""}">
      <div class="setup-name"><strong>${esc(s.name)}</strong>${s.is_current ? `<span class="badge-cur">当前</span>` : ""}
        ${s.note ? `<span class="note">${esc(s.note)}</span>` : ""}</div>
      <div class="setup-actions">
        ${s.is_current ? "" : `<button type="button" class="link" data-act="activate" data-id="${s.id}" title="换回这套调校：之后存的圈归到这个版本">设为当前</button>`}
        ${editing ? "" : `<button type="button" class="link" data-act="edit" data-id="${s.id}">改名</button>`}
        ${s.track_id != null ? `<a class="small" href="/laps.html?track=${s.track_id}&car=${encodeURIComponent(carKey(S.car))}&setup=${s.id}" title="在圈速对比页只看这个版本的圈">看圈</a>` : ""}
      </div>
      ${body}
      ${s.hint ? `<p class="hint">${esc(s.hint)}</p>` : ""}
    </div>`;
  }

  async function setupAction(act, id) {
    if (act === "edit") { S.editing = id; renderSetups(); return; }
    if (act === "cancel") { S.editing = null; renderSetups(); return; }
    if (act === "activate") {
      const r = await getJSON(`/api/setups/${id}/activate`, { method: "POST" });
      if (!r.ok) { alert(detail(r)); return; }
      await loadSetups();
      return;
    }
    if (act === "save") {
      const box = $("#setup-list .setup-edit");
      const name = box.querySelector("input[name=name]").value.trim();
      const note = box.querySelector("input[name=note]").value.trim();
      const msg = $("#edit-msg");
      if (!name) { msg.textContent = "版本名不能为空"; msg.hidden = false; return; }
      const r = await getJSON(`/api/setups/${id}`, {
        method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ name, note }),
      });
      if (!r.ok) { msg.textContent = detail(r); msg.hidden = false; return; }
      S.editing = null;
      await loadSetups();
    }
  }

  // 新建版本
  const form = $("#setup-form");
  $("#btn-new-setup").onclick = () => {
    form.hidden = !form.hidden;
    $("#setup-msg").hidden = true;
    if (!form.hidden) { $("#setup-name").value = ""; $("#setup-note").value = ""; $("#setup-name").focus(); }
  };
  $("#setup-cancel").onclick = () => { form.hidden = true; };
  form.onsubmit = async (e) => {
    e.preventDefault();
    const name = $("#setup-name").value.trim();
    const note = $("#setup-note").value.trim();
    const msg = $("#setup-msg");
    if (!name) { msg.textContent = "版本名不能为空"; msg.hidden = false; $("#setup-name").focus(); return; }
    $("#setup-save").disabled = true;
    const r = await getJSON("/api/setups", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name, note: note || null, game: S.car.game, car_ordinal: S.car.car_ordinal }),
    });
    $("#setup-save").disabled = false;
    if (!r.ok) { msg.textContent = detail(r); msg.hidden = false; return; }
    form.hidden = true;
    await loadSetups();
  };

  // ------------------------------------------------------------------ 版本对比
  function renderCompareForm() {
    const list = S.setups;
    // 只有一个版本也能比：和“没记版本的圈”（第一次记版本之前跑的，id = 0）比，正好是改之前的基线
    show($("#sec-compare"), list.length >= 1);
    if (!list.length) return;
    const selA = $("#cmp-a"), selB = $("#cmp-b");
    const prevA = selA.value, prevB = selB.value;
    const opt = (s) => `<option value="${s.id}">${esc(s.name)}（${s.flying_laps} 圈）</option>`;
    const untagged = `<option value="${UNTAGGED_ID}">没记版本的圈（改之前）</option>`;
    selA.innerHTML = list.map(opt).join("") + untagged;
    selB.innerHTML = list.map(opt).join("") + untagged;
    const has = (v) => v === String(UNTAGGED_ID) || list.some((s) => String(s.id) === v);
    // 默认：B = 当前版本（没有就最新的），A = 它前面那个版本（没有就是没记版本的圈）
    const bDef = list.find((s) => s.is_current) || list[0];
    const aDef = list[list.indexOf(bDef) + 1] || list.find((s) => s !== bDef);
    selB.value = has(prevB) ? prevB : String(bDef.id);
    selA.value = has(prevA) ? prevA : String(aDef ? aDef.id : UNTAGGED_ID);
    selA.onchange = selB.onchange = renderTrackOptions;
    renderTrackOptions();
  }

  function renderTrackOptions() {
    const ids = [Number($("#cmp-a").value), Number($("#cmp-b").value)];
    const need = ids.filter((id) => id !== UNTAGGED_ID).length;   // 没记版本的圈跑过哪些赛道不知道：只看真实版本那边
    const tracks = new Map();
    for (const s of S.setups.filter((x) => ids.includes(x.id))) {
      for (const t of s.tracks || []) {
        const e = tracks.get(t.track_id) || { ...t, n: 0, both: 0 };
        e.n += t.flying_laps;
        e.both += 1;
        tracks.set(t.track_id, e);
      }
    }
    const sel = $("#cmp-track");
    const prev = sel.value;
    const common = [...tracks.values()].filter((t) => t.both === need);
    sel.innerHTML = `<option value="">自动（两个版本都跑过的赛道）</option>` +
      common.map((t) => `<option value="${t.track_id}">${esc(t.track_name || `赛道 #${t.track_id}`)}</option>`).join("");
    if (common.some((t) => String(t.track_id) === prev)) sel.value = prev;
  }

  $("#cmp-form").onsubmit = async (e) => {
    e.preventDefault();
    const a = $("#cmp-a").value, b = $("#cmp-b").value, t = $("#cmp-track").value;
    const out = $("#cmp-out");
    if (a === b) { out.hidden = false; out.innerHTML = `<p class="car-msg err">选两个不同的版本来比。</p>`; return; }
    const seq = ++S.cmpSeq;
    $("#cmp-go").disabled = true;
    out.hidden = false;
    out.innerHTML = `<p class="muted small">对比中（要诊断两边各几圈，可能要几秒）…</p>`;
    const r = await getJSON(`/api/setups/compare?a=${a}&b=${b}${t ? `&track_id=${t}` : ""}`);
    if (seq !== S.cmpSeq) return;
    $("#cmp-go").disabled = false;
    if (!r.ok) { out.innerHTML = `<p class="car-msg err">${esc(detail(r))}</p>`; return; }
    S.cmp = r.data;
    renderCompare();
  };

  function renderCompare() {
    const c = S.cmp;
    if (!c) return;
    const out = $("#cmp-out");
    const A = c.a, B = c.b;
    let cls = "noise", big = "在噪音内";
    if (!c.enough_laps) { cls = "few"; big = c.track_id == null ? "没法比" : "圈数不够"; }
    else if (c.verdict === "faster") { cls = "faster"; big = `B 快了 ${Math.abs(c.delta_median_s).toFixed(3)} s`; }
    else if (c.verdict === "slower") { cls = "slower"; big = `B 慢了 ${Math.abs(c.delta_median_s).toFixed(3)} s`; }
    const stat = (k, v, small = "") => `<div class="stat"><div class="k">${k}</div><div class="v">${v}${small ? ` <small>${small}</small>` : ""}</div></div>`;
    const side = (tag, s) => stat(`${tag} ${esc(s.name)}`, F.fmtLap(s.median_time),
      `中位数 · ${s.n} 圈${num(s.std_time) ? ` · ±${s.std_time.toFixed(3)}` : ""}${s.outliers ? ` · ${s.outliers} 圈失误圈不计` : ""}`);
    out.innerHTML = `
      <div class="verdict ${cls}"><span class="big">${esc(big)}</span><span class="msg">${esc(c.message)}</span></div>
      <div class="cmp-stats">
        ${stat("Δ 中位数（B − A）", num(c.delta_median_s) ? F.fmtDelta(c.delta_median_s) + " s" : "--")}
        ${stat("噪音范围", num(c.noise_threshold_s) ? `±${c.noise_threshold_s.toFixed(3)} s` : "--", "2 × 标准误")}
        ${side('<span class="tag-a">A</span>', A)}
        ${side('<span class="tag-b">B</span>', B)}
      </div>
      ${c.track_name ? `<div class="muted small">比较的赛道：${esc(c.track_name)}</div>` : ""}
      <div id="strip-host"></div>
      <div class="legend">
        <span><i style="background:${COL.a}"></i>A 的圈</span><span><i style="background:${COL.b}"></i>B 的圈</span>
        <span><i class="hollow"></i>失误圈（不计）</span><span><i class="band"></i>A 中位数 ± 噪音范围：B 的中位数（竖线）落在里面 = 在噪音内</span>
      </div>
      ${(c.notes || []).map((n) => `<p class="hint">${esc(n)}</p>`).join("")}
      ${sectorsTable(c)}
      ${findingsTable(c)}
      <p class="muted small method">${esc(c.method || "")}</p>
      <p class="tip small">${esc(c.tip || "")}</p>`;
    drawStrip();
  }

  // 逐弯对比：赛道固定弯道表的每一段（一个弯 + 前后各半段直道），两边中位数用时的差
  function sectorsTable(c) {
    const sc = c.sectors;
    if (!sc || !(sc.sectors || []).length) return "";
    const v = {
      faster: (d) => `<span class="chg-fewer">B 快 ${Math.abs(d).toFixed(3)} s</span>`,
      slower: (d) => `<span class="chg-more">B 慢 ${Math.abs(d).toFixed(3)} s</span>`,
      noise: (d) => `<span class="muted">${num(d) ? F.fmtDelta(d, 3) : ""}（噪音内）</span>`,
      few: (d) => `<span class="muted">${num(d) ? F.fmtDelta(d, 3) : "--"}（圈数不够）</span>`,
    };
    const rows = sc.sectors.map((s) => `<tr${s.verdict === "faster" || s.verdict === "slower" ? "" : ` class="dim"`}>
      <td>${esc(s.name)}${s.name !== s.code ? ` <span class="muted">${esc(s.code)}</span>` : ""}</td>
      <td class="num">${fmtS(s.a_median_s)}</td><td class="num">${fmtS(s.b_median_s)}</td>
      <td>${(v[s.verdict] || v.few)(s.delta_s)}</td></tr>`).join("");
    return `<h3>逐弯对比 <span class="muted small">（每段 = 一个弯加前后各半段直道，A ${sc.laps.a} 圈 / B ${sc.laps.b} 圈的中位数）</span></h3>
      <div class="tbl-wrap"><table>
      <thead><tr><th>弯</th><th class="num"><span class="tag-a">A</span> 用时</th><th class="num"><span class="tag-b">B</span> 用时</th><th>变化</th></tr></thead>
      <tbody>${rows}</tbody></table></div>
      <p class="muted small">每段也按两边圈速的波动算噪音范围（和上面整圈一样是 2 × 标准误），超出范围才算真快了 / 慢了。</p>`;
  }

  function findingsTable(c) {
    const na = c.diag_laps ? c.diag_laps.a : 0, nb = c.diag_laps ? c.diag_laps.b : 0;
    if (!na && !nb) return c.enough_laps ? `<p class="muted small">没能诊断（圈数据不够）。</p>` : "";
    const head = `<h3>诊断对比 <span class="muted small">（每个版本最近 ${na} / ${nb} 圈，同一项问题出现在几圈、累计多久）</span></h3>`;
    if (!(c.findings_by_id || []).length) return head + `<p class="muted small">两个版本的这几圈都没诊断出问题。</p>`;
    const cell = (x, n) => `${x.laps_with}/${n}`;
    const tot = (x) => (num(x.total_s) ? `${x.total_s.toFixed(2)} s` : "");
    const chg = { fewer: `<span class="chg-fewer">B 少了</span>`, more: `<span class="chg-more">B 多了</span>`, same: `<span class="muted">一样</span>` };
    // 调车诊断页“照这条改了”记的版本，说明是“针对诊断：<标题> #<诊断项 id>”：按 id 把这一行标出来
    // （标题里带弯道编号，每圈可能不一样；id 是固定的），一眼看它少没少
    const m = /^针对诊断：.*#([\w.:-]+)\s*$/.exec(c.b.note || "");
    const target = m ? m[1] : null;
    const rows = c.findings_by_id.map((f) => `<tr${f.id === target ? ` class="target"` : ""}>
      <td class="wrap">${esc(f.title)} <span class="muted">${esc(f.category_label || "")}</span>${f.id === target ? ` <span class="badge-target">这次针对的</span>` : ""}</td>
      <td class="num">${cell(f.a, na)}</td><td class="num">${tot(f.a)}</td>
      <td class="num">${cell(f.b, nb)}</td><td class="num">${tot(f.b)}</td>
      <td>${chg[f.change] || ""}</td></tr>`).join("");
    return head + `<div class="tbl-wrap"><table>
      <thead><tr><th>诊断项</th><th class="num"><span class="tag-a">A</span> 出现</th><th class="num">累计</th>
        <th class="num"><span class="tag-b">B</span> 出现</th><th class="num">累计</th><th>变化</th></tr></thead>
      <tbody>${rows}</tbody></table></div>
      <p class="muted small">“出现 3/5” = 诊断的 5 圈里有 3 圈出现这个问题；累计 = 这几圈里这个问题持续的总时长。点
        <a href="/tuning.html">调车诊断</a> 看每条的判断依据。</p>`;
  }

  // 圈时分布：两行点（A 上、B 下），竖线 = 中位数，灰带 = A 中位数 ± 噪音范围；点一下打开那圈的对比
  function drawStrip() {
    const host = $("#strip-host");
    const c = S.cmp;
    if (!host || !c) return;
    const all = c.a.laps.concat(c.b.laps);
    if (!all.length) { host.innerHTML = ""; return; }
    const W = Math.max(280, host.clientWidth || 600), H = 96, padL = 26, padR = 14;
    const kept = all.filter((l) => !l.outlier).map((l) => l.lap_time);
    let lo = Math.min(...(kept.length ? kept : all.map((l) => l.lap_time)));
    let hi = Math.max(...(kept.length ? kept : all.map((l) => l.lap_time)));
    if (num(c.noise_threshold_s) && num(c.a.median_time)) {
      lo = Math.min(lo, c.a.median_time - c.noise_threshold_s);
      hi = Math.max(hi, c.a.median_time + c.noise_threshold_s);
    }
    const span = Math.max(hi - lo, 0.2);
    lo -= span * 0.08; hi += span * 0.08;
    const X = (t) => padL + (Math.min(Math.max(t, lo), hi) - lo) / (hi - lo) * (W - padL - padR);
    const rows = [{ s: c.a, y: 24, col: COL.a, tag: "A" }, { s: c.b, y: 56, col: COL.b, tag: "B" }];
    let svg = "";
    if (num(c.noise_threshold_s) && num(c.a.median_time)) {
      const x0 = X(c.a.median_time - c.noise_threshold_s), x1 = X(c.a.median_time + c.noise_threshold_s);
      svg += `<rect x="${x0}" y="8" width="${Math.max(1, x1 - x0)}" height="64" rx="3" fill="currentColor" fill-opacity="0.10"><title>A 中位数 ± 噪音范围 ${c.noise_threshold_s.toFixed(3)} s</title></rect>`;
    }
    for (const r of rows) {
      svg += `<text class="lbl" x="4" y="${r.y + 4}">${r.tag}</text>`;
      svg += `<line x1="${padL}" x2="${W - padR}" y1="${r.y}" y2="${r.y}" stroke="${COL.line}" />`;
      for (const l of r.s.laps) {
        const cx = X(l.lap_time).toFixed(1);
        const tip = `圈 #${l.id} ${F.fmtLap(l.lap_time)}${l.outlier ? "（失误圈，不计）" : ""}——点击在对比页打开`;
        svg += `<a href="/laps.html?b=${l.id}"><circle cx="${cx}" cy="${r.y}" r="5" fill="${l.outlier ? "none" : r.col}" stroke="${l.outlier ? COL.muted : COL.panel}" stroke-width="${l.outlier ? 2 : 1.5}"><title>${esc(tip)}</title></circle></a>`;
      }
      if (num(r.s.median_time)) {
        const x = X(r.s.median_time).toFixed(1);
        svg += `<line x1="${x}" x2="${x}" y1="${r.y - 11}" y2="${r.y + 11}" stroke="${COL.text}" stroke-width="2"><title>${r.tag} 中位数 ${F.fmtLap(r.s.median_time)}</title></line>`;
      }
    }
    // 横轴：4~5 个刻度
    const step = niceStep((hi - lo) / 4);
    const dec = step >= 1 ? 0 : step >= 0.1 ? 1 : step >= 0.01 ? 2 : 3;     // 刻度标签保留到步长那一位，免得重复
    for (let k = Math.ceil(lo / step); k * step <= hi; k++) {
      const t = k * step;
      const x = X(t).toFixed(1);
      const full = F.fmtLap(t);
      const lab = dec >= 3 ? full : full.slice(0, dec ? dec - 3 : -4);
      svg += `<line x1="${x}" x2="${x}" y1="74" y2="78" stroke="${COL.muted}" /><text x="${x}" y="91" text-anchor="middle">${esc(lab)}</text>`;
    }
    host.innerHTML = `<svg class="strip" viewBox="0 0 ${W} ${H}" width="${W}" height="${H}" role="img" aria-label="两个版本的圈时分布">${svg}</svg>`;
  }
  function niceStep(raw) {
    const p = Math.pow(10, Math.floor(Math.log10(raw)));
    for (const m of [1, 2, 5, 10]) if (raw <= m * p) return m * p;
    return 10 * p;
  }

  // ------------------------------------------------------------------ 动力总成
  async function loadPower() {
    const key = carKey(S.car);
    const seq = ++S.ptSeq;
    $("#pt-sub").textContent = "加载中…";
    const pi = S.pi != null ? `&car_pi=${S.pi}` : "";
    const r = await getJSON(`/api/powertrain?${q()}${pi}`);
    if (seq !== S.ptSeq || key !== carKey(S.car)) return;
    S.pt = r.ok ? r.data : null;
    renderPower(r.ok ? null : detail(r));
  }

  function renderPower(err) {
    const pt = S.pt;
    destroyCharts();
    const hasCurve = !!(pt && pt.power_curve && pt.power_curve.length);
    show($("#pt-charts"), hasCurve);
    const empty = $("#pt-empty");
    show(empty, !hasCurve);
    if (!pt) {
      empty.textContent = err || "还没有数据";
      $("#pt-sub").textContent = "";
      show($("#pi-wrap"), false);
      show($("#sec-gears"), false);
      show($("#sec-final"), false);
      return;
    }
    show($("#sec-gears"), true);
    show($("#sec-final"), Object.keys(pt.ratios || {}).length > 0);
    const pk = pt.peak_power, tk = pt.peak_torque;
    $("#pt-sub").textContent = hasCurve
      ? `${pt.source_laps} 圈数据 · 峰值 ${pk.power_kw.toFixed(0)} kW @ ${pk.rpm} rpm · ${tk.torque.toFixed(0)} N·m @ ${tk.rpm} rpm`
      : "";
    if (!hasCurve) empty.textContent = (pt.notes || []).find((n) => n.includes("功率") || n.includes("PI")) || "还没有全油门的功率数据";
    const piWrap = $("#pi-wrap"), piSel = $("#pi-sel");
    show(piWrap, (pt.pi_list || []).length > 1);
    piSel.innerHTML = (pt.pi_list || []).map((p) => `<option value="${p}">${p}</option>`).join("");
    piSel.value = String(pt.car_pi);
    piSel.onchange = () => { S.pi = Number(piSel.value); loadPower(); };
    if (hasCurve) buildCharts();
    renderGears();
  }

  function destroyCharts() {
    for (const u of S.charts) u.destroy();
    S.charts = [];
    $("#chart-power").innerHTML = "";
    $("#chart-torque").innerHTML = "";
  }

  function shiftMarks() {
    // 同一转速的几个挡合成一个标签（例如 1、2 挡都在红线换）
    const by = new Map();
    for (const [g, rpm] of Object.entries(S.pt.shift_rpm || {})) {
      const k = Math.round(rpm / 10) * 10;
      by.set(k, (by.get(k) || []).concat(`${g}→${Number(g) + 1}`));
    }
    return [...by.entries()].sort((a, b) => a[0] - b[0]);
  }

  function markerPlugin(withLabels) {
    return {
      hooks: {
        draw: [(u) => {
          const ctx = u.ctx, dpr = devicePixelRatio || 1;
          const { left, top, width, height } = u.bbox;
          ctx.save();
          ctx.font = `${11 * dpr}px system-ui, sans-serif`;
          ctx.textBaseline = "middle";
          ctx.textAlign = "left";               // uPlot 画坐标轴时会改成 right / center
          const rowEnds = [];                   // 每一行最右边已经占到哪儿（贪心排行，标签不互相压）
          const pad = 3 * dpr, lh = 15 * dpr;
          for (const [rpm, labels] of shiftMarks()) {
            const x = Math.round(u.valToPos(rpm, "x", true)) + 0.5;
            if (x < left - 1 || x > left + width + 1) continue;
            ctx.setLineDash([4 * dpr, 4 * dpr]);
            ctx.strokeStyle = COL.muted;
            ctx.lineWidth = dpr;
            ctx.beginPath(); ctx.moveTo(x, top); ctx.lineTo(x, top + height); ctx.stroke();
            if (!withLabels) continue;
            const txt = labels.join(" ");
            const w = ctx.measureText(txt).width + 2 * pad;
            // 靠右边放不下就放到竖线左边
            const x0 = x + w + 2 * dpr > left + width ? x - w - 2 * dpr : x + 2 * dpr;
            let row = rowEnds.findIndex((end) => end < x0 - 2 * dpr);
            if (row < 0) { row = rowEnds.length; rowEnds.push(0); }
            rowEnds[row] = x0 + w;
            const y = top + (9 + row * 17) * dpr;
            ctx.setLineDash([]);
            ctx.fillStyle = COL.panel;
            ctx.strokeStyle = COL.line;
            ctx.beginPath();
            ctx.rect(x0, y - lh / 2, w, lh);
            ctx.fill(); ctx.stroke();
            ctx.fillStyle = COL.text;
            ctx.fillText(txt, x0 + pad, y + 0.5 * dpr);
          }
          ctx.restore();
        }],
      },
    };
  }

  function buildCharts() {
    if (typeof uPlot === "undefined") {
      $("#pt-charts").innerHTML = `<p class="car-msg err">曲线库（/vendor/uPlot.iife.min.js）没加载成功，刷新试试。</p>`;
      return;
    }
    const pc = S.pt.power_curve;
    const x = pc.map((p) => p.rpm);
    const red = S.pt.redline_rpm;
    const xmin = Math.min(...x) - 100, xmax = Math.max(red || 0, Math.max(...x)) + 100;
    const width = Math.max(260, $("#chart-power").clientWidth);
    const font = "12px system-ui, sans-serif";
    const mk = (host, data, color, fmt, h, readout, unit, labels) => {
      const opts = {
        width, height: h,
        legend: { show: false },
        cursor: { sync: { key: "forza-pt", setSeries: false }, y: false, drag: { x: false, y: false }, points: { size: 8 } },
        scales: { x: { time: false, range: () => [xmin, xmax] }, y: { range: (u, mn, mx) => [Math.max(0, mn - (mx - mn) * 0.15), mx + (mx - mn) * 0.12 + 1] } },
        axes: [
          { stroke: COL.muted, font, grid: { stroke: GRID }, ticks: { show: false }, values: (u, v) => v.map((r) => `${r}`) },
          { stroke: COL.muted, font, size: 44, grid: { stroke: GRID }, ticks: { show: false }, values: (u, v) => v.map(fmt) },
        ],
        series: [{}, { stroke: color, width: 2, points: { show: false } }],
        plugins: [markerPlugin(labels)],
        hooks: {
          setCursor: [(u) => {
            const i = u.cursor.idx;
            $(readout).textContent = i == null ? "" : `${x[i]} rpm · ${fmt(data[i])} ${unit}`;
          }],
        },
      };
      const u = new uPlot(opts, [x, data], $(host));
      S.charts.push(u);
    };
    mk("#chart-power", pc.map((p) => p.power_kw), COL.a, (v) => v.toFixed(0), 210, "#pt-readout-p", "kW", true);
    mk("#chart-torque", pc.map((p) => p.torque), COL.b, (v) => v.toFixed(0), 150, "#pt-readout-t", "N·m", false);
  }

  function renderGears() {
    const pt = S.pt;
    const gears = pt.gears || [];
    const dt = F.DRIVETRAIN[pt.drivetrain] || "";
    $("#gear-sub").textContent = [pt.redline_rpm ? `红线 ${pt.redline_rpm} rpm` : "", pt.wheel_radius_m ? `车轮半径 ${pt.wheel_radius_m.toFixed(3)} m` : "", dt]
      .filter(Boolean).join(" · ");
    const tbl = $("#gear-tbl");
    if (!gears.length) {
      tbl.innerHTML = `<tbody><tr><td class="muted">还没有反推出传动比（要有离合接合、车速 ≥ 30 km/h 的行驶数据）。</td></tr></tbody>`;
    } else {
      const top = gears[gears.length - 1].gear;
      const how = { cross: "", redline: `<span class="flag">红线</span>` };
      const rows = gears.map((g) => {
        const flags = (g.scaled ? `<span class="flag" title="这挡最近没开到，按其他挡同比例的变化推算">推算</span>` : "") +
          (g.stale ? `<span class="flag" title="这挡最近没开到，别的挡传动比变了，这个数可能已经过期">可能过期</span>` : "");
        const shift = g.shift_rpm != null ? `${g.shift_rpm}${how[g.shift_how] || ""}` : (g.gear === top ? `<span class="muted">最高挡</span>` : `<span class="muted">算不出</span>`);
        const diff = g.shift_rpm != null && g.actual_upshift_rpm != null ? g.actual_upshift_rpm - g.shift_rpm : null;
        const diffTxt = diff == null ? "" : Math.abs(diff) < 150 ? `<span class="muted">（差不多）</span>`
          : diff < 0 ? `<span class="muted">（早 ${-diff}）</span>` : `<span class="muted">（晚 ${diff}）</span>`;
        return `<tr>
          <td>${g.gear}</td>
          <td class="num">${shift}</td>
          <td class="num">${fmtInt(g.after_shift_rpm)}</td>
          <td class="num">${g.actual_upshift_rpm != null ? `${g.actual_upshift_rpm}${diffTxt}` : `<span class="muted">--</span>`}</td>
          <td class="num">${fmtInt(g.actual_drop_rpm)}</td>
          <td class="num">${g.upshift_count || ""}</td>
          <td class="num mono">${g.ratio.toFixed(3)}${flags}</td>
          <td class="num">${g.step_pct != null ? `${g.step_pct.toFixed(1)}%` : ""}</td>
          <td class="num">${num(g.kmh_per_1000rpm) ? g.kmh_per_1000rpm.toFixed(1) : "--"}</td>
          <td class="num">${num(g.redline_kmh) ? g.redline_kmh.toFixed(0) : "--"}</td>
        </tr>`;
      }).join("");
      tbl.innerHTML = `<thead><tr>
          <th>挡</th>
          <th class="num" title="按实测功率曲线算的最佳升挡转速">最佳升挡</th><th class="num" title="在最佳升挡点升挡后，下一挡的转速">升挡后</th>
          <th class="num" title="最近几圈加速升挡时的平均转速">你实际升挡</th><th class="num" title="升挡后转速掉到多少">实际掉到</th><th class="num">次数</th>
          <th class="num" title="挡位齿比 × 终传（转速 ÷ 驱动轮轮速反推）">综合传动比</th><th class="num" title="升到下一挡，转速变化">到下一挡</th>
          <th class="num">每千转 km/h</th><th class="num">红线车速 km/h</th>
        </tr></thead><tbody>${rows}</tbody>`;
    }
    $("#pt-notes").innerHTML = (pt.notes || []).map((n) => `<li>${esc(n)}</li>`).join("");
  }

  // ------------------------------------------------------------------ 改终传
  const fdKey = () => `forza.car.final.${carKey(S.car)}`;
  function loadFinalInputs() {
    const v = store(fdKey()) || {};
    $("#fd-now").value = v.now ?? "";
    $("#fd-new").value = v.new ?? "";
    $("#fd-out").hidden = true;
  }
  $("#fd-form").onsubmit = async (e) => {
    e.preventDefault();
    const now = parseFloat($("#fd-now").value), nw = parseFloat($("#fd-new").value);
    const out = $("#fd-out");
    out.hidden = false;
    if (!(now > 0) || !(nw > 0)) { out.innerHTML = `<p class="car-msg err">两个终传都要填（游戏调校界面里的数字，例如 3.85）。</p>`; return; }
    store(fdKey(), { now, new: nw });
    $("#fd-go").disabled = true;
    const pi = S.pt && S.pt.car_pi != null ? `&car_pi=${S.pt.car_pi}` : "";
    const r = await getJSON(`/api/powertrain/final_drive?${q()}${pi}&final_now=${now}&final_new=${nw}`);
    $("#fd-go").disabled = false;
    if (!r.ok) { out.innerHTML = `<p class="car-msg err">${esc(detail(r))}</p>`; return; }
    const d = r.data;
    const big = { ok: "不会撞断油", close: "可能撞断油", limiter: "会撞断油" }[d.verdict] || "说不准";
    const rows = d.gears.map((g) => `<tr><td>${g.gear}</td><td class="num mono">${g.gear_ratio.toFixed(3)}</td>
      <td class="num">${num(g.redline_kmh_now) ? g.redline_kmh_now.toFixed(0) + " km/h" : "--"}</td>
      <td class="num">${num(g.redline_kmh_new) ? g.redline_kmh_new.toFixed(0) + " km/h" : "--"}</td></tr>`).join("");
    out.innerHTML = `
      <div class="verdict ${d.verdict || "none"}"><span class="big">${esc(big)}</span><span class="msg">${esc(d.message)}</span></div>
      <div class="cmp-stats">
        <div class="stat"><div class="k">终传变化</div><div class="v">${d.change_pct > 0 ? "+" : ""}${d.change_pct}% <small>${d.change_pct > 0 ? "变短：加速更猛" : d.change_pct < 0 ? "变长：极速更高" : ""}</small></div></div>
        <div class="stat"><div class="k">这条赛道的最高车速</div><div class="v">${num(d.vmax_kmh) ? d.vmax_kmh.toFixed(0) + " km/h" : "--"} <small>${d.laps} 圈</small></div></div>
        <div class="stat"><div class="k">现在每圈顶断油</div><div class="v">${num(d.limiter_s_now) ? d.limiter_s_now.toFixed(1) + " s" : "--"}</div></div>
      </div>
      <div class="tbl-wrap"><table class="fd-tbl"><thead><tr><th>挡</th><th class="num" title="综合传动比 ÷ 当前终传">挡位齿比</th>
        <th class="num">现在红线车速</th><th class="num">改后红线车速</th></tr></thead><tbody>${rows}</tbody></table></div>
      <ul class="notes small">${(d.notes || []).map((n) => `<li>${esc(n)}</li>`).join("")}</ul>`;
  };

  // ------------------------------------------------------------------ 刷新
  let reloadTimer = null;
  F.onLapsChanged(() => {
    clearTimeout(reloadTimer);
    reloadTimer = setTimeout(() => {
      if (!S.car || S.editing) return;
      loadSetups();
      loadPower();
    }, 800);
  });
  let resizeTimer = null;
  window.addEventListener("resize", () => {
    clearTimeout(resizeTimer);
    resizeTimer = setTimeout(() => {
      if (S.pt && S.charts.length) { destroyCharts(); buildCharts(); }
      drawStrip();
    }, 150);
  });

  init();
})();
