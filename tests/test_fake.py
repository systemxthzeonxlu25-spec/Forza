"""假数据生成器的测试：几何、时间轴、字段规则、埋的问题、各场景、编解码、可复现。

假数据是下游所有模块的“标准答案”，所以这里尽量直接对着原始帧验证 truth 里写的每一条。
"""
from __future__ import annotations

import json
import math

import numpy as np
import pytest

from forza.fake import build_circuit, build_sprint_route, generate, truth_path_for, write_fake_recording
from forza.frame import G, f_to_c
from forza.packet import FORMATS, PacketDecoder, encode
from forza.recorder import iter_records, read_meta

HZ = 60.0


# ---------------------------------------------------------------- 夹具（模块级缓存，整份测试只生成一次）

@pytest.fixture(scope="module")
def circ():
    return generate("circuit", laps=5, seed=1)


@pytest.fixture(scope="module")
def clean():
    return generate("circuit", laps=4, seed=3, issues=False)


@pytest.fixture(scope="module")
def rew():
    return generate("circuit", laps=3, seed=2, hz=30, rewind=True)


@pytest.fixture(scope="module")
def rst():
    return generate("circuit", laps=2, seed=2, hz=30, restart=True)


@pytest.fixture(scope="module")
def sprint():
    return generate("sprint", family="fh", seed=1, hz=30)


@pytest.fixture(scope="module")
def sprint_clean():
    return generate("sprint", family="fh", seed=4, hz=30, issues=False)


@pytest.fixture(scope="module")
def free():
    return generate("freeroam", family="fh", seed=1)


@pytest.fixture(scope="module")
def free_clean():
    return generate("freeroam", family="fh", seed=1, issues=False)


@pytest.fixture(scope="module")
def all_results(circ, clean, rew, rst, sprint, sprint_clean, free, free_clean):
    return {"circ": circ, "clean": clean, "rew": rew, "rst": rst, "sprint": sprint,
            "sprint_clean": sprint_clean, "free": free, "free_clean": free_clean}


# ---------------------------------------------------------------- 小工具

def race(res):
    return [f for _, f in res.records if f.is_race_on]


def col(frames, name):
    return np.array([getattr(f, name) for f in frames])


def runs_of(mask):
    """布尔数组里连续 True 片段的 [起, 止) 下标。"""
    m = np.concatenate([[False], np.asarray(mask, bool), [False]])
    d = np.diff(m.astype(int))
    return list(zip(np.nonzero(d == 1)[0], np.nonzero(d == -1)[0]))


def lap_view(res):
    """环形赛道比赛帧 + 每帧圈内距离（不含倒带/重开时用）。"""
    fr = race(res)
    L = res.truth["track"]["length_m"]
    lapn = col(fr, "lap_number")
    d = col(fr, "distance") - lapn * L
    return fr, lapn, d


def wheels(frames, name):
    return np.array([getattr(f, name) for f in frames])


# ---------------------------------------------------------------- 赛道几何

def test_circuit_geometry():
    trk = build_circuit()
    L = trk.length_m
    assert 2500 <= L <= 4000 and trk.closed
    assert L == int(L) and len(trk.s) == L + 1
    # 首尾相接，航向连续（正好转一整圈）
    assert math.hypot(trk.x[-1] - trk.x[0], trk.z[-1] - trk.z[0]) < 0.5
    assert abs(trk.heading[-1] - trk.heading[0] - 2 * math.pi) < 1e-6
    assert abs(trk.y[-1] - trk.y[0]) < 1e-6
    # 逐米数组：相邻点相距约 1 m
    step = np.hypot(np.diff(trk.x), np.diff(trk.z))
    assert np.all(np.abs(step - 1.0) < 0.01)
    corners = trk.corners
    assert len(corners) >= 6
    assert [c.name for c in corners] == [f"T{i + 1}" for i in range(len(corners))]
    assert all(a.end_m <= b.start_m for a, b in zip(corners, corners[1:]))          # 按行驶顺序
    assert {c.direction for c in corners} == {"left", "right"}
    hp, sw = trk.role("hairpin"), trk.role("sweeper")
    assert 20 <= hp.min_radius_m <= 35 and hp.angle_deg >= 150
    assert 150 <= sw.min_radius_m <= 250 and sw.angle_deg >= 90
    c1, c2 = (trk.corner(n) for n in trk.roles["chicane"])
    assert (c1.direction, c2.direction) == ("left", "right") and c2.index == c1.index + 1
    # 主直道 ≥ 600 m，末端就是 T1
    ms = trk.main_straight
    assert ms["length_m"] >= 600 and ms["end_m"] == corners[0].start_m and trk.roles["heavy_brake"] == "T1"
    # 坑在直道上（坑的范围内曲率为 0）
    b = trk.bump_dist_m
    assert np.all(trk.curvature_at(np.linspace(b - 15, b + 15, 31)) == 0)
    # 曲率符号：右弯 +
    for c in corners:
        assert np.sign(trk.curvature_at(c.apex_m)) == c.sign


