"""调校版本（forza/setups.py + forza/api/setups.py）：建版本、存圈打标签、版本统计、噪音判断、诊断对比、换改装提示。"""
import time

import pytest
from fastapi.testclient import TestClient

from forza import setups as su
from forza.app import create_app
from forza.fake import write_fake_recording
from forza.lapdata import CHANNEL_NAMES, LapData
from forza.laps import LapDetector, LapSaver
from forza.replay import import_file
from forza.runtime import Runtime
from forza.storage import Store
from tests.helpers import CircleStream

CAR = dict(game="fm2023", car_ordinal=2240)


def _tiny_lap(meta: dict) -> LapData:
    """只有几个采样点的圈（诊断会判“数据不足”），用来测统计。"""
    rows = [[float(i)] + [0.0] * (len(CHANNEL_NAMES) - 1) for i in range(3)]
    base = {"game": "fm2023", "family": "fm", "fmt": "fm2023_dash", "car_ordinal": 2240, "car_pi": 750,
            "kind": "lap", "is_complete": True, "flags": [], "track_id": 1, "distance_m": 3862.0,
            "lap_number": 1, "time_source": "game"}
    return LapData.from_rows({**base, **meta}, rows)


def _add_laps(store, setup_id, times, **meta):
    for t in times:
        store.save_lap(_tiny_lap({"lap_time": t, "setup_id": setup_id, **meta}))


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "t.db")
    yield s
    s.close()


# ---------------------------------------------------------------- 存储 / 打标签

def test_create_activate_and_current(store):
    assert su.current_setup(store, **CAR) is None
    a = su.create_setup(store, name="基准", note="出厂调校", car_pi=750, **CAR)
    assert a["is_current"] and su.current_setup(store, **CAR)["id"] == a["id"]
    b = su.create_setup(store, name="前防倾杆 +2", **CAR)
    assert su.current_setup(store, **CAR)["id"] == b["id"]
    assert su.get_setup(store, a["id"])["is_current"] is False
    su.activate(store, a["id"])                         # 换回以前的调校
    assert su.current_setup(store, **CAR)["id"] == a["id"]
    su.update_setup(store, b["id"], name="前防倾杆 +3", note="")
    b2 = su.get_setup(store, b["id"])
    assert b2["name"] == "前防倾杆 +3" and b2["note"] is None
    with pytest.raises(su.SetupError):
        su.create_setup(store, name="   ", **CAR)
    with pytest.raises(su.SetupError):
        su.update_setup(store, 999, name="x")


def test_tag_by_lap_end_time(store):
    """按这圈跑完的时刻找版本：跑完 → 进菜单 → 手机上记新版本，等这圈落盘时它仍算旧版本。"""
    a = su.create_setup(store, name="A", at=1000.0, **CAR)
    b = su.create_setup(store, name="B", at=2000.0, **CAR)
    m1 = {**CAR, "ended_epoch": 1500.0}
    m2 = {**CAR, "ended_epoch": 2500.0}
    m0 = {**CAR, "ended_epoch": 500.0}
    su.tag_lap_meta(store, m1)
    su.tag_lap_meta(store, m2)
    su.tag_lap_meta(store, m0)
    assert (m1["setup_id"], m1["setup_name"]) == (a["id"], "A")
    assert (m2["setup_id"], m2["setup_name"]) == (b["id"], "B")
    assert "setup_id" not in m0                         # 那时还没记过版本
    other = {"game": "fm2023", "car_ordinal": 1, "ended_epoch": 2500.0}
    su.tag_lap_meta(store, other)
    assert "setup_id" not in other                      # 别的车不受影响


def test_lapsaver_tags_meta_and_survives_listener_errors(tmp_path, caplog):
    """LapSaver 存圈时写 setup_id / setup_name；监听函数出错不算“存档失败”，也不影响别的监听。"""
    store = Store(tmp_path / "t.db")
    s0 = su.create_setup(store, name="基准", game="fm2023", car_ordinal=100)
    det = LapDetector()
    saver = LapSaver(store, "t", background=False)
    got = []

    def bad(lap):
        raise RuntimeError("监听炸了")
    saver.add_listener(bad)
    saver.add_listener(got.append)
    det.add_listener(saver)
    s = CircleStream()
    for f in s.run_dist(s.lap_len * 2.5):
        det.on_frame(f)
    det.flush()
    laps = store.list_laps()
    assert len(laps) == 2 and all(l["setup_id"] == s0["id"] and l["setup_name"] == "基准" for l in laps)
    assert len(got) == 2                                # 第二个监听照样收到
    assert "存档失败" not in caplog.text and "新圈监听出错" in caplog.text
    store.close()


