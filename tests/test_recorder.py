"""录制 / 读取 / UDP 回放链路。"""
import asyncio
import socket

import pytest

from forza.hub import Hub
from forza.recorder import Recorder, iter_records, read_meta, write_recording
from forza.replay import import_file, replay_to_hub, replay_udp
from forza.udp import start_udp


def _packets(n=50):
    return [(i / 60.0, bytes([i % 256]) * (232 + (i % 3) * 79)) for i in range(n)]


@pytest.mark.parametrize("name", ["a.frec", "a.frec.gz"])
def test_roundtrip(tmp_path, name):
    p = tmp_path / name
    recs = _packets()
    write_recording(p, recs, {"source": "test", "game": "fh6"})
    assert read_meta(p)["game"] == "fh6"
    got = list(iter_records(p))
    assert len(got) == len(recs)
    for (t1, d1), (t2, d2) in zip(recs, got):
        assert t1 == pytest.approx(t2) and d1 == d2


def test_truncated_tail_ignored(tmp_path):
    p = tmp_path / "x.frec"
    write_recording(p, _packets(10))
    raw = p.read_bytes()
    p.write_bytes(raw[:-7])  # 模拟程序崩溃：最后一条记录写了一半
    assert len(list(iter_records(p))) == 9


def test_bad_magic(tmp_path):
    p = tmp_path / "bad.frec"
    p.write_bytes(b"hello world")
    with pytest.raises(ValueError):
        list(iter_records(p))


def test_recorder_live_timestamps(tmp_path):
    p = tmp_path / "live.frec"
    with Recorder(p) as rec:
        rec.write(b"\x01" * 232)
        rec.write(b"\x02" * 232)
    ts = [t for t, _ in iter_records(p)]
    assert ts[0] >= 0 and ts[1] >= ts[0]


def test_import_and_replay_to_hub(tmp_path):
    p = tmp_path / "x.frec"
    write_recording(p, _packets(30))
    hub = Hub()
    assert import_file(p, hub) == 30
    assert hub.packets == 30
    hub2 = Hub()
    asyncio.run(replay_to_hub(p, hub2, speed=0))
    assert hub2.packets == 30


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_udp_replay_end_to_end(tmp_path):
    """录像 → UDP 发出 → UDP 收包 → Hub 解析：包数和内容都要对。"""
    from forza.frame import Frame
    from forza.packet import encode

    p = tmp_path / "x.frec"
    frames = [Frame(is_race_on=True, vel_z=float(i), speed=float(i), lap_number=i % 3) for i in range(120)]
    write_recording(p, [(i / 120.0, encode(f, "fh")) for i, f in enumerate(frames)])
    port = _free_port()
    hub = Hub()
    got = []
    hub.subscribe(got.append)

    async def main():
        tr = await start_udp(hub, "127.0.0.1", port)
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, replay_udp, p, "127.0.0.1", port, 4.0, False)
        await asyncio.sleep(0.3)
        tr.close()

    asyncio.run(main())
    assert len(got) == 120
    assert [f.speed for f in got] == [float(i) for i in range(120)]
    assert all(f.fmt == "fh" for f in got)
