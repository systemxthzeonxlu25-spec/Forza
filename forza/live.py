"""实时推送：把最新一帧推给所有连着的浏览器（手机实时面板走 WebSocket /ws/live）。

设计要点
--------
- 收包循环（UDP / 回放）只调用 on_frame()：记下最新一帧、序号 +1，O(1)，不做任何 IO。
- 每个 WebSocket 客户端一个独立的发送协程（serve_client），按自己的频率取“最新的一条”发出去（只发最新、不排队）：
  慢的手机只会自己掉帧，不会堵住别的手机，更不会卡住收包；一次发送超过 SEND_TIMEOUT_S 秒就断开它，让它自己重连。
  （彻底失联的客户端，uvicorn 自带的 WebSocket 心跳也会在约 40 秒后把连接关掉。）
- 消息按 (帧序号, 版本号) 缓存：同一帧只组装 / 序列化一次，多个客户端共用。
  本圈小结、录制状态变了就把版本号 +1（invalidate），缓存自动失效。
- 只有收到新帧才发 frame 消息；没有新数据时每 STATUS_INTERVAL_S 秒发一条轻量的 status 消息，
  面板据此显示“没有收到数据”和设置提示。
- 局域网 IP 查询（getaddrinfo 在有些 Windows 上会卡）放在 run() 里定期后台刷新，不进推送路径。
- 慢活（查库、解压历史最快圈、跑两圈对比）都放到自己的单线程池 _pool 里做，推送路径只读缓存。

本圈小结（新圈存好之后）
------------------------
LapSaver 存好一圈 → on_lap_saved()（在存档线程里调用，自己 try/except）→ 丢进 _pool：
用 api.laps.default_reference 选参考圈（同赛道同车最快的完整圈，新圈自己最快就取第二快），
跑 analysis.compare，取总差距和丢时最多的几个弯的解释句。
本圈或参考圈是起步圈（发车格在线后，这圈比飞驰圈长）时只报总差距、不列逐弯解释：
compare 按圈长等比例缩放距离，起步圈的每个弯都会错开几十到上百米，逐弯结论是错的。
结果放在 last_summary，
**每条 frame / status 消息都带上**（不单独发一条，否则会被下一帧顶掉）；手机发现 id 变了才弹卡片。
最近 SUMMARY_KEEP 条可以用 GET /api/live/summaries 翻看。

实时差距的参照
--------------
- 本场最快（lap.delta / ref_time / predicted）：本场比赛最快的完整圈，含刚过线还在挂起的那圈。
- 历史最快 PB（lap.pb）：数据库里同赛道、同车、同游戏最快的完整圈（和本场最快取更快的那个）。
  赛道只查不建：FM2023 按 TrackOrdinal；其他游戏的赛道赛在第一次过线后按过线位置（+ 圈长）匹配，
  冲刺赛按起点位置匹配（规则同 storage.find_or_create_track，见 match_track）。
  同一个 car_ordinal 在 FH 里可能是不同改装：优先取同 PI 的圈，没有才用别的 PI 的并标 pi_match = false。
- 起步圈（first_lap）：从发车格静止起步，起点和距离零点都和飞驰圈不一样，差距只供参考（lap.first_lap = true）。
- 冲刺赛（kind = "sprint?"）没有本场参照，只有 PB。

客户端参数
----------
    /ws/live?hz=30     这台设备自己的推送频率，限制在 5~30；不给就用 cfg.live_hz（默认 20）

调试：GET /api/live/snapshot 返回和 WebSocket 一条消息完全相同的 JSON。

消息格式（JSON，数值已换算好单位并做了取舍）
-------------------------------------------
{
  "type": "frame" | "status",   # 收到过帧 → "frame"；从没收到过、或只是定时状态 → "status"（没有 frame / lap 字段或为 null）
  "seq": 1234,                   # 帧序号（每收到一帧 +1）
  "t": 1760000000.123,           # 服务器组装消息时的 Unix 时间（秒）
  "frame": {
    "fmt": "fm2023_dash", "fmt_label": "...", "game": "fm2023", "game_name": "Forza Motorsport (2023)", "family": "fm",
    "has_dash": true,            # false = Sled 格式：没有档位/输入/胎温/圈速等，这些字段为 null
    "is_race_on": true,          # false = 菜单 / 暂停
    "speed_kmh": 123.4,          # km/h；Sled 格式用速度向量模长估算（speed_src = "vel"）
    "speed_src": "dash" | "vel",
    "rpm": 6543, "max_rpm": 8000, "idle_rpm": 900,
    "gear": "4",                 # "R" = 倒挡，"-" = 没有（Sled）
    "throttle": 0.78, "brake": 0.0, "clutch": 0.0, "handbrake": 0.0,   # 0~1
    "steer": -0.12,              # -1~1，+ = 右
    "tire_temp_c": [fl, fr, rl, rr],      # °C
    "tire_wear": [..4] | null,            # 仅 FM2023（假数据里 0 = 新胎，越大磨得越多）
    "susp": [..4],               # 归一化悬挂行程 0 = 伸到最长，1 = 压到底
    "slip_ratio": [..4],         # 归一化滑移率，|x| > 1 = 失去抓地
    "slip_angle": [..4],         # 归一化侧偏角，|x| > 1 = 失去抓地
    "comb_slip": [..4],          # 归一化综合滑移
    "rumble": [0|1 ..4],         # 压在路肩上
    "g_lat": 0.85, "g_long": -0.4,        # G；横向 + = 右，纵向 + = 加速
    "boost": 12.3, "fuel": 0.87, "power_kw": 312.5, "torque": 540.0,
    "position": 3,               # 名次；0 = 没有（自由漫游）
    "track_ordinal": 860 | null,
    "car": {"ordinal": 1234, "class": 5, "pi": 800, "drivetrain": 1, "cylinders": 8}
  },
  "lap": {                       # Sled 格式为 null
    "active": true,              # 切圈器正在记录一场比赛
    "kind": "circuit" | "sprint?" | "lap" | null,
    "number": 2,                 # 游戏的 LapNumber（从 0 开始，界面上显示 +1）
    "first_lap": false,          # 本圈是起步圈（发车格静止起步）：差距只供参考
    "time": 45.678,              # 本圈已用时间 s
    "dist": 1234.5,              # 本圈已跑距离 m
    "last": 83.456 | null,       # 上一圈 s
    "best": 82.9 | null,         # 最快圈 s（游戏给的 BestLap，没有就用本场切圈结果）
    "ref_time": 82.9 | null,     # 实时差距的参照圈（本场最快完整圈）圈时
    "delta": -0.42 | null,       # 实时差距 s：按距离对齐，本圈用时 − 参照圈在同一距离的用时；负 = 更快
    "predicted": 82.48 | null,   # 预计圈时 = ref_time + delta
    "pb": null | {               # 历史最快（PB）参照；null = 不适用（没在跑圈）
      "state": "ok" | "loading" | "none",   # loading = 后台正在查库；none = 没有可用的 PB（reason 说明原因）
      "reason": "...",
      "ref_time": 81.5, "delta": 0.3, "predicted": 81.8,
      "lap_id": 12, "track_id": 3, "car_pi": 800,
      "pi_match": true,          # false = PB 是别的 PI（改装）跑的
      "source": "db" | "session" # session = 本场刚跑的圈比数据库里的还快
    },
    "race_time": 300.1, "position": 3
  },
  "last_summary": null | {       # 最近一圈存好后的小结（每条消息都带，手机按 id 判断是不是新的）
    "id": 12, "lap_id": 12, "t": 1760000000.0, "lap_number": 2, "kind": "lap", "lap_time": 83.4,
    "is_complete": true, "first_lap": false, "flags": [], "track_name": "...", "setup_name": null,
    "ref": {"id": 9, "lap_number": 1, "lap_time": 82.9, "first_lap": false} | null,
    "lap_time_delta_s": 0.5 | null, "summary": "比参考圈慢 0.500 s，主要丢在 T3（0.31 s）",
    "top": [{"name": "T3", "time_delta_s": 0.31, "explanation": "T3：慢 0.31 s。刹车点早 25 m"}],
                                 # 本圈或参考圈是起步圈时 top = []，summary 只报总差距（距离对不上，不做逐弯分析）
    "note": "起步圈……" | null
  },
  "status": {
    "source": "udp:5555", "packets": 12345, "pps": 60.0,
    "since_last_packet": 0.02 | null,     # 秒；null = 从没收到过包
    "format": "fm2023_dash", "format_label": "...", "game": "fm2023", "game_name": "...", "game_note": "...",
    "has_dash": true, "warnings": ["..."], "recording": "path" | null, "replay": "path" | null,
    "udp_port": 5555, "http_port": 8000, "lan_ips": ["192.168.1.23"],
    "clients": 1                 # 当前连着几个实时面板
  }
}
定时 status 消息只有 type / seq / t / last_summary / status 五个字段。
"""
from __future__ import annotations

