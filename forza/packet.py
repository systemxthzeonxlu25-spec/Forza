"""Data Out 包格式注册表 + 解析/编码。

格式来源：官方 Data Out 文档（FM / FH6）+ 3 个独立开源实现交叉核对，全部小端序。

    包长  格式 key       说明
    232   sled          FM7 / FM2023 选了 Sled：只有物理量
    311   fm7_dash      FM7 Car Dash：Sled + Dash
    323   fh_legacy     早期 Horizon（少最后 1 个字节）：Sled + Horizon 12 字节 + Dash
    324   fh            FH4 / FH5 / FH6：Sled + Horizon 12 字节 + Dash + 1 字节尾巴
    331   fm2023_dash   FM (2023) Car Dash：Sled + Dash + 四轮磨损 + 赛道 ID

加新格式：照着下面的字段表拼一个 PacketFormat 加进 FORMATS 就行，解析器不用改。
遇到没见过的包长：PacketDecoder 会用 “Speed ≈ |速度向量|” 去试 FM / FH 两种 Dash 偏移，选对得上的那个。
"""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from struct import Struct

from .frame import Frame
from .profiles import resolve_game

log = logging.getLogger(__name__)


def _wheels(name: str, code: str) -> list[tuple[str, str]]:
    """四轮字段展开：name#0..name#3 对应 FL FR RL RR。"""
    return [(f"{name}#{i}", code) for i in range(4)]


# ---------------- 字段表：(Frame 字段名, struct 类型码) ----------------
# 类型码：i = S32，I = U32，f = F32，H = U16，B = U8，b = S8，x = 跳过 1 字节
SLED_FIELDS: list[tuple[str, str]] = [
    ("is_race_on", "i"),
    ("timestamp_ms", "I"),
    ("engine_max_rpm", "f"),
    ("engine_idle_rpm", "f"),
    ("rpm", "f"),
    ("accel_x", "f"), ("accel_y", "f"), ("accel_z", "f"),
    ("vel_x", "f"), ("vel_y", "f"), ("vel_z", "f"),
    ("angvel_x", "f"), ("angvel_y", "f"), ("angvel_z", "f"),
    ("yaw", "f"), ("pitch", "f"), ("roll", "f"),
    *_wheels("susp_norm", "f"),
    *_wheels("slip_ratio", "f"),
    *_wheels("wheel_speed", "f"),
    *_wheels("rumble", "i"),
    *_wheels("puddle", "f"),
    *_wheels("surface_rumble", "f"),
    *_wheels("slip_angle", "f"),
    *_wheels("combined_slip", "f"),
    *_wheels("susp_m", "f"),
    ("car_ordinal", "i"),
    ("car_class", "i"),
    ("car_pi", "i"),
    ("drivetrain", "i"),
    ("num_cylinders", "i"),
]

# Horizon 在 NumCylinders 和 PositionX 之间插入的 12 字节
HORIZON_FIELDS: list[tuple[str, str]] = [
    ("hzn_car_group", "i"),
    ("hzn_raw1", "I"),
    ("hzn_raw2", "I"),
]

DASH_FIELDS: list[tuple[str, str]] = [
    ("pos_x", "f"), ("pos_y", "f"), ("pos_z", "f"),
    ("speed", "f"),
    ("power", "f"),
    ("torque", "f"),
    *_wheels("tire_temp", "f"),
    ("boost", "f"),
    ("fuel", "f"),
    ("distance", "f"),
    ("best_lap", "f"),
    ("last_lap", "f"),
    ("current_lap", "f"),
    ("race_time", "f"),
    ("lap_number", "H"),
    ("race_position", "B"),
    ("throttle", "B"),
    ("brake", "B"),
    ("clutch", "B"),
    ("handbrake", "B"),
    ("gear", "B"),
    ("steer", "b"),
    ("driving_line", "b"),
    ("ai_brake_diff", "b"),
]

FM2023_FIELDS: list[tuple[str, str]] = [
    *_wheels("tire_wear", "f"),
    ("track_ordinal", "i"),
]

PAD1 = [("", "x")]  # Horizon 包末尾多出来的 1 个字节


@dataclass(frozen=True)
class PacketFormat:
    key: str
    label: str
    family: str                       # "fm" / "fh"
    fields: tuple[tuple[str, str], ...]
    dash_offset: int | None           # Dash 段起始偏移；Sled 为 None
    struct: Struct = field(init=False, repr=False, compare=False)

    def __post_init__(self):
        object.__setattr__(self, "struct", Struct("<" + "".join(c for _, c in self.fields)))

    @property
    def size(self) -> int:
        return self.struct.size

    @property
    def has_dash(self) -> bool:
        return self.dash_offset is not None

    @property
    def names(self) -> list[str]:
        """非填充字段的名字（和 unpack 结果一一对应）。"""
        return [n for n, c in self.fields if c != "x"]


