"""实时面板：/api/live/snapshot 的内容（单位换算、实时差距、Sled 格式）+ WebSocket 推送 + 慢客户端不拖累别人。"""
import asyncio
import json

import pytest
from fastapi.testclient import TestClient

import forza.live as live_mod
from forza.app import create_app
from forza.frame import G
from forza.live import LiveBroadcaster
from forza.packet import encode
from forza.runtime import Runtime
from tests.helpers import CircleStream


def _strict_json(text: str) -> dict:
    """解析 JSON；出现 NaN / Infinity 直接失败（浏览器的 JSON.parse 不认它们）。"""
    def bad(c):
        raise AssertionError(f"消息里出现了 {c}")
    return json.loads(text, parse_constant=bad)


def _feed(rt, frames, fmt="fm2023_dash"):
    for f in frames:
        rt.hub.feed(encode(f, fmt))


def _snapshot(rt) -> dict:
    client = TestClient(create_app(rt))
    r = client.get("/api/live/snapshot")
    assert r.status_code == 200
    return _strict_json(r.text)


@pytest.fixture
def rt(cfg):
    r = Runtime(cfg)
    yield r
    r.shutdown()


def test_snapshot_units_and_delta(rt, cfg):
    s = CircleStream()   # 半径 200 m、40 m/s 匀速，一圈约 31.4 s
    _feed(rt, s.run_dist(s.lap_len * 2.3))
    m = _snapshot(rt)
    assert m["type"] == "frame" and m["seq"] > 0
    f = m["frame"]
    assert f["has_dash"] is True and f["speed_src"] == "dash"
    assert f["speed_kmh"] == pytest.approx(40 * 3.6, abs=0.1)
    assert f["rpm"] == 6000 and f["max_rpm"] == 8000 and f["gear"] == "4"
    assert f["throttle"] == pytest.approx(200 / 255, abs=1e-3) and f["brake"] == 0
    assert f["tire_temp_c"] == pytest.approx([(180 - 32) * 5 / 9] * 4, abs=0.1)   # 原始 °F → °C
    assert f["g_lat"] == pytest.approx(40 ** 2 / 200 / G, abs=0.01)
    assert len(f["tire_wear"]) == 4 and len(f["susp"]) == 4 and len(f["slip_angle"]) == 4
    assert f["game"] == "fm2023" and f["fmt"] == "fm2023_dash" and f["game_name"]
    assert f["car"]["ordinal"] == 100

    lap = m["lap"]
    lap_time = s.lap_len / s.v
    assert lap["active"] and lap["kind"] == "circuit" and lap["number"] == 2
    assert lap["last"] == pytest.approx(lap_time, abs=0.02)
    assert lap["ref_time"] == pytest.approx(lap_time, abs=0.02)
    assert lap["dist"] == pytest.approx(0.3 * s.lap_len, abs=2)
    assert lap["delta"] is not None and abs(lap["delta"]) < 0.05        # 匀速：和参照圈几乎一样
    assert lap["predicted"] == pytest.approx(lap["ref_time"] + lap["delta"], abs=0.002)

    st = m["status"]
    assert st["udp_port"] == cfg.udp_port and st["http_port"] == cfg.http_port
    assert st["format"] == "fm2023_dash" and st["warnings"] == [] and isinstance(st["lan_ips"], list)
    assert st["since_last_packet"] is not None and st["packets"] > 0


def test_delta_follows_pace(rt):
    s = CircleStream()
    _feed(rt, s.run_dist(s.lap_len * 1.0 + 1))     # 跑完第 0 圈，刚过线
    s.v = 36.0                                      # 第 1 圈慢 10%
    _feed(rt, s.run_dist(200))
    m = _snapshot(rt)
    # 200 m 时上一圈还挂起在切圈器里（没落盘），也要能当参照
    assert rt.detector.pending is not None
    assert m["lap"]["delta"] == pytest.approx(200 / 36 - 200 / 40, abs=0.1)
    _feed(rt, s.run_dist(600))
    m = _snapshot(rt)
    assert rt.detector.pending is None and rt.detector.event.best_lap is not None
    assert m["lap"]["delta"] == pytest.approx(800 / 36 - 800 / 40, abs=0.1)
    assert m["lap"]["delta"] > 0                    # 正 = 比参照圈慢


def test_no_delta_before_first_complete_lap(rt):
    s = CircleStream()
    _feed(rt, s.run_dist(s.lap_len * 0.5))
    lap = _snapshot(rt)["lap"]
    assert lap["number"] == 0 and lap["time"] == pytest.approx(0.5 * s.lap_len / s.v, abs=0.05)
    assert lap["delta"] is None and lap["ref_time"] is None and lap["last"] is None


