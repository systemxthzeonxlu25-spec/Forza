"""切圈逻辑：正常圈、暂停、倒带（圈内 / 跨线）、重开、最后一圈、冲刺赛、自由漫游、换车。"""
import math

import numpy as np
import pytest

from forza.laps import LapDetector, LapSaver
from forza.storage import Store
from tests.helpers import CircleStream


def _run(frames, det=None):
    det = det or LapDetector()
    laps = []
    det.add_listener(laps.append)
    for f in frames:
        det.on_frame(f)
    return det, laps


def test_basic_laps():
    s = CircleStream()
    det, laps = _run(s.run_dist(s.lap_len * 3.5))
    det.flush()
    assert [l.meta["lap_number"] for l in laps] == [0, 1, 2]
    for l in laps:
        assert l.meta["kind"] == "lap" and l.meta["is_complete"]
        assert l.meta["time_source"] == "game"
        assert l.meta["lap_time"] == pytest.approx(s.lap_len / s.v, abs=0.05)
        assert l.meta["distance_m"] == pytest.approx(s.lap_len, rel=0.01)
        assert np.all(np.diff(l["t"]) > 0) and np.all(np.diff(l["dist"]) >= 0)
    assert "first_lap" in laps[0].meta["flags"] and "first_lap" not in laps[1].meta["flags"]
    assert det.dropped_partial == 1  # 最后半圈


def test_pending_window():
    """过线后不会立刻落盘，新一圈跑过 12 s 才落。"""
    s = CircleStream()
    det, laps = _run(s.run_dist(s.lap_len + 40 * 5))   # 过线后 5 s
    assert laps == [] and det.pending is not None
    for f in s.run(10):
        det.on_frame(f)
    assert len(laps) == 1


def test_pause_does_not_break_lap():
    s = CircleStream()
    frames = s.run_dist(s.lap_len * 0.5) + s.paused(5) + s.run_dist(s.lap_len * 0.7)
    det, laps = _run(frames)
    det.flush()
    assert len(laps) == 1 and laps[0].meta["lap_time"] == pytest.approx(s.lap_len / s.v, abs=0.05)


def test_rewind_within_lap():
    s = CircleStream()
    frames = s.run_dist(s.lap_len * 0.5)
    s.rewind(4.0)
    frames += s.run_dist(s.lap_len * 0.5 + 40 * 4 + 40 * 13)
    det, laps = _run(frames)
    assert len(laps) == 1
    lap = laps[0]
    assert "rewind" in lap.meta["flags"]
    assert np.all(np.diff(lap["t"]) > 0), "倒带后的采样必须保持时间单调"
    assert lap.meta["distance_m"] == pytest.approx(s.lap_len, rel=0.01)
    assert lap.meta["lap_time"] == pytest.approx(s.lap_len / s.v, abs=0.05)


def test_rewind_across_line():
    s = CircleStream()
    frames = s.run_dist(s.lap_len + 40 * 2)   # 过线 2 s
    s.rewind(4.0)                              # 倒回到上一圈
    frames += s.run_dist(40 * 4 + 40 * 14)
    det, laps = _run(frames)
    assert [l.meta["lap_number"] for l in laps] == [0], "上一圈只能出现一次"
    assert "rewind" in laps[0].meta["flags"]
    assert np.all(np.diff(laps[0]["t"]) > 0)
    assert laps[0].meta["distance_m"] == pytest.approx(s.lap_len, rel=0.01)


def test_restart():
    s = CircleStream()
    frames = s.run_dist(s.lap_len * 1.5)
    s.reset()
    frames += s.run_dist(s.lap_len * 2.6)
    det, laps = _run(frames)
    det.flush()
    events = [l.meta["event_id"] for l in laps]
    assert [l.meta["lap_number"] for l in laps] == [0, 0, 1]
    assert events[0] != events[1] == events[2]
    assert laps[0].meta["end_reason"] == "restart" or laps[0].meta["is_complete"]


def test_final_lap_without_increment():
    """最后一圈冲线时游戏没让 LapNumber + 1：靠距离判完整，圈时自己量。"""
    s = CircleStream()
    frames = s.run_dist(s.lap_len * 2)        # 两圈，此时 lap_number = 2
    final = s.run_dist(s.lap_len + 2)
    for f in final:
        f.lap_number = 2                      # 游戏没加
    frames += final + s.paused(2)
    det, laps = _run(frames)
    det.flush()
    assert [l.meta["lap_number"] for l in laps] == [0, 1, 2]
    assert laps[2].meta["time_source"] == "measured"
    assert laps[2].meta["lap_time"] == pytest.approx(s.lap_len / s.v, abs=0.2)