def _fmt(key, label, family, parts, dash_offset) -> PacketFormat:
    fields: list[tuple[str, str]] = []
    for p in parts:
        fields += p
    return PacketFormat(key, label, family, tuple(fields), dash_offset)


FORMATS: dict[str, PacketFormat] = {
    f.key: f
    for f in [
        _fmt("sled", "Sled（FM7 / FM2023）", "fm", [SLED_FIELDS], None),
        _fmt("fm7_dash", "FM7 Car Dash", "fm", [SLED_FIELDS, DASH_FIELDS], 232),
        _fmt("fh_legacy", "Horizon（323 字节旧版）", "fh", [SLED_FIELDS, HORIZON_FIELDS, DASH_FIELDS], 244),
        _fmt("fh", "Horizon 4/5/6", "fh", [SLED_FIELDS, HORIZON_FIELDS, DASH_FIELDS, PAD1], 244),
        _fmt("fm2023_dash", "FM (2023) Car Dash", "fm", [SLED_FIELDS, DASH_FIELDS, FM2023_FIELDS], 232),
    ]
}

# 自检：包长必须和文档一致，改字段表改错了 import 时就会炸
_EXPECTED_SIZES = {"sled": 232, "fm7_dash": 311, "fh_legacy": 323, "fh": 324, "fm2023_dash": 331}
for _k, _n in _EXPECTED_SIZES.items():
    assert FORMATS[_k].size == _n, f"{_k} 包长应为 {_n}，实际 {FORMATS[_k].size}"

FORMAT_BY_SIZE: dict[int, PacketFormat] = {f.size: f for f in FORMATS.values()}

# FH6 官方文档里积水字段是整数、CarGroup 是无符号整数。
# 同样 324 字节的包分不出 FH4 / FH5 / FH6，所以只有用户明确指定 --game fh6 时才按这套类型解析。
# SmashableVelDiff / SmashableMass 仍按 U32 原样保留位模式（hzn_raw1 / hzn_raw2），不做物理换算。
_FH6_FORMAT = PacketFormat(
    "fh", "Horizon 6", "fh",
    tuple((name, "i" if name.startswith("puddle#") else
           "I" if name == "hzn_car_group" else code)
          for name, code in FORMATS["fh"].fields), 244,
)

# 未知包长只探测两个 Dash 起始偏移，能装下才试。
_GUESS_CANDIDATES = ["fh_legacy", "fm7_dash"]
# “Speed ≈ 速度向量模长”只能分辨 Dash 段从哪里开始，判断不了末尾多出来的字节是什么意思，
# 所以绝不拿多出来的字节去猜 FM2023 的轮胎磨损 / 赛道 ID。

def unpack(data: bytes, fmt: PacketFormat) -> dict:
    """按指定格式解包成 {字段名: 值}，四轮字段合并成 tuple。只读前 fmt.size 个字节。"""
    values = fmt.struct.unpack_from(data)
    out: dict = {}
    wheels: dict[str, list] = {}
    for name, val in zip(fmt.names, values):
        if "#" in name:
            base, _ = name.split("#")
            wheels.setdefault(base, []).append(val)
        else:
            out[name] = val
    for base, vals in wheels.items():
        out[base] = tuple(vals)
    return out


def build_frame(data: bytes, fmt: PacketFormat, *, game_override: str | None = None,
                recv_time: float = 0.0) -> Frame:
    wire_fmt = _FH6_FORMAT if fmt.key == "fh" and game_override == "fh6" else fmt
    d = unpack(data, wire_fmt)
    d["is_race_on"] = d["is_race_on"] == 1
    fr = Frame(**d)
    fr.fmt = fmt.key
    fr.family = fmt.family
    fr.size = len(data)
    fr.has_dash = fmt.has_dash
    fr.recv_time = recv_time
    fr.game = resolve_game(fmt.key, fmt.family, game_override)
    return fr


def encode(frame: Frame, fmt_key: str, total_size: int | None = None) -> bytes:
    """把 Frame 编码成指定格式的原始包（假数据生成器和测试用）。

    total_size 比格式长时在末尾补 0，用来模拟“未知包长”。
    """
    fmt = _FH6_FORMAT if fmt_key == "fh" and frame.game == "fh6" else FORMATS[fmt_key]
    vals = []
    for name, code in fmt.fields:
        if code == "x":
            continue
        if "#" in name:
            base, idx = name.split("#")
            seq = getattr(frame, base)
            v = seq[int(idx)] if seq is not None else 0
        else:
            v = getattr(frame, name)
            if v is None:
                v = 0
        if name == "is_race_on":
            v = 1 if v else 0
        if code in "iIHBb":
            v = int(v)
        vals.append(v)
    raw = fmt.struct.pack(*vals)
    if total_size and total_size > len(raw):
        raw += bytes(total_size - len(raw))
    return raw


# ---------------- 布局校验 ----------------

