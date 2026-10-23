"""检查项⑤：刹车抱死 + ⑤b 刹车平衡（直线重刹时前后轴滑移）。"""

from __future__ import annotations

import numpy as np

from .common import (
    AXLE_CN,
    AXLE_WHEELS,
    CHECK_NAME,
    ONE_AT_A_TIME,
    WHEELS,
    WHEEL_CN,
    _Ctx,
    _confidence,
    _corner_key,
    _finding,
    _hands_off,
    _lap_label,
    _lower,
    _pct,
    _runs,
    _wheel_code,
)

# ================================================================ ⑤ 刹车抱死

def _check_brakes(ctx: _Ctx) -> None:
    name = "刹车抱死"
    need = [f"slip_ratio_{w}" for w in WHEELS] + ["brake"]
    if not ctx.dash or ctx.missing(need):
        ctx.check("brakes", name, "skipped", "数据不足：需要滑移率和刹车。")
        return
    r = ctx.r["brakes"]
    g = ctx.r["general"]
    brk = ctx.ch("brake")
    steer = np.abs(ctx.ch("steer"))
    braking = (brk >= r["min_brake"]) & ctx.speed_ok
    hands, hand_txt = _hands_off(ctx, braking)    # 手刹拖住的后轮不是刹车平衡的问题
    braking &= hands
    sr = {w: np.abs(ctx.ch(f"slip_ratio_{w}")) for w in WHEELS}
    lock = {w: braking & (sr[w] > r["slip_limit"]) for w in WHEELS}
    found = []
    for axle, other in (("front", "rear"), ("rear", "front")):
        aw, ow = AXLE_WHEELS[axle], AXLE_WHEELS[other]
        m = lock[aw[0]] | lock[aw[1]]
        events = []
        for i0, i1 in _runs(m, ctx.t, g["merge_gap_s"], ctx.dt):
            dur = ctx.dur(i0, i1)
            if dur < r["min_duration_s"]:
                continue
            sl = slice(i0, i1 + 1)
            q = m[sl]
            wheels = [w for w in aw if lock[w][sl].any()]
            first = {}
            for ax2 in (axle, other):
                hits = np.flatnonzero((lock[AXLE_WHEELS[ax2][0]] | lock[AXLE_WHEELS[ax2][1]])[max(0, i0 - 30): i1 + 1])
                first[ax2] = int(hits[0]) if hits.size else None
            events.append({"i0": i0, "i1": i1, "dur": dur, "wheels": wheels,
                           "mean": float(np.mean([sr[w][sl][q].mean() for w in aw])),
                           "peak": max(float(sr[w][sl].max()) for w in aw),
                           "other": float(np.mean([sr[w][sl][q].mean() for w in ow])),
                           "other_locked": bool(any(lock[w][sl].any() for w in ow)),
                           "brk": float(brk[sl].mean()), "steer": float(steer[sl].mean()),
                           "first": axle if first[other] is None or (first[axle] is not None and first[axle] <= first[other]) else other})
        if not events:
            continue
        found.append(f"{WHEEL_CN[axle]}抱死 {len(events)} 次")
        ctx.findings.append(_lock_finding(ctx, axle, other, events))
    mx = max(float(sr[w][braking].max()) if braking.any() else 0.0 for w in WHEELS)
    if found:
        ctx.check("brakes", name, "found", f"刹车（≥ {_pct(r['min_brake'])}）时{'，'.join(found)}；四轮 |滑移率| 最大 {mx:.2f}"
                                           f"（阈值 {r['slip_limit']:.2f}）{hand_txt}。")
    else:
        ctx.check("brakes", name, "ok", f"刹车（≥ {_pct(r['min_brake'])}）时四轮 |滑移率| 最大 {mx:.2f}，没超过 {r['slip_limit']:.2f}"
                                        f"{hand_txt}。")


