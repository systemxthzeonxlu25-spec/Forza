"""各代游戏的差异配置（GameProfile）。

包长只能区分“格式”，区分不了具体是哪一代：
- 324 字节：FH4 / FH5 / FH6 布局完全一样
- 232 字节（Sled）：FM7 和 FM (2023) 都能发
所以默认只能识别到“系列”，具体哪一代需要用户用 --game 指定（或在网页上给 session 打标签）。
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class GameProfile:
    key: str
    name: str
    family: str               # "fm" = Motorsport 系列，"fh" = Horizon 系列
    has_track_ordinal: bool   # 包里有没有赛道 ID
    has_tire_wear: bool       # 包里有没有轮胎磨损
    has_sprint: bool          # 有没有点对点冲刺赛（LapNumber 全程为 0）
    has_rewind: bool          # 有没有倒带
    localhost_ok: bool | None  # 能不能发往 127.0.0.1；None = 没查到确切说法
    note: str = ""


PROFILES: dict[str, GameProfile] = {
    "fm7": GameProfile(
        "fm7", "Forza Motorsport 7", "fm", False, False, False, True, False,
        "官方说明 FM7 不能发往 localhost，同机使用请填本机局域网 IP 并做 UWP 回环豁免（见 README）",
    ),
    "fm2023": GameProfile(
        "fm2023", "Forza Motorsport (2023)", "fm", True, True, False, True, True,
        "官方支持 localhost；Packet Format 请选 Car Dash",
    ),
    "fm": GameProfile(
        "fm", "Forza Motorsport（代数未知）", "fm", False, False, False, True, None,
        "Sled 格式无法区分 FM7 / FM2023，可用 --game 指定",
    ),
    "fh4": GameProfile("fh4", "Forza Horizon 4", "fh", False, False, True, True, None),
    "fh5": GameProfile("fh5", "Forza Horizon 5", "fh", False, False, True, True, None),
    "fh6": GameProfile(
        "fh6", "Forza Horizon 6", "fh", False, False, True, True, True,
        "官方要求避开 5200~5300 端口；发包频率跟随帧率；不在驾驶时停止发包",
    ),
    "fh": GameProfile(
        "fh", "Forza Horizon（4/5/6）", "fh", False, False, True, True, None,
        "324 字节包无法区分 FH4/5/6，可用 --game 指定",
    ),
}

# 每种包格式默认对应的游戏
DEFAULT_GAME_BY_FORMAT = {
    "sled": "fm",
    "fm7_dash": "fm7",
    "fm2023_dash": "fm2023",
    "fh": "fh",
    "fh_legacy": "fh",
}


def resolve_game(fmt_key: str, family: str, override: str | None) -> str:
    """根据包格式和用户指定的游戏，决定这一帧算哪个游戏。

    用户指定的游戏和包格式的系列对不上时（比如指定了 fh6 却收到 331 字节的 FM 包），以包为准。
    """
    if override and override in PROFILES and PROFILES[override].family == family:
        # Dash 格式能唯一确定 FM 的代数，不让 override 改错
        if fmt_key == "fm7_dash" and override == "fm2023":
            return "fm7"
        if fmt_key == "fm2023_dash" and override == "fm7":
            return "fm2023"
        return override
    return DEFAULT_GAME_BY_FORMAT.get(fmt_key, "fh" if family == "fh" else "fm")


def get_profile(key: str) -> GameProfile:
    return PROFILES.get(key) or PROFILES["fm"]
