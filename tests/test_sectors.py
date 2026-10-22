"""固定弯道 / 分段表、理论最佳圈、逐段稳定性（forza/sectors.py + forza/api/sectors.py）× 假数据。

默认种子的 5 圈假赛道：第 0 圈是站立起步（第一圈），第 2 圈 T3 埋了慢弯，第 3 圈最快。
"""
import json
import shutil

import numpy as np
import pytest
from fastapi.testclient import TestClient

from forza import sectors
from forza.app import create_app
from forza.corners import detect_corners
from forza.fake import write_fake_recording
from forza.hub import Hub
from forza.lapdata import LapData
from forza.laps import LapDetector, LapSaver
from forza.replay import import_file
from forza.runtime import Runtime
from forza.storage import Store


def _import(rec, db):
    store = Store(db)
    hub = Hub()
    det = LapDetector()
    saver = LapSaver(store, "test", background=False)
    det.add_listener(saver)
    hub.subscribe(det.on_frame)
    import_file(rec, hub)
    det.flush()
    return store


@pytest.fixture(scope="module")
def template(tmp_path_factory):
    d = tmp_path_factory.mktemp("sectors")
    rec = d / "demo.frec"
    truth = write_fake_recording(rec, "fm2023_dash", laps=5, seed=1)
    _import(rec, d / "t.db").close()
    return d / "t.db", truth


@pytest.fixture
def store(template, tmp_path):
    path, _ = template
    shutil.copy(path, tmp_path / "s.db")
    st = Store(tmp_path / "s.db")
    yield st
    st.close()


def _by_n(store) -> dict[int, dict]:
    return {m["lap_number"]: m for m in store.list_laps()}


def _track(store) -> int:
    return store.list_laps()[0]["track_id"]


# ---------------------------------------------------------------- 弯道表

def test_layout_from_fastest_clean_lap(store):
    laps = _by_n(store)
    tid = _track(store)
    assert sectors.get_layout(store, tid) is None            # 还没生成
    lay = sectors.ensure_layout(store, tid)
    assert lay["source_lap_id"] == laps[3]["id"]               # 最快的干净圈
    assert [c["code"] for c in lay["corners"]] == [f"T{i}" for i in range(1, 11)]
    assert all(c["name"] == c["code"] for c in lay["corners"])
    secs = lay["sectors"]
    assert secs[0]["start_frac"] == 0.0 and secs[-1]["end_frac"] == 1.0
    for p, q in zip(secs, secs[1:]):
        assert p["end_frac"] == q["start_frac"]
    # 分界在相邻两弯之间直道的中点
    cs = lay["corners"]
    for i in range(1, len(cs)):
        assert secs[i]["start_frac"] == pytest.approx((cs[i - 1]["end_frac"] + cs[i]["start_frac"]) / 2)
        assert cs[i - 1]["end_frac"] < secs[i]["start_frac"] < cs[i]["start_frac"]
    # 存下来了：再取一次是同一张（不会因为来了更快的圈就变）
    assert sectors.ensure_layout(store, tid)["created_at"] == lay["created_at"]


def test_layout_corners_mapped_to_lap(store):
    laps = _by_n(store)
    lap1 = store.get_lap(laps[1]["id"])
    assert sectors.layout_corners(store, lap1) is None        # 没保存的表 → None
    sectors.ensure_layout(store, _track(store))
    cs = sectors.layout_corners(store, lap1)
    own = detect_corners(lap1)
    assert len(cs) == len(own) == 10
    for c, o in zip(cs, own):
        for k in ("name", "index", "start_m", "apex_m", "end_m", "direction", "brake_start_m", "full_throttle_m",
                  "min_speed", "heading_change_deg", "min_radius_m", "label", "sector_start_m", "sector_end_m"):
            assert k in c
        assert c["name"] == o["name"] and c["direction"] == o["direction"]
        assert c["apex_m"] == pytest.approx(o["apex_m"], abs=8)
        assert c["min_speed"] == pytest.approx(o["min_speed"], abs=0.5)        # 用本圈数据算
        if o["brake_start_m"] is not None:
            assert c["brake_start_m"] == pytest.approx(o["brake_start_m"], abs=6)
        assert c["sector_start_m"] <= c["start_m"] < c["end_m"] <= c["sector_end_m"]
    # 没有 track_id 的圈 → None
    assert sectors.layout_corners(store, LapData(meta={}, ch=lap1.ch)) is None
    # 只跑了 70% 的圈：按比例套表会把弯放错位置 → None（调用方退回自己识别）
    k = int(len(lap1) * 0.7)
    short = LapData(meta=dict(lap1.meta), ch={c: a[:k].copy() for c, a in lap1.ch.items()})
    assert sectors.layout_corners(store, short) is None