def _lock_finding(ctx: _Ctx, axle: str, other: str, events: list[dict]) -> dict:
    r = ctx.r["brakes"]
    g = ctx.r["general"]
    axle_cn, other_cn = WHEEL_CN[axle], WHEEL_CN[other]
    total = sum(e["dur"] for e in events)
    sev = "alert" if total >= r["alert_total_s"] or len(events) >= r["alert_events"] else "warn"
    locs, evidence = [], []
    for e in events:
        loc = ctx.loc(e["i0"], e["i1"], wheel=_wheel_code(e["wheels"]))
        locs.append(loc)
        evidence.append(f"{loc['label']}（持续 {e['dur']:.2f} s）：刹车 {_pct(e['brk'])}，方向盘 {_pct(e['steer'])}；"
                        f"{axle_cn} |滑移率| 平均 {e['mean']:.2f}、最大 {e['peak']:.2f}；同一时间{other_cn}平均 {e['other']:.2f}"
                        + ("（也抱死了）" if e["other_locked"] else "（没抱死）"))
    n_first = sum(1 for e in events if e["first"] == axle)
    evidence.append(f"{len(events)} 次里有 {n_first} 次是{axle_cn}先抱死")
    evidence.append(f"判定阈值：刹车 ≥ {_pct(r['min_brake'])} 时 |滑移率| > {r['slip_limit']:.2f}，连续 ≥ {r['min_duration_s']:.2f} s。"
                    f"本圈 {len(events)} 次、累计 {total:.2f} s（累计 ≥ {r['alert_total_s']:.1f} s 或 ≥ {r['alert_events']} 次记为严重）")
    corners = sorted({l["corner"] for l in locs if l["corner"]}, key=_corner_key)
    if axle == "front":
        tips = ["刹车平衡往后挪 1~2%（前轮刹车力少一点）", "刹车压力调小一点（踩到底也不那么容易抱死）",
                "先排除驾驶：别一脚踩死，刹车时少带方向（又刹又转时前轮更容易抱死）"]
        principle_extra = "前轮抱死时车不听方向，直接往前冲。"
    else:
        tips = ["刹车平衡往前挪 1~2%（后轮刹车力少一点）", "刹车压力调小一点",
                "先排除驾驶：降挡别太早太快（后驱车发动机制动会让后轮拖滑）"]
        principle_extra = "后轮抱死时车尾失去侧向抓地，很容易甩尾。"
    basis = {"channels": ["slip_ratio_*（归一化滑移率）", "brake", "steer", "speed"],
             "rule": (f"刹车 ≥ {_pct(r['min_brake'])}、车速 ≥ {g['min_speed_ms']:.0f} m/s 时，{axle_cn}任一轮 |滑移率| > {r['slip_limit']:.2f} "
                      f"连续 ≥ {r['min_duration_s']:.2f} s 记一次；同时对比另一根轴的滑移率，看哪根轴先抱死 / 更容易抱死。"),
             "principle": ("刹车时滑移率衡量轮子比车速转得慢多少，归一化后 |x| > 1 表示轮胎过了纵向抓地极限，轮子接近不转（抱死）。"
                           + principle_extra + "某一根轴总是先抱死，说明刹车力分配偏向这根轴（刹车平衡）或总刹车力太大。"
                           "用绝对值 + 刹车踏板区分抱死和空转，不依赖正负号。")}
    conf = _confidence(ctx.r, total, r["min_duration_s"], float(np.mean([e["mean"] for e in events])) / r["slip_limit"] - 1.0)
    return _finding(f"brake_lock_{axle}", "brakes", sev, f"{axle_cn}刹车抱死" + (f"（{'、'.join(ctx.cn(c) for c in corners)}）" if corners else ""),
                    f"刹车时{axle_cn}抱死 {len(events)} 次、累计 {total:.2f} s，{other_cn}基本正常：刹车力偏向{axle_cn}或太大。",
                    evidence, locs, basis, tips, conf, _lock_caveat(ctx.abs_on) + ONE_AT_A_TIME,
                    data={"axle": axle, "events": len(events), "total_s": round(total, 3), "first_count": n_first,
                          "abs_on": ctx.abs_on})


