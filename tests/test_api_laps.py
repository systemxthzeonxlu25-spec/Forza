"""圈速 / 对比 API（forza/api/laps.py）：列表、取数据、改、删、对比、默认参考圈、改名、错误码。"""
import shutil

import pytest
from fastapi.testclient import TestClient

from forza.app import create_app
from forza.fake import write_fake_recording
from forza.hub import Hub
from forza.laps import LapDetector, LapSaver
from forza.replay import import_file
from forza.runtime import Runtime
from forza.storage import Store


@pytest.fixture(scope="module")
def template_db(tmp_path_factory):
    """导入一次假数据（5 圈，第 2 圈 T3 慢，第 3 圈最快），之后每个测试复制一份来用。"""
    d = tmp_path_factory.mktemp("api_laps")
    rec = d / "demo.frec"
    truth = write_fake_recording(rec, "fm2023_dash", laps=5, seed=1)
    store = Store(d / "template.db")
    hub = Hub()
    det = LapDetector()
    saver = LapSaver(store, "import:demo.frec", background=False)
    det.add_listener(saver)
    hub.subscribe(det.on_frame)
    import_file(rec, hub)
    det.flush()
    store.close()
    return d / "template.db", truth


@pytest.fixture
def client(cfg, template_db):
    path, _ = template_db
    shutil.copy(path, cfg.resolve(cfg.db_path))
    rt = Runtime(cfg)
    with TestClient(create_app(rt)) as c:
        c.rt = rt
        yield c
    rt.store.close()


def _ids(client) -> dict[int, int]:
    """圈号 → 圈 id"""
    return {l["lap_number"]: l["id"] for l in client.get("/api/laps").json()}


def test_list_laps_with_best(client, template_db):
    _, truth = template_db
    laps = client.get("/api/laps").json()
    assert len(laps) == 5
    by_n = {l["lap_number"]: l for l in laps}
    for t in truth["laps"]:
        assert by_n[t["lap_number"]]["lap_time"] == pytest.approx(t["lap_time"], abs=0.002)
    assert [l["lap_number"] for l in laps if l["is_best"]] == [3]
    best = by_n[3]
    assert best["delta_to_best"] == 0.0
    assert by_n[2]["delta_to_best"] == pytest.approx(by_n[2]["lap_time"] - best["lap_time"], abs=1e-3)
    assert all(l["best_lap_id"] == best["id"] for l in laps)
    assert "first_lap" in by_n[0]["flags"]
    # 过滤
    tid, car, game = best["track_id"], best["car_ordinal"], best["game"]
    q = client.get(f"/api/laps?track_id={tid}&car_ordinal={car}&game={game}&complete_only=true").json()
    assert len(q) == 5
    assert client.get(f"/api/laps?track_id={tid + 100}").json() == []
    assert client.get(f"/api/laps?car_ordinal={car + 1}").json() == []
    assert client.get("/api/laps?limit=0").status_code == 400


def test_get_lap(client):
    ids = _ids(client)
    r = client.get(f"/api/laps/{ids[2]}?step=10&channels=speed,throttle")
    assert r.status_code == 200
    j = r.json()
    assert set(j["channels"]) == {"speed", "throttle"}
    assert j["meta"]["lap_number"] == 2
    full = client.get(f"/api/laps/{ids[2]}").json()
    assert len(full["channels"]["speed"]) > 4000
    assert len(j["channels"]["speed"]) == pytest.approx(len(full["channels"]["speed"]) / 10, abs=1)
    assert "pos_x" in full["available_channels"]
    assert client.get("/api/laps/9999").status_code == 404
    assert "不存在" in client.get("/api/laps/9999").json()["detail"]
    assert client.get(f"/api/laps/{ids[2]}?step=0").status_code == 400


def test_patch_and_delete_lap(client):
    ids = _ids(client)
    r = client.patch(f"/api/laps/{ids[2]}", json={"note": "  T3 刹早了 "})
    assert r.status_code == 200 and r.json()["note"] == "T3 刹早了"
    # 把最快圈标为不完整：最佳圈就换成次快的第 1 圈
    r = client.patch(f"/api/laps/{ids[3]}", json={"is_complete": False})
    assert r.status_code == 200 and r.json()["is_complete"] is False
    laps = {l["lap_number"]: l for l in client.get("/api/laps").json()}
    assert laps[1]["is_best"] and not laps[3]["is_best"]
    assert laps[3]["delta_to_best"] is None
    assert len(client.get("/api/laps?complete_only=true").json()) == 4
    assert client.patch(f"/api/laps/{ids[2]}", json={}).status_code == 400
    assert client.patch("/api/laps/9999", json={"note": "x"}).status_code == 404
    # 删除
    assert client.delete(f"/api/laps/{ids[4]}").json() == {"ok": True, "id": ids[4]}
    assert client.get(f"/api/laps/{ids[4]}").status_code == 404
    assert client.delete(f"/api/laps/{ids[4]}").status_code == 404
    assert len(client.get("/api/laps").json()) == 4


