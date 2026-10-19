"""两圈对比（forza/analysis.py）× 假数据：埋好的“T3 慢弯”必须被找出来，正常圈之间不能误报。"""
import json
import math

import pytest

from forza.analysis import (BRAKE_POINT_MIN_M, MIN_SPEED_MIN_KMH, compare)
from forza.fake import write_fake_recording
from forza.hub import Hub
from forza.laps import LapDetector, LapSaver
from forza.lapdata import LapData
from forza.replay import import_file
from forza.storage import Store


@pytest.fixture(scope="module")
def demo(tmp_path_factory):
    """默认种子的 5 圈假赛道（第 2 圈 T3 埋了慢弯），导入数据库后按圈号取出。"""
    d = tmp_path_factory.mktemp("analysis")
    rec = d / "demo.frec"
    truth = write_fake_recording(rec, "fm2023_dash", laps=5, seed=1)
    store = Store(d / "a.db")
    hub = Hub()
    det = LapDetector()
    saver = LapSaver(store, "test", background=False)
    det.add_listener(saver)
    hub.subscribe(det.on_frame)
    import_file(rec, hub)
    det.flush()
    laps = {m["lap_number"]: store.get_lap(m["id"]) for m in store.list_laps()}
    return truth, laps


def _seg(res, name):
    return next(s for s in res["segments"] if s["name"] == name)