def _lock_caveat(abs_on: bool | None) -> str:
    if abs_on is True:
        return ("你勾选了“我开了 ABS”：开着 ABS 还出现 |滑移率| > 1，说明 ABS 介入之前轮子已经超过极限（ABS 只能压住一部分），"
                "刹车压力 / 刹车平衡偏向这根轴的可能性更大。")
    if abs_on is False:
        return "没开 ABS：这里的抱死就是真的抱死，靠刹车压力、刹车平衡和踩法来解决。"
    return "开着 ABS 时一般不会抱死；Data Out 看不到 ABS 是否开启，开了的话请在上面勾选“我开了 ABS”。"


# ================================================================ ⑤b 刹车平衡（直线重刹时前后轴滑移）

def _near_kerb(ctx: _Ctx, window_s: float) -> np.ndarray:
    """任一轮压路肩（rumble = 1）前后 window_s 秒内的采样 = True。没有 rumble 通道时全是 False。"""
    on = np.zeros(ctx.n, bool)
    for w in WHEELS:
        a = ctx.ch(f"rumble_{w}")
        if a is not None:
            on |= a > 0.5
    if not on.any():
        return on
    k = max(1, round(window_s / ctx.dt))
    return np.convolve(on.astype(float), np.ones(2 * k + 1), mode="same") > 0.5


def _abs_caveat(abs_on: bool | None, what: str) -> str:
    if abs_on is True:
        return f"你勾选了“我开了 ABS”：ABS 会把前后轴的滑移都压在阈值附近，{what}的区分度会下降，置信度已降一级。"
    if abs_on is None:
        return f"开着 ABS 时{what}的区分度会下降；Data Out 看不到 ABS 状态，开了的话请在上面勾选“我开了 ABS”。"
    return ""


