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
    # A pause followed directly by a clock reset is indistinguishable from a
    # restart. Preserve that first run, but do not promote it to a complete PB.
    assert [l["is_complete"] for l in laps] == [False, False, True]
    assert "interrupted" in laps[0]["flags"]
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


# ---------------------------------------------------------------- 站立起步的第一圈 / 参考圈长

def _meta_lap(store, track_id, dist, flags=(), kind="lap", complete=True):
    from forza.lapdata import CHANNEL_NAMES, LapData
    rows = [[float(i)] + [0.0] * (len(CHANNEL_NAMES) - 1) for i in range(3)]
    lap = LapData.from_rows({"track_id": track_id, "game": "fh5", "family": "fh", "car_ordinal": 1, "kind": kind,
                             "lap_time": 90.0, "distance_m": dist, "is_complete": complete, "flags": list(flags)}, rows)
    return store.save_lap(lap)


def test_first_lap_ignores_length_and_is_not_reference(tmp_path):
    """第一圈从线后发车格起步，可能长出好几个百分点：匹配赛道时不看圈长，也不拿它当参考圈长。"""
    store = Store(tmp_path / "t.db")
    kw = dict(family="fh", game="fh5", kind="circuit", track_ordinal=None, start_x=100, start_z=200)
    t1 = store.find_or_create_track(length_m=3250, first_lap=True, **kw)        # 第一次跑：先存的是起步圈
    assert store.get_track(t1)["ref_length_m"] is None
    t2 = store.find_or_create_track(length_m=3000, **kw)                         # 飞驰圈：比起步圈短 7.7%
    assert t2 == t1
    store.set_track_length(t1, 3000)
    assert store.find_or_create_track(length_m=3250, first_lap=True, **kw) == t1   # 再来一次起步圈
    assert store.find_or_create_track(length_m=3250, **kw) != t1                   # 不是起步圈：长 8% 就是别的布局
    store.close()


def test_first_lap_picks_closest_layout(tmp_path):
    """共用起终点线的两个布局：起步圈取参考圈长最接近的那个。"""
    store = Store(tmp_path / "t.db")
    kw = dict(family="fh", game="fh5", kind="circuit", track_ordinal=None)
    short = store.find_or_create_track(start_x=100, start_z=200, length_m=3000, **kw)
    long_ = store.find_or_create_track(start_x=110, start_z=205, length_m=4500, **kw)
    assert short != long_
    assert store.find_or_create_track(start_x=105, start_z=200, length_m=4700, first_lap=True, **kw) == long_
    assert store.find_or_create_track(start_x=105, start_z=200, length_m=3200, first_lap=True, **kw) == short
    store.close()


def test_first_lap_of_other_layout_sharing_start_line(tmp_path):
    """共用起终点线的两个布局（3800 m / 2500 m）：短布局的起步圈不能并进长布局，
    也不能拿去当长布局的最快圈；只存过起步圈的赛道，别的布局的飞驰圈也不能不看圈长就并进来。"""
    store = Store(tmp_path / "t.db")
    kw = dict(family="fh", game="fh5", kind="circuit", track_ordinal=None, start_x=100, start_z=200)
    a = store.find_or_create_track(length_m=3800, **kw)
    _meta_lap(store, a, 3800)
    b_first = store.find_or_create_track(length_m=2600, first_lap=True, **kw)    # 比 A 短 32%：不是 A
    assert b_first != a
    assert store.get_track(b_first)["ref_length_m"] is None
    assert store.get_track(b_first)["provisional_length_m"] == 2600
    _meta_lap(store, b_first, 2600, flags=["first_lap"])
    assert store.find_or_create_track(length_m=2500, **kw) == b_first             # B 的飞驰圈：比起步圈短 3.8%
    assert store.best_lap(a)["distance_m"] == 3800                                # A 的最快圈没被起步圈污染
    # 起步圈最多比参考圈长 15%，不会比它短（超过 4%）
    assert store.find_or_create_track(length_m=3800 * 1.14, first_lap=True, **kw) == a
    assert store.find_or_create_track(length_m=3800 * 1.17, first_lap=True, **kw) not in (a, b_first)
    store.close()

    store = Store(tmp_path / "t2.db")
    t1 = store.find_or_create_track(length_m=2600, first_lap=True, **kw)          # 只跑了一圈起步圈就退出
    assert store.find_or_create_track(length_m=3800, **kw) != t1                  # 别的布局的飞驰圈：另建
    assert store.find_or_create_track(length_m=2450, first_lap=True, **kw) == t1  # 发车位置靠前的起步圈
    assert store.get_track(t1)["provisional_length_m"] == 2450                   # 暂定圈长取最短的起步圈
    assert store.find_or_create_track(length_m=2700, **kw) != t1                  # 比最短的起步圈还长 > 4%：不是
    assert store.find_or_create_track(length_m=2400, **kw) == t1
    store.close()


