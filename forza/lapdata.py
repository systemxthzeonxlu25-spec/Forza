"""单圈数据 LapData：切圈器产出、存进 SQLite、给对比和诊断用的统一结构。

列式存储：ch["speed"] 是这一圈所有采样点的车速数组，所有通道等长、按时间排序。
需要加通道：在 CHANNELS 里加一行，并在 frame_sample() 里给出取值即可（老数据读出来缺这个通道而已）。
"""
from __future__ import annotations

import json
import struct
import zlib
from dataclasses import dataclass, field

import numpy as np

from .frame import Frame, WHEEL_NAMES


def _w(prefix: str) -> list[str]:
    return [f"{prefix}_{w}" for w in WHEEL_NAMES]


# 通道表（名字, 说明）。四轮通道后缀 _fl/_fr/_rl/_rr
CHANNELS: list[tuple[str, str]] = [
    ("t", "圈内时间 s"),
    ("dist", "圈内距离 m"),
    ("speed", "车速 m/s"),
    ("rpm", "转速"),
    ("gear", "档位（0 = 倒挡）"),
    ("throttle", "油门 0~1"),
    ("brake", "刹车 0~1"),
    ("clutch", "离合 0~1"),
    ("handbrake", "手刹 0~1"),
    ("steer", "方向 -1~1（+ = 右）"),
    ("g_lat", "横向 G（+ = 右）"),
    ("g_long", "纵向 G（+ = 加速）"),
    ("g_vert", "垂直 G"),
    ("yaw_rate", "偏航角速度 rad/s"),
    ("pos_x", "世界坐标 X m"),
    ("pos_y", "世界坐标 Y（高度）m"),
    ("pos_z", "世界坐标 Z m"),
    *[(n, "归一化悬挂行程 0=伸到底 1=压到底") for n in _w("susp")],
    *[(n, "悬挂行程 m") for n in _w("susp_m")],
    *[(n, "归一化滑移率 |x|>1 失去抓地") for n in _w("slip_ratio")],
    *[(n, "归一化侧偏角 |x|>1 失去抓地") for n in _w("slip_angle")],
    *[(n, "归一化综合滑移") for n in _w("comb_slip")],
    *[(n, "胎温 °C") for n in _w("tire_temp")],
    *[(n, "轮速 rad/s") for n in _w("wheel_speed")],
    *[(n, "压路肩 0/1") for n in _w("rumble")],
    *[(n, "轮胎磨损（仅 FM2023）") for n in _w("tire_wear")],
    ("fuel", "油量 0~1"),
    ("boost", "增压"),
    ("power", "功率 W"),
    ("torque", "扭矩 N·m"),
]
CHANNEL_NAMES: list[str] = [n for n, _ in CHANNELS]
TIRE_WEAR_CHANNELS = set(_w("tire_wear"))


def frame_sample(fr: Frame, t: float, dist: float) -> list[float]:
    """把一帧变成一行采样（顺序与 CHANNEL_NAMES 一致）。"""
    tc = fr.tire_temp_c()
    tw = fr.tire_wear or (float("nan"),) * 4
    return [
        t, dist, fr.speed, fr.rpm, fr.gear,
        fr.throttle_pct, fr.brake_pct, fr.clutch / 255.0, fr.handbrake / 255.0, fr.steer_norm,
        fr.g_lat, fr.g_long, fr.g_vert, fr.angvel_y,
        fr.pos_x, fr.pos_y, fr.pos_z,
        *fr.susp_norm, *fr.susp_m, *fr.slip_ratio, *fr.slip_angle, *fr.combined_slip,
        *tc, *fr.wheel_speed, *fr.rumble, *tw,
        fr.fuel, fr.boost, fr.power, fr.torque,
    ]


@dataclass
class LapData:
    """一圈（或一次冲刺赛）的完整数据。

    meta 常用字段：
        id, session_id, track_id, track_key, game, family, fmt,
        car_ordinal, car_class, car_pi, drivetrain, num_cylinders, engine_max_rpm, engine_idle_rpm,
        track_ordinal, lap_number, kind ("lap" | "sprint"), lap_time, time_source ("game" | "measured"),
        distance_m, max_speed, is_complete, flags (list[str]), start_x, start_z, created_at
    """
    meta: dict = field(default_factory=dict)
    ch: dict[str, np.ndarray] = field(default_factory=dict)

    def __len__(self) -> int:
        return len(self.ch["t"]) if "t" in self.ch else 0

    def __getitem__(self, name: str) -> np.ndarray:
        return self.ch[name]

    def has(self, name: str) -> bool:
        return name in self.ch

    @classmethod
    def from_rows(cls, meta: dict, rows: list[list[float]]) -> "LapData":
        arr = np.asarray(rows, dtype=np.float64).reshape(-1, len(CHANNEL_NAMES))
        ch = {name: arr[:, i].copy() for i, name in enumerate(CHANNEL_NAMES)}
        # 没有磨损数据的游戏，整列都是 NaN，直接去掉
        for name in TIRE_WEAR_CHANNELS:
            if np.isnan(ch[name]).all():
                del ch[name]
        return cls(meta=dict(meta), ch=ch)

    # ---------- 序列化：float32 + 字节重排 + zlib，一圈 60Hz/2 分钟大约几百 KB ----------
    def to_blob(self) -> bytes:
        names = list(self.ch.keys())
        n = len(self)
        mat = np.stack([self.ch[k].astype(np.float32) for k in names]) if names else np.zeros((0, 0), np.float32)
        raw = mat.tobytes()
        # 字节重排（shuffle）：把每个 float 的 4 个字节分组放，压缩率能好一截
        shuffled = np.frombuffer(raw, dtype=np.uint8).reshape(-1, 4).T.tobytes() if raw else b""
        header = json.dumps({"v": 1, "n": n, "channels": names}).encode()
        return b"LAP1" + struct.pack("<I", len(header)) + header + zlib.compress(shuffled, 6)

    @classmethod
    def from_blob(cls, meta: dict, blob: bytes) -> "LapData":
        if blob[:4] != b"LAP1":
            raise ValueError("不认识的圈数据格式")
        (hl,) = struct.unpack_from("<I", blob, 4)
        header = json.loads(blob[8:8 + hl])
        shuffled = zlib.decompress(blob[8 + hl:])
        names, n = header["channels"], header["n"]
        if not names:
            return cls(meta=dict(meta), ch={})
        raw = np.frombuffer(shuffled, dtype=np.uint8).reshape(4, -1).T.tobytes()
        mat = np.frombuffer(raw, dtype=np.float32).reshape(len(names), n).astype(np.float64)
        return cls(meta=dict(meta), ch={k: mat[i] for i, k in enumerate(names)})

    def to_json(self, step: int = 1, channels: list[str] | None = None) -> dict:
        """给前端用：可降采样、可只取部分通道。NaN 转成 None。"""
        names = channels or list(self.ch.keys())
        out = {}
        for k in names:
            if k in self.ch:
                a = np.round(self.ch[k][::max(1, step)].astype(np.float64), 4)
                vals = a.tolist()
                if not np.isfinite(a).all():
                    vals = [x if np.isfinite(x) else None for x in vals]
                out[k] = vals
        return {"meta": self.meta, "channels": out}
