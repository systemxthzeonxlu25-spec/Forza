"""实时推送：把最新一帧推给所有连着的浏览器（手机实时面板走 WebSocket /ws/live）。

设计要点
--------
- 收包循环（UDP / 回放）只调用 on_frame()：记下最新一帧、序号 +1，O(1)，不做任何 IO。
- 每个 WebSocket 客户端一个独立的发送协程（serve_client），按自己的频率取“最新的一条”发出去（只发最新、不排队）：
  慢的手机只会自己掉帧，不会堵住别的手机，更不会卡住收包；一次发送超过 SEND_TIMEOUT_S 秒就断开它，让它自己重连。
  （彻底失联的客户端，uvicorn 自带的 WebSocket 心跳也会在约 40 秒后把连接关掉。）
- 消息按帧序号缓存：同一帧只组装 / 序列化一次，多个客户端共用。
- 只有收到新帧才发 frame 消息；没有新数据时每 STATUS_INTERVAL_S 秒发一条轻量的 status 消息，
  面板据此显示“没有收到数据”和设置提示。
- 局域网 IP 查询（getaddrinfo 在有些 Windows 上会卡）放在 run() 里定期后台刷新，不进推送路径。

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
    "time": 45.678,              # 本圈已用时间 s
    "dist": 1234.5,              # 本圈已跑距离 m
    "last": 83.456 | null,       # 上一圈 s
    "best": 82.9 | null,         # 最快圈 s（游戏给的 BestLap，没有就用本场切圈结果）
    "ref_time": 82.9 | null,     # 实时差距的参照圈（本场最快完整圈）圈时
    "delta": -0.42 | null,       # 实时差距 s：按距离对齐，本圈用时 − 参照圈在同一距离的用时；负 = 更快
    "predicted": 82.48 | null,   # 预计圈时 = ref_time + delta
    "race_time": 300.1, "position": 3
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
定时 status 消息只有 type / seq / t / status 四个字段。
"""
from __future__ import annotations

import asyncio
import json
import logging
import math
import time
from contextlib import suppress
from dataclasses import dataclass

import numpy as np
from fastapi import WebSocket

from .frame import Frame
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
        self._msg_cache: tuple[int, str] | None = None      # (seq, 序列化好的 frame 消息)
        self._status_cache: tuple[float, dict] | None = None
        self._ips: list[str] | None = None
        self._ref_cache: tuple | None = None                 # 参照圈的 (key, dist 数组, t 数组, 是否积分距离)

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
        msg["status"] = self._status_block()
        return msg

    def message_text(self) -> str:
        """序列化好的 frame 消息，按帧序号缓存（同一帧多个客户端共用）。"""
        seq = self.seq
        c = self._msg_cache
        if c is not None and c[0] == seq and self.latest is not None:
            return c[1]
        text = self._dumps(self.snapshot())
        if self.latest is not None:
            self._msg_cache = (seq, text)
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
    def _lap_block(self, fr: Frame) -> dict | None:
        if not fr.has_dash:
            return None
        det = self.rt.detector
        ev, cur = det.event, det.cur
        st_kind = None
        if ev is not None:
            st_kind = "circuit" if ev.boundaries else ("sprint?" if ev.family == "fh" and ev.saw_position else "lap")
        in_lap = ev is not None and cur is not None and cur.lap_number == fr.lap_number
        lap_t = cur.last_t if in_lap else (fr.current_lap if fr.current_lap > 0 else None)

        # 本圈距离：两种口径都算，和参照圈用同一种
        d_field = fr.distance - cur.start_dist if in_lap else None
        d_int = cur.int_dist if in_lap else None

        ref = self._reference(det, fr) if in_lap and st_kind != "sprint?" else None
        delta = None
        dist = d_field
        ref_time = None
        if ref is not None:
            ref_time, rd, rt_, integrated = ref
            dist = d_int if integrated else d_field
            if dist is not None and lap_t is not None and len(rd) > 1 and 0 <= dist <= rd[-1] * DELTA_DIST_TOLERANCE:
                delta = float(lap_t - np.interp(dist, rd, rt_))
        best = _pos(fr.best_lap) or (_r(ev.best_time, 3) if ev and ev.best_time else None) or _r(ref_time, 3)
        return {
            "active": ev is not None,
            "kind": st_kind,
            "number": fr.lap_number,
            "time": _r(lap_t, 3),
            "dist": _r(dist, 1),
            "last": _pos(fr.last_lap),
            "best": best,
            "ref_time": _r(ref_time, 3),
            "delta": _r(delta, 3),
            "predicted": _r(ref_time + delta, 3) if delta is not None and ref_time is not None else None,
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
        if pend is not None and len(pend.rows) > 1:
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

    # ---------------- 状态 ----------------
    def _lan_ips(self) -> list[str]:
        if self._ips is None:
            self._ips = lan_ips()   # 第一次同步查；之后由 run() 在后台刷新
        return self._ips

    def _status_block(self) -> dict:
        now = time.monotonic()
        c = self._status_cache
        if c is not None and now - c[0] < STATUS_CACHE_S:
            return c[1]
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
        self._status_cache = (now, st)
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
        last_seq: int | None = None
        last_sent = -math.inf
        while not recv.done():
            tick = time.monotonic()
            seq = self.seq
            text = None
            if seq != last_seq:
                text = self.message_text()       # 有新帧（或刚连上）：发完整消息
                last_seq = seq
            elif tick - last_sent >= STATUS_INTERVAL_S:
                text = self.status_text()        # 没新数据：定时发状态
            if text is not None:
                await asyncio.wait_for(ws.send_text(text), SEND_TIMEOUT_S)
                client.sent += 1
                last_sent = time.monotonic()
            delay = period - (time.monotonic() - tick)
            await asyncio.wait({recv}, timeout=max(delay, 0.001))
