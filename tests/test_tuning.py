"""调车诊断 × 假数据：埋好的每个问题都要按正确的弯 / 阶段 / 轮子找出来，干净数据不能报 warn / alert。"""
import json

import numpy as np
import pytest
from fastapi.testclient import TestClient

from forza import tuning
from forza.app import create_app
from forza.fake import write_fake_recording
from forza.hub import Hub
from forza.lapdata import LapData
from forza.laps import LapDetector
from forza.replay import import_file
from forza.runtime import Runtime

SEV = {"info": 0, "warn": 1, "alert": 2}


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
    p = tmp_path_factory.mktemp("tuning") / "issues.frec"
    truth = write_fake_recording(p, "fm2023_dash", laps=3, issues=True, seed=11)
    laps = _split(p)
    assert [l.meta["lap_number"] for l in laps] == [0, 1, 2]
    return laps, truth


@pytest.fixture(scope="module")
def clean(tmp_path_factory):
    p = tmp_path_factory.mktemp("tuning") / "clean.frec"
    write_fake_recording(p, "fm2023_dash", laps=3, issues=False, seed=12)
    return _split(p)


@pytest.fixture(scope="module")
def flying_results(issues):
    laps, _ = issues
    return {l.meta["lap_number"]: tuning.diagnose(l) for l in laps}


def _by_id(res):
    return {f["id"]: f for f in res["findings"]}


def _occ(truth, key, lap_number):
    return [o for o in truth["issues"][key]["occurrences"] if o["lap_number"] == lap_number]


def _overlaps(loc, lo, hi, tol=5.0):
    return loc["dist_m"] <= hi + tol and loc["end_m"] >= lo - tol


def _assert_finding_shape(f):
    for k in ("id", "category", "severity", "title", "summary", "evidence", "locations", "basis", "suggestions",
              "confidence"):
        assert k in f, k
    assert f["category"] in ("balance", "suspension", "tires", "traction", "brakes", "differential", "gearing")
    assert f["severity"] in ("info", "warn", "alert")
    assert f["confidence"] in ("low", "medium", "high")
    assert f["evidence"] and all(isinstance(e, str) for e in f["evidence"])
    assert set(f["basis"]) >= {"channels", "rule", "principle"}
    assert f["suggestions"]
    for loc in f["locations"]:
        assert set(loc) >= {"dist_m", "corner", "phase", "wheel"}


# ---------------------------------------------------------------- 埋的问题（每个飞驰圈都要找到）

@pytest.mark.parametrize("lap_no", [1, 2])
def test_understeer_t7_mid(issues, flying_results, lap_no):
    _, truth = issues
    f = _by_id(flying_results[lap_no])["understeer_mid"]
    _assert_finding_shape(f)
    assert SEV[f["severity"]] >= SEV["warn"]
    assert [(l["corner"], l["phase"], l["wheel"]) for l in f["locations"]] == [("T7", "mid", "front")]
    o = _occ(truth, "understeer", lap_no)[0]
    assert _overlaps(f["locations"][0], o["lap_dist_start_m"], o["lap_dist_end_m"])
    assert f["data"]["saturated_axle_mean"] > 1.0 > f["data"]["other_axle_mean"]
    # 证据里要有实测数字和阈值
    assert any("1.00" in e and "0.30" in e for e in f["evidence"])


@pytest.mark.parametrize("lap_no", [1, 2])
def test_oversteer_t5_exit(issues, flying_results, lap_no):
    _, truth = issues
    f = _by_id(flying_results[lap_no])["oversteer_exit"]
    _assert_finding_shape(f)
    assert SEV[f["severity"]] >= SEV["warn"]
    assert [(l["corner"], l["phase"], l["wheel"]) for l in f["locations"]] == [("T5", "exit", "rear")]
    o = _occ(truth, "oversteer_exit", lap_no)[0]
    assert _overlaps(f["locations"][0], o["lap_dist_start_m"], o["lap_dist_end_m"])