import asyncio
import json
import logging
import math
import statistics
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from contextlib import suppress
from dataclasses import dataclass

import numpy as np
from fastapi import WebSocket

from .frame import Frame
from .lapdata import LapData
from .laps import DIST_MISMATCH
from .netinfo import lan_ips
from .packet import FORMATS
from .profiles import get_profile

log = logging.getLogger(__name__)

MIN_HZ, MAX_HZ = 5.0, 30.0       # ?hz= 允许的范围
STATUS_INTERVAL_S = 1.0          # 没有新帧时，多久发一次 status 消息
STATUS_CACHE_S = 0.5             # status 块缓存多久（pps 等不需要每帧算）
SEND_TIMEOUT_S = 3.0             # 一次发送超过这么久 = 客户端太慢 / 掉线，断开让它重连
LAN_IP_REFRESH_S = 30.0          # 局域网 IP 多久刷新一次（换 Wi-Fi 之类）
DELTA_DIST_TOLERANCE = 1.01      # 本圈距离超过参照圈全长的 1% 以上就不给实时差距
SUMMARY_KEEP = 20                # 最近几条本圈小结留着给菜单翻看
SUMMARY_TOP_N = 3                # 小结里列几个丢时最多的弯
SUMMARY_MIN_LOSS_S = 0.03        # 丢时少于这个的弯不列
PB_CACHE_KEEP = 8                # PB 查询结果最多缓存几条（不同赛道 / 车 / 比赛）
PB_SCAN_LIMIT = 200              # 查 PB 时最多看这么多圈（按圈时排序）

# 赛道匹配规则：和 storage.find_or_create_track 一致（只查不建）
try:
    from .storage import TRACK_LENGTH_TOL, TRACK_START_RADIUS_M
except ImportError:              # 存储模块改了常量名时退回默认值
    TRACK_START_RADIUS_M, TRACK_LENGTH_TOL = 60.0, 0.04


def _r(x, nd: int = 2):
    """数值取舍；NaN / inf / 不是数 → None（JSON 里不能出现 NaN，否则浏览器解析会失败）。"""
    try:
        x = float(x)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(x):
        return None
    return round(x, nd)


def _r4(vals, nd: int = 2) -> list:
    return [_r(v, nd) for v in vals]


