"""两圈对比：按赛道距离对齐两圈，逐弯算出“慢在哪、为什么慢”。

纯计算模块（不碰网页 / 数据库），输入两个 LapData，输出一个能直接 json.dumps 的 dict。
网页上的曲线、弯道表、赛道图用的全是这里的结果。

用法：
    from forza.analysis import compare
    res = compare(best_lap, my_lap)       # ref = 参考圈（通常是最快圈），other = 要分析的圈
    res["ranking"][0]                     # 丢时间最多的一段
    res["segments"][i]["explanation"]     # “T3：慢 0.51 s。刹车点早 25 m，弯心最低速低 7 km/h……”

约定：所有“差值”都是 other 相对 ref 说的：时间差 > 0 = other 慢；
刹车点差 > 0 = other 刹得早；全油门点差 > 0 = other 给油晚；速度差 < 0 = other 慢。
想调“多大的差别才值得说”，改下面的阈值常量即可。
"""
from __future__ import annotations

import math

import numpy as np

from .corners import BRAKE_ON, BRAKE_SEARCH_M, FULL_THROTTLE, detect_corners, resample_by_distance
from .lapdata import LapData

# ---------------------------------------------------------------- 分段
PRE_BRAKE_MARGIN_M = 10.0     # 段起点放在（两圈里更早的）刹车点之前这么多米
NO_BRAKE_LEAD_M = 50.0        # 两圈都没踩刹车的弯：段起点 = 弯起点往前这么多米
BRAKE_GAP_M = 6.0             # 刹车中间短暂松开不超过这么多米，仍算同一次刹车
THROTTLE_SEARCH_M = 400.0     # 弯心之后最多往后找这么远的全油门点

# ---------------------------------------------------------------- 解释句阈值（差别小于这些就不提）
TIME_EQUAL_S = 0.03           # 一段的时间差小于这个：基本持平
BRAKE_POINT_MIN_M = 5.0       # 刹车点差
FULL_THROTTLE_MIN_M = 8.0     # 全油门点差
MIN_SPEED_MIN_KMH = 2.0       # 弯心最低速差
ENTRY_SPEED_MIN_KMH = 3.0     # 刹车前车速差
EXIT_SPEED_MIN_KMH = 3.0      # 出弯速度差
BRAKE_PEAK_MIN = 0.10         # 最大刹车力度差（0~1，即 10 个百分点）
STANDING_START_KMH = 10.0     # 过线车速低于这个算站立起步
SUMMARY_MIN_S = 0.05          # 总结里“主要丢在 / 赚在”只列时间差超过这个的段
SUMMARY_TOP_N = 3

# ---------------------------------------------------------------- 其它
LENGTH_WARN_RATIO = 0.03      # 两圈长度相差超过 3% 就提示可能不是同一条路线
LOSS_RATE_SMOOTH_M = 30.0     # 赛道图着色用的“丢时间速率”平滑窗口
MAP_MAX_POINTS = 1200         # 赛道图最多这么多个点
MIN_SAMPLES = 10              # 少于这么多采样点的圈没法对比
MS_TO_KMH = 3.6

# 曲线里输出的通道（名字, 小数位）。speed 输出时换成 km/h
CURVE_CHANNELS: list[tuple[str, int]] = [
    ("speed", 2), ("throttle", 3), ("brake", 3), ("steer", 3), ("gear", 0), ("rpm", 0), ("t", 3),
]
UNITS = {"dist": "m", "speed": "km/h", "throttle": "0~1", "brake": "0~1", "steer": "-1~1（+ = 右）",
         "gear": "挡", "rpm": "rpm", "t": "s", "delta_t": "s（+ = 对比圈慢）",
         "loss_rate": "s/100 m（+ = 对比圈在这里丢时间）"}

# 圈摘要里带上的元数据字段
_SUMMARY_KEYS = ["id", "lap_number", "lap_time", "kind", "is_complete", "flags", "track_id", "track_name",
                 "car_ordinal", "car_name", "car_class", "car_pi", "game", "family", "distance_m",
                 "created_at", "note", "session_id", "time_source"]


