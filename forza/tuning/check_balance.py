"""检查项①：转向平衡（按弯道阶段的转向不足 / 转向过度）。"""

from __future__ import annotations

import numpy as np

from .common import (
    ONE_AT_A_TIME,
    PHASES,
    PHASE_CN,
    WHEELS,
    _Ctx,
    _confidence,
    _corner_key,
    _corner_label,
    _finding,
    _hands_off,
    _lap_label,
    _pct,
    _runs,
)

# ================================================================ ① 转向平衡

_BALANCE_TIPS = {
    ("understeer", "entry"): [
        "先排除驾驶：入弯速度低一点、刹车早一点松，前轮才有余量转向",
        "刹车平衡往后挪 1~2%（前轮刹车负担小一些，入弯时前轮更有余量）",
        "差速器减速锁止率调低一点（松刹车入弯时车尾更容易跟着转）",
        "前防倾杆调软一点",
    ],
    ("understeer", "mid"): [
        "前防倾杆调软一点",
        "后防倾杆调硬一点",
        "前弹簧调软 / 后弹簧调硬（二选一，小步改）",
        "有可调空力时：前下压力加一点，或后下压力减一点",
    ],
    ("understeer", "exit"): [
        "先排除驾驶：等车头转过来再全油，给油更柔一点",
        "差速器加速锁止率调低一点（锁得太死时出弯容易推头）",
        "前防倾杆调软一点 / 后防倾杆调硬一点",
    ],
    ("oversteer", "entry"): [
        "先排除驾驶：刹车时少带方向，降挡别太早太猛（发动机制动会让后轮滑）",
        "刹车平衡往前挪 1~2%（后轮少刹一些）",
        "差速器减速锁止率调高一点（松刹车时车尾更稳）",
        "后防倾杆调软一点 / 前防倾杆调硬一点",
    ],
    ("oversteer", "mid"): [
        "后防倾杆调软一点",
        "前防倾杆调硬一点",
        "后弹簧调软一点",
        "有可调空力时：后下压力加一点",
    ],
    ("oversteer", "exit"): [
        "先排除驾驶：出弯给油更柔、分段踩，车身摆正再全油",
        "差速器加速锁止率调低一点（后驱常见做法）",
        "后防倾杆 / 后弹簧调软一点，让后轮更贴地",
    ],
}

_BALANCE_PRINCIPLE = {
    "understeer": ("归一化侧偏角 |x| > 1 表示这个轮胎已经过了抓地极限，再加大侧偏角也不会多出侧向力。"
                   "前轴先过极限、后轴还有余量，说明车头先“饱和”：车会往弯外推，司机多打方向也转不进去，这就是转向不足（推头）。"),
    "oversteer": ("归一化侧偏角 |x| > 1 表示这个轮胎已经过了抓地极限。后轴先过极限、前轴还有余量，"
                  "说明车尾先滑：车会往弯内甩，这就是转向过度（甩尾）。"),
}