def _pos(x) -> float | None:
    """游戏里 0 表示“没有”的时间字段：<= 0 → None。"""
    v = _r(x, 3)
    return v if v is not None and v > 0 else None


def _flags(v) -> list:
    """laps.flags 列：库里是 JSON 文本，meta 里是列表。"""
    if isinstance(v, str):
        try:
            v = json.loads(v or "[]")
        except ValueError:
            return []
    return list(v or [])


# ====================================================================== 赛道匹配（只查不建）

def match_track(store, *, family: str, kind: str, track_ordinal: int | None, x: float | None,
                z: float | None, length_m: float | None = None, length_tol: float | None = TRACK_LENGTH_TOL,
                car_ordinal: int | None = None, game: str | None = None) -> int | None:
    """按 storage.find_or_create_track 的规则找已有赛道，找不到返回 None（不新建）。

    - 有 TrackOrdinal（FM2023）：按 ordinal
    - 否则：起点坐标相距 < TRACK_START_RADIUS_M；赛道赛再按圈长：
        length_tol 不为 None：圈长差 < length_tol（和存档时一样），多个候选取 id 最小的（存档时也是取第一个）
        length_tol 为 None（只知道起步圈的长度，可能偏长）：取圈长最接近的
        不知道圈长：取这台车在上面跑过最多完整圈的
      冲刺赛只看起点，多个候选取最近的。
    """
    if track_ordinal:
        rows = store.query("SELECT id FROM tracks WHERE family=? AND track_ordinal=? AND kind=? ORDER BY id",
                           (family, track_ordinal, kind))
        return rows[0]["id"] if rows else None
    if x is None or z is None:
        return None
    rows = store.query("SELECT id, start_x, start_z, ref_length_m FROM tracks "
                       "WHERE family=? AND kind=? AND track_ordinal IS NULL ORDER BY id", (family, kind))
    cands = []
    for r in rows:
        if r["start_x"] is None or r["start_z"] is None:
            continue
        d = math.hypot(r["start_x"] - x, r["start_z"] - z)
        if d <= TRACK_START_RADIUS_M:
            cands.append((r, d))
    if not cands:
        return None
    if kind != "circuit":
        return min(cands, key=lambda c: (c[1], c[0]["id"]))[0]["id"]
    if length_m and length_m > 0:
        if length_tol is not None:
            ok = [r for r, _ in cands
                  if not r["ref_length_m"] or abs(r["ref_length_m"] - length_m) / r["ref_length_m"] <= length_tol]
            return ok[0]["id"] if ok else None
        with_len = [(r, abs(r["ref_length_m"] - length_m)) for r, _ in cands if r["ref_length_m"]]
        if with_len:
            return min(with_len, key=lambda c: (c[1], c[0]["id"]))[0]["id"]
        return cands[0][0]["id"]
    if len(cands) == 1:
        return cands[0][0]["id"]
    best_id, best_n = cands[0][0]["id"], -1
    for r, _ in cands:
        sql = "SELECT COUNT(*) AS n FROM laps WHERE track_id=? AND is_complete=1"
        args: list = [r["id"]]
        if car_ordinal is not None:
            sql += " AND car_ordinal=?"
            args.append(car_ordinal)
        if game is not None:
            sql += " AND game=?"
            args.append(game)
        n = store.query(sql, args)[0]["n"]
        if n > best_n:
            best_id, best_n = r["id"], n
    return best_id


def _ref_arrays(lap: LapData) -> tuple[np.ndarray, np.ndarray, bool]:
    """存档圈的 (距离, 时间, 是否速度积分距离)，距离做成单调的（np.interp 要求）。"""
    integrated = "dist_integrated" in _flags(lap.meta.get("flags"))
    rd = np.maximum.accumulate(np.asarray(lap.ch["dist"], dtype=np.float64))
    rt_ = np.asarray(lap.ch["t"], dtype=np.float64)
    return rd, rt_, integrated


# ====================================================================== 本圈小结

def _fixed_corners(store, ref: LapData) -> list[dict] | None:
    """赛道的固定弯道表（forza.sectors，和对比页同一套弯道编号）映射到参考圈上；没有或出错返回 None。"""
    try:
        from . import sectors
    except Exception:
        return None
    try:
        ensure = getattr(sectors, "ensure_layout", None)
        if ensure is not None:
            ensure(store, ref.meta.get("track_id"))
        return sectors.layout_corners(store, ref)
    except Exception:
        log.debug("本圈小结：读取固定弯道表失败，改用参考圈自动识别弯道", exc_info=True)
        return None


def _first_lap_summary(d: float | None, first: bool, ref_first: bool) -> str:
    """本圈 / 参考圈是起步圈时的小结：只说总差距和为什么不做逐弯分析。"""
    if d is None:
        head = "和参考圈的圈时差算不出来"
    elif abs(d) < 0.0005:
        head = "和参考圈圈时一样"
    else:
        head = f"比参考圈{'慢' if d > 0 else '快'} {abs(d):.3f} s"
    if first and ref_first:
        return f"{head}（两圈都是发车格静止起步，发车位置可能不同），起步圈不做逐弯分析"
    if first:
        return f"{head}（含静止起步和发车格到起点线那一段），起步圈不做逐弯分析"
    return f"{head}（参考圈是起步圈，含静止起步和发车格到起点线那一段），等有了飞驰圈再做逐弯分析"