def test_sprint_route_geometry():
    trk = build_sprint_route()
    assert not trk.closed and 3000 <= trk.length_m <= 4000
    assert trk.geom_length > trk.length_m          # 终点后还有收车缓冲区
    assert math.hypot(trk.x[-1] - trk.x[0], trk.z[-1] - trk.z[0]) > 500
    assert {"hairpin", "sweeper", "chicane", "heavy_brake"} <= set(trk.roles)


# ---------------------------------------------------------------- 所有场景通用的帧规则

def test_time_axis_and_basic_rules(all_results):
    for name, res in all_results.items():
        hz = res.truth["hz"]
        t = np.array([t for t, _ in res.records])
        assert t[0] == 0.0, name
        assert np.max(np.abs(t - np.arange(len(t)) / hz)) < 1e-9, name          # 严格 1/hz
        assert len(res.records) == res.truth["n_records"]
        fr = [f for _, f in res.records]
        ts = col(fr, "timestamp_ms")
        assert np.all(np.diff(ts) > 0), name                                       # 游戏时间戳一直往前
        # Speed 必须等于局部速度向量模长（解码器靠它识别未知包格式）
        sp = col(fr, "speed")
        vm = np.array([f.velocity_mag for f in fr])
        assert np.max(np.abs(sp - vm)) < 1e-9, name
        rf = race(res)
        assert set(np.unique(col(rf, "gear"))) <= set(range(1, 7)), name
        for k in ("throttle", "brake"):
            v = col(rf, k)
            assert v.min() >= 0 and v.max() <= 255, (name, k)
        assert np.abs(col(rf, "steer")).max() <= 127
        assert all(f.clutch == 0 and f.handbrake == 0 for f in rf)
        assert all(f.engine_max_rpm == 8000 and f.car_ordinal == 2240 and f.drivetrain == 1 for f in fr)
        json.dumps(res.truth)                                                      # truth 能直接序列化


def test_menu_and_results_frames(circ):
    fr = [f for _, f in circ.records]
    n = int(HZ)
    assert not any(f.is_race_on for f in fr[:n]) and fr[n].is_race_on
    assert not any(f.is_race_on for f in fr[-n:]) and fr[-n - 1].is_race_on
    assert all(f.speed == 0 and f.velocity_mag == 0 for f in fr[:n])
    trk = build_circuit()
    first = fr[n]
    assert (first.pos_x, first.pos_z) == pytest.approx(trk.start_pos, abs=1e-6)  # 起跑线上静止起步
    assert first.speed == 0 and first.race_time == 0 and first.lap_number == 0 and first.distance == 0


# ---------------------------------------------------------------- 环形赛道的圈字段

def check_lap_fields(res, frames=None):
    """lap_number 每次 +1，加一之后那一帧的 last_lap 就是 truth 里的圈速。"""
    fr = frames if frames is not None else race(res)
    laps = res.truth["laps"]
    lapn = col(fr, "lap_number")
    inc = np.nonzero(np.diff(lapn) != 0)[0] + 1
    assert np.all(np.diff(lapn)[inc - 1] == 1)
    assert len(inc) == len(laps) == res.truth["laps_requested"]
    best = math.inf
    for k, i in enumerate(inc):
        f = fr[i]
        assert f.lap_number == k + 1
        assert f.last_lap == pytest.approx(laps[k]["lap_time"], abs=1e-6)
        best = min(best, laps[k]["lap_time"])
        assert f.best_lap == pytest.approx(best, abs=1e-6)
        assert 0 <= f.current_lap < 1.0 / res.truth["hz"] + 1e-9
        assert fr[i - 1].current_lap == pytest.approx(fr[i - 1].race_time - (laps[k - 1]["race_time_end_s"] if k else 0))
    assert lapn[0] == 0 and lapn[-1] == len(laps)
    assert all(f.last_lap == 0 and f.best_lap == 0 for f in fr[:inc[0]])


