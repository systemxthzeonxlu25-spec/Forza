"""调车诊断：给一圈（或几圈）数据，找出常见的调校问题，每条结论都写清楚证据和判断依据。

原则（用户的要求：别瞎猜）：
- 每条 finding 都带 evidence（实测数字 + 比较的阈值）和 basis（用了哪些通道、哪条规则、为什么能说明问题）。
- 数据不够就说“数据不足”；Data Out 本身给不了的东西列在 not_available 里，不硬下结论。
- 只看数值大小（绝对值），不依赖正负号：真实游戏各通道的正负号约定没有逐个核实。
  （左右弯方向来自方向盘输入 steer，见 corners.py。）
- 阈值全部在 tuning_rules.toml（带中文说明），文件缺失时用这里的 DEFAULT_RULES。

对外接口：
    diagnose(lap, rules=None, corners=None, *, abs_on=None, aero=None) -> dict
        单圈诊断。corners = 外部给的弯道表（例如 sectors.layout_corners 映射好的固定弯道表），
        不给就用 corners.detect_corners 自己识别；abs_on / aero = 用户在页面上勾选的“我开了 ABS”/“有可调空力”
        （None = 没说），只影响刹车 / 平衡结论的解读和措辞。
    diagnose_many(laps, rules=None, corners=None, *, abs_on=None, aero=None) -> dict
        多圈合并（同车同赛道），每圈都出现的问题置信度更高；corners 是和 laps 一一对应的列表（元素可为 None）
    rules_info() / get_rules()                 当前阈值（tuning_rules.toml 改了会自动重新读取）
    track_outline(lap, max_points)             画赛道小地图用的降采样坐标

检查项（都在下面各自的 _check_xxx 函数里，好找好改）：
    转向平衡（按弯道阶段的转向不足 / 转向过度）、悬挂触底 / 伸到底、胎温不平衡、
    出弯空转（含内侧 / 外侧轮、四驱前后轴）、刹车抱死、刹车平衡（直线重刹前后轴滑移）、
    刹车压力（踏板-减速度饱和）、差速器（入弯松刹内侧轮 / 四驱前后分配）、齿比（最高挡撞断油 / 转速余量）。

分析块（不是 finding，是给页面画图的量化数据，见 _balance_block / _brake_block / _body_block）：
    balance_map    平衡指数 B = 前轴 |侧偏角| − 后轴 |侧偏角|，按车速分档 + 弯 × 入弯/弯中/出弯 热力表 + 解读句
    brake_analysis 直线重刹段前后轴滑移对比、踏板-减速度曲线（按车速分档）
    body           车身与悬挂：四轮行程分位数、悬挂速度直方图、侧倾 / 点头梯度
"""
from __future__ import annotations

import copy
import math
import threading
import tomllib
from pathlib import Path

import numpy as np

from .corners import corner_at, detect_corners
from .lapdata import LapData

RULES_PATH = Path(__file__).with_name("tuning_rules.toml")

# 内置默认阈值（和 tuning_rules.toml 一致；每项的含义见 toml 里的注释）
DEFAULT_RULES: dict = {
    "general": {"min_lap_m": 400.0, "min_duration_s": 20.0, "min_samples": 200, "min_speed_ms": 8.0,
                "merge_gap_s": 0.10, "low_sample_hz": 20.0},
    "balance": {"grip_limit": 1.0, "axle_diff": 0.30, "min_duration_s": 0.25, "min_lat_g": 0.30,
                "mid_min_lat_g": 0.50, "entry_min_brake": 0.10, "entry_min_steer": 0.05,
                "exit_min_throttle": 0.50, "exit_extend_m": 30.0, "min_phase_s": 0.30,
                "alert_total_s": 1.50, "alert_corners": 3},
    "suspension": {"bottom_level": 0.98, "lift_level": 0.02, "min_samples": 3, "group_m": 15.0,
                   "kerb_window_s": 0.10, "alert_events": 3, "lift_warn_s": 0.30},
    "tires": {"warm_ref_frac": 0.20, "warm_margin_c": 5.0, "min_warm_s": 30.0, "lr_diff_c": 6.0,
              "lr_alert_c": 12.0, "dominant_share": 0.60, "axle_excess_c": 4.0, "axle_diff_c": 8.0,
              "driven_axle_bonus_c": 4.0, "rising_c_per_min": 3.0, "sane_min_c": 40.0, "sane_max_c": 150.0},
    "traction": {"slip_limit": 1.0, "min_throttle": 0.50, "max_brake": 0.10, "min_duration_s": 0.10,
                 "alert_total_s": 1.0, "alert_events": 3},
    "brakes": {"slip_limit": 1.0, "min_brake": 0.30, "min_duration_s": 0.10, "alert_total_s": 0.50,
               "alert_events": 3},
    "gearing": {"limiter_frac": 0.98, "min_duration_s": 1.0, "merge_gap_s": 0.25, "alert_total_s": 3.0,
                "min_gear_time_s": 1.0, "headroom_frac": 0.85},
    "brake_balance": {"min_brake": 0.50, "max_steer": 0.05, "min_speed_kmh": 60.0, "kerb_window_s": 0.10,
                      "min_zone_s": 0.30, "slip_mark": 0.80, "peak_pct": 95.0, "bias_diff": 0.15,
                      "min_eval_peak": 0.30, "mark_lead_s": 0.05, "min_zones": 2, "dominant_share": 0.50},
    "brake_pressure": {"min_brake": 0.05, "max_steer": 0.05, "max_throttle": 0.05, "min_speed_kmh": 60.0,
                       "kerb_window_s": 0.10, "pedal_bin": 0.10, "min_bin_s": 0.15, "min_bins": 4, "min_span": 0.40,
                       "sat_frac": 0.95, "sat_warn_pedal": 0.80, "min_bins_above": 2, "full_pedal": 0.80,
                       "speed_band_quantiles": [0.5], "speed_band_edges_kmh": [], "coast_max_pedal": 0.02,
                       "min_coast_s": 1.0},
    "differential": {"min_lat_g": 0.30, "lift_max_throttle": 0.15, "lift_max_brake": 0.10, "slip_limit": 1.0,
                     "min_duration_s": 0.10, "alert_corners": 3, "sa_rise": 0.20, "sa_window_s": 0.30,
                     "min_lift_s": 0.50, "awd_ratio": 1.6, "awd_min_slip": 0.50, "awd_min_s": 0.30},
    "balance_map": {"speed_band_quantiles": [0.33, 0.67], "speed_band_edges_kmh": [], "min_band_s": 1.0,
                    "tendency_b": 0.15, "tendency_share": 0.15, "color_full_b": 0.50, "top_cells": 3},
    "body": {"kerb_window_s": 0.10, "damper_split_mm_s": 25.0, "hist_bin_mm_s": 10.0, "hist_max_mm_s": 200.0,
             "max_gap_factor": 3.0, "roll_min_lat_g": 0.50, "steady_max_dg_s": 1.0, "pitch_min_long_g": 0.30,
             "pitch_max_lat_g": 0.20, "cruise_max_long_g": 0.10, "near_bottom": 0.95, "near_top": 0.05,
             "min_samples": 120, "min_fit_s": 1.0, "grade_smooth_m": 10.0},
    "confidence": {"high_factor": 4.0, "medium_factor": 1.5, "margin": 0.10},
    "multi": {"sporadic_frac": 0.5},
}

WHEELS = ("fl", "fr", "rl", "rr")
WHEEL_CN = {"fl": "左前", "fr": "右前", "rl": "左后", "rr": "右后", "front": "前轴", "rear": "后轴", "all": "四轮"}
AXLE_WHEELS = {"front": ("fl", "fr"), "rear": ("rl", "rr")}
PHASE_CN = {"entry": "入弯", "mid": "弯中", "exit": "出弯", "straight": "直道"}
PHASES = ("entry", "mid", "exit")
CATEGORY_CN = {"balance": "转向平衡", "suspension": "悬挂", "tires": "轮胎", "traction": "牵引",
               "brakes": "刹车", "differential": "差速器", "gearing": "齿比"}
SEVERITY_RANK = {"info": 0, "warn": 1, "alert": 2}
CONF_LEVELS = ("low", "medium", "high")
DRIVETRAIN_CN = {0: "前驱 FWD", 1: "后驱 RWD", 2: "四驱 AWD"}
DRIVEN_WHEELS = {0: ("fl", "fr"), 1: ("rl", "rr"), 2: WHEELS}
DASH_CHANNELS = ("speed", "throttle", "brake", "gear", "steer")
AXLE_CN = {"front": "前轴", "rear": "后轴"}

# 所有检查项（id, 名字）：数据不足时逐项标成“没法判断”，多圈合并时按这个顺序汇总
CHECK_LIST = (
    ("balance", "转向平衡（转向不足 / 转向过度）"), ("suspension", "悬挂触底 / 伸到底"), ("tires", "胎温平衡"),
    ("traction", "驱动轮空转"), ("brakes", "刹车抱死"), ("brake_balance", "刹车平衡（直线重刹时前后轴滑移）"),
    ("brake_pressure", "刹车压力（踏板-减速度饱和）"), ("differential", "差速器（入弯松刹内侧轮 / 四驱前后分配）"),
    ("gearing", "齿比（最高挡撞断油 / 转速余量）"),
)
CHECK_NAME = dict(CHECK_LIST)

ONE_AT_A_TIME = "一次只改一项、每次小步改，改完跑几圈对比圈速和这里的诊断。"

# Data Out 本身给不了、所以这里判断不了的东西（固定列表，前端单独展示）
NOT_AVAILABLE: list[dict] = [
    {"item": "胎面内/中/外温度",
     "why": "Data Out 每条轮胎只给一个温度，看不出胎面内侧、中间、外侧谁更热，所以没法据此判断外倾角（camber）是否合适。",
     "instead": "在游戏里打开遥测（Telemetry）的轮胎页面，看每条胎内 / 中 / 外三段温度再调外倾角。"},
    {"item": "胎压",
     "why": "Data Out 不发送胎压。",
     "instead": "游戏内遥测的轮胎页面能看到热胎压。"},
    {"item": "当前的调校数值",
     "why": "Data Out 不发送弹簧、阻尼、防倾杆、差速器、齿比、刹车平衡等设定值，所以这里只能给“往哪个方向调”，给不了具体数值。",
     "instead": "对照游戏调校界面里的当前值，小步调整。"},
    {"item": "车一共有几个挡、每挡齿比",
     "why": "Data Out 只给当前挡位，看不出这车最高有几挡，只能看到这圈用到的最高挡。",
     "instead": "在游戏调校界面查看齿比。"},
    {"item": "ABS / 牵引力控制（TCS）/ 稳定控制（STM）是否开启",
     "why": "Data Out 不发送辅助设置；开着 ABS / TCS 时抱死、空转会被压住，看起来“没问题”不代表调校没问题。",
     "instead": "诊断调校时建议记下自己开了哪些辅助。"},
    {"item": "刹车温度 / 刹车衰减、下压力数值",
     "why": "Data Out 没有这些通道。",
     "instead": "游戏内遥测页面（如有）。"},
    {"item": "轮胎最佳工作温度",
     "why": "不同胎种、不同游戏的最佳胎温不一样，Data Out 也不给，所以这里只比较轮胎之间的温差，不评价绝对温度高低。",
     "instead": "参考游戏内遥测里胎温的颜色提示。"},
]

