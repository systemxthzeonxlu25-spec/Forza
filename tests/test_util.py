"""公用小工具（forza/util.py）和切圈 / 实时面板共用的距离口径判断。"""
import math

import numpy as np

from forza.laps import should_use_integrated_distance
from forza.util import is_standing_first_lap, lap_has_flag, parse_flags, round_or_none


def test_parse_flags_accepts_every_storage_form():
    assert parse_flags('["first_lap", "rewind"]') == ["first_lap", "rewind"]    # 数据库里的 JSON 文本
    assert parse_flags(["a"]) == ["a"]                                          # meta 里的列表
    assert parse_flags({"b", "a"}) == ["a", "b"]                                # 切圈缓冲里的集合
    assert parse_flags(None) == [] and parse_flags("") == []
    assert parse_flags("{坏掉的 json") == []                                     # 坏数据不抛异常


def test_standing_first_lap_excludes_sprints():
    assert is_standing_first_lap({"flags": ["first_lap"], "kind": "lap"})
    assert not is_standing_first_lap({"flags": ["first_lap"], "kind": "sprint"})
    assert lap_has_flag({"flags": '["dist_integrated"]'}, "dist_integrated")


def test_round_or_none():
    assert round_or_none(1.23456, 2) == 1.23
    assert round_or_none(math.nan) is None and round_or_none(math.inf) is None
    assert round_or_none(None) is None and round_or_none("abc") is None


def test_integrated_distance_rule():
    d = np.linspace(0, 1000, 101)
    assert not should_use_integrated_distance(d, d)                 # 两种距离一致：用游戏给的
    assert should_use_integrated_distance(d * 1.5, d)               # 差 50%：改用积分
    bumpy = d.copy()
    bumpy[50:] -= 30                                                # 中途往回跳了 30 m（不单调）
    assert should_use_integrated_distance(bumpy, d)
    assert not should_use_integrated_distance(d[:3] * 5, d[:3])     # 积分距离太短（< 50 m）不判