def test_circuit_lap_fields(circ):
    check_lap_fields(circ)
    fr = race(circ)
    rt = col(fr, "race_time")
    assert np.max(np.abs(np.diff(rt) - 1 / HZ)) < 1e-9                             # race_time 连续
    assert np.all(np.diff(col(fr, "distance")) >= 0)                              # 距离单调
    assert set(col(fr, "race_position")) == {1}
    laps = circ.truth["laps"]
    assert [l["kind"] for l in laps] == ["standing_start"] + ["flying"] * 4
    t = [l["lap_time"] for l in laps]
    assert t[0] > max(t[1:])                                                     # 站立起步那圈最慢
    normal = [t[1], t[3], t[4]]                                                  # 第 2 圈埋了慢弯
    assert max(normal) - min(normal) < 0.3
    # 收车：lap_number = laps，还在跑、在减速
    cool = [f for f in fr if f.lap_number == 5]
    assert 1.8 < len(cool) / HZ < 2.2
    assert cool[-1].speed < cool[0].speed and all(f.brake > 0 for f in cool[2:])


def test_circuit_physical_plausibility(circ):
    fr, lapn, d = lap_view(circ)
    trk = build_circuit()
    kmh = col(fr, "speed") * 3.6
    hp = trk.role("hairpin")
    in_hp = (d > hp.start_m) & (d < hp.end_m) & (lapn < 5)
    assert 65 <= kmh[in_hp].min() <= 95
    assert kmh.max() >= 250
    sw = trk.role("sweeper")
    assert 165 <= kmh[(d > sw.arc_start_m) & (d < sw.arc_end_m)].min() <= 200
    assert np.abs(col(fr, "accel_x")).max() / G < 1.45
    assert -col(fr, "accel_z").min() / G < 1.5
    # 横向加速度 = v²κ，偏航角速度 ≈ v·κ，方向盘和横向 G 同号
    ax, v, k = col(fr, "accel_x"), col(fr, "speed"), trk.curvature_at(d)
    assert np.max(np.abs(ax - v * v * k)) < 1e-6
    turning = np.abs(ax) > 5
    assert np.all(np.sign(col(fr, "steer")[turning]) == np.sign(ax[turning])) or \
        np.mean(np.sign(col(fr, "steer")[turning]) == np.sign(ax[turning])) > 0.97     # 只有甩尾反打时例外
    # 位置在中心线上
    smp = trk.sample(d)
    assert np.max(np.hypot(col(fr, "pos_x") - smp["x"], col(fr, "pos_z") - smp["z"])) < 0.05
    # 转速 = 后轮轮速 × 齿比
    rpm = col(fr, "rpm")
    assert rpm.min() >= 900 and rpm.max() <= 8000
    tc = np.array([f.tire_temp_c() for f in fr])
    assert tc.min() > 25 and tc.max() < 110
    fuel = col(fr, "fuel")
    assert fuel[0] == 1.0 and np.all(np.diff(fuel) <= 0) and fuel[-1] == pytest.approx(0.9, abs=0.01)
    assert all(f.track_ordinal == 860 for f in fr) and fr[-1].tire_wear[0] == pytest.approx(0.05, abs=0.005)


# ---------------------------------------------------------------- 埋的问题：在原始帧里看得到

