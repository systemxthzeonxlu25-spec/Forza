"""检查项⑤d：差速器（入弯松刹的内侧轮 / 四驱前后分配）。"""

from __future__ import annotations

import numpy as np

from .common import (
    AXLE_CN,
    AXLE_WHEELS,
    CHECK_NAME,
    DRIVEN_WHEELS,
    ONE_AT_A_TIME,
    WHEELS,
    _Ctx,
    _corner_key,
    _finding,
    _hands_off,
    _inner_wheels,
    _pct,
    _runs,
    _wheel_cn,
    _wheel_code,
)

# ================================================================ ⑤d 差速器（入弯松刹的内侧轮 / 四驱前后分配）

def _check_differential(ctx: _Ctx) -> None:
    cid, name = "differential", CHECK_NAME["differential"]
    need = [f"slip_ratio_{w}" for w in WHEELS] + ["throttle", "brake", "g_lat"]
    if not ctx.dash or ctx.missing(need):
        ctx.check(cid, name, "skipped", "数据不足：需要滑移率、油门、刹车、横向 G。")
        return
    dt_code = ctx.meta.get("drivetrain")
    if dt_code not in DRIVEN_WHEELS:
        ctx.check(cid, name, "skipped", "数据不足：不知道驱动方式（meta 里没有 drivetrain），分不出该看哪个差速器。")
        return
    r = ctx.r["differential"]
    g = ctx.r["general"]
    notes = []
    found = evaluated = False
    thr, brk, glat = ctx.ch("throttle"), ctx.ch("brake"), np.abs(ctx.ch("g_lat"))
    sr = {w: np.abs(ctx.ch(f"slip_ratio_{w}")) for w in WHEELS}
    axles = [ax for ax in ("front", "rear") if all(w in DRIVEN_WHEELS[dt_code] for w in AXLE_WHEELS[ax])]

    # (a) 入弯松刹：弯心前、不踩油门也不踩刹车、在转弯 → 发动机制动经差速器作用在驱动轮上
    if ctx.corners:
        lift = (thr <= r["lift_max_throttle"]) & (brk <= r["lift_max_brake"]) & (glat >= r["min_lat_g"]) & ctx.speed_ok
        entry_rng = np.zeros(ctx.n, bool)
        for c in ctx.corners:
            lo = min(c["brake_start_m"] if c.get("brake_start_m") is not None else c["start_m"], c["start_m"])
            entry_rng |= (ctx.dist >= lo) & (ctx.dist < c["apex_m"])
        # 拉手刹（后轮被手刹拖住）、踩离合（发动机制动传不到车轮）时的滑移和差速器无关
        hands, hand_txt = _hands_off(ctx, lift & entry_rng, clutch=True)
        lift &= hands
        inner_m = {ax: np.zeros(ctx.n, bool) for ax in axles}
        both_m = {ax: np.zeros(ctx.n, bool) for ax in axles}
        lift_entry = np.zeros(ctx.n, bool)
        owner = np.full(ctx.n, -1)
        for c in ctx.corners:
            lo = min(c["brake_start_m"] if c.get("brake_start_m") is not None else c["start_m"], c["start_m"])
            seg = lift & (ctx.dist >= lo) & (ctx.dist < c["apex_m"]) & (owner == -1)
            owner[seg] = c["index"]
            lift_entry |= seg
            inner = _inner_wheels(c)
            for ax in axles:
                iw = next(w for w in AXLE_WHEELS[ax] if w in inner)
                ow = next(w for w in AXLE_WHEELS[ax] if w not in inner)
                i_s, o_s = sr[iw] > r["slip_limit"], sr[ow] > r["slip_limit"]
                inner_m[ax] |= seg & i_s & ~o_s
                both_m[ax] |= seg & i_s & o_s
        lift_s = float(lift_entry.sum() * ctx.dt)
        if lift_s < r["min_lift_s"]:
            notes.append(f"入弯松刹（油门 ≤ {_pct(r['lift_max_throttle'])}、刹车 ≤ {_pct(r['lift_max_brake'])}、横向 G ≥ {r['min_lat_g']:.2f}）"
                         f"只有 {lift_s:.1f} s{hand_txt}，不判断减速锁止")
        else:
            evaluated = True
            events = []
            for ax in axles:
                for kind, mm in (("inner", inner_m[ax]), ("both", both_m[ax])):
                    for i0, i1 in _runs(mm, ctx.t, g["merge_gap_s"], ctx.dt):
                        if ctx.dur(i0, i1) < r["min_duration_s"]:
                            continue
                        c = ctx.corners[int(np.bincount(owner[i0:i1 + 1][owner[i0:i1 + 1] >= 0]).argmax())]
                        inner = _inner_wheels(c)
                        wheels = [w for w in AXLE_WHEELS[ax] if kind == "both" or w in inner]
                        events.append({"i0": i0, "i1": i1, "axle": ax, "kind": kind, "corner": c, "wheels": wheels,
                                       "peak": max(float(sr[w][i0:i1 + 1].max()) for w in wheels),
                                       "other": float(np.mean([sr[w][i0:i1 + 1].mean() for w in AXLE_WHEELS[ax] if w not in wheels]))
                                       if kind == "inner" else None})
            mx = max(float(sr[w][lift_entry].max()) for ax in axles for w in AXLE_WHEELS[ax]) if lift_entry.any() else 0.0
            if events:
                found = True
                ctx.findings.append(_diff_decel_finding(ctx, sorted(events, key=lambda e: e["i0"]), lift_s, hand_txt))
                notes.append(f"入弯松刹 {lift_s:.1f} s{hand_txt} 里驱动轮滑移超限 {len(events)} 次")
            else:
                notes.append(f"入弯松刹 {lift_s:.1f} s{hand_txt}，驱动轮 |滑移率| 最大 {mx:.2f}，没超过 {r['slip_limit']:.2f}")
    else:
        notes.append("没识别出弯道，不判断入弯松刹时的内侧轮")

    # (b) 四驱：给油接近抓地极限时前后轴谁滑得多 → 中央差速器分配
    if dt_code == 2:
        rt = ctx.r["traction"]
        f_sr = (sr["fl"] + sr["fr"]) / 2
        r_sr = (sr["rl"] + sr["rr"]) / 2
        power = (thr >= rt["min_throttle"]) & (brk < rt["max_brake"]) & ctx.speed_ok
        near = power & (np.maximum(f_sr, r_sr) >= r["awd_min_slip"])
        near_s = float(near.sum() * ctx.dt)
        if near_s < r["awd_min_s"] - 1e-6:
            notes.append(f"四驱：给油时接近抓地极限（某一轴 |滑移率| ≥ {r['awd_min_slip']:.2f}）只有 {near_s:.1f} s，不判断中央差速器")
        else:
            evaluated = True
            fm, rm = float(f_sr[near].mean()), float(r_sr[near].mean())
            ratio = rm / max(fm, 1e-3)
            if ratio >= r["awd_ratio"] or ratio <= 1 / r["awd_ratio"]:
                found = True
                ctx.findings.append(_awd_finding(ctx, near, fm, rm, near_s))
                notes.append(f"四驱：给油接近极限的 {near_s:.1f} s 里后轴 / 前轴平均滑移比 {ratio:.2f}")
            else:
                notes.append(f"四驱：给油接近极限的 {near_s:.1f} s 里前轴平均 |滑移率| {fm:.2f}、后轴 {rm:.2f}，前后分配合理")
    if not evaluated:
        ctx.check(cid, name, "skipped", "数据不足：" + "；".join(notes) + "。")
        return
    ctx.check(cid, name, "found" if found else "ok", "；".join(notes) + "。出弯空转分内侧 / 外侧轮的判断见“驱动轮空转”。")