def dash_plausible(fr: Frame) -> bool | None:
    """用 “Speed 字段 ≈ 局部速度向量模长” 判断 Dash 段偏移是否解析对。

    返回 None 表示车几乎静止，判断不了。
    """
    vm = fr.velocity_mag
    sp = fr.speed
    if not (math.isfinite(vm) and math.isfinite(sp)):
        return False
    if vm < 2.0 and abs(sp) < 2.0:
        return None
    return abs(sp - vm) <= max(1.0, 0.10 * vm)


@dataclass
class _GuessState:
    votes: dict[str, int] = field(default_factory=dict)
    decided: str | None = None


class PacketDecoder:
    """有状态的解码器：按包长识别格式，未知包长自动探测，并持续校验布局。"""

    # 未知包长：累计这么多次“对得上”的投票才锁定布局
    GUESS_VOTES_NEEDED = 20
    # 已知包长：移动中的帧里对不上的比例超过这个值就报警
    VERIFY_MIN_SAMPLES = 200
    VERIFY_BAD_RATIO = 0.5

    def __init__(self, game_override: str | None = None):
        self.game_override = game_override
        self._guess: dict[int, _GuessState] = {}
        self._verify: dict[str, list[int]] = {}   # fmt -> [好, 坏]
        self.warnings: dict[str, str] = {}        # key -> 提示文字（给面板显示）
        self.too_short = 0
        self.invalid_core = 0

    def decode(self, data: bytes, recv_time: float = 0.0) -> Frame | None:
        n = len(data)
        fmt = FORMAT_BY_SIZE.get(n)
        if fmt is None:
            if n < FORMATS["sled"].size:
                self.too_short += 1
                self.warnings["short"] = f"收到 {n} 字节的包，比 Sled 格式(232)还短，已丢弃"
                return None
            fmt = self._guess_format(data)
        fr = build_frame(data, fmt, game_override=self.game_override, recv_time=recv_time)
        if n not in FORMAT_BY_SIZE and fmt.family == "fm":
            # 未知包长只能确认 FM 系列共有的前半段，具体是哪一代不知道，游戏按 Sled 口径识别
            fr.game = resolve_game("sled", "fm", self.game_override)
        if fmt.has_dash:
            critical = ("speed", "distance", "race_time", "current_lap", "last_lap", "best_lap",
                        "pos_x", "pos_y", "pos_z")
            if any(not math.isfinite(getattr(fr, name)) for name in critical):
                self.invalid_core += 1
                self.warnings["invalid_core"] = "车速、距离、位置或计时含 NaN/Infinity，已丢弃异常帧"
                return None
        if fmt.has_dash and n in FORMAT_BY_SIZE:
            self._verify_layout(fmt.key, fr)
        return fr

    def _guess_format(self, data: bytes) -> PacketFormat:
        n = len(data)
        st = self._guess.setdefault(n, _GuessState())
        if st.decided:
            return FORMATS[st.decided]
        cands = [FORMATS[k] for k in _GUESS_CANDIDATES if FORMATS[k].size <= n]
        if not cands:
            return FORMATS["sled"]
        # 逐个布局试解析，Speed 能和速度向量对上的投一票
        for f in cands:
            fr = build_frame(data, f)
            if dash_plausible(fr):
                st.votes[f.key] = st.votes.get(f.key, 0) + 1
        best = max(st.votes.items(), key=lambda kv: kv[1], default=(None, 0))
        runner_up = max((v for k, v in st.votes.items() if k != best[0]), default=0)
        if best[0] and best[1] >= self.GUESS_VOTES_NEEDED and best[1] - runner_up >= 5:
            st.decided = best[0]
            self.warnings[f"guess_{n}"] = (
                f"未知包长 {n} 字节，已按「{FORMATS[best[0]].label}」布局解析（Speed 校验 {best[1]} 次通过）"
            )
            log.warning(self.warnings[f"guess_{n}"])
            return FORMATS[best[0]]
        # 未确认之前只交出公共物理字段，不能把猜测的圈时/踏板写进数据库。
        self.warnings[f"guess_{n}"] = f"未知包长 {n} 字节，正在自动探测布局（车动起来才能判断）"
        return FORMATS["sled"]

    def _verify_layout(self, key: str, fr: Frame) -> None:
        ok = dash_plausible(fr)
        if ok is None:
            return
        cnt = self._verify.setdefault(key, [0, 0])
        cnt[0 if ok else 1] += 1
        total = cnt[0] + cnt[1]
        if total >= self.VERIFY_MIN_SAMPLES and total % 50 == 0:
            if cnt[1] / total > self.VERIFY_BAD_RATIO:
                msg = (f"格式 {key}：{cnt[1]}/{total} 帧的 Speed 和速度向量对不上，"
                       f"游戏可能改了包布局，数据可能不可信")
                if self.warnings.get(f"verify_{key}") != msg:
                    log.warning(msg)
                self.warnings[f"verify_{key}"] = msg
            else:
                self.warnings.pop(f"verify_{key}", None)