# ================================================================ 阈值读取

_rules_lock = threading.Lock()
_rules_cache: dict = {"key": "unset", "rules": None, "source": None, "error": None}


def _merge(base: dict, over: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


def load_rules(path: str | Path | None = None) -> tuple[dict, str | None, str | None]:
    """读阈值文件并和默认值合并。返回 (rules, 实际读取的文件路径或 None, 错误信息或 None)。"""
    p = Path(path) if path else RULES_PATH
    if not p.exists():
        return copy.deepcopy(DEFAULT_RULES), None, None
    try:
        with open(p, "rb") as f:
            data = tomllib.load(f)
    except (OSError, tomllib.TOMLDecodeError) as e:
        return copy.deepcopy(DEFAULT_RULES), None, f"{p.name} 读取失败，已改用内置默认阈值：{e}"
    return _merge(DEFAULT_RULES, data), str(p), None


def rules_info() -> dict:
    """当前生效的阈值：{"rules", "source", "error"}。toml 文件改了（修改时间/大小变化）会自动重新读取。"""
    try:
        st = RULES_PATH.stat()
        key = (st.st_mtime_ns, st.st_size)
    except OSError:
        key = None
    with _rules_lock:
        if _rules_cache["rules"] is None or _rules_cache["key"] != key:
            rules, src, err = load_rules()
            _rules_cache.update(key=key, rules=rules, source=src, error=err)
        return {"rules": copy.deepcopy(_rules_cache["rules"]), "source": _rules_cache["source"],
                "error": _rules_cache["error"]}


def get_rules() -> dict:
    return rules_info()["rules"]


# ================================================================ 小工具

def _runs(mask: np.ndarray, t: np.ndarray, max_gap_s: float, dt: float) -> list[tuple[int, int]]:
    """mask 为 True 的连续片段 [(起点下标, 终点下标)]（含两端）；中间断开不超过 max_gap_s 的合并成一段。"""
    idx = np.flatnonzero(mask)
    if idx.size == 0:
        return []
    gaps = np.diff(t[idx])
    brk = np.flatnonzero(gaps > max_gap_s + 1.5 * dt)
    starts = np.concatenate([[0], brk + 1])
    ends = np.concatenate([brk, [idx.size - 1]])
    return [(int(idx[s]), int(idx[e])) for s, e in zip(starts, ends)]


def _wheel_code(wheels) -> str | None:
    ws = sorted(set(wheels), key=WHEELS.index)
    if not ws:
        return None
    if len(ws) == 4:
        return "all"
    if ws == ["fl", "fr"]:
        return "front"
    if ws == ["rl", "rr"]:
        return "rear"
    return ",".join(ws)


def _inner_wheels(corner: dict | None) -> tuple[str, str] | None:
    """弯内侧的两个轮子（右弯 = 右侧轮 fr/rr，左弯 = 左侧轮 fl/rl）。方向来自 corners 的 direction（方向盘方向）。"""
    if not corner or corner.get("direction") not in ("left", "right"):
        return None
    return ("fr", "rr") if corner["direction"] == "right" else ("fl", "rl")


def _wheel_cn(code: str | None) -> str:
    if not code:
        return ""
    return "、".join(WHEEL_CN.get(c, c) for c in code.split(","))


def _pct(x: float) -> str:
    return f"{x * 100:.0f}%"


def _corner_key(code: str | None):
    """弯道编号排序（T1 < T2 < T10）；不是 T+数字 的放最后按字母排，不会崩。"""
    s = str(code or "")
    return (0, int(s[1:]), s) if s[:1] == "T" and s[1:].isdigit() else (1, 0, s)


def _corner_label(c: dict | None) -> str:
    """弯道显示名：固定弯道表里改过的名字（label）优先，否则用编号。"""
    if not c:
        return ""
    lbl = c.get("label")
    return str(lbl) if lbl and lbl != c["name"] else c["name"]


def _lower(conf: str, steps: int = 1) -> str:
    return CONF_LEVELS[max(0, CONF_LEVELS.index(conf) - steps)]


def _raise(conf: str, steps: int = 1) -> str:
    return CONF_LEVELS[min(len(CONF_LEVELS) - 1, CONF_LEVELS.index(conf) + steps)]


def _confidence(rules: dict, total_s: float, min_s: float, margin: float) -> str:
    """按“累计时长是最短时长的几倍”和“超出阈值的幅度”给置信度（单圈）。"""
    c = rules["confidence"]
    if total_s >= min_s * c["high_factor"] and margin >= c["margin"]:
        return "high"
    if total_s >= min_s * c["medium_factor"] or margin >= c["margin"]:
        return "medium"
    return "low"


def _json_safe(x):
    """递归转成普通 Python 类型；NaN / inf → None。"""
    if isinstance(x, dict):
        return {str(k): _json_safe(v) for k, v in x.items()}
    if isinstance(x, (list, tuple)):
        return [_json_safe(v) for v in x]
    if isinstance(x, (np.bool_, bool)):
        return bool(x)
    if isinstance(x, (np.integer, int)):
        return int(x)
    if isinstance(x, (np.floating, float)):
        f = float(x)
        return f if math.isfinite(f) else None
    if isinstance(x, np.ndarray):
        return _json_safe(x.tolist())
    return x


def _finding(fid: str, category: str, severity: str, title: str, summary: str, evidence: list[str],
             locations: list[dict], basis: dict, suggestions: list[str], confidence: str,
             caveat: str | None = None, data: dict | None = None) -> dict:
    return {
        "id": fid, "category": category, "category_label": CATEGORY_CN[category], "severity": severity,
        "title": title, "summary": summary, "evidence": evidence, "locations": locations, "basis": basis,
        "suggestions": suggestions, "caveat": caveat, "confidence": confidence, "data": data or {},
    }


# ================================================================ 单圈上下文

class _Ctx:
    """一圈数据 + 阈值 + 诊断过程中攒的结果。"""

    def __init__(self, lap: LapData, rules: dict, *, abs_on: bool | None = None, aero: bool | None = None):
        self.lap, self.r, self.meta = lap, rules, lap.meta or {}
        self.abs_on, self.aero = abs_on, aero
        self.n = len(lap)
        self.t = np.asarray(lap["t"], float) if lap.has("t") else np.arange(self.n) / 60.0
        self.dist = np.asarray(lap["dist"], float) if lap.has("dist") else np.zeros(self.n)
        dts = np.diff(self.t)
        dts = dts[dts > 0]
        self.dt = float(np.median(dts)) if dts.size else 1.0 / 60.0
        self.corners: list[dict] = []
        self.findings: list[dict] = []
        self.checks: list[dict] = []
        self.quality: list[dict] = []
        self.dash = False
        self.speed_ok = np.ones(self.n, bool)
        self.extra: dict = {}
        self.corner_source = "none"       # "layout" = 外部给的固定弯道表；"detected" = 本圈自己识别；"none" = 没有
        self.samples: dict = {}           # 给分析块（平衡分布 / 刹车 / 车身）攒的采样，多圈合并时直接拼起来，不进 JSON

    # ---- 通道 ----
    def ch(self, name: str) -> np.ndarray | None:
        a = self.lap.ch.get(name)
        return None if a is None else np.nan_to_num(np.asarray(a, float))

    def missing(self, names) -> list[str]:
        return [n for n in names if not self.lap.has(n)]

    # ---- 记录 ----
    def note(self, level: str, text: str) -> None:
        self.quality.append({"level": level, "text": text})

    def check(self, cid: str, name: str, status: str, note: str) -> None:
        """status：found = 发现问题；ok = 检查过、没发现；skipped = 数据不足 / 没法判断。"""
        self.checks.append({"id": cid, "name": name, "status": status, "note": note})

    # ---- 时间 / 位置 ----
    def dur(self, i0: int, i1: int) -> float:
        return float(self.t[i1] - self.t[i0] + self.dt)

    def phase_at(self, c: dict | None, i: int) -> str:
        """通用的阶段判断（给空转、抱死、触底等定位用）：弯心前 = 入弯；弯心后油门大或已出弯 = 出弯；否则弯中。"""
        if c is None:
            return "straight"
        d = self.dist[i]
        if d < c["apex_m"]:
            return "entry"
        thr = self.ch("throttle")
        if d > c["end_m"] or (thr is not None and thr[i] >= self.r["balance"]["exit_min_throttle"]):
            return "exit"
        return "mid"

    def cn(self, code: str | None) -> str:
        """弯道的显示名：固定弯道表里用户改过名字的用 label，否则就是编号 T1…"""
        c = next((c for c in self.corners if c["name"] == code), None)
        return _corner_label(c) if c else (code or "")

    def corner(self, code: str | None) -> dict | None:
        return next((c for c in self.corners if c["name"] == code), None)

    def where(self, corner: dict | None, phase: str, d: float) -> str:
        if corner is not None:
            return f"{_corner_label(corner)} {PHASE_CN.get(phase, phase)}"
        nxt = next((c for c in self.corners if (c.get("brake_start_m") or c["start_m"]) > d), None)
        return f"{_corner_label(nxt)} 前的直道" if nxt else "直道"

    def loc(self, i0: int, i1: int, *, wheel: str | None = None, corner: dict | None = None,
            phase: str | None = None, use_corner: bool = True) -> dict:
        d0, d1 = float(self.dist[i0]), float(self.dist[i1])
        if corner is None and use_corner and self.corners:
            corner = corner_at(self.corners, d0)
        if phase is None:
            phase = self.phase_at(corner, i0)
        span = f"{d0:.0f} m" if d1 - d0 < 5 else f"{d0:.0f}–{d1:.0f} m"
        label = " · ".join(x for x in (self.where(corner, phase, d0), _wheel_cn(wheel), span) if x)
        return {"dist_m": round(d0, 1), "end_m": round(d1, 1), "corner": corner["name"] if corner else None,
                "phase": phase, "phase_label": PHASE_CN.get(phase, phase), "wheel": wheel,
                "wheel_label": _wheel_cn(wheel) or None, "t_s": round(float(self.t[i0]), 2),
                "duration_s": round(self.dur(i0, i1), 2), "label": label}


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
            m = m & ctx.speed_ok & (phase == -1)
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
                f"（入弯 {evaluated[0]} / 弯中 {evaluated[1]} / 出弯 {evaluated[2]}）")
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


