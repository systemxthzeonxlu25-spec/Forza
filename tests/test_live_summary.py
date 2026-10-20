"""本圈小结：新圈存好后在后台算 compare，结果作为 last_summary 跟着每条实时消息下发。"""
import json
import logging

import pytest
from fastapi.testclient import TestClient

import forza.live as live_mod
from forza.app import create_app
from forza.fake import write_fake_recording
from forza.packet import encode
from forza.replay import import_file
from forza.runtime import Runtime
from tests.helpers import CircleStream


def _strict(text: str) -> dict:
    def bad(c):
        raise AssertionError(f"消息里出现了 {c}")
    return json.loads(text, parse_constant=bad)


def _settle(rt):
    rt.saver.wait()
    rt.live.wait_idle()


@pytest.fixture
def rt(cfg):
    r = Runtime(cfg)
    yield r
    r.shutdown()


@pytest.fixture(scope="module")
def fake_rec(tmp_path_factory):
    p = tmp_path_factory.mktemp("sum") / "demo.frec"
    truth = write_fake_recording(p, "fm2023_dash", laps=4, seed=1)
    return p, truth


def test_summary_after_each_saved_lap(rt, fake_rec):
    """假数据：第 2 圈（lap_number=2）T3 刹车早、弯心慢。小结要点名 T3，第 0 圈注明起步圈。"""
    path, _ = fake_rec
    import_file(path, rt.hub)
    rt.detector.flush()
    _settle(rt)
    ids = rt.saver.saved_ids
    assert len(ids) >= 3
    items = rt.live.recent_summaries()
    assert [s["id"] for s in items] == list(reversed(ids))          # 每圈一条，新的在前
    assert rt.live.last_summary["id"] == ids[-1]

    by_num = {s["lap_number"]: s for s in items}
    first = by_num[0]
    assert first["first_lap"] is True and "起步圈" in (first["note"] or "")
    assert first["ref"] is None and "第一个完整圈" in first["summary"]   # 第一圈存的时候还没有别的圈

    slow = by_num[2]
    assert slow["ref"] is not None and slow["ref"]["id"] != slow["id"]
    assert slow["lap_time_delta_s"] is not None and slow["lap_time_delta_s"] > 0.2
    assert slow["summary"].startswith("比参考圈慢")
    assert 1 <= len(slow["top"]) <= live_mod.SUMMARY_TOP_N
    assert slow["top"][0]["name"] == "T3", slow["top"]
    assert slow["top"][0]["explanation"].startswith("T3：慢")
    for t in slow["top"]:
        assert t["time_delta_s"] >= live_mod.SUMMARY_MIN_LOSS_S
    assert slow["first_lap"] is False and slow["track_name"]


def test_summary_rides_on_every_message_and_cache_invalidates(rt):
    s = CircleStream()
    for f in s.run_dist(s.lap_len * 0.3):
        rt.hub.feed(encode(f, "fm2023_dash"))
    live = rt.live
    t1 = _strict(live.message_text())
    assert t1["last_summary"] is None
    assert _strict(live.status_text())["last_summary"] is None
    # 没有新帧（seq 不变），小结变了：缓存必须失效
    live.summaries.append({"id": 42, "summary": "x"})
    live.last_summary = live.summaries[-1]
    live.invalidate()
    t2 = _strict(live.message_text())
    assert t2["seq"] == t1["seq"] and t2["last_summary"]["id"] == 42
    st = _strict(live.status_text())
    assert st["type"] == "status" and st["last_summary"]["id"] == 42


def test_websocket_pushes_new_summary_without_new_frames(rt):
    s = CircleStream()
    for f in s.run(1.0):
        rt.hub.feed(encode(f, "fm2023_dash"))
    client = TestClient(create_app(rt))
    with client.websocket_connect("/ws/live?hz=30") as ws:
        first = _strict(ws.receive_text())
        assert first["type"] == "frame" and first["last_summary"] is None
        rt.live.summaries.append({"id": 7, "summary": "比参考圈慢 0.1 s"})
        rt.live.last_summary = rt.live.summaries[-1]
        rt.live.invalidate()
        nxt = _strict(ws.receive_text())             # 不等定时 status：内容变了立刻再发一条
        assert nxt["last_summary"]["id"] == 7 and nxt["seq"] == first["seq"]