def _check_brake_balance(ctx: _Ctx) -> None:
    cid, name = "brake_balance", CHECK_NAME["brake_balance"]
    need = [f"slip_ratio_{w}" for w in WHEELS] + ["brake", "steer", "speed"]
    if not ctx.dash or ctx.missing(need):
        ctx.check(cid, name, "skipped", "数据不足：需要滑移率、刹车、方向盘、车速。")
        return
    r = ctx.r["brake_balance"]
    g = ctx.r["general"]
    brk, steer, spd = ctx.ch("brake"), np.abs(ctx.ch("steer")), np.abs(ctx.ch("speed")) * 3.6
    fsr = (np.abs(ctx.ch("slip_ratio_fl")) + np.abs(ctx.ch("slip_ratio_fr"))) / 2
    rsr = (np.abs(ctx.ch("slip_ratio_rl")) + np.abs(ctx.ch("slip_ratio_rr"))) / 2
    kerb = _near_kerb(ctx, r["kerb_window_s"])
    m = (brk >= r["min_brake"]) & (steer < r["max_steer"]) & (spd > r["min_speed_kmh"]) & ~kerb
    hands, hand_txt = _hands_off(ctx, m)          # 拉手刹时后轮被手刹拖住，前后轴对比就不是刹车平衡了
    m &= hands

    def first_mark(a: np.ndarray) -> int | None:
        hit = np.flatnonzero((a[:-1] >= r["slip_mark"]) & (a[1:] >= r["slip_mark"])) if a.size > 1 else np.array([])
        return int(hit[0]) if hit.size else None

    zones = []
    for i0, i1 in _runs(m, ctx.t, g["merge_gap_s"], ctx.dt):
        dur = ctx.dur(i0, i1)
        if dur < r["min_zone_s"]:
            continue
        sl = slice(i0, i1 + 1)
        pf, pr = float(np.percentile(fsr[sl], r["peak_pct"])), float(np.percentile(rsr[sl], r["peak_pct"]))
        jf, jr = first_mark(fsr[sl]), first_mark(rsr[sl])
        tf = None if jf is None else float(ctx.t[i0 + jf] - ctx.t[i0])
        tr = None if jr is None else float(ctx.t[i0 + jr] - ctx.t[i0])
        first = lead = None
        if tf is not None and (tr is None or tr - tf >= r["mark_lead_s"]):
            first, lead = "front", (None if tr is None else tr - tf)
        elif tr is not None and (tf is None or tf - tr >= r["mark_lead_s"]):
            first, lead = "rear", (None if tf is None else tf - tr)
        if max(pf, pr) < r["min_eval_peak"]:
            bias = None                                  # 刹得不够重，看不出谁先饱和
        elif first is not None:
            bias = first
        elif pf - pr >= r["bias_diff"]:
            bias = "front"
        elif pr - pf >= r["bias_diff"]:
            bias = "rear"
        else:
            bias = "balanced"
        loc = ctx.loc(i0, i1, wheel=None if bias in (None, "balanced") else bias)
        zones.append({"i0": i0, "i1": i1, "dur": dur, "front_peak": pf, "rear_peak": pr, "front_mark_s": tf,
                      "rear_mark_s": tr, "first": first, "lead_s": lead, "bias": bias, "loc": loc,
                      "speed_kmh": float(spd[i0]), "brake": float(brk[sl].mean())})
    ctx.samples["brake_zones"] = [{**{k: z[k] for k in ("front_peak", "rear_peak", "front_mark_s", "rear_mark_s", "first",
                                                       "lead_s", "bias", "speed_kmh", "brake")},
                                   "duration_s": z["dur"], "location": z["loc"], "lap_label": _lap_label(ctx.meta),
                                   "lap_id": ctx.meta.get("id")} for z in zones]
    ev = [z for z in zones if z["bias"] is not None]
    rule_txt = (f"直线重刹段 = 刹车 ≥ {_pct(r['min_brake'])}、|方向盘| < {_pct(r['max_steer'])}、车速 > {r['min_speed_kmh']:.0f} km/h、"
                f"前后 {r['kerb_window_s']:.2f} s 内没压路肩、没拉手刹，连续 ≥ {r['min_zone_s']:.2f} s")
    if len(ev) < r["min_zones"]:
        ctx.check(cid, name, "skipped", f"数据不足：只有 {len(ev)} 段够重的直线重刹（{rule_txt}，"
                                        f"且某一轴滑移率峰值 ≥ {r['min_eval_peak']:.2f}；至少要 {r['min_zones']} 段）{hand_txt}。")
        return
    n_f = sum(1 for z in ev if z["bias"] == "front")
    n_r = sum(1 for z in ev if z["bias"] == "rear")
    n_b = len(ev) - n_f - n_r
    ctx.extra["brake_balance_counts"] = {"zones": len(ev), "front": n_f, "rear": n_r, "balanced": n_b,
                                         "front_where": [ctx.where(None, "straight", ctx.dist[z["i0"]]) for z in ev if z["bias"] == "front"]}
    count_txt = f"{len(ev)} 段直线重刹：前轴先饱和 {n_f} 段、后轴先饱和 {n_r} 段、前后差不多 {n_b} 段"
    for axle, k in (("front", n_f), ("rear", n_r)):
        other = n_r if axle == "front" else n_f
        if k >= r["min_zones"] and k / len(ev) >= r["dominant_share"] and k > other:
            ctx.findings.append(_brake_bias_finding(ctx, axle, ev, k, count_txt, rule_txt))
            ctx.check(cid, name, "found", count_txt + hand_txt + "。")
            return
    ctx.check(cid, name, "ok", count_txt + hand_txt + f"，没有一根轴在大多数（≥ {_pct(r['dominant_share'])}）重刹段里先饱和。")