def _diff_decel_finding(ctx: _Ctx, events: list[dict], lift_s: float, hand_txt: str = "") -> dict:
    r = ctx.r["differential"]
    inner = [e for e in events if e["kind"] == "inner"]
    corners = sorted({e["corner"]["name"] for e in events}, key=_corner_key)
    evidence = []
    for e in events:
        loc = ctx.loc(e["i0"], e["i1"], wheel=_wheel_code(e["wheels"]), corner=e["corner"], phase="entry")
        e["loc"] = loc
        turn = "右弯" if e["corner"]["direction"] == "right" else "左弯"
        if e["kind"] == "inner":
            evidence.append(f"{loc['label']}（{turn}，持续 {ctx.dur(e['i0'], e['i1']):.2f} s）：松刹车滑行入弯时内侧的"
                            f"{_wheel_cn(e['wheels'][0])} |滑移率| 最大 {e['peak']:.2f}，同轴外侧轮平均只有 {e['other']:.2f}")
        else:
            evidence.append(f"{loc['label']}（{turn}，持续 {ctx.dur(e['i0'], e['i1']):.2f} s）：松刹车滑行入弯时"
                            f"{AXLE_CN[e['axle']]}两个驱动轮 |滑移率| 都超过 {r['slip_limit']:.2f}（最大 {e['peak']:.2f}）")
    evidence.append(f"判定阈值：弯心前、油门 ≤ {_pct(r['lift_max_throttle'])}、刹车 ≤ {_pct(r['lift_max_brake'])}、横向 G ≥ {r['min_lat_g']:.2f}"
                    f"、没拉手刹、没踩离合（本圈共 {lift_s:.1f} s{'，' + hand_txt[1:-1] if hand_txt else ''}）时，驱动轮 |滑移率| > {r['slip_limit']:.2f} 连续 ≥ {r['min_duration_s']:.2f} s；"
                    f"出现在 ≥ {r['alert_corners']} 个弯记为严重")
    if inner:
        sev = "alert" if len(corners) >= r["alert_corners"] else "warn"
        title = f"入弯松刹时内侧驱动轮被拖滑（{'、'.join(ctx.cn(c) for c in corners)}）"
        summary = ("松刹车滑行入弯时，发动机制动集中到了载荷小的内侧驱动轮上，内侧轮先滑：差速器减速锁止偏低。"
                   "这会让入弯时车尾不稳（后驱）或者车头不听话（前驱）。")
        tips = ["差速器减速锁止率调高一点（松油时左右轮被约束得更紧，发动机制动更多给外侧重载轮）",
                "先排除驾驶：降挡别太早太快，降挡时补油（跟趾）"]
    else:
        sev = "info"
        title = f"入弯松刹时驱动轴两轮一起滑（{'、'.join(ctx.cn(c) for c in corners)}）"
        summary = "松刹车滑行入弯时同一根驱动轴两个轮子都滑：发动机制动整体太大，和差速器左右分配关系不大。"
        tips = ["先排除驾驶：降挡别太早、别一次降好几挡", "差速器减速锁止率调低一点也能让车尾更容易跟着转，但解决不了发动机制动太大"]
    basis = {"channels": ["slip_ratio_*（归一化滑移率）", "throttle", "brake", "g_lat", "弯道方向（corners.direction，来自方向盘）",
                          "meta.drivetrain（驱动轮）", "handbrake / clutch（排除拉手刹、踩离合）"],
             "rule": "按弯道方向分出内侧 / 外侧驱动轮（右弯内侧 = 右侧轮），只看弯心前的松刹车滑行段（拉手刹、踩离合的采样不算）。",
             "principle": ("松油时发动机制动通过差速器分给左右驱动轮。差速器越“开放”，两边分到的制动力越接近，"
                           "过弯时内侧轮载荷小，先被拖到极限；减速锁止率越高，左右轮被约束得越紧，内侧轮越不容易单独滑。"
                           "两个轮子一起滑则说明发动机制动本身太大。")}
    return _finding("diff_decel", "differential", sev, title, summary, evidence, [e["loc"] for e in events], basis, tips,
                    "medium" if len(events) >= 2 else "low", ONE_AT_A_TIME,
                    data={"events": len(events), "inner_events": len(inner), "corners": corners})


