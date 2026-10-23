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
    ({"forward": ["LOCALHOST:5555"]}, "无限循环"),                     # 主机名不区分大小写
    ({"forward": ["[::1]:5555"]}, "无限循环"),
    ({"forward": ["127.0.0.2:5555"]}, "无限循环"),                    # 整个 127.0.0.0/8 都是回环
    ({"record_buffer_minutes": float("inf")}, "record_buffer_minutes"),   # TOML 允许写 inf
    ({"live_hz": float("nan")}, "live_hz"),
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


def _flying_laps(store, n_laps=4.2):
    """在没有赛道 ID 的圆形赛道上跑几圈，返回 (track_id, 飞驰圈列表, 圈长)。"""
    saver = LapSaver(store, "import:x", background=False)
    det = LapDetector(saver)
    s = CircleStream(track_ordinal=None)
    for f in s.run(s.lap_len / s.v * n_laps):
        det.on_frame(f)
    det.flush()
    track_id = store.list_tracks()[0]["id"]
    flying = [l for l in store.list_laps() if "first_lap" not in l["flags"]]
    return track_id, flying, s.lap_len, saver


def test_removing_last_flying_lap_clears_reference_length(cfg):
    """删掉 / 标掉最后一圈飞驰圈：参考圈长要清空，不能留着一个已经不存在的圈长去匹配赛道。"""
    store = Store(cfg.resolve(cfg.db_path))
    track_id, flying, _, _ = _flying_laps(store)
    store.close()
    rt = Runtime(cfg)
    with TestClient(create_app(rt)) as c:
        for lap in flying[:-1]:
            assert c.delete(f"/api/laps/{lap['id']}").status_code == 200
        assert rt.store.get_track(track_id)["ref_length_m"] is not None
        assert c.patch(f"/api/laps/{flying[-1]['id']}", json={"is_complete": False}).status_code == 200
        assert rt.store.get_track(track_id)["ref_length_m"] is None
    rt.shutdown()


def test_rewind_invalidation_recomputes_reference_length(tmp_path):
    """倒带作废已存的飞驰圈后，赛道参考圈长按剩下的有效圈重算（和删圈 / 标为不完整一样）。"""
    store = Store(tmp_path / "t.db")
    track_id, flying, lap_len, saver = _flying_laps(store)
    store.set_track_length(track_id, 9999.0)            # 故意弄脏，看作废时会不会重算
    meta = {**flying[0], "flags": [*flying[0]["flags"], "rewound_after_save"]}
    saver.invalidate(meta)
    assert not store.get_lap_meta(flying[0]["id"])["is_complete"]
    assert store.get_track(track_id)["ref_length_m"] == pytest.approx(lap_len, rel=0.01)


def test_reference_length_survives_many_recent_first_laps(tmp_path):
    """最近存了很多起步圈（反复单圈重开），更早的飞驰圈仍然要用来定参考圈长，不能被当成“没有飞驰圈”清掉。"""
    import json
    store = Store(tmp_path / "t.db")
    tid = store.find_or_create_track(family="fh", game="fh5", kind="circuit", track_ordinal=None,
                                     start_x=0, start_z=0, length_m=3000)

    def add(dist, flags):
        store.execute("INSERT INTO laps (track_id, kind, is_complete, distance_m, flags) VALUES (?, 'lap', 1, ?, ?)",
                      (tid, dist, json.dumps(flags)))

    for d in (3000, 3010, 2990):
        add(d, [])
    for _ in range(100):
        add(3300, ["first_lap"])
    assert store.refresh_circuit_length(tid, clear_if_empty=True) == pytest.approx(3000.0)
    assert store.get_track(tid)["ref_length_m"] == pytest.approx(3000.0)


def test_status_does_not_query_network_on_event_loop(cfg):
    """/api/status 跑在事件循环里：只能读缓存的局域网 IP，不能同步做网络 / DNS 查询。"""
    rt = Runtime(cfg)
    rt.live.lan_ips_cached()                            # 启动时那一次（之后由后台线程刷新）
    with mock.patch("forza.live.lan_ips", side_effect=AssertionError("不该同步查")), \
            mock.patch("forza.runtime.lan_ips", side_effect=AssertionError("不该同步查")):
        st = rt.status()
    assert isinstance(st["lan_ips"], list)
    rt.shutdown()
