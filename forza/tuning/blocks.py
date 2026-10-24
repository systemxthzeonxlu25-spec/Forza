"""分析块（不是 finding，是给页面画图的量化数据）：平衡指数分布、刹车分析、车身与悬挂。"""

from __future__ import annotations

import numpy as np

from .common import AXLE_WHEELS, PHASES, PHASE_CN, WHEELS, WHEEL_CN, _Ctx, _corner_key, _pct
from .check_brakes import _near_kerb
from .check_brake_pressure import _band_defs, _band_mask, _decel_sign, _pedal_curves, _speed_edges

# ================================================================ 分析块 ①：平衡指数 B 的分布（车速分档 + 弯 × 阶段热力表）

def _fb(x: float) -> str:
    """B 值的显示：带正负号两位小数，四舍五入是 0 的不带正负号。"""
    return "0.00" if abs(x) < 0.005 else f"{x:+.2f}"


def _tendency(b: float | None, thr: float) -> str | None:
    if b is None:
        return None
    return "understeer" if b >= thr else "oversteer" if b <= -thr else "neutral"


_TEND_CN = {"understeer": "推头", "oversteer": "甩尾", "neutral": "中性", None: "样本不足"}


def _aero_line(kind: str, aero: bool | None) -> str:
    """“只在高速段推头 / 甩尾”的解读：有没有可调空力，说法完全不一样。"""
    if kind == "understeer":
        if aero is True:
            return "这车有可调空力：高速推头多半看前后下压力的比例，前翼加一点 / 后翼减一点。"
        if aero is False:
            return ("这车没有可调空力，所以不是空力能调的：高速推头多半是高速弯入弯速度太快，或者弹簧 / 防倾杆让前轴在大载荷下先饱和，"
                    "可以试前防倾杆软一点 / 后防倾杆硬一点，并检查高速时前轴有没有触底。")
        return ("如果这车装了可调空力件（前后翼），高速推头多半看前后下压力比例（前翼加一点 / 后翼减一点）；没装的话就不是空力问题，"
                "看机械平衡（防倾杆、弹簧）和入弯速度。在上面“可调空力”里选一下，结论会更具体。")
    if aero is True:
        return "这车有可调空力：高速甩尾多半是后下压力不够，后翼加一点 / 前翼减一点。"
    if aero is False:
        return ("这车没有可调空力：高速甩尾多半是后轴机械抓地不够（后防倾杆软一点 / 后弹簧软一点），"
                "或者高速弯里收油 / 带刹车让车尾变轻。")
    return ("如果这车装了可调空力件，高速甩尾多半是后下压力不够（后翼加一点）；没装的话看后轴机械抓地（后防倾杆 / 后弹簧软一点）"
            "和高速弯里有没有突然收油。")


