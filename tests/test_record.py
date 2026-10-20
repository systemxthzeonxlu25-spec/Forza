"""网页录制：Hub 环形缓冲（预打包）、保存最近 N 分钟、开始 / 停止录制接口、回放模式禁用快照。"""
import inspect

import pytest
from fastapi.testclient import TestClient

from forza.api import record as record_api
from forza.app import create_app
from forza.config import Config
from forza.hub import Hub, PacketRing, iter_packed, write_packed
from forza.packet import encode
from forza.recorder import iter_records, read_meta
from forza.runtime import Runtime
from tests.helpers import CircleStream


def _pkt(i: int, n: int = 331) -> bytes:
    return bytes([i % 251]) * n


def test_ring_keeps_window_and_snapshot_is_exact():
    ring = PacketRing(seconds=600, chunk_bytes=64 * 1024)
    hz = 60
    for i in range(20 * 60 * hz):                     # 20 分钟
        ring.append(i / hz, _pkt(i))
    st = ring.stats()
    assert st["seconds"] <= 600
    # 只多留不到一块：64 KB / 341 B ≈ 190 包
    assert 600 * hz <= st["packets"] <= 600 * hz + 200
    packed, n, secs = ring.snapshot(60)
    recs = list(iter_packed(packed))
    assert n == len(recs) == 60 * hz + 1               # [t_last - 60, t_last] 两头都算
    t_last = (20 * 60 * hz - 1) / hz
    assert recs[0][0] == pytest.approx(t_last - 60) and recs[-1][0] == pytest.approx(t_last)
    assert secs == pytest.approx(60, abs=0.02)
    assert recs[-1][1] == _pkt(20 * 60 * hz - 1)
    # 超过窗口的请求按窗口截
    _, n_all, secs_all = ring.snapshot(10_000)
    assert secs_all <= 600 + 1e-6 and n_all <= st["packets"]


def test_ring_snapshot_inside_current_chunk_then_keep_appending():
    """截取的窗口落在正在写的那一块里：截完还要能继续追加（不能留着 memoryview 把 bytearray 锁住）。"""
    ring = PacketRing(seconds=600)
    for i in range(300):
        ring.append(i / 60, _pkt(i))
    packed, n, _ = ring.snapshot(1.0)
    assert n == 61 and len(list(iter_packed(packed))) == 61
    for i in range(300, 400):
        ring.append(i / 60, _pkt(i))
    assert ring.stats()["packets"] == 400


def test_ring_memory_cap():
    ring = PacketRing(seconds=3600, max_bytes=200_000, chunk_bytes=20_000)
    for i in range(10_000):
        ring.append(i / 60, _pkt(i))
    assert ring.stats()["bytes"] <= 200_000 + 2 * 20_000


def test_write_packed_rebases_time(tmp_path):
    ring = PacketRing(seconds=60)
    for i in range(100):
        ring.append(5000.0 + i / 60, _pkt(i, 232 + i % 3))
    packed, n, _ = ring.snapshot()
    p = tmp_path / "s.frec"
    assert write_packed(p, packed, {"snapshot": True}) == n == 100
    recs = list(iter_records(p))
    assert recs[0][0] == 0.0 and recs[-1][0] == pytest.approx(99 / 60)
    assert [d for _, d in recs] == [_pkt(i, 232 + i % 3) for i in range(100)]
    assert read_meta(p)["snapshot"] is True


def test_hub_ring_off_by_default_and_fed_when_on():
    assert Hub().ring is None                           # 离线导入 / info 不占内存
    hub = Hub(ring_seconds=30)
    for i in range(10):
        hub.feed(_pkt(i, 7))                            # 不认识的包也照样进缓冲
    assert hub.ring.stats()["packets"] == 10


@pytest.fixture
def rt(cfg):
    r = Runtime(cfg)
    yield r
    r.shutdown()


@pytest.fixture
def client(rt):
    with TestClient(create_app(rt)) as c:
        yield c


def _feed(rt, n_seconds=2.0):
    s = CircleStream()
    frames = s.run(n_seconds)
    for f in frames:
        rt.hub.feed(encode(f, "fm2023_dash"))
    return len(frames)


def test_routes_are_async():
    """开始 / 停止 / 快照必须和收包在同一个事件循环里跑（async def），不能进线程池。"""
    for fn in (record_api.record_start, record_api.record_stop, record_api.record_snapshot,
               record_api.record_status):
        assert inspect.iscoroutinefunction(fn), fn.__name__