def build_lap_summary(store, lap: LapData) -> dict:
    """新圈存好后的小结：和参考圈（default_reference）对比的总差距 + 丢时最多的几个弯。纯计算 + 读库。"""
    from .analysis import compare
    from .api.laps import default_reference

    m = lap.meta
    flags = _flags(m.get("flags"))
    first = "first_lap" in flags and m.get("kind") != "sprint"
    out = {
        "id": m.get("id"), "lap_id": m.get("id"), "t": round(time.time(), 3),
        "lap_number": m.get("lap_number"), "kind": m.get("kind"),
        "lap_time": _r(m.get("lap_time"), 3), "is_complete": bool(m.get("is_complete")),
        "first_lap": first, "flags": flags,
        "track_id": m.get("track_id"), "track_name": m.get("track_name"),
        "setup_name": m.get("setup_name"),
        "ref": None, "lap_time_delta_s": None, "summary": "", "top": [], "note": None,
    }
    notes = []
    if first:
        notes.append("起步圈：从发车格静止起步，圈时和对比只供参考")
    if not m.get("is_complete"):
        out["summary"] = "没跑完（中途放弃），不做对比"
        out["note"] = "；".join(notes) or None
        return out
    if out["track_name"] is None and m.get("track_id") is not None:
        with suppress(Exception):
            out["track_name"] = (store.get_track(m["track_id"]) or {}).get("name")
    ref_meta = default_reference(store, m) if m.get("id") is not None else None
    if ref_meta is None:
        out["summary"] = "这条赛道 + 这台车的第一个完整圈，还没有参考圈可比"
        out["note"] = "；".join(notes) or None
        return out
    ref = store.get_lap(ref_meta["id"])
    ref_first = "first_lap" in _flags(ref_meta.get("flags")) and ref_meta.get("kind") != "sprint"
    out["ref"] = {"id": ref_meta["id"], "lap_number": ref_meta.get("lap_number"),
                  "lap_time": _r(ref_meta.get("lap_time"), 3), "first_lap": ref_first}
    if ref_first:
        notes.append("参考圈是起步圈")
    if first or ref_first:
        # 起步圈的距离零点在发车格，比飞驰圈长：compare 按圈长等比例缩放，每个弯都对不上，逐弯解释是错的。
        # 只报总差距（圈时直接相减），不跑 compare。
        d = None
        if _r(m.get("lap_time"), 3) is not None and out["ref"]["lap_time"] is not None:
            d = round(float(m["lap_time"]) - float(ref_meta["lap_time"]), 3)
        out["lap_time_delta_s"] = d
        out["summary"] = _first_lap_summary(d, first, ref_first)
        out["note"] = "；".join(notes) or None
        return out
    corners = _fixed_corners(store, ref)
    try:
        res = compare(ref, lap, corners=corners) if corners else compare(ref, lap)
    except ValueError as e:
        out["summary"] = f"没法对比：{e}"
        out["note"] = "；".join(notes) or None
        return out
    except Exception:            # 对比模块自己出错：小结照样出，只是没有逐弯分析
        log.exception("本圈小结：两圈对比出错")
        out["summary"] = "两圈对比出错了，没有逐弯分析（详见服务端日志）"
        out["note"] = "；".join(notes) or None
        return out
    tot = res.get("totals") or {}
    out["lap_time_delta_s"] = _r(tot.get("lap_time_delta_s"), 3)
    out["summary"] = tot.get("summary") or ""
    top = []
    for it in res.get("ranking") or []:
        if len(top) >= SUMMARY_TOP_N:
            break
        dt = _r(it.get("time_delta_s"), 3)
        if dt is None or dt < SUMMARY_MIN_LOSS_S:
            continue
        top.append({"name": it.get("name"), "time_delta_s": dt, "explanation": it.get("explanation")})
    out["top"] = top
    out["note"] = "；".join(notes) or None
    return out


@dataclass(eq=False)   # 按对象身份放进 set
class _Client:
    ws: WebSocket
    hz: float
    sent: int = 0


