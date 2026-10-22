"""两圈对比（forza/analysis.py）× 假数据：埋好的“T3 慢弯”必须被找出来，正常圈之间不能误报。"""
import json
import math

import numpy as np
import pytest

from forza.analysis import BRAKE_POINT_MIN_M, MIN_SPEED_MIN_KMH, compare
from forza.fake import write_fake_recording
from forza.hub import Hub
from forza.lapdata import LapData
from forza.laps import LapDetector, LapSaver
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


# ---------------------------------------------------------------- 驾驶细节：滑行 / 松刹 / 带刹车入弯 / 收油 / 修方向 / 反打

NEW_WORDS = ("滑行", "松刹车", "带刹车", "修了", "反打", "收油", "收了")


def _modified(lap: LapData, fn) -> LapData:
    """复制一圈，按 fn(ch) 改通道（只改输入，时间不变 → 各段时间差 ≈ 0，只看解释句）。"""
    ch = {k: v.copy() for k, v in lap.ch.items()}
    fn(ch)
    return LapData(meta={**lap.meta, "id": 900}, ch=ch)


def _win(ch, lo, hi):
    return (ch["dist"] >= lo) & (ch["dist"] <= hi)


def test_new_metrics_quiet_on_normal_laps(demo):
    """正常圈之间（同一个司机模型、只有随机波动）不能冒出滑行、松刹、修方向这些句子。"""
    _, laps = demo
    for ref_n, oth_n in [(3, 1), (3, 4), (1, 4), (4, 1), (1, 3), (4, 3)]:
        res = compare(laps[ref_n], laps[oth_n])
        for s in res["segments"]:
            for c in s["clauses"]:
                assert not any(w in c for w in NEW_WORDS), (ref_n, oth_n, s["name"], c)
            if s["kind"] == "corner":
                for k in ("coast_pre", "coast_mid", "coast_exit"):
                    assert abs(s[f"{k}_ref_s"] - s[f"{k}_other_s"]) < 0.2
                assert abs(s["trail_ref_m"] - s["trail_other_m"]) < 10


def test_slow_corner_mentions_coasting(demo):
    """第 2 圈 T3：刹得早、刹得轻，松开刹车后两脚都不踩、一路滑到弯心（假数据就是这么开的）。"""
    _, laps = demo
    t3 = _seg(compare(laps[3], laps[2]), "T3")
    assert t3["coast_mid_other_s"] > 1.0 and t3["coast_mid_ref_s"] < 0.2
    assert "松开刹车到弯心之间滑行" in t3["explanation"]
    assert "最大减速" in t3["explanation"]          # 踏板轻了，实际减速 G 也小了
    assert t3["decel_other_g"] < t3["decel_ref_g"]


