"""检查项⑤c：刹车压力（踏板 - 减速度饱和），以及按车速分档的工具函数。"""

from __future__ import annotations

import numpy as np

from .common import CHECK_NAME, ONE_AT_A_TIME, _Ctx, _finding, _hands_off, _pct
from .check_brakes import _abs_caveat, _near_kerb

# ================================================================ ⑤c 刹车压力（踏板 - 减速度饱和）

def _decel_sign(glong: np.ndarray, brk: np.ndarray) -> float:
    """g_long 的正负号：刹车时应该是减速。重刹采样的中位数为正就翻过来（不依赖游戏的正负号约定）。"""
    m = brk >= 0.3
    if m.sum() < 5:
        return -1.0
    return -1.0 if float(np.median(glong[m])) <= 0 else 1.0


def _coast_model(spd_ms: np.ndarray, decel: np.ndarray, mask: np.ndarray, min_n: int) -> tuple[float, float] | None:
    """滑行（不踩油门不踩刹车）时的减速度 ≈ a + b·v²（风阻 + 发动机制动 + 滚阻）。样本不够返回 None。"""
    if mask.sum() < max(10, min_n):
        return None
    v2 = spd_ms[mask] ** 2
    A = np.stack([np.ones_like(v2), v2], axis=1)
    try:
        (a, b), *_ = np.linalg.lstsq(A, decel[mask], rcond=None)
    except np.linalg.LinAlgError:
        return None
    return float(a), float(b)


def _speed_edges(speed: np.ndarray, quantiles, fixed) -> list[float]:
    """车速分档边界（km/h）：配置里写了固定边界就用固定的，否则按这些采样车速的分位数分，去掉重复（相差 < 5 km/h）。

    边界保留原始分位数值、不取整（显示文字由 _band_defs 格式化成整数）：取整会把正好落在分位数上的一大簇几乎恒定的车速
    （全油门长弯、顶着断油的高速弯）整块推到另一档，那一档的结论就跟着变了。
    """
    if fixed:
        return sorted(float(x) for x in fixed)
    if speed.size == 0:
        return []
    s = np.sort(np.asarray(speed, float))
    out: list[float] = []
    qs: list[float] = []
    for q in quantiles:
        e = float(np.quantile(s, q))
        if not out or e - out[-1] >= 5:
            out.append(e)
            qs.append(float(q))
    return _rebalance_edges(s, out, qs)


def _band_shares(s: np.ndarray, edges: list[float]) -> np.ndarray:
    """每档的采样占比（和 _band_mask 一样按 ≥ 下界、< 上界切）。s 已排序。"""
    cuts = np.concatenate([[0], np.searchsorted(s, edges, side="left"), [s.size]])
    return np.diff(cuts) / max(1, s.size)


def _rebalance_edges(s: np.ndarray, edges: list[float], qs: list[float]) -> list[float]:
    """保护：某一档的采样占比不到预期（分位数之差）的一半——通常是大量完全相同的车速正好骑在边界上——
    就把这一档两侧的边界挪到分位数附近最大的车速间隙中点（不劈开一簇车速）；挪完更均匀才采用。s 已排序。"""
    if not edges or s.size < 2:
        return edges
    expect = np.diff(np.concatenate([[0.0], qs, [1.0]]))
    if np.any(expect <= 0):
        return edges

    def worst(es: list[float]) -> tuple[float, int]:
        ratio = _band_shares(s, es) / expect
        return float(ratio.min()), int(ratio.argmin())

    edges = list(edges)
    for _ in range(len(edges)):
        score, band = worst(edges)
        if score >= 0.5:
            break
        best = None
        for j in (band - 1, band):                 # 这一档的下边界、上边界
            if not 0 <= j < len(edges):
                continue
            w = 0.5 * min(expect[j], expect[j + 1])   # 只在相邻两档各一半的范围里找，边界不会交叉
            i0 = int(np.floor(max(0.0, qs[j] - w) * (s.size - 1)))
            i1 = int(np.ceil(min(1.0, qs[j] + w) * (s.size - 1)))
            if i1 <= i0:
                continue
            gaps = np.diff(s[i0:i1 + 1])
            k = int(np.argmax(gaps))
            if gaps[k] <= 0:
                continue
            cand = edges[:j] + [float((s[i0 + k] + s[i0 + k + 1]) / 2)] + edges[j + 1:]
            sc = worst(cand)[0]
            if sc > score and (best is None or sc > best[0]):
                best = (sc, cand)
        if best is None:
            break
        edges = best[1]
    return edges


