"""第三轮审查修复的回归测试：

- 中途接入的半截圈：不建假赛道、不污染暂定圈长
- 暂停超过 30 秒：只要还在收暂停帧，这一圈接着记；刚过线就暂停，挂起圈按墙钟落盘
- 连续暂停太久 / 完全收不到包：结束当前比赛
- 状态接口不和收包线程抢数据（路由是 async def，pps 先拷贝再数）
- 配置校验：类型、取值范围、转发回自己端口
- 跨站写请求被拦
- 删圈 / 改完整标记后重算赛道参考圈长
- 录像写失败时状态里能看到原因
"""
import inspect
import shutil
from unittest import mock

import pytest
from fastapi.testclient import TestClient

from forza import laps as laps_mod
from forza.app import create_app
from forza.config import Config, ConfigError
from forza.hub import Hub
from forza.laps import LapDetector, LapSaver
from forza.runtime import Runtime
from forza.storage import Store
from tests.helpers import CircleStream


class _Clock:
    """假的 monotonic 时钟：跟着帧的墙钟走，tick() 判断空闲时用。"""

    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


def _feed(det, frames, clock, tick=False):
    for f in frames:
        clock.t = f.recv_time
        det.on_frame(f)
        if tick:
            det.tick()


# ---------------------------------------------------------------- 中途接入

def test_partial_start_lap_does_not_create_bogus_track():
    """比赛跑到一半才开始收包：那半圈存成不完整，而且不能凭它的半截长度新建一条“赛道”。"""
    store = Store(":memory:")
    saver = LapSaver(store, "udp:test", background=False)
    det = LapDetector(saver)
    s = CircleStream(track_ordinal=None)          # 没有赛道 ID，只能靠起点 + 圈长认赛道
    s.run(s.lap_len / s.v * 1.5)                  # 这段时间本工具还没开
    for f in s.run(s.lap_len / s.v * 3.2):
        det.on_frame(f)
    det.flush()

    tracks = store.list_tracks()
    assert len(tracks) == 1, tracks
    assert tracks[0]["provisional_length_m"] is None
    assert tracks[0]["ref_length_m"] == pytest.approx(s.lap_len, rel=0.01)
    laps = store.list_laps()
    partial = [l for l in laps if "partial_start" in l["flags"]]
    assert len(partial) == 1 and not partial[0]["is_complete"]
    assert all(l["track_id"] == tracks[0]["id"] for l in laps)


# ---------------------------------------------------------------- 暂停

def test_long_pause_keeps_current_lap():
    """FM 暂停时还会发 IsRaceOn = 0 的包：暂停 40 秒再继续，这一圈不能被丢掉。"""
    clock = _Clock()
    out = []
    det = LapDetector(out.append)
    s = CircleStream()
    with mock.patch.object(laps_mod.time, "monotonic", clock):
        _feed(det, s.run(s.lap_len / s.v * 1.5), clock)
        _feed(det, s.paused(40), clock, tick=True)
        _feed(det, s.run(s.lap_len / s.v * 1.0 + 15), clock)
    assert det.dropped_partial == 0
    assert [l.meta["lap_number"] for l in out] == [0, 1]
    assert out[1].meta["is_complete"] and out[1].meta["distance_m"] == pytest.approx(s.lap_len, rel=0.01)


def test_pending_lap_commits_while_paused():
    """刚过线就暂停（例如比赛结束进了结算画面）：挂起的那圈按墙钟等 12 秒就落盘，不用等到下一场。"""
    clock = _Clock()
    out = []
    det = LapDetector(out.append)
    s = CircleStream()
    with mock.patch.object(laps_mod.time, "monotonic", clock):
        _feed(det, s.run(s.lap_len / s.v + 0.5), clock)   # 刚过线 0.5 秒
        assert det.pending is not None and not out
        _feed(det, s.paused(laps_mod.PENDING_WINDOW_S + 2), clock, tick=True)
    assert len(out) == 1 and out[0].meta["is_complete"]
    assert det.event is not None                            # 只是暂停，比赛还没结束


def test_pause_too_long_and_no_packets_end_event():
    clock = _Clock()
    det = LapDetector()
    s = CircleStream()
    with mock.patch.object(laps_mod.time, "monotonic", clock):
        _feed(det, s.run(5), clock)
        _feed(det, s.paused(laps_mod.PAUSE_MAX_S + 5), clock, tick=True)
        assert det.event is None                            # 连续暂停太久

        _feed(det, s.run(5), clock)
        assert det.event is not None
        clock.t += laps_mod.IDLE_S + 1                      # 之后一个包都没收到
        det.tick()
        assert det.event is None


# ---------------------------------------------------------------- 线程安全