# ================================================================ ② 悬挂触底 / 伸到底

def _check_suspension(ctx: _Ctx) -> None:
    name = "悬挂触底 / 伸到底"
    need = [f"susp_{w}" for w in WHEELS]
    if ctx.missing(need):
        ctx.check("suspension", name, "skipped", "数据不足：缺少悬挂行程通道。")
        return
    r = ctx.r["suspension"]
    k_kerb = max(1, round(r["kerb_window_s"] / ctx.dt))
    events = []
    for w in WHEELS:
        s = ctx.ch(f"susp_{w}")
        rum = ctx.ch(f"rumble_{w}")
        for kind, mask in (("bottom", s >= r["bottom_level"]), ("lift", s <= r["lift_level"])):
            for i0, i1 in _runs(mask, ctx.t, 0.0, ctx.dt):
                if i1 - i0 + 1 < r["min_samples"]:
                    continue
                kerb = rum is not None and bool(np.any(rum[max(0, i0 - k_kerb): i1 + k_kerb + 1] > 0.5))
                seg = s[i0:i1 + 1]
                events.append({"kind": kind, "wheel": w, "axle": "front" if w in ("fl", "fr") else "rear",
                               "i0": i0, "i1": i1, "kerb": kerb,
                               "peak": float(seg.max() if kind == "bottom" else seg.min())})

    # 按 (现象, 是否路肩, 轴) 分组，再按距离聚成“位置”
    groups: dict[tuple, list[list[dict]]] = {}
    for e in sorted(events, key=lambda e: ctx.dist[e["i0"]]):
        key = (e["kind"], e["kerb"], "any" if e["kerb"] else e["axle"])
        clusters = groups.setdefault(key, [])
        if clusters and ctx.dist[e["i0"]] - max(ctx.dist[x["i1"]] for x in clusters[-1]) <= r["group_m"]:
            clusters[-1].append(e)
        else:
            clusters.append([e])

    found = 0
    for (kind, kerb, axle), clusters in groups.items():
        if kerb:
            continue
        found += 1
        ctx.findings.append(_susp_finding(ctx, kind, axle, clusters))
    kerb_clusters = [(k[0], c) for k, cl in groups.items() if k[1] for c in cl]
    if kerb_clusters:
        found += 1
        ctx.findings.append(_kerb_finding(ctx, kerb_clusters))
    lvl = f"压到底 ≥ {r['bottom_level']:.2f}、伸到底 ≤ {r['lift_level']:.2f}，连续 ≥ {r['min_samples']} 个采样"
    lo = min(float(ctx.ch(f"susp_{w}").min()) for w in WHEELS)
    hi = max(float(ctx.ch(f"susp_{w}").max()) for w in WHEELS)
    if found:
        n_place = {k: len(v) for k, v in groups.items()}
        parts = []
        for kind, word in (("bottom", "触底"), ("lift", "伸到底")):
            for axle in ("front", "rear"):
                k = n_place.get((kind, False, axle), 0)
                if k:
                    parts.append(f"{WHEEL_CN[axle]}{word} {k} 处")
        kerb = len(kerb_clusters)
        if kerb:
            parts.append(f"压路肩时 {kerb} 处")
        ctx.check("suspension", name, "found", f"四轮悬挂行程范围 {lo:.2f}~{hi:.2f}；{'，'.join(parts)}（{lvl}）。")
    else:
        ctx.check("suspension", name, "ok", f"四轮悬挂行程范围 {lo:.2f}~{hi:.2f}，没有触底或伸到底（{lvl}）。")


def _cluster_context(ctx: _Ctx, cl: list[dict]) -> dict:
    i0, i1 = min(e["i0"] for e in cl), max(e["i1"] for e in cl)
    sl = slice(i0, i1 + 1)
    out = {"i0": i0, "i1": i1, "wheels": sorted({e["wheel"] for e in cl}, key=WHEELS.index),
           "dur": max(ctx.dur(e["i0"], e["i1"]) for e in cl), "peak": cl[0]["peak"]}
    out["peak"] = max(e["peak"] for e in cl) if cl[0]["kind"] == "bottom" else min(e["peak"] for e in cl)
    for ch, key in (("brake", "brk"), ("throttle", "thr"), ("speed", "spd")):
        a = ctx.ch(ch) if ctx.dash else None
        out[key] = float(a[sl].mean()) if a is not None else None
    gl = ctx.ch("g_lat")
    out["glat"] = float(np.abs(gl[sl]).mean()) if gl is not None else None
    gv = ctx.ch("g_vert")
    out["gvert"] = float(np.abs(gv[sl] - np.median(gv)).max()) if gv is not None else None
    if out["brk"] is not None and out["brk"] >= 0.3:
        out["cause"] = "brake"
    elif out["glat"] is not None and out["glat"] >= 0.8:
        out["cause"] = "corner"
    else:
        out["cause"] = "bump"
    return out


def _susp_finding(ctx: _Ctx, kind: str, axle: str, clusters: list[list[dict]]) -> dict:
    r = ctx.r["suspension"]
    axle_cn = WHEEL_CN[axle]
    infos = [_cluster_context(ctx, cl) for cl in clusters]
    total = sum(x["dur"] for x in infos)
    min_s = r["min_samples"] * ctx.dt
    evidence, locs = [], []
    cause_cn = {"brake": "刹车点头时", "corner": "过弯侧倾时", "bump": "直线上 / 压到起伏时"}
    for x in infos:
        wheel = _wheel_code(x["wheels"])
        loc = ctx.loc(x["i0"], x["i1"], wheel=wheel)
        locs.append(loc)
        ctx_txt = []
        if x["spd"] is not None:
            ctx_txt.append(f"车速 {x['spd'] * 3.6:.0f} km/h")
        if x["brk"] is not None:
            ctx_txt.append(f"刹车 {_pct(x['brk'])}")
        if x["glat"] is not None:
            ctx_txt.append(f"横向 G {x['glat']:.2f}")
        if x["gvert"] is not None:
            ctx_txt.append(f"垂直 G 相对本圈中位数最大变化 {x['gvert']:.2f}")
        word = "压到底" if kind == "bottom" else "伸到底"
        evidence.append(f"{loc['label']}：{_wheel_cn(wheel)}悬挂行程 {x['peak']:.2f}（{word}），持续约 {x['dur']:.2f} s；"
                        f"前后 {r['kerb_window_s']:.2f} s 内没压路肩（rumble = 0）；{cause_cn[x['cause']]}（{'，'.join(ctx_txt)}）")
    causes = {x["cause"] for x in infos}
    if kind == "bottom":
        sev = "alert" if len(infos) >= r["alert_events"] else "warn"
        evidence.append(f"判定阈值：归一化悬挂行程 ≥ {r['bottom_level']:.2f} 连续 ≥ {r['min_samples']} 个采样，"
                        f"且不在压路肩时。本圈{axle_cn}共 {len(infos)} 处（≥ {r['alert_events']} 处记为严重）")
        title = f"{axle_cn}悬挂触底"
        summary = (f"{axle_cn}悬挂在 {len(infos)} 处被压到底（没压路肩），"
                   f"{'、'.join(cause_cn[c] for c in sorted(causes))}发生：触底瞬间轮胎抓地会突然变化、底盘可能擦地。")
        tips = [f"{axle_cn}弹簧调硬一点", f"{axle_cn}压缩（bump）阻尼调硬一点",
                f"{axle_cn}车身高度调高一点（会抬高重心，小步改）"]
        if "brake" in causes and axle == "front":
            tips.append("刹车点头触底：也可以试试刹车别一脚踩死，或前防倾杆略硬")
        if "corner" in causes:
            tips.append(f"过弯侧倾触底：{axle_cn}防倾杆调硬一点")
        caveat = "偶尔一次影响不大；每圈同一位置都触底才值得改。" + ONE_AT_A_TIME
        rule = (f"每个轮子：归一化悬挂行程 ≥ {r['bottom_level']:.2f} 连续 ≥ {r['min_samples']} 个采样记一次；"
                f"前后 {r['kerb_window_s']:.2f} s 内这个轮子 rumble = 1 的算“压路肩”，单独列为提示；"
                f"相距 < {r['group_m']:.0f} m 的归成同一处。")
        principle = ("悬挂行程 1 = 压到底：弹簧已经没有行程，冲击直接传给车身和轮胎，轮胎载荷瞬间突变、抓地不稳。"
                     "没压路肩时触底，说明这根轴的弹簧 / 压缩阻尼 / 车身高度不够应付这里的起伏或载荷转移。")
        conf = "high" if len(infos) >= 2 and total >= min_s * ctx.r["confidence"]["high_factor"] else \
            ("medium" if total >= min_s * ctx.r["confidence"]["medium_factor"] else "low")
    else:
        sev = "warn" if total >= r["lift_warn_s"] else "info"
        evidence.append(f"判定阈值：归一化悬挂行程 ≤ {r['lift_level']:.2f} 连续 ≥ {r['min_samples']} 个采样，且不在压路肩时。"
                        f"本圈累计 {total:.2f} s（≥ {r['lift_warn_s']:.2f} s 记为注意，否则只提示）")
        title = f"{axle_cn}悬挂伸到底（可能离地）"
        summary = (f"{axle_cn}悬挂在 {len(infos)} 处伸到最长（没压路肩），"
                   f"{'过弯时多半是内侧轮被抬起' if 'corner' in causes else '多半是过坡 / 起伏时车轮下垂'}：这一瞬间这个轮子几乎不出力。")
        tips = []
        if "corner" in causes:
            tips += [f"过弯内侧轮离地：{axle_cn}防倾杆调软一点", f"{axle_cn}回弹（rebound）阻尼调软一点，让车轮更快伸下去贴地"]
        tips += [f"过坡离地一般不用管；频繁发生可以试 {axle_cn}弹簧调软一点 / 车身高度调高一点"]
        caveat = "悬挂伸到底不一定等于离地（Data Out 没有“轮子离地”这个字段），这里是推断。" + ONE_AT_A_TIME
        rule = (f"每个轮子：归一化悬挂行程 ≤ {r['lift_level']:.2f} 连续 ≥ {r['min_samples']} 个采样记一次；"
                f"压路肩时的单独列为提示；相距 < {r['group_m']:.0f} m 的归成同一处。")
        principle = "悬挂行程 0 = 伸到最长：车轮已经挂到行程尽头，再往上抬车身轮子就离地了，这个轮子此时基本没有载荷和抓地。"
        conf = "medium" if total >= min_s * ctx.r["confidence"]["medium_factor"] else "low"
    basis = {"channels": ["susp_fl/fr/rl/rr（归一化悬挂行程，0 = 伸到最长，1 = 压到底）", "rumble_*（压路肩）",
                          "brake / g_lat / g_vert / speed（判断当时在刹车、过弯还是压起伏）"],
             "rule": rule, "principle": principle}
    fid = f"{'bottoming' if kind == 'bottom' else 'wheel_lift'}_{axle}"
    return _finding(fid, "suspension", sev, title, summary, evidence, locs, basis, tips, conf, caveat,
                    data={"kind": kind, "axle": axle, "places": len(infos), "total_s": round(total, 3)})


