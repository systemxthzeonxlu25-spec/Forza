"""调车诊断第二批：平衡分布（车速分档 + 弯 × 阶段热力表）、刹车平衡 / 压力、差速器内外轮、车身与悬挂、
固定弯道表、ABS / 空力勾选、多圈合并、接口参数。

埋了问题的假数据要给出合理结果；干净数据不能报 warn / alert；老圈缺通道不能崩。
"""
import copy
import json

import numpy as np
import pytest
from fastapi.testclient import TestClient

from forza import sectors, tuning
from forza.app import create_app
from forza.corners import detect_corners
from forza.fake import write_fake_recording
from forza.hub import Hub
from forza.lapdata import LapData
from forza.laps import LapDetector, LapSaver
from forza.replay import import_file
from forza.runtime import Runtime
from forza.storage import Store

SEV = {"info": 0, "warn": 1, "alert": 2}
NEW_CHECKS = ("brake_balance", "brake_pressure", "differential")


def _split(path):
    hub = Hub()
    det = LapDetector()
    laps = []
    det.add_listener(laps.append)
    hub.subscribe(det.on_frame)
    import_file(path, hub)
    det.flush()
    return laps


@pytest.fixture(scope="module")
def issues(tmp_path_factory):
    p = tmp_path_factory.mktemp("tuning_b2") / "issues.frec"
    write_fake_recording(p, "fm2023_dash", laps=3, issues=True, seed=11)
    return _split(p)


@pytest.fixture(scope="module")
def clean(tmp_path_factory):
    p = tmp_path_factory.mktemp("tuning_b2") / "clean.frec"
    write_fake_recording(p, "fm2023_dash", laps=3, issues=False, seed=12)
    return _split(p)


@pytest.fixture(scope="module")
def res1(issues):
    return tuning.diagnose(issues[1])


def _copy(lap: LapData, **meta) -> LapData:
    return LapData(meta={**copy.deepcopy(lap.meta), **meta}, ch={k: v.copy() for k, v in lap.ch.items()})


def _by_id(res):
    return {f["id"]: f for f in res["findings"]}


def _status(res):
    return {c["id"]: c for c in res["checks"]}


def _json(res):
    return json.loads(json.dumps(res, ensure_ascii=False, allow_nan=False))


def _runs(mask):
    idx = np.flatnonzero(mask)
    if not idx.size:
        return []
    br = np.flatnonzero(np.diff(idx) > 1)
    return list(zip(idx[np.concatenate([[0], br + 1])], idx[np.concatenate([br, [idx.size - 1]])]))


# ================================================================ 平衡分布（第 2 条）

def test_balance_map_issue_lap(res1):
    bm = res1["balance_map"]
    assert bm["available"] and bm["laps"] == 1
    assert [b["name"] for b in bm["bands"]] == ["低速", "中速", "高速"]
    assert len(bm["edges_kmh"]) == 2 and bm["edges_kmh"][0] < bm["edges_kmh"][1]
    hi, lo = bm["bands"][-1], bm["bands"][0]
    assert hi["ok"] and hi["tendency"] == "understeer" and hi["us_share"] > 0.15   # T7 高速长弯弯中推头
    assert lo["tendency"] == "neutral"
    text = " ".join(bm["notes"])
    assert "只在高速段推头" in text and "如果这车装了可调空力件" in text   # 没说有没有空力：两种情况都讲
    rows = {r["code"]: {c["phase"]: c for c in r["cells"]} for r in bm["rows"]}
    assert [r["code"] for r in bm["rows"]] == [f"T{i}" for i in range(1, 11)]
    t7, t5 = rows["T7"]["mid"], rows["T5"]["exit"]
    assert t7["ok"] and t7["tendency"] == "understeer" and t7["b"] > 0.15 and 0 < t7["level"] <= 1
    assert t5["ok"] and t5["tendency"] == "oversteer" and t5["b"] < -0.15
    gray = [c for r in rows.values() for c in r.values() if not c["ok"]]
    assert gray and all(c["b"] is None and c["tendency"] is None and c["level"] is None for c in gray)
    assert {(c["code"], c["phase"]) for c in bm["top_cells"]} >= {("T7", "mid"), ("T5", "exit")}
    others = [c for code, r in rows.items() for ph, c in r.items() if c["ok"] and (code, ph) not in {("T7", "mid"), ("T5", "exit")}]
    assert all(c["tendency"] == "neutral" for c in others)


