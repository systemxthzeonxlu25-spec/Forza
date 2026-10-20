"""动力总成（forza/powertrain.py + forza/api/powertrain.py）：用假数据验证反推的传动比、功率曲线、换挡点、改终传预测。"""
import math

import numpy as np
import pytest
from fastapi.testclient import TestClient

from forza import powertrain as pt
from forza.app import create_app
from forza.fake import write_fake_recording
from forza.fake.generator import _PWR_TAB, _RPM_TAB
from forza.hub import Hub
from forza.laps import LapDetector, LapSaver
from forza.replay import import_file
from forza.runtime import Runtime
from forza.storage import Store


def _import(store, path):
    hub = Hub()
    det = LapDetector()
    saver = LapSaver(store, "t", background=False)
    det.add_listener(saver)
    hub.subscribe(det.on_frame)
    import_file(path, hub)
    det.flush()


@pytest.fixture(scope="module")
def short_gears(tmp_path_factory):
    """齿比短的假数据（终传 4.10，6 挡在主直道末端撞断油），导入一次后各测试只读。"""
    d = tmp_path_factory.mktemp("pt")
    rec = d / "c.frec"
    truth = write_fake_recording(rec, "fm2023_dash", laps=5, seed=1)
    store = Store(d / "t.db")
    _import(store, rec)
    yield store, truth, d
    store.close()


def _true_shift(ratios, redline):
    """标准答案：用生成器的真实功率表和齿比，按“本挡功率 < 升挡后同车速下一挡功率”暴力扫。"""
    out = {}
    for g in range(1, len(ratios)):
        k = ratios[g] / ratios[g - 1]
        xs = np.arange(5600 / k, redline + 1, 1.0)
        d = np.interp(xs, _RPM_TAB, _PWR_TAB) - np.interp(xs * k, _RPM_TAB, _PWR_TAB)
        neg = np.nonzero(d < 0)[0]
        out[str(g)] = float(xs[neg[0]]) if len(neg) else float(redline)
    return out


def test_ratios_match_generator(short_gears):
    """反推出的综合传动比 = 生成器的齿比 × 终传。"""
    store, truth, _ = short_gears
    car = truth["car"]
    s = pt.summary(store, "fm2023", car["car_ordinal"])
    expect = {str(g + 1): r * car["final_drive"] for g, r in enumerate(car["gear_ratios"])}
    assert set(s["ratios"]) == set(expect)
    for g, r in expect.items():
        assert s["ratios"][g] == pytest.approx(r, rel=0.003)
    assert s["wheel_radius_m"] == pytest.approx(car["wheel_radius_m"], rel=0.01)
    assert s["redline_rpm"] == car["engine_max_rpm"] and s["car_pi"] == car["car_pi"]
    # 每挡红线车速（km/h）和生成器 truth 的 6 挡红线车速一致
    top = s["gears"][-1]
    assert top["redline_kmh"] == pytest.approx(car["top_gear_redline_kmh"], abs=1.0)


def test_power_curve_and_shift_points(short_gears):
    store, truth, _ = short_gears
    car = truth["car"]
    s = pt.summary(store, "fm2023", car["car_ordinal"])
    curve = s["power_curve"]
    assert curve and all(p["power_kw"] > 0 for p in curve)
    # 上包络 ≈ 真实功率曲线（全油门、按箱中心比）
    for p in curve:
        true_kw = car["power_kw"] * float(np.interp(p["rpm"], _RPM_TAB, _PWR_TAB))
        assert p["power_kw"] == pytest.approx(true_kw, rel=0.03)
    assert s["peak_power"]["power_kw"] == pytest.approx(car["power_kw"], rel=0.02)
    assert 6800 <= s["peak_power"]["rpm"] <= 7300
    # 换挡点：和真实曲线算出来的差 < 80 rpm；1、2 挡曲线不交叉 → 在红线换
    expect = _true_shift(car["gear_ratios"], car["engine_max_rpm"])
    assert set(s["shift_rpm"]) == set(expect)          # 6 挡是最高挡，不给升挡点
    for g, x in expect.items():
        assert s["shift_rpm"][g] == pytest.approx(x, abs=80), g
    assert s["shift_how"]["1"] == "redline" and s["shift_how"]["4"] == "cross"
    assert all(isinstance(k, str) for k in s["shift_rpm"])


