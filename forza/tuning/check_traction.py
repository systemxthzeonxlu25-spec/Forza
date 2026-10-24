"""检查项④：出弯驱动轮空转（含内侧 / 外侧轮、四驱前后轴）。"""

from __future__ import annotations

import numpy as np

from .common import (
    AXLE_WHEELS,
    DRIVEN_WHEELS,
    DRIVETRAIN_CN,
    ONE_AT_A_TIME,
    WHEELS,
    WHEEL_CN,
    _Ctx,
    _confidence,
    _corner_key,
    _corner_label,
    _finding,
    _hands_off,
    _inner_wheels,
    _pct,
    _runs,
    _wheel_code,
)

# ================================================================ ④ 空转

def _check_traction(ctx: _Ctx) -> None:
    name = "驱动轮空转"
    need = [f"slip_ratio_{w}" for w in WHEELS] + ["throttle", "brake"]
    if not ctx.dash or ctx.missing(need):
        ctx.check("traction", name, "skipped", "数据不足：需要滑移率、油门、刹车。")
        return
    r = ctx.r["traction"]
    g = ctx.r["general"]
    dt_code = ctx.meta.get("drivetrain")
    driven = DRIVEN_WHEELS.get(dt_code)
    if driven is None:
        driven = WHEELS
        ctx.note("info", "不知道驱动方式（meta 里没有 drivetrain），空转检查按四轮都算驱动轮。")
    thr, brk = ctx.ch("throttle"), ctx.ch("brake")
    pedal = (thr >= r["min_throttle"]) & (brk < r["max_brake"]) & ctx.speed_ok
    hands, hand_txt = _hands_off(ctx, pedal)      # 拉着手刹给油时后轮是被手刹拖住，不是空转
    pedal &= hands
    sr = {w: np.abs(ctx.ch(f"slip_ratio_{w}")) for w in driven}
    spin = {w: pedal & (sr[w] > r["slip_limit"]) for w in driven}
    any_spin = np.zeros(ctx.n, bool)
    for m in spin.values():
        any_spin |= m
    events = []
    for i0, i1 in _runs(any_spin, ctx.t, g["merge_gap_s"], ctx.dt):
        dur = ctx.dur(i0, i1)
        if dur < r["min_duration_s"]:
            continue
        sl = slice(i0, i1 + 1)
        wheels = [w for w in driven if spin[w][sl].any()]
        events.append({"i0": i0, "i1": i1, "dur": dur, "wheels": wheels,
                       "peak": {w: float(sr[w][sl].max()) for w in wheels},
                       "mean": {w: float(sr[w][sl][spin[w][sl]].mean()) for w in wheels},
                       "thr": float(thr[sl].mean())})
    dt_txt = DRIVETRAIN_CN.get(dt_code, "驱动方式未知")
    mx = max(float(sr[w][pedal].max()) if pedal.any() else 0.0 for w in driven)
    if not events:
        ctx.check("traction", name, "ok", f"{dt_txt}：给油（≥ {_pct(r['min_throttle'])}）时驱动轮 |滑移率| 最大 {mx:.2f}，"
                                          f"没超过 {r['slip_limit']:.2f}{hand_txt}。")
        return
    total = sum(e["dur"] for e in events)
    sev = "alert" if total >= r["alert_total_s"] or len(events) >= r["alert_events"] else "warn"
    rd = ctx.r["differential"]
    ra = None
    if not ctx.missing(("slip_angle_rl", "slip_angle_rr")):
        ra = (np.abs(ctx.ch("slip_angle_rl")) + np.abs(ctx.ch("slip_angle_rr"))) / 2
    k_win = max(1, round(rd["sa_window_s"] / ctx.dt))
    locs, evidence = [], []
    both_axle_spin = single_spin = inner_only = outer_only = sa_rise = False
    for e in events:
        code = _wheel_code(e["wheels"])
        loc = ctx.loc(e["i0"], e["i1"], wheel=code)
        locs.append(loc)
        per = "，".join(f"{WHEEL_CN[w]}平均 {e['mean'][w]:.2f}、最大 {e['peak'][w]:.2f}" for w in e["wheels"])
        evidence.append(f"{loc['label']}（持续 {e['dur']:.2f} s）：油门平均 {_pct(e['thr'])}，空转轮 |滑移率| {per}")
        corner = ctx.corner(loc["corner"])
        inner_side = _inner_wheels(corner) if corner else None
        for ax in ("front", "rear"):
            aw = [w for w in AXLE_WHEELS[ax] if w in driven]
            if len(aw) != 2:
                continue
            spun = [w for w in aw if w in e["wheels"]]
            if len(spun) == 2:
                both_axle_spin = True
                # 两轮一起空转时，后轴侧偏角有没有跟着涨（车尾开始滑）
                if ax == "rear" and ra is not None:
                    before = ra[max(0, e["i0"] - k_win): e["i0"]]
                    during = ra[e["i0"]: min(ctx.n, e["i1"] + k_win) + 1]
                    if before.size and during.size:
                        rise = float(during.max() - before.mean())
                        if rise >= rd["sa_rise"]:
                            sa_rise = True
                            evidence.append(
                                f"　同一段后轴 |侧偏角| 从 {before.mean():.2f} 升到 {during.max():.2f}（+{rise:.2f}，≥ {rd['sa_rise']:.2f}）："
                                "两个后轮一起空转、车尾同时在滑 → 加速锁止偏高（两轮被锁在一起一起丢抓地）或者油门给早了，先试油门")
            elif len(spun) == 1:
                single_spin = True
                if inner_side:
                    w = spun[0]
                    turn = "右弯" if corner["direction"] == "right" else "左弯"
                    if w in inner_side:
                        inner_only = True
                        evidence.append(f"　只有内侧的{WHEEL_CN[w]}在空转（{_corner_label(corner)} 是{turn}，内侧轮载荷小）"
                                        "→ 扭矩从内侧轮“漏掉”了：差速器加速锁止偏低")
                    else:
                        outer_only = True
                        evidence.append(f"　只有外侧的{WHEEL_CN[w]}在空转（{_corner_label(corner)} 是{turn}）"
                                        "→ 不是差速器偏开放的典型表现（开放差速器漏扭矩的是内侧轮）：先看是不是压了路肩 / 颠簸")
    if both_axle_spin:
        evidence.append("同一根驱动轴的左右两个轮子一起空转 → 不是差速器“漏扭矩”，而是轮上扭矩超过了这根轴的抓地")
    if single_spin and not (inner_only or outer_only):
        evidence.append("有时只有同轴的一个驱动轮在空转 → 扭矩从打滑的那一侧“漏掉”了，差速器偏开放")
    evidence.append(f"判定阈值：{dt_txt} 的驱动轮 |滑移率| > {r['slip_limit']:.2f}，同时油门 ≥ {_pct(r['min_throttle'])}、刹车 < {_pct(r['max_brake'])}，"
                    f"连续 ≥ {r['min_duration_s']:.2f} s。本圈 {len(events)} 次、累计 {total:.2f} s"
                    f"（累计 ≥ {r['alert_total_s']:.1f} s 或 ≥ {r['alert_events']} 次记为严重）")
    phases = {l["phase"] for l in locs}
    corners = sorted({l["corner"] for l in locs if l["corner"]}, key=_corner_key)
    title = ("出弯空转" if phases == {"exit"} else "驱动轮空转") + (f"（{'、'.join(ctx.cn(c) for c in corners)}）" if corners else "")
    tips = ["先排除驾驶：出弯给油更柔、分段踩，等车身摆正再全油"]
    if both_axle_spin:
        tips += ["驱动轴的弹簧 / 防倾杆调软一点，让驱动轮更贴地",
                 "差速器加速锁止率调低一点（两轮一起空转时可以试，调太低会变成内侧轮单独空转）",
                 "出弯用高一挡，或把低挡齿比调长一点（轮上扭矩小一些）"]
    if single_spin and (inner_only or not outer_only):
        tips += ["差速器加速锁止率调高一点（只有内侧轮空转说明差速器太开放）"]
    basis = {"channels": ["slip_ratio_*（归一化滑移率，|x| > 1 = 失去抓地）", "throttle", "brake", "speed",
                          "meta.drivetrain（哪几个是驱动轮）"],
             "rule": (f"只看驱动轮（{dt_txt}）。油门 ≥ {_pct(r['min_throttle'])}、刹车 < {_pct(r['max_brake'])}、车速 ≥ {g['min_speed_ms']:.0f} m/s 时，"
                      f"任一驱动轮 |滑移率| > {r['slip_limit']:.2f} 连续 ≥ {r['min_duration_s']:.2f} s 记一次（中间断开 ≤ {g['merge_gap_s']:.2f} s 算同一次）。"),
             "principle": ("滑移率衡量轮子转速和车速的差别，归一化后 |x| > 1 表示轮胎已经过了纵向抓地极限。"
                           "踩着油门时驱动轮超过 1，就是轮子在空转、扭矩没变成加速度；后驱车出弯空转还会让后轴丢侧向抓地，引起甩尾。"
                           "用的是绝对值 + 踏板状态区分空转和抱死，不依赖正负号。")}
    conf = _confidence(ctx.r, total, r["min_duration_s"],
                       float(np.mean([max(e["mean"].values()) for e in events])) / r["slip_limit"] - 1.0)
    ctx.findings.append(_finding("wheelspin", "traction", sev, title,
                                 f"{dt_txt}：给油时驱动轮空转 {len(events)} 次、累计 {total:.2f} s，扭矩没有全部变成加速。",
                                 evidence, locs, basis, tips, conf,
                                 "如果开着牵引力控制（TCS），空转会被压住；Data Out 看不到辅助设置。" + ONE_AT_A_TIME,
                                 data={"events": len(events), "total_s": round(total, 3),
                                       "both_wheels": both_axle_spin, "single_wheel": single_spin,
                                       "inner_only": inner_only, "outer_only": outer_only, "rear_slip_angle_rise": sa_rise}))
    ctx.check("traction", name, "found", f"{dt_txt}：给油时驱动轮空转 {len(events)} 次、累计 {total:.2f} s，"
                                         f"|滑移率| 最大 {mx:.2f}（阈值 {r['slip_limit']:.2f}）{hand_txt}。")