def test_status_routes_run_on_event_loop(cfg):
    """读 hub / 切圈器状态的路由必须是 async def（普通 def 会被放进线程池，和收包并发）。"""
    from forza.api import live as live_api
    app = create_app(Runtime(cfg))
    status = next(r.endpoint for r in app.routes if getattr(r, "path", None) == "/api/status")
    for fn in (status, live_api.live_snapshot, live_api.live_summaries):
        assert inspect.iscoroutinefunction(fn), fn.__name__


def test_detector_status_has_no_side_effects():
    det = LapDetector()
    s = CircleStream()
    for f in s.run(3):
        det.on_frame(f)
    before = det.cur.use_rt_clock
    st = det.status()
    assert st["event_kind"] == det.event_kind() == "lap"
    assert det.cur.use_rt_clock == before


# ---------------------------------------------------------------- 配置

@pytest.mark.parametrize("kwargs, words", [
    ({"udp_port": "5555"}, "udp_port"),
    ({"http_port": 70000}, "http_port"),
    ({"live_hz": 0}, "live_hz"),
    ({"record_buffer_minutes": -1}, "record_buffer_minutes"),
    ({"tire_temp_unit": "K"}, "tire_temp_unit"),
    ({"game": "fh9"}, "game"),
    ({"forward": ["127.0.0.1"]}, "格式不对"),
    ({"forward": ["127.0.0.1:5555"]}, "无限循环"),
    ({"forward": ["localhost:5555"]}, "无限循环"),
])
def test_config_validate_rejects(kwargs, words):
    with pytest.raises(ConfigError, match=words):
        Config(**kwargs).validate()


def test_config_validate_accepts_defaults_and_other_forward_port():
    Config().validate()
    Config(forward=["127.0.0.1:20777", "192.168.1.50:5555"]).validate()


def test_main_reports_config_error(tmp_path, capsys):
    from forza.__main__ import main
    assert main(["record", "--forward", "127.0.0.1:5555", "--duration", "0"]) == 2
    assert "无限循环" in capsys.readouterr().err


# ---------------------------------------------------------------- 网页接口

@pytest.fixture
def client(cfg):
    rt = Runtime(cfg)
    with TestClient(create_app(rt)) as c:
        c.rt = rt
        yield c
    rt.shutdown()


def test_cross_site_write_is_rejected(client):
    r = client.post("/api/record/stop", headers={"Origin": "http://evil.example"})
    assert r.status_code == 403
    # 同源（自己的网页）和不带 Origin（curl / 脚本）照常
    assert client.post("/api/record/stop", headers={"Origin": "http://testserver"}).status_code == 200
    assert client.post("/api/record/stop").status_code == 200
    # 只读请求不拦
    assert client.get("/api/status", headers={"Origin": "http://evil.example"}).status_code == 200


def test_delete_and_patch_refresh_reference_length(cfg):
    store = Store(cfg.resolve(cfg.db_path))
    saver = LapSaver(store, "import:x", background=False)
    det = LapDetector(saver)
    s = CircleStream(track_ordinal=None)
    for f in s.run(s.lap_len / s.v * 4.2):
        det.on_frame(f)
    det.flush()
    track_id = store.list_tracks()[0]["id"]
    flying = [l for l in store.list_laps() if "first_lap" not in l["flags"]]
    assert len(flying) >= 2
    store.set_track_length(track_id, 9999.0)            # 故意弄脏，看接口会不会重算
    store.close()

    rt = Runtime(cfg)
    with TestClient(create_app(rt)) as c:
        assert c.patch(f"/api/laps/{flying[0]['id']}", json={"is_complete": False}).status_code == 200
        assert rt.store.get_track(track_id)["ref_length_m"] == pytest.approx(s.lap_len, rel=0.01)
        rt.store.set_track_length(track_id, 9999.0)
        assert c.delete(f"/api/laps/{flying[1]['id']}").status_code == 200
        assert rt.store.get_track(track_id)["ref_length_m"] == pytest.approx(s.lap_len, rel=0.01)
    rt.shutdown()


def test_record_write_failure_is_reported(client, tmp_path):
    rt = client.rt
    client.post("/api/record/start")
    rec = rt.hub.recorder
    with mock.patch.object(rec, "write", side_effect=OSError("disk full")):
        rt.hub.feed(bytes(331))
    assert rt.hub.recorder is None
    st = client.get("/api/record/status").json()
    assert not st["recording"] and "写录像失败" in st["error"]
    assert any("写录像失败" in w for w in client.get("/api/status").json()["warnings"])
    client.post("/api/record/start")                    # 重新开始录制：清掉旧的错误
    assert client.get("/api/record/status").json()["error"] is None
    client.post("/api/record/stop")


def test_hub_pps_counts_recent_packets():
    hub = Hub()
    for _ in range(30):
        hub.feed(bytes(10))                             # 太短的包也算收到
    assert hub.pps() == 30
