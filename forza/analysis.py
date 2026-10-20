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
想调“多大的差别才值得说”，改下面的阈值常量即可（全部集中在文件顶部）。

注意：Data Out 没有 ABS 状态，刹车值只代表踏板踩了多深（不代表实际制动力，实际减速看 g_long）；
手柄玩家的方向输入可能已经被游戏做过死区 / 平滑处理，“修方向”次数仅供参考。
"""
from __future__ import annotations

import math

import numpy as np

from .corners import (
    BRAKE_ON,
    BRAKE_SEARCH_M,
    FULL_THROTTLE,
    detect_corners,
    resample_by_distance,
)
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

# ---------------------------------------------------------------- 驾驶细节：滑行 / 松刹车 / 带刹车入弯 / 出弯收油 / 修方向 / 反打
# 只看踏板和方向盘输入。手柄死区大、方向抖动多的话，调大 STEER_CORR_MIN / COUNTER_STEER_MIN。
COAST_THROTTLE_MAX = 0.05     # 油门 < 5%……
COAST_BRAKE_MAX = 0.05        # ……刹车 < 5%……
COAST_CLUTCH_MAX = 0.50       # ……离合没踩下（换挡瞬间的空油门不算滑行）……
COAST_MIN_KMH = 30.0          # ……车速 > 30 km/h，才算“滑行”
COAST_MIN_RUN_S = 0.15        # 连续滑行超过这么久才计入
COAST_DIFF_S = 0.20           # 两圈滑行时间差超过这个才说
BRAKE_OFF = 0.05              # 刹车低于这个算完全松开
RELEASE_FROM = 0.90           # “开始松刹车” = 弯心前最后一次刹车 ≥ 峰值 × 这个比例的位置
RELEASE_DIFF_M = 12.0         # 松刹车（从重刹到松开）用的距离差超过这么多米……
RELEASE_DIFF_RATIO = 0.40     # ……并且相对差超过 40% 才说
TRAIL_STEER_MIN = 0.10        # 刹车 ≥ BRAKE_OFF 同时 |方向| ≥ 10% = 带着刹车转向（拖刹入弯）
TRAIL_DIFF_M = 10.0           # 两圈“带刹车转向”的距离差超过这么多米才说
TRAIL_NONE_M = 4.0            # 少于这么多米算“没带刹车入弯”
LIFT_MIN = 0.15               # 出弯时油门比之前的最高值回落 ≥ 15 个百分点 = 收了一次油
LIFT_RECOVER = 0.05           # 回落后再加回这么多才算重新给油（防止抖动重复计数）
LIFT_MARGIN = 0.05            # 比较两圈时的回差：另一圈按 LIFT_MIN − 这个值再数一遍，免得 14% / 16% 这种擦边情况来回跳
EXIT_WINDOW_M = 80.0          # 出弯收油 / 反打的统计范围：弯心 → 弯终点后这么多米（不超过本段终点）
STEER_SMOOTH_M = 6.0          # 数修方向次数前先把方向盘平滑这么多米（去掉采样抖动）
STEER_CORR_MIN = 0.05         # 方向来回超过满舵的 5% 才算一次修正（正常入弯打、出弯回不算）
STEER_CORR_DIFF = 2           # 两圈修方向次数差 ≥ 这个才说
STEER_MARGIN = 0.02           # 比较两圈时的回差（修方向 / 反打的阈值对另一圈放宽这么多再数一遍）
COUNTER_STEER_MIN = 0.05      # 方向和弯道方向相反、超过满舵的 5% 算反打
COUNTER_MIN_M = 4.0           # 连续反打这么多米以上才算一次
TURN_IN_STEER = 0.10          # 转向点 = 进弯时第一次 |方向| ≥ 10% 的位置（赛道图标记用）
TURN_IN_SEARCH_M = 250.0      # 从弯起点往前最多找这么远的转向点
DECEL_MIN_G = 0.10            # 最大减速 G 差超过这个才在“刹车轻 / 重”里补一句

# ---------------------------------------------------------------- 赛道图 / 走线偏移
OFFSET_SEARCH_M = 40.0        # 算横向偏移时，在 A 轨迹上前后各找这么远的最近点

# ---------------------------------------------------------------- 可选曲线（/api/compare?channels=...，按需返回）
# key: (标题, 单位, [(输出通道名, 说明)])。驱动轮按圈的 drivetrain 选（0 = 前驱，1 = 后驱，2 = 四驱）
EXTRA_CHANNELS: dict[str, tuple[str, str, list[tuple[str, str]]]] = {
    "g_lat": ("横向 G（+ = 向右）", "G", [("g_lat", "横向 G")]),
    "g_comb": ("合成 G（横向 + 纵向）", "G", [("g_comb", "合成 G")]),
    "slip_angle": ("前后轴平均 |侧偏角|（> 1 = 过了抓地极限）", "归一化",
                   [("slip_angle_front", "前轴"), ("slip_angle_rear", "后轴")]),
    "slip_ratio": ("驱动轮滑移率（+ = 空转，− = 抱死）", "归一化", [("slip_ratio_drive", "驱动轮")]),
    "tire_temp": ("胎温", "°C", [("tire_temp_fl", "左前"), ("tire_temp_fr", "右前"),
                                  ("tire_temp_rl", "左后"), ("tire_temp_rr", "右后")]),
    "susp": ("悬挂行程（0 = 伸到底，1 = 压到底）", "0~1", [("susp_fl", "左前"), ("susp_fr", "右前"),
                                                          ("susp_rl", "左后"), ("susp_rr", "右后")]),
}

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
# 只参与逐弯指标计算、不作为曲线输出的通道
METRIC_CHANNELS = ["clutch", "g_long"]
UNITS = {"dist": "m", "speed": "km/h", "throttle": "0~1", "brake": "0~1", "steer": "-1~1（+ = 右）",
         "gear": "挡", "rpm": "rpm", "t": "s", "delta_t": "s（+ = 对比圈慢）",
         "loss_rate": "s/100 m（+ = 对比圈在这里丢时间）",
         "lateral_offset": "m（B 相对 A 轨迹的横向距离，+ = B 在 A 右侧）"}

# 圈摘要里带上的元数据字段
_SUMMARY_KEYS = ["id", "lap_number", "lap_time", "kind", "is_complete", "flags", "track_id", "track_name",
                 "car_ordinal", "car_name", "car_class", "car_pi", "game", "family", "distance_m",
                 "created_at", "note", "session_id", "time_source"]


# ================================================================ 对外接口

def compare(ref: LapData, other: LapData, step: float = 2.0, *, corners: list[dict] | None = None,
            extra: list[str] | None = None) -> dict:
    """对比两圈。ref = 参考圈（通常是最快圈），other = 要分析的圈。

    corners：可选，按 ref 圈距离给出的弯道表（格式同 corners.detect_corners，可带 label = 显示名）。
             赛道有固定弯道表时由调用方传入（forza.sectors.layout_corners），不给就用 ref 自己识别。
    extra：可选，额外要输出的曲线组（EXTRA_CHANNELS 的 key，如 ["g_lat", "tire_temp"]），不认识的 key 抛 ValueError。

    返回（全部可 JSON 序列化，没有 NaN）：
        step, length_m, scale, warnings[], units{},
        ref{圈摘要}, other{圈摘要},
        channels{dist[], ref{speed,throttle,brake,steer,gear,rpm,t, +extra}, other{同}, delta_t[], loss_rate[],
                 lateral_offset[]（B 相对 A 轨迹的横向偏移，没有坐标时为 None）},
        extra{key: {title, unit, series[{name, label}]}}   只含请求了的曲线组
        extra_options[{key, title}]                         所有可选曲线组
        segments[{name, label, kind, 时间差、刹车点、最低速、滑行、松刹、带刹车入弯、收油、修方向、反打、转向点……, explanation}]
        ranking[{name, kind, time_delta_s, explanation}]                 按丢时间从多到少
        totals{delta_t_end_s, lap_time_delta_s, lost_s, gained_s, summary}
        map{dist[], x[], z[], x_b[], z_b[], loss_rate[], corners[{name, label, x, z, apex_m}], start{x, z}} 或 None
        corner_source: "layout"（固定弯道表）| "detected"（参考圈自动识别）
    """
    if step <= 0:
        raise ValueError("step 必须大于 0")
    extra = list(dict.fromkeys(extra or []))
    bad = [k for k in extra if k not in EXTRA_CHANNELS]
    if bad:
        raise ValueError(f"不认识的曲线 {', '.join(bad)}，可选：{', '.join(EXTRA_CHANNELS)}")
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
    src_extra = sorted({c for k in extra for c in _extra_sources(k)})
    rs_ref = resample_by_distance(ref, step, names + METRIC_CHANNELS + map_names + src_extra, length=ref_len)
    rs_oth = resample_by_distance(other_scaled, step, names + METRIC_CHANNELS + map_names + src_extra,
                                  length=ref_len)
    grid = rs_ref["dist"]
    for rs in (rs_ref, rs_oth):
        for c in names + METRIC_CHANNELS:
            if c not in rs:                      # 老数据可能缺通道：补 0，不让整个对比失败
                rs[c] = np.zeros_like(grid)
        for c in map_names + src_extra:
            if c not in rs:                      # 坐标 / 可选曲线缺了就是缺了（NaN → 输出 None）
                rs[c] = np.full_like(grid, np.nan)
        rs["gear"] = np.round(rs["gear"])        # 挡位插值后会出现 3.5 挡，取整
        rs["speed_kmh"] = rs["speed"] * MS_TO_KMH
    extra_out = {}
    for k in extra:
        title, unit, series = EXTRA_CHANNELS[k]
        for lap, rs in ((ref, rs_ref), (other, rs_oth)):
            rs.update(_extra_values(k, rs, lap.meta))
        extra_out[k] = {"title": title, "unit": unit, "series": [{"name": n, "label": lb} for n, lb in series]}

    delta_t = rs_oth["t"] - rs_ref["t"]
    loss_rate = _smooth(np.gradient(delta_t, grid) * 100.0, max(1, round(LOSS_RATE_SMOOTH_M / step)))

    # ---- 弯道 + 分段 ----
    corner_source = "layout" if corners else "detected"
    if not corners:
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
    offset = _lateral_offset(rs_ref, rs_oth, step)

    def curves(rs: dict) -> dict:
        out = _curve_dict(rs)
        for k in extra:
            for n, _ in EXTRA_CHANNELS[k][2]:
                out[n] = _clean(rs[n], 3 if not n.startswith("tire_temp") else 1)
        return out

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
            "ref": curves(rs_ref),
            "other": curves(rs_oth),
            "delta_t": _clean(delta_t, 4),
            "loss_rate": _clean(loss_rate, 4),
            "lateral_offset": None if offset is None else _clean(offset, 2),
        },
        "extra": extra_out,
        "extra_options": [{"key": k, "title": v[0]} for k, v in EXTRA_CHANNELS.items()],
        "segments": segments,
        "ranking": ranking,
        "totals": totals,
        "map": _track_map(rs_ref, rs_oth, grid, loss_rate, segments),
        "corner_source": corner_source,
    }
    return out


def _extra_sources(key: str) -> list[str]:
    """可选曲线组要从圈里重采样哪些原始通道。"""
    wheels = ["fl", "fr", "rl", "rr"]
    return {
        "g_lat": ["g_lat"], "g_comb": ["g_lat", "g_long"],
        "slip_angle": [f"slip_angle_{w}" for w in wheels], "slip_ratio": [f"slip_ratio_{w}" for w in wheels],
        "tire_temp": [f"tire_temp_{w}" for w in wheels], "susp": [f"susp_{w}" for w in wheels],
    }[key]


def _extra_values(key: str, rs: dict, meta: dict) -> dict[str, np.ndarray]:
    """由重采样后的原始通道算出可选曲线（名字见 EXTRA_CHANNELS）。"""
    if key == "g_lat":
        return {"g_lat": rs["g_lat"]}
    if key == "g_comb":
        return {"g_comb": np.hypot(rs["g_lat"], rs["g_long"])}
    if key == "slip_angle":
        return {"slip_angle_front": (np.abs(rs["slip_angle_fl"]) + np.abs(rs["slip_angle_fr"])) / 2,
                "slip_angle_rear": (np.abs(rs["slip_angle_rl"]) + np.abs(rs["slip_angle_rr"])) / 2}
    if key == "slip_ratio":
        wheels = {0: ["fl", "fr"], 1: ["rl", "rr"]}.get(meta.get("drivetrain"), ["fl", "fr", "rl", "rr"])
        return {"slip_ratio_drive": np.mean([rs[f"slip_ratio_{w}"] for w in wheels], axis=0)}
    if key == "tire_temp":
        return {f"tire_temp_{w}": rs[f"tire_temp_{w}"] for w in ("fl", "fr", "rl", "rr")}
    if key == "susp":
        return {f"susp_{w}": rs[f"susp_{w}"] for w in ("fl", "fr", "rl", "rr")}
    raise ValueError(key)


def lap_summary(lap: LapData) -> dict:
    """圈的简要信息（给对比结果和网页标题用）。"""
    m = lap.meta
    return {k: jsonable(m.get(k)) for k in _SUMMARY_KEYS}


# ================================================================ 分段与逐弯指标

def _build_segments(corners: list[dict], grid: np.ndarray, rs_ref: dict, rs_oth: dict,
                    delta_t: np.ndarray, step: float) -> list[dict]:
    """把一圈切成段：每个弯一段，范围 = 收油 / 刹车区 → 弯 → 弯后直道（到下一个弯的段起点为止）。

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
    prev_end_i = 0
    for c in corners:
        a_i, s_i, e_i = idx(c["apex_m"]), idx(c["start_m"]), idx(c["end_m"])
        lo = max(prev_apex_i + 1 if infos else 0, s_i - int(BRAKE_SEARCH_M / step))
        b_ref = _brake_point(rs_ref["brake"], lo, a_i, step)
        b_oth = _brake_point(rs_oth["brake"], lo, a_i, step)
        cands = [b for b in (b_ref, b_oth) if b is not None]
        if cands:
            # 刹车前紧挨着的松油滑行也算进这个弯（滑行丢的时间要算在这个弯头上）
            lifts = [_lift_point(rs, b, lo) for rs, b in ((rs_ref, b_ref), (rs_oth, b_oth)) if b is not None]
            raw = min(min(cands + lifts), s_i) * step - PRE_BRAKE_MARGIN_M
        else:
            raw = c["start_m"] - NO_BRAKE_LEAD_M
        # 段起点不能早于上一个弯的弯心（连续弯 / 减速弯），也不能晚于本弯起点
        low = (prev_apex_i + 1) * step if infos else 0.0
        seg_start = min(max(raw, low), c["start_m"])
        infos.append({"c": c, "apex_i": a_i, "start_i": s_i, "end_i": e_i, "prev_end_i": prev_end_i,
                      "b_ref": b_ref, "b_oth": b_oth, "seg_start": seg_start})
        prev_apex_i = a_i
        prev_end_i = e_i

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
        if kind == "start":
            name, label = "起点", "起点"
        else:
            name = info["c"]["name"]
            label = info["c"].get("label") or name
        seg = {"name": name, "label": label, "kind": kind,
               "seg_start_m": round(s0, 1), "seg_end_m": round(s1, 1),
               "time_delta_s": round(dt_at(s1) - dt_at(s0), 3)}
        if kind == "start":
            seg.update(_start_metrics(rs_ref, rs_oth, idx(s1)))
            first = infos[0]["c"] if infos else None
            seg["label"] = f"起点→{first.get('label') or first['name']}" if first else "整圈"
        else:
            seg.update(_corner_metrics(info, rs_ref, rs_oth, idx(s0), idx(s1), step))
        loose = seg.pop("_loose", {})
        seg["clauses"] = _clauses(seg, loose)
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
    gap_max = max(1, round(BRAKE_GAP_M / step))
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


