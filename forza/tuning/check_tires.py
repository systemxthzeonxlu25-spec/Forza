"""检查项③：胎温平衡（左右 / 前后轴温差）。"""

from __future__ import annotations

import numpy as np

from .common import AXLE_WHEELS, ONE_AT_A_TIME, WHEELS, WHEEL_CN, _Ctx, _finding, _lower, _pct
from .quality import _turn_share

# ================================================================ ③ 胎温

def _check_tires(ctx: _Ctx) -> None:
    name = "胎温平衡"
    need = [f"tire_temp_{w}" for w in WHEELS]
    if not ctx.dash or ctx.missing(need):
        ctx.check("tires", name, "skipped", "数据不足：没有胎温（Sled 格式或缺少通道）。")
        return
    r = ctx.r["tires"]
    temps = np.stack([ctx.ch(f"tire_temp_{w}") for w in WHEELS])
    if np.ptp(temps) < 0.01:
        ctx.check("tires", name, "skipped", "数据不足：胎温整圈没有任何变化，像是没有真实胎温数据。")
        return
    mean4 = temps.mean(axis=0)
    n = ctx.n
    tail = mean4[int(n * (1 - r["warm_ref_frac"])):]
    ref = float(np.median(tail))
    hot = np.flatnonzero(mean4 >= ref - r["warm_margin_c"])
    i_warm = int(hot[0]) if hot.size else n - 1
    warm_s = float(ctx.t[-1] - ctx.t[i_warm])
    if warm_s < r["min_warm_s"]:
        ctx.check("tires", name, "skipped",
                  f"数据不足：热胎之后的数据只有 {warm_s:.0f} s（至少 {r['min_warm_s']:.0f} s）。")
        return
    w = temps[:, i_warm:]
    avg = {wh: float(w[k].mean()) for k, wh in enumerate(WHEELS)}
    ctx.extra["tire_temps_c"] = {k: round(v, 1) for k, v in avg.items()}
    slope = float(np.polyfit(ctx.t[i_warm:], mean4[i_warm:], 1)[0]) * 60.0 if w.shape[1] > 2 else 0.0
    rising = slope >= r["rising_c_per_min"]
    if rising:
        ctx.note("info", f"热胎段四轮平均胎温还在以 {slope:.1f} °C/分钟上升（≥ {r['rising_c_per_min']:.1f}），胎温结论置信度降一级。")
    a4 = float(np.mean(list(avg.values())))
    if not (r["sane_min_c"] <= a4 <= r["sane_max_c"]):
        ctx.note("warn", f"热胎平均胎温 {a4:.0f} °C 不太像正常热胎（{r['sane_min_c']:.0f}~{r['sane_max_c']:.0f} °C 之外），"
                         "请确认配置里的胎温单位（tire_temp_unit，°F / °C）是否正确。")
    conf_base = "high" if warm_s >= 2 * r["min_warm_s"] else "medium"
    if rising:
        conf_base = _lower(conf_base)

    share = _turn_share(ctx.corners)
    if share:
        if share["right"] >= r["dominant_share"]:
            expect, dom_txt = "left", f"右弯为主（右弯转角占 {_pct(share['right'])}，左弯 {_pct(share['left'])}）"
        elif share["left"] >= r["dominant_share"]:
            expect, dom_txt = "right", f"左弯为主（左弯转角占 {_pct(share['left'])}，右弯 {_pct(share['right'])}）"
        else:
            expect, dom_txt = None, f"左右弯差不多（右弯转角 {_pct(share['right'])}，左弯 {_pct(share['left'])}）"
    else:
        expect, dom_txt = None, "没识别出弯道，无法判断赛道方向"
    temp_line = (f"热胎后（从圈内 {ctx.t[i_warm]:.0f} s 起，共 {warm_s:.0f} s 数据）平均胎温：左前 {avg['fl']:.1f} °C、"
                 f"右前 {avg['fr']:.1f} °C、左后 {avg['rl']:.1f} °C、右后 {avg['rr']:.1f} °C")
    dir_line = f"本圈弯道：{dom_txt}。" + ("左侧轮胎在右弯是外侧，本来就会更热一些。" if expect == "left" else
                                         "右侧轮胎在左弯是外侧，本来就会更热一些。" if expect == "right" else "")
    found = 0
    diffs = {"front": avg["fl"] - avg["fr"], "rear": avg["rl"] - avg["rr"]}   # + = 左边更热
    for axle, d in diffs.items():
        if abs(d) < r["lr_diff_c"]:
            continue
        other_axle = "rear" if axle == "front" else "front"
        d_other = diffs[other_axle]
        hot_side = "left" if d > 0 else "right"
        same_dir = max(0.0, d_other * np.sign(d))
        excess = abs(d) - same_dir if expect == hot_side else abs(d)
        if excess < r["axle_excess_c"]:
            sev = "info"
        elif excess >= r["lr_alert_c"]:
            sev = "alert"
        else:
            sev = "warn"
        found += 1
        ctx.findings.append(_tire_lr_finding(ctx, axle, d, d_other, excess, sev, expect, hot_side, temp_line,
                                             dir_line, conf_base, i_warm))
    fa, ra = (avg["fl"] + avg["fr"]) / 2, (avg["rl"] + avg["rr"]) / 2
    dfa = fa - ra
    hot_axle = "front" if dfa > 0 else "rear"
    dt_code = ctx.meta.get("drivetrain")
    only_driven = (dt_code == 0 and hot_axle == "front") or (dt_code == 1 and hot_axle == "rear")
    thr_axle = r["axle_diff_c"] + (r["driven_axle_bonus_c"] if only_driven else 0.0)
    if abs(dfa) >= thr_axle:
        found += 1
        ctx.findings.append(_tire_axle_finding(ctx, fa, ra, hot_axle, thr_axle, only_driven, temp_line, conf_base, i_warm))
    lr_txt = f"前轴左右差 {diffs['front']:+.1f} °C、后轴 {diffs['rear']:+.1f} °C（+ = 左边热），前后轴差 {dfa:+.1f} °C"
    if found:
        ctx.check("tires", name, "found", lr_txt + "。")
    else:
        ctx.check("tires", name, "ok", lr_txt + f"，都在阈值内（同轴左右 < {r['lr_diff_c']:.0f} °C，前后轴 < {thr_axle:.0f} °C）。")


