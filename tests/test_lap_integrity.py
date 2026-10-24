"""切圈器的刁钻状态转换：假数据生成器原有场景里没覆盖到的情况（中途接入、深度倒带、异步存档竞争等）。"""
import numpy as np
import pytest

from forza.laps import LapDetector
from tests.helpers import CircleStream


@pytest.mark.parametrize("horizon", [False, True])
def test_restart_near_finish_never_promotes_abandoned_run(horizon):
    s = CircleStream(**({"family": "fh", "game": "fh5", "fmt": "fh"} if horizon else {}))
    frames = s.run_dist(s.lap_len * (0.98 if horizon else 1.98))
    s.reset()
    frames += s.run(0.5)
    laps = []
    det = LapDetector(laps.append)
    for f in frames:
        det.on_frame(f)
    det.flush()
    assert len(laps) == 1
    if horizon:
        # FH 冲刺赛：重开前跑到 98% 与“跑完立刻重来”在时钟上无法区分，只能标成完赛推定，由保存方按路线长度裁决
        assert "finish_inferred" in laps[0].meta["flags"]
    else:
        assert laps[0].meta["end_reason"] != "restart"


def test_rewind_discards_maximum_from_removed_samples():
    s = CircleStream()
    laps = []
    det = LapDetector(laps.append)
    frames = s.run_dist(s.lap_len * 0.5)
    frames[-1].speed = 150
    for f in frames:
        det.on_frame(f)
    s.rewind(4)
    for f in s.run_dist(s.lap_len * 0.6 + 160):
        det.on_frame(f)
    det.flush()
    assert len(laps) == 1
    assert laps[0].meta["max_speed"] == pytest.approx(40)
    assert max(laps[0]["speed"]) == pytest.approx(40)


def test_missing_current_lap_keeps_first_second_of_samples():
    s = CircleStream(radius=2000, family="fh", fmt="fh", game="fh5")
    det = LapDetector()
    for f in s.run(3):
        f.current_lap = 0
        det.on_frame(f)
    times = np.array([row[0] for row in det.cur.rows])
    assert (times < 1).sum() >= 55
    assert np.max(np.diff(times)) < 0.04


def test_attach_mid_lap_does_not_produce_complete_reference():
    s = CircleStream()
    s.run_dist(s.lap_len * 0.5)  # 这段时间本工具还没开始收包
    laps = []
    det = LapDetector(laps.append)
    for f in s.run_dist(s.lap_len):
        det.on_frame(f)
    det.flush()
    assert len(laps) == 1
    assert not laps[0].meta["is_complete"]
    assert "partial_start" in laps[0].meta["flags"]
    assert det.laps_emitted == 1


def test_partial_pending_lap_is_not_live_reference(cfg):
    from forza.packet import encode
    from forza.runtime import Runtime
    rt = Runtime(cfg)
    try:
        s = CircleStream()
        s.run_dist(s.lap_len * 0.5)
        for f in s.run_dist(s.lap_len * 0.5 + 40):
            rt.hub.feed(encode(f, "fm2023_dash"))
        assert rt.detector.pending is not None
        assert rt.live.snapshot()["lap"]["ref_time"] is None
    finally:
        rt.shutdown()


def test_deep_rewind_invalidates_already_saved_pb_and_summary(cfg):
    from forza.packet import encode
    from forza.runtime import Runtime
    rt = Runtime(cfg)
    try:
        s = CircleStream()
        for f in s.run_dist(s.lap_len + 40 * 14):
            rt.hub.feed(encode(f, "fm2023_dash"))
        rt.saver.wait()
        rt.live.wait_idle()
        saved = rt.store.list_laps()[0]
        assert saved["is_complete"]
        assert rt.store.best_lap(saved["track_id"]) is not None
        assert rt.live.last_summary["id"] == saved["id"]
        s.rewind(16)  # 退得比 12 秒挂起窗口还远，倒回到起终点线之前
        for f in s.run(4):
            rt.hub.feed(encode(f, "fm2023_dash"))
        rt.saver.wait()
        rt.live.wait_idle()
        invalid = rt.store.get_lap_meta(saved["id"])
        assert not invalid["is_complete"]
        assert "rewound_after_save" in invalid["flags"]
        assert rt.store.best_lap(saved["track_id"]) is None
        assert all(s["id"] != saved["id"] for s in rt.live.recent_summaries())
    finally:
        rt.shutdown()


def test_deep_rewind_during_async_save_is_not_lost(cfg, monkeypatch):
    import threading
    from forza.packet import encode
    from forza.runtime import Runtime
    rt = Runtime(cfg)
    release = threading.Event()
    started = threading.Event()
    save = rt.saver._save
    def blocked_save(lap):
        started.set()
        assert release.wait(10)
        save(lap)
    monkeypatch.setattr(rt.saver, "_save", blocked_save)
    try:
        s = CircleStream()
        for f in s.run_dist(s.lap_len + 40 * 14):
            rt.hub.feed(encode(f, "fm2023_dash"))
        assert started.wait(5)
        # 写库（INSERT）被卡住：作废操作必须排在它后面
        s.rewind(16)
        for f in s.run(4):
            rt.hub.feed(encode(f, "fm2023_dash"))
        release.set()
        rt.saver.wait()
        saved = rt.store.list_laps()
        assert len(saved) == 1 and not saved[0]["is_complete"]
        assert "rewound_after_save" in saved[0]["flags"]
    finally:
        release.set()
        rt.shutdown()


def test_rewind_past_pending_lap_does_not_commit_erased_future_lap():
    s = CircleStream(radius=20)
    laps = []
    det = LapDetector(laps.append)
    for f in s.run_dist(s.lap_len * 2.1):
        det.on_frame(f)
    assert len(laps) == 1 and det.pending.lap_number == 1
    s.rewind(4.5)  # 退回第 0 圈：同时跨过挂起圈和已落盘的圈
    det.on_frame(s.step())
    det.flush()
    assert len(laps) == 1 and not laps[0].meta["is_complete"]


def test_invalidated_pb_is_not_returned_from_stale_cache(cfg):
    from forza.runtime import Runtime
    rt = Runtime(cfg)
    try:
        rt.live._pb_cache[("old",)] = {"state": "ok", "lap_id": 1}
        rt.live._pb_last[("old",)] = {"state": "ok", "lap_id": 1}
        rt.live.on_lap_invalidated({"id": 1})
        assert rt.live._pb_cache == {} and rt.live._pb_last == {}
    finally:
        rt.shutdown()