def _lift_point(rs: dict, brake_i: int, lo: int) -> int:
    """刹车点之前紧挨着的“松油滑行”从哪儿开始（网格下标；没有滑行就是刹车点本身）。"""
    coast = _coast_mask(rs)
    i = brake_i
    while i - 1 >= lo and coast[i - 1]:
        i -= 1
    return i


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

    # 刹车点 / 刹车前车速 / 最大刹车力度（踏板）/ 最大减速 G（实际效果）
    out["brake_ref_m"] = None if b_ref is None else round(b_ref * step, 1)
    out["brake_other_m"] = None if b_oth is None else round(b_oth * step, 1)
    out["brake_diff_m"] = None if b_ref is None or b_oth is None else round((b_ref - b_oth) * step, 1)
    out["entry_speed_ref_kmh"] = None if b_ref is None else _r(v_ref[b_ref], 1)
    out["entry_speed_other_kmh"] = None if b_oth is None else _r(v_oth[b_oth], 1)
    out["entry_speed_delta_kmh"] = (None if b_ref is None or b_oth is None
                                    else _r(v_oth[b_oth] - v_ref[b_ref], 1))
    out["brake_peak_ref"] = _r(np.max(rs_ref["brake"][s0_i:a_i + 1]), 2) if a_i >= s0_i else 0.0
    out["brake_peak_other"] = _r(np.max(rs_oth["brake"][s0_i:a_i + 1]), 2) if a_i >= s0_i else 0.0
    out["decel_ref_g"] = _r(max(0.0, -np.min(rs_ref["g_long"][s0_i:a_i + 1])), 2) if a_i >= s0_i else 0.0
    out["decel_other_g"] = _r(max(0.0, -np.min(rs_oth["g_long"][s0_i:a_i + 1])), 2) if a_i >= s0_i else 0.0

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

    # ---- 驾驶细节（两圈用同一套范围，才好比）----
    sign = 1.0 if c["direction"] == "right" else -1.0
    exit_hi = min(s1_i, e_i + round(EXIT_WINDOW_M / step))
    turn_lo = max(s0_i, s_i - int(TURN_IN_SEARCH_M / step))
    loose: dict = {}
    for side, rs, b_i in (("ref", rs_ref, b_ref), ("other", rs_oth, b_oth)):
        cp, cm, cx = _coast_time(rs, s0_i, s1_i, a_i if b_i is None else min(b_i, a_i), a_i)
        out[f"coast_pre_{side}_s"] = _r(cp, 2)          # 刹车点之前（松油还没踩刹车）的滑行时间
        out[f"coast_mid_{side}_s"] = _r(cm, 2)          # 松开刹车到弯心之间的滑行时间
        out[f"coast_exit_{side}_s"] = _r(cx, 2)         # 弯心之后（迟迟不给油）的滑行时间
        rel = _release_m(rs["brake"], s0_i, a_i, s1_i, step)
        out[f"release_{side}_m"] = None if rel is None else round(rel, 1)
        out[f"trail_{side}_m"] = round(_trail_m(rs, s0_i, e_i, step), 1)
        out[f"lifts_{side}"] = _lift_count(rs["throttle"], a_i, exit_hi)
        out[f"steer_corrections_{side}"] = _steer_corrections(rs["steer"], s_i, e_i, sign, step)
        out[f"counter_steer_{side}"] = _counter_steer(rs["steer"], a_i, exit_hi, sign, step)
        ti = _turn_in_point(rs["steer"], turn_lo, a_i, sign)
        out[f"turn_in_{side}_m"] = None if ti is None else round(ti * step, 1)
        # 放宽阈值再数一遍（只用来比较，不输出）
        loose[f"lifts_{side}"] = _lift_count(rs["throttle"], a_i, exit_hi, LIFT_MIN - LIFT_MARGIN)
        loose[f"steer_corrections_{side}"] = _steer_corrections(rs["steer"], s_i, e_i, sign, step,
                                                                 STEER_CORR_MIN - STEER_MARGIN)
        loose[f"counter_steer_{side}"] = _counter_steer(rs["steer"], a_i, exit_hi, sign, step,
                                                         COUNTER_STEER_MIN - STEER_MARGIN)
    out["_loose"] = loose
    return out