# ================================================================ 对外接口

def compare(ref: LapData, other: LapData, step: float = 2.0) -> dict:
    """对比两圈。ref = 参考圈（通常是最快圈），other = 要分析的圈。

    返回（全部可 JSON 序列化，没有 NaN）：
        step, length_m, scale, warnings[], units{},
        ref{圈摘要}, other{圈摘要},
        channels{dist[], ref{speed,throttle,brake,steer,gear,rpm,t}, other{同}, delta_t[], loss_rate[]},
        segments[{name, kind, 时间差、刹车点、最低速……, explanation}]   按赛道顺序
        ranking[{name, kind, time_delta_s, explanation}]                 按丢时间从多到少
        totals{delta_t_end_s, lap_time_delta_s, lost_s, gained_s, summary}
        map{dist[], x[], z[], loss_rate[], corners[{name, x, z, apex_m}], start{x, z}} 或 None
    """
    if step <= 0:
        raise ValueError("step 必须大于 0")
    for name, lap in (("参考圈", ref), ("对比圈", other)):
        if len(lap) < MIN_SAMPLES or not lap.has("dist") or not lap.has("t"):
            raise ValueError(f"{name}数据太少，没法对比")

    ref_len = _lap_length(ref)
    other_len = _lap_length(other)
    if ref_len < step * 5 or other_len < step * 5:
        raise ValueError("圈的距离太短，没法对比")

    # ---- 按距离对齐 ----
    # 两圈的长度永远不会完全一样：走线不同（内线外线）、过线时刻落在两帧之间，都会差几米到几十米。
    # 如果直接拿各自的“圈内距离”对齐，差值会一路累积：同一个弯在两圈里的距离越往后错得越多，
    # 曲线叠在一起就对不上弯。所以先把 other 的距离按 ref_len / other_len 等比例缩放到参考圈长度，
    # 再把两圈重采样到同一张等间距网格上：起点、终点严格对齐，中间按比例对齐，
    # 剩下的误差只来自局部走线差异（一般只有几米）。
    scale = ref_len / other_len
    other_scaled = LapData(meta=other.meta, ch={**other.ch, "dist": other["dist"] * scale})
    names = [c for c, _ in CURVE_CHANNELS]
    map_names = ["pos_x", "pos_z"]
    rs_ref = resample_by_distance(ref, step, names + map_names, length=ref_len)
    rs_oth = resample_by_distance(other_scaled, step, names, length=ref_len)
    grid = rs_ref["dist"]
    for rs in (rs_ref, rs_oth):
        for c in names:
            if c not in rs:                      # 老数据可能缺通道：补 0，不让整个对比失败
                rs[c] = np.zeros_like(grid)
        rs["gear"] = np.round(rs["gear"])        # 挡位插值后会出现 3.5 挡，取整
        rs["speed_kmh"] = rs["speed"] * MS_TO_KMH

    delta_t = rs_oth["t"] - rs_ref["t"]
    loss_rate = _smooth(np.gradient(delta_t, grid) * 100.0, max(1, int(round(LOSS_RATE_SMOOTH_M / step))))

    # ---- 弯道 + 分段 ----
    corners = detect_corners(ref)
    segments = _build_segments(corners, grid, rs_ref, rs_oth, delta_t, step)
    ranking = [{"name": s["name"], "kind": s["kind"], "time_delta_s": s["time_delta_s"],
                "explanation": s["explanation"]}
               for s in sorted(segments, key=lambda s: s["time_delta_s"], reverse=True)]

    lap_time_delta = None
    if _num(other.meta.get("lap_time")) is not None and _num(ref.meta.get("lap_time")) is not None:
        lap_time_delta = round(float(other.meta["lap_time"]) - float(ref.meta["lap_time"]), 3)
    totals = {
        "delta_t_end_s": _r(delta_t[-1], 3),
        "lap_time_delta_s": lap_time_delta,
        "lost_s": round(sum(s["time_delta_s"] for s in segments if s["time_delta_s"] > 0), 3),
        "gained_s": round(sum(s["time_delta_s"] for s in segments if s["time_delta_s"] < 0), 3),
    }
    totals["summary"] = _summary(lap_time_delta if lap_time_delta is not None else float(delta_t[-1]), segments)

    warnings = _warnings(ref, other, ref_len, other_len, corners)

    out = {
        "step": step,
        "length_m": round(ref_len, 1),
        "other_length_m": round(other_len, 1),
        "scale": round(scale, 5),
        "warnings": warnings,
        "units": UNITS,
        "ref": lap_summary(ref),
        "other": lap_summary(other),
        "channels": {
            "dist": _clean(grid, 2),
            "ref": _curve_dict(rs_ref),
            "other": _curve_dict(rs_oth),
            "delta_t": _clean(delta_t, 4),
            "loss_rate": _clean(loss_rate, 4),
        },
        "segments": segments,
        "ranking": ranking,
        "totals": totals,
        "map": _track_map(rs_ref, grid, loss_rate, segments),
    }
    return out