def test_compare_explicit(client):
    ids = _ids(client)
    r = client.get(f"/api/compare?a={ids[3]}&b={ids[2]}")
    assert r.status_code == 200
    j = r.json()
    assert j["ref"]["id"] == ids[3] and j["other"]["id"] == ids[2]
    assert j["auto_reference"] is False
    assert j["ranking"][0]["name"] == "T3"
    assert j["totals"]["lap_time_delta_s"] == pytest.approx(0.713, abs=0.002)
    assert j["map"] and len(j["map"]["corners"]) == 10
    # 反过来比：T3 变成赚时间最多的
    j2 = client.get(f"/api/compare?a={ids[2]}&b={ids[3]}&step=5").json()
    assert j2["ranking"][-1]["name"] == "T3" and j2["ranking"][-1]["time_delta_s"] < -0.3
    assert j2["step"] == 5.0


def test_compare_default_best(client):
    ids = _ids(client)
    # 不给 b：第 2 圈和最快的第 3 圈比
    j = client.get(f"/api/compare?a={ids[2]}").json()
    assert j["auto_reference"] is True
    assert j["ref"]["id"] == ids[3] and j["other"]["id"] == ids[2]
    # a 本身就是最快圈：和第二快的第 1 圈比
    j = client.get(f"/api/compare?a={ids[3]}").json()
    assert j["ref"]["id"] == ids[1] and j["other"]["id"] == ids[3]
    # 只剩一圈完整圈时没得比
    for n in (0, 1, 2, 4):
        client.patch(f"/api/laps/{ids[n]}", json={"is_complete": False})
    r = client.get(f"/api/compare?a={ids[3]}")
    assert r.status_code == 404 and "没有别的完整圈" in r.json()["detail"]


def test_compare_errors(client):
    ids = _ids(client)
    cases = [
        ("/api/compare", 400, "缺少参数 a"),
        ("/api/compare?a=abc", 400, "整数"),
        (f"/api/compare?a={ids[2]}&b=xyz", 400, "整数"),
        ("/api/compare?a=9999", 404, "不存在"),
        (f"/api/compare?a={ids[2]}&b=9999", 404, "不存在"),
        (f"/api/compare?a={ids[2]}&b={ids[2]}", 400, "同一圈"),
        (f"/api/compare?a={ids[2]}&b={ids[3]}&step=0", 400, "step"),
        (f"/api/compare?a={ids[2]}&b={ids[3]}&step=500", 400, "step"),
    ]
    for url, code, text in cases:
        r = client.get(url)
        assert r.status_code == code, url
        assert text in r.json()["detail"], url


def test_tracks_cars_sessions(client):
    tracks = client.get("/api/tracks").json()
    assert len(tracks) == 1 and tracks[0]["lap_count"] == 5
    tid = tracks[0]["id"]
    r = client.patch(f"/api/tracks/{tid}", json={"name": " Fake Ring "})
    assert r.status_code == 200 and r.json()["name"] == "Fake Ring"
    assert client.get("/api/laps").json()[0]["track_name"] == "Fake Ring"
    assert client.patch(f"/api/tracks/{tid}", json={"name": "   "}).status_code == 400
    assert client.patch(f"/api/tracks/{tid}", json={"name": "长" * 100}).status_code == 400
    assert client.patch("/api/tracks/999", json={"name": "x"}).status_code == 404

    cars = client.get("/api/cars").json()
    assert len(cars) == 1
    game, ordinal = cars[0]["game"], cars[0]["car_ordinal"]
    r = client.patch(f"/api/cars/{game}/{ordinal}", json={"name": "假 GT"})
    assert r.status_code == 200 and r.json()["name"] == "假 GT"
    assert client.get("/api/laps").json()[0]["car_name"] == "假 GT"
    assert client.patch(f"/api/cars/{game}/{ordinal + 1}", json={"name": "x"}).status_code == 404
    assert client.patch(f"/api/cars/{game}/{ordinal}", json={}).status_code == 422

    sessions = client.get("/api/sessions").json()
    assert len(sessions) == 1 and sessions[0]["track_id"] == tid


def test_laps_page_served(client):
    r = client.get("/laps.html")
    assert r.status_code == 200 and "laps.js" in r.text
    assert client.get("/laps.js").status_code == 200
    assert client.get("/laps.css").status_code == 200