_TIRE_BASIS_CH = ["tire_temp_fl/fr/rl/rr（°C，每条胎只有一个温度）", "t（判断热胎）", "弯道方向（corners：转角 + 方向盘方向）"]


def _tire_lr_finding(ctx, axle, d, d_other, excess, sev, expect, hot_side, temp_line, dir_line, conf, i_warm) -> dict:
    r = ctx.r["tires"]
    axle_cn = WHEEL_CN[axle]
    hot_w = AXLE_WHEELS[axle][0 if d > 0 else 1]
    cold_w = AXLE_WHEELS[axle][1 if d > 0 else 0]
    other_cn = WHEEL_CN["rear" if axle == "front" else "front"]
    evidence = [temp_line,
                f"{axle_cn}左右差 {abs(d):.1f} °C（{WHEEL_CN[hot_w]}更热），{other_cn}左右差 {d_other:+.1f} °C（+ = 左边热）",
                dir_line]
    if expect == hot_side:
        evidence.append(f"偏热的一侧和赛道方向一致；扣掉{other_cn}同方向的温差后，{axle_cn}还多出 {excess:.1f} °C"
                        f"（< {r['axle_excess_c']:.0f} °C 视为赛道方向造成；≥ {r['lr_alert_c']:.0f} °C 记为严重）")
    elif expect is None:
        evidence.append(f"赛道没有明显的主方向，温差 {excess:.1f} °C 不能用赛道方向解释（≥ {r['lr_alert_c']:.0f} °C 记为严重）")
    else:
        evidence.append(f"偏热的一侧和赛道方向相反，温差 {excess:.1f} °C 不能用赛道方向解释（≥ {r['lr_alert_c']:.0f} °C 记为严重）")
    evidence.append(f"判定阈值：同轴左右差 ≥ {r['lr_diff_c']:.0f} °C 才看；赛道以某个方向为主（转角占比 ≥ {_pct(r['dominant_share'])}）"
                    f"且两轴同方向都有温差时，只看多出来的部分")
    if sev == "info":
        title = f"{axle_cn}左右胎温差（和赛道方向一致）"
        summary = f"{WHEEL_CN[hot_w]}比{WHEEL_CN[cold_w]}热 {abs(d):.1f} °C，但另一根轴也有类似温差、方向和赛道一致，多半是赛道造成的正常现象。"
        tips = ["可以不管；想均衡的话，留意这条赛道主要方向的弯里别过度推头 / 甩尾"]
    else:
        title = f"{axle_cn}左右胎温不平衡（{WHEEL_CN[hot_w]}偏热）"
        summary = (f"{WHEEL_CN[hot_w]}比{WHEEL_CN[cold_w]}热 {abs(d):.1f} °C，扣掉赛道方向的影响还多 {excess:.1f} °C："
                   f"{WHEEL_CN[hot_w]}这条胎负担明显更重。")
        tips = ["Forza 的调校左右对称，没法只调一边：先找是哪几个弯让这条胎一直当外侧重载轮（看下面转向平衡的诊断）",
                f"如果那些弯有推头（转向不足）：按转向不足的建议减轻{WHEEL_CN['front'] if axle == 'front' else WHEEL_CN['rear']}负担，比如{'前防倾杆调软一点' if axle == 'front' else '后防倾杆调软一点'}",
                f"在游戏内遥测看{axle_cn}的热胎压和胎面内 / 外温度，再决定胎压和外倾角怎么调（Data Out 看不到这些）"]
    basis = {"channels": _TIRE_BASIS_CH,
             "rule": (f"只用热胎后的数据（四轮平均胎温达到“本圈最后 {_pct(r['warm_ref_frac'])} 的中位数 − {r['warm_margin_c']:.0f} °C”之后）。"
                      f"同轴左右平均胎温差 ≥ {r['lr_diff_c']:.0f} °C 时：赛道以某方向为主且偏热的是外侧，就扣掉另一根轴同方向的温差，"
                      f"剩下的 < {r['axle_excess_c']:.0f} °C 只提示，≥ {r['lr_alert_c']:.0f} °C 严重，其余注意。"),
             "principle": ("轮胎越吃力（侧向力越大、滑得越多）越热。过弯时外侧轮胎载荷大，所以右弯多的赛道左边轮胎天然更热，"
                           "这部分是赛道造成的，不是调校问题；两根轴都会受同样的影响。只有一根轴左右差特别大，才说明这根轴的那条胎额外吃力。")}
    return _finding(f"tire_lr_{axle}", "tires", sev, title, summary, evidence,
                    [ctx.loc(i_warm, ctx.n - 1, wheel=hot_w, use_corner=False, phase="straight") | {
                        "corner": None, "phase": None, "phase_label": "全圈", "label": f"全圈（热胎后）· {WHEEL_CN[hot_w]}"}],
                    basis, tips, conf, ONE_AT_A_TIME,
                    data={"axle": axle, "diff_c": round(d, 2), "other_axle_diff_c": round(d_other, 2),
                          "excess_c": round(excess, 2), "hot_wheel": hot_w})


