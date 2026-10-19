"""调车诊断：给一圈（或几圈）数据，找出常见的调校问题，每条结论都写清楚证据和判断依据。

原则（用户的要求：别瞎猜）：
- 每条 finding 都带 evidence（实测数字 + 比较的阈值）和 basis（用了哪些通道、哪条规则、为什么能说明问题）。
- 数据不够就说“数据不足”；Data Out 本身给不了的东西列在 not_available 里，不硬下结论。
- 只看数值大小（绝对值），不依赖正负号：真实游戏各通道的正负号约定没有逐个核实。
  （左右弯方向来自方向盘输入 steer，见 corners.py。）
- 阈值全部在 tuning_rules.toml（带中文说明），文件缺失时用这里的 DEFAULT_RULES。

对外接口：
    diagnose(lap, rules=None) -> dict          单圈诊断
    diagnose_many(laps, rules=None) -> dict    多圈合并（同车同赛道），每圈都出现的问题置信度更高
    rules_info() / get_rules()                 当前阈值（tuning_rules.toml 改了会自动重新读取）
    track_outline(lap, max_points)             画赛道小地图用的降采样坐标

检查项（都在下面各自的 _check_xxx 函数里，好找好改）：
    转向平衡（按弯道阶段的转向不足 / 转向过度）、悬挂触底 / 伸到底、胎温不平衡、
    出弯空转、刹车抱死、齿比（最高挡撞断油 / 转速余量）。
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
    "confidence": {"high_factor": 4.0, "medium_factor": 1.5, "margin": 0.10},
    "multi": {"sporadic_frac": 0.5},
}

WHEELS = ("fl", "fr", "rl", "rr")
WHEEL_CN = {"fl": "左前", "fr": "右前", "rl": "左后", "rr": "右后", "front": "前轴", "rear": "后轴", "all": "四轮"}
AXLE_WHEELS = {"front": ("fl", "fr"), "rear": ("rl", "rr")}
PHASE_CN = {"entry": "入弯", "mid": "弯中", "exit": "出弯", "straight": "直道"}
PHASES = ("entry", "mid", "exit")
CATEGORY_CN = {"balance": "转向平衡", "suspension": "悬挂", "tires": "轮胎", "traction": "牵引",
               "brakes": "刹车", "gearing": "齿比"}
SEVERITY_RANK = {"info": 0, "warn": 1, "alert": 2}
CONF_LEVELS = ("low", "medium", "high")
DRIVETRAIN_CN = {0: "前驱 FWD", 1: "后驱 RWD", 2: "四驱 AWD"}
DRIVEN_WHEELS = {0: ("fl", "fr"), 1: ("rl", "rr"), 2: WHEELS}
DASH_CHANNELS = ("speed", "throttle", "brake", "gear", "steer")

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


def _wheel_cn(code: str | None) -> str:
    if not code:
        return ""
    return "、".join(WHEEL_CN.get(c, c) for c in code.split(","))


def _pct(x: float) -> str:
    return f"{x * 100:.0f}%"


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

    def __init__(self, lap: LapData, rules: dict):
        self.lap, self.r, self.meta = lap, rules, lap.meta or {}
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

    def where(self, corner: dict | None, phase: str, d: float) -> str:
        if corner is not None:
            return f"{corner['name']} {PHASE_CN.get(phase, phase)}"
        nxt = next((c for c in self.corners if (c.get("brake_start_m") or c["start_m"]) > d), None)
        return f"{nxt['name']} 前的直道" if nxt else "直道"

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


def _find_corners(ctx: _Ctx) -> None:
    if not ctx.dash:
        return
    if ctx.missing(("pos_x", "pos_z")):
        ctx.note("warn", "缺少坐标通道（pos_x / pos_z），无法识别弯道：按弯道阶段的判断全部跳过。")
        return
    try:
        ctx.corners = detect_corners(ctx.lap)
    except Exception as e:  # noqa: BLE001  弯道识别失败不应该让整个诊断崩掉
        ctx.note("warn", f"弯道识别失败（{e}），按弯道阶段的判断全部跳过。")
        ctx.corners = []
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
    if not ctx.corners:
        ctx.check("balance", name, "skipped", "数据不足：没识别出弯道，无法按入弯 / 弯中 / 出弯分阶段判断。")
        return
    r = ctx.r["balance"]
    g = ctx.r["general"]
    d = ctx.dist
    thr, brk = ctx.ch("throttle"), ctx.ch("brake")
    steer, glat = np.abs(ctx.ch("steer")), np.abs(ctx.ch("g_lat"))
    fa = (np.abs(ctx.ch("slip_angle_fl")) + np.abs(ctx.ch("slip_angle_fr"))) / 2
    ra = (np.abs(ctx.ch("slip_angle_rl")) + np.abs(ctx.ch("slip_angle_rr"))) / 2

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
    corners = sorted({e["corner"]["name"] for e in events}, key=lambda s: int(s[1:]))
    sev = "alert" if total >= r["alert_total_s"] or len(corners) >= r["alert_corners"] else "warn"
    sat_mean = float(np.average([e["sat_mean"] for e in events], weights=[e["dur"] for e in events]))
    oth_mean = float(np.average([e["oth_mean"] for e in events], weights=[e["dur"] for e in events]))
    margin = (sat_mean - r["grip_limit"]) / r["grip_limit"]
    conf = _confidence(ctx.r, total, r["min_duration_s"], margin)
    kind_cn = "转向不足" if kind == "understeer" else "转向过度"
    phase_cn = PHASE_CN[pname]
    where = "、".join(corners)
    if kind == "understeer":
        summary = (f"{where} {phase_cn}时前轴先超过抓地极限（|侧偏角| 平均 {sat_mean:.2f}），后轴只有 {oth_mean:.2f}："
                   "车头往弯外推，多打方向也转不进去。")
    else:
        summary = (f"{where} {phase_cn}时后轴先超过抓地极限（|侧偏角| 平均 {sat_mean:.2f}），前轴只有 {oth_mean:.2f}："
                   "车尾往外滑，需要反打方向救车。")
    evidence = []
    for e in events:
        evidence.append(
            f"{e['corner']['name']} {phase_cn}（{ctx.dist[e['i0']]:.0f}–{ctx.dist[e['i1']]:.0f} m，持续 {e['dur']:.2f} s）："
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
    locs, evidence = [], []
    both_axle_spin = single_spin = False
    for e in events:
        code = _wheel_code(e["wheels"])
        loc = ctx.loc(e["i0"], e["i1"], wheel=code)
        locs.append(loc)
        per = "，".join(f"{WHEEL_CN[w]}平均 {e['mean'][w]:.2f}、最大 {e['peak'][w]:.2f}" for w in e["wheels"])
        evidence.append(f"{loc['label']}（持续 {e['dur']:.2f} s）：油门平均 {_pct(e['thr'])}，空转轮 |滑移率| {per}")
        for ax in ("front", "rear"):
            aw = [w for w in AXLE_WHEELS[ax] if w in driven]
            if len(aw) == 2:
                spun = [w for w in aw if w in e["wheels"]]
                if len(spun) == 2:
                    both_axle_spin = True
                elif len(spun) == 1:
                    single_spin = True
    if both_axle_spin:
        evidence.append("同一根驱动轴的左右两个轮子一起空转 → 不是差速器“漏扭矩”，而是轮上扭矩超过了这根轴的抓地")
    if single_spin:
        evidence.append("有时只有同轴的一个驱动轮在空转 → 扭矩从打滑的那一侧“漏掉”了，差速器偏开放")
    evidence.append(f"判定阈值：{dt_txt} 的驱动轮 |滑移率| > {r['slip_limit']:.2f}，同时油门 ≥ {_pct(r['min_throttle'])}、刹车 < {_pct(r['max_brake'])}，"
                    f"连续 ≥ {r['min_duration_s']:.2f} s。本圈 {len(events)} 次、累计 {total:.2f} s"
                    f"（累计 ≥ {r['alert_total_s']:.1f} s 或 ≥ {r['alert_events']} 次记为严重）")
    phases = {l["phase"] for l in locs}
    corners = sorted({l["corner"] for l in locs if l["corner"]}, key=lambda s: int(s[1:]))
    title = ("出弯空转" if phases == {"exit"} else "驱动轮空转") + (f"（{'、'.join(corners)}）" if corners else "")
    tips = ["先排除驾驶：出弯给油更柔、分段踩，等车身摆正再全油"]
    if both_axle_spin:
        tips += ["驱动轴的弹簧 / 防倾杆调软一点，让驱动轮更贴地",
                 "差速器加速锁止率调低一点（两轮一起空转时可以试，调太低会变成内侧轮单独空转）",
                 "出弯用高一挡，或把低挡齿比调长一点（轮上扭矩小一些）"]
    if single_spin:
        tips += ["差速器加速锁止率调高一点（只有一侧空转说明差速器太开放）"]
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
                                       "both_wheels": both_axle_spin, "single_wheel": single_spin}))
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
    corners = sorted({l["corner"] for l in locs if l["corner"]}, key=lambda s: int(s[1:]))
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
    return _finding(f"brake_lock_{axle}", "brakes", sev, f"{axle_cn}刹车抱死" + (f"（{'、'.join(corners)}）" if corners else ""),
                    f"刹车时{axle_cn}抱死 {len(events)} 次、累计 {total:.2f} s，{other_cn}基本正常：刹车力偏向{axle_cn}或太大。",
                    evidence, locs, basis, tips, conf,
                    "开着 ABS 时一般不会抱死；Data Out 看不到 ABS 是否开启。" + ONE_AT_A_TIME,
                    data={"axle": axle, "events": len(events), "total_s": round(total, 3), "first_count": n_first})


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


# ================================================================ 交叉印证

def _cross_reference(ctx: _Ctx) -> None:
    """同一位置同时出现的相关问题，互相补一句说明（不改变判定）。"""
    by_id = {f["id"]: f for f in ctx.findings}
    spin, os_exit = by_id.get("wheelspin"), by_id.get("oversteer_exit")
    if spin and os_exit:
        common = {l["corner"] for l in spin["locations"]} & {l["corner"] for l in os_exit["locations"]} - {None}
        if common:
            txt = f"{'、'.join(sorted(common))} 出弯同时检测到转向过度和驱动轮空转：多半是同一件事——驱动轮空转让后轴丢了侧向抓地。"
            os_exit["evidence"].insert(len(os_exit["evidence"]) - 1, txt)
            spin["evidence"].insert(len(spin["evidence"]) - 1, txt)
    us_mid = by_id.get("understeer_mid")
    tire = by_id.get("tire_lr_front")
    if us_mid and tire and tire["severity"] != "info":
        txt = f"同一圈还有弯中转向不足（{'、'.join(us_mid['data']['corners'])}）：前轮长时间过极限滑动，也会让外侧前胎更热。"
        tire["evidence"].insert(len(tire["evidence"]) - 1, txt)


# ================================================================ 对外接口

def _lap_info(meta: dict) -> dict:
    keys = ("id", "lap_number", "lap_time", "track_id", "track_name", "car_ordinal", "car_name", "car_class",
            "car_pi", "game", "family", "kind", "is_complete", "flags", "distance_m", "session_id", "created_at")
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


def diagnose(lap: LapData, rules: dict | None = None) -> dict:
    """单圈诊断。返回（可直接 JSON 化，没有 NaN）：

        mode: "single"
        usable: 数据够不够做诊断
        lap: 这圈的元数据（id、圈号、圈时、赛道、车……）
        summary: 圈时、距离、采样率、驱动方式、最高转速、最高车速、用到的最高挡、弯道数、左右弯占比、热胎平均胎温、
                 各严重程度的问题数 counts
        corners: [{name, direction, start_m, apex_m, end_m, heading_change_deg, min_speed_kmh}]
        findings: [{id, category, category_label, severity(info|warn|alert), title, summary, evidence[],
                    locations[{dist_m, end_m, corner, phase, phase_label, wheel, wheel_label, t_s, duration_s, label}],
                    basis{channels[], rule, principle}, suggestions[], caveat, confidence(low|medium|high), data{}}]
                  按严重程度、置信度排序
        checks: [{id, name, status(found|ok|skipped), note}]  每项检查做了什么、结论如何（“没发现”和“没法判断”分开）
        not_available: [{item, why, instead}]  Data Out 给不了、所以判断不了的东西
        data_quality: [{level(info|warn|error), text}]
        rules: {source, error}  阈值来自哪个文件
    """
    info = rules_info() if rules is None else {"rules": _merge(DEFAULT_RULES, rules), "source": "调用方传入", "error": None}
    ctx = _Ctx(lap, info["rules"])
    if info["error"]:
        ctx.note("warn", info["error"])
    usable = _check_quality(ctx)
    if usable:
        _find_corners(ctx)
        for fn in (_check_balance, _check_suspension, _check_tires, _check_traction, _check_brakes, _check_gearing):
            fn(ctx)
        _cross_reference(ctx)
    else:
        for cid, nm in (("balance", "转向平衡（转向不足 / 转向过度）"), ("suspension", "悬挂触底 / 伸到底"),
                        ("tires", "胎温平衡"), ("traction", "驱动轮空转"), ("brakes", "刹车抱死"),
                        ("gearing", "齿比（最高挡撞断油 / 转速余量）")):
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
        "turn_share": {"right": round(share["right"], 3), "left": round(share["left"], 3)} if share else None,
        "tire_temps_c": ctx.extra.get("tire_temps_c"),
        "has_dash": ctx.dash,
        "counts": {s: sum(1 for f in findings if f["severity"] == s) for s in ("alert", "warn", "info")},
    }
    corners = [{"name": c["name"], "direction": c["direction"], "start_m": round(c["start_m"], 1),
                "apex_m": round(c["apex_m"], 1), "end_m": round(c["end_m"], 1),
                "heading_change_deg": c["heading_change_deg"], "min_speed_kmh": round(c["min_speed"] * 3.6, 1)}
               for c in ctx.corners]
    return _json_safe({
        "mode": "single", "usable": usable, "lap": _lap_info(m), "lap_label": _lap_label(m), "summary": summary,
        "corners": corners, "findings": findings, "checks": ctx.checks, "not_available": copy.deepcopy(NOT_AVAILABLE),
        "data_quality": ctx.quality, "rules": {"source": info["source"], "error": info["error"]},
    })


def diagnose_many(laps: list[LapData], rules: dict | None = None) -> dict:
    """多圈合并（同一辆车、同一条赛道的几圈）。

    每圈先单独诊断，再按 finding.id 合并：
    - 每圈都出现（≥ 2 圈）→ 置信度升一级；
    - 只在少于 sporadic_frac 比例的圈里出现 → 视为偶发，严重程度、置信度各降一级；
    - evidence 开头写“在几圈里出现”，接着是代表圈（最严重那圈）的完整证据，再每圈一行摘要；
    - locations 合并所有圈，每个位置带 lap_id / lap_label。
    返回结构和 diagnose() 一样（mode = "multi"），另有 laps（每圈摘要）和每条 finding 的 repeat 字段。
    """
    info = rules_info() if rules is None else {"rules": _merge(DEFAULT_RULES, rules), "source": "调用方传入", "error": None}
    rr = info["rules"]
    results = [diagnose(l, rr) for l in laps]
    for res in results:
        res["rules"] = {"source": info["source"], "error": info["error"]}
    usable = [x for x in results if x["usable"]]
    quality: list[dict] = []
    if info["error"]:
        quality.append({"level": "warn", "text": info["error"]})
    if not laps:
        quality.append({"level": "error", "text": "数据不足：没有给任何一圈。"})
    cars = {(l.meta.get("game"), l.meta.get("car_ordinal")) for l in laps}
    tracks = {l.meta.get("track_id") for l in laps}
    if len(cars) > 1:
        quality.append({"level": "warn", "text": "这几圈不是同一辆车，合并结果仅供参考（不同车的问题不能互相印证）。"})
    if len(tracks) > 1:
        quality.append({"level": "warn", "text": "这几圈不是同一条赛道，合并结果仅供参考（弯道编号对不上）。"})
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
    for cid in ("balance", "suspension", "tires", "traction", "brakes", "gearing"):
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
                 "lap_time_s": res["summary"]["lap_time_s"], "usable": res["usable"], "counts": res["summary"]["counts"]}
                for res in results]
    summary = dict(first["summary"]) if first else {}
    summary["counts"] = {s: sum(1 for f in findings if f["severity"] == s) for s in ("alert", "warn", "info")}
    summary["laps_used"] = n
    summary["laps_total"] = n_all
    summary["reference_lap"] = first["lap_label"] if first else None
    return _json_safe({
        "mode": "multi", "usable": n > 0, "lap": first["lap"] if first else {}, "lap_label": f"{n_all} 圈合并",
        "laps": lap_rows, "summary": summary, "corners": first["corners"] if first else [],
        "findings": findings, "checks": checks, "not_available": copy.deepcopy(NOT_AVAILABLE),
        "data_quality": quality, "rules": {"source": info["source"], "error": info["error"]},
    })


def track_outline(lap: LapData, max_points: int = 600) -> dict:
    """赛道小地图用：降采样后的 {pos_x, pos_z, dist} 列表（和 LapData.to_json 的通道名一致）。"""
    n = len(lap)
    if n == 0 or not (lap.has("pos_x") and lap.has("pos_z") and lap.has("dist")):
        return {"pos_x": [], "pos_z": [], "dist": []}
    step = max(1, math.ceil(n / max_points))
    idx = np.unique(np.concatenate([np.arange(0, n, step), [n - 1]]))
    return _json_safe({k: np.round(np.asarray(lap[k], float)[idx], 2) for k in ("pos_x", "pos_z", "dist")})