def _corner_spots(res) -> set[tuple[str, str]]:
    """诊断结果里落在某个弯上的 (问题 id, 弯编号)。"""
    return {(f["id"], l["corner"]) for f in res["findings"] for l in (f.get("locations") or []) if l.get("corner")}


def test_layout_corners_grid_offset(tmp_path):
    """发车格在线后 150 m：第一圈的距离是整体平移，不是等比拉长。按弯心坐标映射后，
    第一圈每个弯的位置都要和本圈自己识别的一致，诊断结论也一样（不能把 T5 出弯的问题丢掉 / 挪到直道上）。"""
    from forza import tuning
    rec = tmp_path / "grid.frec"
    write_fake_recording(rec, "fm2023_dash", laps=3, seed=7, grid_offset_m=150)
    st = _import(rec, tmp_path / "g.db")
    metas = sorted(st.list_laps(), key=lambda m: m["id"])
    first = st.get_lap(metas[0]["id"])
    assert "first_lap" in first.meta["flags"]
    lay = sectors.ensure_layout(st, metas[0]["track_id"])
    assert lay["source_lap_id"] != first.meta["id"]
    assert abs(sectors._lap_length(first) - lay["length_m"]) == pytest.approx(150, abs=10)   # 在容差内，照样套表

    own = detect_corners(first)
    nopos = LapData(meta=first.meta, ch={k: v for k, v in first.ch.items() if k not in ("pos_x", "pos_z")})
    for lap in (first, nopos):                       # 有坐标：按坐标找弯心；没坐标：按“表 + 发车格偏移”预计
        cs = sectors.layout_corners(st, lap)
        assert [c["name"] for c in cs] == [o["name"] for o in own]
        for c, o in zip(cs, own):
            assert c["apex_m"] == pytest.approx(o["apex_m"], abs=8), c["name"]
            assert c["start_m"] == pytest.approx(o["start_m"], abs=15) and c["end_m"] == pytest.approx(o["end_m"], abs=15)
            assert c["sector_start_m"] <= c["start_m"] < c["apex_m"] < c["end_m"] <= c["sector_end_m"]
        assert cs[0]["sector_start_m"] == 0 and cs[-1]["sector_end_m"] == pytest.approx(sectors._lap_length(first))
    # 飞驰圈照样对得上
    fly = st.get_lap(metas[1]["id"])
    for c, o in zip(sectors.layout_corners(st, fly), detect_corners(fly)):
        assert c["apex_m"] == pytest.approx(o["apex_m"], abs=8)
    # 诊断：用固定表和本圈自己识别，落在各弯上的结论一样（埋好的 T5 出弯甩尾在里面）
    with_layout = tuning.diagnose(first, corners=sectors.layout_corners(st, first))
    assert with_layout["summary"]["corner_source"] == "layout"
    spots = _corner_spots(with_layout)
    assert ("oversteer_exit", "T5") in spots
    assert spots == _corner_spots(tuning.diagnose(first))
    # 第一圈不能拿来生成弯道表
    with pytest.raises(ValueError, match="第一圈"):
        sectors.rebuild_layout(st, metas[0]["track_id"], first.meta["id"])
    st.close()


# ---------------------------------------------------------------- 理论最佳 / 稳定性

def test_theoretical_best_and_consistency(store, template):
    _, truth = template
    laps = _by_n(store)
    res = sectors.track_sectors(store, _track(store))
    json.dumps(res, allow_nan=False, ensure_ascii=False)
    theo = res["theoretical_best"]
    fastest = min(t["lap_time"] for t in truth["laps"] if t["kind"] == "flying")
    assert theo["fastest_lap_id"] == laps[3]["id"]
    assert theo["fastest_lap_time"] == pytest.approx(fastest, abs=0.002)
    assert theo["time_s"] <= theo["fastest_lap_time"] and theo["gain_s"] >= 0
    assert theo["time_s"] == pytest.approx(sum(s["best_s"] for s in res["segments"]), abs=1e-6)
    assert [f["code"] for f in theo["from"]] == [f"T{i}" for i in range(1, 11)]
    # 第一圈（站立起步）不参与；其余 4 圈的各段用时加起来 = 圈时
    rows = {r["lap_number"]: r for r in res["laps"]}
    assert rows[0]["used"] is False and "第一圈" in rows[0]["reason"]
    for n in (1, 2, 3, 4):
        assert rows[n]["used"] and sum(rows[n]["times"]) == pytest.approx(rows[n]["lap_time"], abs=0.006)
    # 第 2 圈的 T3 段是 T3 里最慢的，也是整张表里离最快最远的一格
    t3 = next(s for s in res["segments"] if s["code"] == "T3")
    j = t3["index"]
    assert max(rows, key=lambda n: rows[n]["times"][j] if rows[n]["used"] else -1) == 2
    worst = max((rows[n]["delta_to_best"][k], n, k) for n in (1, 2, 3, 4) for k in range(10))
    assert worst[1:] == (2, j) and worst[0] == pytest.approx(0.52, abs=0.08)
    assert t3["best_lap_number"] != 2
    assert res["consistency"]["most_inconsistent"]["code"] == "T3"
    assert "T3 最不稳 ±" in res["consistency"]["summary"]
    assert all(s["n"] == 4 and s["outliers"] == 0 for s in res["segments"])
    assert t3["brake_std_m"] > 8                              # 第 2 圈早刹 25 m：刹车点离散度大
    others = [s["brake_std_m"] for s in res["segments"] if s["code"] != "T3" and s["brake_std_m"] is not None]
    assert max(others) < 4