def test_issue_slow_corner(circ):
    sc = circ.truth["issues"]["slow_corner"]
    assert sc["lap_number"] == 2 and sc["corner"] == "T3"
    assert 0.3 <= sc["expected_time_loss_s"] <= 0.6
    assert sc["time_loss_in_window_s"] >= 0.9 * sc["expected_time_loss_s"]        # 几乎全丢在 T3
    assert sc["brake_earlier_m"] == pytest.approx(25, abs=1)
    assert sc["min_speed_factor"] == pytest.approx(0.92, abs=0.005)
    t = [l["lap_time"] for l in circ.truth["laps"]]
    others = [t[1], t[3], t[4]]
    assert t[2] - np.mean(others) == pytest.approx(sc["expected_time_loss_s"], abs=0.2)
    assert t[2] > max(others) + 0.25
    # 原始帧：第 2 圈 T3 的最低速度低 8%，刹车起点提前约 25 m
    fr, lapn, d = lap_view(circ)
    trk = build_circuit()
    c = trk.corner("T3")
    sp, br = col(fr, "speed"), col(fr, "brake")

    def min_speed(lap):
        return sp[(lapn == lap) & (d >= c.start_m) & (d <= c.end_m)].min()

    prev = trk.corners[c.index - 1]                                              # T2 出口之后才是 T3 的刹车区

    def brake_point(lap):
        sel = np.nonzero((lapn == lap) & (d > prev.end_m) & (d < c.arc_start_m) & (br > 40))[0]
        return d[sel[0]]
    assert min_speed(2) / np.mean([min_speed(k) for k in (1, 3, 4)]) == pytest.approx(0.92, abs=0.01)
    assert np.mean([brake_point(k) for k in (1, 3, 4)]) - brake_point(2) == pytest.approx(25, abs=3)
    assert brake_point(2) == pytest.approx(sc["slow_brake_point_m"], abs=3)


def test_issue_understeer(circ, clean):
    iss = circ.truth["issues"]["understeer"]
    assert iss["corner"] == "T7" and len(iss["occurrences"]) == 5
    lo, hi = iss["design_dist_m"]
    for res, is_issue in ((circ, True), (clean, False)):
        fr, lapn, d = lap_view(res)
        sel = (d > lo + 15) & (d < hi - 15)
        sa = np.abs(wheels([f for f, s in zip(fr, sel) if s], "slip_angle"))
        if is_issue:
            assert sa[:, :2].min() >= 1.1 and sa[:, :2].max() <= 1.25
            assert sa[:, 2:].min() >= 0.55 and sa[:, 2:].max() <= 0.70
            steer_issue = col(fr, "steer")[sel].mean()
        else:
            assert sa.max() < 0.95
            steer_clean = col(fr, "steer")[sel].mean()
    assert steer_issue / steer_clean == pytest.approx(1.3, abs=0.08)


def test_issue_oversteer_exit_and_wheelspin(circ):
    iss = circ.truth["issues"]["oversteer_exit"]
    trk = build_circuit()
    hp = trk.role("hairpin")
    assert iss["corner"] == hp.name == "T5"
    fr, lapn, d = lap_view(circ)
    for lap in range(5):
        idx = np.nonzero((lapn == lap) & (d >= hp.arc_end_m))[0]
        j0 = idx[0]
        spin = fr[j0:j0 + int(0.5 * HZ)]
        sr = wheels(spin, "slip_ratio")
        assert sr[:, 2:].min() >= 1.2 and sr[:, 2:].max() <= 1.5           # 后轮空转约 0.5 s
        assert np.abs(sr[:, :2]).max() < 0.1
        assert min(f.throttle for f in spin) >= 0.85 * 255 - 1
        assert fr[j0 + int(0.5 * HZ) + 1].slip_ratio[2] < 1.0
        mid = wheels(fr[j0 + 6:j0 + int(0.6 * HZ) - 6], "slip_angle")
        assert np.all(np.sign(mid) == hp.sign)                              # 左弯，侧偏角为负
        assert np.abs(mid[:, 2:]).min() >= 1.1 and np.abs(mid[:, 2:]).max() <= 1.3
        assert np.abs(mid[:, :2]).min() >= 0.5 and np.abs(mid[:, :2]).max() <= 0.7
        before = wheels(fr[j0 - 30:j0 - 1], "slip_angle")
        assert np.abs(before).max() < 1.0                                    # 弯中还是正常的