def _band_defs(edges: list[float]) -> list[dict]:
    names = {1: ["全部"], 2: ["低速", "高速"], 3: ["低速", "中速", "高速"]}.get(len(edges) + 1)
    out = []
    for i in range(len(edges) + 1):
        lo = edges[i - 1] if i > 0 else None
        hi = edges[i] if i < len(edges) else None
        rng = (f"< {hi:.0f} km/h" if lo is None else f"> {lo:.0f} km/h" if hi is None else f"{lo:.0f}–{hi:.0f} km/h")
        out.append({"name": names[i] if names else f"第 {i + 1} 档", "lo_kmh": lo, "hi_kmh": hi, "range": rng})
    return out


def _band_mask(speed: np.ndarray, band: dict) -> np.ndarray:
    m = np.ones(speed.size, bool)
    if band["lo_kmh"] is not None:
        m &= speed >= band["lo_kmh"]
    if band["hi_kmh"] is not None:
        m &= speed < band["hi_kmh"]
    return m


def _pedal_samples(ctx: _Ctx) -> dict | None:
    """刹车压力分析用的采样：直线（不转向、不踩油门、没压路肩）踩刹车时的 (踏板, 扣掉滑行减速后的减速度 G, 车速)。"""
    need = ("brake", "g_long", "speed", "steer", "throttle")
    if not ctx.dash or ctx.missing(need):
        return None
    r = ctx.r["brake_pressure"]
    brk, thr = ctx.ch("brake"), ctx.ch("throttle")
    steer, spd = np.abs(ctx.ch("steer")), np.abs(ctx.ch("speed"))
    decel = ctx.ch("g_long") * _decel_sign(ctx.ch("g_long"), brk)
    base = (steer < r["max_steer"]) & (thr <= r["max_throttle"]) & (spd * 3.6 > r["min_speed_kmh"]) \
        & ~_near_kerb(ctx, r["kerb_window_s"])
    hands, hand_txt = _hands_off(ctx, base & (brk >= r["min_brake"]))   # 手刹也会产生减速度，这些采样不算
    base &= hands
    coast = base & (brk <= r["coast_max_pedal"])
    model = _coast_model(spd, decel, coast, round(r["min_coast_s"] / ctx.dt))
    m = base & (brk >= r["min_brake"])
    net = decel - (model[0] + model[1] * spd ** 2 if model else 0.0)
    return {"dt": ctx.dt, "pedal": brk[m], "decel_g": net[m], "speed_kmh": spd[m] * 3.6, "coast": model,
            "coast_s": float(coast.sum() * ctx.dt), "hand_txt": hand_txt}


def _pedal_curves(sets: list[dict], r: dict) -> dict:
    """按车速分档算“踏板 → 减速度”曲线和饱和点（单圈 / 多圈合并共用）。"""
    sets = [s for s in sets if s and s["pedal"].size]
    if not sets:
        return {"bands": [], "edges_kmh": []}
    ped = np.concatenate([s["pedal"] for s in sets])
    dec = np.concatenate([s["decel_g"] for s in sets])
    spd = np.concatenate([s["speed_kmh"] for s in sets])
    w = np.concatenate([np.full(s["pedal"].size, s["dt"]) for s in sets])
    edges = _speed_edges(spd, r["speed_band_quantiles"], r["speed_band_edges_kmh"])
    bands = []
    for band in _band_defs(edges):
        bm = _band_mask(spd, band)
        k = np.floor(ped[bm] / r["pedal_bin"] + 1e-9).astype(int)
        bins = []
        for b in sorted(set(k.tolist())):
            q = k == b
            t_s = float(w[bm][q].sum())
            if t_s < r["min_bin_s"]:
                continue
            bins.append({"pedal": round(min(1.0, (b + 0.5) * r["pedal_bin"]), 3), "decel_g": float(np.median(dec[bm][q])),
                         "time_s": round(t_s, 2)})
        info = {**band, "bins": bins, "time_s": round(float(w[bm].sum()), 2), "sat_pedal": None, "sat_decel_g": None,
                "status": "insufficient", "max_pedal": max((x["pedal"] for x in bins), default=None)}
        span = (bins[-1]["pedal"] - bins[0]["pedal"]) if bins else 0.0
        if len(bins) >= r["min_bins"] and span >= r["min_span"] - 1e-9:
            top = max(x["decel_g"] for x in bins)
            s_idx = None
            for i, x in enumerate(bins):
                if top > 0 and all(y["decel_g"] >= r["sat_frac"] * top for y in bins[i:]):
                    s_idx = i
                    break
            above = len(bins) - 1 - s_idx if s_idx is not None else 0
            if s_idx is not None and above >= r["min_bins_above"] and bins[-1]["pedal"] >= r["full_pedal"]:
                info.update(sat_pedal=bins[s_idx]["pedal"], sat_decel_g=round(bins[s_idx]["decel_g"], 3),
                            status="saturated" if bins[s_idx]["pedal"] <= r["sat_warn_pedal"] else "late")
            else:
                info["status"] = "linear"
        bands.append(info)
    return {"bands": bands, "edges_kmh": edges}