def _balance_block(sets: list[dict], rules: dict, aero: bool | None) -> dict:
    """平衡指数 B = 前轴平均 |侧偏角| − 后轴平均 |侧偏角|（> 0 推头，< 0 甩尾）。多圈时把各圈的采样拼起来算。"""
    r, rb = rules["balance_map"], rules["balance"]
    sets = [s for s in sets if s and s["b"].size]
    method = (f"B = 前轴（左右两轮）平均 |侧偏角| − 后轴平均 |侧偏角|，B > 0 前轴更接近极限（推头），B < 0 后轴更接近极限（甩尾）。"
              f"只用在转弯的采样（横向 G ≥ {rb['min_lat_g']:.2f}）；每档平均 |B| ≥ {r['tendency_b']:.2f}，"
              f"或者这档里 |B| ≥ {rb['axle_diff']:.2f} 的时间占 ≥ {_pct(r['tendency_share'])}（且是另一方向的 2 倍以上）算偏推头 / 偏甩尾；"
              + ("车速分档按 tuning_rules.toml 里的固定边界。" if r["speed_band_edges_kmh"] else
                 f"车速分档按这些采样车速的 {'/'.join(f'{q * 100:.0f}%' for q in r['speed_band_quantiles'])} 分位数切（按这台车自己的车速，不写死）。"))
    if not sets:
        return {"available": False, "why": "数据不足：需要侧偏角、横向 G、车速、方向盘等通道，且这圈要有转弯的数据。",
                "method": method}
    w = np.concatenate([np.full(s["b"].size, s["dt"]) for s in sets])
    spd = np.concatenate([s["speed_kmh"] for s in sets])
    b = np.concatenate([s["b"] for s in sets])
    fa = np.concatenate([s["fa"] for s in sets])
    ra = np.concatenate([s["ra"] for s in sets])
    edges = _speed_edges(spd, r["speed_band_quantiles"], r["speed_band_edges_kmh"])
    bands = []
    for band in _band_defs(edges):
        m = _band_mask(spd, band)
        t_s = float(w[m].sum())
        ok = t_s >= r["min_band_s"]
        mean = float(np.average(b[m], weights=w[m])) if m.any() else None
        us = float(w[m & (b >= rb["axle_diff"])].sum() / t_s) if t_s > 0 else 0.0
        os_ = float(w[m & (b <= -rb["axle_diff"])].sum() / t_s) if t_s > 0 else 0.0
        tend = None
        if ok:
            tend = _tendency(mean, r["tendency_b"])
            if tend == "neutral" and max(us, os_) >= r["tendency_share"] and max(us, os_) >= 2 * min(us, os_):
                tend = "understeer" if us > os_ else "oversteer"
        bands.append({**band, "time_s": round(t_s, 2), "ok": ok, "b": mean if ok else None, "b_raw": mean,
                      "us_share": us, "os_share": os_,
                      "front": float(np.average(fa[m], weights=w[m])) if m.any() else None,
                      "rear": float(np.average(ra[m], weights=w[m])) if m.any() else None,
                      "speed_kmh": float(np.average(spd[m], weights=w[m])) if m.any() else None,
                      "tendency": tend})
    # 热力表：按弯道编号对齐（多圈时同一个编号合并；有固定弯道表时编号在各圈里是同一个弯）
    corner_info: dict[str, dict] = {}
    for s in sets:
        for c in s["corners"]:
            corner_info.setdefault(c["code"], c)
    rows = []
    cell_sets = [s for s in sets if s["cells"] is not None]
    if cell_sets:
        code = np.concatenate([s["cells"]["code"] for s in cell_sets])
        ph = np.concatenate([s["cells"]["phase"] for s in cell_sets])
        cb = np.concatenate([s["cells"]["b"] for s in cell_sets])
        cs = np.concatenate([s["cells"]["speed_kmh"] for s in cell_sets])
        cw = np.concatenate([np.full(s["cells"]["b"].size, s["dt"]) for s in cell_sets])
        for k in sorted(corner_info, key=_corner_key):
            info = corner_info[k]
            cells = []
            for p, pname in enumerate(PHASES):
                m = (code == k) & (ph == p)
                t_s = float(cw[m].sum())
                ok = t_s >= rb["min_phase_s"]
                mean = float(np.average(cb[m], weights=cw[m])) if m.any() else None
                cells.append({"phase": pname, "phase_label": PHASE_CN[pname], "time_s": round(t_s, 2), "ok": ok,
                              "b": mean if ok else None,
                              "speed_kmh": float(np.average(cs[m], weights=cw[m])) if m.any() else None,
                              "tendency": _tendency(mean, r["tendency_b"]) if ok else None,
                              "level": min(1.0, abs(mean) / r["color_full_b"]) if ok and mean is not None else None})
            rows.append({"code": k, "label": info.get("label") or k, "direction": info.get("direction"), "cells": cells})
    # 最明显的几格
    flat = [(row, c) for row in rows for c in row["cells"] if c["ok"] and c["tendency"] in ("understeer", "oversteer")]
    flat.sort(key=lambda x: -abs(x[1]["b"]))
    top = [{"code": row["code"], "label": row["label"], "phase": c["phase"], "phase_label": c["phase_label"], "b": c["b"],
            "tendency": c["tendency"], "speed_kmh": c["speed_kmh"]} for row, c in flat[:int(r["top_cells"])]]
    notes = _balance_reading(bands, top, r, aero)
    sources = {s["corner_source"] for s in sets}
    align_note = None
    if len(sets) > 1 and "layout" not in sources:
        align_note = "各圈的弯道是各自识别的，编号可能对不上（同一个编号未必是同一个弯）；赛道有了固定弯道表后会按表对齐。"
    elif len(sets) > 1 and len(sources) > 1:
        align_note = "有的圈用了固定弯道表、有的圈是自己识别的弯道（圈长和表差太多），这些圈的弯道编号可能对不上。"
    return {"available": True, "method": method, "edges_kmh": edges, "bands": bands, "rows": rows,
            "phases": [{"phase": p, "label": PHASE_CN[p]} for p in PHASES], "top_cells": top, "notes": notes,
            "tendency_b": r["tendency_b"], "color_full_b": r["color_full_b"], "min_band_s": r["min_band_s"],
            "min_cell_s": rb["min_phase_s"], "aero": aero, "laps": len(sets), "corner_source": sorted(sources),
            "align_note": align_note,
            "legend": "蓝 = 推头（B > 0，前轴先到极限），红 = 甩尾（B < 0，后轴先到极限），灰 = 这一格样本太少不评价"}