def test_listener_errors_do_not_count_as_save_failure(rt, monkeypatch, caplog):
    def boom(*a, **k):
        raise RuntimeError("小结模块炸了")
    monkeypatch.setattr(rt.live, "_submit", boom)
    s = CircleStream()
    with caplog.at_level(logging.INFO):
        for f in s.run_dist(s.lap_len * 2.5):
            rt.hub.feed(encode(f, "fm2023_dash"))
        rt.detector.flush()
        _settle(rt)
    assert len(rt.saver.saved_ids) == 2
    assert "存档失败" not in caplog.text and "处理新存档的圈出错" in caplog.text


def test_summary_survives_compare_crash(rt, monkeypatch):
    from forza import analysis

    def broken(*a, **k):
        raise KeyError("对比模块改坏了")
    monkeypatch.setattr(analysis, "compare", broken)
    s = CircleStream()
    for f in s.run_dist(s.lap_len * 2.5):
        rt.hub.feed(encode(f, "fm2023_dash"))
    rt.detector.flush()
    _settle(rt)
    last = rt.live.last_summary
    assert last is not None and last["id"] == rt.saver.saved_ids[-1]
    assert last["ref"] is not None and "对比出错" in last["summary"] and last["top"] == []


def test_summaries_api(rt):
    s = CircleStream()
    for f in s.run_dist(s.lap_len * 3.5):
        rt.hub.feed(encode(f, "fm2023_dash"))
    _settle(rt)
    client = TestClient(create_app(rt))
    r = client.get("/api/live/summaries")
    assert r.status_code == 200
    items = _strict(r.text)["items"]
    assert [i["id"] for i in items] == list(reversed(rt.saver.saved_ids))
    # 恒速圆周：后面的圈和参考圈几乎一样快
    later = items[0]
    assert later["ref"] is not None and abs(later["lap_time_delta_s"]) < 0.05
    assert "基本一样快" in later["summary"]
    snap = _strict(client.get("/api/live/snapshot").text)
    assert snap["last_summary"]["id"] == items[0]["id"]


def test_incomplete_sprint_summary_has_no_compare(cfg):
    from tests.test_laps import _sprint_run

    rt = Runtime(cfg)
    try:
        s = CircleStream(radius=2000, family="fh", fmt="fh", game="fh5", position=3)
        frames = _sprint_run(s, 3000, True) + _sprint_run(s, 1500, False)
        for f in frames:
            rt.hub.feed(encode(f, "fh"))
        rt.detector.flush()
        _settle(rt)
        items = rt.live.recent_summaries()
        assert [i["kind"] for i in items] == ["sprint", "sprint"]
        assert items[0]["is_complete"] is False and "没跑完" in items[0]["summary"]
        assert items[1]["first_lap"] is False          # 冲刺赛本来就是静止起步，不标“起步圈”
    finally:
        rt.shutdown()


def test_pre_save_hook_tags_meta(rt, monkeypatch):
    """存档前钩子（给调校版本模块用）：能往新圈 meta 里写字段，钩子出错不影响存档。"""
    import forza.runtime as runtime_mod

    monkeypatch.setattr(runtime_mod, "PRE_SAVE_HOOKS", [])

    def tag(_rt, lap):
        lap.meta["setup_id"] = 3
        lap.meta["setup_name"] = "后防倾杆 -2"

    def broken(_rt, lap):
        raise ValueError("钩子出错")
    runtime_mod.register_pre_save_hook(broken)
    runtime_mod.register_pre_save_hook(tag)
    runtime_mod.register_pre_save_hook(tag)            # 重复注册只算一次
    assert len(runtime_mod.PRE_SAVE_HOOKS) == 2
    s = CircleStream()
    for f in s.run_dist(s.lap_len * 2.5):
        rt.hub.feed(encode(f, "fm2023_dash"))
    rt.detector.flush()
    _settle(rt)
    laps = rt.store.list_laps()
    assert len(laps) == 2 and all(l["setup_id"] == 3 and l["setup_name"] == "后防倾杆 -2" for l in laps)
    assert rt.live.last_summary["setup_name"] == "后防倾杆 -2"