def _check_brake_pressure(ctx: _Ctx) -> None:
    cid, name = "brake_pressure", CHECK_NAME["brake_pressure"]
    r = ctx.r["brake_pressure"]
    ps = _pedal_samples(ctx)
    if ps is None:
        ctx.check(cid, name, "skipped", "数据不足：需要刹车、纵向 G、车速、方向盘、油门。")
        return
    ctx.samples["brake_pedal"] = ps
    curves = _pedal_curves([ps], r)
    coast_txt = ("已扣掉风阻和发动机制动（按滑行段拟合 减速度 = a + b·车速²）" if ps["coast"] else
                 f"滑行数据不足 {r['min_coast_s']:.0f} s，没扣风阻和发动机制动（高速段的减速度会偏大）")
    evaluated = [b for b in curves["bands"] if b["status"] != "insufficient"]
    if not evaluated:
        ctx.check(cid, name, "skipped",
                  f"数据不足：直线刹车时踏板位置太集中（每个车速档至少要 {r['min_bins']} 个 {_pct(r['pedal_bin'])} 宽的踏板区间、"
                  f"每个区间 ≥ {r['min_bin_s']:.2f} s、跨度 ≥ {_pct(r['min_span'])}）——比如总是一脚踩到底，就看不出饱和点"
                  f"{ps['hand_txt']}。")
        return
    sat = [b for b in evaluated if b["status"] == "saturated"]
    parts = []
    for b in curves["bands"]:
        if b["status"] == "insufficient":
            parts.append(f"{b['name']}（{b['range']}）数据不足")
        elif b["sat_pedal"] is not None:
            parts.append(f"{b['name']}（{b['range']}）踩到 {_pct(b['sat_pedal'])} 后减速度不再增加（约 {b['sat_decel_g']:.2f} G）")
        else:
            parts.append(f"{b['name']}（{b['range']}）踩到 {_pct(b['max_pedal'])} 减速度还在增加")
    note = "；".join(parts) + f"（{coast_txt}）" + ps["hand_txt"]
    if not sat:
        ctx.check(cid, name, "ok", note + f"。没有在 {_pct(r['sat_warn_pedal'])} 踏板以内就饱和的。")
        return
    ctx.findings.append(_brake_pressure_finding(ctx, sat, curves, coast_txt))
    ctx.check(cid, name, "found", note + "。")