def _balance_reading(bands: list[dict], top: list[dict], r: dict, aero: bool | None) -> list[str]:
    ok = [b for b in bands if b["ok"]]
    out: list[str] = []
    if len(ok) < 2:
        out.append(f"车速分档的数据不够（每档至少 {r['min_band_s']:.1f} s 转弯数据），先看下面的热力表。")
    else:
        lo, hi = ok[0], ok[-1]
        tend = [b["tendency"] for b in ok]
        def _band_txt(b: dict) -> str:
            extra = "".join(f"，{_pct(x)} 的时间明显{w}" for x, w in ((b["us_share"], "推头"), (b["os_share"], "甩尾")) if x >= 0.05)
            return f"{b['name']}（{b['range']}）B {_fb(b['b'])}{extra} → {_TEND_CN[b['tendency']]}"
        desc = "；".join(_band_txt(b) for b in ok)
        out.append(f"按车速分档：{desc}。")
        for kind, word in (("understeer", "推头"), ("oversteer", "甩尾")):
            if all(t == kind for t in tend):
                out.append(f"各个车速段平均都偏{word}：整体平衡偏{word}，和车速关系不大，先看机械平衡"
                           f"（{'前防倾杆软一点 / 后防倾杆硬一点' if kind == 'understeer' else '后防倾杆软一点 / 前防倾杆硬一点'}、弹簧、差速器）。")
                break
            if hi["tendency"] == kind and lo["tendency"] != kind:
                out.append(f"只在高速段{word}，低速段不{word}：" + _aero_line(kind, aero))
                break
            if lo["tendency"] == kind and hi["tendency"] != kind:
                out.append(f"只在低速段{word}：低速弯里空力作用很小，多半是机械抓地——"
                           + ("前防倾杆软一点 / 后防倾杆硬一点，出弯推头时再看差速器加速锁止。" if kind == "understeer" else
                              "后防倾杆软一点、出弯给油柔一点，出弯甩尾时再看差速器加速锁止。"))
                break
        else:
            if all(t == "neutral" for t in tend):
                out.append(f"各个车速段平均都接近中性（|B| < {r['tendency_b']:.2f}）：整体平衡没有明显偏向，问题多半在个别弯，看下面的热力表。")
    if top:
        out.append("最明显的格子：" + "、".join(f"{c['label']} {c['phase_label']} B = {_fb(c['b'])}（{_TEND_CN[c['tendency']]}）" for c in top) + "。")
    out.append("B 是平均值，只说明哪根轴更接近极限；是否真的过了极限、该不该改，看上面的“转向平衡”诊断。")
    return out


# ================================================================ 分析块 ②：刹车（直线重刹前后轴对比 + 踏板-减速度曲线）

