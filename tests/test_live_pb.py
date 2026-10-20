"""实时差距对比历史最快圈（PB）：赛道只查不建、后台查库、FM2023 / 过线位置 / 冲刺赛三种匹配、PI 过滤、起步圈。"""
import threading

import pytest

from forza.live import match_track
from forza.packet import encode
from forza.runtime import Runtime
from forza.storage import Store
from tests.helpers import CircleStream
from tests.test_laps import _sprint_run


def _feed(rt, frames, fmt):
    for f in frames:
        rt.hub.feed(encode(f, fmt))


def _lap(rt) -> dict:
    """先组装一次（触发后台查库），等后台做完再取，避免依赖时序。"""
    rt.live.snapshot()
    rt.live.wait_idle()
    return rt.live.snapshot()["lap"]


def _with_pi(frames, pi):
    for f in frames:
        f.car_pi = pi
    return frames


def _history(cfg, stream_kw, fmt, laps=2.5, pi=800, speed=40.0):
    """第一场：用同一个数据库跑几圈存档，当作历史。"""
    rt = Runtime(cfg)
    s = CircleStream(speed=speed, **stream_kw)
    _feed(rt, _with_pi(s.run_dist(s.lap_len * laps), pi), fmt)
    rt.shutdown()
    rt.store.close()
    return s.lap_len / speed


def test_fm2023_pb_on_first_lap_by_track_ordinal(cfg):
    pb_time = _history(cfg, {}, "fm2023_dash")
    rt = Runtime(cfg)                                   # 新的一场：本场还没有完整圈
    try:
        s = CircleStream(speed=36.0)
        _feed(rt, _with_pi(s.run_dist(200), 800), "fm2023_dash")
        lap = _lap(rt)
        assert lap["delta"] is None and lap["ref_time"] is None     # 本场还没有参照
        assert lap["first_lap"] is True                             # 起步圈：前端标“仅供参考”
        pb = lap["pb"]
        assert pb["state"] == "ok" and pb["source"] == "db" and pb["pi_match"] is True
        assert pb["ref_time"] == pytest.approx(pb_time, abs=0.02)
        assert pb["delta"] == pytest.approx(200 / 36 - 200 / 40, abs=0.1)
        assert pb["predicted"] == pytest.approx(pb["ref_time"] + pb["delta"], abs=0.002)
        best = rt.store.query("SELECT id FROM laps WHERE lap_number=1")[0]["id"]
        assert pb["lap_id"] == best                         # 起步圈（lap_number=0）不拿来当 PB
        n_tracks = len(rt.store.list_tracks())
        _feed(rt, _with_pi(s.run_dist(s.lap_len), 800), "fm2023_dash")
        lap = _lap(rt)
        assert lap["first_lap"] is False and lap["pb"]["delta"] > 0
        assert len(rt.store.list_tracks()) == n_tracks      # 只查不建
    finally:
        rt.shutdown()


def test_pb_lookup_runs_off_the_event_loop(cfg):
    _history(cfg, {}, "fm2023_dash")
    rt = Runtime(cfg)
    try:
        s = CircleStream()
        _feed(rt, _with_pi(s.run_dist(150), 800), "fm2023_dash")
        calls = []
        for name in ("query", "get_lap", "list_laps"):
            orig = getattr(rt.store, name)

            def wrap(*a, _orig=orig, _name=name, **k):
                calls.append((_name, threading.current_thread().name))
                return _orig(*a, **k)
            setattr(rt.store, name, wrap)
        first = rt.live.snapshot()["lap"]["pb"]
        assert first["state"] == "loading"                  # 推送路径不等查库
        rt.live.wait_idle()
        assert calls and all(th.startswith("live-bg") for _, th in calls), calls
        n = len(calls)
        for _ in range(5):                                   # 之后每帧只读缓存
            _feed(rt, _with_pi(s.run(0.1), 800), "fm2023_dash")
            assert rt.live.snapshot()["lap"]["pb"]["state"] == "ok"
        rt.live.wait_idle()
        assert len(calls) == n
    finally:
        rt.shutdown()


def test_line_match_without_track_ordinal(cfg):
    """FM7 没有赛道 ID：第一次过线前不知道是哪条赛道，过线后按过线位置匹配到历史赛道。"""
    pb_time = _history(cfg, {"fmt": "fm7_dash", "game": "fm7", "track_ordinal": None}, "fm7_dash")
    rt = Runtime(cfg)
    try:
        s = CircleStream(fmt="fm7_dash", game="fm7", track_ordinal=None, speed=38.0)
        _feed(rt, _with_pi(s.run_dist(300), 800), "fm7_dash")
        lap = _lap(rt)
        assert lap["kind"] == "lap" and lap["pb"]["state"] == "none"
        _feed(rt, _with_pi(s.run_dist(s.lap_len - 300 + 250), 800), "fm7_dash")   # 过线后 250 m
        lap = _lap(rt)
        assert lap["kind"] == "circuit" and lap["first_lap"] is False
        pb = lap["pb"]
        assert pb["state"] == "ok" and pb["source"] == "db"
        assert pb["ref_time"] == pytest.approx(pb_time, abs=0.02)
        assert pb["delta"] == pytest.approx(250 / 38 - 250 / 40, abs=0.1)
        # 本场参照（慢的起步圈）和 PB 不一样
        assert lap["ref_time"] > pb["ref_time"]
    finally:
        rt.shutdown()