@pytest.mark.parametrize("lap_no", [1, 2])
def test_wheelspin_t5_exit(issues, flying_results, lap_no):
    _, truth = issues
    f = _by_id(flying_results[lap_no])["wheelspin"]
    _assert_finding_shape(f)
    assert SEV[f["severity"]] >= SEV["warn"]
    assert [(l["corner"], l["phase"], l["wheel"]) for l in f["locations"]] == [("T5", "exit", "rear")]
    o = next(x for x in truth["issues"]["oversteer_exit"]["wheelspin"]["occurrences"] if x["lap_number"] == lap_no)
    assert _overlaps(f["locations"][0], o["lap_dist_start_m"], o["lap_dist_end_m"])
    assert f["data"]["both_wheels"] is True


@pytest.mark.parametrize("lap_no", [1, 2])
def test_bottoming_front_on_straight(issues, flying_results, lap_no):
    _, truth = issues
    by = _by_id(flying_results[lap_no])
    f = by["bottoming_front"]
    _assert_finding_shape(f)
    assert SEV[f["severity"]] >= SEV["warn"]
    assert [(l["corner"], l["phase"], l["wheel"]) for l in f["locations"]] == [(None, "straight", "front")]
    o = _occ(truth, "bottoming", lap_no)[0]
    assert _overlaps(f["locations"][0], o["lap_dist_start_m"], o["lap_dist_end_m"])
    assert "bottoming_rear" not in by and "kerb_strike" not in by   # 坑不是路肩


@pytest.mark.parametrize("lap_no", [1, 2])
def test_front_lock_into_t1(issues, flying_results, lap_no):
    _, truth = issues
    by = _by_id(flying_results[lap_no])
    f = by["brake_lock_front"]
    _assert_finding_shape(f)
    assert SEV[f["severity"]] >= SEV["warn"]
    assert [(l["corner"], l["phase"], l["wheel"]) for l in f["locations"]] == [("T1", "entry", "front")]
    o = _occ(truth, "front_lock", lap_no)[0]
    assert _overlaps(f["locations"][0], o["lap_dist_start_m"], o["lap_dist_end_m"])
    assert f["data"]["first_count"] == 1
    assert "brake_lock_rear" not in by


@pytest.mark.parametrize("lap_no", [1, 2])
def test_rev_limiter_top_gear(issues, flying_results, lap_no):
    _, truth = issues
    f = _by_id(flying_results[lap_no])["rev_limiter_top"]
    _assert_finding_shape(f)
    assert SEV[f["severity"]] >= SEV["warn"]
    assert f["data"]["top_gear"] == 6
    lo, hi = truth["issues"]["rev_limiter"]["dist_range_m"]
    assert _overlaps(f["locations"][0], lo, hi)
    assert f["locations"][0]["phase"] == "straight"


@pytest.mark.parametrize("lap_no", [0, 1, 2])
def test_tire_imbalance_front_fl_hotter(flying_results, lap_no):
    res = flying_results[lap_no]
    by = _by_id(res)
    f = by["tire_lr_front"]
    _assert_finding_shape(f)
    assert SEV[f["severity"]] >= SEV["warn"]
    assert f["data"]["hot_wheel"] == "fl" and f["locations"][0]["wheel"] == "fl"
    # 赛道以右弯为主要写进证据，避免把“左边天然更热”误判
    assert res["summary"]["turn_share"]["right"] > 0.6
    assert any("右弯为主" in e for e in f["evidence"])
    assert "tire_lr_rear" not in by or by["tire_lr_rear"]["severity"] == "info"


def test_first_lap_notes(flying_results):
    res = flying_results[0]
    texts = " ".join(q["text"] for q in res["data_quality"])
    assert "第一圈" in texts
    assert "rev_limiter_top" not in _by_id(res)   # 站立起步的第一圈没撞断油
    assert "top_gear_headroom" not in _by_id(res)


# ---------------------------------------------------------------- 误报保护

def test_clean_data_has_no_warnings(clean):
    for lap in clean:
        res = tuning.diagnose(lap)
        bad = [(f["id"], f["severity"]) for f in res["findings"] if f["severity"] != "info"]
        assert bad == [], (lap.meta["lap_number"], bad)
        status = {c["id"]: c["status"] for c in res["checks"]}
        for cid in ("balance", "suspension", "tires", "traction", "brakes"):
            assert status[cid] == "ok", (cid, res["checks"])