def _brake_bias_finding(ctx: _Ctx, axle: str, ev: list[dict], k: int, count_txt: str, rule_txt: str) -> dict:
    r = ctx.r["brake_balance"]
    axle_cn, other_cn = AXLE_CN[axle], AXLE_CN["rear" if axle == "front" else "front"]
    hit = [z for z in ev if z["bias"] == axle]
    evidence = []
    for z in hit:
        p_hi, p_lo = (z["front_peak"], z["rear_peak"]) if axle == "front" else (z["rear_peak"], z["front_peak"])
        mark = ""
        if z["first"] == axle:
            mark = (f"；{axle_cn}先到 {r['slip_mark']:.2f}" +
                    (f"（比{other_cn}早 {z['lead_s']:.2f} s）" if z["lead_s"] is not None else f"，{other_cn}一直没到"))
        evidence.append(f"{z['loc']['label']}（{z['dur']:.2f} s，车速 {z['speed_kmh']:.0f} km/h 起，刹车平均 {_pct(z['brake'])}）："
                        f"{axle_cn} |滑移率| 峰值 {p_hi:.2f}、{other_cn} {p_lo:.2f}{mark}")
    evidence.append(count_txt)
    evidence.append(f"判定阈值：{rule_txt}；某一轴先到 {r['slip_mark']:.2f}（领先 ≥ {r['mark_lead_s']:.2f} s），"
                    f"或两轴峰值（p{r['peak_pct']:.0f}）差 ≥ {r['bias_diff']:.2f} 算这一段偏向它；"
                    f"≥ {r['min_zones']} 段且占 ≥ {_pct(r['dominant_share'])} 才报")
    if axle == "front":
        tips = ["刹车平衡往后挪 1~2%（后轮多分担一点刹车力）", "改完看这里前后轴的峰值是否接近，以及“刹车抱死”有没有变少"]
        summary = (f"{count_txt}：多数重刹段里前轴的滑移率先到极限、后轴还有余量——"
                   "刹车力偏向前轴，还没抱死也已经先饱和，后轴的抓地没用上。")
    else:
        tips = ["刹车平衡往前挪 1~2%（后轮少分担一点刹车力）", "后轴先饱和时刹车容易甩尾，入弯前先把直线刹车做稳"]
        summary = (f"{count_txt}：多数重刹段里后轴的滑移率先到极限、前轴还有余量——"
                   "刹车力偏向后轴，直线刹车时车尾容易不稳。")
    conf = "high" if k >= 2 * r["min_zones"] and k / len(ev) >= 0.75 else "medium"
    if ctx.abs_on is True:
        conf = _lower(conf)
    caveat = " ".join(x for x in (_abs_caveat(ctx.abs_on, "前后轴对比"), ONE_AT_A_TIME) if x)
    basis = {"channels": ["slip_ratio_fl/fr/rl/rr（归一化滑移率，前后轴各取左右两轮 |x| 的平均）", "brake", "steer", "speed",
                          "rumble_*（排除压路肩）"],
             "rule": (f"{rule_txt}。每段比较前后轴 |滑移率| 的 p{r['peak_pct']:.0f} 峰值，和谁先连续两个采样到 {r['slip_mark']:.2f}。"
                      "直线上不转向，前后轴的滑移差别主要来自刹车力分配。"),
             "principle": ("刹车时滑移率越接近 1，轮胎越接近纵向抓地极限。直线重刹时前后轴受的刹车力按“刹车平衡”分配："
                           "总是同一根轴先接近极限，说明分给它的刹车力相对它的载荷偏多。理想状态是两根轴差不多同时到极限，"
                           "这样总的减速度最大。比抱死更早就能看出来。")}
    return _finding(f"brake_bias_{axle}", "brakes", "warn", f"刹车平衡偏{'前' if axle == 'front' else '后'}（直线重刹时{axle_cn}先饱和）",
                    summary, evidence, [z["loc"] for z in hit], basis, tips, conf, caveat,
                    data={"bias": axle, "zones": len(ev), "biased_zones": k, "abs_on": ctx.abs_on})