def test_sprint_uses_pb(cfg):
    kw = dict(radius=2000, family="fh", fmt="fh", game="fh5", position=3)
    rt = Runtime(cfg)
    s = CircleStream(**kw)
    _feed(rt, _with_pi(_sprint_run(s, 3000, True), 700), "fh")
    rt.shutdown()
    rt.store.close()

    rt = Runtime(cfg)
    try:
        s = CircleStream(speed=44.0, **kw)
        s.reset()
        _feed(rt, _with_pi(s.run_dist(400), 700), "fh")
        lap = _lap(rt)
        assert lap["kind"] == "sprint?" and lap["delta"] is None     # 冲刺赛没有本场参照
        pb = lap["pb"]
        assert pb["state"] == "ok" and pb["ref_time"] == pytest.approx(3000 / 40, abs=0.1)
        assert pb["delta"] == pytest.approx(400 / 44 - 400 / 40, abs=0.1) and pb["delta"] < 0
        assert lap["first_lap"] is False                              # 冲刺赛本来就静止起步，不算“起步圈”
    finally:
        rt.shutdown()


def test_pb_prefers_same_pi_and_flags_mismatch(cfg):
    _history(cfg, {}, "fm2023_dash", pi=800, speed=44.0)     # PI 800 跑得快
    _history(cfg, {}, "fm2023_dash", pi=700, speed=40.0)     # PI 700 慢一点
    rt = Runtime(cfg)
    try:
        s = CircleStream()
        _feed(rt, _with_pi(s.run_dist(200), 700), "fm2023_dash")
        pb = _lap(rt)["pb"]
        assert pb["pi_match"] is True and pb["car_pi"] == 700
        assert pb["ref_time"] == pytest.approx(s.lap_len / 40, abs=0.02)
    finally:
        rt.shutdown()
    rt = Runtime(cfg)
    try:
        s = CircleStream()
        _feed(rt, _with_pi(s.run_dist(200), 650), "fm2023_dash")    # 这个 PI 没跑过：用最快的，标出 PI 不同
        pb = _lap(rt)["pb"]
        assert pb["state"] == "ok" and pb["pi_match"] is False and pb["car_pi"] == 800
    finally:
        rt.shutdown()


def test_session_lap_faster_than_db_wins(cfg):
    _history(cfg, {}, "fm2023_dash", speed=36.0)
    rt = Runtime(cfg)
    try:
        s = CircleStream(speed=40.0)
        _feed(rt, _with_pi(s.run_dist(s.lap_len + 100), 800), "fm2023_dash")   # 刚过线，上一圈还挂起没落盘
        assert rt.detector.pending is not None
        pb = _lap(rt)["pb"]
        assert pb["source"] == "session" and pb["ref_time"] == pytest.approx(s.lap_len / 40, abs=0.02)
    finally:
        rt.shutdown()


def test_no_pb_on_unknown_track(cfg):
    rt = Runtime(cfg)
    try:
        s = CircleStream()
        _feed(rt, s.run_dist(200), "fm2023_dash")
        pb = _lap(rt)["pb"]
        assert pb["state"] == "none" and pb["reason"]
        assert rt.store.list_tracks() == []                 # 查的时候没有顺手建赛道
    finally:
        rt.shutdown()


def test_match_track_rules(tmp_path):
    st = Store(tmp_path / "m.db")
    kw = dict(family="fh", game="fh5", kind="circuit", track_ordinal=None)
    a = st.find_or_create_track(start_x=100, start_z=200, length_m=3000, **kw)
    b = st.find_or_create_track(start_x=110, start_z=205, length_m=4500, **kw)   # 共用起点线的长布局
    sp = st.find_or_create_track(family="fh", game="fh5", kind="sprint", track_ordinal=None,
                                 start_x=100, start_z=200, length_m=8000)
    fm = st.find_or_create_track(family="fm", game="fm2023", kind="circuit", track_ordinal=860,
                                 start_x=0, start_z=0, length_m=3862)
    m = dict(family="fh", kind="circuit", track_ordinal=None)
    assert match_track(st, x=105, z=200, length_m=3050, **m) == a            # 飞驰圈长：4% 以内
    assert match_track(st, x=105, z=200, length_m=4400, **m) == b
    assert match_track(st, x=105, z=200, length_m=3700, **m) is None          # 两个都不像：不认（存档时会另建）
    assert match_track(st, x=105, z=200, length_m=3300, length_tol=None, **m) == a   # 起步圈长：取最接近的
    assert match_track(st, x=900, z=200, length_m=3000, **m) is None          # 起点太远
    assert match_track(st, family="fh", kind="sprint", track_ordinal=None, x=120, z=190) == sp
    assert match_track(st, family="fm", kind="circuit", track_ordinal=860, x=None, z=None) == fm
    assert match_track(st, family="fm", kind="circuit", track_ordinal=861, x=None, z=None) is None
    assert len(st.list_tracks()) == 4                                         # 一条都没新建


def test_pb_does_not_flicker_after_new_lap_saved(cfg):
    """存了一圈（PB 版本号变了）、新结果还没查完时，先沿用上一次的 PB，不退回“本场最快”。"""
    _history(cfg, {}, "fm2023_dash", speed=44.0)
    rt = Runtime(cfg)
    try:
        s = CircleStream(speed=40.0)
        _feed(rt, _with_pi(s.run_dist(s.lap_len + 300), 800), "fm2023_dash")
        assert _lap(rt)["pb"]["source"] == "db"
        rt.live._pb_ver += 1                         # 模拟刚存好一圈
        rt.live._submit = lambda *a: True            # 后台还没来得及查
        pb = rt.live.snapshot()["lap"]["pb"]
        assert pb["source"] == "db" and pb["ref_time"] == pytest.approx(s.lap_len / 44, abs=0.02)
    finally:
        rt.shutdown()
