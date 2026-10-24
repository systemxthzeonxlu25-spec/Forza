"""各项检查共用的常量、小工具、单圈上下文 _Ctx（检查函数都只认 _Ctx）。"""

from __future__ import annotations


import numpy as np

from ..corners import corner_at
from ..lapdata import LapData

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


def _hands_off(ctx: _Ctx, region: np.ndarray, *, clutch: bool = False) -> tuple[np.ndarray, str]:
    """去掉拉着手刹（clutch=True 时还有踩着离合）的采样。

    拉手刹时后轮是被手刹拖住的（FH 里用手刹过发卡弯很常见）；踩着离合时发动机制动 / 驱动力传不到车轮。
    这些采样里的滑移是司机操作造成的，不能算到刹车平衡、刹车压力、差速器、空转、转向平衡头上。
    返回 (可用的采样掩码, 说明)：说明是 region 里被去掉的时间，例如“（拉手刹 0.40 s 不计）”，没去掉就是空字符串。
    老圈没有手刹 / 离合通道就全算。
    """
    g = ctx.r["general"]
    ok = np.ones(ctx.n, bool)
    parts = []
    for ch_name, lim, word in (("handbrake", g["handbrake_max"], "拉手刹"), ("clutch", g["clutch_max"], "踩离合")):
        a = ctx.ch(ch_name) if (ch_name == "handbrake" or clutch) else None
        if a is None:
            continue
        on = a >= lim
        s = float(np.sum(region & on) * ctx.dt)
        if s > 0:
            parts.append(f"{word} {s:.2f} s")
        ok &= ~on
    return ok, (f"（{'、'.join(parts)} 不计）" if parts else "")


# ---------------- 圈的标签（检查项和主流程都要用）

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
