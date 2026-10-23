"""数据质量检查（数据够不够做诊断）和弯道识别 / 归一化。"""

from __future__ import annotations

import numpy as np

from ..corners import detect_corners
from .common import DASH_CHANNELS, _Ctx

# ================================================================ 数据质量

def _check_quality(ctx: _Ctx) -> bool:
    """基本数据量和 Dash 段检查。返回 False 表示数据不足、不再做任何判断。"""
    g = ctx.r["general"]
    m = ctx.meta
    n = ctx.n
    length = float(ctx.dist[-1] - ctx.dist[0]) if n else 0.0
    duration = float(ctx.t[-1] - ctx.t[0]) if n else 0.0
    if ctx.missing(("t", "dist")):
        ctx.note("error", f"数据不足：缺少基础通道 {', '.join(ctx.missing(('t', 'dist')))}，无法诊断。")
        return False
    if n < g["min_samples"] or length < g["min_lap_m"] or duration < g["min_duration_s"]:
        ctx.note("error", f"数据不足：这段数据只有 {n} 个采样、{length:.0f} m、{duration:.1f} s"
                          f"（至少要 {g['min_samples']} 个采样、{g['min_lap_m']:.0f} m、{g['min_duration_s']:.0f} s），"
                          "不下任何结论。")
        return False

    # Dash 段：Sled 格式没有车速 / 踏板 / 挡位 / 胎温
    fmt = str(m.get("fmt") or "")
    missing = ctx.missing(DASH_CHANNELS)
    if fmt.startswith("sled") or missing:
        why = "包格式是 Sled（只有车身运动数据）" if fmt.startswith("sled") else f"缺少通道 {', '.join(missing)}"
        ctx.note("warn", f"数据不足：{why}，没有车速、油门、刹车、挡位、胎温，只能检查悬挂。")
    else:
        spd, thr, brk = ctx.ch("speed"), ctx.ch("throttle"), ctx.ch("brake")
        if np.max(np.abs(spd)) < 1.0 and not np.any(thr > 0) and not np.any(brk > 0):
            ctx.note("warn", "数据不足：车速、油门、刹车全是 0（像是 Sled 格式或没有 Dash 段），只能检查悬挂。")
        else:
            ctx.dash = True
            ctx.speed_ok = np.abs(spd) >= g["min_speed_ms"]

    hz = 1.0 / ctx.dt if ctx.dt > 0 else 0.0
    if hz < g["low_sample_hz"]:
        ctx.note("warn", f"采样率只有约 {hz:.0f} Hz（低于 {g['low_sample_hz']:.0f} Hz），很短的现象（触底、抱死）可能漏掉。")
    flags = set(m.get("flags") or [])
    if "first_lap" in flags:
        ctx.note("info", "这是本场第一圈（站立起步、冷胎）：胎温只用热胎之后的数据；起步阶段的现象不一定代表调校。")
    if "rewind" in flags:
        ctx.note("info", "这圈中途倒带过：倒带处的数据可能有跳变。")
    if "dist_integrated" in flags:
        ctx.note("info", "这圈的距离是按车速积分算的（游戏给的距离不可靠），位置可能有几米误差。")
    if m.get("is_complete") is False:
        ctx.note("info", "这圈没跑完（不完整圈），结论只代表跑过的部分。")
    if m.get("kind") == "sprint":
        ctx.note("info", "这是一次冲刺赛（点对点），按整段路线分析。")
    return True


def _normalize_corners(corners: list[dict]) -> list[dict]:
    """外部给的弯道表（如 sectors.layout_corners）整理成 detect_corners 的样子：按位置排好、index = 列表下标，
    缺的字段补上（min_speed / heading_change_deg 可能是 None）。"""
    out = []
    for c in sorted(corners, key=lambda c: float(c["apex_m"])):
        c = dict(c)
        c.setdefault("brake_start_m", None)
        c.setdefault("full_throttle_m", None)
        c.setdefault("direction", "right")
        c["heading_change_deg"] = float(c.get("heading_change_deg") or 0.0)
        c["min_speed"] = float(c["min_speed"]) if c.get("min_speed") is not None else float("nan")
        c["index"] = len(out)
        out.append(c)
    return out


def _find_corners(ctx: _Ctx, given: list[dict] | None = None) -> None:
    if not ctx.dash:
        return
    if given:
        try:
            ctx.corners = _normalize_corners(given)
            ctx.corner_source = "layout"
            ctx.note("info", f"弯道编号来自这条赛道的固定弯道表（{len(ctx.corners)} 个弯，和圈速对比页一致）。")
            return
        except (KeyError, TypeError, ValueError) as e:
            ctx.note("warn", f"固定弯道表格式不对（{e}），改用本圈自动识别的弯道。")
    if ctx.missing(("pos_x", "pos_z")):
        ctx.note("warn", "缺少坐标通道（pos_x / pos_z），无法识别弯道：按弯道阶段的判断全部跳过。")
        return
    try:
        ctx.corners = detect_corners(ctx.lap)
    except Exception as e:  # noqa: BLE001  弯道识别失败不应该让整个诊断崩掉
        ctx.note("warn", f"弯道识别失败（{e}），按弯道阶段的判断全部跳过。")
        ctx.corners = []
    if ctx.corners:
        ctx.corner_source = "detected"
    if not ctx.corners:
        ctx.note("warn", "没识别出弯道（坐标可能没变化），按弯道阶段的判断全部跳过。")


def _turn_share(corners: list[dict]) -> dict | None:
    right = sum(c["heading_change_deg"] for c in corners if c["direction"] == "right")
    left = sum(c["heading_change_deg"] for c in corners if c["direction"] == "left")
    tot = right + left
    if tot <= 0:
        return None
    return {"right": right / tot, "left": left / tot, "right_deg": right, "left_deg": left}