def _check_balance(ctx: _Ctx) -> None:
    name = "转向平衡（转向不足 / 转向过度）"
    need = [f"slip_angle_{w}" for w in WHEELS] + ["throttle", "brake", "steer", "g_lat"]
    if not ctx.dash or ctx.missing(need):
        ctx.check("balance", name, "skipped", "数据不足：需要侧偏角、油门、刹车、方向盘、横向 G。")
        return
    r = ctx.r["balance"]
    g = ctx.r["general"]
    d = ctx.dist
    thr, brk = ctx.ch("throttle"), ctx.ch("brake")
    steer, glat = np.abs(ctx.ch("steer")), np.abs(ctx.ch("g_lat"))
    fa = (np.abs(ctx.ch("slip_angle_fl")) + np.abs(ctx.ch("slip_angle_fr"))) / 2
    ra = (np.abs(ctx.ch("slip_angle_rl")) + np.abs(ctx.ch("slip_angle_rr"))) / 2
    spd_kmh = np.abs(ctx.ch("speed")) * 3.6
    # 平衡分布（按车速分档）用所有“在转弯”的采样：横向 G ≥ min_lat_g、车速够
    turning = (glat >= r["min_lat_g"]) & ctx.speed_ok
    hands, hand_txt = _hands_off(ctx, turning)    # 拉手刹甩尾是司机故意的，不算调校的平衡问题
    turning &= hands
    ctx.samples["balance"] = {
        "dt": ctx.dt, "speed_kmh": spd_kmh[turning], "b": (fa - ra)[turning], "fa": fa[turning], "ra": ra[turning],
        "cells": None, "corners": [], "corner_source": ctx.corner_source, "lap_label": _lap_label(ctx.meta),
    }
    if not ctx.corners:
        ctx.check("balance", name, "skipped", "数据不足：没识别出弯道，无法按入弯 / 弯中 / 出弯分阶段判断。")
        return

    # 给每个采样点标上 (弯道, 阶段)；一个点只属于一个阶段（先到先得）
    phase = np.full(ctx.n, -1)
    cidx = np.full(ctx.n, -1)
    for c in ctx.corners:
        lo = min(c["brake_start_m"] if c.get("brake_start_m") is not None else c["start_m"], c["start_m"])
        masks = (
            (d >= lo) & (d < c["apex_m"]) & (brk >= r["entry_min_brake"]) & (steer >= r["entry_min_steer"])
            & (glat >= r["min_lat_g"]),
            (d >= c["start_m"]) & (d <= c["end_m"]) & (brk < r["entry_min_brake"])
            & (thr < r["exit_min_throttle"]) & (glat >= r["mid_min_lat_g"]),
            (d >= c["apex_m"]) & (d <= c["end_m"] + r["exit_extend_m"]) & (thr >= r["exit_min_throttle"])
            & (brk < r["entry_min_brake"]) & (glat >= r["min_lat_g"]),
        )
        for p, m in enumerate(masks):
            m = m & ctx.speed_ok & hands & (phase == -1)
            phase[m] = p
            cidx[m] = c["index"]
    # 热力表（弯 × 阶段）用分好阶段的采样
    sel = phase >= 0
    names = np.array([c["name"] for c in ctx.corners], dtype=object)
    ctx.samples["balance"].update(
        cells={"code": names[cidx[sel]], "phase": phase[sel], "b": (fa - ra)[sel], "speed_kmh": spd_kmh[sel]},
        corners=[{"code": c["name"], "label": _corner_label(c), "direction": c["direction"],
                  "apex_m": round(float(c["apex_m"]), 1)} for c in ctx.corners])

    # 哪些 (弯, 阶段) 数据够多、真正评估了
    evaluated = {p: 0 for p in range(3)}
    seg_samples: dict[tuple[int, int], int] = {}
    for c in ctx.corners:
        for p in range(3):
            k = int(np.sum((cidx == c["index"]) & (phase == p)))
            seg_samples[(c["index"], p)] = k
            if k * ctx.dt >= r["min_phase_s"]:
                evaluated[p] += 1
    n_eval = sum(evaluated.values())
    eval_txt = (f"评估了 {len(ctx.corners)} 个弯里数据足够的 {n_eval} 个阶段"
                f"（入弯 {evaluated[0]} / 弯中 {evaluated[1]} / 出弯 {evaluated[2]}）{hand_txt}")
    if n_eval == 0:
        ctx.check("balance", name, "skipped",
                  f"数据不足：识别出 {len(ctx.corners)} 个弯，但没有一个阶段的数据超过 {r['min_phase_s']} s。")
        return

    us = (fa > r["grip_limit"]) & (fa - ra >= r["axle_diff"])
    os_ = (ra > r["grip_limit"]) & (ra - fa >= r["axle_diff"])
    found = 0
    for kind, km in (("understeer", us), ("oversteer", os_)):
        sat, other = (fa, ra) if kind == "understeer" else (ra, fa)
        sat_cn, other_cn = ("前轴", "后轴") if kind == "understeer" else ("后轴", "前轴")
        for p, pname in enumerate(PHASES):
            m = km & (phase == p)
            events = []
            for i0, i1 in _runs(m, ctx.t, g["merge_gap_s"], ctx.dt):
                dur = ctx.dur(i0, i1)
                if dur < r["min_duration_s"]:
                    continue
                q = np.zeros(ctx.n, bool)
                q[i0:i1 + 1] = m[i0:i1 + 1]
                ci = int(np.bincount(cidx[q]).argmax())
                c = ctx.corners[ci]
                events.append({
                    "i0": i0, "i1": i1, "dur": dur, "corner": c,
                    "sat_mean": float(sat[q].mean()), "sat_max": float(sat[q].max()),
                    "oth_mean": float(other[q].mean()), "oth_max": float(other[q].max()),
                    "diff_mean": float((sat[q] - other[q]).mean()),
                    "glat": float(glat[q].mean()), "steer": float(steer[q].mean()),
                    "thr": float(thr[q].mean()), "brk": float(brk[q].mean()),
                    "share": float(q.sum()) / max(1, seg_samples.get((ci, p), 0)),
                })
            if not events:
                continue
            found += 1
            ctx.findings.append(_balance_finding(ctx, kind, pname, events, sat_cn, other_cn))
    if found:
        ctx.check("balance", name, "found", eval_txt + "。")
    else:
        ctx.check("balance", name, "ok", eval_txt + "，没有发现前后轴侧偏角明显失衡。")