def _tire_axle_finding(ctx, fa, ra, hot_axle, thr_axle, only_driven, temp_line, conf, i_warm) -> dict:
    r = ctx.r["tires"]
    hot_cn = WHEEL_CN[hot_axle]
    d = fa - ra
    evidence = [temp_line, f"前轴平均 {fa:.1f} °C，后轴平均 {ra:.1f} °C，{hot_cn}高 {abs(d):.1f} °C",
                f"判定阈值：前后轴平均差 ≥ {thr_axle:.0f} °C" + (f"（偏热的是唯一的驱动轴，本来就更热，阈值已放宽 {r['driven_axle_bonus_c']:.0f} °C）" if only_driven else "")]
    if hot_axle == "front":
        tips = ["前轴负担更重，常见于转向不足、刹车平衡偏前：结合上面的转向平衡 / 刹车诊断一起看",
                "前防倾杆调软一点，或刹车平衡往后挪一点"]
    else:
        tips = ["后轴负担更重，常见于出弯空转、转向过度：结合上面的转向平衡 / 牵引诊断一起看",
                "出弯给油更柔；后防倾杆调软一点或差速器加速锁止调低一点"]
    tips.append("在游戏内遥测里确认这根轴的热胎压是否偏离目标")
    basis = {"channels": _TIRE_BASIS_CH,
             "rule": f"热胎后，前轴两轮平均 和 后轴两轮平均 差 ≥ {r['axle_diff_c']:.0f} °C（偏热的是唯一驱动轴时再放宽 {r['driven_axle_bonus_c']:.0f} °C）。",
             "principle": "一根轴明显比另一根热，说明这根轴的轮胎长期更吃力（更多滑动 / 更大载荷），整车平衡偏向让这根轴先到极限。"}
    return _finding("tire_axle_balance", "tires", "warn", f"{hot_cn}胎温明显高于另一轴",
                    f"{hot_cn}平均比另一轴热 {abs(d):.1f} °C，{hot_cn}轮胎负担更重。", evidence,
                    [ctx.loc(i_warm, ctx.n - 1, wheel=hot_axle, use_corner=False, phase="straight") | {
                        "corner": None, "phase": None, "phase_label": "全圈", "label": f"全圈（热胎后）· {hot_cn}"}],
                    basis, tips, conf, ONE_AT_A_TIME,
                    data={"front_avg_c": round(fa, 2), "rear_avg_c": round(ra, 2), "hot_axle": hot_axle})