# ---------------------------------------------------------------- 驾驶细节的计算（都在等间距网格上）

def _runs(mask: np.ndarray) -> list[tuple[int, int]]:
    """布尔数组里连续为 True 的区间 [(起, 止)]（含两端）。"""
    if not len(mask):
        return []
    m = np.concatenate([[0], mask.astype(np.int8), [0]])
    d = np.diff(m)
    return list(zip(np.nonzero(d == 1)[0].tolist(), (np.nonzero(d == -1)[0] - 1).tolist()))


def _coast_mask(rs: dict) -> np.ndarray:
    """每个网格点是不是“滑行”：油门、刹车都没踩，离合没踩，车还在跑。"""
    if "_coast" not in rs:
        rs["_coast"] = ((rs["throttle"] < COAST_THROTTLE_MAX) & (rs["brake"] < COAST_BRAKE_MAX)
                        & (rs["clutch"] < COAST_CLUTCH_MAX) & (rs["speed_kmh"] > COAST_MIN_KMH))
    return rs["_coast"]


def _coast_time(rs: dict, i0: int, i1: int, brake_i: int, apex_i: int) -> tuple[float, float, float]:
    """[i0, i1] 里持续 ≥ COAST_MIN_RUN_S 的滑行时间（s），按 刹车点前 / 刹车点~弯心 / 弯心后 分三份。"""
    if i1 <= i0:
        return 0.0, 0.0, 0.0
    t = rs["t"]
    n = len(t)
    cell_dt = np.diff(t, append=t[-1])                # 每个网格点到下一个点的用时
    keep = np.zeros(n, dtype=bool)
    for r0, r1 in _runs(_coast_mask(rs)[i0:i1 + 1]):
        g0, g1 = i0 + r0, i0 + r1
        if float(t[min(g1 + 1, n - 1)] - t[g0]) >= COAST_MIN_RUN_S:
            keep[g0:g1 + 1] = True
    dt = np.where(keep, cell_dt, 0.0)
    return (float(np.sum(dt[i0:brake_i])), float(np.sum(dt[brake_i:apex_i])),
            float(np.sum(dt[apex_i:i1 + 1])))


