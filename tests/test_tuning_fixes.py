"""调车诊断审查修复的回归测试：

- 车速分档边界不取整（一大簇几乎恒定的车速正好落在分位数上时不能整块落进错误的档），以及档位饿死时的保护；
- 拉手刹 / 踩离合的采样不算进差速器、刹车平衡、刹车抱死、刹车压力、驱动轮空转（结论会归错原因）。
"""
import copy

import numpy as np
import pytest

from forza import tuning
from forza.fake import write_fake_recording
from forza.hub import Hub
from forza.lapdata import LapData
from forza.laps import LapDetector
from forza.replay import import_file


def _split(path):
    hub = Hub()
    det = LapDetector()
    laps = []
    det.add_listener(laps.append)
    hub.subscribe(det.on_frame)
    import_file(path, hub)
    det.flush()
    return laps


def _copy(lap: LapData) -> LapData:
    return LapData(meta=copy.deepcopy(lap.meta), ch={k: v.copy() for k, v in lap.ch.items()})


def _by_id(res):
    return {f["id"]: f for f in res["findings"]}


def _status(res):
    return {c["id"]: c for c in res["checks"]}


@pytest.fixture(scope="module")
def sprint(tmp_path_factory):
    p = tmp_path_factory.mktemp("tuning_fix") / "sprint.frec"
    write_fake_recording(p, "fh", "sprint", laps=1)
    return _split(p)


@pytest.fixture(scope="module")
def clean(tmp_path_factory):
    p = tmp_path_factory.mktemp("tuning_fix") / "clean.frec"
    write_fake_recording(p, "fm2023_dash", laps=3, issues=False, seed=12)
    return _split(p)


# ================================================================ 车速分档边界

def test_speed_edges_keep_raw_quantiles():
    # 29% 的采样正好是 191.6026474 km/h（顶着断油的高速弯），67% 分位数落在这簇里
    rng = np.random.default_rng(3)
    s = np.concatenate([rng.uniform(40, 80, 330), rng.uniform(85, 190, 290), np.full(290, 191.6026474),
                        rng.uniform(192.5, 230, 90)])
    edges = tuning._speed_edges(s, [0.33, 0.67], [])
    assert edges == pytest.approx([float(np.quantile(s, 0.33)), float(np.quantile(s, 0.67))])
    assert edges[1] != round(edges[1])                     # 没被取整成 192（取整会把整簇推到中速档）
    hi = tuning._band_defs(edges)[-1]
    assert tuning._band_mask(s, hi).mean() > 0.3           # 整簇留在高速档
    assert hi["range"] == "> 192 km/h"                     # 显示文字仍是整数
    # 固定边界、空数组、全部同一个车速都不出错
    assert tuning._speed_edges(s, [0.5], [160, 100]) == [100.0, 160.0]
    assert tuning._speed_edges(np.array([]), [0.5], []) == []
    assert tuning._speed_edges(np.full(50, 100.0), [0.33, 0.67], []) == [100.0]


def test_speed_edges_rebalance_starved_band():
    # 大量完全相同的车速骑在 67% 分位数上：按 ≥ 下界切，中速档只剩 7%（预期 34%）→ 边界挪到附近最大的车速间隙
    rng = np.random.default_rng(1)
    s = np.concatenate([rng.uniform(40, 80, 330), rng.uniform(90, 140, 70), np.full(400, 150.0),
                        rng.uniform(170, 230, 200)])
    raw = [float(np.quantile(s, q)) for q in (0.33, 0.67)]
    ss = np.sort(s)
    assert tuning._band_shares(ss, raw)[1] < 0.1
    edges = tuning._speed_edges(s, [0.33, 0.67], [])
    shares = tuning._band_shares(ss, edges)
    assert all(shares >= 0.5 * np.array([0.33, 0.34, 0.33]))
    assert 150 < edges[1] < 170                            # 落在 150 那簇和 170 之间的空档里，没劈开那簇
    # 连续分布的数据不触发保护：边界就是原始分位数
    u = rng.uniform(40, 230, 3000)
    assert tuning._speed_edges(u, [0.33, 0.67], []) == pytest.approx([float(np.quantile(u, q)) for q in (0.33, 0.67)])


def test_sprint_high_speed_cluster_lands_in_high_band(sprint):
    # 审查复现：冲刺赛第 3 段（完整）里 T4 高速弯 191.6 km/h 撞断油，占转弯采样近三成
    runs = [l for l in sprint if float(l.meta.get("distance_m") or 0) > 3000]
    assert len(runs) == 2
    for lap in runs + [l for l in sprint if l not in runs]:
        bm = tuning.diagnose(lap)["balance_map"]
        assert bm["available"]
        lo, hi = bm["bands"][0], bm["bands"][-1]
        assert hi["ok"] and hi["tendency"] == "understeer", [b["range"] for b in bm["bands"]]
        assert lo["tendency"] == "neutral"
        assert any("只在高速段推头" in n for n in bm["notes"])
        # 三档的转弯时间不会有一档被挤得只剩一点点
        times = [b["time_s"] for b in bm["bands"]]
        assert min(times) >= 0.5 * sum(times) / len(times) * 0.5


# ================================================================ 手刹 / 离合