# ---------------------------------------------------------------- 输出格式 / Data Out 判断不了的

def test_json_safe_and_not_available(flying_results):
    res = flying_results[1]
    s = json.dumps(res, ensure_ascii=False, allow_nan=False)
    assert "NaN" not in s
    items = [x["item"] for x in res["not_available"]]
    assert "胎面内/中/外温度" in items
    camber = next(x for x in res["not_available"] if x["item"] == "胎面内/中/外温度")
    assert "外倾角" in camber["why"] and "遥测" in camber["instead"]
    sev = [SEV[f["severity"]] for f in res["findings"]]
    assert sev == sorted(sev, reverse=True)
    for f in res["findings"]:
        _assert_finding_shape(f)


# ---------------------------------------------------------------- 数据不足

def _slice(lap: LapData, n: int) -> LapData:
    return LapData(meta=dict(lap.meta), ch={k: v[:n].copy() for k, v in lap.ch.items()})


def _assert_insufficient(res):
    json.dumps(res, ensure_ascii=False, allow_nan=False)
    assert res["usable"] is False
    assert res["findings"] == []
    assert any("数据不足" in q["text"] for q in res["data_quality"])
    assert all(c["status"] == "skipped" for c in res["checks"])
    assert res["not_available"]


def test_short_lap_is_insufficient(issues):
    laps, _ = issues
    _assert_insufficient(tuning.diagnose(_slice(laps[1], 150)))


def test_empty_lap_does_not_crash():
    _assert_insufficient(tuning.diagnose(LapData(meta={}, ch={})))
    _assert_insufficient(tuning.diagnose_many([]))


def test_sled_like_lap_only_checks_suspension(issues):
    laps, _ = issues
    src = laps[1]
    ch = {k: v.copy() for k, v in src.ch.items()}
    for k in ("speed", "throttle", "brake", "gear", "steer", "rpm", "pos_x", "pos_z"):
        ch[k] = np.zeros_like(ch[k])
    for w in ("fl", "fr", "rl", "rr"):
        ch[f"tire_temp_{w}"] = np.full_like(ch[f"tire_temp_{w}"], -17.78)
    res = tuning.diagnose(LapData(meta={**src.meta, "fmt": "sled"}, ch=ch))
    json.dumps(res, ensure_ascii=False, allow_nan=False)
    assert any("Sled" in q["text"] and "数据不足" in q["text"] for q in res["data_quality"])
    status = {c["id"]: c["status"] for c in res["checks"]}
    assert status["suspension"] == "found"
    assert all(status[c] == "skipped" for c in ("balance", "tires", "traction", "brakes", "gearing"))
    assert {f["category"] for f in res["findings"]} == {"suspension"}


def test_missing_channels_old_data(issues):
    laps, _ = issues
    src = laps[1]
    ch = {k: v for k, v in src.ch.items() if not k.startswith(("slip_angle_", "tire_temp_"))}
    res = tuning.diagnose(LapData(meta=dict(src.meta), ch=ch))
    status = {c["id"]: c["status"] for c in res["checks"]}
    assert status["balance"] == "skipped" and status["tires"] == "skipped"
    assert "brake_lock_front" in _by_id(res)


def test_unknown_drivetrain_and_no_max_rpm(issues):
    laps, _ = issues
    meta = {k: v for k, v in laps[1].meta.items() if k not in ("drivetrain", "engine_max_rpm")}
    res = tuning.diagnose(LapData(meta=meta, ch=laps[1].ch))
    status = {c["id"]: c for c in res["checks"]}
    assert status["gearing"]["status"] == "skipped" and "数据不足" in status["gearing"]["note"]
    assert "wheelspin" in _by_id(res)
    assert any("驱动方式" in q["text"] for q in res["data_quality"])


# ---------------------------------------------------------------- 多圈合并