def lap_summary(lap: LapData) -> dict:
    """圈的简要信息（给对比结果和网页标题用）。"""
    m = lap.meta
    return {k: jsonable(m.get(k)) for k in _SUMMARY_KEYS}


# ================================================================ 分段与逐弯指标

def _build_segments(corners: list[dict], grid: np.ndarray, rs_ref: dict, rs_oth: dict,
                    delta_t: np.ndarray, step: float) -> list[dict]:
    """把一圈切成段：每个弯一段，范围 = 刹车区 → 弯 → 弯后直道（到下一个弯的段起点为止）。

    第一个弯之前的那截（起点直道）单独成一段 "起点"。所有段首尾相接、覆盖整圈，
    所以各段时间差加起来 = 整圈时间差。
    """
    n = len(grid)
    L = float(grid[-1])

    def idx(d: float) -> int:
        return int(min(n - 1, max(0, round(d / step))))

    # 1) 每个弯：两圈各自的刹车点（同一套规则，在同一张距离网格上找，才好比）
    infos = []
    prev_apex_i = 0
    for c in corners:
        a_i, s_i, e_i = idx(c["apex_m"]), idx(c["start_m"]), idx(c["end_m"])
        lo = max(prev_apex_i + 1 if infos else 0, s_i - int(BRAKE_SEARCH_M / step))
        b_ref = _brake_point(rs_ref["brake"], lo, a_i, step)
        b_oth = _brake_point(rs_oth["brake"], lo, a_i, step)
        cands = [b for b in (b_ref, b_oth) if b is not None]
        if cands:
            raw = min(min(cands), s_i) * step - PRE_BRAKE_MARGIN_M
        else:
            raw = c["start_m"] - NO_BRAKE_LEAD_M
        # 段起点不能早于上一个弯的弯心（连续弯 / 减速弯），也不能晚于本弯起点
        low = (prev_apex_i + 1) * step if infos else 0.0
        seg_start = min(max(raw, low), c["start_m"])
        infos.append({"c": c, "apex_i": a_i, "start_i": s_i, "end_i": e_i,
                      "b_ref": b_ref, "b_oth": b_oth, "seg_start": seg_start})
        prev_apex_i = a_i

    # 2) 段的边界
    bounds: list[tuple[str, dict | None, float, float]] = []
    first_start = infos[0]["seg_start"] if infos else L
    if first_start > step * 0.5:
        bounds.append(("start", None, 0.0, first_start))
    for k, info in enumerate(infos):
        end = infos[k + 1]["seg_start"] if k + 1 < len(infos) else L
        bounds.append(("corner", info, info["seg_start"], end))

    def dt_at(d: float) -> float:
        return float(np.interp(d, grid, delta_t))

    segs = []
    for kind, info, s0, s1 in bounds:
        seg = {"name": "起点" if kind == "start" else info["c"]["name"], "kind": kind,
               "seg_start_m": round(s0, 1), "seg_end_m": round(s1, 1),
               "time_delta_s": round(dt_at(s1) - dt_at(s0), 3)}
        if kind == "start":
            seg.update(_start_metrics(rs_ref, rs_oth, idx(s1)))
            seg["label"] = f"起点→{infos[0]['c']['name']}" if infos else "整圈"
        else:
            seg.update(_corner_metrics(info, rs_ref, rs_oth, idx(s0), idx(s1), step))
            seg["label"] = seg["name"]
        seg["clauses"] = _clauses(seg)
        seg["explanation"] = _explain(seg)
        segs.append(seg)
    return segs