def _walk(obj):
    """遍历结果里所有的数字，确认没有 NaN / inf。"""
    if isinstance(obj, dict):
        for v in obj.values():
            yield from _walk(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _walk(v)
    else:
        yield obj


def test_slow_corner_found(demo):
    truth, laps = demo
    sc = truth["issues"]["slow_corner"]
    res = compare(laps[3], laps[sc["lap_number"]])      # 参考 = 最快的第 3 圈，对比 = 第 2 圈
    # 丢时间最多的就是 T3
    assert res["ranking"][0]["name"] == sc["corner"] == "T3"
    t3 = _seg(res, "T3")
    assert t3["time_delta_s"] == pytest.approx(sc["time_loss_in_window_s"], abs=0.15)
    # 刹车点早约 25 m（正 = 对比圈刹得早）
    assert t3["brake_diff_m"] == pytest.approx(sc["brake_earlier_m"], abs=8)
    assert t3["brake_ref_m"] == pytest.approx(sc["ref_brake_point_m"], abs=8)
    assert t3["brake_other_m"] == pytest.approx(sc["slow_brake_point_m"], abs=8)
    # 弯心最低速更低，差值接近标准答案
    assert t3["min_speed_other_kmh"] < t3["min_speed_ref_kmh"]
    expect_dv = sc["slow_min_speed_kmh"] - sc["ref_min_speed_kmh"]
    assert t3["min_speed_delta_kmh"] == pytest.approx(expect_dv, abs=2.0)
    # 解释句里有这几条，并且是从数字来的
    assert t3["explanation"].startswith("T3：慢 ")
    assert f"刹车点早 {abs(t3['brake_diff_m']):.0f} m" in t3["explanation"]
    assert f"弯心最低速低 {abs(t3['min_speed_delta_kmh']):.0f} km/h" in t3["explanation"]
    # T3 这段的范围覆盖标准答案里的丢时间窗口的主体
    w0, w1 = sc["loss_window_m"]
    assert t3["seg_start_m"] <= w0 + 15 and t3["seg_end_m"] >= w1
    # 总时间差 ≈ 两圈圈速差
    lap_diff = laps[2].meta["lap_time"] - laps[3].meta["lap_time"]
    assert res["totals"]["delta_t_end_s"] == pytest.approx(lap_diff, abs=0.05)
    assert res["totals"]["lap_time_delta_s"] == pytest.approx(lap_diff, abs=0.002)
    # 各段首尾相接，时间差加起来 = 曲线终点的时间差（起点时间差几乎是 0）
    segs = res["segments"]
    assert segs[0]["seg_start_m"] == 0.0
    for p, q in zip(segs, segs[1:]):
        assert p["seg_end_m"] == q["seg_start_m"]
    assert sum(s["time_delta_s"] for s in segs) == pytest.approx(res["totals"]["delta_t_end_s"], abs=0.03)
    assert "T3" in res["totals"]["summary"]
    assert res["warnings"] == []


def test_normal_laps_no_false_alarm(demo):
    _, laps = demo
    for ref_n, oth_n in [(3, 1), (3, 4), (1, 4), (4, 1)]:
        res = compare(laps[ref_n], laps[oth_n])
        corners = [s for s in res["segments"] if s["kind"] == "corner"]
        assert len(corners) == 10
        worst = max(corners, key=lambda s: abs(s["time_delta_s"]))
        assert abs(worst["time_delta_s"]) <= 0.15, (ref_n, oth_n, worst["explanation"])
        # 正常圈之间刹车点 / 最低速差别都在阈值以内，解释句里不该冒出这些
        for s in corners:
            assert "刹车点" not in "".join(s["clauses"]) or abs(s["brake_diff_m"]) >= BRAKE_POINT_MIN_M
            assert abs(s["min_speed_delta_kmh"]) < MIN_SPEED_MIN_KMH + 1.0


def test_output_json_safe_and_shape(demo):
    _, laps = demo
    res = compare(laps[3], laps[2], step=2.0)
    js = json.dumps(res, allow_nan=False, ensure_ascii=False)      # 有 NaN 会直接抛错
    assert len(js) > 1000
    assert not any(isinstance(v, float) and not math.isfinite(v) for v in _walk(res))
    ch = res["channels"]
    n = len(ch["dist"])
    assert n == pytest.approx(res["length_m"] / 2.0, abs=2)
    for side in ("ref", "other"):
        assert set(ch[side]) == {"speed", "throttle", "brake", "steer", "gear", "rpm", "t"}
        assert all(len(v) == n for v in ch[side].values())
    assert len(ch["delta_t"]) == n and len(ch["loss_rate"]) == n
    assert max(ch["ref"]["speed"]) > 150          # km/h，不是 m/s
    assert all(float(g).is_integer() for g in ch["ref"]["gear"])
    # 赛道图：坐标、着色值、弯道标签
    m = res["map"]
    assert len(m["x"]) == len(m["z"]) == len(m["loss_rate"]) == len(m["dist"]) > 100
    assert [c["name"] for c in m["corners"]] == [f"T{i}" for i in range(1, 11)]
    # 赛道图上 T3 一带应该是“丢时间”（着色值为正）
    t3 = _seg(res, "T3")
    rates = [r for d, r in zip(m["dist"], m["loss_rate"]) if t3["seg_start_m"] <= d <= t3["apex_m"]]
    assert max(rates) > 0
    assert res["ref"]["id"] == laps[3].meta["id"] and res["other"]["id"] == laps[2].meta["id"]


def test_standing_start_lap(demo):
    _, laps = demo
    res = compare(laps[3], laps[0])
    assert res["ranking"][0]["kind"] == "start"
    assert "站立起步" in res["ranking"][0]["explanation"]


def test_mismatched_laps_warn(demo):
    _, laps = demo
    ref = laps[3]
    # 造一圈“别的赛道 / 别的车 / 短很多”的数据：只取前 70% 的采样
    k = int(len(ref) * 0.7)
    other = LapData(meta={**laps[1].meta, "id": 999, "track_id": 42, "track_name": "别的赛道",
                          "car_ordinal": 1, "is_complete": False},
                    ch={c: a[:k].copy() for c, a in laps[1].ch.items()})
    res = compare(ref, other)
    w = " ".join(res["warnings"])
    assert "不在同一条赛道" in w and "不是同一辆车" in w and "长度相差" in w and "不是完整的一圈" in w
    json.dumps(res, allow_nan=False)


def test_bad_input():
    tiny = LapData(meta={}, ch={"t": [0.0, 0.1], "dist": [0.0, 1.0]})
    with pytest.raises(ValueError):
        compare(tiny, tiny)