def _kerb_finding(ctx: _Ctx, kerb_clusters: list[tuple[str, list[dict]]]) -> dict:
    r = ctx.r["suspension"]
    evidence, locs = [], []
    for kind, cl in kerb_clusters:
        x = _cluster_context(ctx, cl)
        wheel = _wheel_code(x["wheels"])
        loc = ctx.loc(x["i0"], x["i1"], wheel=wheel)
        locs.append(loc)
        word = "压到底" if kind == "bottom" else "伸到底"
        evidence.append(f"{loc['label']}：{_wheel_cn(wheel)}悬挂{word}（{x['peak']:.2f}），同时压着路肩（rumble = 1），持续约 {x['dur']:.2f} s")
    evidence.append(f"判定：悬挂行程 ≥ {r['bottom_level']:.2f} 或 ≤ {r['lift_level']:.2f} 的前后 {r['kerb_window_s']:.2f} s 内该轮压了路肩")
    basis = {"channels": ["susp_fl/fr/rl/rr", "rumble_fl/fr/rl/rr"],
             "rule": "悬挂触底 / 伸到底发生时，同一个轮子正在压路肩（rumble = 1），归为“路肩造成”。",
             "principle": "路肩是突然的高低差，压上去时悬挂被瞬间压到底或掉下去都很常见，不一定是调校问题。"}
    tips = ["如果压路肩时车不稳：压缩（bump）阻尼调软一点，更“吃”路肩", "或者少压 / 换条走线避开高路肩"]
    return _finding("kerb_strike", "suspension", "info", "压路肩时悬挂触底 / 伸到底",
                    f"有 {len(locs)} 处悬挂到极限时正压着路肩，多半是路肩造成的，属于常见现象。",
                    evidence, locs, basis, tips, "medium", "只是提示：如果压这些路肩时车没有明显不稳，可以不管。",
                    data={"places": len(locs)})


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
                                          f"没超过 {r['slip_limit']:.2f}。")
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
                                         f"|滑移率| 最大 {mx:.2f}（阈值 {r['slip_limit']:.2f}）。")


# ================================================================ ⑤ 刹车抱死

def _check_brakes(ctx: _Ctx) -> None:
    name = "刹车抱死"
    need = [f"slip_ratio_{w}" for w in WHEELS] + ["brake"]
    if not ctx.dash or ctx.missing(need):
        ctx.check("brakes", name, "skipped", "数据不足：需要滑移率和刹车。")
        return
    r = ctx.r["brakes"]
    g = ctx.r["general"]
    brk = ctx.ch("brake")
    steer = np.abs(ctx.ch("steer"))
    braking = (brk >= r["min_brake"]) & ctx.speed_ok
    sr = {w: np.abs(ctx.ch(f"slip_ratio_{w}")) for w in WHEELS}
    lock = {w: braking & (sr[w] > r["slip_limit"]) for w in WHEELS}
    found = []
    for axle, other in (("front", "rear"), ("rear", "front")):
        aw, ow = AXLE_WHEELS[axle], AXLE_WHEELS[other]
        m = lock[aw[0]] | lock[aw[1]]
        events = []
        for i0, i1 in _runs(m, ctx.t, g["merge_gap_s"], ctx.dt):
            dur = ctx.dur(i0, i1)
            if dur < r["min_duration_s"]:
                continue
            sl = slice(i0, i1 + 1)
            q = m[sl]
            wheels = [w for w in aw if lock[w][sl].any()]
            first = {}
            for ax2 in (axle, other):
                hits = np.flatnonzero((lock[AXLE_WHEELS[ax2][0]] | lock[AXLE_WHEELS[ax2][1]])[max(0, i0 - 30): i1 + 1])
                first[ax2] = int(hits[0]) if hits.size else None
            events.append({"i0": i0, "i1": i1, "dur": dur, "wheels": wheels,
                           "mean": float(np.mean([sr[w][sl][q].mean() for w in aw])),
                           "peak": max(float(sr[w][sl].max()) for w in aw),
                           "other": float(np.mean([sr[w][sl][q].mean() for w in ow])),
                           "other_locked": bool(any(lock[w][sl].any() for w in ow)),
                           "brk": float(brk[sl].mean()), "steer": float(steer[sl].mean()),
                           "first": axle if first[other] is None or (first[axle] is not None and first[axle] <= first[other]) else other})
        if not events:
            continue
        found.append(f"{WHEEL_CN[axle]}抱死 {len(events)} 次")
        ctx.findings.append(_lock_finding(ctx, axle, other, events))
    mx = max(float(sr[w][braking].max()) if braking.any() else 0.0 for w in WHEELS)
    if found:
        ctx.check("brakes", name, "found", f"刹车（≥ {_pct(r['min_brake'])}）时{'，'.join(found)}；四轮 |滑移率| 最大 {mx:.2f}"
                                           f"（阈值 {r['slip_limit']:.2f}）。")
    else:
        ctx.check("brakes", name, "ok", f"刹车（≥ {_pct(r['min_brake'])}）时四轮 |滑移率| 最大 {mx:.2f}，没超过 {r['slip_limit']:.2f}。")


def _lock_finding(ctx: _Ctx, axle: str, other: str, events: list[dict]) -> dict:
    r = ctx.r["brakes"]
    g = ctx.r["general"]
    axle_cn, other_cn = WHEEL_CN[axle], WHEEL_CN[other]
    total = sum(e["dur"] for e in events)
    sev = "alert" if total >= r["alert_total_s"] or len(events) >= r["alert_events"] else "warn"
    locs, evidence = [], []
    for e in events:
        loc = ctx.loc(e["i0"], e["i1"], wheel=_wheel_code(e["wheels"]))
        locs.append(loc)
        evidence.append(f"{loc['label']}（持续 {e['dur']:.2f} s）：刹车 {_pct(e['brk'])}，方向盘 {_pct(e['steer'])}；"
                        f"{axle_cn} |滑移率| 平均 {e['mean']:.2f}、最大 {e['peak']:.2f}；同一时间{other_cn}平均 {e['other']:.2f}"
                        + ("（也抱死了）" if e["other_locked"] else "（没抱死）"))
    n_first = sum(1 for e in events if e["first"] == axle)
    evidence.append(f"{len(events)} 次里有 {n_first} 次是{axle_cn}先抱死")
    evidence.append(f"判定阈值：刹车 ≥ {_pct(r['min_brake'])} 时 |滑移率| > {r['slip_limit']:.2f}，连续 ≥ {r['min_duration_s']:.2f} s。"
                    f"本圈 {len(events)} 次、累计 {total:.2f} s（累计 ≥ {r['alert_total_s']:.1f} s 或 ≥ {r['alert_events']} 次记为严重）")
    corners = sorted({l["corner"] for l in locs if l["corner"]}, key=_corner_key)
    if axle == "front":
        tips = ["刹车平衡往后挪 1~2%（前轮刹车力少一点）", "刹车压力调小一点（踩到底也不那么容易抱死）",
                "先排除驾驶：别一脚踩死，刹车时少带方向（又刹又转时前轮更容易抱死）"]
        principle_extra = "前轮抱死时车不听方向，直接往前冲。"
    else:
        tips = ["刹车平衡往前挪 1~2%（后轮刹车力少一点）", "刹车压力调小一点",
                "先排除驾驶：降挡别太早太快（后驱车发动机制动会让后轮拖滑）"]
        principle_extra = "后轮抱死时车尾失去侧向抓地，很容易甩尾。"
    basis = {"channels": ["slip_ratio_*（归一化滑移率）", "brake", "steer", "speed"],
             "rule": (f"刹车 ≥ {_pct(r['min_brake'])}、车速 ≥ {g['min_speed_ms']:.0f} m/s 时，{axle_cn}任一轮 |滑移率| > {r['slip_limit']:.2f} "
                      f"连续 ≥ {r['min_duration_s']:.2f} s 记一次；同时对比另一根轴的滑移率，看哪根轴先抱死 / 更容易抱死。"),
             "principle": ("刹车时滑移率衡量轮子比车速转得慢多少，归一化后 |x| > 1 表示轮胎过了纵向抓地极限，轮子接近不转（抱死）。"
                           + principle_extra + "某一根轴总是先抱死，说明刹车力分配偏向这根轴（刹车平衡）或总刹车力太大。"
                           "用绝对值 + 刹车踏板区分抱死和空转，不依赖正负号。")}
    conf = _confidence(ctx.r, total, r["min_duration_s"], float(np.mean([e["mean"] for e in events])) / r["slip_limit"] - 1.0)
    return _finding(f"brake_lock_{axle}", "brakes", sev, f"{axle_cn}刹车抱死" + (f"（{'、'.join(ctx.cn(c) for c in corners)}）" if corners else ""),
                    f"刹车时{axle_cn}抱死 {len(events)} 次、累计 {total:.2f} s，{other_cn}基本正常：刹车力偏向{axle_cn}或太大。",
                    evidence, locs, basis, tips, conf, _lock_caveat(ctx.abs_on) + ONE_AT_A_TIME,
                    data={"axle": axle, "events": len(events), "total_s": round(total, 3), "first_count": n_first,
                          "abs_on": ctx.abs_on})


def _lock_caveat(abs_on: bool | None) -> str:
    if abs_on is True:
        return ("你勾选了“我开了 ABS”：开着 ABS 还出现 |滑移率| > 1，说明 ABS 介入之前轮子已经超过极限（ABS 只能压住一部分），"
                "刹车压力 / 刹车平衡偏向这根轴的可能性更大。")
    if abs_on is False:
        return "没开 ABS：这里的抱死就是真的抱死，靠刹车压力、刹车平衡和踩法来解决。"
    return "开着 ABS 时一般不会抱死；Data Out 看不到 ABS 是否开启，开了的话请在上面勾选“我开了 ABS”。"


# ================================================================ ⑤b 刹车平衡（直线重刹时前后轴滑移）