def _release_m(brk: np.ndarray, s0_i: int, apex_i: int, s1_i: int, step: float) -> float | None:
    """松刹车用了多少米：弯心前最后一次刹车 ≥ 峰值 × RELEASE_FROM 的位置 → 刹车 < BRAKE_OFF 的位置。"""
    if apex_i < s0_i:
        return None
    seg = brk[s0_i:apex_i + 1]
    peak = float(seg.max())
    if peak < BRAKE_ON:
        return None
    hi_i = s0_i + int(np.nonzero(seg >= RELEASE_FROM * peak)[0][-1])
    off = np.nonzero(brk[hi_i:max(hi_i, s1_i) + 1] < BRAKE_OFF)[0]
    return float(off[0]) * step if len(off) else None


def _trail_m(rs: dict, i0: int, i1: int, step: float) -> float:
    """带着刹车转向（刹车 ≥ BRAKE_OFF 且 |方向| ≥ TRAIL_STEER_MIN）的距离（m）。"""
    if i1 < i0:
        return 0.0
    sl = slice(i0, i1 + 1)
    return float(np.sum((rs["brake"][sl] >= BRAKE_OFF) & (np.abs(rs["steer"][sl]) >= TRAIL_STEER_MIN))) * step


def _lift_count(thr: np.ndarray, i0: int, i1: int, lift: float = LIFT_MIN) -> int:
    """[i0, i1] 里油门从之前的最高值回落 ≥ lift 的次数（回落后要再加回 LIFT_RECOVER 才算重新给油）。"""
    if i1 <= i0:
        return 0
    count = 0
    peak = float(thr[i0])
    low = None
    for v in thr[i0:i1 + 1].tolist():
        if low is None:
            peak = max(peak, v)
            if peak - v >= lift:
                count += 1
                low = v
        else:
            low = min(low, v)
            if v - low >= LIFT_RECOVER:
                peak, low = v, None
    return count


