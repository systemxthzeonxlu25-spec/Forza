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
    assert all(l["track_id"] == tracks[0]["id"] for l in laps)          # 半圈由后面的整圈补上了赛道


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
    ({"game": ["fh6"]}, "game"),
    ({"forward": ["[2001:db8::1]:20777"]}, "IPv6"),                   # 转发 socket 只支持 IPv4                                        # 写成列表：给中文提示，不抛 TypeError
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


def _run_partial_session(store, s):
    """比赛跑到一半才开始收包，再跑两圈多。返回按 id 排好的圈。"""
    saver = LapSaver(store, "udp:test", background=False)
    det = LapDetector(saver)
    s.run(s.lap_len / s.v * 1.5)                                          # 本工具还没开
    for f in s.run(s.lap_len / s.v * 2.2):
        det.on_frame(f)
    det.flush()
    return sorted(store.list_laps(), key=lambda l: l["id"])


@pytest.mark.parametrize("known", ["both_layouts", "only_other_layout"])
def test_partial_lap_waits_for_full_lap_to_pick_track(known):
    """中途接入的半圈光看位置认不准布局：先不归赛道，等同一场第一个完整圈认出赛道后，半圈和会话一起补上。

    only_other_layout：库里只有同一起终点线的另一个布局（附近“正好一条”），当前跑的是还没存过的新布局——
    半圈也不能挂到那条旧布局上。
    """
    store = Store(":memory:")
    s = CircleStream(track_ordinal=None)
    kw = dict(family=s.family, game=s.game, kind="circuit", track_ordinal=None, start_x=0.0, start_z=-s.R)
    other = store.find_or_create_track(length_m=s.lap_len * 0.6, **kw)    # 同一起终点线的另一个布局
    if known == "both_layouts":
        store.find_or_create_track(length_m=s.lap_len, **kw)
    laps = _run_partial_session(store, s)
    right = laps[1]["track_id"]
    assert right is not None and right != other
    assert "partial_start" in laps[0]["flags"] and laps[0]["track_id"] == right   # 半圈被补到了对的赛道
    assert all(l["track_id"] == right for l in laps)
    sess = store.list_sessions()
    assert len(sess) == 1 and sess[0]["track_id"] == right


def test_flying_lengths_skip_only_standing_first_lap():
    """中途接入时第一圈不完整、根本不进长度表：后面第一圈飞驰圈的长度要留着，给实时面板区分布局用。"""
    det = LapDetector()
    s = CircleStream(track_ordinal=None)
    s.run(s.lap_len / s.v * 1.5)                                          # 本工具还没开
    for f in s.run(s.lap_len / s.v * 1.5 + laps_mod.PENDING_WINDOW_S + 1):
        det.on_frame(f)
    ev = det.event
    assert len(ev.flying_lengths) == 1 and ev.flying_lengths[0] == pytest.approx(s.lap_len, rel=0.01)

    det2 = LapDetector()                                                  # 正常从发车格起步：第一圈不算飞驰圈
    s2 = CircleStream(track_ordinal=None)
    for f in s2.run(s2.lap_len / s2.v * 2 + laps_mod.PENDING_WINDOW_S + 1):
        det2.on_frame(f)
    assert len(det2.event.lap_lengths) == 2 and len(det2.event.flying_lengths) == 1


def test_invalidation_bumps_laps_revision(tmp_path):
    """倒带作废已存的圈也要让网页刷新：laps_revision 和存新圈一样会变。"""
    store = Store(tmp_path / "t.db")
    _, flying, _, saver = _flying_laps(store)
    rev = saver.revision
    assert rev == len(store.list_laps())
    saver.invalidate({**flying[0], "flags": [*flying[0]["flags"], "rewound_after_save"]})
    assert saver.revision == rev + 1


def test_info_command_validates_config(tmp_path, capsys):
    from forza import __main__ as cli
    from forza.fake import write_fake_recording
    rec = tmp_path / "x.frec"
    write_fake_recording(rec, "fm2023_dash", laps=1, seed=1)
    with mock.patch.object(cli, "load_config", return_value=Config(tire_temp_unit="K")):
        assert cli.main(["info", str(rec)]) == 2
    assert "tire_temp_unit" in capsys.readouterr().err


def test_powertrain_rows_get_backfilled_track(tmp_path):
    """中途接入的半圈先存了动力总成数据（track_id 为空），后面整圈认出赛道后也要一起补上。"""
    from forza import powertrain

    def fake_record(store, lap):            # 每圈都写一行动力总成数据（真实数据要有换挡等才会写）
        powertrain.ensure_schema(store)
        m = lap.meta
        store.execute("INSERT INTO powertrain_laps (lap_id, game, car_ordinal, track_id) VALUES (?,?,?,?)",
                      (m["id"], m["game"], m["car_ordinal"], m.get("track_id")))

    store = Store(tmp_path / "t.db")
    with mock.patch.object(powertrain, "record_lap", side_effect=fake_record):
        laps = _run_partial_session(store, CircleStream(track_ordinal=None))
    rows = store.query("SELECT lap_id, track_id FROM powertrain_laps ORDER BY lap_id")
    right = laps[1]["track_id"]
    assert right is not None and len(rows) == len(laps)
    assert all(r["track_id"] == right for r in rows)


def test_match_track_prefers_layout_with_matching_length():
    """共用起终点线：参考圈长为空的旧布局不能抢在圈长真正对得上的布局前面；不许猜时有多个候选就不选。"""
    from forza.live import match_track
    store = Store(":memory:")
    kw = dict(family="fh", game="fh5", kind="circuit", track_ordinal=None, start_x=0.0, start_z=0.0)
    old = store.find_or_create_track(length_m=2000, **kw)
    right = store.find_or_create_track(length_m=3000, **kw)
    assert old != right
    store.set_track_length(old, None)                                  # 旧布局的飞驰圈后来都被删掉了
    q = dict(family="fh", kind="circuit", track_ordinal=None, x=0.0, z=0.0)
    assert match_track(store, length_m=3010, **q) == right
    assert match_track(store, length_m=5000, **q) == old                # 都对不上时才退回到空长度的
    assert match_track(store, length_m=None, guess=False, **q) is None
    assert match_track(store, length_m=None, guess=True, **q) in (old, right)


def test_invalidated_summary_is_announced_and_not_replaced(cfg):
    """倒带作废了最新那条小结：消息里带上它的 id 让手机删掉，last_summary 清空而不是退回上一条（否则会再弹一次）。"""
    rt = Runtime(cfg)
    live = rt.live
    live.summaries.extend([{"id": 1}, {"id": 2}])
    live.last_summary = {"id": 2}
    live._remove_summary(2)
    msg = live.snapshot()
    assert msg["last_summary"] is None and msg["invalidated_summary_ids"] == [2]
    assert [s["id"] for s in live.recent_summaries()] == [1]
    rt.shutdown()