def _near_kerb(ctx: _Ctx, window_s: float) -> np.ndarray:
    """任一轮压路肩（rumble = 1）前后 window_s 秒内的采样 = True。没有 rumble 通道时全是 False。"""
    on = np.zeros(ctx.n, bool)
    for w in WHEELS:
        a = ctx.ch(f"rumble_{w}")
        if a is not None:
            on |= a > 0.5
    if not on.any():
        return on
    k = max(1, round(window_s / ctx.dt))
    return np.convolve(on.astype(float), np.ones(2 * k + 1), mode="same") > 0.5


def _abs_caveat(abs_on: bool | None, what: str) -> str:
    if abs_on is True:
        return f"你勾选了“我开了 ABS”：ABS 会把前后轴的滑移都压在阈值附近，{what}的区分度会下降，置信度已降一级。"
    if abs_on is None:
        return f"开着 ABS 时{what}的区分度会下降；Data Out 看不到 ABS 状态，开了的话请在上面勾选“我开了 ABS”。"
    return ""


def _check_brake_balance(ctx: _Ctx) -> None:
    cid, name = "brake_balance", CHECK_NAME["brake_balance"]
    need = [f"slip_ratio_{w}" for w in WHEELS] + ["brake", "steer", "speed"]
    if not ctx.dash or ctx.missing(need):
        ctx.check(cid, name, "skipped", "数据不足：需要滑移率、刹车、方向盘、车速。")
        return
    r = ctx.r["brake_balance"]
    g = ctx.r["general"]
    brk, steer, spd = ctx.ch("brake"), np.abs(ctx.ch("steer")), np.abs(ctx.ch("speed")) * 3.6
    fsr = (np.abs(ctx.ch("slip_ratio_fl")) + np.abs(ctx.ch("slip_ratio_fr"))) / 2
    rsr = (np.abs(ctx.ch("slip_ratio_rl")) + np.abs(ctx.ch("slip_ratio_rr"))) / 2
    kerb = _near_kerb(ctx, r["kerb_window_s"])
    m = (brk >= r["min_brake"]) & (steer < r["max_steer"]) & (spd > r["min_speed_kmh"]) & ~kerb

    def first_mark(a: np.ndarray) -> int | None:
        hit = np.flatnonzero((a[:-1] >= r["slip_mark"]) & (a[1:] >= r["slip_mark"])) if a.size > 1 else np.array([])
        return int(hit[0]) if hit.size else None

    zones = []
    for i0, i1 in _runs(m, ctx.t, g["merge_gap_s"], ctx.dt):
        dur = ctx.dur(i0, i1)
        if dur < r["min_zone_s"]:
            continue
        sl = slice(i0, i1 + 1)
        pf, pr = float(np.percentile(fsr[sl], r["peak_pct"])), float(np.percentile(rsr[sl], r["peak_pct"]))
        jf, jr = first_mark(fsr[sl]), first_mark(rsr[sl])
        tf = None if jf is None else float(ctx.t[i0 + jf] - ctx.t[i0])
        tr = None if jr is None else float(ctx.t[i0 + jr] - ctx.t[i0])
        first = lead = None
        if tf is not None and (tr is None or tr - tf >= r["mark_lead_s"]):
            first, lead = "front", (None if tr is None else tr - tf)
        elif tr is not None and (tf is None or tf - tr >= r["mark_lead_s"]):
            first, lead = "rear", (None if tf is None else tf - tr)
        if max(pf, pr) < r["min_eval_peak"]:
            bias = None                                  # 刹得不够重，看不出谁先饱和
        elif first is not None:
            bias = first
        elif pf - pr >= r["bias_diff"]:
            bias = "front"
        elif pr - pf >= r["bias_diff"]:
            bias = "rear"
        else:
            bias = "balanced"
        loc = ctx.loc(i0, i1, wheel=None if bias in (None, "balanced") else bias)
        zones.append({"i0": i0, "i1": i1, "dur": dur, "front_peak": pf, "rear_peak": pr, "front_mark_s": tf,
                      "rear_mark_s": tr, "first": first, "lead_s": lead, "bias": bias, "loc": loc,
                      "speed_kmh": float(spd[i0]), "brake": float(brk[sl].mean())})
    ctx.samples["brake_zones"] = [{**{k: z[k] for k in ("front_peak", "rear_peak", "front_mark_s", "rear_mark_s", "first",
                                                       "lead_s", "bias", "speed_kmh", "brake")},
                                   "duration_s": z["dur"], "location": z["loc"], "lap_label": _lap_label(ctx.meta),
                                   "lap_id": ctx.meta.get("id")} for z in zones]
    ev = [z for z in zones if z["bias"] is not None]
    rule_txt = (f"直线重刹段 = 刹车 ≥ {_pct(r['min_brake'])}、|方向盘| < {_pct(r['max_steer'])}、车速 > {r['min_speed_kmh']:.0f} km/h、"
                f"前后 {r['kerb_window_s']:.2f} s 内没压路肩，连续 ≥ {r['min_zone_s']:.2f} s")
    if len(ev) < r["min_zones"]:
        ctx.check(cid, name, "skipped", f"数据不足：只有 {len(ev)} 段够重的直线重刹（{rule_txt}，"
                                        f"且某一轴滑移率峰值 ≥ {r['min_eval_peak']:.2f}；至少要 {r['min_zones']} 段）。")
        return
    n_f = sum(1 for z in ev if z["bias"] == "front")
    n_r = sum(1 for z in ev if z["bias"] == "rear")
    n_b = len(ev) - n_f - n_r
    ctx.extra["brake_balance_counts"] = {"zones": len(ev), "front": n_f, "rear": n_r, "balanced": n_b,
                                         "front_where": [ctx.where(None, "straight", ctx.dist[z["i0"]]) for z in ev if z["bias"] == "front"]}
    count_txt = f"{len(ev)} 段直线重刹：前轴先饱和 {n_f} 段、后轴先饱和 {n_r} 段、前后差不多 {n_b} 段"
    for axle, k in (("front", n_f), ("rear", n_r)):
        other = n_r if axle == "front" else n_f
        if k >= r["min_zones"] and k / len(ev) >= r["dominant_share"] and k > other:
            ctx.findings.append(_brake_bias_finding(ctx, axle, ev, k, count_txt, rule_txt))
            ctx.check(cid, name, "found", count_txt + "。")
            return
    ctx.check(cid, name, "ok", count_txt + f"，没有一根轴在大多数（≥ {_pct(r['dominant_share'])}）重刹段里先饱和。")


def _brake_bias_finding(ctx: _Ctx, axle: str, ev: list[dict], k: int, count_txt: str, rule_txt: str) -> dict:
    r = ctx.r["brake_balance"]
    axle_cn, other_cn = AXLE_CN[axle], AXLE_CN["rear" if axle == "front" else "front"]
    hit = [z for z in ev if z["bias"] == axle]
    evidence = []
    for z in hit:
        p_hi, p_lo = (z["front_peak"], z["rear_peak"]) if axle == "front" else (z["rear_peak"], z["front_peak"])
        mark = ""
        if z["first"] == axle:
            mark = (f"；{axle_cn}先到 {r['slip_mark']:.2f}" +
                    (f"（比{other_cn}早 {z['lead_s']:.2f} s）" if z["lead_s"] is not None else f"，{other_cn}一直没到"))
        evidence.append(f"{z['loc']['label']}（{z['dur']:.2f} s，车速 {z['speed_kmh']:.0f} km/h 起，刹车平均 {_pct(z['brake'])}）："
                        f"{axle_cn} |滑移率| 峰值 {p_hi:.2f}、{other_cn} {p_lo:.2f}{mark}")
    evidence.append(count_txt)
    evidence.append(f"判定阈值：{rule_txt}；某一轴先到 {r['slip_mark']:.2f}（领先 ≥ {r['mark_lead_s']:.2f} s），"
                    f"或两轴峰值（p{r['peak_pct']:.0f}）差 ≥ {r['bias_diff']:.2f} 算这一段偏向它；"
                    f"≥ {r['min_zones']} 段且占 ≥ {_pct(r['dominant_share'])} 才报")
    if axle == "front":
        tips = ["刹车平衡往后挪 1~2%（后轮多分担一点刹车力）", "改完看这里前后轴的峰值是否接近，以及“刹车抱死”有没有变少"]
        summary = (f"{count_txt}：多数重刹段里前轴的滑移率先到极限、后轴还有余量——"
                   "刹车力偏向前轴，还没抱死也已经先饱和，后轴的抓地没用上。")
    else:
        tips = ["刹车平衡往前挪 1~2%（后轮少分担一点刹车力）", "后轴先饱和时刹车容易甩尾，入弯前先把直线刹车做稳"]
        summary = (f"{count_txt}：多数重刹段里后轴的滑移率先到极限、前轴还有余量——"
                   "刹车力偏向后轴，直线刹车时车尾容易不稳。")
    conf = "high" if k >= 2 * r["min_zones"] and k / len(ev) >= 0.75 else "medium"
    if ctx.abs_on is True:
        conf = _lower(conf)
    caveat = " ".join(x for x in (_abs_caveat(ctx.abs_on, "前后轴对比"), ONE_AT_A_TIME) if x)
    basis = {"channels": ["slip_ratio_fl/fr/rl/rr（归一化滑移率，前后轴各取左右两轮 |x| 的平均）", "brake", "steer", "speed",
                          "rumble_*（排除压路肩）"],
             "rule": (f"{rule_txt}。每段比较前后轴 |滑移率| 的 p{r['peak_pct']:.0f} 峰值，和谁先连续两个采样到 {r['slip_mark']:.2f}。"
                      "直线上不转向，前后轴的滑移差别主要来自刹车力分配。"),
             "principle": ("刹车时滑移率越接近 1，轮胎越接近纵向抓地极限。直线重刹时前后轴受的刹车力按“刹车平衡”分配："
                           "总是同一根轴先接近极限，说明分给它的刹车力相对它的载荷偏多。理想状态是两根轴差不多同时到极限，"
                           "这样总的减速度最大。比抱死更早就能看出来。")}
    return _finding(f"brake_bias_{axle}", "brakes", "warn", f"刹车平衡偏{'前' if axle == 'front' else '后'}（直线重刹时{axle_cn}先饱和）",
                    summary, evidence, [z["loc"] for z in hit], basis, tips, conf, caveat,
                    data={"bias": axle, "zones": len(ev), "biased_zones": k, "abs_on": ctx.abs_on})


# ================================================================ ⑤c 刹车压力（踏板 - 减速度饱和）

def _decel_sign(glong: np.ndarray, brk: np.ndarray) -> float:
    """g_long 的正负号：刹车时应该是减速。重刹采样的中位数为正就翻过来（不依赖游戏的正负号约定）。"""
    m = brk >= 0.3
    if m.sum() < 5:
        return -1.0
    return -1.0 if float(np.median(glong[m])) <= 0 else 1.0