def test_balance_map_aero_wording(issues):
    yes = " ".join(tuning.diagnose(issues[1], aero=True)["balance_map"]["notes"])
    no = " ".join(tuning.diagnose(issues[1], aero=False)["balance_map"]["notes"])
    assert "这车有可调空力" in yes and "前翼加一点" in yes
    assert "这车没有可调空力" in no and "不是空力能调的" in no and "前翼加一点" not in no
    r = tuning.diagnose(issues[1], aero=False)
    assert r["summary"]["assists"] == {"abs_on": None, "aero": False}


def test_balance_map_clean_is_neutral(clean):
    for lap in clean:
        bm = tuning.diagnose(lap)["balance_map"]
        assert bm["available"]
        assert all(b["tendency"] in ("neutral", None) for b in bm["bands"])
        assert bm["top_cells"] == []
        assert all(c["tendency"] in ("neutral", None) for r in bm["rows"] for c in r["cells"])


def test_balance_speed_bands_follow_car_or_config(issues):
    # 分档按这台车自己的车速分位数：同一圈车速减半，分档边界也跟着减半（不写死 100 / 160）
    base = tuning.diagnose(issues[1])["balance_map"]["edges_kmh"]
    slow = _copy(issues[1])
    slow.ch["speed"] = slow.ch["speed"] * 0.5
    half = tuning.diagnose(slow)["balance_map"]["edges_kmh"]
    assert half == pytest.approx([e / 2 for e in base], abs=3)
    # 配置里写了固定边界就用固定的
    bm = tuning.diagnose(issues[1], rules={"balance_map": {"speed_band_edges_kmh": [100, 160]}})["balance_map"]
    assert bm["edges_kmh"] == [100, 160]
    assert [b["range"] for b in bm["bands"]] == ["< 100 km/h", "100–160 km/h", "> 160 km/h"]


def test_balance_map_without_corners_still_has_bands(issues):
    lap = _copy(issues[1])
    lap.ch["pos_x"] = np.zeros_like(lap.ch["pos_x"])
    lap.ch["pos_z"] = np.zeros_like(lap.ch["pos_z"])
    lap.ch["yaw_rate"] = np.zeros_like(lap.ch["yaw_rate"])
    res = _json(tuning.diagnose(lap))
    assert _status(res)["balance"]["status"] == "skipped"
    bm = res["balance_map"]
    assert bm["available"] and bm["rows"] == [] and len(bm["bands"]) == 3


# ================================================================ 刹车平衡（第 11 条）

def test_brake_balance_issue_lap_is_t1_only(res1):
    st = _status(res1)["brake_balance"]
    assert st["status"] == "ok" and "前轴先饱和 1 段" in st["note"]
    zones = res1["brake_analysis"]["zones"]
    front = [z for z in zones if z["bias"] == "front"]
    assert len(front) == 1 and front[0]["first"] == "front" and front[0]["front_peak"] > 1.0 > front[0]["rear_peak"]
    assert front[0]["location"]["dist_m"] < 694                      # T1 弯心之前的直线重刹
    # 抱死那条要说明：其余重刹段前后平衡正常 → 更像是那一脚的问题
    lock = _by_id(res1)["brake_lock_front"]
    assert any("刹车平衡整体并不偏前" in e for e in lock["evidence"])
    assert lock["suggestions"][0].startswith("先排除驾驶")
    assert "brake_bias_front" not in _by_id(res1)


def _scale_axle_slip(lap: LapData, axle: str, k: float) -> LapData:
    out = _copy(lap)
    braking = out.ch["brake"] > 0.05
    for w in (("fl", "fr") if axle == "front" else ("rl", "rr")):
        out.ch[f"slip_ratio_{w}"] = np.where(braking, out.ch[f"slip_ratio_{w}"] * k, out.ch[f"slip_ratio_{w}"])
    return out