def _awd_finding(ctx: _Ctx, near: np.ndarray, fm: float, rm: float, near_s: float) -> dict:
    r = ctx.r["differential"]
    rear_heavy = rm > fm
    hi, lo = ("后轴", "前轴") if rear_heavy else ("前轴", "后轴")
    runs = [(a, b) for a, b in _runs(near, ctx.t, ctx.r["general"]["merge_gap_s"], ctx.dt)]
    runs.sort(key=lambda x: x[1] - x[0], reverse=True)
    locs = [ctx.loc(a, b, wheel="rear" if rear_heavy else "front") for a, b in runs[:5]]
    evidence = [f"给油（油门 ≥ {_pct(ctx.r['traction']['min_throttle'])}）且某一轴 |滑移率| ≥ {r['awd_min_slip']:.2f} 的 {near_s:.1f} s 里："
                f"前轴平均 |滑移率| {fm:.2f}、后轴 {rm:.2f}，{hi}是{lo}的 {max(fm, rm) / max(min(fm, rm), 1e-3):.1f} 倍"]
    evidence += [f"　{l['label']}" for l in locs]
    evidence.append(f"判定阈值：两轴平均滑移比 ≥ {r['awd_ratio']:.1f} 倍（或 ≤ 1/{r['awd_ratio']:.1f}），接近极限的数据 ≥ {r['awd_min_s']:.1f} s")
    tips = (["中央差速器扭矩分配往前挪一点（前轴多分一点，后轴少滑）"] if rear_heavy else
            ["中央差速器扭矩分配往后挪一点（后轴多分一点，前轴少滑、出弯少推头）"])
    tips.append("先排除驾驶：出弯给油更柔")
    sev = "warn" if any(f["id"] == "wheelspin" for f in ctx.findings) else "info"
    basis = {"channels": ["slip_ratio_*（前后轴各取左右两轮 |x| 的平均）", "throttle", "brake", "meta.drivetrain = 四驱"],
             "rule": "只看四驱车给油、接近抓地极限的时刻，比较前后轴的平均滑移。",
             "principle": ("四驱车的中央差速器决定扭矩怎么分给前后轴。分给某一轴太多，它会先到纵向极限开始空转，"
                           "另一轴的抓地却没用上；往另一轴挪一点分配，两轴能更均匀地出力。")}
    return _finding(f"awd_center_{'rear' if rear_heavy else 'front'}", "differential", sev,
                    f"四驱{hi}滑得多（中央差速器分配偏{'后' if rear_heavy else '前'}）",
                    f"给油接近极限时{hi}的滑移明显比{lo}大：中央差速器给{hi}的扭矩偏多。", evidence, locs, basis, tips,
                    "medium" if near_s >= 4 * r["awd_min_s"] else "low", ONE_AT_A_TIME,
                    data={"front_mean": round(fm, 3), "rear_mean": round(rm, 3), "near_limit_s": round(near_s, 2)})