def _coast_model(spd_ms: np.ndarray, decel: np.ndarray, mask: np.ndarray, min_n: int) -> tuple[float, float] | None:
    """滑行（不踩油门不踩刹车）时的减速度 ≈ a + b·v²（风阻 + 发动机制动 + 滚阻）。样本不够返回 None。"""
    if mask.sum() < max(10, min_n):
        return None
    v2 = spd_ms[mask] ** 2
    A = np.stack([np.ones_like(v2), v2], axis=1)
    try:
        (a, b), *_ = np.linalg.lstsq(A, decel[mask], rcond=None)
    except np.linalg.LinAlgError:
        return None
    return float(a), float(b)


def _speed_edges(speed: np.ndarray, quantiles, fixed) -> list[float]:
    """车速分档边界（km/h）：配置里写了固定边界就用固定的，否则按这些采样车速的分位数分，去掉重复。"""
    if fixed:
        return sorted(float(x) for x in fixed)
    if speed.size == 0:
        return []
    edges = [round(float(np.quantile(speed, q))) for q in quantiles]
    out: list[float] = []
    for e in edges:
        if not out or e - out[-1] >= 5:
            out.append(float(e))
    return out


def _band_defs(edges: list[float]) -> list[dict]:
    names = {1: ["全部"], 2: ["低速", "高速"], 3: ["低速", "中速", "高速"]}.get(len(edges) + 1)
    out = []
    for i in range(len(edges) + 1):
        lo = edges[i - 1] if i > 0 else None
        hi = edges[i] if i < len(edges) else None
        rng = (f"< {hi:.0f} km/h" if lo is None else f"> {lo:.0f} km/h" if hi is None else f"{lo:.0f}–{hi:.0f} km/h")
        out.append({"name": names[i] if names else f"第 {i + 1} 档", "lo_kmh": lo, "hi_kmh": hi, "range": rng})
    return out


def _band_mask(speed: np.ndarray, band: dict) -> np.ndarray:
    m = np.ones(speed.size, bool)
    if band["lo_kmh"] is not None:
        m &= speed >= band["lo_kmh"]
    if band["hi_kmh"] is not None:
        m &= speed < band["hi_kmh"]
    return m


def _pedal_samples(ctx: _Ctx) -> dict | None:
    """刹车压力分析用的采样：直线（不转向、不踩油门、没压路肩）踩刹车时的 (踏板, 扣掉滑行减速后的减速度 G, 车速)。"""
    need = ("brake", "g_long", "speed", "steer", "throttle")
    if not ctx.dash or ctx.missing(need):
        return None
    r = ctx.r["brake_pressure"]
    brk, thr = ctx.ch("brake"), ctx.ch("throttle")
    steer, spd = np.abs(ctx.ch("steer")), np.abs(ctx.ch("speed"))
    decel = ctx.ch("g_long") * _decel_sign(ctx.ch("g_long"), brk)
    base = (steer < r["max_steer"]) & (thr <= r["max_throttle"]) & (spd * 3.6 > r["min_speed_kmh"]) \
        & ~_near_kerb(ctx, r["kerb_window_s"])
    coast = base & (brk <= r["coast_max_pedal"])
    model = _coast_model(spd, decel, coast, round(r["min_coast_s"] / ctx.dt))
    m = base & (brk >= r["min_brake"])
    net = decel - (model[0] + model[1] * spd ** 2 if model else 0.0)
    return {"dt": ctx.dt, "pedal": brk[m], "decel_g": net[m], "speed_kmh": spd[m] * 3.6, "coast": model,
            "coast_s": float(coast.sum() * ctx.dt)}


def _pedal_curves(sets: list[dict], r: dict) -> dict:
    """按车速分档算“踏板 → 减速度”曲线和饱和点（单圈 / 多圈合并共用）。"""
    sets = [s for s in sets if s and s["pedal"].size]
    if not sets:
        return {"bands": [], "edges_kmh": []}
    ped = np.concatenate([s["pedal"] for s in sets])
    dec = np.concatenate([s["decel_g"] for s in sets])
    spd = np.concatenate([s["speed_kmh"] for s in sets])
    w = np.concatenate([np.full(s["pedal"].size, s["dt"]) for s in sets])
    edges = _speed_edges(spd, r["speed_band_quantiles"], r["speed_band_edges_kmh"])
    bands = []
    for band in _band_defs(edges):
        bm = _band_mask(spd, band)
        k = np.floor(ped[bm] / r["pedal_bin"] + 1e-9).astype(int)
        bins = []
        for b in sorted(set(k.tolist())):
            q = k == b
            t_s = float(w[bm][q].sum())
            if t_s < r["min_bin_s"]:
                continue
            bins.append({"pedal": round(min(1.0, (b + 0.5) * r["pedal_bin"]), 3), "decel_g": float(np.median(dec[bm][q])),
                         "time_s": round(t_s, 2)})
        info = {**band, "bins": bins, "time_s": round(float(w[bm].sum()), 2), "sat_pedal": None, "sat_decel_g": None,
                "status": "insufficient", "max_pedal": max((x["pedal"] for x in bins), default=None)}
        span = (bins[-1]["pedal"] - bins[0]["pedal"]) if bins else 0.0
        if len(bins) >= r["min_bins"] and span >= r["min_span"] - 1e-9:
            top = max(x["decel_g"] for x in bins)
            s_idx = None
            for i, x in enumerate(bins):
                if top > 0 and all(y["decel_g"] >= r["sat_frac"] * top for y in bins[i:]):
                    s_idx = i
                    break
            above = len(bins) - 1 - s_idx if s_idx is not None else 0
            if s_idx is not None and above >= r["min_bins_above"] and bins[-1]["pedal"] >= r["full_pedal"]:
                info.update(sat_pedal=bins[s_idx]["pedal"], sat_decel_g=round(bins[s_idx]["decel_g"], 3),
                            status="saturated" if bins[s_idx]["pedal"] <= r["sat_warn_pedal"] else "late")
            else:
                info["status"] = "linear"
        bands.append(info)
    return {"bands": bands, "edges_kmh": edges}


def _check_brake_pressure(ctx: _Ctx) -> None:
    cid, name = "brake_pressure", CHECK_NAME["brake_pressure"]
    r = ctx.r["brake_pressure"]
    ps = _pedal_samples(ctx)
    if ps is None:
        ctx.check(cid, name, "skipped", "数据不足：需要刹车、纵向 G、车速、方向盘、油门。")
        return
    ctx.samples["brake_pedal"] = ps
    curves = _pedal_curves([ps], r)
    coast_txt = ("已扣掉风阻和发动机制动（按滑行段拟合 减速度 = a + b·车速²）" if ps["coast"] else
                 f"滑行数据不足 {r['min_coast_s']:.0f} s，没扣风阻和发动机制动（高速段的减速度会偏大）")
    evaluated = [b for b in curves["bands"] if b["status"] != "insufficient"]
    if not evaluated:
        ctx.check(cid, name, "skipped",
                  f"数据不足：直线刹车时踏板位置太集中（每个车速档至少要 {r['min_bins']} 个 {_pct(r['pedal_bin'])} 宽的踏板区间、"
                  f"每个区间 ≥ {r['min_bin_s']:.2f} s、跨度 ≥ {_pct(r['min_span'])}）——比如总是一脚踩到底，就看不出饱和点。")
        return
    sat = [b for b in evaluated if b["status"] == "saturated"]
    parts = []
    for b in curves["bands"]:
        if b["status"] == "insufficient":
            parts.append(f"{b['name']}（{b['range']}）数据不足")
        elif b["sat_pedal"] is not None:
            parts.append(f"{b['name']}（{b['range']}）踩到 {_pct(b['sat_pedal'])} 后减速度不再增加（约 {b['sat_decel_g']:.2f} G）")
        else:
            parts.append(f"{b['name']}（{b['range']}）踩到 {_pct(b['max_pedal'])} 减速度还在增加")
    note = "；".join(parts) + f"（{coast_txt}）"
    if not sat:
        ctx.check(cid, name, "ok", note + f"。没有在 {_pct(r['sat_warn_pedal'])} 踏板以内就饱和的。")
        return
    ctx.findings.append(_brake_pressure_finding(ctx, sat, curves, coast_txt))
    ctx.check(cid, name, "found", note + "。")