def test_old_db_gets_provisional_length(tmp_path):
    """老库（tracks 没有 provisional_length_m 列）：打开时补列，只有起步圈的赛道用最短的起步圈补上暂定圈长。"""
    import sqlite3
    path = tmp_path / "old.db"
    db = sqlite3.connect(path)
    db.executescript("""
        CREATE TABLE tracks (id INTEGER PRIMARY KEY AUTOINCREMENT, family TEXT NOT NULL, game TEXT,
            track_ordinal INTEGER, kind TEXT NOT NULL, name TEXT, start_x REAL, start_z REAL,
            ref_length_m REAL, created_at TEXT);
        CREATE TABLE laps (id INTEGER PRIMARY KEY AUTOINCREMENT, session_id INTEGER, track_id INTEGER, game TEXT,
            family TEXT, fmt TEXT, car_ordinal INTEGER, car_class INTEGER, car_pi INTEGER, lap_number INTEGER,
            kind TEXT, lap_time REAL, time_source TEXT, distance_m REAL, max_speed REAL, is_complete INTEGER,
            flags TEXT, meta TEXT, note TEXT, sample_count INTEGER, created_at TEXT, samples BLOB);
        INSERT INTO tracks (family, kind, start_x, start_z, ref_length_m) VALUES ('fh', 'circuit', 0, 0, 3000);
        INSERT INTO tracks (family, kind, start_x, start_z, ref_length_m) VALUES ('fh', 'circuit', 0, 0, NULL);
        INSERT INTO laps (track_id, kind, distance_m, is_complete, flags) VALUES (2, 'lap', 2700, 1, '["first_lap"]');
        INSERT INTO laps (track_id, kind, distance_m, is_complete, flags) VALUES (2, 'lap', 2650, 1, '["first_lap"]');
    """)
    db.commit()
    db.close()
    store = Store(path)
    assert store.get_track(1)["provisional_length_m"] is None
    assert store.get_track(2)["provisional_length_m"] == 2650
    kw = dict(family="fh", game="fh5", kind="circuit", track_ordinal=None, start_x=0, start_z=0)
    assert store.find_or_create_track(length_m=2550, **kw) == 2
    assert store.find_or_create_track(length_m=3050, **kw) == 1
    store.close()
    Store(path).close()                                                          # 再打开一次：不重复补列


def test_circuit_reference_length_is_flying_median(tmp_path):
    """赛道参考圈长 = 飞驰圈距离中位数（可变短，不是 MAX）；起步圈不参与；冲刺路线仍取最长。"""
    store = Store(tmp_path / "t.db")
    tid = store.find_or_create_track(family="fh", game="fh5", kind="circuit", track_ordinal=None,
                                     start_x=0, start_z=0, length_m=3100)
    assert store.get_track(tid)["ref_length_m"] == 3100
    _meta_lap(store, tid, 3300, flags=["first_lap"])
    assert store.refresh_circuit_length(tid) is None                     # 只有起步圈：不动
    for d in (3000, 3010, 2990, 3500):
        _meta_lap(store, tid, d)
    _meta_lap(store, tid, 2000, complete=False)                         # 没跑完的不算
    assert store.refresh_circuit_length(tid) == pytest.approx(3005.0)
    assert store.get_track(tid)["ref_length_m"] == pytest.approx(3005.0)   # 从 3100 变短了
    sp = store.find_or_create_track(family="fh", game="fh5", kind="sprint", track_ordinal=None,
                                    start_x=5000, start_z=0, length_m=8000)
    store.update_track_length(sp, 7000)
    store.update_track_length(sp, 9000)
    assert store.get_track(sp)["ref_length_m"] == 9000
    store.close()


def test_saver_listener_errors_are_isolated(tmp_path, caplog):
    """存好之后的监听函数出错：只记日志，不算“存档失败”，后面的监听照样收到。"""
    store = Store(tmp_path / "t.db")
    det = LapDetector()
    saver = LapSaver(store, "t", background=False)
    seen = []
    saver.add_listener(lambda lap: 1 / 0)
    saver.add_listener(seen.append)
    det.add_listener(saver)
    s = CircleStream()
    for f in s.run_dist(s.lap_len * 2.5):
        det.on_frame(f)
    det.flush()
    assert len(store.list_laps()) == 2 and len(seen) == 2
    assert "存档失败" not in caplog.text
    store.close()


def test_lap_meta_has_end_time():
    """每圈带 ended_epoch（跑完的时刻），调校版本按它归属。"""
    import time
    t0 = time.time()
    s = CircleStream()
    det, laps = _run(s.run_dist(s.lap_len * 2.5))
    det.flush()
    assert all(t0 - 1 <= l.meta["ended_epoch"] <= time.time() + 1 for l in laps)