def test_sled_snapshot(rt):
    s = CircleStream(fmt="sled")
    _feed(rt, s.run(3.0), fmt="sled")
    m = _snapshot(rt)
    f = m["frame"]
    assert f["has_dash"] is False and f["fmt"] == "sled"
    assert f["speed_src"] == "vel" and f["speed_kmh"] == pytest.approx(144.0, abs=0.1)   # 用速度向量估算
    assert f["gear"] == "-"
    for k in ("throttle", "brake", "steer", "tire_temp_c", "tire_wear", "fuel", "power_kw", "position"):
        assert f[k] is None, k
    assert f["rpm"] == 6000 and len(f["susp"]) == 4 and f["g_lat"] is not None
    assert m["lap"] is None
    assert m["status"]["format"] == "sled" and m["status"]["has_dash"] is False


def test_nan_fields_become_null(rt):
    s = CircleStream()
    fr = s.step()
    fr.slip_ratio = (float("nan"), 0.0, 0.0, 0.0)
    fr.power = float("inf")
    _feed(rt, [fr])
    f = _snapshot(rt)["frame"]       # _strict_json 保证没有 NaN
    assert f["slip_ratio"][0] is None and f["power_kw"] is None


def test_no_data_snapshot_is_status(rt):
    m = _snapshot(rt)
    assert m["type"] == "status" and m["frame"] is None and m["lap"] is None
    assert m["status"]["since_last_packet"] is None and m["status"]["packets"] == 0


def test_websocket_pushes_frames(rt):
    s = CircleStream()
    _feed(rt, s.run(1.0))
    client = TestClient(create_app(rt))
    with client.websocket_connect("/ws/live?hz=30") as ws:
        first = _strict_json(ws.receive_text())
        assert first["type"] == "frame" and first["frame"]["speed_kmh"] == pytest.approx(144.0, abs=0.1)
        assert "lap" in first and "status" in first
        _feed(rt, s.run(0.5))
        nxt = _strict_json(ws.receive_text())
        assert nxt["type"] == "frame" and nxt["seq"] > first["seq"]
        assert len(rt.live.clients) == 1
    assert rt.live.clients == set()                 # 断开后服务端协程自己清理


def test_websocket_status_without_data(rt, monkeypatch):
    monkeypatch.setattr(live_mod, "STATUS_INTERVAL_S", 0.1)
    client = TestClient(create_app(rt))
    with client.websocket_connect("/ws/live") as ws:
        a = _strict_json(ws.receive_text())
        b = _strict_json(ws.receive_text())       # 没有数据也会定时发状态
        assert a["type"] == "status" and a["frame"] is None
        assert b["type"] == "status" and "frame" not in b
        assert b["status"]["since_last_packet"] is None and b["status"]["udp_port"] == rt.cfg.udp_port


def test_client_hz_clamped():
    hz = LiveBroadcaster.client_hz
    assert hz("100", 20) == 30 and hz("1", 20) == 5 and hz("12", 20) == 12
    assert hz("abc", 20) == 20 and hz("nan", 20) == 20 and hz(None, 20) == 20
    assert hz(None, 50) == 50


class _FakeWS:
    """假的 WebSocket：stall=True 时 send 永远发不出去（模拟卡住的手机）。"""

    def __init__(self, hz="30", stall=False):
        self.query_params = {"hz": hz}
        self.stall = stall
        self.sent: list[str] = []
        self.closed = None
        self._gone = asyncio.Event()

    async def accept(self):
        pass

    async def receive(self):
        await self._gone.wait()
        return {"type": "websocket.disconnect"}

    async def send_text(self, text):
        if self.stall:
            await asyncio.sleep(3600)
        self.sent.append(text)

    async def close(self, code=1000):
        self.closed = code
        self._gone.set()

    def disconnect(self):
        self._gone.set()


def test_slow_client_is_dropped_and_does_not_block_others(rt, monkeypatch):
    monkeypatch.setattr(live_mod, "SEND_TIMEOUT_S", 0.2)
    live = rt.live
    s = CircleStream()

    async def scenario():
        stalled, fast = _FakeWS(stall=True), _FakeWS()
        t_stall = asyncio.create_task(live.serve_client(stalled))
        t_fast = asyncio.create_task(live.serve_client(fast))
        for f in s.run(0.8):                       # 收包照常进行（60 包/秒）
            rt.hub.feed(encode(f, "fm2023_dash"))
            await asyncio.sleep(1 / 60)
        await asyncio.wait_for(t_stall, 1.0)       # 卡住的客户端超时后被断开
        n_during = len(fast.sent)
        fast.disconnect()
        await asyncio.wait_for(t_fast, 1.0)
        return stalled, fast, n_during

    stalled, fast, n_during = asyncio.run(scenario())
    assert stalled.closed == 1013 and stalled.sent == []
    assert n_during >= 10                          # 正常客户端一直在收（30Hz × 0.8 s）
    assert all(_strict_json(t)["type"] == "frame" for t in fast.sent)
    assert live.clients == set()
    assert rt.hub.packets == int(round(0.8 * 60))  # 收包没有被拖住