def _brake_pressure_finding(ctx: _Ctx, sat: list[dict], curves: dict, coast_txt: str) -> dict:
    r = ctx.r["brake_pressure"]
    worst = min(sat, key=lambda b: b["sat_pedal"])
    evidence = []
    for b in curves["bands"]:
        if not b["bins"]:
            continue
        pts = "、".join(f"{_pct(x['pedal'])}→{x['decel_g']:.2f} G" for x in b["bins"])
        evidence.append(f"{b['name']}（{b['range']}，直线刹车 {b['time_s']:.1f} s）：踏板→减速度 {pts}")
        if b["sat_pedal"] is not None:
            evidence.append(f"　{b['name']}：踩到约 {_pct(b['sat_pedal'])} 时减速度已达到这档最大值的 ≥ {_pct(r['sat_frac'])}，"
                            "再往下踩减速度基本不变")
    evidence.append(coast_txt)
    evidence.append(f"判定阈值：直线（|方向盘| < {_pct(r['max_steer'])}、油门 ≤ {_pct(r['max_throttle'])}、车速 > {r['min_speed_kmh']:.0f} km/h、"
                    f"没压路肩）刹车时，按 {_pct(r['pedal_bin'])} 踏板分区间取减速度中位数（每区间 ≥ {r['min_bin_s']:.2f} s）；"
                    f"从某个踏板位置起后面所有区间都 ≥ 最大值的 {_pct(r['sat_frac'])}、后面还有 ≥ {r['min_bins_above']} 个区间、"
                    f"最深踩到 ≥ {_pct(r['full_pedal'])}，这个位置 ≤ {_pct(r['sat_warn_pedal'])} 才报")
    if ctx.abs_on is True:
        sev = "info"
        title = f"刹车踩到约 {_pct(worst['sat_pedal'])} ABS 就开始限制减速度"
        summary = (f"{worst['name']}段踩到约 {_pct(worst['sat_pedal'])} 后减速度就不再增加（约 {worst['sat_decel_g']:.2f} G）。"
                   "你开着 ABS，多半是 ABS 在这里开始介入：后面那截踏板只是让 ABS 更忙，不会刹得更快。")
        tips = ["想要更细的刹车控制：刹车压力调小一点，让踩到 90~100% 时才接近极限",
                "不想动调校的话：重刹时踩到这个位置附近就够了，不必一脚踩死"]
    else:
        sev = "warn"
        title = f"刹车压力偏大（踩到约 {_pct(worst['sat_pedal'])} 减速度就到头）"
        summary = (f"{worst['name']}段踩到约 {_pct(worst['sat_pedal'])} 后减速度就不再增加（约 {worst['sat_decel_g']:.2f} G）："
                   "后面那截踏板行程是浪费的，而且再往下踩只会更容易抱死。")
        tips = ["刹车压力调小一点（让踩到 90~100% 时才到极限，刹车控制更细）",
                "如果同时有“刹车抱死”，这条更值得先改"]
    bands = [b["name"] for b in sat]
    caveat = ("减速度受车速和下压力影响很大（高速时下压力大、减速度也大），所以按车速分档分别看；"
              "手柄扳机和方向盘踏板的行程感觉不一样，以实际踩到的百分比为准。" +
              (" " + _abs_caveat(ctx.abs_on, "饱和点") if ctx.abs_on is None else "") + " " + ONE_AT_A_TIME)
    basis = {"channels": ["brake（踏板 0~1）", "g_long（纵向 G，自动判断正负号）", "speed", "steer", "throttle", "rumble_*"],
             "rule": (f"只用直线刹车的采样，按车速分档（默认按本圈刹车时车速的中位数分成低速 / 高速，可在 tuning_rules.toml 改成固定边界），"
                      f"每档把踏板按 {_pct(r['pedal_bin'])} 分区间，取扣掉滑行减速后的减速度中位数，找减速度不再增加的踏板位置。"),
             "principle": ("轮胎能提供的最大减速度是有限的：踏板踩到某个位置时轮胎已经到了纵向极限，再踩只会让轮子更接近抱死，"
                           "减速度不会再增加。这个位置太浅说明刹车压力设得太大，可用的踏板行程很短、很难精细控制。")}
    first_zone = next((z["location"] for z in ctx.samples.get("brake_zones", []) if z["location"]), None)
    locs = [first_zone | {"phase": None, "phase_label": "全圈", "corner": None,
                          "label": "全圈直线刹车段（统计）"}] if first_zone else []
    return _finding("brake_pressure_high", "brakes", sev, title, summary, evidence, locs, basis, tips,
                    "medium" if len(bands) >= 2 or worst["sat_pedal"] <= r["sat_warn_pedal"] - 0.2 else "low",
                    caveat, data={"sat_pedal": worst["sat_pedal"], "bands": bands, "abs_on": ctx.abs_on})


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
                         f"只有 {lift_s:.1f} s，不判断减速锁止")
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
                ctx.findings.append(_diff_decel_finding(ctx, sorted(events, key=lambda e: e["i0"]), lift_s))
                notes.append(f"入弯松刹 {lift_s:.1f} s 里驱动轮滑移超限 {len(events)} 次")
            else:
                notes.append(f"入弯松刹 {lift_s:.1f} s，驱动轮 |滑移率| 最大 {mx:.2f}，没超过 {r['slip_limit']:.2f}")
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