def _brake_block(zone_sets: list[list[dict]], pedal_sets: list[dict], rules: dict, abs_on: bool | None) -> dict:
    zones = [z for zs in zone_sets for z in (zs or [])]
    curves = _pedal_curves(pedal_sets, rules["brake_pressure"])
    if not zones and not curves["bands"]:
        return {"available": False, "why": "数据不足：没有直线刹车的数据（或者缺少滑移率 / 纵向 G 通道）。", "abs_on": abs_on}
    rbb = rules["brake_balance"]
    ev = [z for z in zones if z["bias"] is not None]
    counts = {k: sum(1 for z in ev if z["bias"] == k) for k in ("front", "rear", "balanced")}
    coast = [s for s in pedal_sets if s]
    notes = []
    if ev:
        notes.append(f"{len(ev)} 段够重的直线重刹：前轴先饱和 {counts['front']} 段、后轴先饱和 {counts['rear']} 段、前后差不多 {counts['balanced']} 段。")
    for b in curves["bands"]:
        if b["status"] == "saturated":
            notes.append(f"{b['name']}（{b['range']}）踩到约 {_pct(b['sat_pedal'])} 减速度就不再增加。")
        elif b["status"] == "late":
            notes.append(f"{b['name']}（{b['range']}）踩到约 {_pct(b['sat_pedal'])} 才到最大减速度，踏板行程用得比较满。")
        elif b["status"] == "linear":
            notes.append(f"{b['name']}（{b['range']}）踩到 {_pct(b['max_pedal'])} 减速度还在增加。")
    if abs_on is True:
        notes.append("你勾选了“我开了 ABS”：前后对比和饱和点的区分度会下降，饱和点更可能是 ABS 介入的位置。")
    elif abs_on is None:
        notes.append("Data Out 看不到 ABS 状态；开了 ABS 的话请在上面勾选“我开了 ABS”，结论会按开着 ABS 来解读。")
    return {"available": True, "abs_on": abs_on, "slip_mark": rbb["slip_mark"], "zones": zones, "counts": counts,
            "pedal": curves, "coast_fitted": bool(coast) and all(s["coast"] for s in coast), "notes": notes}


# ================================================================ 分析块 ③：车身与悬挂

def _deriv(a: np.ndarray, t: np.ndarray, max_gap: float) -> np.ndarray:
    """中心差分求导（每秒）。两侧时间间隔 ≤ 0（重复时间戳）或 > max_gap（丢包）的点给 NaN，首尾也是 NaN。"""
    out = np.full(a.size, np.nan)
    if a.size < 3:
        return out
    gap = np.diff(t)
    ok = (gap[:-1] > 0) & (gap[1:] > 0) & (gap[:-1] <= max_gap) & (gap[1:] <= max_gap)
    den = t[2:] - t[:-2]
    with np.errstate(divide="ignore", invalid="ignore"):
        d = (a[2:] - a[:-2]) / den
    out[1:-1] = np.where(ok, d, np.nan)
    return out