def test_config_default_buffer(cfg, rt):
    assert Config().record_buffer_minutes == 10
    assert rt.hub.ring is not None and rt.hub.ring.seconds == 600


def test_start_stop_recording(client, rt, cfg):
    st = client.get("/api/record/status").json()
    assert st["recording"] is False and st["path"] is None and st["buffer_packets"] == 0
    r = client.post("/api/record/start", json={})
    assert r.status_code == 200, r.text
    st = r.json()
    path = st["path"]
    assert st["recording"] is True and path.startswith(str(cfg.resolve(cfg.rec_dir)))
    assert client.post("/api/record/start").status_code == 400          # 已经在录
    n = _feed(rt)
    st = client.get("/api/record/status").json()
    assert st["packets"] == n and st["buffer_packets"] == n
    live = client.get("/api/live/snapshot").json()
    assert live["status"]["recording"] == path                           # 实时面板状态栏立刻能看到
    r = client.post("/api/record/stop")
    assert r.status_code == 200 and r.json()["recording"] is False
    assert r.json()["stopped"] == {"path": path, "packets": n}
    assert len(list(iter_records(path))) == n
    assert client.get("/api/live/snapshot").json()["status"]["recording"] is None
    assert client.post("/api/record/stop").json()["stopped"] is None      # 没在录：不报错


def test_start_with_name(client, rt, cfg):
    r = client.post("/api/record/start", json={"path": "t1"})
    assert r.status_code == 200
    assert r.json()["path"] == str(cfg.resolve(cfg.rec_dir) / "t1.frec")
    client.post("/api/record/stop")
    assert client.post("/api/record/start", json={"path": "t1.frec"}).status_code == 400   # 不覆盖
    for bad in ("../x.frec", "/tmp/x.frec"):
        r = client.post("/api/record/start", json={"path": bad})
        assert r.status_code == 400 and "录像目录" in r.json()["detail"]


def test_snapshot(client, rt, cfg):
    r = client.post("/api/record/snapshot", json={"minutes": 5})
    assert r.status_code == 400 and "还没有收到" in r.json()["detail"]
    n = _feed(rt, 3.0)
    r = client.post("/api/record/snapshot", json={"minutes": 5})
    assert r.status_code == 200, r.text
    res = r.json()
    assert set(res) == {"path", "packets", "seconds"} and res["packets"] == n
    recs = list(iter_records(res["path"]))
    assert len(recs) == n and recs[0][0] == 0.0
    assert read_meta(res["path"])["snapshot"] is True
    r2 = client.post("/api/record/snapshot", json={"minutes": 5})   # 同一秒再点一次：不撞名
    assert r2.status_code == 200 and r2.json()["path"] != res["path"]
    r3 = client.post("/api/record/snapshot")                         # 不给 minutes = 整个缓冲
    assert r3.status_code == 200 and "last10min" in r3.json()["path"]
    st = client.get("/api/record/status").json()
    assert st["last_snapshot"]["path"] == r3.json()["path"] and st["snapshot_disabled"] is None
    assert client.post("/api/record/snapshot", json={"minutes": 0}).status_code == 400
    # 录出来的快照能完整切圈：用 info 同样的方式读回
    hub = Hub()
    for t, data in iter_records(res["path"]):
        assert hub.feed(data, t) is not None


def test_snapshot_disabled_in_replay(cfg, tmp_path):
    rt = Runtime(cfg, replay=str(tmp_path / "x.frec"))
    try:
        with TestClient(create_app(rt)) as c:
            st = c.get("/api/record/status").json()
            assert st["replay"] is True and "回放" in st["snapshot_disabled"]
            r = c.post("/api/record/snapshot", json={"minutes": 1})
            assert r.status_code == 400 and "回放" in r.json()["detail"]
    finally:
        rt.shutdown()


def test_buffer_off(tmp_path):
    cfg = Config(db_path=str(tmp_path / "t.db"), rec_dir=str(tmp_path / "rec"), record_buffer_minutes=0)
    rt = Runtime(cfg)
    try:
        assert rt.hub.ring is None
        with TestClient(create_app(rt)) as c:
            assert c.get("/api/record/status").json()["snapshot_disabled"]
            assert c.post("/api/record/snapshot").status_code == 400
    finally:
        rt.shutdown()