def _lift_segment(lap: LapData, code: str):
    """某个弯的入弯松刹段（和 _check_differential 同样的条件）+ 这个弯的内侧后轮。"""
    res, ctx = tuning._run(lap, tuning._rules_info(None), None, None, None)
    r = ctx.r["differential"]
    lift = ((ctx.ch("throttle") <= r["lift_max_throttle"]) & (ctx.ch("brake") <= r["lift_max_brake"])
            & (np.abs(ctx.ch("g_lat")) >= r["min_lat_g"]) & ctx.speed_ok)
    c = next(c for c in ctx.corners if c["name"] == code)
    lo = min(c["brake_start_m"] if c.get("brake_start_m") is not None else c["start_m"], c["start_m"])
    seg = lift & (ctx.dist >= lo) & (ctx.dist < c["apex_m"])
    return seg, ("rl" if c["direction"] == "left" else "rr")


def _drag_inner_rear(lap: LapData, seg, inner, **pedals) -> LapData:
    out = _copy(lap)
    out.ch[f"slip_ratio_{inner}"] = np.where(seg, -1.3, out.ch[f"slip_ratio_{inner}"]).astype(out.ch["throttle"].dtype)
    for name, v in pedals.items():
        out.ch[name] = np.where(seg, v, 0.0).astype(out.ch["throttle"].dtype)
    return out


def test_differential_ignores_handbrake_and_clutch(clean):
    lap = clean[2]
    assert tuning.diagnose(lap)["checks"]                      # 干净圈本身没有 diff_decel
    assert "diff_decel" not in _by_id(tuning.diagnose(lap))
    seg, inner = _lift_segment(lap, "T5")
    assert seg.sum() > 50
    # 对照：没拉手刹时内侧后轮被拖滑 → 报减速锁止偏低（说明这个构造确实会触发）
    base = tuning.diagnose(_drag_inner_rear(lap, seg, inner))
    assert _by_id(base)["diff_decel"]["severity"] == "warn"
    # 拉着手刹 / 踩着离合：滑移是司机操作造成的，不能说成差速器的问题
    for pedal in ("handbrake", "clutch"):
        res = tuning.diagnose(_drag_inner_rear(lap, seg, inner, **{pedal: 1.0}))
        assert "diff_decel" not in _by_id(res), pedal
        note = _status(res)["differential"]["note"]
        assert ("拉手刹" if pedal == "handbrake" else "踩离合") in note and "不计" in note
    # 两个后轮一起被手刹拖住：也不能说“发动机制动整体太大”
    both = _drag_inner_rear(lap, seg, inner, handbrake=1.0)
    other = "rr" if inner == "rl" else "rl"
    both.ch[f"slip_ratio_{other}"] = np.where(seg, -1.3, both.ch[f"slip_ratio_{other}"]).astype(both.ch["throttle"].dtype)
    assert "diff_decel" not in _by_id(tuning.diagnose(both))
    # 老圈没有手刹 / 离合通道：照常判断，不崩
    old = _drag_inner_rear(lap, seg, inner)
    old.ch.pop("handbrake", None)
    old.ch.pop("clutch", None)
    assert "diff_decel" in _by_id(tuning.diagnose(old))


def test_straight_braking_ignores_handbrake(clean):
    lap = clean[2]
    brk, steer, spd = lap.ch["brake"], np.abs(lap.ch["steer"]), np.abs(lap.ch["speed"]) * 3.6
    zone = (brk >= 0.5) & (steer < 0.05) & (spd > 60)
    assert zone.sum() > 30
    dragged = _copy(lap)
    for w in ("rl", "rr"):
        dragged.ch[f"slip_ratio_{w}"] = np.where(zone, -1.3, dragged.ch[f"slip_ratio_{w}"]).astype(brk.dtype)
    # 对照：后轮滑移大 → 刹车平衡偏后 + 后轮抱死
    base = _by_id(tuning.diagnose(dragged))
    assert "brake_bias_rear" in base and "brake_lock_rear" in base
    # 同样的滑移但是拉着手刹：不算刹车平衡 / 抱死的问题
    hb = _copy(dragged)
    hb.ch["handbrake"] = np.where(zone, 1.0, 0.0).astype(brk.dtype)
    res = tuning.diagnose(hb)
    got = _by_id(res)
    assert "brake_bias_rear" not in got and "brake_lock_rear" not in got
    st = _status(res)
    assert "拉手刹" in st["brake_balance"]["note"] and "拉手刹" in st["brakes"]["note"]
    assert "拉手刹" in st["brake_pressure"]["note"]
    # 踏板 - 减速度的采样也去掉了手刹那部分
    _, c0 = tuning._run(lap, tuning._rules_info(None), None, None, None)
    _, c1 = tuning._run(hb, tuning._rules_info(None), None, None, None)
    assert c1.samples["brake_pedal"]["pedal"].size < c0.samples["brake_pedal"]["pedal"].size


def test_traction_ignores_handbrake(clean):
    lap = clean[2]
    on = (lap.ch["throttle"] >= 0.6) & (lap.ch["brake"] < 0.05) & (np.abs(lap.ch["speed"]) > 15)
    idx = np.flatnonzero(on)[:40]
    m = np.zeros(len(lap), bool)
    m[idx] = True
    spin = _copy(lap)
    for w in ("rl", "rr"):
        spin.ch[f"slip_ratio_{w}"] = np.where(m, -1.5, spin.ch[f"slip_ratio_{w}"]).astype(lap.ch["throttle"].dtype)
    assert "wheelspin" in _by_id(tuning.diagnose(spin))
    spin.ch["handbrake"] = np.where(m, 1.0, 0.0).astype(lap.ch["throttle"].dtype)
    res = tuning.diagnose(spin)
    assert "wheelspin" not in _by_id(res)
    assert "拉手刹" in _status(res)["traction"]["note"]
