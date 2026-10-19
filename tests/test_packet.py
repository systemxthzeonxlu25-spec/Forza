"""包格式：5 种已知包长 round-trip + 偏移核对 + 未知包长自动探测 + 游戏识别。"""
import struct

import pytest

from forza.frame import Frame
from forza.packet import FORMATS, PacketDecoder, encode


def _frame(**kw) -> Frame:
    base = dict(
        is_race_on=True, timestamp_ms=123456, engine_max_rpm=8000.0, engine_idle_rpm=900.0, rpm=6543.5,
        accel_x=3.5, accel_y=-0.2, accel_z=-9.1, vel_x=0.5, vel_y=0.0, vel_z=40.0,
        angvel_x=0.01, angvel_y=0.2, angvel_z=-0.03, yaw=1.0, pitch=0.01, roll=-0.02,
        susp_norm=(0.4, 0.5, 0.6, 0.7), slip_ratio=(0.1, -0.2, 1.3, -1.4), wheel_speed=(120.0, 121.0, 122.0, 123.0),
        rumble=(1, 0, 0, 1), puddle=(0.0, 0.0, 0.5, 0.0), surface_rumble=(0.1, 0.2, 0.3, 0.4),
        slip_angle=(0.9, 1.1, -0.5, -0.6), combined_slip=(1.0, 1.2, 1.4, 1.5), susp_m=(0.05, 0.06, 0.07, 0.08),
        car_ordinal=2240, car_class=5, car_pi=750, drivetrain=1, num_cylinders=8,
        hzn_car_group=3, pos_x=-1234.5, pos_y=56.0, pos_z=789.25, power=250000.0, torque=480.0,
        tire_temp=(190.0, 185.0, 180.0, 175.0), boost=1.5, fuel=0.8, distance=4321.0,
        best_lap=95.5, last_lap=96.25, current_lap=12.5, race_time=300.75, lap_number=3, race_position=2,
        throttle=255, brake=12, clutch=0, handbrake=0, gear=5, steer=-64, driving_line=10, ai_brake_diff=-5,
        tire_wear=(0.1, 0.2, 0.3, 0.4), track_ordinal=860,
    )
    base.update(kw)
    fr = Frame(**base)
    fr.speed = fr.velocity_mag
    return fr


@pytest.mark.parametrize("key,size", [("sled", 232), ("fm7_dash", 311), ("fh_legacy", 323),
                                      ("fh", 324), ("fm2023_dash", 331)])
def test_sizes(key, size):
    assert FORMATS[key].size == size
    assert len(encode(_frame(), key)) == size


@pytest.mark.parametrize("key", list(FORMATS))
def test_roundtrip(key):
    src = _frame()
    out = PacketDecoder().decode(encode(src, key))
    assert out.fmt == key
    assert out.rpm == pytest.approx(src.rpm)
    assert out.susp_norm == pytest.approx(src.susp_norm)
    assert out.slip_ratio == pytest.approx(src.slip_ratio)
    assert out.rumble == src.rumble
    assert out.car_ordinal == 2240 and out.drivetrain == 1 and out.num_cylinders == 8
    if FORMATS[key].has_dash:
        assert out.has_dash
        assert out.speed == pytest.approx(src.speed)
        assert out.tire_temp == pytest.approx(src.tire_temp)
        assert out.lap_number == 3 and out.race_position == 2
        assert out.throttle == 255 and out.brake == 12 and out.gear == 5
        assert out.steer == -64 and out.ai_brake_diff == -5
        assert out.last_lap == pytest.approx(96.25)
        assert out.pos_x == pytest.approx(-1234.5)
    else:
        assert not out.has_dash and out.speed == 0
    if key == "fm2023_dash":
        assert out.track_ordinal == 860 and out.tire_wear == pytest.approx((0.1, 0.2, 0.3, 0.4))
    else:
        assert out.track_ordinal is None and out.tire_wear is None
    if key.startswith("fh"):
        assert out.hzn_car_group == 3 and out.family == "fh"
    else:
        assert out.hzn_car_group is None and out.family == "fm"


def test_offsets_match_reference():
    """直接按文档偏移读字节，确认字段表没错位。"""
    fr = _frame()
    b = encode(fr, "fm2023_dash")
    assert struct.unpack_from("<f", b, 16)[0] == pytest.approx(fr.rpm)            # CurrentEngineRpm
    assert struct.unpack_from("<i", b, 212)[0] == 2240                             # CarOrdinal
    assert struct.unpack_from("<f", b, 232 + 12)[0] == pytest.approx(fr.speed)    # Speed
    assert struct.unpack_from("<H", b, 232 + 68)[0] == 3                           # LapNumber
    assert struct.unpack_from("<b", b, 232 + 76)[0] == -64                         # Steer
    assert struct.unpack_from("<i", b, 327)[0] == 860                              # TrackOrdinal
    h = encode(fr, "fh")
    assert struct.unpack_from("<i", h, 232)[0] == 3                                # Horizon CarGroup
    assert struct.unpack_from("<f", h, 244)[0] == pytest.approx(-1234.5)           # PositionX
    assert struct.unpack_from("<f", h, 244 + 12)[0] == pytest.approx(fr.speed)    # Speed


def test_game_detection_and_override():
    d = PacketDecoder()
    assert d.decode(encode(_frame(), "fh")).game == "fh"
    assert d.decode(encode(_frame(), "fm7_dash")).game == "fm7"
    assert d.decode(encode(_frame(), "fm2023_dash")).game == "fm2023"
    assert d.decode(encode(_frame(), "sled")).game == "fm"
    d6 = PacketDecoder("fh6")
    assert d6.decode(encode(_frame(), "fh")).game == "fh6"
    # 指定了 fh6 却来了 FM 的包：以包为准
    assert d6.decode(encode(_frame(), "fm2023_dash")).game == "fm2023"
    # Dash 格式能唯一确定 FM 代数，override 改不了
    assert PacketDecoder("fm2023").decode(encode(_frame(), "fm7_dash")).game == "fm7"
    assert PacketDecoder("fm2023").decode(encode(_frame(), "sled")).game == "fm2023"


@pytest.mark.parametrize("layout,size", [("fh", 340), ("fm2023_dash", 352), ("fm7_dash", 315)])
def test_unknown_size_autodetect(layout, size):
    """未知包长（游戏以后加字段）：靠 Speed ≈ |速度向量| 自动选出正确的 Dash 偏移。"""
    d = PacketDecoder()
    frames = []
    for i in range(40):
        fr = _frame(vel_z=20.0 + i, vel_x=0.3 * i)
        frames.append((fr, d.decode(encode(fr, layout, total_size=size))))
    src, out = frames[-1]
    assert out.speed == pytest.approx(src.speed)
    assert out.lap_number == 3
    assert out.family == ("fh" if layout == "fh" else "fm")
    assert any(f"{size}" in w for w in d.warnings.values())


def test_too_short_dropped():
    d = PacketDecoder()
    assert d.decode(b"\x00" * 100) is None
    assert d.too_short == 1


def test_layout_verification_warns_on_garbage():
    """已知包长但内容对不上（模拟游戏改了布局）：要报警。"""
    d = PacketDecoder()
    for i in range(400):
        fr = _frame(vel_z=30.0)
        fr.speed = 5.0  # 故意和速度向量对不上
        d.decode(encode(fr, "fh"))
    assert any("对不上" in w for w in d.warnings.values())