def _balance_finding(ctx: _Ctx, kind: str, pname: str, events: list[dict], sat_cn: str, other_cn: str) -> dict:
    r = ctx.r["balance"]
    total = sum(e["dur"] for e in events)
    corners = sorted({e["corner"]["name"] for e in events}, key=_corner_key)
    sev = "alert" if total >= r["alert_total_s"] or len(corners) >= r["alert_corners"] else "warn"
    sat_mean = float(np.average([e["sat_mean"] for e in events], weights=[e["dur"] for e in events]))
    oth_mean = float(np.average([e["oth_mean"] for e in events], weights=[e["dur"] for e in events]))
    margin = (sat_mean - r["grip_limit"]) / r["grip_limit"]
    conf = _confidence(ctx.r, total, r["min_duration_s"], margin)
    kind_cn = "转向不足" if kind == "understeer" else "转向过度"
    phase_cn = PHASE_CN[pname]
    where = "、".join(ctx.cn(c) for c in corners)
    if kind == "understeer":
        summary = (f"{where} {phase_cn}时前轴先超过抓地极限（|侧偏角| 平均 {sat_mean:.2f}），后轴只有 {oth_mean:.2f}："
                   "车头往弯外推，多打方向也转不进去。")
    else:
        summary = (f"{where} {phase_cn}时后轴先超过抓地极限（|侧偏角| 平均 {sat_mean:.2f}），前轴只有 {oth_mean:.2f}："
                   "车尾往外滑，需要反打方向救车。")
    evidence = []
    for e in events:
        evidence.append(
            f"{_corner_label(e['corner'])} {phase_cn}（{ctx.dist[e['i0']]:.0f}–{ctx.dist[e['i1']]:.0f} m，持续 {e['dur']:.2f} s）："
            f"{sat_cn} |侧偏角| 平均 {e['sat_mean']:.2f}、最大 {e['sat_max']:.2f}；"
            f"{other_cn}平均 {e['oth_mean']:.2f}、最大 {e['oth_max']:.2f}；两轴差平均 {e['diff_mean']:.2f}")
        evidence.append(
            f"　同一段：横向 G 平均 {e['glat']:.2f}，方向盘 {_pct(e['steer'])}，油门 {_pct(e['thr'])}，刹车 {_pct(e['brk'])}；"
            f"占这个弯{phase_cn}阶段数据的 {_pct(min(1.0, e['share']))}")
    evidence.append(
        f"判定阈值：{sat_cn} |侧偏角| > {r['grip_limit']:.2f} 且比{other_cn}大 ≥ {r['axle_diff']:.2f}，连续 ≥ {r['min_duration_s']:.2f} s。"
        f"本圈累计 {total:.2f} s（累计 ≥ {r['alert_total_s']:.1f} s 或出现在 ≥ {r['alert_corners']} 个弯记为严重）")
    basis = {
        "channels": ["slip_angle_fl/fr/rl/rr（归一化侧偏角）", "throttle", "brake", "steer", "g_lat", "dist（弯道识别）"],
        "rule": (f"按 corners.detect_corners 切出弯道，再分阶段：入弯 = 弯心前、刹车 ≥ {_pct(r['entry_min_brake'])} 且方向盘 ≥ {_pct(r['entry_min_steer'])}；"
                 f"弯中 = 弯道范围内、刹车 < {_pct(r['entry_min_brake'])}、油门 < {_pct(r['exit_min_throttle'])}、横向 G ≥ {r['mid_min_lat_g']:.2f}；"
                 f"出弯 = 过弯心后（到弯道终点后 {r['exit_extend_m']:.0f} m）油门 ≥ {_pct(r['exit_min_throttle'])}。"
                 f"每轴取左右两轮 |侧偏角| 的平均，{sat_cn} > {r['grip_limit']:.2f} 且比{other_cn}大 ≥ {r['axle_diff']:.2f}、"
                 f"连续 ≥ {r['min_duration_s']:.2f} s 记一次（车速 < {ctx.r['general']['min_speed_ms']:.0f} m/s 的点不算）。"),
        "principle": _BALANCE_PRINCIPLE[kind] + " 只比较绝对值，不依赖游戏的正负号约定；前后两轴都过极限（四轮一起滑）不算。",
    }
    tips = list(_BALANCE_TIPS[(kind, pname)])
    dt_code = ctx.meta.get("drivetrain")
    caveat = ONE_AT_A_TIME
    if kind == "understeer" and pname == "exit" and dt_code == 0:
        caveat = "前驱车出弯推头比较常见（前轮又要转向又要驱动）。" + caveat
    if kind == "oversteer" and pname == "exit" and dt_code == 1:
        caveat = "后驱车出弯给油过多也会这样，先确认是不是油门太急。" + caveat
    loc_wheel = "front" if kind == "understeer" else "rear"
    locs = [ctx.loc(e["i0"], e["i1"], wheel=loc_wheel, corner=e["corner"], phase=pname) for e in events]
    return _finding(f"{kind}_{pname}", "balance", sev, f"{phase_cn}{kind_cn}（{where}）", summary, evidence, locs, basis,
                    tips, conf, caveat,
                    data={"kind": kind, "phase": pname, "total_s": round(total, 3), "corners": corners,
                          "saturated_axle_mean": round(sat_mean, 3), "other_axle_mean": round(oth_mean, 3)})