def _brake_point(brake: np.ndarray, lo: int, apex_i: int, step: float) -> int | None:
    """从弯心往回找：离弯心最近的那次刹车的起点（网格下标）。中间短暂松开（< BRAKE_GAP_M）不算断开。"""
    i = apex_i
    while i > lo and brake[i] < BRAKE_ON:
        i -= 1
    if brake[i] < BRAKE_ON:
        return None
    gap_max = max(1, int(round(BRAKE_GAP_M / step)))
    start = i
    j = i - 1
    gap = 0
    while j >= lo:
        if brake[j] >= BRAKE_ON:
            start = j
            gap = 0
        else:
            gap += 1
            if gap > gap_max:
                break
        j -= 1
    return start


def _full_throttle_point(thr: np.ndarray, from_i: int, to_i: int) -> int | None:
    """从 from_i 往后找第一次全油门（网格下标）。"""
    hit = np.nonzero(thr[from_i:to_i + 1] >= FULL_THROTTLE)[0]
    return from_i + int(hit[0]) if len(hit) else None


def _corner_metrics(info: dict, rs_ref: dict, rs_oth: dict, s0_i: int, s1_i: int, step: float) -> dict:
    c = info["c"]
    a_i, s_i, e_i = info["apex_i"], info["start_i"], info["end_i"]
    b_ref, b_oth = info["b_ref"], info["b_oth"]
    out: dict = {
        "index": c["index"], "direction": c["direction"],
        "start_m": round(c["start_m"], 1), "apex_m": round(c["apex_m"], 1), "end_m": round(c["end_m"], 1),
    }
    # 弯里（参考圈的弯起点~弯终点）的最低速
    v_ref, v_oth = rs_ref["speed_kmh"], rs_oth["speed_kmh"]
    lo, hi = s_i, max(s_i, e_i)
    mr = lo + int(np.argmin(v_ref[lo:hi + 1]))
    mo = lo + int(np.argmin(v_oth[lo:hi + 1]))
    out["min_speed_ref_kmh"] = _r(v_ref[mr], 1)
    out["min_speed_other_kmh"] = _r(v_oth[mo], 1)
    out["min_speed_delta_kmh"] = _r(v_oth[mo] - v_ref[mr], 1)
    out["min_speed_ref_m"] = round(mr * step, 1)
    out["min_speed_other_m"] = round(mo * step, 1)

    # 刹车点 / 刹车前车速 / 最大刹车力度
    out["brake_ref_m"] = None if b_ref is None else round(b_ref * step, 1)
    out["brake_other_m"] = None if b_oth is None else round(b_oth * step, 1)
    out["brake_diff_m"] = None if b_ref is None or b_oth is None else round((b_ref - b_oth) * step, 1)
    out["entry_speed_ref_kmh"] = None if b_ref is None else _r(v_ref[b_ref], 1)
    out["entry_speed_other_kmh"] = None if b_oth is None else _r(v_oth[b_oth], 1)
    out["entry_speed_delta_kmh"] = (None if b_ref is None or b_oth is None
                                    else _r(v_oth[b_oth] - v_ref[b_ref], 1))
    out["brake_peak_ref"] = _r(np.max(rs_ref["brake"][s0_i:a_i + 1]), 2) if a_i >= s0_i else 0.0
    out["brake_peak_other"] = _r(np.max(rs_oth["brake"][s0_i:a_i + 1]), 2) if a_i >= s0_i else 0.0

    # 全油门点（从参考圈弯心往后找，两圈用同一个起点）
    to_i = min(s1_i, a_i + int(THROTTLE_SEARCH_M / step))
    f_ref = _full_throttle_point(rs_ref["throttle"], a_i, to_i)
    f_oth = _full_throttle_point(rs_oth["throttle"], a_i, to_i)
    out["full_throttle_ref_m"] = None if f_ref is None else round(f_ref * step, 1)
    out["full_throttle_other_m"] = None if f_oth is None else round(f_oth * step, 1)
    out["full_throttle_diff_m"] = None if f_ref is None or f_oth is None else round((f_oth - f_ref) * step, 1)

    # 出弯速度：参考圈弯终点处
    out["exit_speed_ref_kmh"] = _r(v_ref[e_i], 1)
    out["exit_speed_other_kmh"] = _r(v_oth[e_i], 1)
    out["exit_speed_delta_kmh"] = _r(v_oth[e_i] - v_ref[e_i], 1)
    return out


