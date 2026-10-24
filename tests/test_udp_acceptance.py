"""本机回环上的真实 UDP / HTTP / WebSocket 链路验收，包是独立手工构造的。

只监听本机，不需要开 Forza，也不代表已经用真实游戏验证过。
"""
import asyncio
import json
import math
import socket
import struct

import httpx
import numpy as np
import pytest
import uvicorn
import websockets

from forza.app import create_app
from forza.config import Config
from forza.hub import Hub
from forza.laps import LapDetector, LapSaver
from forza.recorder import iter_records
from forza.replay import import_file
from forza.runtime import Runtime
from forza.storage import Store
from forza.udp import start_udp
from tests.test_data_contract import raw_dash


def _packets(size):
    """三圈各 500 m，分别用时 20、22、21 秒，再跑下一圈的 1 秒。"""
    shift = 12 if size == 324 else 0
    elapsed = 0
    for lap, duration in enumerate((20, 22, 21, 20)):
        steps = 50 * duration if lap < 3 else 50
        for i in range(steps):
            t = (i + 1) / 50
            fraction = t / duration
            speed = 500 / duration
            angle = fraction * 2 * math.pi
            b = bytearray(raw_dash(size, lap=lap, lap_t=t, race_t=elapsed + t,
                                   distance=lap * 500 + fraction * 500))
            struct.pack_into("<I", b, 4, round((elapsed + t) * 1000))
            struct.pack_into("<f", b, 40, speed)
            struct.pack_into("<3f", b, 232 + shift,
                             500 / (2 * math.pi) * math.sin(angle), 0,
                             500 / (2 * math.pi) * math.cos(angle))
            struct.pack_into("<f", b, 244 + shift, speed)
            struct.pack_into("<f", b, 288 + shift, (0, 20, 22, 21)[lap])
            yield bytes(b)
        elapsed += duration


async def _until(predicate):
    async with asyncio.timeout(10):
        while not predicate():
            await asyncio.sleep(0.002)


def _strict_json(text):
    def reject(value):
        raise AssertionError(value)
    return json.loads(text, parse_constant=reject)


@pytest.mark.parametrize("size,game", [(324, "fh5"), (331, "fm2023")])
def test_real_udp_database_compare_and_live(tmp_path, size, game):
    async def check():
        cfg = Config(udp_host="127.0.0.1", udp_port=0, http_host="127.0.0.1", http_port=0,
                     game=game, db_path=str(tmp_path / "udp.db"), rec_dir=str(tmp_path / "rec"))
        rt = Runtime(cfg)
        rec_path = tmp_path / "synthetic.frec"
        rt.hub.start_recording(rec_path, {"source": "synthetic-acceptance", "game": game})
        tcp = socket.socket()
        tcp.bind(("127.0.0.1", 0))
        tcp.listen()
        port = tcp.getsockname()[1]
        server = uvicorn.Server(uvicorn.Config(create_app(rt), host="127.0.0.1", port=port,
                                               log_level="error", ws="websockets-sansio"))
        serve = asyncio.create_task(server.serve(sockets=[tcp]))
        broadcast = asyncio.create_task(rt.live.run())
        udp = await start_udp(rt.hub, "127.0.0.1", 0)
        sender = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        packets = list(_packets(size))
        try:
            await _until(lambda: server.started or serve.done())
            assert server.started
            async with websockets.connect(f"ws://127.0.0.1:{port}/ws/live?hz=30") as ws:
                first = _strict_json(await asyncio.wait_for(ws.recv(), 5))
                assert first["type"] == "status"
                for i, packet in enumerate(packets, 1):
                    sender.sendto(packet, udp.get_extra_info("sockname"))
                    if i % 20 == 0:
                        await _until(lambda: rt.hub.packets == i)
                await _until(lambda: rt.hub.packets == len(packets))
                # 确认客户端真的收到了一条 WebSocket 消息
                async with asyncio.timeout(10):
                    while True:
                        msg = _strict_json(await ws.recv())
                        if msg.get("frame") is not None:
                            break
                assert msg["frame"]["game"] == game
                assert msg["frame"]["throttle"] == 1
                assert msg["frame"]["brake"] == pytest.approx(128 / 255, abs=0.001)
            rt.detector.flush()
            await asyncio.to_thread(rt.saver.wait)
            rt.hub.stop_recording()
            laps = sorted(rt.store.list_laps(), key=lambda row: row["lap_number"])
            assert [row["lap_time"] for row in laps] == [20, 22, 21]
            assert all(row["is_complete"] for row in laps)
            assert len({row["track_id"] for row in laps}) == 1
            assert rt.hub.packets == len(packets) == 3200
            assert [data for _, data in iter_records(rec_path)] == packets
            assert rt.store._q("PRAGMA integrity_check")[0][0] == "ok"
            async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}", trust_env=False) as client:
                assert (await client.get("/")).status_code == 200
                assert (await client.get("/live.js")).status_code == 200
                snap = _strict_json((await client.get("/api/live/snapshot")).text)
                assert snap["frame"]["speed_kmh"] == 90
                assert snap["frame"]["tire_temp_c"] == [0, 50, 100, 150]
                response = await client.get("/api/compare", params={"a": laps[0]["id"],
                                             "b": laps[1]["id"], "channels": "tire_temp,slip_ratio"})
                assert response.status_code == 200, response.text
                comp = _strict_json(response.text)
                assert comp["totals"]["lap_time_delta_s"] == pytest.approx(2)
                assert comp["totals"]["delta_t_end_s"] == pytest.approx(2, abs=0.1)
                assert np.mean(comp["channels"]["ref"]["speed"]) == pytest.approx(90)
                assert np.mean(comp["channels"]["other"]["speed"]) == pytest.approx(500 / 22 * 3.6, abs=0.01)
            # 把录下来的原始包重新解码、切圈，存进一个全新的 SQLite 库，结果要和实时的一致
            offline = Store(tmp_path / "import.db")
            hub = Hub(game)
            saver = LapSaver(offline, "import:synthetic.frec", background=False)
            detector = LapDetector(saver)
            hub.subscribe(detector.on_frame)
            assert import_file(rec_path, hub) == len(packets)
            detector.flush()
            imported = sorted(offline.list_laps(), key=lambda row: row["lap_number"])
            assert [row["lap_time"] for row in imported] == [20, 22, 21]
            for a, b in zip(laps, imported):
                live_lap, offline_lap = rt.store.get_lap(a["id"]), offline.get_lap(b["id"])
                assert live_lap.ch.keys() == offline_lap.ch.keys()
                for channel in live_lap.ch:
                    np.testing.assert_array_equal(live_lap[channel], offline_lap[channel])
        finally:
            sender.close()
            udp.close()
            server.should_exit = True
            await asyncio.wait_for(serve, 10)
            broadcast.cancel()
            await asyncio.gather(broadcast, return_exceptions=True)
            rt.shutdown()
            tcp.close()
    asyncio.run(check())