def test_brake_bias_front_and_rear_synthetic(clean):
    front = tuning.diagnose(_scale_axle_slip(clean[1], "rear", 0.5))     # 后轴滑得少 = 刹车力偏前
    f = _by_id(front)["brake_bias_front"]
    assert f["severity"] == "warn" and f["category"] == "brakes" and f["data"]["bias"] == "front"
    assert f["data"]["biased_zones"] == f["data"]["zones"] >= 4
    assert f["confidence"] == "high"
    assert any("往后挪" in s for s in f["suggestions"])
    assert "勾选“我开了 ABS”" in f["caveat"]
    assert _status(front)["brake_balance"]["status"] == "found"
    rear = tuning.diagnose(_scale_axle_slip(clean[1], "front", 0.5))
    r = _by_id(rear)["brake_bias_rear"]
    assert any("往前挪" in s for s in r["suggestions"]) and "brake_bias_front" not in _by_id(rear)
    # 开着 ABS：区分度下降，置信度降一级，结论里写明
    with_abs = _by_id(tuning.diagnose(_scale_axle_slip(clean[1], "rear", 0.5), abs_on=True))["brake_bias_front"]
    assert with_abs["confidence"] == "medium" and "你勾选了“我开了 ABS”" in with_abs["caveat"]


def test_brake_lock_wording_follows_abs(issues):
    def cav(**kw):
        return _by_id(tuning.diagnose(issues[1], **kw))["brake_lock_front"]["caveat"]
    assert "请在上面勾选“我开了 ABS”" in cav()
    assert "开着 ABS 还出现" in cav(abs_on=True)
    assert "就是真的抱死" in cav(abs_on=False)


# ================================================================ 刹车压力（第 11 条）

def _pedal_lap(lap: LapData, sat_at: float | None) -> LapData:
    """把每段直线刹车改成踏板在 10%~100% 之间来回踩两遍（锯齿），刹车前 0.5 s 改成松油滑行；
    减速度 = 滑行减速（a + b·v²）+ 刹车减速。sat_at 给定时刹车减速在这个踏板位置之后不再增加（模拟压力偏大）。"""
    out = _copy(lap)
    v = out.ch["speed"]
    coast = 0.05 + 0.00004 * v ** 2
    brk, thr, gl = out.ch["brake"].copy(), out.ch["throttle"].copy(), out.ch["g_long"].copy()
    straight = (brk > 0.05) & (np.abs(out.ch["steer"]) < 0.05) & (v * 3.6 > 60)
    for i0, i1 in _runs(straight):
        k = np.arange(i1 - i0 + 1) / (i1 - i0 + 1)
        brk[i0:i1 + 1] = 0.10 + 0.899 * ((2 * k) % 1.0)
        j0 = max(0, i0 - 30)
        brk[j0:i0], thr[j0:i0], gl[j0:i0] = 0.0, 0.0, -coast[j0:i0]
    braking = 1.4 * brk if sat_at is None else 1.4 * np.minimum(brk, sat_at) / sat_at
    gl = np.where(straight, -(coast + braking), gl)
    out.ch["brake"], out.ch["throttle"], out.ch["g_long"] = brk, thr, gl
    return out


def test_brake_pressure_linear_is_ok(clean):
    res = tuning.diagnose(_pedal_lap(clean[1], None))
    st = _status(res)["brake_pressure"]
    assert st["status"] == "ok", st
    bands = res["brake_analysis"]["pedal"]["bands"]
    assert len(bands) == 2 and all(b["status"] == "linear" for b in bands)
    assert all(len(b["bins"]) >= 4 for b in bands)
    assert "已扣掉风阻和发动机制动" in st["note"]


def test_brake_pressure_saturation_found(clean):
    res = tuning.diagnose(_pedal_lap(clean[1], 0.6))
    f = _by_id(res)["brake_pressure_high"]
    assert f["severity"] == "warn" and 0.55 <= f["data"]["sat_pedal"] <= 0.75
    assert set(f["data"]["bands"]) == {"低速", "高速"}
    assert any("调小" in s for s in f["suggestions"])
    assert all(b["status"] == "saturated" for b in res["brake_analysis"]["pedal"]["bands"])
    assert _status(res)["brake_pressure"]["status"] == "found"
    # 开着 ABS：只是提示，说明多半是 ABS 介入
    fa = _by_id(tuning.diagnose(_pedal_lap(clean[1], 0.6), abs_on=True))["brake_pressure_high"]
    assert fa["severity"] == "info" and "ABS" in fa["summary"]
    # 阈值可配：饱和点要 ≤ 50% 才报 → 不报
    r2 = tuning.diagnose(_pedal_lap(clean[1], 0.6), rules={"brake_pressure": {"sat_warn_pedal": 0.5}})
    assert "brake_pressure_high" not in _by_id(r2)