# ---------------------------------------------------------------- 统计 / 噪音判断

def test_stats_exclude_first_lap_rewind_and_outliers(store):
    a = su.create_setup(store, name="A", **CAR)
    _add_laps(store, a["id"], [100.0, 100.2, 99.8, 100.1, 99.9])
    _add_laps(store, a["id"], [106.0])                              # 失误圈（慢 6%）
    _add_laps(store, a["id"], [104.0], flags=["first_lap"])         # 站立起步
    _add_laps(store, a["id"], [99.0], flags=["rewind"])             # 倒过带
    _add_laps(store, a["id"], [90.0], is_complete=False)            # 没跑完
    row = next(s for s in su.list_setups(store, **CAR) if s["id"] == a["id"])
    assert row["lap_count"] == 9
    assert row["flying_laps"] == 5 and row["outlier_laps"] == 1
    assert row["median_time"] == pytest.approx(100.0)
    assert row["std_time"] == pytest.approx(0.158, abs=1e-3)
    assert row["best_time"] == pytest.approx(99.0)                 # 最快完整圈（倒带圈也算完整圈）
    assert row["is_current"] and row["track_id"] == 1


def test_compare_verdicts_standard_error(store):
    a = su.create_setup(store, name="A", **CAR)
    b = su.create_setup(store, name="B", **CAR)
    c = su.create_setup(store, name="C", **CAR)
    d = su.create_setup(store, name="D", **CAR)
    _add_laps(store, a["id"], [100.0, 100.2, 99.8, 100.1, 99.9])
    _add_laps(store, b["id"], [99.5, 99.7, 99.3, 99.6, 99.4])           # 快 0.5 s
    _add_laps(store, c["id"], [99.95, 100.15, 99.75, 100.05, 99.85])    # 快 0.05 s：在噪音内
    _add_laps(store, d["id"], [99.0, 99.1])                             # 只有 2 圈
    r = su.compare_setups(store, a["id"], b["id"])
    # 阈值 = 2·sqrt(s1²/n1 + s2²/n2)，两组 s = 0.158、n = 5 → 0.2
    assert r["noise_threshold_s"] == pytest.approx(0.2, abs=1e-3)
    assert r["delta_median_s"] == pytest.approx(-0.5) and r["verdict"] == "faster" and r["enough_laps"]
    assert su.compare_setups(store, b["id"], a["id"])["verdict"] == "slower"
    rc = su.compare_setups(store, a["id"], c["id"])
    assert rc["verdict"] == "noise" and rc["enough_laps"] and "噪音" in rc["message"]
    rd = su.compare_setups(store, a["id"], d["id"])
    assert rd["verdict"] == "noise" and rd["enough_laps"] is False and "再跑几圈" in rd["message"]
    with pytest.raises(su.SetupError):
        su.compare_setups(store, a["id"], a["id"])


def test_upgrade_hints(store):
    a = su.create_setup(store, name="A", car_pi=750, engine_max_rpm=8000.0, num_cylinders=8, **CAR)
    assert su.car_hints(a, {"car_pi": 750, "engine_max_rpm": 8000.0, "num_cylinders": 8}) == []
    h = su.car_hints(a, {"car_pi": 780, "engine_max_rpm": 8500.0, "num_cylinders": 8})
    assert len(h) == 1 and "PI 750 → 780" in h[0] and "最高转速" in h[0] and "像是换了改装" in h[0]
    _add_laps(store, a["id"], [100.0, 100.1, 100.2])
    _add_laps(store, a["id"], [99.0, 99.1], car_pi=780)
    row = next(s for s in su.list_setups(store, **CAR) if s["id"] == a["id"])
    assert row["hint"] and "PI" in row["hint"]


# ---------------------------------------------------------------- 接口（假数据全链路）

@pytest.fixture(scope="module")
def recs(tmp_path_factory):
    d = tmp_path_factory.mktemp("setups")
    a = d / "a.frec"
    b = d / "b.frec"
    write_fake_recording(a, "fm2023_dash", laps=5, seed=1)                    # 齿比短：直道末端撞断油
    write_fake_recording(b, "fm2023_dash", laps=5, seed=2, issues=False)      # 干净调校
    return a, b


def _import(rt, path):
    import_file(path, rt.hub)
    rt.detector.flush()
    rt.saver.wait()


