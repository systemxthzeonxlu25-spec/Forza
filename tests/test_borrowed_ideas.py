"""从同类开源项目借鉴来的功能：FM2023 车名 / 赛道名（社区编号表）、单圈导出 CSV。"""
import csv
import io

import pytest
from fastapi.testclient import TestClient

from forza import names
from forza.app import create_app
from forza.runtime import Runtime
from forza.storage import Store


def test_name_tables_only_for_fm2023():
    assert names.car_name("fm2023", 2240) == "2014 Mazda #70 SpeedSource Lola B12/80"
    assert names.track_name("fm2023", 0) == "WeatherTech Raceway Laguna Seca Full Circuit"
    assert names.track_length_m("fm2023", 0) == pytest.approx(3600.0)
    # 别的游戏的编号不保证一致：宁可显示编号，也不显示错的名字
    assert names.car_name("fh5", 2240) is None and names.track_name("fm7", 0) is None
    assert names.car_name("fm2023", 999999) is None and names.car_name("fm2023", 0) is None


def test_new_cars_and_tracks_get_real_names(tmp_path):
    store = Store(tmp_path / "t.db")
    store.upsert_car("fm2023", 2240, 5, 750, 1, 8)
    store.upsert_car("fh5", 2240, 5, 750, 1, 8)
    cars = {c["game"]: c["name"] for c in store.list_cars()}
    assert cars == {"fm2023": "2014 Mazda #70 SpeedSource Lola B12/80", "fh5": None}
    tid = store.find_or_create_track(family="fm", game="fm2023", kind="circuit", track_ordinal=0,
                                     start_x=0, start_z=0, length_m=3600)
    assert store.get_track(tid)["name"] == "WeatherTech Raceway Laguna Seca Full Circuit"
    unknown = store.find_or_create_track(family="fm", game="fm2023", kind="circuit", track_ordinal=99999,
                                         start_x=0, start_z=0, length_m=3600)
    assert store.get_track(unknown)["name"] == "赛道 #99999"


def test_old_database_is_backfilled_but_user_names_kept(tmp_path):
    path = tmp_path / "old.db"
    store = Store(path)
    # 模拟老版本存下来的数据：车没名字、赛道是自动生成的“赛道 #编号”；另有一辆车、一条赛道用户改过名
    store.execute("INSERT INTO cars (game, car_ordinal) VALUES ('fm2023', 2240)")
    store.execute("INSERT INTO cars (game, car_ordinal, name) VALUES ('fm2023', 247, '我的车')")
    store.execute("INSERT INTO tracks (family, game, track_ordinal, kind, name) VALUES ('fm','fm2023',0,'circuit','赛道 #0')")
    store.execute("INSERT INTO tracks (family, game, track_ordinal, kind, name) VALUES ('fm','fm2023',1,'circuit','练车用')")
    store.close()
    store = Store(path)                                   # 重新打开：启动时补名字
    cars = {c["car_ordinal"]: c["name"] for c in store.list_cars()}
    assert cars == {2240: "2014 Mazda #70 SpeedSource Lola B12/80", 247: "我的车"}
    tracks = {t["track_ordinal"]: t["name"] for t in store.list_tracks()}
    assert tracks == {0: "WeatherTech Raceway Laguna Seca Full Circuit", 1: "练车用"}


def test_lap_csv_export(cfg, tmp_path):
    from forza.fake import write_fake_recording
    from forza.hub import Hub
    from forza.laps import LapDetector, LapSaver
    from forza.replay import import_file
    rec = tmp_path / "demo.frec"
    write_fake_recording(rec, "fm2023_dash", laps=2, seed=3)
    store = Store(cfg.resolve(cfg.db_path))
    hub, det = Hub(), LapDetector()
    det.add_listener(LapSaver(store, "import:demo", background=False))
    hub.subscribe(det.on_frame)
    import_file(rec, hub)
    det.flush()
    lap = store.list_laps()[0]
    n = store.get_lap(lap["id"])
    store.close()

    rt = Runtime(cfg)
    with TestClient(create_app(rt)) as c:
        r = c.get(f"/api/laps/{lap['id']}/csv")
        assert r.status_code == 200 and r.headers["content-type"].startswith("text/csv")
        assert f'lap_{lap["id"]}.csv' in r.headers["content-disposition"]
        rows = list(csv.reader(io.StringIO(r.text)))
        assert rows[0][:3] == ["t", "dist", "speed"] and len(rows) == len(n) + 1
        assert c.get("/api/laps/999999/csv").status_code == 404
    rt.shutdown()


def test_track_ordinal_zero_is_a_real_track(tmp_path):
    """FM2023 的赛道 ID 0（Laguna Seca 全赛道）是真实赛道：按 ID 认，不能退回去按起点位置 + 圈长猜。"""
    store = Store(tmp_path / "t.db")
    kw = dict(family="fm", game="fm2023", kind="circuit", track_ordinal=0, length_m=3600)
    a = store.find_or_create_track(start_x=0, start_z=0, **kw)
    b = store.find_or_create_track(start_x=5000, start_z=5000, **kw)     # 起点位置差很远也是同一条
    assert a == b and store.get_track(a)["track_ordinal"] == 0
    assert store.get_track(a)["provisional_length_m"] is None             # 有 ID 的赛道不记暂定圈长