def _diff_decel_finding(ctx: _Ctx, events: list[dict], lift_s: float) -> dict:
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
                    f"（本圈共 {lift_s:.1f} s）时，驱动轮 |滑移率| > {r['slip_limit']:.2f} 连续 ≥ {r['min_duration_s']:.2f} s；"
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
                          "meta.drivetrain（驱动轮）"],
             "rule": "按弯道方向分出内侧 / 外侧驱动轮（右弯内侧 = 右侧轮），只看弯心前的松刹车滑行段。",
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
    tips = ([f"中央差速器扭矩分配往前挪一点（前轴多分一点，后轴少滑）"] if rear_heavy else
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


# ================================================================ 交叉印证

def _cross_reference(ctx: _Ctx) -> None:
    """同一位置同时出现的相关问题，互相补一句说明（不改变判定）。"""
    by_id = {f["id"]: f for f in ctx.findings}
    spin, os_exit = by_id.get("wheelspin"), by_id.get("oversteer_exit")
    if spin and os_exit:
        common = {l["corner"] for l in spin["locations"]} & {l["corner"] for l in os_exit["locations"]} - {None}
        if common:
            txt = (f"{'、'.join(ctx.cn(c) for c in sorted(common, key=_corner_key))} 出弯同时检测到转向过度和驱动轮空转："
                   "多半是同一件事——驱动轮空转让后轴丢了侧向抓地。")
            os_exit["evidence"].insert(len(os_exit["evidence"]) - 1, txt)
            spin["evidence"].insert(len(spin["evidence"]) - 1, txt)
    us_mid = by_id.get("understeer_mid")
    tire = by_id.get("tire_lr_front")
    if us_mid and tire and tire["severity"] != "info":
        txt = f"同一圈还有弯中转向不足（{'、'.join(ctx.cn(c) for c in us_mid['data']['corners'])}）：前轮长时间过极限滑动，也会让外侧前胎更热。"
        tire["evidence"].insert(len(tire["evidence"]) - 1, txt)
    # 抱死 × 直线重刹的前后对比：只有个别段偏向抱死的那根轴 → 更像是那一脚的问题，不是刹车平衡整体偏
    bb = ctx.extra.get("brake_balance_counts")
    for axle in ("front", "rear"):
        lock = by_id.get(f"brake_lock_{axle}")
        if not lock or not bb or f"brake_bias_{axle}" in by_id:
            continue
        k = bb[axle]
        if bb["balanced"] >= max(1, bb["zones"] - k) and bb["balanced"] > k:
            txt = (f"直线重刹段里 {bb['balanced']}/{bb['zones']} 段前后轴滑移差不多，只有 {k} 段{AXLE_CN[axle]}先饱和："
                   f"刹车平衡整体并不偏{'前' if axle == 'front' else '后'}，这次抱死更像是那一脚踩得太猛 / 刹车压力偏大，"
                   "先试驾驶和刹车压力，再考虑挪刹车平衡。")
            lock["evidence"].insert(len(lock["evidence"]) - 1, txt)
            lock["suggestions"] = [x for x in lock["suggestions"] if not x.startswith("先排除驾驶")]
            lock["suggestions"].insert(0, "先排除驾驶：别一脚踩死，踩到快抱死的位置就停（其他重刹段前后平衡是正常的）")


# ================================================================ 对外接口

def _lap_info(meta: dict) -> dict:
    keys = ("id", "lap_number", "lap_time", "track_id", "track_name", "car_ordinal", "car_name", "car_class",
            "car_pi", "game", "family", "kind", "is_complete", "flags", "distance_m", "session_id", "created_at",
            "setup_id", "setup_name")
    return {k: meta.get(k) for k in keys if k in meta}


def _lap_label(meta: dict) -> str:
    parts = []
    if meta.get("id") is not None:
        parts.append(f"#{meta['id']}")
    if meta.get("lap_number") is not None:
        parts.append(f"第 {meta['lap_number']} 圈")
    return " ".join(parts) or "这圈"


def _sort_findings(findings: list[dict]) -> list[dict]:
    return sorted(findings, key=lambda f: (-SEVERITY_RANK[f["severity"]], -CONF_LEVELS.index(f["confidence"]),
                                           min((l["dist_m"] for l in f["locations"]), default=0.0)))


def _rules_info(rules: dict | None) -> dict:
    return rules_info() if rules is None else {"rules": _merge(DEFAULT_RULES, rules), "source": "调用方传入", "error": None}


CHECKS = (_check_balance, _check_suspension, _check_tires, _check_traction, _check_brakes, _check_brake_balance,
          _check_brake_pressure, _check_differential, _check_gearing)


def _run(lap: LapData, info: dict, corners: list[dict] | None, abs_on: bool | None, aero: bool | None) -> tuple[dict, _Ctx]:
    """单圈诊断，返回 (可 JSON 化的结果, 上下文)。上下文里的 samples 给多圈合并拼分析块用。"""
    ctx = _Ctx(lap, info["rules"], abs_on=abs_on, aero=aero)
    if info["error"]:
        ctx.note("warn", info["error"])
    usable = _check_quality(ctx)
    if usable:
        _find_corners(ctx, corners)
        for fn in CHECKS:
            fn(ctx)
        _cross_reference(ctx)
        ctx.samples["body"] = _body_samples(ctx)
    else:
        for cid, nm in CHECK_LIST:
            ctx.check(cid, nm, "skipped", "数据不足，未检查。")
    findings = _sort_findings(ctx.findings)
    m = ctx.meta
    spd = ctx.ch("speed") if ctx.dash else None
    share = _turn_share(ctx.corners)
    summary = {
        "lap_time_s": m.get("lap_time"),
        "distance_m": round(float(ctx.dist[-1] - ctx.dist[0]), 1) if ctx.n else 0.0,
        "duration_s": round(float(ctx.t[-1] - ctx.t[0]), 2) if ctx.n else 0.0,
        "samples": ctx.n,
        "sample_hz": round(1.0 / ctx.dt, 1) if ctx.dt > 0 else None,
        "drivetrain": m.get("drivetrain"),
        "drivetrain_label": DRIVETRAIN_CN.get(m.get("drivetrain"), "未知"),
        "engine_max_rpm": m.get("engine_max_rpm"),
        "max_speed_kmh": round(float(spd.max()) * 3.6, 1) if spd is not None and spd.size else None,
        "top_gear_used": ctx.extra.get("top_gear_used"),
        "corner_count": len(ctx.corners),
        "corner_source": ctx.corner_source,
        "turn_share": {"right": round(share["right"], 3), "left": round(share["left"], 3)} if share else None,
        "tire_temps_c": ctx.extra.get("tire_temps_c"),
        "has_dash": ctx.dash,
        "assists": {"abs_on": abs_on, "aero": aero},
        "counts": {s: sum(1 for f in findings if f["severity"] == s) for s in ("alert", "warn", "info")},
    }
    rr = info["rules"]
    blocks = _blocks([ctx], rr, abs_on, aero) if usable else _blocks([], rr, abs_on, aero)
    res = {
        "mode": "single", "usable": usable, "lap": _lap_info(m), "lap_label": _lap_label(m), "summary": summary,
        "corners": _corners_out(ctx.corners), "findings": findings, "checks": ctx.checks,
        "not_available": copy.deepcopy(NOT_AVAILABLE), "data_quality": ctx.quality,
        "rules": {"source": info["source"], "error": info["error"]}, **blocks,
    }
    return _json_safe(res), ctx


def _corners_out(corners: list[dict]) -> list[dict]:
    out = []
    for c in corners:
        ms = c.get("min_speed")
        out.append({"name": c["name"], "label": _corner_label(c), "direction": c["direction"],
                    "start_m": round(c["start_m"], 1), "apex_m": round(c["apex_m"], 1), "end_m": round(c["end_m"], 1),
                    "heading_change_deg": c.get("heading_change_deg"),
                    "min_speed_kmh": round(ms * 3.6, 1) if ms is not None and math.isfinite(ms) else None})
    return out


def _blocks(ctxs: list[_Ctx], rules: dict, abs_on: bool | None, aero: bool | None) -> dict:
    """三个分析块（单圈：一个 ctx；多圈：所有可用圈的 ctx 拼起来）。"""
    return {
        "balance_map": _balance_block([c.samples.get("balance") for c in ctxs], rules, aero),
        "brake_analysis": _brake_block([c.samples.get("brake_zones") for c in ctxs],
                                       [c.samples.get("brake_pedal") for c in ctxs], rules, abs_on),
        "body": _body_block([c.samples.get("body") for c in ctxs], rules),
    }


def diagnose(lap: LapData, rules: dict | None = None, corners: list[dict] | None = None, *,
             abs_on: bool | None = None, aero: bool | None = None) -> dict:
    """单圈诊断。返回（可直接 JSON 化，没有 NaN）：

        mode: "single"
        usable: 数据够不够做诊断
        lap: 这圈的元数据（id、圈号、圈时、赛道、车、调校版本……）
        summary: 圈时、距离、采样率、驱动方式、最高转速、最高车速、用到的最高挡、弯道数、弯道来源（layout / detected）、
                 左右弯占比、热胎平均胎温、用户勾选的辅助 assists{abs_on, aero}、各严重程度的问题数 counts
        corners: [{name（编号 T1…）, label（显示名）, direction, start_m, apex_m, end_m, heading_change_deg, min_speed_kmh}]
        findings: [{id, category, category_label, severity(info|warn|alert), title, summary, evidence[],
                    locations[{dist_m, end_m, corner, phase, phase_label, wheel, wheel_label, t_s, duration_s, label}],
                    basis{channels[], rule, principle}, suggestions[], caveat, confidence(low|medium|high), data{}}]
                  按严重程度、置信度排序
        checks: [{id, name, status(found|ok|skipped), note}]  每项检查做了什么、结论如何（“没发现”和“没法判断”分开）
        balance_map / brake_analysis / body: 分析块（画图用的量化数据 + 解读句，结构见 _balance_block 等）
        not_available: [{item, why, instead}]  Data Out 给不了、所以判断不了的东西
        data_quality: [{level(info|warn|error), text}]
        rules: {source, error}  阈值来自哪个文件

    corners：外部给的弯道表（如 sectors.layout_corners 的结果，name 必须是 T1… 编号，可带 label），不给就本圈自己识别。
    abs_on / aero：用户勾选的“我开了 ABS”/“有可调空力”（None = 没说），只影响刹车 / 平衡结论的措辞和置信度。
    """
    return _run(lap, _rules_info(rules), corners, abs_on, aero)[0]


def diagnose_many(laps: list[LapData], rules: dict | None = None, corners: list | None = None, *,
                  abs_on: bool | None = None, aero: bool | None = None) -> dict:
    """多圈合并（同一辆车、同一条赛道的几圈）。corners = 和 laps 一一对应的弯道表列表（元素可为 None）。

    每圈先单独诊断，再按 finding.id 合并：
    - 每圈都出现（≥ 2 圈）→ 置信度升一级；
    - 只在少于 sporadic_frac 比例的圈里出现 → 视为偶发，严重程度、置信度各降一级；
    - evidence 开头写“在几圈里出现”，接着是代表圈（最严重那圈）的完整证据，再每圈一行摘要；
    - locations 合并所有圈，每个位置带 lap_id / lap_label。
    分析块（平衡分布 / 刹车 / 车身）把所有可用圈的采样拼起来算，热力表按弯道编号对齐。
    返回结构和 diagnose() 一样（mode = "multi"），另有 laps（每圈摘要）和每条 finding 的 repeat 字段。
    """
    info = _rules_info(rules)
    rr = info["rules"]
    corners = list(corners) if corners is not None else []
    corners += [None] * (len(laps) - len(corners))
    runs = [_run(l, info, c, abs_on, aero) for l, c in zip(laps, corners)]
    results = [x[0] for x in runs]
    usable_ctx = [ctx for res, ctx in runs if res["usable"]]
    usable = [x for x in results if x["usable"]]
    quality: list[dict] = []
    if info["error"]:
        quality.append({"level": "warn", "text": info["error"]})
    if not laps:
        quality.append({"level": "error", "text": "数据不足：没有给任何一圈。"})
    cars = {(l.meta.get("game"), l.meta.get("car_ordinal")) for l in laps}
    tracks = {l.meta.get("track_id") for l in laps}
    setups = {l.meta.get("setup_id") for l in laps}
    if len(cars) > 1:
        quality.append({"level": "warn", "text": "这几圈不是同一辆车，合并结果仅供参考（不同车的问题不能互相印证）。"})
    if len(tracks) > 1:
        quality.append({"level": "warn", "text": "这几圈不是同一条赛道，合并结果仅供参考（弯道编号对不上）。"})
    if len(setups) > 1 and setups != {None}:
        quality.append({"level": "info", "text": "这几圈不是同一个调校版本：合并后看到的是几套调校的混合结果。"})
    # 每圈的数据质量提示：相同文字合并，注明是哪几圈
    seen: dict[str, dict] = {}
    for res in results:
        for q in res["data_quality"]:
            if q["text"] == info["error"]:
                continue
            item = seen.setdefault(q["text"], {"level": q["level"], "text": q["text"], "laps": []})
            item["laps"].append(res["lap_label"])
    n_all = len(results)
    for item in seen.values():
        who = "所有圈" if len(item["laps"]) == n_all and n_all > 1 else "、".join(item["laps"])
        quality.append({"level": item["level"], "text": f"{who}：{item['text']}"})
    if laps and not usable:
        quality.append({"level": "error", "text": "数据不足：没有一圈的数据够做诊断。"})

    n = len(usable)
    sporadic = rr["multi"]["sporadic_frac"]
    groups: dict[str, list[tuple[dict, dict]]] = {}
    for res in usable:
        for f in res["findings"]:
            groups.setdefault(f["id"], []).append((res, f))
    merged = []

    def _flying(res: dict) -> bool:
        return "first_lap" not in (res["lap"].get("flags") or [])

    for items in groups.values():
        # 代表圈：最严重 → 置信度最高 → 优先非第一圈（冷胎、站立起步）→ 靠后的圈
        rep_res, rep = max(enumerate(items), key=lambda x: (
            SEVERITY_RANK[x[1][1]["severity"]], CONF_LEVELS.index(x[1][1]["confidence"]), _flying(x[1][0]), x[0]))[1]
        f = copy.deepcopy(rep)
        k = len({id(res) for res, _ in items})
        labels = [res["lap_label"] for res, _ in items]
        conf, sev = f["confidence"], f["severity"]
        if n >= 2 and k == n:
            conf = _raise(conf)
            rep_txt = f"在 {n} 圈里每圈都出现（{'、'.join(labels)}）→ 不是偶然，置信度升一级"
        elif n >= 2 and k / n < sporadic:
            conf, sev = _lower(conf), ("warn" if sev == "alert" else "info")
            rep_txt = f"在 {n} 圈里只有 {k} 圈出现（{'、'.join(labels)}）→ 可能是偶发失误，严重程度和置信度各降一级"
        else:
            rep_txt = f"在 {n} 圈里有 {k} 圈出现（{'、'.join(labels)}）"
        f["confidence"], f["severity"] = conf, sev
        f["evidence"] = [rep_txt, f"以下是 {rep_res['lap_label']} 的详细证据："] + rep["evidence"]
        for res, other in items:
            if other is not rep:
                f["evidence"].append(f"{res['lap_label']}：{other['evidence'][0]}")
        f["locations"] = []
        for res, other in items:
            for loc in other["locations"]:
                f["locations"].append({**loc, "lap_id": res["lap"].get("id"), "lap_label": res["lap_label"]})
        f["repeat"] = {"laps": k, "of": n, "lap_labels": labels}
        merged.append(f)
    findings = _sort_findings(merged)
    # 摘要 / 弯道表用的参考圈：优先第一个非“第一圈”的可用圈
    first = next((x for x in usable if _flying(x)), usable[0] if usable else (results[0] if results else None))
    checks = []
    for cid, _ in CHECK_LIST:
        rows = [(res["lap_label"], c) for res in results for c in res["checks"] if c["id"] == cid]
        if not rows:
            continue
        statuses = {c["status"] for _, c in rows}
        status = "found" if "found" in statuses else ("ok" if "ok" in statuses else "skipped")
        by_note: dict[str, list[str]] = {}
        for lbl, c in rows:
            by_note.setdefault(c["note"], []).append(lbl)
        parts = [(("所有圈" if len(lbls) == len(rows) and len(rows) > 1 else "、".join(lbls)) + "：" + txt)
                 for txt, lbls in by_note.items()]
        checks.append({"id": cid, "name": rows[0][1]["name"], "status": status, "note": "\n".join(parts)})
    lap_rows = [{"id": res["lap"].get("id"), "label": res["lap_label"], "lap_number": res["lap"].get("lap_number"),
                 "lap_time_s": res["summary"]["lap_time_s"], "usable": res["usable"], "counts": res["summary"]["counts"],
                 "setup_id": res["lap"].get("setup_id"), "setup_name": res["lap"].get("setup_name"),
                 "corner_source": res["summary"].get("corner_source")}
                for res in results]
    summary = dict(first["summary"]) if first else {}
    summary["counts"] = {s: sum(1 for f in findings if f["severity"] == s) for s in ("alert", "warn", "info")}
    summary["laps_used"] = n
    summary["laps_total"] = n_all
    summary["reference_lap"] = first["lap_label"] if first else None
    summary["assists"] = {"abs_on": abs_on, "aero": aero}
    return _json_safe({
        "mode": "multi", "usable": n > 0, "lap": first["lap"] if first else {}, "lap_label": f"{n_all} 圈合并",
        "laps": lap_rows, "summary": summary, "corners": first["corners"] if first else [],
        "findings": findings, "checks": checks, "not_available": copy.deepcopy(NOT_AVAILABLE),
        "data_quality": quality, "rules": {"source": info["source"], "error": info["error"]},
        **_blocks(usable_ctx, rr, abs_on, aero),
    })


def track_outline(lap: LapData, max_points: int = 600) -> dict:
    """赛道小地图用：降采样后的 {pos_x, pos_z, dist} 列表（和 LapData.to_json 的通道名一致）。"""
    n = len(lap)
    if n == 0 or not (lap.has("pos_x") and lap.has("pos_z") and lap.has("dist")):
        return {"pos_x": [], "pos_z": [], "dist": []}
    step = max(1, math.ceil(n / max_points))
    idx = np.unique(np.concatenate([np.arange(0, n, step), [n - 1]]))
    return _json_safe({k: np.round(np.asarray(lap[k], float)[idx], 2) for k in ("pos_x", "pos_z", "dist")})