def test_actual_upshifts(short_gears):
    """实际升挡转速：假司机到 7400 rpm（按车速）升挡；Data Out 的转速按驱动轮轮速，带一点打滑。"""
    store, truth, _ = short_gears
    car = truth["car"]
    s = pt.summary(store, "fm2023", car["car_ordinal"])
    r = car["gear_ratios"]
    for g, rpm in s["actual_upshift_rpm"].items():
        g = int(g)
        assert car["upshift_rpm"] - 50 <= rpm <= car["upshift_rpm"] * 1.06
        drop = s["actual_upshift_drop_rpm"][str(g)]
        assert drop == pytest.approx(rpm * r[g] / r[g - 1], rel=0.02)
        assert s["upshift_counts"][str(g)] >= 4


def test_ratio_cache_updates_when_gearing_changes(short_gears, tmp_path):
    """改终传不会改 PI：同一台车、同一个 PI 换成长齿比之后，缓存的传动比要跟着更新（差 > 2%）。"""
    _, truth, d = short_gears
    store = Store(tmp_path / "t.db")
    _import(store, d / "c.frec")
    clean = tmp_path / "clean.frec"
    t2 = write_fake_recording(clean, "fm2023_dash", laps=2, seed=2, issues=False)     # 终传 3.17，PI 一样
    assert t2["car"]["car_pi"] == truth["car"]["car_pi"]
    _import(store, clean)
    s = pt.summary(store, "fm2023", 2240)
    for g, r in enumerate(t2["car"]["gear_ratios"], start=1):
        assert s["ratios"][str(g)] == pytest.approx(r * t2["car"]["final_drive"], rel=0.003)
    store.close()


def test_update_ratios_rules(tmp_path):
    store = Store(tmp_path / "t.db")
    base = {"1": [12.0, 100], "2": [9.0, 100], "3": [7.0, 100], "4": [5.5, 100]}
    pt.update_ratios(store, "fh5", 1, 1, base)
    # 差 < 2%：不更新
    res = pt.update_ratios(store, "fh5", 1, 2, {"2": [9.1, 50]})
    assert res["updated"] == [] and pt._cached_ratios(store, "fh5", 1)[2]["ratio"] == 9.0
    # 开到的 2、3 挡同比例 ×1.1（改了终传）：没开到的 1、4 挡按同一比例推算
    res = pt.update_ratios(store, "fh5", 1, 3, {"2": [9.9, 50], "3": [7.7, 50]})
    c = pt._cached_ratios(store, "fh5", 1)
    assert res["factor"] == pytest.approx(1.1) and res["scaled"] == [1, 4]
    assert c[1]["ratio"] == pytest.approx(13.2) and c[1]["scaled"] == 1 and c[4]["ratio"] == pytest.approx(6.05)
    # 只有一个挡变了（改了这挡齿比）：别的没开到的挡标“可能过期”，不乱推
    res = pt.update_ratios(store, "fh5", 1, 4, {"3": [8.5, 50]})
    c = pt._cached_ratios(store, "fh5", 1)
    assert res["stale"] == [1, 2, 4] and c[2]["stale"] == 1 and c[3]["ratio"] == 8.5
    # 再开到 2 挡、数值一致：去掉“可能过期”
    pt.update_ratios(store, "fh5", 1, 5, {"2": [9.9, 50]})
    assert pt._cached_ratios(store, "fh5", 1)[2]["stale"] == 0
    store.close()


