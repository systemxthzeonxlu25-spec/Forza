"""整条数据链路 × 假数据：各种包格式 / 场景下，切圈结果要和生成器的标准答案（truth）一致。"""
import pytest

from forza.corners import detect_corners
from forza.fake import write_fake_recording
from forza.hub import Hub
from forza.laps import LapDetector, LapSaver
from forza.replay import import_file
from forza.storage import Store


def _split(path, game=None):
    hub = Hub(game)
    det = LapDetector()
    laps = []
    det.add_listener(laps.append)
    hub.subscribe(det.on_frame)
    import_file(path, hub)
    det.flush()
    return hub, det, laps


@pytest.fixture(scope="module")
def circuit(tmp_path_factory):
    p = tmp_path_factory.mktemp("fake") / "c.frec"
    truth = write_fake_recording(p, "fm2023_dash", laps=4, seed=3)
    return p, truth


@pytest.mark.parametrize("fmt,size,family,game", [
    ("fm2023_dash", None, "fm", "fm2023"),
    ("fm7_dash", None, "fm", "fm7"),
    ("fh", None, "fh", "fh"),
    ("fh_legacy", None, "fh", "fh"),
    ("fh", 340, "fh", "fh"),            # 未知包长：自动探测成 Horizon 布局
    ("fm2023_dash", 352, "fm", "fm2023"),
])
def test_laps_match_truth_all_formats(tmp_path, fmt, size, family, game):
    p = tmp_path / "x.frec"
    truth = write_fake_recording(p, fmt, laps=3, seed=5, total_size=size)
    hub, det, laps = _split(p)
    assert hub.latest.family == family and hub.latest.game == game
    expect = [(l["lap_number"], l["lap_time"]) for l in truth["laps"]]
    got = [(l.meta["lap_number"], l.meta["lap_time"]) for l in laps]
    assert [g[0] for g in got] == [e[0] for e in expect]
    for (_, gt), (_, et) in zip(got, expect):
        assert gt == pytest.approx(et, abs=0.002)
    assert all(l.meta["time_source"] == "game" for l in laps)


def test_sled_has_no_laps(tmp_path):
    p = tmp_path / "s.frec"
    write_fake_recording(p, "sled", laps=2)
    hub, det, laps = _split(p)
    assert laps == [] and hub.latest.has_dash is False


def test_corners_match_truth(circuit):
    p, truth = circuit
    _, _, laps = _split(p)
    tc = truth["track"]["corners"]
    for lap in laps:
        cs = detect_corners(lap)
        assert len(cs) == len(tc)
        for c, t in zip(cs, tc):
            assert c["direction"] == t["direction"]
            assert c["apex_m"] == pytest.approx(t["apex_m"], abs=8)
            assert t["start_m"] - 30 <= c["start_m"] <= t["apex_m"] <= c["end_m"] <= t["end_m"] + 30


def test_rewind_matches_truth(tmp_path):
    p = tmp_path / "r.frec"
    truth = write_fake_recording(p, "fh", laps=3, rewind=True)
    _, _, laps = _split(p)
    assert [l.meta["lap_number"] for l in laps] == [l["lap_number"] for l in truth["laps"]]
    for l, t in zip(laps, truth["laps"]):
        assert l.meta["lap_time"] == pytest.approx(t["lap_time"], abs=0.002)
    assert "rewind" in laps[1].meta["flags"]


def test_restart(tmp_path):
    p = tmp_path / "rs.frec"
    truth = write_fake_recording(p, "fm2023_dash", laps=3, restart=True)
    _, _, laps = _split(p)
    events = sorted({l.meta["event_id"] for l in laps})
    assert len(events) == 2
    final = [l for l in laps if l.meta["event_id"] == events[-1]]
    assert [l.meta["lap_number"] for l in final] == [l["lap_number"] for l in truth["laps"]]
    for l, t in zip(final, truth["laps"]):
        assert l.meta["lap_time"] == pytest.approx(t["lap_time"], abs=0.002)


def test_sprint_with_store(tmp_path):
    p = tmp_path / "sp.frec"
    truth = write_fake_recording(p, "fh", "sprint")
    store = Store(tmp_path / "t.db")
    hub = Hub()
    det = LapDetector()
    saver = LapSaver(store, "t", background=False)
    det.add_listener(saver)
    hub.subscribe(det.on_frame)
    import_file(p, hub)
    det.flush()
    laps = sorted(store.list_laps(), key=lambda l: l["id"])
    runs = truth["runs"]
    assert [l["is_complete"] for l in laps] == [r["finished"] for r in runs]
    for l, r in zip(laps, runs):
        if r["finished"]:
            assert l["lap_time"] == pytest.approx(r["time_s"], abs=0.002)
    assert len({l["track_id"] for l in laps}) == 1


def test_freeroam(tmp_path):
    p = tmp_path / "fr.frec"
    write_fake_recording(p, "fh", "freeroam")
    _, det, laps = _split(p)
    assert laps == []


def test_fh_grid_start_behind_line_single_track(tmp_path):
    """FH 赛道赛从线后发车格静止起步（第一圈比飞驰圈长 > 4%）：不再拆成两条赛道，参考圈长取飞驰圈中位数。"""
    from forza.storage import TRACK_LENGTH_TOL
    p = tmp_path / "g.frec"
    truth = write_fake_recording(p, "fh", laps=4, seed=2, grid_offset_m=200)
    L = truth["track"]["length_m"]
    store = Store(tmp_path / "t.db")
    for _ in range(2):                         # 跑两次（两次会话），第二次的起步圈也要认对赛道
        hub = Hub()
        det = LapDetector()
        saver = LapSaver(store, "t", background=False)
        det.add_listener(saver)
        hub.subscribe(det.on_frame)
        import_file(p, hub)
        det.flush()
    laps = sorted(store.list_laps(), key=lambda l: l["id"])
    assert len(laps) == 8
    first = [l for l in laps if "first_lap" in l["flags"]]
    flying = [l for l in laps if "first_lap" not in l["flags"]]
    assert len(first) == 2
    # 这个用例确实能触发老问题：起步圈比飞驰圈长出 4% 以上
    assert (first[0]["distance_m"] - flying[0]["distance_m"]) / flying[0]["distance_m"] > TRACK_LENGTH_TOL
    assert first[0]["distance_m"] == pytest.approx(L + 200, abs=5)
    assert len({l["track_id"] for l in laps}) == 1 and len(store.list_tracks()) == 1
    assert store.list_tracks()[0]["ref_length_m"] == pytest.approx(L, abs=5)
    for l, t in zip(laps[:4], truth["laps"]):
        assert l["lap_time"] == pytest.approx(t["lap_time"], abs=0.002)