def _smooth_steer(steer: np.ndarray, i0: int, i1: int, sign: float, step: float) -> np.ndarray:
    """取 [i0, i1] 的方向（换算成“朝弯内为正”）并平滑。"""
    x = steer[i0:i1 + 1] * sign
    return _smooth(x, max(1, round(STEER_SMOOTH_M / step)))


def _steer_corrections(steer: np.ndarray, i0: int, i1: int, sign: float, step: float,
                       h: float = STEER_CORR_MIN) -> int:
    """弯里方向来回修了几次：平滑后的方向转折点个数（回差 h），扣掉正常的“打进去再回正”那一次。"""
    if i1 - i0 < 3:
        return 0
    x = _smooth_steer(steer, i0, i1, sign, step).tolist()
    dirn, hi, lo, rev = 0, x[0], x[0], 0
    for v in x:
        if dirn == 0:
            hi, lo = max(hi, v), min(lo, v)
            if hi - v >= h:
                dirn, lo = -1, v
            elif v - lo >= h:
                dirn, hi = 1, v
        elif dirn == 1:
            if v > hi:
                hi = v
            elif hi - v >= h:
                dirn, lo, rev = -1, v, rev + 1
        else:
            if v < lo:
                lo = v
            elif v - lo >= h:
                dirn, hi, rev = 1, v, rev + 1
    return max(0, rev - 1)


