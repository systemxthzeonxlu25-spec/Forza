"""车辆 / 赛道编号 → 名字（内置参考数据，见 forza/data/README.md）。

Data Out 只发编号（CarOrdinal / TrackOrdinal），没有名字。FM (2023) 有社区整理的编号表，
这里用它给新出现的车和赛道起默认名字；用户在网页上改过的名字优先，不会被覆盖。

    car_name(game, ordinal)    → "2014 Mazda #70 SpeedSource Lola B12/80" 或 None
    track_name(game, ordinal)  → "Brands Hatch Indy" 或 None
    track_length_m(game, ordinal) → 官方圈长（米）或 None

只认 fm2023：别的游戏的编号不保证和这份表一致，宁可显示编号也不显示错的名字。
"""
from __future__ import annotations

import csv
import logging
from functools import lru_cache
from pathlib import Path

log = logging.getLogger(__name__)

DATA_DIR = Path(__file__).with_name("data")
SUPPORTED_GAMES = ("fm2023",)


def _read_csv(name: str) -> list[list[str]]:
    """读一个内置 csv；文件缺失或格式坏了只记日志、返回空表（名字表只是锦上添花，不能拖垮启动）。"""
    try:
        with open(DATA_DIR / name, encoding="utf-8", newline="") as f:
            return [r for r in csv.reader(f) if r and r[0].strip().isdigit()]
    except OSError:
        log.warning("找不到内置名字表 %s，车 / 赛道只显示编号", name)
        return []


@lru_cache(maxsize=1)
def _cars() -> dict[int, str]:
    out = {}
    for r in _read_csv("fm2023_cars.csv"):
        year, make, model = (r + ["", "", ""])[1:4]
        out[int(r[0])] = " ".join(p.strip() for p in (year, make, model) if p.strip())
    return out


@lru_cache(maxsize=1)
def _tracks() -> dict[int, tuple[str, float | None]]:
    out = {}
    for r in _read_csv("fm2023_tracks.csv"):
        circuit, layout, km = (r + [""] * 6)[1], (r + [""] * 6)[4], (r + [""] * 6)[5]
        name = f"{circuit.strip()} {layout.strip()}".strip()
        try:
            length = float(km) * 1000.0
        except ValueError:
            length = None
        out[int(r[0])] = (name, length)
    return out


def car_name(game: str | None, ordinal: int | None) -> str | None:
    if game not in SUPPORTED_GAMES or ordinal is None:
        return None
    return _cars().get(int(ordinal)) or None


def track_name(game: str | None, ordinal: int | None) -> str | None:
    # 用 is None 判断：赛道 ID 0 是真实赛道（Laguna Seca 全赛道）
    if game not in SUPPORTED_GAMES or ordinal is None:
        return None
    t = _tracks().get(int(ordinal))
    return t[0] if t and t[0] else None


def track_length_m(game: str | None, ordinal: int | None) -> float | None:
    if game not in SUPPORTED_GAMES or ordinal is None:
        return None
    t = _tracks().get(int(ordinal))
    return t[1] if t else None