def _start_metrics(rs_ref: dict, rs_oth: dict, s1_i: int) -> dict:
    """起点段：过线车速（飞驰圈 = 上一圈最后一个弯的出弯速度带过来的；站立起步 ≈ 0）。"""
    v_ref, v_oth = rs_ref["speed_kmh"], rs_oth["speed_kmh"]
    return {
        "line_speed_ref_kmh": _r(v_ref[0], 1),
        "line_speed_other_kmh": _r(v_oth[0], 1),
        "line_speed_delta_kmh": _r(v_oth[0] - v_ref[0], 1),
        "top_speed_ref_kmh": _r(np.max(v_ref[:s1_i + 1]), 1),
        "top_speed_other_kmh": _r(np.max(v_oth[:s1_i + 1]), 1),
    }


# ================================================================ 解释句

def _more_less(x: float, pos: str, neg: str) -> str:
    return pos if x > 0 else neg


def _clauses(seg: dict) -> list[str]:
    """把超过阈值的差别逐条写成短句（只说数字能证明的事）。顺序按开车的先后：刹车前 → 刹车 → 弯心 → 出弯。"""
    out: list[str] = []
    if seg["kind"] == "start":
        d = seg["line_speed_delta_kmh"]
        vo, vr = seg["line_speed_other_kmh"], seg["line_speed_ref_kmh"]
        if vo is not None and vr is not None and vo < STANDING_START_KMH <= vr:
            out.append(f"站立起步（参考圈过线时 {vr:.0f} km/h）")
        elif vo is not None and vr is not None and vr < STANDING_START_KMH <= vo:
            out.append(f"参考圈是站立起步（这圈过线时 {vo:.0f} km/h）")
        elif d is not None and abs(d) >= ENTRY_SPEED_MIN_KMH:
            out.append(f"过线车速{_more_less(d, '高', '低')} {abs(d):.0f} km/h")
        return out

    br, bo = seg["brake_ref_m"], seg["brake_other_m"]
    # 刹车前车速：只有两圈刹车点差不多时才说。刹车点不同时，车速差主要是“在直道上早/晚停止加速”
    # 造成的，和刹车点那句重复，单独说反而误导（像是上一个弯出弯慢了）
    d = seg["entry_speed_delta_kmh"]
    same_point = seg["brake_diff_m"] is not None and abs(seg["brake_diff_m"]) < BRAKE_POINT_MIN_M
    if d is not None and same_point and abs(d) >= ENTRY_SPEED_MIN_KMH:
        out.append(f"刹车前车速{_more_less(d, '高', '低')} {abs(d):.0f} km/h")

    if br is not None and bo is not None:
        d = seg["brake_diff_m"]
        if abs(d) >= BRAKE_POINT_MIN_M:
            out.append(f"刹车点{_more_less(d, '早', '晚')} {abs(d):.0f} m")
    elif br is not None:
        out.append("这个弯没踩刹车（参考圈踩了）")
    elif bo is not None:
        out.append("多踩了一脚刹车（参考圈没踩）")

    pr, po = seg["brake_peak_ref"], seg["brake_peak_other"]
    if br is not None and bo is not None and abs(po - pr) >= BRAKE_PEAK_MIN:
        out.append(f"刹车{_more_less(po - pr, '更重', '更轻')}（最大 {po:.0%}，参考圈 {pr:.0%}）")

    d = seg["min_speed_delta_kmh"]
    if d is not None and abs(d) >= MIN_SPEED_MIN_KMH:
        out.append(f"弯心最低速{_more_less(d, '高', '低')} {abs(d):.0f} km/h")

    fr, fo = seg["full_throttle_ref_m"], seg["full_throttle_other_m"]
    if fr is not None and fo is not None:
        d = seg["full_throttle_diff_m"]
        if abs(d) >= FULL_THROTTLE_MIN_M:
            out.append(f"全油门{_more_less(d, '晚', '早')} {abs(d):.0f} m")
    elif fr is not None:
        out.append("出弯一直没到全油门（参考圈到了）")
    elif fo is not None:
        out.append("出弯到了全油门（参考圈没到）")

    d = seg["exit_speed_delta_kmh"]
    if d is not None and abs(d) >= EXIT_SPEED_MIN_KMH:
        out.append(f"出弯速度{_more_less(d, '高', '低')} {abs(d):.0f} km/h")
    return out