def _counter_steer(steer: np.ndarray, i0: int, i1: int, sign: float, step: float,
                   level: float = COUNTER_STEER_MIN) -> int:
    """弯心之后方向打到和弯道相反一侧（超过 level、连续 ≥ COUNTER_MIN_M）的次数。"""
    if i1 - i0 < 2:
        return 0
    x = _smooth_steer(steer, i0, i1, sign, step)
    min_pts = max(1, int(math.ceil(COUNTER_MIN_M / step)))
    return sum(1 for r0, r1 in _runs(x <= -level) if r1 - r0 + 1 >= min_pts)


def _turn_in_point(steer: np.ndarray, i0: int, apex_i: int, sign: float) -> int | None:
    """转向点：从 i0 往后第一次朝弯内打方向 ≥ TURN_IN_STEER（网格下标），弯心前没打到就是 None。"""
    if apex_i < i0:
        return None
    hit = np.nonzero(steer[i0:apex_i + 1] * sign >= TURN_IN_STEER)[0]
    return i0 + int(hit[0]) if len(hit) else None


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


def _clauses(seg: dict, loose: dict | None = None) -> list[str]:
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

    loose = loose or {}

    def more(key: str) -> tuple[bool, bool]:
        """(这圈明显更多, 参考圈明显更多)：比较时另一圈用放宽后的计数，擦边的不算。"""
        o, r = seg[f"{key}_other"], seg[f"{key}_ref"]
        return o > loose.get(f"{key}_ref", r), r > loose.get(f"{key}_other", o)

    def coast_diff(key: str) -> tuple[float, float] | None:
        o, r = seg[f"{key}_other_s"], seg[f"{key}_ref_s"]
        return (o, r) if o is not None and r is not None and abs(o - r) >= COAST_DIFF_S else None

    br, bo = seg["brake_ref_m"], seg["brake_other_m"]
    # 刹车前松油滑行（“入弯前滑行了 0.4 s”）
    c = coast_diff("coast_pre")
    if c:
        out.append(f"入弯前（刹车前）滑行 {c[0]:.1f} s（油门刹车都没踩，参考圈 {c[1]:.1f} s）")

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
        txt = f"刹车{_more_less(po - pr, '更重', '更轻')}（踏板最大 {po:.0%}，参考圈 {pr:.0%}"
        gr, go = seg.get("decel_ref_g"), seg.get("decel_other_g")
        if gr is not None and go is not None and abs(go - gr) >= DECEL_MIN_G:
            txt += f"；最大减速 {go:.2f} G / {gr:.2f} G"
        out.append(txt + "）")

    # 松刹车：从重刹到完全松开用了多少米
    rr, ro = seg["release_ref_m"], seg["release_other_m"]
    if rr is not None and ro is not None:
        diff = ro - rr
        if abs(diff) >= RELEASE_DIFF_M and abs(diff) >= RELEASE_DIFF_RATIO * max(rr, ro):
            out.append(f"松刹车{_more_less(diff, '更慢', '太急')}（从重刹到松开 {ro:.0f} m，参考圈 {rr:.0f} m）")

    # 带刹车入弯（刹车和转向重叠）
    tr, to = seg["trail_ref_m"], seg["trail_other_m"]
    if tr - to >= TRAIL_DIFF_M:
        if to < TRAIL_NONE_M:
            out.append(f"没带刹车入弯（参考圈刹车和转向重叠 {tr:.0f} m）")
        else:
            out.append(f"带刹车入弯少 {tr - to:.0f} m（刹车和转向重叠 {to:.0f} m，参考圈 {tr:.0f} m）")
    elif to - tr >= TRAIL_DIFF_M:
        out.append(f"带刹车转向多 {to - tr:.0f} m（重叠 {to:.0f} m，参考圈 {tr:.0f} m）")

    # 松开刹车后到弯心之间滑行
    c = coast_diff("coast_mid")
    if c:
        out.append(f"松开刹车到弯心之间滑行 {c[0]:.1f} s（参考圈 {c[1]:.1f} s）")

    # 弯里修方向
    sr_, so_ = seg["steer_corrections_ref"], seg["steer_corrections_other"]
    if so_ - loose.get("steer_corrections_ref", sr_) >= STEER_CORR_DIFF:
        out.append(f"弯里方向来回修了 {so_} 次（参考圈 {sr_} 次）")
    elif sr_ - loose.get("steer_corrections_other", so_) >= STEER_CORR_DIFF:
        out.append(f"方向更稳（修了 {so_} 次，参考圈 {sr_} 次）")

    d = seg["min_speed_delta_kmh"]
    if d is not None and abs(d) >= MIN_SPEED_MIN_KMH:
        out.append(f"弯心最低速{_more_less(d, '高', '低')} {abs(d):.0f} km/h")

    # 弯心后迟迟不给油（滑行）
    c = coast_diff("coast_exit")
    if c:
        out.append(f"弯心后滑行 {c[0]:.1f} s 才给油（参考圈 {c[1]:.1f} s）")

    # 反打
    kr, ko = seg["counter_steer_ref"], seg["counter_steer_other"]
    o_more, r_more = more("counter_steer")
    if o_more:
        out.append(f"出弯反打方向 {ko} 次（参考圈 {kr} 次），车尾多半滑了")
    elif r_more:
        out.append(f"出弯反打更少（{ko} 次，参考圈 {kr} 次）")

    # 出弯收油
    lr, lo_ = seg["lifts_ref"], seg["lifts_other"]
    o_more, r_more = more("lifts")
    if o_more:
        out.append(f"出弯收了 {lo_} 次油（参考圈 {lr} 次）")
    elif r_more:
        out.append(f"出弯收油更少（{lo_} 次，参考圈 {lr} 次）")

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
    return head + "。刹车、弯心速度、给油、方向都和参考圈差不多（差别可能在走线上，看“走线偏移”曲线）"


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