def test_issue_bottoming(circ):
    b = circ.truth["track"]["bump_dist_m"]
    iss = circ.truth["issues"]["bottoming"]
    assert iss["bump_dist_m"] == b and len(iss["occurrences"]) == 5
    fr, lapn, d = lap_view(circ)
    for lap in range(5):
        sel = (lapn == lap) & (np.abs(d - b) < 15)
        su = wheels([f for f, s in zip(fr, sel) if s], "susp_norm")
        for w in (0, 1):
            dur = (su[:, w] >= 0.99).sum() / HZ
            assert 0.1 <= dur <= 0.25
        assert su[:, 2:].max() < 0.95
        assert np.abs(col(fr, "accel_y")[sel]).max() > 2 * G                  # 竖直 G 尖峰
    far = np.abs(d - b) > 30
    assert wheels([f for f, s in zip(fr, far) if s], "susp_norm").max() < 0.95


def test_issue_front_lock(circ):
    iss = circ.truth["issues"]["front_lock"]
    assert iss["corner"] == "T1" and len(iss["occurrences"]) == 5
    trk = build_circuit()
    t1 = trk.corner("T1")
    fr, lapn, d = lap_view(circ)
    for lap in range(5):
        sel = np.nonzero((lapn == lap) & (d > t1.start_m - 300) & (d < t1.apex_m))[0]
        sr = wheels([fr[i] for i in sel], "slip_ratio")
        locked = (sr[:, 0] <= -1.1) & (sr[:, 1] <= -1.1)
        (a, b), = runs_of(locked)
        assert 0.15 <= (b - a) / HZ <= 0.25
        assert np.all((sr[a:b, 2:] > -0.4) & (sr[a:b, 2:] < -0.2))
        assert all(fr[i].brake > 200 for i in sel[a:b])                     # 正在重刹
        o = iss["occurrences"][lap]
        assert o["lap_dist_start_m"] == pytest.approx(d[sel[a]], abs=0.01)


def test_issue_tire_temp_imbalance(circ, clean):
    for res, lo, hi in ((circ, 7, 13), (clean, -3, 3)):
        fr, lapn, d = lap_view(res)
        last = [f for f, k in zip(fr, lapn) if k == res.truth["laps_requested"] - 1]
        tc = np.array([f.tire_temp_c() for f in last]).mean(axis=0)
        assert lo <= tc[0] - tc[1] <= hi
    ss = circ.truth["issues"]["tire_temp_imbalance"]["steady_state_c"]
    assert ss["fl"] - ss["fr"] >= 7
    # Frame 里是 °F
    f = race(circ)[-1]
    assert f_to_c(f.tire_temp[0]) == pytest.approx(f.tire_temp_c()[0])
    assert f.tire_temp[0] > 150


def test_issue_rev_limiter(circ, clean):
    trk = build_circuit()
    t1 = trk.corner("T1")
    fr, lapn, d = lap_view(circ)
    rpm, gear, brake = col(fr, "rpm"), col(fr, "gear"), col(fr, "brake")
    lim = (gear == 6) & (rpm >= 0.98 * 8000)
    for lap in range(1, 5):
        sel = (lapn == lap) & (d < t1.start_m)
        (a, b), = [r for r in runs_of(lim & sel)]
        assert (b - a) / HZ >= 1.5
        assert brake[b:b + int(0.2 * HZ)].max() > 200                       # 撞着断油直接进重刹
    assert circ.truth["issues"]["rev_limiter"]["gear"] == 6
    cfr = race(clean)
    crpm, cgear = col(cfr, "rpm"), col(cfr, "gear")
    assert crpm[cgear == 6].max() <= 0.95 * 8000


def test_truth_occurrences_match_frames(circ, sprint):
    """truth 里每个 occurrence 的时间段，在原始帧里都确实满足对应条件。"""
    checks = {
        "bottoming": lambda f: max(f.susp_norm[0], f.susp_norm[1]) >= 0.99,
        "front_lock": lambda f: f.slip_ratio[0] <= -1.1,
        "rev_limiter": lambda f: f.gear == 6 and f.rpm >= 7840,
        "understeer": lambda f: abs(f.slip_angle[0]) > 0.95,
        "oversteer_exit": lambda f: abs(f.slip_angle[2]) > 0.95,
    }
    for res in (circ, sprint):
        hz = res.truth["hz"]
        for key, pred in checks.items():
            for o in res.truth["issues"][key]["occurrences"]:
                k0, k1 = round(o["t_start"] * hz), round(o["t_end"] * hz)
                frames = [res.records[k][1] for k in range(k0, k1 + 1)]
                assert all(pred(f) for f in frames), (key, o)
                assert not pred(res.records[k0 - 1][1]) or key in ("understeer", "oversteer_exit")