def test_multi_lap_repeat_raises_confidence(issues, flying_results):
    laps, _ = issues
    res = tuning.diagnose_many(laps)
    json.dumps(res, ensure_ascii=False, allow_nan=False)
    assert res["mode"] == "multi" and len(res["laps"]) == 3
    by = _by_id(res)
    us = by["understeer_mid"]
    assert us["repeat"] == {"laps": 3, "of": 3, "lap_labels": ["第 0 圈", "第 1 圈", "第 2 圈"]}
    assert len(us["locations"]) == 3 and all(l["corner"] == "T7" for l in us["locations"])
    single = _by_id(flying_results[1])["brake_lock_front"]["confidence"]
    assert tuning.CONF_LEVELS.index(by["brake_lock_front"]["confidence"]) == \
        min(2, tuning.CONF_LEVELS.index(single) + 1)
    assert by["rev_limiter_top"]["repeat"]["laps"] == 2   # 第一圈站立起步没撞断油
    assert res["summary"]["reference_lap"] == "第 1 圈"


def test_multi_lap_sporadic_downgrades(issues, clean):
    laps, _ = issues
    res = tuning.diagnose_many([laps[1], clean[1], clean[2]])
    us = _by_id(res)["understeer_mid"]
    assert us["repeat"]["laps"] == 1
    assert us["severity"] == "warn"            # 单圈是 alert，偶发降一级
    assert "偶发" in us["evidence"][0]


# ---------------------------------------------------------------- 阈值文件

def test_rules_file_matches_defaults_and_fallbacks(tmp_path):
    rules, src, err = tuning.load_rules(tuning.RULES_PATH)
    assert err is None and src and rules == tuning.DEFAULT_RULES   # toml 和内置默认值保持一致
    rules, src, err = tuning.load_rules(tmp_path / "nope.toml")
    assert rules == tuning.DEFAULT_RULES and src is None and err is None
    bad = tmp_path / "bad.toml"
    bad.write_text("[balance\ngrip_limit = ", encoding="utf-8")
    rules, src, err = tuning.load_rules(bad)
    assert rules == tuning.DEFAULT_RULES and "读取失败" in err


def test_custom_rules_change_result(issues):
    laps, _ = issues
    res = tuning.diagnose(laps[1], rules={"balance": {"grip_limit": 1.5}})
    assert "understeer_mid" not in _by_id(res)
    assert "brake_lock_front" in _by_id(res)


# ---------------------------------------------------------------- API

def test_api_endpoints(cfg, issues):
    laps, _ = issues
    rt = Runtime(cfg)
    ids = [rt.store.save_lap(LapData(meta=dict(l.meta), ch=l.ch)) for l in laps]
    client = TestClient(create_app(rt))

    r = client.get(f"/api/laps/{ids[1]}/diagnose")
    assert r.status_code == 200
    body = r.json()
    assert body["lap"]["id"] == ids[1] and body["lap_label"] == f"#{ids[1]} 第 1 圈"
    assert {"understeer_mid", "oversteer_exit", "wheelspin", "bottoming_front", "brake_lock_front",
            "rev_limiter_top", "tire_lr_front"} <= {f["id"] for f in body["findings"]}

    r = client.get("/api/laps/99999/diagnose")
    assert r.status_code == 404 and "找不到" in r.json()["detail"]

    r = client.get("/api/diagnose", params={"ids": ",".join(map(str, ids))})
    assert r.status_code == 200 and r.json()["mode"] == "multi" and len(r.json()["laps"]) == 3
    assert client.get("/api/diagnose", params={"ids": "1,x"}).status_code == 400
    assert client.get("/api/diagnose", params={"ids": f"{ids[0]},99999"}).status_code == 404

    r = client.get("/api/tuning/rules")
    assert r.status_code == 200 and r.json()["rules"]["balance"]["grip_limit"] == 1.0

    r = client.get(f"/api/tuning/outline/{ids[1]}")
    o = r.json()
    assert r.status_code == 200 and 50 <= len(o["pos_x"]) == len(o["pos_z"]) == len(o["dist"]) <= 601

    for page in ("/tuning.html", "/tuning.js", "/tuning.css"):
        assert client.get(page).status_code == 200
    rt.store.close()