def _track_map(rs_ref: dict, rs_oth: dict, grid: np.ndarray, loss_rate: np.ndarray,
               segments: list[dict]) -> dict | None:
    x, z = rs_ref["pos_x"], rs_ref["pos_z"]
    if not _has_pos(rs_ref):
        return None
    stride = max(1, math.ceil(len(grid) / MAP_MAX_POINTS))
    sel = slice(None, None, stride)
    corners = []
    for s in segments:
        if s["kind"] != "corner":
            continue
        corners.append({"name": s["name"], "label": s.get("label") or s["name"], "apex_m": s["apex_m"],
                        "x": _r(np.interp(s["apex_m"], grid, x), 1), "z": _r(np.interp(s["apex_m"], grid, z), 1)})
    has_b = _has_pos(rs_oth)
    return {
        "dist": _clean(grid[sel], 1),
        "x": _clean(x[sel], 1),
        "z": _clean(z[sel], 1),
        # B 圈的走线（同一张距离网格上；B 没有坐标时为 None）
        "x_b": _clean(rs_oth["pos_x"][sel], 2) if has_b else None,
        "z_b": _clean(rs_oth["pos_z"][sel], 2) if has_b else None,
        "loss_rate": _clean(loss_rate[sel], 4),
        "corners": corners,
        "start": {"x": _r(x[0], 1), "z": _r(z[0], 1)},
    }