def test_shift_points_no_extrapolation():
    """功率数据没覆盖到的转速不外推：算不出就不给，写明原因。"""
    curve = [{"rpm": r, "power_kw": 100 + r / 100, "torque": 0} for r in range(3050, 6050, 100)]   # 一路上升
    shift, how, notes = pt.shift_points(curve, {1: 10.0, 2: 7.0}, 8000)
    assert shift == {} and any("没覆盖到红线" in n for n in notes)
    curve2 = curve + [{"rpm": r, "power_kw": 160 - (r - 6000) / 50, "torque": 0} for r in range(6050, 8050, 100)]
    shift, how, _ = pt.shift_points(curve2, {1: 10.0, 2: 7.0}, 8000)
    assert "1" in shift and how["1"] in ("cross", "redline")


def test_final_drive_prediction(short_gears):
    store, truth, _ = short_gears
    car = truth["car"]
    longer = pt.predict_final_drive(store, "fm2023", 2240, 4.10, 3.17)
    assert longer["verdict"] == "ok" and "不会再撞断油" in longer["message"]
    assert longer["limiter_s_now"] > 1.0
    g6 = longer["gears"][-1]
    assert g6["gear_ratio"] == pytest.approx(car["gear_ratios"][-1], rel=0.005)
    assert g6["redline_kmh_new"] == pytest.approx(car["top_gear_redline_kmh"] * 4.10 / 3.17, rel=0.01)
    shorter = pt.predict_final_drive(store, "fm2023", 2240, 4.10, 4.40)
    assert shorter["verdict"] == "limiter" and "会撞断油" in shorter["message"]
    with pytest.raises(ValueError):
        pt.predict_final_drive(store, "fm2023", 2240, 0, 3.5)


def test_api(cfg, short_gears):
    store, truth, d = short_gears
    rt = Runtime(cfg)
    try:
        with TestClient(create_app(rt)) as c:
            r = c.get("/api/powertrain", params={"game": "fm2023", "car_ordinal": 2240})
            assert r.status_code == 404 and r.json()["detail"]
            assert c.get("/api/powertrain").status_code == 404          # 没有实时车
            import_file(d / "c.frec", rt.hub)
            rt.detector.flush()
            rt.saver.wait()
            s = c.get("/api/powertrain").json()                          # 省略参数 = 实时那台车
            assert s["car_pi"] == 750 and s["source_laps"] == 5 and s["redline_rpm"] == 8000
            assert set(s["shift_rpm"]) == {"1", "2", "3", "4", "5"} and set(s["ratios"]) == {str(g) for g in range(1, 7)}
            for key in ("power_curve", "actual_upshift_rpm", "notes", "gears"):
                assert s[key]
            # 换了改装（PI 变了）：功率曲线按 PI 分开，这个 PI 还没有数据 → 没有换挡点，但传动比照样给
            s2 = c.get("/api/powertrain", params={"game": "fm2023", "car_ordinal": 2240, "car_pi": 800}).json()
            assert s2["shift_rpm"] == {} and s2["power_curve"] == [] and s2["ratios"]
            fd = c.get("/api/powertrain/final_drive",
                       params={"game": "fm2023", "car_ordinal": 2240, "final_now": 4.1, "final_new": 4.4}).json()
            assert fd["verdict"] == "limiter"
            assert c.get("/api/powertrain/final_drive",
                         params={"game": "fm2023", "car_ordinal": 2240, "final_now": -1, "final_new": 4}).status_code == 400
            assert c.post("/api/powertrain/rebuild", params={"game": "fm2023", "car_ordinal": 2240}).json()["laps"] == 5
    finally:
        rt.shutdown()
        rt.store.close()


def test_analyze_lap_awd_and_missing_channels(short_gears):
    """驱动方式未知时按四轮平均；缺通道时返回 None 不报错。"""
    store, truth, _ = short_gears
    lap = store.get_lap(store.list_laps(limit=1)[0]["id"])
    lap.meta["drivetrain"] = None
    a = pt.analyze_lap(lap)
    assert a and a["ratios"]
    lap.ch.pop("rpm")
    assert pt.analyze_lap(lap) is None
    assert math.isfinite(a["vmax"])