# ---------------------------------------------------------------- 没埋问题时什么都不该有（测误报用）

def test_clean_has_no_issues(clean, sprint_clean, free_clean):
    for res in (clean, sprint_clean, free_clean):
        assert res.truth["issues"] == {} and not res.truth["issues_enabled"]
        fr = race(res)
        su = wheels(fr, "susp_norm")
        assert su.min() >= 0.08 and su.max() <= 0.90
        assert np.abs(wheels(fr, "slip_angle")).max() < 1.0
        assert np.abs(wheels(fr, "slip_ratio")).max() < 1.0
        rpm, gear = col(fr, "rpm"), col(fr, "gear")
        assert rpm[gear == 6].max(initial=0) <= 0.95 * 8000
    t = [l["lap_time"] for l in clean.truth["laps"]][1:]
    assert max(t) - min(t) < 0.3


# ---------------------------------------------------------------- 倒带 / 重开

def test_rewind(rew):
    hz = rew.truth["hz"]
    recs = rew.records
    ev, = rew.truth["events"]
    assert ev["type"] == "rewind" and ev["lap_number"] == 1 and ev["jump_s"] == 4.0
    k = round(ev["t"] * hz)
    a, b = recs[k - 1][1], recs[k][1]
    assert a.is_race_on and b.is_race_on and a.lap_number == b.lap_number == 1
    assert a.race_time - b.race_time == pytest.approx(4.0, abs=1e-9)
    assert a.current_lap - b.current_lap == pytest.approx(4.0, abs=1e-9)
    L = rew.truth["track"]["length_m"]
    assert (a.distance - L) / L == pytest.approx(0.55, abs=0.01)
    # 所有游戏字段都回到 4 s 前那一帧（录像时间和 timestamp_ms 除外）
    old = recs[k - 1 - round(4.0 * hz)][1]
    for name in ("pos_x", "pos_z", "distance", "race_time", "speed", "tire_temp", "susp_norm", "fuel", "gear"):
        assert getattr(old, name) == getattr(b, name), name
    assert b.timestamp_ms > a.timestamp_ms and recs[k][0] > recs[k - 1][0]
    # 圈速不受影响，切圈规则照旧
    check_lap_fields(rew)
    lap1 = rew.truth["laps"][1]
    assert lap1["t_end"] - lap1["t_start"] == pytest.approx(lap1["lap_time"] + 4.0 + 1 / hz, abs=1e-6)


def test_restart(rst):
    hz = rst.truth["hz"]
    recs = rst.records
    ev, = rst.truth["events"]
    assert ev["type"] == "restart" and ev["lap_number"] == 1
    k = round(ev["t"] * hz)
    a, b = recs[k - 1][1], recs[k][1]
    assert a.lap_number == 1 and b.lap_number == 0
    assert b.race_time == 0 and b.current_lap == 0 and b.distance == 0 and b.speed == 0
    assert (b.pos_x, b.pos_z) == pytest.approx(build_circuit().start_pos, abs=1e-6)
    L = rst.truth["track"]["length_m"]
    assert (a.distance - L) / L == pytest.approx(0.60, abs=0.01)
    # 重开前跑完的那圈只记在 events 里，并且和帧里的 last_lap 一致
    aborted = [f for _, f in recs[:k] if f.is_race_on]
    j = next(i for i, f in enumerate(aborted) if f.lap_number == 1)
    assert aborted[j].last_lap == pytest.approx(ev["aborted_laps"][0]["lap_time"], abs=1e-6)
    # truth.laps 只算重开之后的
    after = [f for _, f in recs[k:] if f.is_race_on]
    check_lap_fields(rst, after)
    assert rst.truth["laps"][0]["t_start"] == pytest.approx(ev["t"])


# ---------------------------------------------------------------- 冲刺 / 漫游

