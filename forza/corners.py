"""弯道识别：从一圈数据里自动切出 T1、T2……，给圈对比和调车诊断共用。

思路（纯几何，不依赖游戏的正负号约定）：
1. 用世界坐标 pos_x / pos_z 算行驶轨迹，按 1 m 重采样
2. 轨迹航向角对距离求导 = 曲率 κ（1/半径），平滑一下
3. |κ| 超过阈值（半径 < CORNER_MAX_RADIUS_M）的连续路段算弯，间隔很近的同向弯合并
4. 每个弯找：最低速点（apex）、往前找刹车起点、往后找全油门点

左右方向用这段路的平均方向盘（steer + = 右）判断。
"""
from __future__ import annotations

import math

import numpy as np

from .lapdata import LapData

CORNER_MAX_RADIUS_M = 300.0   # 半径小于这个算弯
MIN_CORNER_LEN_M = 10.0       # 太短的不算
MIN_HEADING_DEG = 15.0        # 总转角太小的不算
MERGE_GAP_M = 25.0            # 同向的两段弯间隔小于这个就合并
SMOOTH_M = 15                 # 曲率平滑窗口（米）
BRAKE_ON = 0.10               # 刹车超过 10% 算在刹车
BRAKE_SEARCH_M = 250.0        # 往前最多找这么远的刹车起点
FULL_THROTTLE = 0.95


def resample_by_distance(lap: LapData, step: float = 1.0, channels: list[str] | None = None,
                         length: float | None = None) -> dict[str, np.ndarray]:
    """把一圈数据按距离重采样到等间距网格（默认 1 m）。返回 {"dist": 网格, 通道: 数组}。

    length 给定时网格覆盖 [0, length]（超出这圈实际距离的部分用末尾值填）。
    """
    d = lap["dist"]
    # 距离必须单调递增才能插值：去掉倒退/重复的点
    keep = np.concatenate([[True], np.diff(d) > 1e-6]) if len(d) > 1 else np.ones(len(d), bool)
    d = np.maximum.accumulate(d[keep])
    keep_idx = np.nonzero(keep)[0]
    end = length if length is not None else (float(d[-1]) if len(d) else 0.0)
    grid = np.arange(0.0, max(end, step) + 1e-9, step)
    out = {"dist": grid}
    for name in channels or list(lap.ch.keys()):
        if name == "dist" or name not in lap.ch:
            continue
        out[name] = np.interp(grid, d, lap[name][keep_idx])
    return out


def _smooth(a: np.ndarray, n: int) -> np.ndarray:
    """滑动平均。两端用边缘值填充（直接补 0 会把圈首圈尾的坐标拉偏，凭空多出假弯）。"""
    if n <= 1 or len(a) < n:
        return a
    k = np.ones(n) / n
    pad = n // 2
    return np.convolve(np.pad(a, (pad, n - 1 - pad), mode="edge"), k, mode="valid")


def curvature(rs: dict[str, np.ndarray]) -> np.ndarray:
    """按 1 m 网格上的坐标算平滑后的曲率（带符号，正负只表示和航向角增减方向一致）。"""
    x, z = rs["pos_x"], rs["pos_z"]
    if np.ptp(x) + np.ptp(z) < 1.0 and "yaw_rate" in rs:
        # 没有坐标（比如全 0）：退回用 偏航角速度 / 车速
        return _smooth(rs["yaw_rate"] / np.maximum(rs["speed"], 5.0), SMOOTH_M)
    xs, zs = _smooth(x, 5), _smooth(z, 5)
    heading = np.unwrap(np.arctan2(np.gradient(xs), np.gradient(zs)))
    step = float(rs["dist"][1] - rs["dist"][0]) if len(rs["dist"]) > 1 else 1.0
    k = np.gradient(heading) / step
    return _smooth(k, SMOOTH_M)