def _has_pos(rs: dict) -> bool:
    x, z = rs.get("pos_x"), rs.get("pos_z")
    return (x is not None and z is not None and bool(np.all(np.isfinite(x))) and bool(np.all(np.isfinite(z)))
            and float(np.ptp(x) + np.ptp(z)) >= 1.0)


def _lateral_offset(rs_ref: dict, rs_oth: dict, step: float) -> np.ndarray | None:
    """B 的每个点投影到 A 的轨迹上，带符号的横向距离（m，+ = B 在 A 行驶方向的右侧）。

    只在同一距离附近（± OFFSET_SEARCH_M）找 A 上最近的点，所以 A、B 局部错开几米也不影响。
    “右侧”按俯视图 x 向右、z 向上、+ 方向盘 = 右转的约定（和赛道图一致）。
    """
    if not _has_pos(rs_ref) or not _has_pos(rs_oth):
        return None
    ax, az = rs_ref["pos_x"], rs_ref["pos_z"]
    bx, bz = rs_oth["pos_x"], rs_oth["pos_z"]
    n = len(ax)
    w = max(1, round(OFFSET_SEARCH_M / step))
    idx = np.arange(n)
    ks = np.arange(-w, w + 1)
    cand = np.clip(idx[None, :] + ks[:, None], 0, n - 1)              # (2w+1, n)
    d2 = (ax[cand] - bx[None, :]) ** 2 + (az[cand] - bz[None, :]) ** 2
    j = cand[np.argmin(d2, axis=0), idx]                                # A 上最近的网格点
    # 在最近点前后两段折线上找更精确的投影
    best = np.full(n, np.inf)
    off = np.zeros(n)
    for j0, j1 in ((np.maximum(j - 1, 0), j), (j, np.minimum(j + 1, n - 1))):
        sx, sz = ax[j1] - ax[j0], az[j1] - az[j0]
        seg2 = sx * sx + sz * sz
        ok = seg2 > 1e-9
        u = np.where(ok, ((bx - ax[j0]) * sx + (bz - az[j0]) * sz) / np.where(ok, seg2, 1.0), 0.0)
        u = np.clip(u, 0.0, 1.0)
        px, pz = ax[j0] + u * sx, az[j0] + u * sz
        dist = np.hypot(bx - px, bz - pz)
        ln = np.sqrt(np.where(ok, seg2, 1.0))
        # 右侧单位向量 = (航向 z, −航向 x)
        side = ((bx - px) * (sz / ln) - (bz - pz) * (sx / ln))
        signed = np.where(ok, np.sign(side) * dist, np.nan)
        better = ok & (dist < best)
        best = np.where(better, dist, best)
        off = np.where(better, signed, off)
    off[~np.isfinite(best)] = np.nan
    return off


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