def test_api_full_flow(cfg, recs):
    rt = Runtime(cfg)
    try:
        with TestClient(create_app(rt)) as c:
            # 还没收到数据：current 也是 200（setup = null），新建要给车辆字段
            assert c.get("/api/setups/current").json() == {"setup": None, "car": None, "hints": []}
            r = c.post("/api/setups", json={"name": "基准"})
            assert r.status_code == 400 and "哪台车" in r.json()["detail"]
            assert c.post("/api/setups", json={"name": " ", **CAR}).status_code == 400
            a = c.post("/api/setups", json={"name": "基准（短齿比）", "note": "终传 4.10", **CAR}).json()
            _import(rt, recs[0])
            # 收到数据之后：不给车辆字段 = 实时那台车
            b = c.post("/api/setups", json={"name": "终传 3.17"}).json()
            assert (b["game"], b["car_ordinal"], b["car_pi"]) == ("fm2023", 2240, 750)
            _import(rt, recs[1])

            laps = c.get("/api/laps").json()
            assert {l["setup_name"] for l in laps} == {"基准（短齿比）", "终传 3.17"}
            assert sum(l["setup_id"] == a["id"] for l in laps) == 5

            cur = c.get("/api/setups/current").json()
            assert cur["setup"]["id"] == b["id"] and cur["car"] == {**CAR, "car_pi": 750} and cur["hints"] == []
            assert cur["setup"]["lap_count"] == 5 and cur["setup"]["flying_laps"] == 4

            lst = c.get("/api/setups", params=CAR).json()
            assert [s["id"] for s in lst] == [b["id"], a["id"]]
            assert [s["is_current"] for s in lst] == [True, False]
            for s in lst:
                assert s["lap_count"] == 5 and s["median_time"] and s["std_time"] is not None and s["best_time"]

            cmp_ = c.get("/api/setups/compare", params={"a": a["id"], "b": b["id"]}).json()
            assert cmp_["enough_laps"] and cmp_["a"]["n"] >= 3 and cmp_["b"]["n"] >= 3
            thr, delta = cmp_["noise_threshold_s"], cmp_["delta_median_s"]
            expect = "noise" if abs(delta) < thr else ("faster" if delta < 0 else "slower")
            assert cmp_["verdict"] == expect
            # 诊断对比：短齿比那组每圈都撞断油，干净调校那组没有
            by_id = {f["id"]: f for f in cmp_["findings_by_id"]}
            assert cmp_["diag_laps"]["a"] >= 3 and cmp_["diag_laps"]["b"] >= 3
            rev = next(f for fid, f in by_id.items() if "rev_limiter" in fid)
            assert rev["a"]["laps_with"] == cmp_["diag_laps"]["a"] and rev["b"]["laps_with"] == 0
            assert rev["a"]["total_s"] > 1.0 and rev["change"] == "fewer"

            # 改名、设为当前、错误码
            assert c.patch(f"/api/setups/{a['id']}", json={"name": "基准"}).json()["name"] == "基准"
            assert c.post(f"/api/setups/{a['id']}/activate").json()["is_current"] is True
            assert c.get("/api/setups/current", params=CAR).json()["setup"]["id"] == a["id"]
            assert c.patch("/api/setups/999", json={"name": "x"}).status_code == 404
            assert c.get("/api/setups/compare", params={"a": a["id"], "b": 999}).status_code == 404
            assert c.get("/api/setups/compare", params={"a": a["id"], "b": a["id"]}).status_code == 400
            assert c.get("/api/setups/current", params={"game": "fm2023"}).status_code == 400
    finally:
        rt.shutdown()
        rt.store.close()


def test_api_upgrade_hint_from_live_frame(cfg, recs):
    """实时帧的 PI 和记版本时不一样 → current 接口给“像是换了改装”的提示。"""
    rt = Runtime(cfg)
    try:
        with TestClient(create_app(rt)) as c:
            c.post("/api/setups", json={"name": "基准", "car_pi": 700, **CAR})
            _import(rt, recs[1])                      # 假数据的车 PI 750
            cur = c.get("/api/setups/current").json()
            assert cur["car"]["car_pi"] == 750
            assert cur["hints"] and "PI 700 → 750" in cur["hints"][0]
    finally:
        rt.shutdown()
        rt.store.close()


def test_activation_time_order_same_instant(store):
    """同一时刻连点两次“设为当前”：最后点的那个生效。"""
    a = su.create_setup(store, name="A", **CAR)
    b = su.create_setup(store, name="B", **CAR)
    t = time.time()
    su.activate(store, a["id"])
    su.activate(store, b["id"])
    su.activate(store, a["id"])
    assert su.current_setup(store, **CAR)["id"] == a["id"]
    assert su.setup_at(store, **CAR, at=t + 10)["id"] == a["id"]
