"""包格式契约测试：用独立手写的字节偏移造包，绝不调用 packet.encode()、也不复用它的字段表。

偏移按 Forza 官方支持页面上 FM 和 FH6 的 Data Out 字段声明推出来。
FH4 / FH5 沿用现有的“Horizon 多 12 字节”偏移，这是一个明确写出来、但还没实机核实的假设。
这里的包都是手工构造的，不是从游戏里抓的。
"""
import struct

import pytest

from forza import frame as frame_mod
from forza.lapdata import CHANNEL_NAMES, frame_sample
from forza.packet import PacketDecoder


def raw_dash(size=324, *, lap=2, lap_t=12.5, race_t=100.0, distance=2500.0):
    b = bytearray(size)
    shift = 12 if size in (323, 324) else 0
    for off, fmt, values in [
        (0, "iI", (1, 0xFFFFFFF0)),
        (8, "3f", (8000, 900, 6000)),
        (20, "3f", (9.80665, 0, -9.80665)),
        (32, "3f", (0, 0, 25)),
        (68, "4f", (0.1, 0.2, 0.3, 0.4)),
        (84, "4f", (-1.25, 0.5, 1.5, -0.75)),
        (100, "4f", (70, 71, 72, 73)),
        (116, "4i", (1, 0, 0, 1)),
        (132, "4f", (0, 0.25, 0.5, 1)),
        (164, "4f", (-0.2, 0.4, -1.2, 1.4)),
        (180, "4f", (0.25, 0.5, 1.25, 1.5)),
        (212, "5i", (2240, 5, 800, 1, 8)),
        (232 + shift, "3f", (10, 20, 30)),
        (244 + shift, "3f", (25, 200000, 400)),
        (256 + shift, "4f", (32, 122, 212, 302)),
        (272 + shift, "3f", (4, 0.5, distance)),
        (284 + shift, "4f", (60.25, 61.5, lap_t, race_t)),
        (300 + shift, "H6B3b", (lap, 3, 255, 128, 64, 32, 4, -127, 100, -100)),
    ]:
        struct.pack_into("<" + fmt, b, off, *values)
    if size == 331:
        struct.pack_into("<4fi", b, 311, 0.1, 0.2, 0.3, 0.4, 860)
    return bytes(b)


@pytest.mark.parametrize("size,game", [(311, "fm7"), (331, "fm2023"), (323, "fh5"), (324, "fh5")])
def test_independent_offsets_units_and_wheel_order(size, game, monkeypatch):
    monkeypatch.setattr(frame_mod, "TIRE_TEMP_UNIT", "F")
    f = PacketDecoder(game).decode(raw_dash(size))
    assert f.timestamp_ms == 0xFFFFFFF0
    assert (f.car_ordinal, f.car_pi, f.drivetrain) == (2240, 800, 1)
    assert (f.pos_x, f.pos_y, f.pos_z) == (10, 20, 30)
    assert f.speed == 25 and f.speed_kmh == 90
    assert f.g_lat == pytest.approx(1) and f.g_long == pytest.approx(-1)
    assert f.slip_ratio == (-1.25, 0.5, 1.5, -0.75)
    assert f.slip_angle == pytest.approx((-0.2, 0.4, -1.2, 1.4))
    assert f.combined_slip == (0.25, 0.5, 1.25, 1.5)
    assert f.wheel_speed == (70, 71, 72, 73)
    assert f.puddle == (0, 0.25, 0.5, 1)
    assert (f.best_lap, f.last_lap, f.current_lap, f.race_time) == (60.25, 61.5, 12.5, 100)
    assert (f.lap_number, f.race_position, f.gear) == (2, 3, 4)
    assert (f.throttle, f.brake, f.clutch, f.handbrake, f.steer) == (255, 128, 64, 32, -127)
    assert (f.driving_line, f.ai_brake_diff) == (100, -100)
    sample = dict(zip(CHANNEL_NAMES, frame_sample(f, 12.5, 100)))
    assert sample["speed"] == 25 and sample["throttle"] == 1
    assert sample["brake"] == pytest.approx(128 / 255)
    assert sample["clutch"] == pytest.approx(64 / 255)
    assert sample["handbrake"] == pytest.approx(32 / 255)
    assert sample["steer"] == -1
    assert [sample[f"tire_temp_{w}"] for w in ("fl", "fr", "rl", "rr")] == [0, 50, 100, 150]
    monkeypatch.setattr(frame_mod, "TIRE_TEMP_UNIT", "C")
    assert f.tire_temp_c() == (32, 122, 212, 302)
    if size == 331:
        assert f.track_ordinal == 860 and f.tire_wear == pytest.approx((0.1, 0.2, 0.3, 0.4))
    else:
        assert f.track_ordinal is None and f.tire_wear is None


def test_fh6_documented_integer_fields():
    b = bytearray(raw_dash())
    struct.pack_into("<4i", b, 132, 1, 0, 1, 0)
    struct.pack_into("<Iff", b, 232, 0xF0000001, 3.5, 200)
    f = PacketDecoder("fh6").decode(bytes(b))
    assert f.puddle == (1, 0, 1, 0)
    assert f.hzn_car_group == 0xF0000001
    # 碰撞相关的两个 float 故意按原始位模式保留（不做换算，不丢信息）
    assert f.hzn_raw1 == struct.unpack("<I", struct.pack("<f", 3.5))[0]
    assert f.hzn_raw2 == struct.unpack("<I", struct.pack("<f", 200))[0]


def test_horizon_generation_cannot_be_inferred_from_length():
    assert PacketDecoder().decode(raw_dash()).game == "fh"
    assert PacketDecoder("fh5").decode(raw_dash()).game == "fh5"


@pytest.mark.parametrize("offset", [244, 280, 284, 288, 292, 296])
@pytest.mark.parametrize("bad", [float("nan"), float("inf"), -float("inf")])
def test_nonfinite_core_fields_do_not_enter_pipeline(offset, bad):
    b = bytearray(raw_dash(311))
    struct.pack_into("<f", b, offset, bad)
    assert PacketDecoder().decode(bytes(b)) is None


def test_unknown_stationary_layout_is_not_published_as_dash():
    b = bytes(340)
    assert not PacketDecoder().decode(b).has_dash


def test_unknown_fm_extension_does_not_invent_track_and_tire_wear():
    b = raw_dash(311) + bytes(41)
    d = PacketDecoder()
    for _ in range(25):
        f = d.decode(b)
    assert f.has_dash and f.speed == 25
    assert f.game == "fm" and f.track_ordinal is None and f.tire_wear is None