def test_outlier_segment_excluded(store):
    """某段比中位数慢 20% 以上（多半是出事故）：这一格不计入最快、标准差。"""
    laps = _by_n(store)
    src = store.get_lap(laps[1]["id"])
    sectors.ensure_layout(store, _track(store))
    before = sectors.track_sectors(store, _track(store))
    t5 = next(s for s in before["segments"] if s["code"] == "T5")
    mid = (t5["start_m"] + t5["end_m"]) / 2
    ch = {k: v.copy() for k, v in src.ch.items()}
    ch["t"] = ch["t"] + np.where(ch["dist"] > mid, 4.0, 0.0)        # T5 段里卡了 4 s
    meta = {k: v for k, v in src.meta.items() if k not in ("id",)}
    meta["lap_time"] = src.meta["lap_time"] + 4.0
    new_id = store.save_lap(LapData(meta=meta, ch=ch))
    res = sectors.track_sectors(store, _track(store))
    row = next(r for r in res["laps"] if r["id"] == new_id)
    assert row["used"] and row["outlier"][t5["index"]] and sum(row["outlier"]) == 1
    seg = next(s for s in res["segments"] if s["code"] == "T5")
    assert seg["outliers"] == 1 and seg["n"] == 4
    assert seg["best_s"] == t5["best_s"] and seg["std_s"] == pytest.approx(t5["std_s"], abs=1e-6)
    assert res["theoretical_best"]["time_s"] == before["theoretical_best"]["time_s"]


def test_rename_and_rebuild_keep_names(store):
    tid = _track(store)
    laps = _by_n(store)
    sectors.ensure_layout(store, tid)
    lay = sectors.rename_corner(store, tid, "T5", "  发卡弯 ")
    assert next(c for c in lay["corners"] if c["code"] == "T5")["name"] == "发卡弯"
    assert next(s for s in lay["sectors"] if s["code"] == "T5")["name"] == "发卡弯"
    lap1 = store.get_lap(laps[1]["id"])
    assert next(c for c in sectors.layout_corners(store, lap1) if c["code"] == "T5")["label"] == "发卡弯"
    with pytest.raises(KeyError):
        sectors.rename_corner(store, tid, "T99", "x")
    # 换一圈重算：编号重新排，改过的名字按弯心坐标找回来
    lay2, kept = sectors.rebuild_layout(store, tid, laps[4]["id"])
    assert lay2["source_lap_id"] == laps[4]["id"]
    assert kept == [{"code": "T5", "name": "发卡弯", "from_code": "T5", "distance_m": pytest.approx(0, abs=10)}]
    assert next(c for c in lay2["corners"] if c["code"] == "T5")["name"] == "发卡弯"
    # 名字清空 = 恢复编号
    lay3 = sectors.rename_corner(store, tid, "T5", "")
    assert next(c for c in lay3["corners"] if c["code"] == "T5")["name"] == "T5"
    with pytest.raises(ValueError):
        sectors.rebuild_layout(store, tid, laps[0]["id"] + 999)


def test_no_clean_laps(store):
    tid = _track(store)
    for m in store.list_laps():
        if "first_lap" not in m["flags"]:
            store.update_lap(m["id"], is_complete=False)
    assert sectors.ensure_layout(store, tid) is None
    with pytest.raises(LookupError):
        sectors.track_sectors(store, tid)
    with pytest.raises(ValueError):
        sectors.rebuild_layout(store, tid)


def test_setup_filter(store):
    laps = _by_n(store)
    for n in (1, 2):
        store.execute("UPDATE laps SET meta = json_set(meta, '$.setup_id', 7, '$.setup_name', '软前防倾杆') WHERE id=?",
                      (laps[n]["id"],))
    res = sectors.track_sectors(store, _track(store), setup_id="7")
    assert [r["lap_number"] for r in res["laps"]] == [1, 2]
    assert res["laps"][0]["setup_name"] == "软前防倾杆"
    assert res["theoretical_best"]["fastest_lap_id"] == laps[1]["id"]
    res = sectors.track_sectors(store, _track(store), setup_id="none")
    assert sorted(r["lap_number"] for r in res["laps"]) == [0, 3, 4]