def test_synthetic_driving_faults(demo):
    """在最快圈上人为制造各种毛病，解释句要逐条说出来。"""
    _, laps = demo
    ref = laps[3]
    base = compare(ref, ref)
    seg0 = {s["name"]: s for s in base["segments"]}
    b4 = seg0["T4"]["brake_ref_m"]
    t6_apex = seg0["T6"]["apex_m"]
    t10_apex = seg0["T10"]["apex_m"]
    t8 = seg0["T8"]

    def faults(ch):
        d = ch["dist"]
        # T4：刹车点前 35 m 松油滑行
        m = _win(ch, b4 - 35, b4 - 1)
        ch["throttle"][m] = 0.0
        # T1：转向以后立刻松掉刹车（不带刹车入弯）
        m = _win(ch, 600, 700) & (np.abs(ch["steer"]) >= 0.08)
        ch["brake"][m] = 0.0
        # T6：出弯两次收油
        m = _win(ch, t6_apex + 5, t6_apex + 95)
        x = (d[m] - (t6_apex + 5)) / 90.0
        ch["throttle"][m] = np.interp(x, [0, .15, .3, .45, .6, .75, 1], [.6, .6, .25, .8, .8, .35, 1.0])
        # T8：弯里方向来回修
        m = _win(ch, t8["start_m"], t8["end_m"])
        ch["steer"][m] += 0.12 * np.sin((d[m] - t8["start_m"]) / 8.0 * 2 * np.pi)
        # T10：弯心之后反打方向
        m = _win(ch, t10_apex + 10, t10_apex + 24)
        ch["steer"][m] = -0.15

    res = compare(ref, _modified(ref, faults))
    seg = {s["name"]: s for s in res["segments"]}
    txt = {k: "，".join(s["clauses"]) for k, s in seg.items()}
    assert "入弯前（刹车前）滑行" in txt["T4"], txt["T4"]
    assert seg["T4"]["coast_pre_other_s"] == pytest.approx(0.6, abs=0.2)
    assert "没带刹车入弯" in txt["T1"], txt["T1"]
    assert "松刹车太急" in txt["T1"], txt["T1"]
    assert "出弯收了 2 次油" in txt["T6"], txt["T6"]
    assert "方向来回修了" in txt["T8"], txt["T8"]
    assert seg["T8"]["steer_corrections_other"] - seg["T8"]["steer_corrections_ref"] >= 2
    assert "反打方向 1 次" in txt["T10"], txt["T10"]
    # 没动过的弯不受影响
    for name in ("T2", "T3", "T5", "T7", "T9"):
        assert not any(w in txt[name] for w in NEW_WORDS), (name, txt[name])
    # 反过来比：这些毛病变成参考圈的
    rev = {s["name"]: "，".join(s["clauses"]) for s in compare(_modified(ref, faults), ref)["segments"]}
    assert "出弯收油更少" in rev["T6"] and "反打更少" in rev["T10"]


def test_clutch_in_is_not_coasting(demo):
    """踩着离合（换挡 / 空挡）不算滑行：离合通道要参与重采样。"""
    _, laps = demo
    ref = laps[3]
    b4 = _seg(compare(ref, ref), "T4")["brake_ref_m"]

    def coast(ch):
        ch["throttle"][_win(ch, b4 - 35, b4 - 1)] = 0.0

    def coast_clutch(ch):
        coast(ch)
        ch["clutch"][_win(ch, b4 - 36, b4)] = 1.0

    assert _seg(compare(ref, _modified(ref, coast)), "T4")["coast_pre_other_s"] > 0.4
    assert _seg(compare(ref, _modified(ref, coast_clutch)), "T4")["coast_pre_other_s"] == 0.0


def test_coast_hysteresis_on_maintenance_throttle(demo):
    """维持油门 4% 对 6%、0% 对 3% 这种擦边差别不出“滑行”句子；真的松开（0%）对维持油门（8%）才说。"""
    _, laps = demo
    ref = laps[3]
    apex = _seg(compare(ref, ref), "T6")["apex_m"]

    def hold(level):
        def fn(ch):
            m = _win(ch, apex - 40, apex + 40)
            ch["throttle"][m] = level
            ch["brake"][m] = 0.0
        return fn

    def coast_clauses(a, b):
        res = compare(_modified(ref, hold(a)), _modified(ref, hold(b)))
        return [c for s in res["segments"] for c in s["clauses"] if "滑行" in c]

    assert coast_clauses(0.04, 0.06) == [] and coast_clauses(0.06, 0.04) == []
    assert coast_clauses(0.03, 0.0) == [] and coast_clauses(0.0, 0.03) == []
    assert coast_clauses(12 / 255, 13 / 255) == []
    more = coast_clauses(0.08, 0.0)                 # 这圈松开滑行，参考圈维持 8% 油门
    assert more and "参考圈 0.0 s" in more[0]
    less = coast_clauses(0.0, 0.08)                 # 反过来：参考圈滑行
    assert less and "滑行 0.0 s" in less[0]


# ---------------------------------------------------------------- 两圈走线 / 横向偏移 / 可选曲线 / 固定弯道表