def test_brake_pressure_one_foot_is_insufficient(res1):
    # 假数据的刹车基本是一脚到底：看不出饱和点，就说数据不足，不硬下结论
    st = _status(res1)["brake_pressure"]
    assert st["status"] == "skipped" and "数据不足" in st["note"]


# ================================================================ 差速器 / 出弯空转内外侧（第 11 条）

def test_wheelspin_both_wheels_with_rear_slide(res1):
    f = _by_id(res1)["wheelspin"]
    assert f["data"]["both_wheels"] and f["data"]["rear_slip_angle_rise"]
    assert not f["data"]["inner_only"] and not f["data"]["outer_only"]
    assert any("后轴 |侧偏角| 从" in e and "油门给早" in e for e in f["evidence"])


def _one_rear_spin(lap: LapData, keep: str) -> LapData:
    """T5 出弯两个后轮一起空转 → 只留 keep 这个轮子空转，另一个改回正常滑移。"""
    out = _copy(lap)
    other = "rr" if keep == "rl" else "rl"
    spin = (np.abs(out.ch[f"slip_ratio_{other}"]) > 1.0) & (out.ch["throttle"] > 0.5)
    out.ch[f"slip_ratio_{other}"] = np.where(spin, 0.3, out.ch[f"slip_ratio_{other}"])
    return out


def test_wheelspin_inner_vs_outer(issues):
    cs = {c["name"]: c for c in detect_corners(issues[1])}
    assert cs["T5"]["direction"] == "left"                   # 左弯：内侧 = 左侧轮
    inner = _by_id(tuning.diagnose(_one_rear_spin(issues[1], "rl")))["wheelspin"]
    assert inner["data"]["inner_only"] and not inner["data"]["outer_only"]
    assert any("内侧的左后" in e and "加速锁止偏低" in e for e in inner["evidence"])
    assert any("加速锁止率调高" in s for s in inner["suggestions"])
    outer = _by_id(tuning.diagnose(_one_rear_spin(issues[1], "rr")))["wheelspin"]
    assert outer["data"]["outer_only"] and not outer["data"]["inner_only"]
    assert any("外侧的右后" in e for e in outer["evidence"])
    assert not any("加速锁止率调高" in s for s in outer["suggestions"])


def _lift_mask(lap: LapData):
    cs = detect_corners(lap)
    thr, brk, glat, spd = lap["throttle"], lap["brake"], np.abs(lap["g_lat"]), lap["speed"]
    best = None
    for c in cs:
        lo = c["brake_start_m"] if c["brake_start_m"] is not None else c["start_m"]
        m = (thr <= 0.15) & (brk <= 0.10) & (glat >= 0.3) & (spd >= 8) & (lap["dist"] >= lo) & (lap["dist"] < c["apex_m"])
        for i0, i1 in _runs(m):
            if best is None or i1 - i0 > best[2] - best[1]:
                best = (c, i0, i1)
    return best


def test_diff_decel_inner_rear(clean):
    c, i0, i1 = _lift_mask(clean[1])
    assert i1 - i0 >= 12
    inner = "rr" if c["direction"] == "right" else "rl"
    lap = _copy(clean[1])
    lap.ch[f"slip_ratio_{inner}"][i0:i1 + 1] = -1.3
    res = tuning.diagnose(lap)
    f = _by_id(res)["diff_decel"]
    assert f["category"] == "differential" and f["severity"] == "warn"
    assert f["data"]["inner_events"] == 1 and f["data"]["corners"] == [c["name"]]
    assert f["locations"][0]["wheel"] == inner and f["locations"][0]["phase"] == "entry"
    assert any("减速锁止率调高" in s for s in f["suggestions"])
    assert _status(res)["differential"]["status"] == "found"
    # 两个后轮一起滑：是发动机制动太大，只提示
    other = "rl" if inner == "rr" else "rr"
    lap.ch[f"slip_ratio_{other}"][i0:i1 + 1] = -1.3
    f2 = _by_id(tuning.diagnose(lap))["diff_decel"]
    assert f2["severity"] == "info" and f2["data"]["inner_events"] == 0 and "发动机制动" in f2["summary"]