def _body_samples(ctx: _Ctx) -> dict | None:
    """车身与悬挂的采样（剔除压路肩前后 kerb_window_s 秒、低速）。缺通道的部分就是 None。"""
    r = ctx.r["body"]
    if ctx.missing([f"susp_{w}" for w in WHEELS]) and ctx.missing([f"susp_m_{w}" for w in WHEELS]):
        return None
    kerb = _near_kerb(ctx, r["kerb_window_s"])
    keep = ~kerb
    if ctx.dash:
        keep &= ctx.speed_ok
    out: dict = {"dt": ctx.dt, "kerb_s": float(kerb.sum() * ctx.dt)}
    out["norm"] = None if ctx.missing([f"susp_{w}" for w in WHEELS]) else {w: ctx.ch(f"susp_{w}")[keep] for w in WHEELS}
    out["vel"] = out["roll"] = out["pitch"] = out["ref"] = None
    out["sign_flipped"] = False
    glat = ctx.ch("g_lat")
    glong = ctx.ch("g_long")
    if not ctx.missing([f"susp_m_{w}" for w in WHEELS]):
        mm = {}
        flipped = False
        for w in WHEELS:
            a = ctx.ch(f"susp_m_{w}") * 1000.0
            nrm = ctx.ch(f"susp_{w}")
            # susp_m 的方向用归一化行程（0 = 伸到最长，1 = 压到底）校正：负相关就翻过来，保证“变大 = 压缩”
            if nrm is not None and np.std(a) > 0 and np.std(nrm) > 0 and np.corrcoef(a, nrm)[0, 1] < 0:
                a = -a
                flipped = True
            mm[w] = a
        out["sign_flipped"] = flipped
        # 悬挂速度：中心差分；两边采样间隔超过 max_gap_factor × 典型间隔（丢包）的点不要
        vel = {w: _deriv(mm[w], ctx.t, r["max_gap_factor"] * ctx.dt) for w in WHEELS}
        vk = keep & np.isfinite(vel["fl"])
        if vk.any():
            out["vel"] = {w: vel[w][vk] for w in WHEELS}
        # 侧倾：稳态弯（|横向 G| 够大、变化慢）里每根轴左右行程差 vs 横向 G（带正负号拟合斜率，取绝对值；
        # 再加上直线上横向 G 很小的采样定截距，扣掉静态的左右不对称）
        if glat is not None:
            sel = _roll_mask(ctx, glat, keep, r)
            if sel is not None:
                out["roll"] = {"g": glat[sel], "front": (mm["fl"] - mm["fr"])[sel], "rear": (mm["rl"] - mm["rr"])[sel],
                               "steady": (np.abs(glat) >= r["roll_min_lat_g"])[sel]}
        # 点头 / 抬头：前轴平均行程 − 后轴平均行程 vs 纵向 G（刹车、加速各一个斜率，共用截距）
        if glong is not None and glat is not None and ctx.dash:
            pitch_mm = (mm["fl"] + mm["fr"]) / 2 - (mm["rl"] + mm["rr"]) / 2
            out["pitch"] = _pitch_parts(ctx, pitch_mm, glong, glat, keep, r)
    # 参考：roll / pitch 字段（世界坐标姿态角，含倾斜 / 坡度）
    if ctx.missing(("roll", "pitch")):
        out["ref"] = {"missing": True}
    elif glat is not None:
        roll_deg = np.degrees(ctx.ch("roll"))
        pitch = ctx.ch("pitch")
        grade_fixed = False
        if not ctx.missing(("pos_y",)) and ctx.n > 10 and np.ptp(ctx.dist) > 50:
            # 坡度 = 前后各 grade_smooth_m/2 米处的高度差 / 距离差（按距离取窗口，和车速无关）
            d = np.maximum.accumulate(ctx.dist)
            y = ctx.ch("pos_y")
            half = r["grade_smooth_m"] / 2
            i0 = np.clip(np.searchsorted(d, d - half, side="left"), 0, ctx.n - 1)
            i1 = np.clip(np.searchsorted(d, d + half, side="right") - 1, 0, ctx.n - 1)
            dd = d[i1] - d[i0]
            grade = np.arctan(np.divide(y[i1] - y[i0], dd, out=np.zeros(ctx.n), where=dd > 1.0))
            # pitch 和坡度的正负号约定不一定一致：按相关性定号再扣
            if np.std(grade) > 1e-4 and np.std(pitch) > 1e-6:
                sgn = 1.0 if np.corrcoef(pitch, grade)[0, 1] >= 0 else -1.0
                pitch = pitch - sgn * grade
                grade_fixed = True
        pitch_deg = np.degrees(pitch)
        sel = _roll_mask(ctx, glat, keep, r)
        ref = {"missing": False, "grade_fixed": grade_fixed, "roll": None, "pitch": None}
        if sel is not None:
            ref["roll"] = {"g": glat[sel], "front": roll_deg[sel], "steady": (np.abs(glat) >= r["roll_min_lat_g"])[sel]}
        if glong is not None and ctx.dash:
            ref["pitch"] = _pitch_parts(ctx, pitch_deg, glong, glat, keep, r)
        out["ref"] = ref
    return out