class LiveBroadcaster:
    def __init__(self, rt):
        self.rt = rt
        self.latest: Frame | None = None
        self.seq = 0                     # 帧序号，每收到一帧 +1
        self.clients: set[_Client] = set()
        self._ver = 0                    # 消息内容版本：小结 / 录制状态变了就 +1，让缓存失效
        self._msg_cache: tuple[tuple, str] | None = None     # ((seq, ver), 序列化好的 frame 消息)
        self._status_cache: tuple[float, int, dict] | None = None
        self._ips: list[str] | None = None
        self._ref_cache: tuple | None = None                 # 参照圈的 (key, dist 数组, t 数组, 是否积分距离)
        # 后台单线程：小结（compare）、PB 查库 / 解压都在这里做，不进推送路径
        self._pool: ThreadPoolExecutor | None = ThreadPoolExecutor(max_workers=1, thread_name_prefix="live-bg")
        # 本圈小结
        self.summaries: deque[dict] = deque(maxlen=SUMMARY_KEEP)
        self.last_summary: dict | None = None
        # 历史最快（PB）
        self._pb_ver = 0                 # 每存好一圈 +1：PB 可能变了，重新查
        self._pb_cache: dict[tuple, dict] = {}
        self._pb_last: dict[tuple, dict] = {}    # 同一赛道 + 车的上一个结果（不含版本号）：新结果出来前先用它，不闪
        self._pb_jobs: set[tuple] = set()
        self._pb_lap: tuple | None = None    # (lap_id, rd, rt, integrated)：解压过的 PB 圈，id 没变就不再解压

    # ------------------------------------------------------------------ 收包侧
    def on_frame(self, fr: Frame) -> None:
        """Hub 每解析出一帧就调用一次：只记下来，不做别的（不能拖慢收包）。"""
        self.latest = fr
        self.seq += 1

    async def run(self) -> None:
        """后台维护：定期在线程里刷新局域网 IP。推送本身由每个客户端自己的协程负责（见 serve_client）。"""
        while True:
            try:
                self._ips = await asyncio.to_thread(lan_ips)
            except Exception:
                log.debug("查询局域网 IP 失败", exc_info=True)
            await asyncio.sleep(LAN_IP_REFRESH_S)

    def invalidate(self) -> None:
        """消息内容变了（小结 / 录制状态）：让 frame 消息缓存和 status 块缓存失效。可以在任意线程调用。"""
        self._ver += 1
        self._status_cache = None

    def _submit(self, fn, *args) -> bool:
        pool = self._pool
        if pool is None:
            return False
        try:
            pool.submit(fn, *args)
            return True
        except RuntimeError:     # 已经关闭
            return False

    def close(self) -> None:
        """等后台任务做完并关闭线程池（程序退出时调用）。"""
        pool, self._pool = self._pool, None
        if pool is not None:
            pool.shutdown(wait=True)

    def wait_idle(self, timeout: float = 30.0) -> None:
        """等后台线程把手上的任务做完（测试用）。"""
        pool = self._pool
        if pool is not None:
            pool.submit(lambda: None).result(timeout)

    # ------------------------------------------------------------------ 新圈存好之后
    def on_lap_saved(self, lap: LapData) -> None:
        """LapSaver 存好一圈后调用（在存档线程里）。只把活丢进后台线程，自己出错也不能被记成“存档失败”。"""
        try:
            self._pb_ver += 1            # 数据库里多了一圈：PB 可能变了
            self._submit(self._make_summary, lap)
        except Exception:
            log.exception("处理新存档的圈出错")

    def _make_summary(self, lap: LapData) -> None:
        try:
            s = build_lap_summary(self.rt.store, lap)
        except Exception:
            log.exception("生成本圈小结出错")
            return
        if s.get("id") is None:
            return
        self.summaries.append(s)
        self.last_summary = s
        self.invalidate()
        log.info("本圈小结：#%s %s", s["id"], s["summary"])

    def on_lap_invalidated(self, meta: dict) -> None:
        self._pb_ver += 1
        self._pb_cache.clear()
        self._pb_last.clear()
        # 删除小结也丢进后台队列：排在正在生成的小结之后，免得刚删掉又被生成出来
        self._submit(self._remove_summary, meta["id"])
        self.invalidate()

    def _remove_summary(self, lap_id: int) -> None:
        self.summaries = deque((s for s in self.summaries if s.get("id") != lap_id),
                               maxlen=self.summaries.maxlen)
        self.last_summary = self.summaries[-1] if self.summaries else None
        self.invalidate()

    def recent_summaries(self) -> list[dict]:
        """最近的小结，新的在前。"""
        return list(reversed(self.summaries))

    # ------------------------------------------------------------------ 组装消息
    def snapshot(self, status_only: bool = False) -> dict:
        """当前状态的一条完整消息（dict）。"""
        fr = self.latest
        msg: dict = {"type": "frame" if fr is not None and not status_only else "status",
                     "seq": self.seq, "t": round(time.time(), 3)}
        if not status_only:
            msg["frame"] = self._frame_block(fr) if fr is not None else None
            lap = None
            if fr is not None:
                try:
                    lap = self._lap_block(fr)
                except Exception:   # 切圈器内部状态读取出错时不能影响推送
                    log.debug("组装圈速信息出错", exc_info=True)
            msg["lap"] = lap
        msg["last_summary"] = self.last_summary
        msg["status"] = self._status_block()
        return msg

    def message_text(self) -> str:
        """序列化好的 frame 消息，按 (帧序号, 版本号) 缓存（同一帧多个客户端共用）。"""
        key = (self.seq, self._ver)      # 先取 key 再组装：组装途中版本变了，下次自然重新组装
        c = self._msg_cache
        if c is not None and c[0] == key and self.latest is not None:
            return c[1]
        text = self._dumps(self.snapshot())
        if self.latest is not None:
            self._msg_cache = (key, text)
        return text

    def status_text(self) -> str:
        return self._dumps(self.snapshot(status_only=True))

    @staticmethod
    def _dumps(msg: dict) -> str:
        return json.dumps(msg, ensure_ascii=False, separators=(",", ":"), allow_nan=False)

    def _frame_block(self, fr: Frame) -> dict:
        dash = fr.has_dash
        prof = get_profile(fr.game)
        fmt = FORMATS.get(fr.fmt)
        speed = fr.speed if dash else fr.velocity_mag
        return {
            "fmt": fr.fmt,
            "fmt_label": fmt.label if fmt else fr.fmt,
            "game": fr.game,
            "game_name": prof.name,
            "family": fr.family,
            "has_dash": dash,
            "is_race_on": bool(fr.is_race_on),
            "speed_kmh": _r(speed * 3.6, 1),
            "speed_src": "dash" if dash else "vel",
            "rpm": int(round(fr.rpm)) if math.isfinite(fr.rpm) else None,
            "max_rpm": int(round(fr.engine_max_rpm)) if math.isfinite(fr.engine_max_rpm) else None,
            "idle_rpm": int(round(fr.engine_idle_rpm)) if math.isfinite(fr.engine_idle_rpm) else None,
            "gear": fr.gear_label,
            "throttle": _r(fr.throttle / 255.0, 3) if dash else None,
            "brake": _r(fr.brake / 255.0, 3) if dash else None,
            "clutch": _r(fr.clutch / 255.0, 3) if dash else None,
            "handbrake": _r(fr.handbrake / 255.0, 3) if dash else None,
            "steer": _r(fr.steer / 127.0, 3) if dash else None,
            "tire_temp_c": _r4(fr.tire_temp_c(), 1) if dash else None,
            "tire_wear": _r4(fr.tire_wear, 4) if dash and fr.tire_wear is not None else None,
            "susp": _r4(fr.susp_norm, 3),
            "slip_ratio": _r4(fr.slip_ratio, 2),
            "slip_angle": _r4(fr.slip_angle, 2),
            "comb_slip": _r4(fr.combined_slip, 2),
            "rumble": [1 if r else 0 for r in fr.rumble],
            "g_lat": _r(fr.g_lat, 3),
            "g_long": _r(fr.g_long, 3),
            "boost": _r(fr.boost, 1) if dash else None,
            "fuel": _r(fr.fuel, 3) if dash else None,
            "power_kw": _r(fr.power / 1000.0, 1) if dash else None,
            "torque": _r(fr.torque, 1) if dash else None,
            "position": fr.race_position if dash else None,
            "track_ordinal": fr.track_ordinal,
            "car": {"ordinal": fr.car_ordinal, "class": fr.car_class, "pi": fr.car_pi,
                    "drivetrain": fr.drivetrain, "cylinders": fr.num_cylinders},
        }

    # ---------------- 圈速 + 实时差距 ----------------
    @staticmethod
    def _delta_vs(rd: np.ndarray, rt_: np.ndarray, integrated: bool, lap_t, d_field, d_int):
        """按距离对齐算差距：返回 (用的距离, 差距)；距离超出参照圈全长就不给差距。"""
        dist = d_int if integrated else d_field
        if dist is None or lap_t is None or len(rd) < 2 or not 0 <= dist <= rd[-1] * DELTA_DIST_TOLERANCE:
            return dist, None
        return dist, float(lap_t - np.interp(dist, rd, rt_))

    def _lap_block(self, fr: Frame) -> dict | None:
        if not fr.has_dash:
            return None
        det = self.rt.detector
        ev, cur = det.event, det.cur
        st_kind = det.event_kind()
        in_lap = ev is not None and cur is not None and cur.lap_number == fr.lap_number
        lap_t = cur.last_t if in_lap else (fr.current_lap if fr.current_lap > 0 else None)
        first_lap = bool(in_lap and "first_lap" in cur.flags and st_kind != "sprint?")

        # 本圈距离：两种口径都算，和参照圈用同一种
        d_field = fr.distance - cur.start_dist if in_lap else None
        d_int = cur.int_dist if in_lap else None

        ref = self._reference(det, fr) if in_lap and st_kind != "sprint?" else None
        delta = None
        dist = d_field
        ref_time = None
        if ref is not None:
            ref_time, rd, rt_, integrated = ref
            dist, delta = self._delta_vs(rd, rt_, integrated, lap_t, d_field, d_int)

        pb = None
        if in_lap:
            try:
                pb = self._pb_block(det, fr, st_kind, ref, lap_t, d_field, d_int)
            except Exception:
                log.debug("组装 PB 差距出错", exc_info=True)
                pb = {"state": "none", "reason": "查询 PB 出错"}
            if dist is None and pb and pb.get("dist") is not None:
                dist = pb["dist"]
            if pb:
                pb.pop("dist", None)

        best = _pos(fr.best_lap) or (_r(ev.best_time, 3) if ev and ev.best_time else None) or _r(ref_time, 3)
        return {
            "active": ev is not None,
            "kind": st_kind,
            "number": fr.lap_number,
            "first_lap": first_lap,
            "time": _r(lap_t, 3),
            "dist": _r(dist, 1),
            "last": _pos(fr.last_lap),
            "best": best,
            "ref_time": _r(ref_time, 3),
            "delta": _r(delta, 3),
            "predicted": _r(ref_time + delta, 3) if delta is not None and ref_time is not None else None,
            "pb": pb,
            "race_time": _pos(fr.race_time),
            "position": fr.race_position,
        }

    def _reference(self, det, fr: Frame):
        """本场比赛最快的完整圈（含刚过线、还在挂起等待落盘的那一圈），作为实时差距的参照。

        返回 (圈时, 距离数组, 时间数组, 距离是否用速度积分口径)；没有参照圈返回 None。
        """
        ev = det.event
        cands = []
        if ev is not None and ev.best_lap is not None and ev.best_time is not None:
            cands.append((float(ev.best_time), "best", ev.best_lap))
        pend = det.pending
        if pend is not None and len(pend.rows) > 1 and "partial_start" not in pend.flags:
            # 挂起圈的圈时：和切圈器落盘时一样，优先用游戏给的 LastLap
            t = fr.last_lap if fr.lap_number == pend.lap_number + 1 and fr.last_lap > 0 else pend.last_t
            if t > 0:
                cands.append((float(t), "pending", pend))
        if not cands:
            return None
        lap_time, kind, obj = min(cands, key=lambda c: c[0])
        key = (kind, id(obj), len(obj.rows) if kind == "pending" else len(obj))
        c = self._ref_cache
        if c is None or c[0] != key:
            if kind == "best":
                integrated = "dist_integrated" in (obj.meta.get("flags") or [])
                rd = np.asarray(obj.ch["dist"], dtype=np.float64)
                rt_ = np.asarray(obj.ch["t"], dtype=np.float64)
            else:
                rt_ = np.asarray([r[0] for r in obj.rows], dtype=np.float64)
                d_field = np.asarray([r[1] for r in obj.rows], dtype=np.float64)
                d_int = np.asarray(obj.idist, dtype=np.float64)
                # 和切圈器 _emit 的规则一致：DistanceTraveled 对不上速度积分（或不单调）就用积分距离
                total_int = float(d_int[-1]) if len(d_int) else 0.0
                total_field = float(d_field[-1]) if len(d_field) else 0.0
                monotonic = bool(np.all(np.diff(d_field) >= -0.5))
                integrated = total_int > 50 and (not monotonic or abs(total_field - total_int) / total_int > DIST_MISMATCH)
                rd = d_int if integrated else d_field
            rd = np.maximum.accumulate(rd)   # np.interp 要求 x 单调
            c = self._ref_cache = (key, rd, rt_, integrated)
        return lap_time, c[1], c[2], c[3]

    # ---------------- 历史最快（PB） ----------------
    def _pb_query(self, det, fr: Frame, st_kind: str | None) -> tuple | None:
        """这一帧该用哪条 PB 查询：返回 (缓存 key, 查询参数)；还没法匹配赛道时返回 None。"""
        ev = det.event
        if ev is None:
            return None
        q = {"family": ev.family, "game": ev.game, "car_ordinal": ev.car_ordinal,
             "car_pi": int(fr.car_pi or 0), "track_ordinal": None, "x": None, "z": None,
             "length_m": None, "length_tol": None}
        if fr.track_ordinal:                      # FM2023：一开始就知道是哪条赛道
            q.update(kind="circuit", track_ordinal=int(fr.track_ordinal))
            tkey = ("ord", int(fr.track_ordinal))
        elif st_kind == "sprint?":                # 冲刺赛：按起点
            q.update(kind="sprint", x=ev.start_x, z=ev.start_z)
            tkey = ("sprint", round(ev.start_x, 1), round(ev.start_z, 1))
        elif st_kind == "circuit" and ev.line_x is not None:
            # 赛道赛：第一次过线后按过线位置匹配；共用起点线的布局再按圈长区分
            flying = ev.lap_lengths[1:]           # [0] 是起步圈（发车格起步，可能偏长）
            if flying:
                length, tol = statistics.median(flying), TRACK_LENGTH_TOL
            elif ev.lap_lengths:
                length, tol = ev.lap_lengths[0], None
            elif det.pending is not None:
                length, tol = det.buf_distance(det.pending), None
            else:
                length, tol = None, None
            q.update(kind="circuit", x=ev.line_x, z=ev.line_z, length_m=length, length_tol=tol)
            tkey = ("line", round(ev.line_x, 1), round(ev.line_z, 1),
                    None if length is None else round(length / 10.0), tol)
        else:
            return None
        key = (ev.game, ev.family, ev.car_ordinal, q["car_pi"], tkey, self._pb_ver)
        return key, q

    def _pb_block(self, det, fr: Frame, st_kind, session_ref, lap_t, d_field, d_int) -> dict | None:
        """历史最快参照的差距。查库在后台线程里做，这里只读缓存（O(1)）。"""
        found = self._pb_query(det, fr, st_kind)
        if found is None:
            if st_kind == "circuit" or st_kind == "sprint?" or fr.track_ordinal:
                return {"state": "none", "reason": "还不知道是哪条赛道"}
            return {"state": "none", "reason": "第一次过线后才知道是哪条赛道"}
        key, q = found
        res = self._pb_cache.get(key)
        if res is None:
            if key not in self._pb_jobs:
                self._pb_jobs.add(key)
                if not self._submit(self._pb_job, key, q):
                    self._pb_jobs.discard(key)
            # 查库期间：先用上一次的结果（刚存了一圈、版本号变了的情况），没有就用本场最快顶上
            res = self._pb_last.get(key[:-1])
            if res is None:
                if session_ref is None:
                    return {"state": "loading"}
                res = {"state": "none"}
        cand = None
        if res.get("state") == "ok":
            cand = ("db", res["lap_time"], res["rd"], res["rt"], res["integrated"])
        # 本场刚跑的圈（含还没落盘的挂起圈）比数据库里的更快就用它；一样快时用数据库里的（有圈号可查）
        if session_ref is not None and (cand is None or session_ref[0] < cand[1] - 1e-3):
            cand = ("session", session_ref[0], session_ref[1], session_ref[2], session_ref[3])
        if cand is None:
            return {"state": "none", "reason": res.get("reason") or "没有历史最快圈"}
        source, ref_time, rd, rt_, integrated = cand
        dist, delta = self._delta_vs(rd, rt_, integrated, lap_t, d_field, d_int)
        out = {
            "state": "ok", "source": source,
            "ref_time": _r(ref_time, 3), "delta": _r(delta, 3),
            "predicted": _r(ref_time + delta, 3) if delta is not None else None,
            "lap_id": res.get("lap_id") if source == "db" else None,
            "track_id": res.get("track_id"),
            "car_pi": res.get("car_pi") if source == "db" else (int(fr.car_pi) if fr.car_pi else None),
            "pi_match": bool(res.get("pi_match", True)) if source == "db" else True,
            "dist": dist,
        }
        return out

    def _pb_job(self, key: tuple, q: dict) -> None:
        try:
            res = self._lookup_pb(**q)
        except Exception:
            log.exception("查询历史最快圈出错")
            res = {"state": "none", "reason": "查询出错"}
        if key[-1] != self._pb_ver:
            self._pb_jobs.discard(key)
            return  # A save/invalidation superseded this query while it was running.
        for cache, k in ((self._pb_cache, key), (self._pb_last, key[:-1])):
            cache[k] = res
            while len(cache) > PB_CACHE_KEEP:
                cache.pop(next(iter(cache)), None)
        self._pb_jobs.discard(key)

    def _lookup_pb(self, *, family, game, car_ordinal, car_pi, kind, track_ordinal, x, z,
                   length_m, length_tol) -> dict:
        """（后台线程）找赛道 → 找同车最快完整圈 → 解压出距离 / 时间数组。"""
        store = self.rt.store
        track_id = match_track(store, family=family, kind=kind, track_ordinal=track_ordinal, x=x, z=z,
                               length_m=length_m, length_tol=length_tol, car_ordinal=car_ordinal, game=game)
        if track_id is None:
            return {"state": "none", "reason": "这条赛道还没有存过圈"}
        rows = store.query(
            "SELECT id, lap_time, car_pi, flags, kind FROM laps WHERE track_id=? AND car_ordinal=? AND game=? "
            "AND is_complete=1 AND lap_time > 0 ORDER BY lap_time ASC LIMIT ?",
            (track_id, car_ordinal, game, PB_SCAN_LIMIT))
        if kind == "circuit":   # 起步圈的起点和距离零点不一样，不拿来当参照
            rows = [r for r in rows if "first_lap" not in _flags(r["flags"])]
        if not rows:
            return {"state": "none", "reason": "这台车在这条赛道上还没有完整的飞驰圈", "track_id": track_id}
        same = [r for r in rows if car_pi and r["car_pi"] == car_pi]
        best = same[0] if same else rows[0]
        pi_match = bool(same) or not car_pi or best["car_pi"] in (None, 0, car_pi)
        c = self._pb_lap
        if c is None or c[0] != best["id"]:
            lap = store.get_lap(best["id"])
            if lap is None or len(lap) < 2 or not lap.has("dist") or not lap.has("t"):
                return {"state": "none", "reason": "历史最快圈数据读不出来", "track_id": track_id}
            rd, rt_, integrated = _ref_arrays(lap)
            c = self._pb_lap = (best["id"], rd, rt_, integrated)
        return {"state": "ok", "track_id": track_id, "lap_id": best["id"], "lap_time": float(best["lap_time"]),
                "car_pi": best["car_pi"], "pi_match": pi_match, "rd": c[1], "rt": c[2], "integrated": c[3]}

    # ---------------- 状态 ----------------
    def _lan_ips(self) -> list[str]:
        if self._ips is None:
            self._ips = lan_ips()   # 第一次同步查；之后由 run() 在后台刷新
        return self._ips

    def _status_block(self) -> dict:
        now = time.monotonic()
        c = self._status_cache
        if c is not None and c[1] == self._ver and now - c[0] < STATUS_CACHE_S:
            return c[2]
        ver = self._ver
        hs = self.rt.hub.status()
        cfg = self.rt.cfg
        st = {
            "source": hs.get("source"),
            "packets": hs.get("packets"),
            "pps": _r(hs.get("pps"), 0),
            "since_last_packet": hs.get("since_last_packet"),
            "format": hs.get("format"),
            "format_label": hs.get("format_label"),
            "game": hs.get("game"),
            "game_name": hs.get("game_name"),
            "game_note": hs.get("game_note"),
            "has_dash": hs.get("has_dash"),
            "warnings": hs.get("warnings") or [],
            "recording": hs.get("recording"),
            "replay": getattr(self.rt, "replay_path", None),
            "udp_port": cfg.udp_port,
            "http_port": cfg.http_port,
            "lan_ips": self._lan_ips(),
            "clients": len(self.clients),
        }
        self._status_cache = (now, ver, st)
        return st

    # ------------------------------------------------------------------ 推送侧
    @staticmethod
    def client_hz(raw, default: float) -> float:
        """解析 ?hz=：限制在 MIN_HZ~MAX_HZ；没给 / 不合法就用默认值。"""
        try:
            hz = float(raw)
            if not math.isfinite(hz):
                raise ValueError
            return min(max(hz, MIN_HZ), MAX_HZ)
        except (TypeError, ValueError):
            return min(max(float(default or 20.0), 1.0), 60.0)

    async def serve_client(self, ws: WebSocket) -> None:
        """一个 WebSocket 客户端的完整生命周期：接受连接 → 按自己的频率推送 → 断开清理。"""
        hz = self.client_hz(ws.query_params.get("hz"), self.rt.cfg.live_hz)
        await ws.accept()
        client = _Client(ws, hz)
        self.clients.add(client)
        recv = asyncio.create_task(self._recv_until_close(ws))
        try:
            await self._send_loop(client, recv)
        except asyncio.TimeoutError:
            log.info("实时面板客户端发送超时（%.0f 秒），断开让它重连", SEND_TIMEOUT_S)
            with suppress(Exception):
                await asyncio.wait_for(ws.close(code=1013), 1.0)
        except Exception as e:   # 对方掉线时 send 会抛各种异常，都当作断开
            log.debug("实时面板客户端断开：%r", e)
        finally:
            self.clients.discard(client)
            recv.cancel()

    @staticmethod
    async def _recv_until_close(ws: WebSocket) -> None:
        """读掉客户端发来的消息（目前不用），收到断开就结束。"""
        with suppress(Exception):
            while True:
                msg = await ws.receive()
                if msg.get("type") == "websocket.disconnect":
                    return

    async def _send_loop(self, client: _Client, recv: asyncio.Task) -> None:
        ws, period = client.ws, 1.0 / client.hz
        last_key: tuple | None = None
        last_sent = -math.inf
        while not recv.done():
            tick = time.monotonic()
            key = (self.seq, self._ver)
            text = None
            if key != last_key:
                text = self.message_text()       # 有新帧 / 小结等内容变了（或刚连上）：发完整消息
                last_key = key
            elif tick - last_sent >= STATUS_INTERVAL_S:
                text = self.status_text()        # 没新数据：定时发状态
            if text is not None:
                await asyncio.wait_for(ws.send_text(text), SEND_TIMEOUT_S)
                client.sent += 1
                last_sent = time.monotonic()
            delay = period - (time.monotonic() - tick)
            await asyncio.wait({recv}, timeout=max(delay, 0.001))