def test_awd_center_diff(issues, clean):
    res = tuning.diagnose(_copy(issues[1], drivetrain=2))
    f = _by_id(res)["awd_center_rear"]
    assert f["severity"] == "warn" and f["data"]["rear_mean"] > f["data"]["front_mean"]
    assert any("往前挪" in s for s in f["suggestions"])
    assert "四驱" in _status(res)["differential"]["note"]
    # 干净数据当四驱：不报 warn / alert
    rc = tuning.diagnose(_copy(clean[1], drivetrain=2))
    assert not [x for x in rc["findings"] if x["category"] == "differential" and x["severity"] != "info"]


def test_differential_skips_without_drivetrain(issues):
    meta = {k: v for k, v in issues[1].meta.items() if k != "drivetrain"}
    res = tuning.diagnose(LapData(meta=meta, ch=issues[1].ch))
    st = _status(res)["differential"]
    assert st["status"] == "skipped" and "驱动方式" in st["note"]


# ================================================================ 车身与悬挂（第 12 条）

def test_body_block_matches_fake_truth(res1):
    # 假数据：悬挂 lat = 0.11·g_lat、lon = 0.09·g_long（归一化），总行程 0.12 m；roll = 0.012 rad/g，pitch 刹车 0.006 rad/g
    body = res1["body"]
    assert body["available"]
    w = body["travel"]["wheels"]
    assert set(w) == {"fl", "fr", "rl", "rr"}
    for x in w.values():
        assert 0 <= x["p1"] < x["p50"] < x["p99"] <= 1
    assert body["roll"]["front_mm_per_g"] == pytest.approx(0.22 * 120, rel=0.05)
    assert body["roll"]["rear_mm_per_g"] == pytest.approx(0.22 * 120, rel=0.05)
    assert body["roll"]["front_share"] == pytest.approx(0.5, abs=0.03)
    assert body["pitch"]["dive_mm_per_g"] == pytest.approx(0.18 * 120, rel=0.10)
    assert body["pitch"]["squat_mm_per_g"] == pytest.approx(0.18 * 120, rel=0.10)
    ref = body["attitude_ref"]
    assert ref["available"] and ref["grade_fixed"]
    assert ref["roll_deg_per_g"] == pytest.approx(np.degrees(0.012), rel=0.05)
    assert ref["dive_deg_per_g"] == pytest.approx(np.degrees(0.006), rel=0.15)
    assert "倾斜" in ref["why"] and "坡度" in ref["why"]
    d = body["damper"]
    assert set(d["hist"]) == {"front", "rear"} and sum(d["hist"]["front"]) == pytest.approx(1.0, abs=1e-6)
    assert len(d["edges_mm_s"]) == len(d["hist"]["front"]) + 1
    for c in d["classes"].values():
        assert sum(c.values()) == pytest.approx(1.0, abs=1e-6)
    assert any("60 Hz" in c for c in body["caveats"])
    # 前悬埋了“太软”的问题：前轴 p99 比后轴高
    assert max(w["fl"]["p99"], w["fr"]["p99"]) > max(w["rl"]["p99"], w["rr"]["p99"])


def test_body_old_lap_without_attitude_channels(issues):
    ch = {k: v for k, v in issues[1].ch.items() if k not in ("pitch", "roll", "pitch_rate", "roll_rate")}
    res = _json(tuning.diagnose(LapData(meta=dict(issues[1].meta), ch=ch)))
    body = res["body"]
    assert body["attitude_ref"]["available"] is False and "老版本" in body["attitude_ref"]["why"]
    assert body["roll"]["front_mm_per_g"] > 0 and body["damper"] is not None


def test_body_without_susp_m_and_sign_flip(issues, res1):
    ch = {k: v for k, v in issues[1].ch.items() if not k.startswith("susp_m_")}
    body = _json(tuning.diagnose(LapData(meta=dict(issues[1].meta), ch=ch)))["body"]
    assert body["travel"] is not None and body["damper"] is None and body["roll"] is None and body["pitch"] is None
    assert any("没有悬挂行程（米）通道" in n for n in body["notes"])
    # susp_m 的正负号反过来（游戏约定不一样）：按归一化行程自动校正，结果不变
    flip = _copy(issues[1])
    for w in ("fl", "fr", "rl", "rr"):
        flip.ch[f"susp_m_{w}"] = -flip.ch[f"susp_m_{w}"]
    b2 = tuning.diagnose(flip)["body"]
    assert b2["damper"]["sign_flipped"] is True
    assert b2["damper"]["classes"]["fl"] == pytest.approx(res1["body"]["damper"]["classes"]["fl"], abs=1e-9)
    assert b2["roll"]["front_mm_per_g"] == pytest.approx(res1["body"]["roll"]["front_mm_per_g"], rel=1e-6)