def _explain(seg: dict) -> str:
    """一句话解释，例：T3：慢 0.51 s。刹车点早 25 m，弯心最低速低 7 km/h，全油门晚 12 m"""
    name = seg["label"]
    dt = round(seg["time_delta_s"], 2) + 0.0     # + 0.0：避免显示成 “-0.00”
    clauses = seg["clauses"]
    if abs(dt) < TIME_EQUAL_S:
        head = f"{name}：基本持平（{dt:+.2f} s）"
        return head + ("。" + "，".join(clauses) if clauses else "")
    head = f"{name}：{'慢' if dt > 0 else '快'} {abs(dt):.2f} s"
    if clauses:
        return head + "。" + "，".join(clauses)
    if seg["kind"] == "start":
        return head + "。过线车速差别不大"
    return head + "。刹车点、弯心最低速、全油门点都和参考圈差不多"


def _summary(total: float, segments: list[dict]) -> str:
    if abs(total) < TIME_EQUAL_S:
        head = f"和参考圈基本一样快（{total:+.3f} s）"
    else:
        head = f"比参考圈{'慢' if total > 0 else '快'} {abs(total):.3f} s"
    losses = sorted([s for s in segments if s["time_delta_s"] >= SUMMARY_MIN_S],
                    key=lambda s: -s["time_delta_s"])[:SUMMARY_TOP_N]
    gains = sorted([s for s in segments if s["time_delta_s"] <= -SUMMARY_MIN_S],
                   key=lambda s: s["time_delta_s"])[:SUMMARY_TOP_N]
    parts = [head]
    if losses:
        parts.append("主要丢在 " + "、".join(f"{s['label']}（{s['time_delta_s']:.2f} s）" for s in losses))
    if gains:
        parts.append("在 " + "、".join(f"{s['label']}（{-s['time_delta_s']:.2f} s）" for s in gains) + " 赚回")
    return "，".join(parts)


# ================================================================ 提示 / 赛道图 / 工具函数