def detect_corners(lap: LapData, step: float = 1.0) -> list[dict]:
    """返回弯道列表：
        name, index, start_m, apex_m, end_m, direction ("left"/"right"),
        min_speed (m/s), min_radius_m, heading_change_deg, brake_start_m (可能为 None), full_throttle_m (可能为 None)
    """
    if len(lap) < 10:
        return []
    rs = resample_by_distance(lap, step, ["pos_x", "pos_z", "speed", "brake", "throttle", "steer", "yaw_rate"])
    k = curvature(rs)
    absk = np.abs(k)
    on = absk > 1.0 / CORNER_MAX_RADIUS_M
    grid = rs["dist"]

    # 找连续区间
    regions: list[list[int]] = []
    i, n = 0, len(on)
    while i < n:
        if on[i]:
            j = i
            while j + 1 < n and on[j + 1]:
                j += 1
            regions.append([i, j])
            i = j + 1
        else:
            i += 1

    # 同向且间隔很近的合并
    merged: list[list[int]] = []
    for r in regions:
        if merged:
            p = merged[-1]
            gap = (r[0] - p[1]) * step
            same_dir = np.sign(k[p[0]:p[1] + 1].mean()) == np.sign(k[r[0]:r[1] + 1].mean())
            if gap < MERGE_GAP_M and same_dir:
                p[1] = r[1]
                continue
        merged.append(r)

    corners = []
    for a, b in merged:
        length = (b - a + 1) * step
        turn = abs(float(np.sum(k[a:b + 1]) * step))
        if length < MIN_CORNER_LEN_M or math.degrees(turn) < MIN_HEADING_DEG:
            continue
        seg_speed = rs["speed"][a:b + 1]
        # 弯心 = 最低速平台的中点（恒定半径的弯速度是一段平台，直接取 argmin 会落在平台起点）
        low = np.nonzero(seg_speed <= seg_speed.min() + 1.0 / 3.6)[0]
        apex = a + int((low[0] + low[-1]) // 2)
        steer_mean = float(np.mean(rs["steer"][a:b + 1]))
        corners.append({
            "start_m": float(grid[a]),
            "apex_m": float(grid[apex]),
            "end_m": float(grid[b]),
            "direction": "right" if steer_mean > 0 else "left",
            "min_speed": float(seg_speed.min()),
            "min_radius_m": float(1.0 / max(absk[a:b + 1].max(), 1e-6)),
            "heading_change_deg": round(math.degrees(turn), 1),
            "brake_start_m": _find_brake_start(rs, a, apex, step),
            "full_throttle_m": _find_full_throttle(rs, apex, step),
        })
    for idx, c in enumerate(corners):
        c["index"] = idx
        c["name"] = f"T{idx + 1}"
    return corners


def _find_brake_start(rs, start_idx: int, apex_idx: int, step: float) -> float | None:
    """从弯心往回找：最近一段连续刹车的起点。"""
    brake = rs["brake"]
    lo = max(0, start_idx - int(BRAKE_SEARCH_M / step))
    i = apex_idx
    # 先往回找到正在刹车的位置
    while i > lo and brake[i] < BRAKE_ON:
        i -= 1
    if brake[i] < BRAKE_ON:
        return None
    while i > lo and brake[i - 1] >= BRAKE_ON:
        i -= 1
    return float(rs["dist"][i])


def _find_full_throttle(rs, apex_idx: int, step: float) -> float | None:
    thr = rs["throttle"]
    hi = min(len(thr) - 1, apex_idx + int(400 / step))
    for i in range(apex_idx, hi + 1):
        if thr[i] >= FULL_THROTTLE:
            return float(rs["dist"][i])
    return None


def corner_at(corners: list[dict], dist: float, margin: float = 30.0) -> dict | None:
    """给一个距离，找它所在（或最近的）弯；前后各放宽 margin 米。"""
    best, best_gap = None, float("inf")
    for c in corners:
        lo = (c.get("brake_start_m") or c["start_m"]) - margin
        hi = c["end_m"] + margin
        if lo <= dist <= hi:
            gap = abs(dist - c["apex_m"])
            if gap < best_gap:
                best, best_gap = c, gap
    return best