def test_body_excludes_kerbs(issues):
    lap = _copy(issues[1])
    base = tuning.diagnose(lap)["body"]["travel"]["samples"]
    n = len(lap)
    lap.ch["rumble_fl"] = np.zeros(n)
    lap.ch["rumble_fl"][n // 3: n // 3 + 120] = 1                 # 2 s 压路肩
    lap.ch["susp_fl"][n // 3: n // 3 + 120] = 1.0                 # 压路肩时悬挂到底，不该算进分位数
    body = tuning.diagnose(lap)["body"]
    assert body["travel"]["samples"] <= base - 120
    assert body["travel"]["wheels"]["fl"]["p99"] < 0.99


# ================================================================ 固定弯道表（C5）/ 多圈合并

@pytest.fixture(scope="module")
def saved(tmp_path_factory, issues, clean):
    """用 LapSaver 存进库（有 track_id），再生成这条赛道的固定弯道表。"""
    store = Store(tmp_path_factory.mktemp("tuning_b2_db") / "t.db")
    saver = LapSaver(store, background=False)
    for lap in clean + issues:
        saver(_copy(lap))
    metas = sorted(store.list_laps(limit=100), key=lambda m: m["id"])
    assert len(metas) == 6 and len({m["track_id"] for m in metas}) == 1
    sectors.ensure_layout(store, metas[0]["track_id"])
    yield store, metas
    store.close()


def test_diagnose_with_layout_corners(saved):
    store, metas = saved
    lap = store.get_lap(metas[4]["id"])                    # 埋了问题的第 1 圈
    cs = sectors.layout_corners(store, lap)
    assert cs and cs[0]["name"] == "T1"
    res = tuning.diagnose(lap, corners=cs)
    assert res["summary"]["corner_source"] == "layout"
    assert any("固定弯道表" in q["text"] for q in res["data_quality"])
    by = _by_id(res)
    assert [l["corner"] for l in by["understeer_mid"]["locations"]] == ["T7"]
    assert [l["corner"] for l in by["oversteer_exit"]["locations"]] == ["T5"]
    # 用户把 T7 改了名：显示用新名字，编号（corner 字段）不变，排序不崩
    sectors.rename_corner(store, lap.meta["track_id"], "T7", "大弯")
    res2 = tuning.diagnose(lap, corners=sectors.layout_corners(store, lap))
    f = _by_id(res2)["understeer_mid"]
    assert "大弯" in f["title"] and f["locations"][0]["corner"] == "T7" and "大弯" in f["locations"][0]["label"]
    assert next(c for c in res2["corners"] if c["name"] == "T7")["label"] == "大弯"
    assert any(r["code"] == "T7" and r["label"] == "大弯" for r in res2["balance_map"]["rows"])
    sectors.rename_corner(store, lap.meta["track_id"], "T7", None)


def test_multi_pools_blocks_and_aligns_corners(saved, issues):
    store, metas = saved
    laps = [store.get_lap(m["id"]) for m in metas[3:]]
    res = _json(tuning.diagnose_many(laps, corners=[sectors.layout_corners(store, l) for l in laps], abs_on=True))
    assert res["balance_map"]["laps"] == 3 and res["balance_map"]["align_note"] is None
    assert res["body"]["laps"] == 3
    assert len(res["brake_analysis"]["zones"]) >= 18 and res["brake_analysis"]["abs_on"] is True
    assert {z["lap_id"] for z in res["brake_analysis"]["zones"]} == {m["id"] for m in metas[3:]}
    rows = {r["code"]: {c["phase"]: c for c in r["cells"]} for r in res["balance_map"]["rows"]}
    assert rows["T7"]["mid"]["tendency"] == "understeer" and rows["T5"]["exit"]["tendency"] == "oversteer"
    assert res["summary"]["assists"]["abs_on"] is True
    assert [c["id"] for c in res["checks"]] == [c for c, _ in tuning.CHECK_LIST]
    # 没有固定表：提示弯道编号可能对不上
    plain = tuning.diagnose_many(issues)
    assert "各自识别" in plain["balance_map"]["align_note"]


# ================================================================ 干净数据 / 缺通道 / 数据不足

def test_clean_data_new_checks_quiet(clean):
    for lap in clean:
        res = tuning.diagnose(lap)
        assert [(f["id"], f["severity"]) for f in res["findings"] if f["severity"] != "info"] == []
        st = _status(res)
        assert st["brake_balance"]["status"] == "ok"
        assert st["differential"]["status"] == "ok"
        assert st["brake_pressure"]["status"] != "found"
        assert not res["body"]["notes"] or not any("用满" in n for n in res["body"]["notes"])


def test_missing_channels_do_not_crash(issues):
    src = issues[1]
    for drop in (("slip_ratio_",), ("g_long",), ("susp_",), ("rumble_",), ("slip_angle_",), ("g_lat",)):
        ch = {k: v for k, v in src.ch.items() if not k.startswith(drop)}
        res = _json(tuning.diagnose(LapData(meta=dict(src.meta), ch=ch)))
        st = _status(res)
        assert set(st) == {c for c, _ in tuning.CHECK_LIST}
        if drop == ("slip_ratio_",):
            assert st["brake_balance"]["status"] == "skipped" and st["differential"]["status"] == "skipped"
        if drop == ("g_long",):
            assert st["brake_pressure"]["status"] == "skipped"
        if drop == ("susp_",):
            assert res["body"]["available"] is False
    # Sled 格式：新检查都没法判断
    ch = {k: v.copy() for k, v in src.ch.items()}
    for k in ("speed", "throttle", "brake", "gear", "steer", "rpm", "pos_x", "pos_z"):
        ch[k] = np.zeros_like(ch[k])
    res = _json(tuning.diagnose(LapData(meta={**src.meta, "fmt": "sled"}, ch=ch)))
    st = _status(res)
    assert all(st[c]["status"] == "skipped" for c in NEW_CHECKS)
    assert res["balance_map"]["available"] is False and res["brake_analysis"]["available"] is False
    assert res["body"]["available"] is True


def test_insufficient_lap_has_all_checks_and_blocks():
    res = _json(tuning.diagnose(LapData(meta={}, ch={})))
    assert [c["id"] for c in res["checks"]] == [c for c, _ in tuning.CHECK_LIST]
    assert res["balance_map"]["available"] is False and res["body"]["available"] is False
    res = _json(tuning.diagnose_many([]))
    assert res["balance_map"]["available"] is False


# ================================================================ 接口：abs / aero 参数、固定弯道表

def test_api_params_and_layout(cfg, issues, clean):
    rt = Runtime(cfg)
    saver = LapSaver(rt.store, background=False)
    for lap in clean + issues:
        saver(_copy(lap))
    ids = sorted(m["id"] for m in rt.store.list_laps(limit=100))
    client = TestClient(create_app(rt))
    r = client.get(f"/api/laps/{ids[4]}/diagnose", params={"abs": "1", "aero": "0"})
    assert r.status_code == 200
    body = r.json()
    assert body["summary"]["assists"] == {"abs_on": True, "aero": False}
    assert body["summary"]["corner_source"] == "layout"             # 第一次诊断时自动生成了固定弯道表
    assert sectors.get_layout(rt.store, body["lap"]["track_id"]) is not None
    assert body["brake_analysis"]["abs_on"] is True
    assert client.get(f"/api/laps/{ids[4]}/diagnose").json()["summary"]["assists"] == {"abs_on": None, "aero": None}
    r = client.get(f"/api/laps/{ids[4]}/diagnose", params={"abs": "maybe"})
    assert r.status_code == 400 and "abs" in r.json()["detail"]
    r = client.get("/api/diagnose", params={"ids": ",".join(map(str, ids[3:])), "aero": "1"})
    assert r.status_code == 200
    m = r.json()
    assert m["summary"]["assists"]["aero"] is True and m["balance_map"]["align_note"] is None
    assert all(row["corner_source"] == "layout" for row in m["laps"])
    rules = client.get("/api/tuning/rules").json()["rules"]
    assert {"brake_balance", "brake_pressure", "differential", "balance_map", "body"} <= set(rules)
    rt.store.close()