def _warnings(ref: LapData, other: LapData, ref_len: float, other_len: float, corners: list) -> list[str]:
    m1, m2 = ref.meta, other.meta
    w: list[str] = []
    if m1.get("id") is not None and m1.get("id") == m2.get("id"):
        w.append("两边是同一圈")
    if m1.get("track_id") is not None and m2.get("track_id") is not None and m1["track_id"] != m2["track_id"]:
        w.append(f"两圈不在同一条赛道（{m1.get('track_name') or m1['track_id']} / "
                 f"{m2.get('track_name') or m2['track_id']}），按距离硬对齐，结果只能参考")
    if (m1.get("game"), m1.get("car_ordinal")) != (m2.get("game"), m2.get("car_ordinal")):
        w.append("两圈用的不是同一辆车（或不是同一个游戏），差别里有一部分是车的差别")
    diff = abs(other_len - ref_len) / ref_len
    if diff > LENGTH_WARN_RATIO:
        w.append(f"两圈长度相差 {diff:.0%}（{ref_len:.0f} m / {other_len:.0f} m），"
                 "可能不是同一条路线或有一圈没跑完，对齐会有偏差")
    for name, m in (("参考圈", m1), ("对比圈", m2)):
        if m.get("is_complete") is False:
            w.append(f"{name}不是完整的一圈")
        if "rewind" in (m.get("flags") or []):
            w.append(f"{name}中途倒过带，时间可能不连贯")
    if not corners:
        w.append("参考圈没识别出弯道（可能没有坐标数据），只能看曲线")
    return w


def _track_map(rs_ref: dict, grid: np.ndarray, loss_rate: np.ndarray, segments: list[dict]) -> dict | None:
    x, z = rs_ref["pos_x"], rs_ref["pos_z"]
    if not np.all(np.isfinite(x)) or not np.all(np.isfinite(z)) or np.ptp(x) + np.ptp(z) < 1.0:
        return None
    stride = max(1, math.ceil(len(grid) / MAP_MAX_POINTS))
    sel = slice(None, None, stride)
    corners = []
    for s in segments:
        if s["kind"] != "corner":
            continue
        corners.append({"name": s["name"], "apex_m": s["apex_m"],
                        "x": _r(np.interp(s["apex_m"], grid, x), 1), "z": _r(np.interp(s["apex_m"], grid, z), 1)})
    return {
        "dist": _clean(grid[sel], 1),
        "x": _clean(x[sel], 1),
        "z": _clean(z[sel], 1),
        "loss_rate": _clean(loss_rate[sel], 4),
        "corners": corners,
        "start": {"x": _r(x[0], 1), "z": _r(z[0], 1)},
    }


def _lap_length(lap: LapData) -> float:
    d = lap["dist"]
    d = d[np.isfinite(d)]
    return float(np.max(d)) if len(d) else 0.0


def _curve_dict(rs: dict) -> dict:
    out = {}
    for name, nd in CURVE_CHANNELS:
        src = rs["speed_kmh"] if name == "speed" else rs[name]
        out[name] = _clean(src, nd)
    return out


def _smooth(a: np.ndarray, n: int) -> np.ndarray:
    """滑动平均，两端用边缘值填充。"""
    if n <= 1 or len(a) < n:
        return a
    pad = n // 2
    return np.convolve(np.pad(a, (pad, n - 1 - pad), mode="edge"), np.ones(n) / n, mode="valid")


def _r(x, nd: int):
    """四舍五入成普通 float；NaN / inf → None。"""
    x = float(x)
    return round(x, nd) if math.isfinite(x) else None


def _num(x):
    return x if isinstance(x, (int, float)) and math.isfinite(x) else None


def _clean(a: np.ndarray, nd: int) -> list:
    """数组 → JSON 列表；NaN / inf → None。nd = 0 时输出整数。"""
    a = np.asarray(a, dtype=np.float64)
    r = np.round(a, nd)
    fin = np.isfinite(r)
    if nd == 0:
        return [int(v) if f else None for v, f in zip(r.tolist(), fin.tolist())]
    if fin.all():
        return r.tolist()
    return [v if f else None for v, f in zip(r.tolist(), fin.tolist())]


def jsonable(v):
    """元数据里的值转成 JSON 安全的（NaN → None，numpy 标量 → Python 标量）。"""
    if isinstance(v, (np.generic,)):
        v = v.item()
    if isinstance(v, float) and not math.isfinite(v):
        return None
    if isinstance(v, (list, tuple)):
        return [jsonable(x) for x in v]
    if isinstance(v, dict):
        return {k: jsonable(x) for k, x in v.items()}
    return v
