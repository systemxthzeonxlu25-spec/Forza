// 调车诊断页：选圈 → 调 /api/laps/{id}/diagnose（多圈：/api/diagnose?ids=）→ 问题卡片 + 赛道小地图 + 分析块。
// 地址栏参数：?lap=ID 单圈；?ids=1,2,3 多圈合并。
// 分析块：转向平衡分布（车速分档 + 弯 × 阶段热力表）、刹车分析（直线重刹前后轴 + 踏板-减速度）、车身与悬挂。
// 新圈存好后（Forza.onLapsChanged）自动刷新圈列表；正在看的是最新一圈时自动换成新圈并重新诊断。
(function () {
  "use strict";
  const F = window.Forza;
  F.nav("tuning");

  const SEV = { alert: "重点", warn: "注意", info: "提示" };
  const SEV_ORDER = { info: 0, warn: 1, alert: 2 };
  const CONF = { high: "高", medium: "中", low: "低" };
  const CHECK = { found: "发现问题", ok: "正常", skipped: "没法判断" };
  const WHEEL = { fl: "左前", fr: "右前", rl: "左后", rr: "右后" };
  const TEND = { understeer: "推头", oversteer: "甩尾", neutral: "中性" };
  const LS_ABS = "forza.tuning.abs";
  const LS_AERO = "forza.tuning.aero:";       // + game:car_ordinal（空力是每台车的属性）

  const $ = (id) => document.getElementById(id);
  function el(tag, cls, text) {
    const e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text !== undefined && text !== null) e.textContent = text;
    return e;
  }
  const cssVar = window.Forza.cssVar;      // 读 CSS 变量（见 common.js）
  function lsGet(k) { try { return localStorage.getItem(k); } catch (_) { return null; } }
  function lsSet(k, v) { try { if (v === null) localStorage.removeItem(k); else localStorage.setItem(k, v); } catch (_) { /* 隐私模式等 */ } }
  const num = (x, d = 2) => (x === null || x === undefined || !isFinite(x) ? "--" : Number(x).toFixed(d));
  const signed = (x, d = 2) => {
    if (x === null || x === undefined || !isFinite(x)) return "--";
    if (Math.abs(x) < 0.5 * 10 ** -d) return (0).toFixed(d);          // 四舍五入是 0 的不带正负号
    return (x >= 0 ? "+" : "−") + Math.abs(x).toFixed(d);
  };
  const pct = (x) => (x === null || x === undefined || !isFinite(x) ? "--" : `${Math.round(x * 100)}%`);
  // 对比页深链（C4）：b = 这个位置所属的圈，at = 这圈自己的距离（对比页会换算到对比网格）
  const cmpHref = (lapId, dist) => `/laps.html?b=${encodeURIComponent(lapId)}&at=${Math.max(0, Math.round(dist))}`;

  const state = {
    laps: null,       // /api/laps 的结果；拿不到时为 null（改用手动输入）
    result: null,     // 当前诊断结果
    map: null,        // {x, z, d}：地图坐标
    markers: [],      // 画出来的标记
    hl: null,         // 高亮的标记 key
    baseMsg: "",      // 常驻提示（比如圈列表拿不到），诊断时不清掉
    running: false,   // 正在诊断
    newLapTimer: 0,
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
    if (l.setup_name) parts.push(`· ${l.setup_name}`);
    if (l.is_complete === false) parts.push("（不完整）");
    return parts.join("  ");
  }

  // 拉圈列表。keep = 保留下拉框当前的选择（自动刷新时用）
  async function loadLaps(keep) {
    const sel = $("tn-lap");
    const prev = keep ? sel.value : "";
    try {
      const data = await F.api("/api/laps?limit=300");
      const arr = Array.isArray(data) ? data : data && (data.laps || data.items || data.data);
      if (!Array.isArray(arr)) throw new Error("返回格式不认识");
      state.laps = arr.filter((l) => l && l.id !== null && l.id !== undefined).sort((a, b) => b.id - a.id);
    } catch (e) {
      if (keep && state.laps) return;              // 自动刷新失败：保留旧列表
      state.laps = null;
      useManual(`圈列表暂时拿不到（${e.message}），可以直接输入圈号。`);
      return;
    }
    if (state.baseMsg && $("tn-pick-wrap").hidden) {   // 之前拿不到、现在拿到了：切回下拉框
      $("tn-pick-wrap").hidden = false;
      $("tn-manual-wrap").hidden = true;
      $("tn-multi-wrap").hidden = false;
      state.baseMsg = "";
    }
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
    if (prev && [...sel.options].some((o) => o.value === prev)) sel.value = prev;
    updateMultiHint();
  }

  function lapById(id) {
    return state.laps ? state.laps.find((l) => l.id === id) : null;
  }

  // 选中的圈 + 同车同赛道、id 不大于它的最近 n-1 圈。
  // 选中的圈有调校版本（setup_id）时只取同一版本的完整圈（C2）；没有版本时维持原来的规则。
  function recentIds(id, n) {
    const base = lapById(id);
    if (!base) return [id];
    const hasSetup = base.setup_id !== null && base.setup_id !== undefined;
    const same = state.laps
      .filter((l) => l.id <= id && l.track_id === base.track_id && l.car_ordinal === base.car_ordinal && l.game === base.game)
      .filter((l) => !hasSetup || l.id === id || (l.setup_id === base.setup_id && l.is_complete !== false))
      .sort((a, b) => b.id - a.id)
      .slice(0, n)
      .map((l) => l.id);
    return same.sort((a, b) => a - b);
  }

  function updateMultiHint() {
    const hint = $("tn-multi-hint");
    const base = state.laps ? lapById(parseInt($("tn-lap").value, 10)) : null;
    const on = $("tn-multi").checked && base && base.setup_id !== null && base.setup_id !== undefined;
    hint.hidden = !on;
    if (on) hint.textContent = `合并时只取同一调校版本「${base.setup_name || `#${base.setup_id}`}」的完整圈`;
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

  // ---- 用户勾选的辅助：ABS（全局记住）、可调空力（按车记住）
  function carKey(lap) {
    return lap && lap.game && lap.car_ordinal !== null && lap.car_ordinal !== undefined ? `${lap.game}:${lap.car_ordinal}` : null;
  }

  function syncAero() {
    const base = state.laps ? lapById(parseInt($("tn-lap").value, 10)) : null;
    const k = carKey(base);
    const v = k ? lsGet(LS_AERO + k) : null;
    $("tn-aero").value = v === "1" || v === "0" ? v : "";
  }

  function assistQuery() {
    const q = [];
    if ($("tn-abs").checked) q.push("abs=1");
    const a = $("tn-aero").value;
    if (a === "1" || a === "0") q.push(`aero=${a}`);
    return q.join("&");
  }

  // ================================================================ 诊断

  async function run(ids, opts = {}) {
    if (!ids.length) {
      showMsg("先选一圈（或输入圈号）。", true);
      return;
    }
    const go = $("tn-go");
    go.disabled = true;
    go.textContent = "诊断中…";
    state.running = true;
    showMsg("", false);
    const aq = assistQuery();
    const url = (ids.length === 1 ? `/api/laps/${ids[0]}/diagnose` : `/api/diagnose?ids=${ids.join(",")}`) +
      (aq ? (ids.length === 1 ? "?" : "&") + aq : "");
    try {
      const res = await F.api(url);
      state.result = res;
      history.replaceState(null, "", ids.length === 1 ? `?lap=${ids[0]}` : `?ids=${ids.join(",")}`);
      render(res);
      loadMap(res);
      if (ids.length > 1 && res.laps && res.laps.length < ids.length) showMsg("有的圈没找到，已跳过。", false);
      if (opts.after) opts.after();
    } catch (e) {
      clearResult();
      showMsg(`诊断失败：${e.message}`, true);
    } finally {
      state.running = false;
      go.disabled = false;
      go.textContent = "诊断";
    }
  }

  // ================================================================ 新圈自动刷新（第 1 条）

  function hideNewLap() {
    $("tn-newlap").hidden = true;
    clearTimeout(state.newLapTimer);
  }

  function showNewLap(text, action) {
    const box = $("tn-newlap");
    $("tn-newlap-text").textContent = text;
    const go = $("tn-newlap-go");
    go.hidden = !action;
    if (action) {
      go.textContent = action.label;
      go.onclick = () => { hideNewLap(); action.fn(); };
    }
    box.hidden = false;
    clearTimeout(state.newLapTimer);
    if (!action) state.newLapTimer = setTimeout(hideNewLap, 15000);
  }

  async function onLapsChanged(detail) {
    if (!state.laps) {                           // 手动输入模式：试着重新拿列表
      await loadLaps(false);
      if (!state.laps) return;
    }
    const prevTop = state.laps.length ? state.laps[0].id : null;
    await loadLaps(true);
    const newId = (detail && detail.last_saved_lap_id) || (state.laps.length ? state.laps[0].id : null);
    if (!newId || newId === prevTop) return;
    const lap = lapById(newId);
    const what = lap ? lapText(lap) : `#${newId}`;
    const res = state.result;
    const showingLatest = !res || (res.mode === "single" && res.lap && res.lap.id === prevTop);
    if (showingLatest && !state.running && !$("tn-multi").checked) {
      $("tn-lap").value = String(newId);
      syncAero();
      run([newId], { after: () => showNewLap(`新圈已存好，已自动换成这一圈：${what}`, null) });
      return;
    }
    if ($("tn-multi").checked) {
      showNewLap(`新圈已存好：${what}`, { label: "合并时带上新圈", fn: () => { $("tn-lap").value = String(newId); syncAero(); updateMultiHint(); run(currentSelection()); } });
    } else {
      showNewLap(`新圈已存好：${what}`, { label: "诊断这一圈", fn: () => { $("tn-lap").value = String(newId); syncAero(); run([newId]); } });
    }
  }

  // ================================================================ 渲染

  const BLOCKS = ["tn-bal", "tn-brk", "tn-body"];

  // 诊断失败时把上一次的结果收起来，免得误以为是这一圈的
  function clearResult() {
    state.result = null;
    state.map = null;
    state.markers = [];
    $("tn-findings").textContent = "";
    for (const id of ["tn-head", "tn-checks", "tn-map-card", "tn-dq", "tn-na", ...BLOCKS]) $(id).hidden = true;
  }

  function render(res) {
    renderHead(res);
    renderFindings(res);
    renderBlocks(res);
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
    if (res.mode === "multi" && res.laps) {          // 多圈：几圈的调校版本一样才显示版本名
      const names = [...new Set(res.laps.map((l) => (l.setup_id === null || l.setup_id === undefined ? "（没标版本）" : l.setup_name || `#${l.setup_id}`)))];
      if (names.length === 1 && names[0] !== "（没标版本）") t.appendChild(el("span", "badge plain", `调校：${names[0]}`));
      else if (names.length > 1) t.appendChild(el("span", "badge plain", `调校：混合 ${names.length} 个版本`));
    } else if (lap.setup_name) t.appendChild(el("span", "badge plain", `调校：${lap.setup_name}`));
    if (res.mode === "multi" && s.reference_lap) t.appendChild(el("span", "muted", `（概况数字取自 ${s.reference_lap}）`));
    if (s.samples) t.appendChild(el("span", "muted tn-small", `${s.samples} 个采样点${s.sample_hz ? ` · ${Math.round(s.sample_hz)} Hz` : ""}`));
    box.appendChild(t);

    const g = el("div", "tn-stats");
    g.appendChild(stat("圈时", F.fmtLap(s.lap_time_s)));
    g.appendChild(stat("驱动", s.drivetrain_label || "未知"));
    g.appendChild(stat("最高车速", s.max_speed_kmh ? `${Math.round(s.max_speed_kmh)}` : "--", s.max_speed_kmh ? "km/h" : ""));
    g.appendChild(stat("用到的最高挡", s.top_gear_used ? `${s.top_gear_used} 挡` : "--"));
    const src = s.corner_source === "layout" ? "固定表" : s.corner_source === "detected" ? "自动识别" : "";
    if (s.turn_share) {
      g.appendChild(stat("弯道", `${s.corner_count} 个`, `右 ${Math.round(s.turn_share.right * 100)}% / 左 ${Math.round(s.turn_share.left * 100)}%${src ? ` · ${src}` : ""}`));
    } else {
      g.appendChild(stat("弯道", s.corner_count ? `${s.corner_count} 个` : "--", src));
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
    const as = s.assists || {};
    const asTxt = [as.abs_on === true ? "按开着 ABS 解读" : null,
      as.aero === true ? "有可调空力" : as.aero === false ? "没有可调空力" : null].filter(Boolean);
    if (asTxt.length) c.appendChild(el("span", "badge plain", asTxt.join(" · ")));
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
    fs.forEach((f, i) => box.appendChild(findingCard(f, i + 1, res)));
  }

  function findingCard(f, num, res) {
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

    // 位置（点按钮：在地图上高亮；“在对比页查看”：跳到对比页对应的距离）
    const locs = el("div", "fd-locs");
    (f.locations || []).forEach((l, j) => {
      const onMap = l.phase !== null && l.phase !== undefined;
      const text = (l.lap_label ? `${l.lap_label}：` : "") + (l.label || `${Math.round(l.dist_m)} m`);
      const wrap = el("span", "fd-loc-wrap");
      const b = el("button", "fd-loc" + (onMap ? "" : " static"), text);
      b.type = "button";
      if (onMap) {
        b.title = "在赛道图上标出";
        b.addEventListener("click", () => highlight(`${num}:${j}`, b));
      } else {
        b.title = "整圈统计，不在图上标";
      }
      b.dataset.key = `${num}:${j}`;
      wrap.appendChild(b);
      const lapId = l.lap_id !== undefined && l.lap_id !== null ? l.lap_id : res.lap && res.lap.id;
      if (onMap && lapId && l.dist_m !== null && l.dist_m !== undefined) {
        const a = el("a", "fd-cmp", "在对比页查看");
        a.href = cmpHref(lapId, l.dist_m);
        a.title = "打开圈速对比页，放大到这个位置（和同车同赛道最快的圈比）";
        wrap.appendChild(a);
      }
      locs.appendChild(wrap);
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
    const lap = res.lap || null;                   // 诊断结果自带圈的 game / car_ordinal（不依赖页面上拉到的圈列表）
    for (const s of f.suggestions || []) {
      const li = el("li", "", s);
      if (canRecord(lap) && isSetupChange(s)) li.appendChild(tryButton(s, f, lap));
      ol.appendChild(li);
    }
    card.appendChild(ol);
    if (f.caveat) card.appendChild(el("p", "muted fd-caveat", f.caveat));
    return card;
  }

  // “照这条改了”：在游戏里按某条建议改完调校后，一键记成新的调校版本（名字预填成这条建议）。
  // 之后存的圈归到新版本；车辆调校页的版本对比会告诉你圈速变没变、这项问题是不是少了。
  // 只有真的是“改调校”的建议才给按钮：驾驶建议（先排除驾驶、换走线、先把刹车做稳…）和提示性的话
  // 点了会凭空记一个新版本、把之后的圈错归过去，所以按调校项关键词白名单判断
  const SETUP_WORDS = /防倾杆|弹簧|阻尼|下压力|空力|齿比|终传|差速|锁止|刹车平衡|刹车压力|胎压|外倾|前束|后倾|车身高度|离地间隙/;
  function isSetupChange(s) {
    return SETUP_WORDS.test(s) && !/^(先|如果)/.test(s);
  }
  function canRecord(lap) {
    return !!(lap && lap.game && lap.car_ordinal !== null && lap.car_ordinal !== undefined);
  }
  function tryButton(text, f, lap) {
    const b = el("button", "fd-try", "照这条改了");
    b.type = "button";
    b.title = "在游戏里改完之后点：记成一个新的调校版本，之后跑的圈都归到它，方便和改之前对比";
    b.addEventListener("click", async () => {
      const name = window.prompt("新调校版本的名字（改了什么，写具体点更好，比如“前防倾杆 30 → 27”）：", text.slice(0, 60));
      if (name === null || !name.trim()) return;
      b.disabled = true;
      try {
        await F.api("/api/setups", { method: "POST", body: {
          game: lap.game, car_ordinal: lap.car_ordinal, name: name.trim().slice(0, 60),
          note: `针对诊断：${f.title} #${f.id}`.slice(0, 300) } });
        b.replaceWith(el("span", "fd-try-ok", "已记成新版本。跑 5 圈以上，再到“车辆调校”页和改之前的版本对比"));
      } catch (e) {
        b.disabled = false;
        alert(`没记上：${e.message}`);
      }
    });
    return b;
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
      ctx.fillText(c.label || c.name, pos[0], pos[1]);
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
      ctx.fillStyle = cssVar("--on-accent");
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

  // ================================================================ 分析块：公用小工具

  const SVGNS = "http://www.w3.org/2000/svg";
  function svgEl(tag, attrs, parent) {
    const e = document.createElementNS(SVGNS, tag);
    for (const k in attrs) e.setAttribute(k, attrs[k]);
    if (parent) parent.appendChild(e);
    return e;
  }
  function svgTitle(node, text) {
    svgEl("title", {}, node).textContent = text;
  }

  function fillNotes(id, notes) {
    const ul = $(id);
    ul.textContent = "";
    for (const n of notes || []) ul.appendChild(el("li", "", n));
    ul.hidden = !(notes && notes.length);
  }

  // 折线图（SVG，宽度跟容器走）：series = [{name, color, points:[{x, y, tip}], mark:{x, label}}]
  function lineChart(box, o) {
    box.textContent = "";
    const W = Math.max(260, box.clientWidth || 600), H = o.height || 210;
    const m = { l: 44, r: 12, t: 24, b: 34 };
    const s = svgEl("svg", { viewBox: `0 0 ${W} ${H}`, width: W, height: H, role: "img", "aria-label": o.aria || "" }, box);
    const X = (x) => m.l + ((x - o.xMin) / (o.xMax - o.xMin)) * (W - m.l - m.r);
    const Y = (y) => H - m.b - ((y - o.yMin) / (o.yMax - o.yMin || 1)) * (H - m.t - m.b);
    const col = { line: cssVar("--line"), muted: cssVar("--muted"), text: cssVar("--text") };
    // 网格 + 刻度（克制：只画水平网格）
    for (const yv of o.yTicks) {
      svgEl("line", { x1: m.l, x2: W - m.r, y1: Y(yv), y2: Y(yv), stroke: col.line, "stroke-width": 1 }, s);
      const t = svgEl("text", { x: m.l - 6, y: Y(yv) + 4, "text-anchor": "end", fill: col.muted, "font-size": 11 }, s);
      t.textContent = o.yFmt(yv);
    }
    for (const xv of o.xTicks) {
      const t = svgEl("text", { x: X(xv), y: H - m.b + 15, "text-anchor": "middle", fill: col.muted, "font-size": 11 }, s);
      t.textContent = o.xFmt(xv);
    }
    if (o.xLabel) {
      const t = svgEl("text", { x: W - m.r, y: H - 4, "text-anchor": "end", fill: col.muted, "font-size": 11 }, s);
      t.textContent = o.xLabel;
    }
    if (o.yLabel) {
      const t = svgEl("text", { x: 4, y: 11, "text-anchor": "start", fill: col.muted, "font-size": 11 }, s);
      t.textContent = o.yLabel;
    }
    for (const v of o.vlines || []) {
      svgEl("line", { x1: X(v.x), x2: X(v.x), y1: m.t, y2: H - m.b, stroke: v.color || col.muted, "stroke-width": 1, "stroke-dasharray": v.solid ? "" : "4 3" }, s);
      if (v.label) {
        const t = svgEl("text", { x: X(v.x) + (v.anchor === "end" ? -4 : 4), y: m.t + 10 + (v.dy || 0), "text-anchor": v.anchor || "start", fill: v.color || col.muted, "font-size": 11 }, s);
        t.textContent = v.label;
      }
    }
    for (const se of o.series) {
      if (!se.points.length) continue;
      const d = se.points.map((p, i) => `${i ? "L" : "M"}${X(p.x).toFixed(1)},${Y(p.y).toFixed(1)}`).join("");
      svgEl("path", { d, fill: "none", stroke: se.color, "stroke-width": 2, "stroke-linejoin": "round" }, s);
      if (se.markers !== false) {
        for (const p of se.points) {
          const c = svgEl("circle", { cx: X(p.x), cy: Y(p.y), r: 4, fill: se.color, stroke: cssVar("--panel"), "stroke-width": 2 }, s);
          // 命中范围比点大
          const hit = svgEl("circle", { cx: X(p.x), cy: Y(p.y), r: 11, fill: "transparent" }, s);
          if (p.tip) { svgTitle(c, p.tip); svgTitle(hit, p.tip); }
        }
      } else {
        for (const p of se.points) {
          const hit = svgEl("rect", { x: X(p.x) - 5, y: m.t, width: 10, height: H - m.t - m.b, fill: "transparent" }, s);
          if (p.tip) svgTitle(hit, p.tip);
        }
      }
    }
    if (o.series.length > 1 || o.legend) {
      const lg = el("div", "tn-legend tn-chart-legend");
      for (const se of o.series) {
        const sp = el("span");
        const sw = el("i", "sw");
        sw.style.background = se.color;
        sp.appendChild(sw);
        sp.appendChild(document.createTextNode(se.name));
        lg.appendChild(sp);
      }
      box.appendChild(lg);
    }
  }

  function seriesColors() {
    return [cssVar("--accent"), cssVar("--accent-2"), cssVar("--good")];
  }

  function renderBlocks(res) {
    renderBalance(res.balance_map, res);
    renderBrake(res.brake_analysis, res);
    renderBody(res.body);
  }

  // 窗口尺寸变了：SVG 图按新宽度重画
  let blockRaf = 0;
  function redrawCharts() {
    cancelAnimationFrame(blockRaf);
    blockRaf = requestAnimationFrame(() => {
      const r = state.result;
      if (!r) return;
      if (r.brake_analysis && r.brake_analysis.available) drawPedal(r.brake_analysis);
      if (r.body && r.body.available) drawDamper(r.body);
    });
  }

  // ================================================================ 分析块 ①：转向平衡分布

  // B 的颜色：蓝 = 推头（B > 0），红 = 甩尾（B < 0），中间是面板灰；level 0~1 = |B| / color_full_b
  function heatColor(b, level) {
    const pole = b >= 0 ? "var(--accent)" : "var(--bad)";
    const p = Math.round(85 * Math.min(1, Math.max(0, level)));       // |B| ≈ 0 → 灰（中性），越偏越深
    return `color-mix(in srgb, ${pole} ${p}%, var(--panel-2))`;
  }

  function renderBalance(bm, res) {
    const sec = $("tn-bal");
    if (!bm || (!bm.available && !res.usable)) { sec.hidden = true; return; }
    sec.hidden = false;
    const notes = bm.available ? [...(bm.notes || [])] : [bm.why];
    if (bm.align_note) notes.push(bm.align_note);
    fillNotes("tn-bal-notes", notes);
    $("tn-bal-method").textContent = bm.method || "";
    const bandsBox = $("tn-bal-bands");
    const heat = $("tn-bal-heat");
    const legend = $("tn-bal-legend");
    bandsBox.textContent = "";
    heat.textContent = "";
    legend.textContent = "";
    for (const h of sec.querySelectorAll("h3")) h.hidden = !bm.available;
    if (!bm.available) return;

    // ---- 车速分档：从中线往两边长的条（右 = 推头，左 = 甩尾）
    const full = Math.max(0.2, bm.color_full_b || 0.5);
    const axis = el("div", "tn-band tn-band-axis");
    axis.appendChild(el("span", "nm", ""));
    const ax = el("div", "ax");
    ax.appendChild(el("span", "", `← 甩尾 ${signed(-full)}`));
    ax.appendChild(el("span", "", "0"));
    ax.appendChild(el("span", "", `${signed(full)} 推头 →`));
    axis.appendChild(ax);
    axis.appendChild(el("span", "val", ""));
    bandsBox.appendChild(axis);
    for (const b of bm.bands) {
      const row = el("div", "tn-band" + (b.ok ? "" : " na"));
      const nm = el("span", "nm");
      nm.appendChild(el("b", "", b.name));
      nm.appendChild(el("span", "muted", ` ${b.range}`));
      row.appendChild(nm);
      const track = el("div", "track");
      track.appendChild(el("i", "mid"));
      const val = el("span", "val");
      if (b.ok && b.b !== null) {
        const bar = el("i", "bar " + (b.b >= 0 ? "pos" : "neg"));
        const w = Math.min(1, Math.abs(b.b) / full) * 50;
        bar.style.width = `${Math.max(w, 0.6)}%`;
        bar.style.background = b.b >= 0 ? "var(--accent)" : "var(--bad)";
        track.appendChild(bar);
        val.textContent = `${signed(b.b)} ${TEND[b.tendency] || ""}`;
        row.title = `${b.name}（${b.range}）：平均 B ${signed(b.b)}；明显推头 ${pct(b.us_share)}、明显甩尾 ${pct(b.os_share)} 的时间；共 ${num(b.time_s, 1)} s 转弯数据`;
      } else {
        track.appendChild(el("span", "na-txt", `样本不足（${num(b.time_s, 1)} s < ${num(bm.min_band_s, 1)} s）`));
        val.textContent = "--";
      }
      row.appendChild(track);
      row.appendChild(val);
      bandsBox.appendChild(row);
      if (b.ok) {
        const sub = el("div", "tn-band-sub muted", `明显推头 ${pct(b.us_share)} · 明显甩尾 ${pct(b.os_share)} 的时间 · ${num(b.time_s, 1)} s`);
        bandsBox.appendChild(sub);
      }
    }

    // ---- 弯 × 阶段热力表
    const thead = el("thead");
    const hr = el("tr");
    hr.appendChild(el("th", "", "弯"));
    for (const p of bm.phases) hr.appendChild(el("th", "", p.label));
    thead.appendChild(hr);
    heat.appendChild(thead);
    const tb = el("tbody");
    const dirCn = { left: "左", right: "右" };
    for (const row of bm.rows) {
      const tr = el("tr");
      const th = el("th");
      th.appendChild(el("b", "", row.label));
      if (row.label !== row.code) th.appendChild(el("span", "muted", ` ${row.code}`));
      if (row.direction) th.appendChild(el("span", "muted", ` ${dirCn[row.direction] || ""}`));
      tr.appendChild(th);
      for (const c of row.cells) {
        const td = el("td");
        if (c.ok && c.b !== null) {
          td.style.background = heatColor(c.b, c.level);
          if (c.level >= 0.6) td.classList.add("dark");
          td.textContent = signed(c.b);
          td.title = `${row.label} ${c.phase_label}：B ${signed(c.b)}（${TEND[c.tendency] || ""}），${num(c.time_s, 1)} s 数据` +
            (c.speed_kmh ? `，平均 ${Math.round(c.speed_kmh)} km/h` : "");
        } else {
          td.className = "na";
          td.textContent = "—";
          td.title = `${row.label} ${c.phase_label}：样本太少（${num(c.time_s, 2)} s < ${num(bm.min_cell_s, 2)} s），不评价`;
        }
        tr.appendChild(td);
      }
      tb.appendChild(tr);
    }
    heat.appendChild(tb);
    heat.closest(".tn-scroll").hidden = !bm.rows.length;
    sec.querySelectorAll("h3")[1].hidden = !bm.rows.length;
    if (!bm.rows.length) {
      legend.textContent = "没有弯道数据（没识别出弯道），只有车速分档。";
      return;
    }
    const lg = el("div", "tn-heat-scale");
    lg.appendChild(el("span", "", `甩尾 ${signed(-full)}`));
    const ramp = el("i", "ramp");
    ramp.style.background = `linear-gradient(90deg, ${heatColor(-1, 1)}, ${heatColor(-1, 0)} 45%, ${heatColor(1, 0)} 55%, ${heatColor(1, 1)})`;
    lg.appendChild(ramp);
    lg.appendChild(el("span", "", `${signed(full)} 推头`));
    const na = el("span", "na-key");
    na.appendChild(el("i", "na"));
    na.appendChild(document.createTextNode("样本太少"));
    lg.appendChild(na);
    legend.appendChild(lg);
  }

  // ================================================================ 分析块 ②：刹车分析

  const BIAS = { front: "前轴先饱和", rear: "后轴先饱和", balanced: "前后差不多" };

  function renderBrake(ba, res) {
    const sec = $("tn-brk");
    if (!ba || (!ba.available && !res.usable)) { sec.hidden = true; return; }
    sec.hidden = false;
    fillNotes("tn-brk-notes", ba.available ? ba.notes : [ba.why]);
    const tbl = $("tn-brk-zones");
    tbl.textContent = "";
    const h3 = sec.querySelectorAll("h3");
    let zones = (ba.available && ba.zones) || [];
    if (res.mode === "multi") {     // 多圈：按位置排，同一个弯的几圈挨在一起
      zones = zones.slice().sort((a, b) => ((a.location && a.location.dist_m) || 0) - ((b.location && b.location.dist_m) || 0));
    }
    h3[0].hidden = !zones.length;
    tbl.closest(".tn-scroll").hidden = !zones.length;
    if (zones.length) {
      const multi = res.mode === "multi";
      const mark = num(ba.slip_mark, 2);
      const thead = el("thead");
      const hr = el("tr");
      for (const [t, c] of [[multi ? "圈" : null, ""], ["位置", ""], ["车速", "n spd"], ["前轴", "n"], ["后轴", "n"], ["判断", ""]]) {
        if (t) hr.appendChild(el("th", c, t));
      }
      thead.appendChild(hr);
      tbl.appendChild(thead);
      const tb = el("tbody");
      const FOLD = 10;                // 超过这么多行：没偏向的行先收起来
      let folded = 0;
      zones.forEach((z, zi) => {
        const tr = el("tr", z.bias === "front" || z.bias === "rear" ? "hot" : "");
        if (zones.length > FOLD + 2 && zi >= FOLD && !tr.classList.contains("hot")) { tr.hidden = true; tr.dataset.fold = "1"; folded++; }
        if (multi) {
          const lc = el("td", "muted", z.lap_id ? `#${z.lap_id}` : z.lap_label || "");
          lc.title = z.lap_label || "";
          tr.appendChild(lc);
        }
        const loc = el("td", "loc");
        const lapId = z.lap_id || (res.lap && res.lap.id);
        const name = z.location ? z.location.label.split(" · ")[0] : "";
        const d0 = z.location ? z.location.dist_m : null;
        if (lapId && d0 !== null) {
          const a = el("a", "", `${name} ${Math.round(d0)} m`);
          a.href = cmpHref(lapId, d0);
          a.title = "在对比页查看";
          loc.appendChild(a);
        } else loc.textContent = name;
        tr.appendChild(loc);
        tr.appendChild(el("td", "n spd", `${Math.round(z.speed_kmh)}`));
        tr.appendChild(el("td", "n" + (z.bias === "front" ? " em" : ""), num(z.front_peak)));
        tr.appendChild(el("td", "n" + (z.bias === "rear" ? " em" : ""), num(z.rear_peak)));
        const first = z.first === "front" ? "前轴" : z.first === "rear" ? "后轴" : null;
        const judge = el("td", "judge", z.bias ? BIAS[z.bias] : "刹得不够重");
        if (first) judge.appendChild(el("span", "muted sub", `${first}先到 ${mark}`));
        tr.title = `${z.location ? z.location.label : ""}：|滑移率| 峰值（p95）前轴 ${num(z.front_peak)}、后轴 ${num(z.rear_peak)}；` +
          (first ? `${first}先到 ${mark}` + (z.lead_s !== null && z.lead_s !== undefined ? `，早 ${num(z.lead_s)} s` : "，另一轴一直没到") : `两轴都没到 ${mark}`);
        tr.appendChild(judge);
        tb.appendChild(tr);
      });
      if (folded) {
        const tr = el("tr", "more");
        const td = el("td");
        td.colSpan = multi ? 6 : 5;
        const btn = el("button", "", `显示全部 ${zones.length} 段（还有 ${folded} 段前后差不多的）`);
        btn.type = "button";
        btn.addEventListener("click", () => { tb.querySelectorAll("tr[data-fold]").forEach((r) => { r.hidden = false; }); tr.remove(); });
        td.appendChild(btn);
        tr.appendChild(td);
        tb.appendChild(tr);
      }
      tbl.appendChild(tb);
    }
    const bands = (ba.available && ba.pedal && ba.pedal.bands) || [];
    const has = bands.some((b) => b.bins && b.bins.length);
    h3[1].hidden = !ba.available;
    $("tn-brk-pedal").hidden = !ba.available;
    if (!ba.available) return;
    if (!has) {
      $("tn-brk-pedal").textContent = "没有直线刹车的踏板数据。";
      return;
    }
    drawPedal(ba);
  }

  function drawPedal(ba) {
    const box = $("tn-brk-pedal");
    const cols = seriesColors();
    const bands = ba.pedal.bands.filter((b) => b.bins && b.bins.length);
    const ymax = Math.max(0.5, ...bands.flatMap((b) => b.bins.map((x) => x.decel_g)));
    const top = Math.ceil(ymax * 1.15 * 4) / 4;
    const yTicks = [];
    for (let y = 0; y <= top + 1e-9; y += top > 1.5 ? 0.5 : 0.25) yTicks.push(y);
    const vlines = [];
    const series = bands.map((b, i) => {
      if (b.sat_pedal !== null && b.sat_pedal !== undefined && b.status !== "linear") {
        vlines.push({ x: b.sat_pedal, color: cols[i], label: `${b.name}饱和 ${pct(b.sat_pedal)}`, anchor: "end", dy: i * 14 });
      }
      const status = { saturated: "（饱和偏早）", late: "", linear: "", insufficient: "（数据不足）" }[b.status] || "";
      return {
        name: `${b.name} ${b.range}${status}`, color: cols[i % cols.length],
        points: b.bins.map((x) => ({ x: x.pedal, y: x.decel_g, tip: `${b.name}（${b.range}）踏板 ${pct(x.pedal)}：减速 ${num(x.decel_g)} G（${num(x.time_s, 2)} s 数据）` })),
      };
    });
    lineChart(box, {
      series, xMin: 0, xMax: 1, yMin: 0, yMax: top, xTicks: [0, 0.2, 0.4, 0.6, 0.8, 1], yTicks,
      xFmt: (x) => `${Math.round(x * 100)}%`, yFmt: (y) => y.toFixed(2), xLabel: "刹车踏板", yLabel: "减速 G",
      vlines, legend: true, aria: "刹车踏板位置和减速度的关系",
    });
    const coast = ba.coast_fitted ? "已扣掉风阻和发动机制动（按直线滑行段拟合）。" : "直线滑行数据不够，没扣风阻和发动机制动（高速时减速度会偏大）。";
    box.appendChild(el("p", "muted tn-small", coast + " 每个点 = 一个 10% 踏板区间的减速度中位数；曲线变平的地方就是再踩也不会刹得更快。"));
  }

  // ================================================================ 分析块 ③：车身与悬挂

  function renderBody(body) {
    const sec = $("tn-body");
    if (!body) { sec.hidden = true; return; }
    sec.hidden = false;
    const notes = body.available ? body.notes : [body.why];
    fillNotes("tn-body-notes", notes);
    const stats = $("tn-body-stats");
    const travel = $("tn-body-travel");
    const classes = $("tn-body-classes");
    stats.textContent = "";
    travel.textContent = "";
    classes.textContent = "";
    const h3 = sec.querySelectorAll("h3");
    const caveats = $("tn-body-caveats");
    caveats.textContent = "";
    if (!body.available) {
      h3.forEach((h) => { h.hidden = true; });
      $("tn-body-damper").hidden = true;
      classes.closest(".tn-scroll").hidden = true;
      stats.hidden = true;
      return;
    }
    // ---- 梯度
    const r = body.roll, p = body.pitch, ref = body.attitude_ref || {};
    stats.hidden = false;
    if (r) {
      stats.appendChild(stat("侧倾 · 前轴", num(r.front_mm_per_g, 1), "mm/g"));
      stats.appendChild(stat("侧倾 · 后轴", num(r.rear_mm_per_g, 1), "mm/g"));
      stats.appendChild(stat("侧倾前轴占比", pct(r.front_share)));
    }
    if (p) {
      if (p.dive_mm_per_g !== null) stats.appendChild(stat("刹车点头", num(p.dive_mm_per_g, 1), "mm/g"));
      if (p.squat_mm_per_g !== null) stats.appendChild(stat("加速抬头", num(p.squat_mm_per_g, 1), "mm/g"));
    }
    if (ref.available) {
      if (ref.roll_deg_per_g !== null) stats.appendChild(statRef("参考：车身侧倾角", num(ref.roll_deg_per_g, 2), "°/g"));
      if (ref.dive_deg_per_g !== null) stats.appendChild(statRef("参考：刹车俯仰角", num(ref.dive_deg_per_g, 2), "°/g"));
    }
    if (!stats.children.length) stats.hidden = true;
    // ---- 行程分布
    const tr = body.travel;
    h3[0].hidden = !tr;
    if (tr) {
      const scale = el("div", "tn-trow tn-trow-axis");
      scale.appendChild(el("span", "nm", ""));
      const ax = el("div", "ax");
      for (const t of ["0 伸到最长", "0.5", "1 压到底"]) ax.appendChild(el("span", "", t));
      scale.appendChild(ax);
      scale.appendChild(el("span", "val", ""));
      travel.appendChild(scale);
      for (const w of ["fl", "fr", "rl", "rr"]) {
        const x = tr.wheels[w];
        const row = el("div", "tn-trow");
        row.appendChild(el("span", "nm", WHEEL[w]));
        const track = el("div", "track");
        const zone = el("i", "zone");
        zone.style.left = `${tr.near_bottom * 100}%`;
        zone.style.width = `${(1 - tr.near_bottom) * 100}%`;
        track.appendChild(zone);
        const band = el("i", "band" + (x.p99 >= tr.near_bottom ? " hot" : ""));
        band.style.left = `${x.p1 * 100}%`;
        band.style.width = `${Math.max(0.5, (x.p99 - x.p1) * 100)}%`;
        track.appendChild(band);
        const tick = el("i", "tick");
        tick.style.left = `${x.p50 * 100}%`;
        track.appendChild(tick);
        row.appendChild(track);
        row.appendChild(el("span", "val" + (x.p99 >= tr.near_bottom ? " warn" : ""), `${num(x.p1)} · ${num(x.p50)} · ${num(x.p99)}`));
        row.title = `${WHEEL[w]}：p1 ${num(x.p1)}、中位数 ${num(x.p50)}、p99 ${num(x.p99)}；≥ ${num(tr.near_bottom)} 的时间 ${pct(x.near_bottom)}`;
        travel.appendChild(row);
      }
    }
    // ---- 悬挂速度
    const d = body.damper;
    h3[1].hidden = !d;
    $("tn-body-damper").hidden = !d;
    classes.closest(".tn-scroll").hidden = !d;
    if (d) {
      drawDamper(body);
      const thead = el("thead");
      const hr = el("tr");
      ["", "回弹·快", "回弹·慢", "压缩·慢", "压缩·快", "|v| p95"]
        .forEach((t, i) => hr.appendChild(el("th", i ? "n" : "", t)));
      thead.appendChild(hr);
      classes.appendChild(thead);
      const tb = el("tbody");
      for (const w of ["fl", "fr", "rl", "rr"]) {
        const c = d.classes[w];
        const row = el("tr");
        row.appendChild(el("th", "", WHEEL[w]));
        for (const k of ["rebound_fast", "rebound_slow", "bump_slow", "bump_fast"]) row.appendChild(el("td", "n", pct(c[k])));
        row.appendChild(el("td", "n", `${Math.round(d.p95_abs_mm_s[w])}`));
        tb.appendChild(row);
      }
      classes.appendChild(tb);
    }
    const cv = [...(body.caveats || [])];
    if (ref.why) cv.push(ref.why);
    if (d && d.sign_flipped) cv.push("这台车 / 这个游戏的悬挂行程（米）正负号和归一化行程相反，已自动翻过来（变大 = 压缩）。");
    for (const c of cv) caveats.appendChild(el("li", "", c));
  }

  function statRef(k, v, small) {
    const d = stat(k, v, small);
    d.classList.add("ref");
    d.title = "车在世界坐标里的姿态角：含路面坡度 / 弯道倾斜，只作参考";
    return d;
  }

  function drawDamper(body) {
    const d = body.damper;
    const box = $("tn-body-damper");
    const e = d.edges_mm_s;
    const centers = e.slice(0, -1).map((x, i) => (x + e[i + 1]) / 2);
    const cols = seriesColors();
    const mx = Math.max(0.01, ...d.hist.front, ...d.hist.rear);
    const top = Math.ceil(mx * 100 / 5) * 5 / 100;
    const yTicks = [];
    for (let y = 0; y <= top + 1e-9; y += top > 0.2 ? 0.1 : 0.05) yTicks.push(y);
    const vmax = e[e.length - 1];
    const series = [["front", "前轴（左前 + 右前）"], ["rear", "后轴（左后 + 右后）"]].map(([k, name], i) => ({
      name, color: cols[i], markers: false,
      points: centers.map((x, j) => ({ x, y: d.hist[k][j], tip: `${name.slice(0, 2)} ${Math.round(e[j])}~${Math.round(e[j + 1])} mm/s：${(d.hist[k][j] * 100).toFixed(1)}% 的时间` })),
    }));
    lineChart(box, {
      series, xMin: -vmax, xMax: vmax, yMin: 0, yMax: top, xTicks: [-vmax, -vmax / 2, 0, vmax / 2, vmax], yTicks,
      xFmt: (x) => `${Math.round(x)}`, yFmt: (y) => `${Math.round(y * 100)}%`, xLabel: "mm/s", yLabel: "时间占比",
      vlines: [{ x: -d.split_mm_s, label: "← 回弹", anchor: "end" }, { x: 0, solid: true }, { x: d.split_mm_s, label: "压缩 →" }],
      legend: true, aria: "悬挂速度分布：负 = 回弹，正 = 压缩",
    });
    box.appendChild(el("p", "muted tn-small", `虚线 = ±${d.split_mm_s} mm/s 低速 / 高速分界（阻尼调校里的“慢 / 快”大致按这个分）。下表“快 / 慢”按这条线分，|v| p95 单位 mm/s。`));
  }

  // ================================================================ 启动

  async function init() {
    const cv = $("tn-map");
    cv.addEventListener("pointermove", showTip);
    cv.addEventListener("pointerleave", () => { $("tn-tip").hidden = true; });
    cv.addEventListener("click", (ev) => { const { m } = markerAt(ev); if (m) jumpToCard(m); });
    let raf = 0;
    new ResizeObserver(() => { cancelAnimationFrame(raf); raf = requestAnimationFrame(drawMap); }).observe($("tn-map-box"));
    let lastW = 0;
    new ResizeObserver((ents) => {
      const w = Math.round(ents[0].contentRect.width);
      if (Math.abs(w - lastW) > 4) { lastW = w; redrawCharts(); }
    }).observe($("tn-findings"));

    $("tn-form").addEventListener("submit", (ev) => {
      ev.preventDefault();
      run(currentSelection());
    });
    $("tn-lap").addEventListener("change", () => { syncAero(); updateMultiHint(); run(currentSelection()); });
    $("tn-multi").addEventListener("change", () => { updateMultiHint(); run(currentSelection()); });
    $("tn-multi-n").addEventListener("change", () => { if ($("tn-multi").checked) run(currentSelection()); });
    $("tn-abs").checked = lsGet(LS_ABS) === "1";
    $("tn-abs").addEventListener("change", () => {
      lsSet(LS_ABS, $("tn-abs").checked ? "1" : "0");
      if (state.result) run(currentSelection());
    });
    $("tn-aero").addEventListener("change", () => {
      const base = state.laps ? lapById(parseInt($("tn-lap").value, 10)) : null;
      const k = carKey(base);
      if (k) lsSet(LS_AERO + k, $("tn-aero").value || null);
      if (state.result) run(currentSelection());
    });
    $("tn-newlap-x").addEventListener("click", hideNewLap);
    F.onLapsChanged((d) => { onLapsChanged(d); });

    await loadLaps(false);
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
      syncAero();
      updateMultiHint();
    } else if (start.length) {
      $("tn-manual").value = start.join(",");
    }
    if (start.length) run(start);
  }

  init();
})();
