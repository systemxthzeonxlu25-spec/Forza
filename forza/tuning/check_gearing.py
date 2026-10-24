"""检查项⑥：齿比（最高挡撞断油 / 转速余量）。"""

from __future__ import annotations

import numpy as np

from .common import ONE_AT_A_TIME, _Ctx, _confidence, _finding, _pct, _runs

# ================================================================ ⑥ 齿比

def _check_gearing(ctx: _Ctx) -> None:
    name = "齿比（最高挡撞断油 / 转速余量）"
    if not ctx.dash or ctx.missing(("gear", "rpm", "speed")):
        ctx.check("gearing", name, "skipped", "数据不足：需要挡位、转速、车速。")
        return
    max_rpm = float(ctx.meta.get("engine_max_rpm") or 0.0)
    if max_rpm <= 0:
        ctx.check("gearing", name, "skipped", "数据不足：不知道发动机最高转速（engine_max_rpm）。")
        return
    r = ctx.r["gearing"]
    gear = np.round(ctx.ch("gear")).astype(int)
    rpm = ctx.ch("rpm")
    spd = ctx.ch("speed")
    valid = (gear >= 1) & (gear <= 10) & ctx.speed_ok
    used = [gr for gr in range(1, 11) if np.sum(valid & (gear == gr)) * ctx.dt >= r["min_gear_time_s"]]
    if not used:
        ctx.check("gearing", name, "skipped", "数据不足：没有哪个挡位用够时间。")
        return
    top = max(used)
    in_top = valid & (gear == top)
    ctx.extra["top_gear_used"] = top
    m = in_top & (rpm >= r["limiter_frac"] * max_rpm)
    events = [(i0, i1) for i0, i1 in _runs(m, ctx.t, r["merge_gap_s"], ctx.dt) if ctx.dur(i0, i1) >= r["min_duration_s"]]
    top_rpm = float(rpm[in_top].max())
    basis_ch = ["gear", "rpm", "speed", "meta.engine_max_rpm"]
    if events:
        total = sum(ctx.dur(a, b) for a, b in events)
        sev = "alert" if total >= r["alert_total_s"] else "warn"
        locs, evidence = [], []
        for i0, i1 in events:
            loc = ctx.loc(i0, i1, use_corner=False, phase="straight")
            loc["label"] = f"{ctx.where(None, 'straight', ctx.dist[i0])} · {ctx.dist[i0]:.0f}–{ctx.dist[i1]:.0f} m"
            locs.append(loc)
            sl = slice(i0, i1 + 1)
            evidence.append(f"{loc['label']}：{top} 挡转速 ≥ {r['limiter_frac'] * max_rpm:.0f} rpm 持续 {ctx.dur(i0, i1):.2f} s，"
                            f"最高 {rpm[sl].max():.0f} rpm，车速 {spd[sl].max() * 3.6:.0f} km/h 上不去了")
        evidence.append(f"判定阈值：本圈用到的最高挡（{top} 挡）里转速 ≥ 最高转速 {max_rpm:.0f} × {r['limiter_frac']:.2f}，"
                        f"连续 ≥ {r['min_duration_s']:.1f} s（中间断开 ≤ {r['merge_gap_s']:.2f} s 算同一次；累计 ≥ {r['alert_total_s']:.1f} s 记为严重）")
        basis = {"channels": basis_ch,
                 "rule": (f"统计每个挡位用了多久（≥ {r['min_gear_time_s']:.1f} s 才算用到），取用到的最高挡；"
                          f"这个挡里转速 ≥ {_pct(r['limiter_frac'])} 最高转速连续 ≥ {r['min_duration_s']:.1f} s 记一次。"),
                 "principle": "最高挡已经顶到断油转速，车速就被齿比锁死了，直道末端再踩油也不会更快：齿比对这条赛道偏短。"}
        conf = _confidence(ctx.r, total, r["min_duration_s"], 0.0)
        ctx.findings.append(_finding(
            "rev_limiter_top", "gearing", sev, f"最高挡（{top} 挡）撞断油",
            f"{top} 挡在直道上顶着断油跑了 {total:.1f} s，车速被齿比限制住了。", evidence, locs, basis,
            ["主减速比（Final Drive）调小一点，齿比整体变长，让最长直道末端刚好接近断油",
             f"或者只把 {top} 挡单独调长一点（低挡加速不受影响）"],
            conf, "Data Out 不告诉车一共有几个挡：如果这车其实还有更高的挡，那是没升挡（驾驶 / 自动挡逻辑），不是齿比问题。" + ONE_AT_A_TIME,
            data={"top_gear": top, "total_s": round(total, 3), "engine_max_rpm": max_rpm}))
        ctx.check("gearing", name, "found", f"本圈用到的最高挡 {top} 挡，撞断油 {len(events)} 次。")
        return
    frac = top_rpm / max_rpm
    if frac < r["headroom_frac"] and "first_lap" in set(ctx.meta.get("flags") or []):
        ctx.check("gearing", name, "ok", f"本圈用到的最高挡 {top} 挡，最高转速 {_pct(frac)}，没有撞断油。"
                                         "这是站立起步的第一圈（起跑那段直道跑不到最高速），不判断齿比是否偏长。")
        return
    if frac < r["headroom_frac"]:
        i = int(np.flatnonzero(in_top)[np.argmax(rpm[in_top])])
        loc = ctx.loc(i, i, use_corner=False, phase="straight")
        loc["label"] = f"{ctx.where(None, 'straight', ctx.dist[i])} · {ctx.dist[i]:.0f} m（{top} 挡最高转速处）"
        ctx.findings.append(_finding(
            "top_gear_headroom", "gearing", "info", f"{top} 挡转速余量很大（齿比可能偏长）",
            f"本圈最高只用到 {top} 挡，最高转速只到最高转速的 {_pct(frac)}。",
            [f"{top} 挡里最高 {top_rpm:.0f} rpm，最高转速 {max_rpm:.0f} rpm 的 {_pct(frac)}（< {_pct(r['headroom_frac'])} 才提示）",
             f"最高车速 {float(spd.max()) * 3.6:.0f} km/h"],
            [loc],
            {"channels": basis_ch,
             "rule": f"本圈用到的最高挡里，最高转速 < {_pct(r['headroom_frac'])} 最高转速时提示。",
             "principle": "最长的直道末端转速还离红线很远，说明这条赛道上齿比偏长，各挡加速没吃满。"},
            ["主减速比（Final Drive）调大一点，各挡加速更好（最高速会降低）"],
            "low", "只是提示：如果最长直道后面接急弯、很早就收油，转速余量大是正常的；Data Out 也看不到这车一共有几挡。",
            data={"top_gear": top, "top_rpm_frac": round(frac, 3)}))
        ctx.check("gearing", name, "found", f"本圈用到的最高挡 {top} 挡，最高转速 {_pct(frac)}。")
        return
    ctx.check("gearing", name, "ok", f"本圈用到的最高挡 {top} 挡，最高转速 {top_rpm:.0f} rpm（{_pct(frac)}），"
                                     f"没有持续撞断油（≥ {_pct(r['limiter_frac'])} 连续 ≥ {r['min_duration_s']:.1f} s）。")