def _roll_mask(ctx: _Ctx, glat: np.ndarray, keep: np.ndarray, r: dict) -> np.ndarray | None:
    """侧倾拟合用的采样：稳态弯（|横向 G| ≥ roll_min_lat_g、横向 G 变化慢）+ 横向 G 很小的直线（定截距）。"""
    dg = np.abs(_deriv(glat, ctx.t, r["max_gap_factor"] * ctx.dt))
    steady = keep & (np.abs(glat) >= r["roll_min_lat_g"]) & (dg <= r["steady_max_dg_s"])
    if not steady.any():
        return None
    return steady | (keep & (np.abs(glat) <= 0.5 * r["pitch_max_lat_g"]) & (dg <= r["steady_max_dg_s"]))


def _pitch_parts(ctx: _Ctx, val: np.ndarray, glong: np.ndarray, glat: np.ndarray, keep: np.ndarray, r: dict) -> dict:
    """点头 / 抬头拟合用的直线采样：val（前后行程差或俯仰角）、刹车减速度 dec（≥ 0）、加速度 acc（≥ 0）。"""
    sgn = _decel_sign(glong, ctx.ch("brake"))
    decel = glong * sgn                        # 刹车 = 正
    straight = keep & (np.abs(glat) <= r["pitch_max_lat_g"])
    return {"v": val[straight], "dec": np.clip(decel[straight], 0, None), "acc": np.clip(-decel[straight], 0, None)}


def _fit_roll(parts: list, key: str, dts: list[float], min_s: float) -> tuple[float | None, float]:
    """val ≈ c + k·横向G（带正负号）。返回 (|k| 每 G, 稳态弯数据秒数)；稳态弯数据不足 min_s 秒返回 None。"""
    use = [(p, dt) for p, dt in zip(parts, dts) if p is not None and p["g"].size]
    t = sum(float(p["steady"].sum()) * dt for p, dt in use)
    if not use or t < min_s:
        return None, t
    g = np.concatenate([p["g"] for p, _ in use])
    v = np.concatenate([p[key] for p, _ in use])
    A = np.stack([np.ones_like(g), g], axis=1)
    try:
        (_, k), *_ = np.linalg.lstsq(A, v, rcond=None)
    except np.linalg.LinAlgError:
        return None, t
    return abs(float(k)), t


def _fit_pitch(parts: list, dts: list[float], min_g: float, min_s: float) -> dict:
    """val ≈ c + kd·刹车减速度 + ka·加速度。各斜率要有 ≥ min_s 秒、|G| ≥ min_g 的数据才给（取绝对值）。"""
    use = [(p, dt) for p, dt in zip(parts, dts) if p is not None and p["v"].size]
    out = {"dive": None, "dive_s": 0.0, "squat": None, "squat_s": 0.0}
    if not use:
        return out
    v = np.concatenate([p["v"] for p, _ in use])
    dec = np.concatenate([p["dec"] for p, _ in use])
    acc = np.concatenate([p["acc"] for p, _ in use])
    w = np.concatenate([np.full(p["v"].size, dt) for p, dt in use])
    out["dive_s"] = float(w[dec >= min_g].sum())
    out["squat_s"] = float(w[acc >= min_g].sum())
    cols = [np.ones_like(v)]
    names = []
    for nm, x in (("dive", dec), ("squat", acc)):
        if out[f"{nm}_s"] >= min_s:
            cols.append(x)
            names.append(nm)
    if not names:
        return out
    try:
        coef, *_ = np.linalg.lstsq(np.stack(cols, axis=1), v, rcond=None)
    except np.linalg.LinAlgError:
        return out
    for nm, k in zip(names, coef[1:]):
        out[nm] = abs(float(k))
    return out