def test_sprint_sectors(tmp_path):
    """冲刺赛也一样：每次一条记录，站立起步不排除，放弃的那次排除。"""
    rec = tmp_path / "sprint.frec"
    write_fake_recording(rec, "fh", "sprint", seed=1)
    st = _import(rec, tmp_path / "sp.db")
    runs = st.list_laps()
    assert len(runs) == 3 and all(r["kind"] == "sprint" for r in runs)
    tid = runs[0]["track_id"]
    res = sectors.track_sectors(st, tid)
    used = [r for r in res["laps"] if r["used"]]
    assert len(used) == 2
    assert next(r for r in res["laps"] if not r["used"])["reason"] in ("不完整", "中途放弃")
    theo = res["theoretical_best"]
    assert theo["time_s"] <= min(r["lap_time"] for r in used) + 1e-6
    assert len(res["segments"]) >= 5
    for r in used:
        assert sum(r["times"]) == pytest.approx(r["lap_time"], abs=0.006)
    st.close()


# ---------------------------------------------------------------- API

@pytest.fixture
def client(cfg, template):
    path, _ = template
    shutil.copy(path, cfg.resolve(cfg.db_path))
    rt = Runtime(cfg)
    with TestClient(create_app(rt)) as c:
        yield c
    rt.store.close()


def test_api_sectors(client):
    laps = {l["lap_number"]: l for l in client.get("/api/laps").json()}
    tid, car, game = laps[3]["track_id"], laps[3]["car_ordinal"], laps[3]["game"]
    r = client.get(f"/api/sectors?track_id={tid}&car_ordinal={car}&game={game}")
    assert r.status_code == 200
    j = r.json()
    assert j["track_id"] == tid and j["car_ordinal"] == car
    assert len(j["segments"]) == 10 and j["layout"]["source_lap_id"] == laps[3]["id"]
    assert j["theoretical_best"]["time_s"] <= laps[3]["lap_time"]
    assert client.get(f"/api/sectors?track_id={tid}&car_ordinal={car + 1}").json()["laps"] == []
    assert client.get("/api/sectors?track_id=999").status_code == 404
    assert client.get("/api/sectors").status_code == 422
    assert client.get(f"/api/sectors/{tid}/layout").json()["corners"][0]["code"] == "T1"

    # 改名 → 对比页的逐弯分析用新名字（固定弯道表）
    r = client.patch(f"/api/sectors/{tid}/corners/T3", json={"name": "大右弯"})
    assert r.status_code == 200 and r.json()["corners"][2]["name"] == "大右弯"
    assert client.patch(f"/api/sectors/{tid}/corners/T42", json={"name": "x"}).status_code == 404
    assert client.patch(f"/api/sectors/{tid}/corners/T3", json={"name": "长" * 40}).status_code == 400
    cmp = client.get(f"/api/compare?a={laps[3]['id']}&b={laps[2]['id']}").json()
    assert cmp["corner_source"] == "layout"
    assert cmp["ranking"][0]["name"] == "T3" and cmp["ranking"][0]["explanation"].startswith("大右弯：慢 ")
    assert client.get(f"/api/sectors?track_id={tid}").json()["segments"][2]["name"] == "大右弯"

    # 重算：沿用改过的名字
    r = client.post(f"/api/sectors/{tid}/rebuild", json={"lap_id": laps[1]["id"]})
    assert r.status_code == 200
    assert r.json()["layout"]["source_lap_id"] == laps[1]["id"]
    assert r.json()["kept_names"][0]["name"] == "大右弯"
    assert client.post(f"/api/sectors/{tid}/rebuild").json()["layout"]["source_lap_id"] == laps[3]["id"]
    r = client.post(f"/api/sectors/{tid}/rebuild", json={"lap_id": 9999})
    assert r.status_code == 400 and "不存在" in r.json()["detail"]
    assert client.post("/api/sectors/999/rebuild").status_code == 404


def test_api_sectors_no_clean_laps(client):
    laps = client.get("/api/laps").json()
    for l in laps:
        client.patch(f"/api/laps/{l['id']}", json={"is_complete": False})
    r = client.get(f"/api/sectors?track_id={laps[0]['track_id']}")
    assert r.status_code == 404 and "干净的完整圈" in r.json()["detail"]
    # 没有固定表时对比照常（退回参考圈自己识别弯道）
    j = client.get(f"/api/compare?a={laps[0]['id']}&b={laps[1]['id']}").json()
    assert j["corner_source"] == "detected" and len(j["segments"]) == 11