def test_sprint(sprint):
    tr = sprint.truth
    hz = tr["hz"]
    assert tr["family"] == "fh" and tr["laps"] == []
    runs = tr["runs"]
    assert [r["finished"] for r in runs] == [True, False, True]
    assert runs[2]["time_s"] < runs[0]["time_s"]                           # 第 3 次更快
    segs = []
    cur = None
    for _, f in sprint.records:
        if f.is_race_on:
            if cur is None:
                cur = []
                segs.append(cur)
            cur.append(f)
        else:
            cur = None
    assert len(segs) == 3
    assert not sprint.records[0][1].is_race_on and not sprint.records[-1][1].is_race_on
    start = tuple(tr["track"]["start_pos"])
    for seg, run in zip(segs, runs):
        f0 = seg[0]
        assert (f0.pos_x, f0.pos_z) == pytest.approx(start, abs=1e-3)       # 同一个起点
        assert f0.race_time == 0 and f0.distance == 0 and f0.speed == 0
        assert all(f.lap_number == 0 and f.current_lap == f.race_time for f in seg)
        assert run["t_start"] == pytest.approx(next(t for t, f in sprint.records if f is f0))
        if run["finished"]:
            rt = col(seg, "race_time")
            assert rt.max() == pytest.approx(run["time_s"], abs=1e-6)
            assert set(col(seg, "race_position")) == {run["race_position"]}
            frozen = seg[-int(1.5 * hz):]                                    # 过终点后计时停住、车还在走
            assert len({f.race_time for f in frozen}) == 1 and frozen[-1].speed > 10
            assert max(f.distance for f in seg) > tr["route"]["length_m"]
        else:
            quit_k = next(i for i, f in enumerate(seg) if f.race_position == 0)
            assert seg[quit_k - 1].distance == pytest.approx(tr["route"]["length_m"] / 2, abs=40)
            roam = seg[quit_k:]
            assert len(roam) / hz == pytest.approx(3.0, abs=0.1)
            assert all(f.race_time == 0 and f.distance == 0 and f.speed > 5 for f in roam)
            assert run["abandoned_race_time_s"] == pytest.approx(seg[quit_k - 1].race_time)
    assert {"understeer", "oversteer_exit", "front_lock", "tire_temp_imbalance"} <= set(tr["issues"])
    assert all("run" in o for o in tr["issues"]["understeer"]["occurrences"])


def test_freeroam(free):
    tr = free.truth
    fr = [f for _, f in free.records]
    assert all(f.is_race_on for f in fr)
    assert len(fr) / tr["hz"] == pytest.approx(60.0)
    for name in ("race_position", "lap_number", "race_time", "current_lap", "last_lap", "best_lap", "distance"):
        assert set(col(fr, name)) == {0}, name
    assert tr["laps"] == [] and tr["expect_laps"] is False
    assert max(f.speed for f in fr) > 30


# ---------------------------------------------------------------- 编码 / 解码 / 写录像

SLED_KEYS = ("rpm", "accel_x", "vel_z", "yaw", "angvel_y", "susp_norm", "slip_ratio", "slip_angle",
             "wheel_speed", "rumble", "timestamp_ms", "car_ordinal", "car_pi", "num_cylinders")
DASH_KEYS = ("speed", "pos_x", "pos_z", "tire_temp", "distance", "race_time", "last_lap", "best_lap",
             "current_lap", "lap_number", "race_position", "throttle", "brake", "gear", "steer", "fuel")


def _same(a, b):
    if isinstance(a, tuple):
        return all(_same(x, y) for x, y in zip(a, b))
    if isinstance(a, float):
        return abs(a - b) <= 1e-6 * max(1.0, abs(b))
    return a == b


@pytest.mark.parametrize("fmt_key", list(FORMATS))
def test_encode_decode_roundtrip(circ, fmt_key):
    fmt = FORMATS[fmt_key]
    dec = PacketDecoder()
    frames = [f for _, f in circ.records][::40] + [f for _, f in circ.records][-70:]
    for f in frames:
        g = dec.decode(encode(f, fmt_key))
        assert g.fmt == fmt_key and g.is_race_on == f.is_race_on
        keys = SLED_KEYS + (DASH_KEYS if fmt.has_dash else ())
        for k in keys:
            assert _same(getattr(g, k), getattr(f, k)), (fmt_key, k, getattr(g, k), getattr(f, k))
        if fmt_key == "fm2023_dash":
            assert g.track_ordinal == 860 and _same(g.tire_wear, f.tire_wear)
        if fmt.family == "fh":
            assert g.hzn_car_group == 3
    assert not any(k.startswith("verify") for k in dec.warnings)