def _body_block(sets: list[dict], rules: dict) -> dict:
    r = rules["body"]
    sets = [s for s in sets if s]
    caveats = [f"采样最高约 60 Hz：悬挂速度的高速段（快速冲击，> {r['damper_split_mm_s']:.0f} mm/s 那部分）不准，只适合比较改调校前后的相对变化。",
               "这些数字没有“标准答案”，用来比较改调校前后：防倾杆调硬 → 侧倾 mm/g 变小；弹簧调硬 → 点头 mm/g 变小、p99 降低；"
               "阻尼调硬 → 悬挂速度分布变窄。",
               f"已剔除压路肩前后 {r['kerb_window_s']:.2f} s 和低速的采样。"]
    if not sets:
        return {"available": False, "why": "数据不足：没有悬挂行程通道。", "caveats": caveats}
    dts = [s["dt"] for s in sets]
    out: dict = {"available": True, "laps": len(sets), "caveats": caveats, "notes": []}
    notes = out["notes"]
    # ---- 行程分位数
    norm_sets = [s for s in sets if s["norm"] is not None]
    if norm_sets:
        wheels = {}
        n_all = sum(s["norm"]["fl"].size for s in norm_sets)
        if n_all >= r["min_samples"]:
            for w in WHEELS:
                a = np.concatenate([s["norm"][w] for s in norm_sets])
                p1, p50, p99 = (float(np.percentile(a, q)) for q in (1, 50, 99))
                wheels[w] = {"p1": p1, "p50": p50, "p99": p99, "near_bottom": float(np.mean(a >= r["near_bottom"])),
                             "near_top": float(np.mean(a <= r["near_top"]))}
            out["travel"] = {"wheels": wheels, "samples": n_all, "near_bottom": r["near_bottom"], "near_top": r["near_top"]}
            for w, x in wheels.items():
                if x["p99"] >= r["near_bottom"]:
                    notes.append(f"{WHEEL_CN[w]}行程 p99 = {x['p99']:.2f}（≥ {r['near_bottom']:.2f}）：行程基本用满了，再软 / 再低就会触底。")
                if x["p1"] <= r["near_top"]:
                    notes.append(f"{WHEEL_CN[w]}行程 p1 = {x['p1']:.2f}（≤ {r['near_top']:.2f}）：会伸到最长（可能短暂离地）。")
            f50 = (wheels["fl"]["p50"] + wheels["fr"]["p50"]) / 2
            r50 = (wheels["rl"]["p50"] + wheels["rr"]["p50"]) / 2
            notes.insert(0, f"行程中位数：前轴 {f50:.2f}、后轴 {r50:.2f}（0 = 伸到最长，1 = 压到底）。")
        else:
            out["travel"] = None
            notes.append(f"悬挂行程采样太少（{n_all} 个，至少 {r['min_samples']} 个），不算分位数。")
    else:
        out["travel"] = None
    # ---- 悬挂速度直方图
    vel_sets = [s for s in sets if s["vel"] is not None]
    if vel_sets and sum(s["vel"]["fl"].size for s in vel_sets) >= r["min_samples"]:
        step, vmax, split = r["hist_bin_mm_s"], r["hist_max_mm_s"], r["damper_split_mm_s"]
        edges = np.arange(-vmax, vmax + step / 2, step)
        hist, classes, p95 = {}, {}, {}
        for axle, ws in AXLE_WHEELS.items():
            a = np.concatenate([s["vel"][w] for s in vel_sets for w in ws])
            h, _ = np.histogram(np.clip(a, -vmax + 1e-6, vmax - 1e-6), bins=edges)
            hist[axle] = (h / max(1, a.size)).tolist()
        for w in WHEELS:
            a = np.concatenate([s["vel"][w] for s in vel_sets])
            classes[w] = {"rebound_fast": float(np.mean(a <= -split)), "rebound_slow": float(np.mean((a < 0) & (a > -split))),
                          "bump_slow": float(np.mean((a >= 0) & (a < split))), "bump_fast": float(np.mean(a >= split))}
            p95[w] = float(np.percentile(np.abs(a), 95))
        out["damper"] = {"edges_mm_s": edges.tolist(), "hist": hist, "classes": classes, "p95_abs_mm_s": p95,
                         "split_mm_s": split, "sign_flipped": any(s["sign_flipped"] for s in vel_sets)}
        fb = (classes["fl"]["bump_fast"] + classes["fr"]["bump_fast"]) / 2
        rb_ = (classes["rl"]["bump_fast"] + classes["rr"]["bump_fast"]) / 2
        notes.append(f"悬挂速度：高速压缩（≥ {split:.0f} mm/s）占前轴 {_pct(fb)}、后轴 {_pct(rb_)} 的时间；"
                     f"|速度| p95 前轴约 {(p95['fl'] + p95['fr']) / 2:.0f} mm/s、后轴约 {(p95['rl'] + p95['rr']) / 2:.0f} mm/s。")
    else:
        out["damper"] = None
        if not vel_sets:
            notes.append("没有悬挂行程（米）通道，算不了悬挂速度和侧倾 / 点头梯度。")
    # ---- 侧倾 / 点头梯度（悬挂行程差，相对车身，不受赛道倾斜影响）
    kf, tf = _fit_roll([s["roll"] for s in sets], "front", dts, r["min_fit_s"])
    kr, _ = _fit_roll([s["roll"] for s in sets], "rear", dts, r["min_fit_s"])
    if kf is not None and kr is not None:
        share = kf / (kf + kr) if kf + kr > 0 else None
        out["roll"] = {"front_mm_per_g": kf, "rear_mm_per_g": kr, "front_share": share, "time_s": round(tf, 1),
                       "min_lat_g": r["roll_min_lat_g"]}
        notes.append(f"侧倾（稳态弯 |横向 G| ≥ {r['roll_min_lat_g']:.1f}，{tf:.0f} s 数据）：前轴左右行程差 {kf:.1f} mm/g、后轴 {kr:.1f} mm/g"
                     + (f"，前轴占 {_pct(share)}。" if share is not None else "。"))
    else:
        out["roll"] = None
    pf = _fit_pitch([s["pitch"] for s in sets], dts, r["pitch_min_long_g"], r["min_fit_s"])
    out["pitch"] = None
    if pf["dive"] is not None or pf["squat"] is not None:
        out["pitch"] = {"dive_mm_per_g": pf["dive"], "dive_s": round(pf["dive_s"], 1), "squat_mm_per_g": pf["squat"],
                        "squat_s": round(pf["squat_s"], 1), "min_long_g": r["pitch_min_long_g"]}
        parts = []
        if pf["dive"] is not None:
            parts.append(f"刹车点头 {pf['dive']:.1f} mm/g（{pf['dive_s']:.0f} s 数据）")
        if pf["squat"] is not None:
            parts.append(f"加速抬头 {pf['squat']:.1f} mm/g（{pf['squat_s']:.0f} s 数据）")
        notes.append(f"点头 / 抬头（直线、|纵向 G| ≥ {r['pitch_min_long_g']:.1f}，前轴平均行程 − 后轴平均行程）：" + "，".join(parts) + "。")
    # ---- roll / pitch 字段（参考）
    use = [(s["ref"], s["dt"]) for s in sets if s["ref"] is not None and not s["ref"].get("missing")]
    if not use:
        out["attitude_ref"] = {"available": False,
                               "why": "这圈没有 roll / pitch 通道（老版本录的圈没存，新录的圈才有），只用悬挂行程算。"}
    else:
        kroll, troll = _fit_roll([x["roll"] for x, _ in use], "front", [d for _, d in use], r["min_fit_s"])
        pr = _fit_pitch([x["pitch"] for x, _ in use], [d for _, d in use], r["pitch_min_long_g"], r["min_fit_s"])
        fixed = all(x["grade_fixed"] for x, _ in use)
        out["attitude_ref"] = {"available": True, "roll_deg_per_g": kroll, "roll_s": round(troll, 1),
                               "dive_deg_per_g": pr["dive"], "dive_s": round(pr["dive_s"], 1),
                               "squat_deg_per_g": pr["squat"], "squat_s": round(pr["squat_s"], 1), "grade_fixed": fixed,
                               "why": ("roll / pitch 是车在世界坐标里的姿态角：roll 里混着弯道倾斜和路面横坡（扣不掉），"
                                       "pitch 里混着路面坡度（" + ("已用高度对距离的变化大致扣掉" if fixed else "没有高度数据，没扣") +
                                       "），所以只作参考，以上面的悬挂行程为准。")}
    return out