def _shift_right(lap: LapData, meters: float) -> LapData:
    """把整圈走线往行驶方向右侧平移 meters 米（负数 = 左）。"""
    def fn(ch):
        x, z = ch["pos_x"], ch["pos_z"]
        hx, hz = np.gradient(x), np.gradient(z)
        n = np.hypot(hx, hz)
        n[n == 0] = 1.0
        ch["pos_x"] = x + meters * hz / n
        ch["pos_z"] = z - meters * hx / n
    return _modified(lap, fn)


def test_lateral_offset_sign_and_size(demo):
    _, laps = demo
    ref = laps[3]
    for m in (3.0, -2.0):
        res = compare(ref, _shift_right(ref, m))
        off = np.array([v for v in res["channels"]["lateral_offset"] if v is not None])
        assert len(off) > 0.95 * len(res["channels"]["dist"])
        assert np.median(off) == pytest.approx(m, abs=0.2)
        assert np.percentile(np.abs(off - m), 95) < 0.6
    # 同一条走线：偏移 ≈ 0
    off = compare(laps[3], laps[1])["channels"]["lateral_offset"]
    assert max(abs(v) for v in off if v is not None) < 1.0


def test_map_has_both_paths_and_markers(demo):
    _, laps = demo
    res = compare(laps[3], laps[2])
    m = res["map"]
    assert len(m["x_b"]) == len(m["z_b"]) == len(m["x"]) > 100
    assert all(c["label"] for c in m["corners"])
    t1 = _seg(res, "T1")
    # 转向点：第一次 |方向| ≥ 10%，在刹车点之后、弯心之前
    for side in ("ref", "other"):
        assert t1[f"brake_{side}_m"] < t1[f"turn_in_{side}_m"] < t1["apex_m"]
        assert t1[f"min_speed_{side}_m"] is not None and t1[f"full_throttle_{side}_m"] > t1["apex_m"] - 30
    assert res["corner_source"] == "detected"


def test_extra_channels(demo):
    _, laps = demo
    res = compare(laps[3], laps[2], extra=["g_lat", "g_comb", "slip_angle", "slip_ratio", "tire_temp", "susp"])
    n = len(res["channels"]["dist"])
    names = {s["name"] for v in res["extra"].values() for s in v["series"]}
    assert names == {"g_lat", "g_comb", "slip_angle_front", "slip_angle_rear", "slip_ratio_drive",
                     "tire_temp_fl", "tire_temp_fr", "tire_temp_rl", "tire_temp_rr",
                     "susp_fl", "susp_fr", "susp_rl", "susp_rr"}
    for side in ("ref", "other"):
        ch = res["channels"][side]
        assert all(len(ch[k]) == n for k in names)
        assert 50 < np.median(ch["tire_temp_fl"]) < 130           # °C
        assert max(ch["g_comb"]) >= max(abs(v) for v in ch["g_lat"]) - 1e-6
        assert all(0 <= v <= 1 for v in ch["susp_fl"])
    # 后驱车：驱动轮 = 后轮，出弯（T5 后面）有明显正滑移
    assert max(res["channels"]["other"]["slip_ratio_drive"]) > 1.0
    json.dumps(res, allow_nan=False)
    assert {o["key"] for o in res["extra_options"]} == {"g_lat", "g_comb", "slip_angle", "slip_ratio", "tire_temp", "susp"}
    with pytest.raises(ValueError):
        compare(laps[3], laps[2], extra=["nope"])
    assert compare(laps[3], laps[2])["extra"] == {}


def test_fixed_corners_with_labels(demo):
    """传入固定弯道表：段名用编号（T5），解释句和总结用显示名。"""
    from forza.corners import detect_corners
    _, laps = demo
    cs = detect_corners(laps[3])
    for c in cs:
        c["label"] = "发卡弯" if c["name"] == "T3" else c["name"]
    res = compare(laps[3], laps[2], corners=cs)
    assert res["corner_source"] == "layout"
    assert res["ranking"][0]["name"] == "T3"
    assert res["ranking"][0]["explanation"].startswith("发卡弯：慢 ")
    assert "发卡弯" in res["totals"]["summary"]
    assert next(c for c in res["map"]["corners"] if c["name"] == "T3")["label"] == "发卡弯"