def test_freeroam_no_laps():
    s = CircleStream(family="fh", fmt="fh", game="fh6", position=0)
    frames = s.run(30)
    for f in frames:
        f.race_time = 0.0
        f.current_lap = 0.0
        f.lap_number = 0
    det, laps = _run(frames)
    det.flush()
    assert laps == [] and det.event is None


def test_sled_frames_ignored():
    s = CircleStream()
    frames = s.run(10)
    for f in frames:
        f.has_dash = False
    det, laps = _run(frames)
    assert det.event is None


def test_car_change_ends_event():
    s = CircleStream()
    frames = s.run_dist(s.lap_len * 1.3)
    s2 = CircleStream(car=999)
    s2.rt = s.rt
    frames += s2.run(5)
    det, laps = _run(frames)
    assert len(laps) == 1 and det.event.car_ordinal == 999


def _sprint_run(s: CircleStream, meters: float, finish: bool):
    s.reset()
    frames = s.run_dist(meters)
    if finish:
        frames += s.paused(1)
    else:
        quit_frames = s.run(3)
        for f in quit_frames:          # 退出比赛回到自由漫游
            f.race_time = 0.0
            f.race_position = 0
            f.current_lap = 0.0
        frames += quit_frames
    return frames


def test_sprint_finish_and_abandon(tmp_path):
    store = Store(tmp_path / "t.db")
    det = LapDetector()
    saver = LapSaver(store, "test", background=False)
    det.add_listener(saver)
    s = CircleStream(radius=2000, family="fh", fmt="fh", game="fh5", position=3)
    route = 3000.0
    frames = _sprint_run(s, route, True) + _sprint_run(s, route * 0.5, False) + _sprint_run(s, route, True)
    for f in frames:
        det.on_frame(f)
    det.flush()
    laps = sorted(store.list_laps(), key=lambda l: l["id"])
    assert [l["kind"] for l in laps] == ["sprint"] * 3
    assert [l["is_complete"] for l in laps] == [True, False, True]
    assert "abandoned" in laps[1]["flags"]
    assert len({l["track_id"] for l in laps}) == 1, "同一起点应识别为同一条路线"
    assert laps[0]["lap_time"] == pytest.approx(route / s.v, abs=0.1)


def test_sprint_abandoned_first_then_full(tmp_path):
    """第一次就半路放弃：当时只能当跑完；后面跑了完整的，前面那条要改标成放弃。"""
    store = Store(tmp_path / "t.db")
    det = LapDetector()
    saver = LapSaver(store, "test", background=False)
    det.add_listener(saver)
    s = CircleStream(radius=2000, family="fh", fmt="fh", game="fh5", position=3)
    for f in _sprint_run(s, 1500, False) + _sprint_run(s, 3000, True):
        det.on_frame(f)
    det.flush()
    laps = sorted(store.list_laps(), key=lambda l: l["id"])
    assert [l["is_complete"] for l in laps] == [False, True]


def test_saver_store_roundtrip(tmp_path):
    store = Store(tmp_path / "t.db")
    det = LapDetector()
    saver = LapSaver(store, "test", background=True)
    det.add_listener(saver)
    s = CircleStream()
    for f in s.run_dist(s.lap_len * 3.5):
        det.on_frame(f)
    det.flush()
    saver.wait()
    metas = store.list_laps()
    assert len(metas) == 3
    assert len({m["track_id"] for m in metas}) == 1
    assert len({m["session_id"] for m in metas}) == 1
    lap = store.get_lap(metas[0]["id"])
    assert len(lap) > 100 and lap.meta["track_name"] == "赛道 #7"
    assert lap["tire_temp_fl"][0] == pytest.approx((180 - 32) * 5 / 9, abs=0.01)
    best = store.best_lap(metas[0]["track_id"])
    assert best["lap_time"] == min(m["lap_time"] for m in metas)
    assert store.list_cars()[0]["car_ordinal"] == 100


def test_track_fingerprint_without_ordinal(tmp_path):
    """没有 TrackOrdinal 的游戏：同一起点 + 同圈长 = 同一赛道；圈长差很多 = 不同布局。"""
    store = Store(tmp_path / "t.db")
    kw = dict(family="fh", game="fh5", kind="circuit", track_ordinal=None)
    a = store.find_or_create_track(start_x=100, start_z=200, length_m=3000, **kw)
    b = store.find_or_create_track(start_x=130, start_z=210, length_m=3050, **kw)
    c = store.find_or_create_track(start_x=100, start_z=200, length_m=4500, **kw)
    d = store.find_or_create_track(start_x=900, start_z=200, length_m=3000, **kw)
    assert a == b and len({a, c, d}) == 3
