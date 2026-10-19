"""统一的内部数据结构 Frame。

不管包来自哪个游戏、哪种格式，解析后都变成同一个 Frame，下游（实时面板、切圈、分析、诊断）只认它。

约定：
- 坐标系沿用游戏原始定义（车身局部坐标）：X = 右，Y = 上，Z = 前。
- 四轮数组顺序统一为 (FL, FR, RL, RR) = (左前, 右前, 左后, 右后)。
- 速度 m/s，加速度 m/s²，角速度 rad/s，距离 m，时间 s，功率 W，扭矩 N·m。
- 胎温保留游戏原始值（社区共识是华氏度），用 tire_temp_c() 取摄氏度。
- 包里叫 Accel 的 u8 字段是“油门”，这里改名 throttle，避免和加速度 accel_x/y/z 混淆。
"""
from __future__ import annotations

import math
from dataclasses import asdict, dataclass

G = 9.80665  # 标准重力加速度，用来把 m/s² 换成 G

Wheels = tuple[float, float, float, float]
WHEEL_NAMES = ("fl", "fr", "rl", "rr")

_ZERO4: Wheels = (0.0, 0.0, 0.0, 0.0)

# 胎温单位，由 config 在启动时设置（"F" 或 "C"）
TIRE_TEMP_UNIT = "F"


@dataclass(slots=True)
class Frame:
    # ---- 元信息（不是包里的字段，由解析器填） ----
    fmt: str = ""            # 包格式 key，见 packet.FORMATS
    game: str = ""           # 游戏 key，见 profiles.PROFILES
    family: str = ""         # "fm" 或 "fh"
    size: int = 0            # 包长（字节）
    recv_time: float = 0.0   # 收到这个包的时刻（秒；实时=monotonic，回放=录像内相对时间）
    has_dash: bool = False   # 是否带 Dash 段（Sled 格式没有车速/胎温/圈速/输入）

    # ---- Sled 段（所有格式都有） ----
    is_race_on: bool = False     # 1 = 在比赛/驾驶中；0 = 菜单或暂停
    timestamp_ms: int = 0        # 游戏内时间戳（毫秒，会溢出回 0）
    engine_max_rpm: float = 0.0
    engine_idle_rpm: float = 0.0
    rpm: float = 0.0
    accel_x: float = 0.0         # 局部坐标加速度 m/s²：X 右
    accel_y: float = 0.0         # Y 上
    accel_z: float = 0.0         # Z 前
    vel_x: float = 0.0           # 局部坐标速度 m/s
    vel_y: float = 0.0
    vel_z: float = 0.0
    angvel_x: float = 0.0        # 角速度 rad/s：X = 俯仰
    angvel_y: float = 0.0        # Y = 偏航
    angvel_z: float = 0.0        # Z = 侧倾
    yaw: float = 0.0
    pitch: float = 0.0
    roll: float = 0.0
    susp_norm: Wheels = _ZERO4       # 归一化悬挂行程：0 = 伸到最长，1 = 压到底
    slip_ratio: Wheels = _ZERO4      # 归一化滑移率：0 = 完全抓地，|x| > 1 = 失去抓地
    wheel_speed: Wheels = _ZERO4     # 轮子转速 rad/s
    rumble: tuple[int, int, int, int] = (0, 0, 0, 0)  # 1 = 压在路肩上
    puddle: Wheels = _ZERO4          # 水坑深度 0~1
    surface_rumble: Wheels = _ZERO4  # 路面震动（给力反馈用的无量纲值）
    slip_angle: Wheels = _ZERO4      # 归一化侧偏角：0 = 完全抓地，|x| > 1 = 失去抓地
    combined_slip: Wheels = _ZERO4   # 归一化综合滑移
    susp_m: Wheels = _ZERO4          # 悬挂行程（米）
    car_ordinal: int = 0             # 车辆 ID（同一游戏内唯一）
    car_class: int = 0               # 车辆等级：0 (D) ~ 7 (X)
    car_pi: int = 0                  # 性能指数 100~999
    drivetrain: int = 0              # 0 = FWD，1 = RWD，2 = AWD
    num_cylinders: int = 0

    # ---- Horizon 专有 12 字节（232~243） ----
    # FH6 官方文档里叫 CarGroup / SmashableVelDiff / SmashableMass，类型未核实，只存原始值
    hzn_car_group: int | None = None
    hzn_raw1: int | None = None
    hzn_raw2: int | None = None

    # ---- Dash 段 ----
    pos_x: float = 0.0           # 世界坐标 m
    pos_y: float = 0.0
    pos_z: float = 0.0
    speed: float = 0.0           # m/s
    power: float = 0.0           # W
    torque: float = 0.0          # N·m
    tire_temp: Wheels = _ZERO4   # 游戏原始单位（默认按 °F）
    boost: float = 0.0
    fuel: float = 0.0
    distance: float = 0.0        # DistanceTraveled：本场比赛累计行驶距离 m
    best_lap: float = 0.0        # s
    last_lap: float = 0.0        # s
    current_lap: float = 0.0     # s
    race_time: float = 0.0       # CurrentRaceTime s
    lap_number: int = 0          # 从 0 开始
    race_position: int = 0       # 名次；FH 自由漫游时为 0
    throttle: int = 0            # 0~255
    brake: int = 0               # 0~255
    clutch: int = 0              # 0~255
    handbrake: int = 0           # 0~255
    gear: int = 0                # 0 = 倒挡
    steer: int = 0               # -127~127
    driving_line: int = 0        # 归一化行车线
    ai_brake_diff: int = 0       # 归一化 AI 刹车差

    # ---- FM (2023) 扩展 ----
    tire_wear: Wheels | None = None   # 轮胎磨损
    track_ordinal: int | None = None  # 赛道 ID

    # ---------------- 派生量 ----------------
    @property
    def speed_kmh(self) -> float:
        return self.speed * 3.6

    @property
    def velocity_mag(self) -> float:
        """局部速度向量的模长，用来校验 Speed 字段（布局是否解析对）。"""
        return math.sqrt(self.vel_x ** 2 + self.vel_y ** 2 + self.vel_z ** 2)

    @property
    def g_lat(self) -> float:
        """横向 G（正 = 向右）。"""
        return self.accel_x / G

    @property
    def g_long(self) -> float:
        """纵向 G（正 = 加速，负 = 减速）。"""
        return self.accel_z / G

    @property
    def g_vert(self) -> float:
        return self.accel_y / G

    @property
    def throttle_pct(self) -> float:
        return self.throttle / 255.0

    @property
    def brake_pct(self) -> float:
        return self.brake / 255.0

    @property
    def steer_norm(self) -> float:
        return self.steer / 127.0

    @property
    def gear_label(self) -> str:
        if not self.has_dash:
            return "-"
        return "R" if self.gear == 0 else str(self.gear)

    def tire_temp_c(self) -> Wheels:
        """四轮胎温（°C）。"""
        if TIRE_TEMP_UNIT.upper() == "C":
            return self.tire_temp
        return tuple(f_to_c(t) for t in self.tire_temp)  # type: ignore[return-value]

    def to_dict(self) -> dict:
        return asdict(self)


def f_to_c(f: float) -> float:
    return (f - 32.0) * 5.0 / 9.0