def _brake_pressure_finding(ctx: _Ctx, sat: list[dict], curves: dict, coast_txt: str) -> dict:
    r = ctx.r["brake_pressure"]
    worst = min(sat, key=lambda b: b["sat_pedal"])
    evidence = []
    for b in curves["bands"]:
        if not b["bins"]:
            continue
        pts = "、".join(f"{_pct(x['pedal'])}→{x['decel_g']:.2f} G" for x in b["bins"])
        evidence.append(f"{b['name']}（{b['range']}，直线刹车 {b['time_s']:.1f} s）：踏板→减速度 {pts}")
        if b["sat_pedal"] is not None:
            evidence.append(f"　{b['name']}：踩到约 {_pct(b['sat_pedal'])} 时减速度已达到这档最大值的 ≥ {_pct(r['sat_frac'])}，"
                            "再往下踩减速度基本不变")
    evidence.append(coast_txt)
    evidence.append(f"判定阈值：直线（|方向盘| < {_pct(r['max_steer'])}、油门 ≤ {_pct(r['max_throttle'])}、车速 > {r['min_speed_kmh']:.0f} km/h、"
                    f"没压路肩、没拉手刹）刹车时，按 {_pct(r['pedal_bin'])} 踏板分区间取减速度中位数（每区间 ≥ {r['min_bin_s']:.2f} s）；"
                    f"从某个踏板位置起后面所有区间都 ≥ 最大值的 {_pct(r['sat_frac'])}、后面还有 ≥ {r['min_bins_above']} 个区间、"
                    f"最深踩到 ≥ {_pct(r['full_pedal'])}，这个位置 ≤ {_pct(r['sat_warn_pedal'])} 才报")
    if ctx.abs_on is True:
        sev = "info"
        title = f"刹车踩到约 {_pct(worst['sat_pedal'])} ABS 就开始限制减速度"
        summary = (f"{worst['name']}段踩到约 {_pct(worst['sat_pedal'])} 后减速度就不再增加（约 {worst['sat_decel_g']:.2f} G）。"
                   "你开着 ABS，多半是 ABS 在这里开始介入：后面那截踏板只是让 ABS 更忙，不会刹得更快。")
        tips = ["想要更细的刹车控制：刹车压力调小一点，让踩到 90~100% 时才接近极限",
                "不想动调校的话：重刹时踩到这个位置附近就够了，不必一脚踩死"]
    else:
        sev = "warn"
        title = f"刹车压力偏大（踩到约 {_pct(worst['sat_pedal'])} 减速度就到头）"
        summary = (f"{worst['name']}段踩到约 {_pct(worst['sat_pedal'])} 后减速度就不再增加（约 {worst['sat_decel_g']:.2f} G）："
                   "后面那截踏板行程是浪费的，而且再往下踩只会更容易抱死。")
        tips = ["刹车压力调小一点（让踩到 90~100% 时才到极限，刹车控制更细）",
                "如果同时有“刹车抱死”，这条更值得先改"]
    bands = [b["name"] for b in sat]
    caveat = ("减速度受车速和下压力影响很大（高速时下压力大、减速度也大），所以按车速分档分别看；"
              "手柄扳机和方向盘踏板的行程感觉不一样，以实际踩到的百分比为准。" +
              (" " + _abs_caveat(ctx.abs_on, "饱和点") if ctx.abs_on is None else "") + " " + ONE_AT_A_TIME)
    basis = {"channels": ["brake（踏板 0~1）", "g_long（纵向 G，自动判断正负号）", "speed", "steer", "throttle", "rumble_*"],
             "rule": (f"只用直线刹车的采样，按车速分档（默认按本圈刹车时车速的中位数分成低速 / 高速，可在 tuning_rules.toml 改成固定边界），"
                      f"每档把踏板按 {_pct(r['pedal_bin'])} 分区间，取扣掉滑行减速后的减速度中位数，找减速度不再增加的踏板位置。"),
             "principle": ("轮胎能提供的最大减速度是有限的：踏板踩到某个位置时轮胎已经到了纵向极限，再踩只会让轮子更接近抱死，"
                           "减速度不会再增加。这个位置太浅说明刹车压力设得太大，可用的踏板行程很短、很难精细控制。")}
    first_zone = next((z["location"] for z in ctx.samples.get("brake_zones", []) if z["location"]), None)
    locs = [first_zone | {"phase": None, "phase_label": "全圈", "corner": None,
                          "label": "全圈直线刹车段（统计）"}] if first_zone else []
    return _finding("brake_pressure_high", "brakes", sev, title, summary, evidence, locs, basis, tips,
                    "medium" if len(bands) >= 2 or worst["sat_pedal"] <= r["sat_warn_pedal"] - 0.2 else "low",
                    caveat, data={"sat_pedal": worst["sat_pedal"], "bands": bands, "abs_on": ctx.abs_on})