def test_write_fake_recording(tmp_path):
    path = tmp_path / "demo.frec"
    truth = write_fake_recording(path, "fh", laps=2, hz=20, seed=7)
    tpath = tmp_path / "demo.truth.json"
    assert truth_path_for(path) == tpath and tpath.exists()
    assert json.loads(tpath.read_text(encoding="utf-8")) == json.loads(json.dumps(truth))
    assert truth["fmt"] == "fh" and truth["family"] == "fh" and truth["total_size"] is None
    meta = read_meta(path)
    assert {k: meta[k] for k in ("source", "scenario", "fmt", "game", "seed")} == \
        {"source": "fake", "scenario": "circuit", "fmt": "fh", "game": "fh", "seed": 7}
    recs = list(iter_records(path))
    assert len(recs) == truth["n_records"] and all(len(b) == 324 for _, b in recs)
    dec = PacketDecoder()
    frames = [dec.decode(b, t) for t, b in recs]
    assert frames[0].family == "fh" and frames[0].hzn_car_group == 3
    lapn = [f.lap_number for f in frames if f.is_race_on]
    inc = [i for i in range(1, len(lapn)) if lapn[i] != lapn[i - 1]]
    race_frames = [f for f in frames if f.is_race_on]
    assert [race_frames[i].last_lap for i in inc] == pytest.approx([l["lap_time"] for l in truth["laps"]], abs=1e-4)


def test_write_padded_unknown_size(tmp_path):
    """补零到未知包长：解码器靠 Speed ≈ |速度| 自动认出布局。"""
    path = tmp_path / "pad.frec.gz"
    truth = write_fake_recording(path, "fm2023_dash", laps=1, hz=20, total_size=350)
    assert truth_path_for(path) == tmp_path / "pad.truth.json" and truth["packet_size"] == 350
    dec = PacketDecoder()
    out = [dec.decode(b) for _, b in iter_records(path)]
    assert any("350" in w for w in dec.warnings.values())
    moving = [f for f in out[len(out) // 3:] if f.is_race_on]
    assert all(abs(f.speed - f.velocity_mag) < 1e-3 for f in moving)
    assert moving[-1].lap_number == 1


def test_invalid_arguments(tmp_path):
    with pytest.raises(ValueError):
        generate("drag")
    with pytest.raises(ValueError):
        generate("circuit", laps=1, rewind=True)
    with pytest.raises(ValueError):
        write_fake_recording(tmp_path / "x.frec", "fm2023_dash", "sprint")      # 冲刺只有 Horizon 有
    with pytest.raises(ValueError):
        write_fake_recording(tmp_path / "x.frec", "fh", total_size=100)


# ---------------------------------------------------------------- 可复现

def test_deterministic():
    a = generate("circuit", laps=2, hz=20, seed=5, rewind=True)
    b = generate("circuit", laps=2, hz=20, seed=5, rewind=True)
    c = generate("circuit", laps=2, hz=20, seed=6, rewind=True)
    assert a.truth == b.truth
    assert [(t, f.to_dict()) for t, f in a.records] == [(t, f.to_dict()) for t, f in b.records]
    assert [l["lap_time"] for l in a.truth["laps"]] != [l["lap_time"] for l in c.truth["laps"]]
    # 同一个 seed，重开不影响主比赛
    d = generate("circuit", laps=2, hz=20, seed=5, restart=True)
    assert [l["lap_time"] for l in d.truth["laps"]] == [l["lap_time"] for l in a.truth["laps"]]


def test_family_fh_circuit():
    res = generate("circuit", family="fh", laps=1, hz=20)
    assert res.truth["family"] == "fh"
    assert all(f.family == "fh" and f.hzn_car_group == 3 for _, f in res.records)
    check_lap_fields(res)
