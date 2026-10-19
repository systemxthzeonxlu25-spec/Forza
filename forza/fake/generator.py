"""假遥测数据生成器：不开游戏也能拿到“物理上说得通、问题位置精确已知”的数据。

用法：
    from forza.fake import generate, write_fake_recording
    res = generate("circuit", laps=5)        # res.records = [(t, Frame), ...]；res.truth = 标准答案
    truth = write_fake_recording("samples/demo.frec", "fm2023_dash")   # 写录像 + samples/demo.truth.json

每次“驾驶”的流程：
1. 速度规划（经典圈速模拟）：按曲率算每米的过弯极限速度 → 反向扫一遍得到刹车包络
   → 正向扫一遍算加速（功率 / 牵引力 / 风阻限制 + 摩擦圆），同时按转速换挡、撞断油。
2. 按 1/hz 的游戏时间在速度曲线上取样（每米内按匀加速处理，速度、距离、时间严格自洽）。
3. 由 距离 / 速度 / 加速度 推出所有通道：位置、姿态、G 值、踏板、方向盘、挡位转速、悬挂、滑移、胎温……
4. issues=True 时叠加埋好的调车问题（参数都在下面“埋的问题”一节，好找好改）。
5. 场景拼装：菜单帧、比赛帧、收车帧、结算帧；倒带 / 重开 / 冲刺多次跑 都在这一步拼。

约定（和 Frame 一致）：四轮顺序 FL FR RL RR；曲率、横向加速度、方向盘、偏航角速度、侧偏角 都是 + = 右。
胎温在 Frame 里是 °F（游戏原始单位），truth 里写 °C。
"""
from __future__ import annotations

import json
import math
import random
from bisect import bisect_right
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from ..frame import Frame, G
from ..packet import FORMATS, encode
from ..profiles import DEFAULT_GAME_BY_FORMAT
from ..recorder import write_recording
from .track import Corner, Track, build_circuit, build_sprint_route

# ================================================================ 车

# 发动机功率曲线：转速 → 功率 / 峰值功率（峰值 350 kW @ 7000 rpm，扭矩大约 450~520 N·m）
_RPM_TAB = (0.0, 900.0, 1500.0, 2000.0, 3000.0, 4000.0, 5000.0, 6000.0, 7000.0, 7600.0, 8000.0)
_PWR_TAB = (0.0, 0.08, 0.18, 0.29, 0.44, 0.60, 0.77, 0.93, 1.00, 0.99, 0.95)

LONG_FINAL_DRIVE = 3.17    # 正常齿比：6 挡 8000 rpm ≈ 330 km/h，比赛里永远到不了
SHORT_FINAL_DRIVE = 4.10   # 埋问题用的“齿比太短”：6 挡 8000 rpm ≈ 255 km/h，主直道末端撞断油


@dataclass(frozen=True)
class Car:
    """一台后驱 V8 GT 车（物理够用就行，不追求真实车型）。"""
    name: str = "Fake GT (RWD V8)"
    mass: float = 1300.0             # kg
    power_w: float = 350_000.0       # 峰值功率 W
    eta: float = 0.90                # 传动效率
    cda: float = 0.75                # 风阻系数 × 迎风面积 m²
    rho: float = 1.225               # 空气密度
    crr: float = 0.015               # 滚阻系数
    mu_lat_g: float = 1.35           # 横向抓地极限（G）
    brake_tire_g: float = 1.22       # 轮胎能给的最大制动（G）；加上风阻，高速时 ≈ 1.4 G
    traction_g: float = 0.85         # 后驱能给的最大纵向加速（G）
    wheel_r: float = 0.33            # 车轮半径 m
    wheelbase: float = 2.7           # 轴距 m
    ratios: tuple = (3.10, 2.20, 1.68, 1.34, 1.12, 0.95)
    final_drive: float = LONG_FINAL_DRIVE
    max_rpm: float = 8000.0
    idle_rpm: float = 900.0
    upshift_rpm: float = 7400.0      # 加速时到这个转速升挡
    downshift_rpm: float = 6300.0    # 减速时：低一挡的转速不超过这个就降挡
    # 游戏里的车辆信息字段
    car_ordinal: int = 2240
    car_class: int = 5
    car_pi: int = 750
    drivetrain: int = 1              # 1 = RWD
    num_cylinders: int = 8

    @property
    def top_gear(self) -> int:
        return len(self.ratios)

    def rpm_per_ms(self, gear) -> np.ndarray | float:
        """每 1 m/s 车速对应的发动机转速（按挡位）。gear 可以是数组。"""
        r = np.asarray(self.ratios)[np.asarray(gear) - 1]
        return r * self.final_drive / self.wheel_r * 60.0 / (2 * math.pi)

    def v_at_rpm(self, rpm: float, gear: int) -> float:
        return rpm / float(self.rpm_per_ms(gear))

    def resist(self, v):
        """风阻 + 滚阻（N）。"""
        return 0.5 * self.rho * self.cda * v * v + self.crr * self.mass * G

    def gear_for_speed(self, v: float) -> int:
        for g in range(1, self.top_gear + 1):
            if v * float(self.rpm_per_ms(g)) <= self.upshift_rpm:
                return g
        return self.top_gear

    def drive_force(self, v: float, gear: int) -> float:
        """全油门时轮上的驱动力（N），标量版，给速度规划的循环用。"""
        k = self.ratios[gear - 1] * self.final_drive / self.wheel_r
        rpm = v * k * 60.0 / (2 * math.pi)
        rpm = min(max(rpm, self.idle_rpm), self.max_rpm)
        i = min(bisect_right(_RPM_TAB, rpm), len(_RPM_TAB) - 1)
        r0, r1 = _RPM_TAB[i - 1], _RPM_TAB[i]
        p0, p1 = _PWR_TAB[i - 1], _PWR_TAB[i]
        pf = p0 + (p1 - p0) * (rpm - r0) / (r1 - r0)
        torque = self.power_w * pf / (rpm * 2 * math.pi / 60.0)
        return self.eta * torque * k

    def drive_force_vec(self, v: np.ndarray, gear: np.ndarray) -> np.ndarray:
        """全油门驱动力（N），数组版。"""
        k = np.asarray(self.ratios)[gear - 1] * self.final_drive / self.wheel_r
        rpm = np.clip(v * k * 60.0 / (2 * math.pi), self.idle_rpm, self.max_rpm)
        torque = self.power_w * np.interp(rpm, _RPM_TAB, _PWR_TAB) / (rpm * 2 * math.pi / 60.0)
        return self.eta * torque * k

    def to_truth(self) -> dict:
        top_v = self.v_at_rpm(self.max_rpm, self.top_gear)
        return {
            "name": self.name, "mass_kg": self.mass, "power_kw": self.power_w / 1000,
            "drivetrain": "RWD", "drivetrain_code": self.drivetrain,
            "lat_grip_g": self.mu_lat_g, "max_brake_g": 1.4, "max_traction_g": self.traction_g,
            "gears": self.top_gear, "gear_ratios": list(self.ratios), "final_drive": self.final_drive,
            "top_gear_redline_kmh": round(top_v * 3.6, 1),
            "upshift_rpm": self.upshift_rpm, "downshift_rpm": self.downshift_rpm,
            "engine_max_rpm": self.max_rpm, "engine_idle_rpm": self.idle_rpm,
            "wheel_radius_m": self.wheel_r, "wheelbase_m": self.wheelbase,
            "car_ordinal": self.car_ordinal, "car_class": self.car_class, "car_pi": self.car_pi,
            "num_cylinders": self.num_cylinders,
        }


@dataclass(frozen=True)
class Pace:
    """司机开得多猛。"""
    grip: float = 0.97          # 过弯用到横向抓地极限的比例
    brake: float = 1.0          # 刹车力度比例
    power: float = 1.0          # 最大油门比例（漫游时开得温柔点）
    corner_noise: float = 0.004  # 每圈每个弯的抓地随机波动（标准差），让每圈圈速略有不同
    brake_noise: float = 0.012   # 每圈刹车力度随机波动


# ================================================================ 埋的问题（issues=True 时生效）
# 改这里就能调问题的大小；truth 里写的就是这些值。

SLOW_LAP = 2                    # ① 第几圈（lap_number）在 T3 慢了
SLOW_BRAKE_EARLIER_M = 25.0     #    提前多少米刹车
SLOW_SPEED_FACTOR = 0.92        #    弯里最低速度只有平时的 92%

US_FRONT = (1.18, 1.10, 1.25)   # ② 长高速弯弯中转向不足：前轮侧偏（目标值, 下限, 上限）
US_REAR = (0.62, 0.55, 0.70)    #    后轮侧偏
US_STEER_GAIN = 1.30            #    司机多打 30% 方向
US_PHASE = (0.20, 0.80)         #    “弯中”= 圆弧段 20%~80% 的位置（两头各有 12 m 过渡）

OS_REAR = (1.20, 1.10, 1.30)    # ③ 发卡弯出弯转向过度：后轮侧偏
OS_FRONT = (0.60, 0.50, 0.70)   #    前轮侧偏
OS_DURATION_S = 0.6             #    从圆弧结束（开始出弯给油）算起持续多久
WS_RATIO = (1.35, 1.20, 1.50)   #    同时后轮空转：后轮滑移率
WS_DURATION_S = 0.5
WS_MIN_THROTTLE = 0.85          #    空转期间油门至少 85%（就是油给多了）

BOTTOM_EXTRA_GAIN = 0.25        # ④ 前悬太软：前轮压缩对竖直 G 的额外增益（只在压缩方向）

LOCK_FRONT_SR = -1.2            # ⑤ T1 重刹前轮抱死：前轮滑移率
LOCK_REAR_SR = -0.3             #    后轮滑移率
LOCK_DELAY_S = 0.5              #    刹车开始后多久抱死
LOCK_DURATION_S = 0.2

TIRE_STEADY_ISSUE_C = (98.0, 88.0, 90.0, 89.0)   # ⑥ 稳态胎温（FL FR RL RR，°C）：左前明显偏热
TIRE_STEADY_CLEAN_C = (90.0, 89.0, 88.0, 88.0)   #    没问题时
TIRE_START_C = 30.0             #    比赛开始时的胎温
TIRE_TAU_S = 40.0               #    升温时间常数
TIRE_CORNER_GAIN_C = 6.0        #    长弯里外侧轮胎额外升温（快速分量，时间常数 4 s）
TIRE_CORNER_TAU_S = 4.0

REV_LIMIT_FRACTION = 0.98       # ⑦ 撞断油判据：6 挡且转速 ≥ 98% 最高转速

# ================================================================ 其它常量

MENU_S = 1.0                    # 开头菜单帧时长（is_race_on = 0）
RESULTS_S = 1.0                 # 结尾结算帧时长
COOLDOWN_S = 2.0                # 过终点后收车减速时长
COOLDOWN_DECEL = 0.45 * G       # 收车减速度
REWIND_S = 4.0                  # 倒带回退多少秒
REWIND_AT = 0.55                # 第 1 圈（lap_number=1）跑到 55% 时倒带
RESTART_AT = 0.60               # 第 1 圈跑到 60% 时重开
SPRINT_ABANDON_AT = 0.50        # 冲刺第 2 次跑到 50% 时退出
SPRINT_FREEROAM_S = 3.0         # 退出后自由漫游多久
FREEROAM_S = 60.0
TIMESTAMP_START_MS = 123456
TRACK_ORDINAL = 860             # FM2023 的赛道 ID（只对闭合赛道）
HZN_CAR_GROUP = 3
HALF_WB = 1.35                  # 半轴距：前轴在车身参考点前 1.35 m，后轴在后 1.35 m
STEER_LOCK_RAD = 0.22           # 方向盘打满（127）对应的前轮转角
BRAKE_DEADBAND_N = 800.0        # 小于这个的减速力靠收油/发动机制动，不踩刹车
SUSP_TRAVEL_M = 0.12            # 悬挂总行程（susp_m = susp_norm × 0.12）
SUSP_VERT_GAIN = 0.11           # 竖直 G 对悬挂压缩的增益（正常设定）
_BIG = 1000.0                   # “不限速”


# ================================================================ 速度规划

@dataclass
class _Profile:
    """逐米速度曲线。v[i] = 第 i 米节点的速度，gear[i] = 第 i 米到 i+1 米用的挡位。"""
    v: np.ndarray
    gear: np.ndarray
    t: np.ndarray       # 到达每个节点的时刻
    a: np.ndarray       # 每一米内的恒定加速度（长度 n-1）

    @classmethod
    def from_speeds(cls, v, gear) -> _Profile:
        v = np.asarray(v, dtype=float)
        vs = v[1:] + v[:-1]
        seg_t = np.where(vs > 1e-9, 2.0 / np.maximum(vs, 1e-9), 1e9)  # 匀加速走 1 m 的时间
        t = np.concatenate([[0.0], np.cumsum(seg_t)])
        a = (v[1:] ** 2 - v[:-1] ** 2) / 2.0
        return cls(v=v, gear=np.asarray(gear, dtype=int), t=t, a=a)

    def at(self, tau: np.ndarray):
        """在游戏时间 tau 处取 距离、速度、加速度、挡位（数组进数组出，精确匀加速）。"""
        n = len(self.v)
        i = np.clip(np.searchsorted(self.t, tau, side="right") - 1, 0, n - 2)
        u = tau - self.t[i]
        a = self.a[i]
        v0, v1 = self.v[i], self.v[i + 1]
        v = np.clip(v0 + a * u, np.minimum(v0, v1), np.maximum(v0, v1))
        s = np.minimum(i + v0 * u + 0.5 * a * u * u, i + 1.0)
        return s, v, a, self.gear[i]


def _backward(car: Car, vcap, kabs, alat, brk):
    """反向扫：从后往前算“来得及刹住”的最高速度（摩擦圆：转弯时能用的刹车力变小）。"""
    n = len(vcap)
    vb = list(vcap)
    abr = car.brake_tire_g * G
    kr, fr, m = 0.5 * car.rho * car.cda, car.crr * car.mass * G, car.mass
    for i in range(n - 2, -1, -1):
        vn = vb[i + 1]
        r = vn * vn * kabs[i] / alat[i]
        ell = math.sqrt(1.0 - r * r) if r < 1.0 else 0.0
        dec = abr * brk[i] * ell + (kr * vn * vn + fr) / m
        vp = math.sqrt(vn * vn + 2.0 * dec)
        if vp < vb[i]:
            vb[i] = vp
    return vb


def _forward(car: Car, power_scale, vcap, kabs, alat, i_finish, v0, cool_decel, g0: int | None = None):
    """正向扫：功率 / 牵引力（摩擦圆）/ 风阻限制下加速，碰到刹车包络就跟着包络走；顺带换挡。

    i_finish 之后的节点按 cool_decel 收车减速；g0 = 起始挡位（默认按车速选）。
    """
    n = len(vcap)
    v = [0.0] * n
    gear = [1] * n
    m = car.mass
    ftrac = car.traction_g * G * m
    kr, fr = 0.5 * car.rho * car.cda, car.crr * car.mass * G
    top = car.top_gear
    v_top = car.v_at_rpm(car.max_rpm * 0.999, top)   # 6 挡断油时的车速
    k_rpm = [float(car.rpm_per_ms(g)) for g in range(1, top + 1)]
    vi = min(v0, vcap[0])
    g = g0 if g0 else car.gear_for_speed(vi)
    v[0], gear[0] = vi, g
    for i in range(n - 1):
        if i >= i_finish:
            vn2 = vi * vi - 2.0 * cool_decel          # 过终点后收车
        else:
            f_eng = car.drive_force(vi, g) * power_scale
            r = vi * vi * kabs[i] / alat[i]
            ell = math.sqrt(1.0 - r * r) if r < 1.0 else 0.0
            acc = (min(f_eng, ftrac * ell) - kr * vi * vi - fr) / m
            vn2 = vi * vi + 2.0 * acc
        vn = math.sqrt(vn2) if vn2 > 0 else 0.0
        if g == top and vn > v_top:
            vn = v_top                                # 撞断油，再踩也不会更快
        if vn > vcap[i + 1]:
            vn = vcap[i + 1]
        if vn > vi:
            if g < top and vn * k_rpm[g - 1] >= car.upshift_rpm:
                g += 1
        elif vn < vi:
            while g > 1 and vn * k_rpm[g - 2] <= car.downshift_rpm:
                g -= 1
        v[i + 1], gear[i + 1] = vn, g
        vi = vn
    return v, gear


@dataclass
class _PlanInput:
    """速度规划的逐节点输入。"""
    d: np.ndarray        # 每个节点在赛道上的距离（已折回）
    lap: np.ndarray      # 每个节点属于第几圈（几何意义）
    kabs: list
    alat: list           # 横向加速度上限 m/s²
    vcap: list
    brk: list


def _plan_input(track: Track, car: Car, pace: Pace, rng: random.Random, *, n_nodes: int, i_finish: int,
                d0: float = 0.0) -> _PlanInput:
    i = np.arange(n_nodes, dtype=float)
    geo = d0 + i
    d = track.wrap(geo)
    lap = (geo // track.length_m).astype(int) if track.closed else np.zeros(n_nodes, dtype=int)
    kabs = np.abs(track.curvature_at(d))
    cid = track.corner_index_at(d)
    n_laps, n_c = int(lap.max()) + 1, len(track.corners)
    # 每圈每个弯的抓地随机 + 每圈刹车随机（按固定顺序抽，保证可复现）
    cn = np.array([[1.0 + rng.gauss(0.0, pace.corner_noise) for _ in range(n_c)] for _ in range(n_laps)])
    bn = np.array([1.0 + rng.gauss(0.0, pace.brake_noise) for _ in range(n_laps)])
    factor = np.where(cid >= 0, cn[lap, np.maximum(cid, 0)], 1.0)
    alat = car.mu_lat_g * G * pace.grip * factor
    vcap = np.where(kabs > 1e-7, np.sqrt(alat / np.maximum(kabs, 1e-7)), _BIG)
    vcap = np.minimum(vcap, _BIG)
    vcap[i_finish + 1:] = _BIG
    brk = pace.brake * bn[lap]
    return _PlanInput(d=d, lap=lap, kabs=kabs.tolist(), alat=alat.tolist(), vcap=vcap.tolist(), brk=brk.tolist())


def _solve(car: Car, pace: Pace, pin: _PlanInput, *, i_finish: int, v0: float):
    """反向 + 正向扫，返回 (速度, 挡位, 刹车包络)，都是逐米列表。"""
    vb = _backward(car, pin.vcap, pin.kabs, pin.alat, pin.brk)
    v, gear = _forward(car, pace.power, vb, pin.kabs, pin.alat, i_finish, v0, COOLDOWN_DECEL)
    return v, gear, vb


def _brake_onset(v, d, corner: Corner, look_back: float = 250.0) -> float | None:
    """进弯前刹车起点：弯道起点前 look_back 米内，第一个减速度超过 2.5 m/s² 的节点的距离。"""
    v = np.asarray(v)
    a = (v[1:] ** 2 - v[:-1] ** 2) / 2.0
    dd = np.asarray(d)[:-1]
    idx = np.nonzero((dd >= corner.start_m - look_back) & (dd < corner.arc_start_m) & (a < -2.5))[0]
    return float(dd[idx[0]]) if len(idx) else None


def _slow_corner_splice(track: Track, car: Car, pace: Pace, pin: _PlanInput, ref_v, ref_gear, ref_vb, L: int):
    """① 第 SLOW_LAP 圈在慢弯附近局部重新规划，其余部分和对照组完全一样。

    - 弯心最低速度 ×0.92：速度系数从弯道起点的 1 余弦过渡到弯心的 0.92，再回到弯道终点的 1；
    - 刹车起点提前 25 m：司机刹得更轻（刹车力度系数用二分法解出来，正好让起点提前 25 m）。
    窗口 = 上一个弯出口 → 下一个弯弯心；窗口末端两条速度曲线已经重新汇合（都贴着同一条刹车包络）。
    """
    sc = track.role("slow_corner")
    prev_c, next_c = track.corners[sc.index - 1], track.corners[sc.index + 1]
    base = SLOW_LAP * L
    w0, w1 = base + int(prev_c.end_m) + 1, base + int(next_c.apex_m)
    sl = slice(w0, w1 + 1)
    d = pin.d[sl]
    kabs = pin.kabs[sl]
    f = np.ones(len(d))
    pre = (d >= sc.start_m) & (d <= sc.apex_m)
    post = (d > sc.apex_m) & (d <= sc.end_m)
    cut = 1.0 - SLOW_SPEED_FACTOR
    f[pre] = 1.0 - cut * (1 - np.cos(math.pi * (d[pre] - sc.start_m) / (sc.apex_m - sc.start_m))) / 2
    f[post] = 1.0 - cut * (1 - np.cos(math.pi * (sc.end_m - d[post]) / (sc.end_m - sc.apex_m))) / 2
    alat = (np.asarray(pin.alat[sl]) * f * f).tolist()
    ka = np.asarray(kabs)
    vcap = np.minimum(np.where(ka > 1e-7, np.sqrt(np.asarray(alat) / np.maximum(ka, 1e-7)), _BIG), _BIG).tolist()
    vcap[-1] = min(vcap[-1], ref_vb[w1])
    brk0 = np.asarray(pin.brk[sl])
    before = d < sc.arc_start_m

    def run(kb):
        vb = _backward(car, vcap, kabs, alat, np.where(before, brk0 * kb, brk0).tolist())
        return _forward(car, pace.power, vb, kabs, alat, len(vb) + 1, ref_v[w0], 0.0, g0=ref_gear[w0])

    onset_ref = _brake_onset(ref_v[sl], d, sc)
    if onset_ref is None:
        raise RuntimeError(f"{sc.name} 前没有刹车区，没法埋“提前刹车”，请调整赛道")
    target = onset_ref - SLOW_BRAKE_EARLIER_M
    lo, hi = 0.2, 1.0                       # 刹车越轻，起点越早
    best = None
    for _ in range(40):
        mid = (lo + hi) / 2
        v, g = run(mid)
        on = _brake_onset(v, d, sc)
        if best is None or abs(on - target) < abs(best[2] - target) or (on == best[2] and mid > best[0]):
            best = (mid, (v, g), on)
        if on > target:
            hi = mid
        else:
            lo = mid
        if hi - lo < 1e-4:
            break
    kb, (v, g), onset = best
    if abs(v[-1] - ref_v[w1]) > 1e-6 or g[-1] != ref_gear[w1]:
        raise RuntimeError("慢弯局部重规划没能和对照组汇合，请把窗口放大")
    v_all, g_all = list(ref_v), list(ref_gear)
    v_all[w0:w1 + 1], g_all[w0:w1 + 1] = v, g
    seg = (d >= sc.start_m) & (d <= sc.end_m)
    meta = {"brake_factor": kb, "onset_ref_m": onset_ref, "onset_slow_m": onset,
            "min_ref": float(np.asarray(ref_v[sl])[seg].min()), "min_slow": float(np.asarray(v)[seg].min())}
    return v_all, g_all, meta


# ================================================================ 通道合成

@dataclass
class _Drive:
    """一次连续驾驶的“基础帧”（游戏时间 tau 严格等间隔）。倒带/重开在拼装时通过下标实现。"""
    label: str
    tau: np.ndarray
    s: np.ndarray            # 本次驾驶累计距离（从起点/起跑线量）
    d: np.ndarray            # 赛道上的距离（闭合赛道 = 圈内距离）
    geo_lap: np.ndarray      # 几何圈序号
    cols: dict               # Frame 字段名 → 数组
    masks: dict              # 问题名 → 每帧是否“正在发生”
    tire_end_c: tuple = (TIRE_START_C,) * 4
    info: dict = field(default_factory=dict)
    aborted: bool = False    # 重开前被放弃的那次

    @property
    def n(self) -> int:
        return len(self.tau)


def _window(d, lo, hi, ramp):
    """距离窗口权重：[lo, hi] 内为 1，两头各 ramp 米平滑过渡（smoothstep）。"""
    def ss(x):
        x = np.clip(x, 0.0, 1.0)
        return x * x * (3 - 2 * x)
    return ss((d - lo) / ramp) * ss((hi - d) / ramp)


def _trapezoid(n, ramp):
    """长度 n 的时间窗权重：两头各 ramp 帧线性过渡。"""
    w = np.ones(n)
    for k in range(min(ramp, n)):
        f = (k + 1) / (ramp + 1)
        w[k] = min(w[k], f)
        w[n - 1 - k] = min(w[n - 1 - k], f)
    return w


def _noisy(nrng, spec, size):
    """在 (目标, 下限, 上限) 范围内抖一抖。"""
    target, lo, hi = spec
    return np.clip(target + nrng.normal(0.0, (hi - lo) / 8, size), lo + 0.005, hi - 0.005)


def _first_entries(mask: np.ndarray, geo_lap: np.ndarray) -> list[int]:
    """每个几何圈里 mask 第一次从 False 变 True 的帧下标（开头就是 True 的不算）。"""
    out = []
    for lap in np.unique(geo_lap):
        idx = np.nonzero(mask & (geo_lap == lap))[0]
        if len(idx) and idx[0] > 0 and not mask[idx[0] - 1]:
            out.append(int(idx[0]))
    return out


def _synthesize(track: Track, car: Car, *, issues: bool, hz: float, nrng: np.random.Generator,
                tau, s, v, a, gear, d0: float = 0.0, tire_start_c=(TIRE_START_C,) * 4,
                fuel_len: float | None = None) -> tuple[dict, dict]:
    """由 距离/速度/加速度/挡位 生成所有通道（Frame 字段名 → 数组）和问题掩码。"""
    n = len(tau)
    dt = 1.0 / hz
    m, mu = car.mass, car.mu_lat_g
    geo = d0 + s
    d = track.wrap(geo)
    geo_lap = (geo // track.length_m).astype(int) if track.closed else np.zeros(n, dtype=int)
    smp = track.sample(d)
    kap = smp["curvature"]
    v2 = v * v
    accel_x = v2 * kap                        # 横向加速度，+ = 右弯
    glat = accel_x / G
    glong = a / G

    # ---- 竖直方向：坑和起伏 ----
    ddy_f = track.elevation(d + HALF_WB, 2)
    ddy_r = track.elevation(d - HALF_WB, 2)
    accel_y = v2 * smp["ddy"] + nrng.normal(0.0, 0.04, n)
    gv_f, gv_r = v2 * ddy_f / G, v2 * ddy_r / G

    # ---- 踏板：需要的力 vs 全油门能给的力 ----
    f_req = m * a + car.resist(v)
    f_full = car.drive_force_vec(v, gear)
    throttle = np.where(f_req > 0, np.clip(f_req / f_full, 0.0, 1.0), 0.0)
    brake = np.clip((-f_req - BRAKE_DEADBAND_N) / (m * car.brake_tire_g * G), 0.0, 1.0)

    masks: dict[str, np.ndarray] = {}
    w_us = np.zeros(n)
    w_os = np.zeros(n)
    m_ws = np.zeros(n, dtype=bool)
    m_lock = np.zeros(n, dtype=bool)
    sw, hp, hb = track.role("sweeper"), track.role("hairpin"), track.role("heavy_brake")
    if issues:
        # ② 转向不足：长高速弯的弯中
        arc = sw.arc_end_m - sw.arc_start_m
        w_us = _window(d, sw.arc_start_m + US_PHASE[0] * arc, sw.arc_start_m + US_PHASE[1] * arc, 12.0)
        # ③ 转向过度 + 空转：发卡弯圆弧结束后（开始出弯给油）的一段时间
        n_os, n_ws = max(1, round(OS_DURATION_S * hz)), max(1, round(WS_DURATION_S * hz))
        exit_zone = (d >= hp.arc_end_m) & (d < hp.end_m + 150)
        for j0 in _first_entries(exit_zone, geo_lap):
            k = min(n_os, n - j0)
            w_os[j0:j0 + k] = _trapezoid(n_os, max(1, round(0.06 * hz)))[:k]
            m_ws[j0:j0 + min(n_ws, n - j0)] = True
        # ⑤ 前轮抱死：T1 重刹开始后 LOCK_DELAY_S 秒，持续 LOCK_DURATION_S 秒
        zone = (d >= hb.start_m - 500) & (d <= hb.apex_m) & (brake > 0.5)
        for jb in _first_entries(zone, geo_lap):
            j1 = jb + round(LOCK_DELAY_S * hz)
            j2 = j1 + max(1, round(LOCK_DURATION_S * hz))
            if j2 <= n and np.all(brake[j1:j2] > 0.5):
                m_lock[j1:j2] = True
        throttle = np.where(m_ws, np.maximum(throttle, WS_MIN_THROTTLE), throttle)
        # 掩码只算“达到设定幅度”的帧（两头的短暂过渡不算）
        masks["understeer"] = w_us >= 0.99
        masks["oversteer_exit"] = w_os >= 0.99
        masks["wheelspin"] = m_ws
        masks["front_lock"] = m_lock

    # ---- 侧偏角（归一化，|x|>1 = 失去抓地），符号跟着弯走 ----
    sa = np.repeat((0.85 * glat / mu)[:, None], 4, axis=1) + nrng.normal(0.0, 0.012, (n, 4))
    if issues:
        for cols_, spec, w, sign in (((0, 1), US_FRONT, w_us, sw.sign), ((2, 3), US_REAR, w_us, sw.sign),
                                     ((2, 3), OS_REAR, w_os, hp.sign), ((0, 1), OS_FRONT, w_os, hp.sign)):
            for c in cols_:
                tgt = sign * _noisy(nrng, spec, n)
                sa[:, c] += w * (tgt - sa[:, c])

    # ---- 滑移率（归一化）：刹车时四轮为负，加速时后轮（驱动轮）为正 ----
    sr = np.repeat((-0.5 * brake)[:, None], 4, axis=1)
    sr[:, 2:] += (0.5 * np.clip(a, 0.0, None) / (car.traction_g * G))[:, None]
    sr += nrng.normal(0.0, 0.006, (n, 4))
    if issues:
        k = int(m_ws.sum())
        if k:
            sr[m_ws, 2] = _noisy(nrng, WS_RATIO, k)
            sr[m_ws, 3] = _noisy(nrng, WS_RATIO, k)
        k = int(m_lock.sum())
        if k:
            sr[m_lock, 0] = LOCK_FRONT_SR + nrng.normal(0.0, 0.02, k)
            sr[m_lock, 1] = LOCK_FRONT_SR + nrng.normal(0.0, 0.02, k)
            sr[m_lock, 2] = LOCK_REAR_SR + nrng.normal(0.0, 0.02, k)
            sr[m_lock, 3] = LOCK_REAR_SR + nrng.normal(0.0, 0.02, k)
    combined = np.sqrt(sa * sa + sr * sr)
    wheel_speed = (v / car.wheel_r)[:, None] * (1.0 + 0.1 * sr)

    # ---- 转速 / 功率 / 扭矩：转速按后轮（驱动轮）轮速算，空转时会跟着涨 ----
    rpm = (wheel_speed[:, 2] + wheel_speed[:, 3]) / 2 * car.wheel_r * car.rpm_per_ms(gear)
    rpm = np.clip(rpm, car.idle_rpm, car.max_rpm)
    at_lim = (gear == car.top_gear) & (rpm >= 0.995 * car.max_rpm)
    rpm[at_lim] = car.max_rpm - nrng.uniform(0.0, 90.0, int(at_lim.sum()))   # 断油器来回跳
    throttle[at_lim] = 1.0                                                    # 撞着断油还踩死
    omega = rpm * 2 * math.pi / 60.0
    power = np.where(throttle > 0, throttle * car.power_w * np.interp(rpm, _RPM_TAB, _PWR_TAB),
                     -0.04 * car.power_w * rpm / car.max_rpm)                 # 收油时发动机制动
    torque = power / omega
    masks["rev_limiter"] = (gear == car.top_gear) & (rpm >= REV_LIMIT_FRACTION * car.max_rpm)

    # ---- 方向盘：几何转角 + 轻微不足转向梯度；+ = 右 ----
    delta = car.wheelbase * kap + 0.035 * glat
    steer_f = delta / STEER_LOCK_RAD * 127.0
    if issues:
        steer_f = steer_f * (1.0 + (US_STEER_GAIN - 1.0) * w_us) * (1.0 - 1.2 * w_os)  # 转向过度时反打
    steer = np.clip(np.round(steer_f + nrng.normal(0.0, 0.6, n)), -127, 127).astype(int)

    # ---- 姿态：车身侧偏角 beta（车头比速度方向更朝弯内），俯仰、侧倾 ----
    beta = 0.012 * glat / mu
    if issues:
        beta = beta + 0.05 * w_os * hp.sign       # 甩尾：车头额外朝弯内转
    yaw = (smp["heading"] + beta + math.pi) % (2 * math.pi) - math.pi
    vel_x = -v * np.sin(beta)
    vel_z = v * np.cos(beta)
    vel_y = np.zeros(n)
    pitch = np.arctan(smp["dy"]) + 0.006 * glong   # + = 抬头（加速抬头、刹车点头）
    roll = 0.012 * glat                            # 每 G 横向侧倾 0.012 rad，+ = 右弯
    grad = (lambda x: np.gradient(x, dt)) if n > 1 else (lambda x: np.zeros_like(x))
    angvel_y = v * kap + grad(beta)                # 偏航角速度 = v·κ（+ 侧偏角变化，平时≈0）
    angvel_x = grad(pitch)
    angvel_z = grad(roll)

    # ---- 悬挂（0 = 伸到最长，1 = 压到底）----
    base = 0.45 + 0.05 * (v / 70.0) ** 2           # 下压力让车速越高压得越多
    lat = 0.11 * np.clip(glat, -1.6, 1.6)          # 右弯（glat>0）时左侧（外侧）压缩
    lon = 0.09 * np.clip(glong, -1.6, 1.6)         # 刹车前压，加速后压
    cv_f, cv_r = SUSP_VERT_GAIN * gv_f, SUSP_VERT_GAIN * gv_r
    if issues:
        cv_f = cv_f + BOTTOM_EXTRA_GAIN * np.clip(gv_f, 0.0, None)   # ④ 前悬太软：压坑时触底
    susp = np.stack([base + lat - lon + cv_f, base - lat - lon + cv_f,
                     base + lat + lon + cv_r, base - lat + lon + cv_r], axis=1)
    susp = np.clip(susp + nrng.normal(0.0, 0.005, (n, 4)), 0.0, 1.0)
    masks["bottoming"] = (susp[:, 0] >= 0.99) | (susp[:, 1] >= 0.99)

    # ---- 胎温：慢分量（向稳态靠拢，时间常数 40 s）+ 快分量（长弯外侧轮胎额外升温）----
    steady = np.array(TIRE_STEADY_ISSUE_C if issues else TIRE_STEADY_CLEAN_C)
    start = np.array(tire_start_c, dtype=float)
    slow = steady + (start - steady) * np.exp(-(tau - tau[0])[:, None] / TIRE_TAU_S)
    load_l = np.clip(glat / mu, 0.0, 1.0).tolist()     # 右弯时左侧是外侧
    load_r = np.clip(-glat / mu, 0.0, 1.0).tolist()
    alpha = 1.0 - math.exp(-dt / TIRE_CORNER_TAU_S)
    fast_l, fast_r, fl_, fr_ = [0.0] * n, [0.0] * n, 0.0, 0.0
    for j in range(n):
        fl_ += (TIRE_CORNER_GAIN_C * load_l[j] - fl_) * alpha
        fr_ += (TIRE_CORNER_GAIN_C * load_r[j] - fr_) * alpha
        fast_l[j], fast_r[j] = fl_, fr_
    fast_l, fast_r = np.array(fast_l), np.array(fast_r)
    temp_c = slow + np.stack([fast_l, fast_r, fast_l, fast_r], axis=1) + nrng.normal(0.0, 0.05, (n, 4))
    tire_f = temp_c * 9.0 / 5.0 + 32.0

    # ---- 路肩：连续弯的弯心，内侧车轮压路肩约 0.2 s（前轴先压，后轴晚一个轴距）----
    rumble = np.zeros((n, 4), dtype=int)
    for name in track.roles.get("chicane", []):
        c = track.corner(name)
        front, rear = (1, 3) if c.sign > 0 else (0, 2)
        rumble[np.abs(d + HALF_WB - c.apex_m) <= 0.1 * v, front] = 1
        rumble[np.abs(d - HALF_WB - c.apex_m) <= 0.1 * v, rear] = 1

    # ---- 消耗：每圈油 -2%、胎磨 +1% ----
    per = fuel_len or track.length_m
    fuel = 1.0 - 0.02 * s / per
    wear = np.repeat((0.01 * s / per)[:, None], 4, axis=1)

    cols = {
        "rpm": rpm, "accel_x": accel_x, "accel_y": accel_y, "accel_z": a.astype(float),
        "vel_x": vel_x, "vel_y": vel_y, "vel_z": vel_z,
        "angvel_x": angvel_x, "angvel_y": angvel_y, "angvel_z": angvel_z,
        "yaw": yaw, "pitch": pitch, "roll": roll,
        "susp_norm": susp, "slip_ratio": sr, "wheel_speed": wheel_speed, "rumble": rumble,
        "slip_angle": sa, "combined_slip": combined, "susp_m": susp * SUSP_TRAVEL_M,
        "pos_x": smp["x"], "pos_y": smp["y"], "pos_z": smp["z"],
        "speed": v.astype(float), "power": power, "torque": torque, "tire_temp": tire_f,
        "fuel": fuel, "tire_wear": wear,
        "throttle": np.round(throttle * 255).astype(int), "brake": np.round(brake * 255).astype(int),
        "gear": gear.astype(int), "steer": steer,
    }
    cols["_temp_c"] = temp_c
    return cols, masks


def _make_drive(label, track, car, *, issues, hz, nrng, prof: _Profile, t_end: float, d0=0.0,
                tire_start_c=(TIRE_START_C,) * 4, fuel_len=None) -> _Drive:
    """在速度曲线上按 1/hz 取样并合成通道。"""
    n = int(math.floor(t_end * hz + 1e-9)) + 1
    tau = np.arange(n) / hz
    s, v, a, gear = prof.at(tau)
    cols, masks = _synthesize(track, car, issues=issues, hz=hz, nrng=nrng, tau=tau, s=s, v=v, a=a, gear=gear,
                              d0=d0, tire_start_c=tire_start_c, fuel_len=fuel_len)
    geo = d0 + s
    dr = _Drive(label=label, tau=tau, s=s, d=track.wrap(geo),
                geo_lap=(geo // track.length_m).astype(int) if track.closed else np.zeros(n, dtype=int),
                cols=cols, masks=masks)
    dr.tire_end_c = tuple(float(x) for x in cols["_temp_c"][-1])
    return dr


def _sub_rng(seed: int, label: str) -> tuple[random.Random, np.random.Generator]:
    """每次驾驶独立的随机数流（同一个 seed 下，主比赛不受“有没有重开”影响）。"""
    r = random.Random(f"forza-fake:{seed}:{label}")
    return r, np.random.default_rng(r.getrandbits(64))


# ================================================================ 场景：环形赛道比赛

def _race_drive(label, track: Track, car: Car, *, laps: int, hz: float, seed: int, issues: bool,
                slow_corner: bool) -> tuple[_Drive, dict]:
    """站立起步跑 laps 圈 + 收车。返回驾驶和附加信息（过线时刻、慢弯的时间损失等）。"""
    rng, nrng = _sub_rng(seed, label)
    L = int(round(track.length_m))
    i_finish = laps * L
    n_nodes = i_finish + 400                      # 终点后再留 400 m 收车（主直道够长）
    pace = Pace()
    pin = _plan_input(track, car, pace, rng, n_nodes=n_nodes, i_finish=i_finish)
    info: dict = {}
    v, gear, vb = _solve(car, pace, pin, i_finish=i_finish, v0=0.0)
    if slow_corner:
        # 对照组 = 不埋问题的同一次驾驶；埋问题的版本只在第 2 圈 T3 附近不同
        v_s, g_s, meta = _slow_corner_splice(track, car, pace, pin, v, gear, vb, L)
        info["ref_profile"] = _Profile.from_speeds(v, gear)
        info["slow_profile"] = prof = _Profile.from_speeds(v_s, g_s)
        info["slow_meta"] = meta
    else:
        prof = _Profile.from_speeds(v, gear)
    t_cross = np.array([prof.t[k * L] for k in range(1, laps + 1)])
    dr = _make_drive(label, track, car, issues=issues, hz=hz, nrng=nrng, prof=prof, t_end=t_cross[-1] + COOLDOWN_S)
    # ---- 比赛字段 ----
    tau = dr.tau
    lap_number = np.searchsorted(t_cross, tau, side="right")
    starts = np.concatenate([[0.0], t_cross])
    lap_times = np.diff(starts)
    best_by_count = np.concatenate([[0.0], np.minimum.accumulate(lap_times)])
    last_by_count = np.concatenate([[0.0], lap_times])
    dr.cols.update({
        "lap_number": lap_number, "current_lap": tau - starts[lap_number], "race_time": tau.copy(),
        "last_lap": last_by_count[lap_number], "best_lap": best_by_count[lap_number],
        "distance": dr.s.copy(), "race_position": np.ones(dr.n, dtype=int),
    })
    if slow_corner:
        sc = track.role("slow_corner")
        onset = info["slow_meta"]["onset_slow_m"]
        dr.masks["slow_corner"] = (lap_number == SLOW_LAP) & (dr.d >= onset) & (dr.d <= sc.end_m)
    info.update({"t_cross": t_cross, "lap_times": lap_times, "profile": prof, "L": L})
    dr.info = info
    return dr, info


# ================================================================ 拼装

@dataclass
class _Piece:
    label: str                        # menu / race / results / ...
    drive: _Drive | None = None
    idx: np.ndarray | None = None     # 用哪些基础帧（倒带就是下标往回跳）
    n_idle: int = 0                   # 空闲帧（is_race_on = 0）的数量


_WHEEL_COLS = ("susp_norm", "slip_ratio", "wheel_speed", "rumble", "slip_angle", "combined_slip", "susp_m",
               "tire_temp", "tire_wear")
_FRAME_COLS = ("rpm", "accel_x", "accel_y", "accel_z", "vel_x", "vel_y", "vel_z", "angvel_x", "angvel_y",
               "angvel_z", "yaw", "pitch", "roll", *_WHEEL_COLS, "pos_x", "pos_y", "pos_z", "speed", "power",
               "torque", "fuel", "distance", "best_lap", "last_lap", "current_lap", "race_time", "lap_number",
               "race_position", "throttle", "brake", "gear", "steer")


def _static_fields(car: Car, family: str, track_ordinal: int | None) -> dict:
    """每一帧都一样的字段（菜单帧也带）。"""
    return {
        "family": family, "game": "fm2023" if family == "fm" else "fh", "has_dash": True,
        "engine_max_rpm": car.max_rpm, "engine_idle_rpm": car.idle_rpm,
        "car_ordinal": car.car_ordinal, "car_class": car.car_class, "car_pi": car.car_pi,
        "drivetrain": car.drivetrain, "num_cylinders": car.num_cylinders,
        "hzn_car_group": HZN_CAR_GROUP, "hzn_raw1": 0, "hzn_raw2": 0, "track_ordinal": track_ordinal,
    }


@dataclass
class _Assembled:
    records: list
    rec_piece: np.ndarray       # 每条记录来自哪个 piece（-1 = 空闲帧）
    rec_j: np.ndarray           # 对应的基础帧下标
    pieces: list
    phases: list


def _assemble(pieces: list[_Piece], hz: float, static: dict) -> _Assembled:
    """把各段拼成 [(t, Frame), ...]。t 从 0 开始、严格按 1/hz 递增；timestamp_ms 一直往前走。"""
    records: list[tuple[float, Frame]] = []
    rec_piece: list[int] = []
    rec_j: list[int] = []
    phases: list[dict] = []
    k = 0
    for pi, pc in enumerate(pieces):
        k0 = k
        if pc.drive is None:
            for _ in range(pc.n_idle):
                t = k / hz
                records.append((t, Frame(is_race_on=False, recv_time=t, timestamp_ms=_ts(k, hz),
                                         tire_wear=(0.0, 0.0, 0.0, 0.0), **static)))
                rec_piece.append(-1)
                rec_j.append(-1)
                k += 1
        else:
            dr = pc.drive
            cols = {}
            for name in _FRAME_COLS:
                arr = dr.cols[name]
                cols[name] = [tuple(r) for r in arr.tolist()] if name in _WHEEL_COLS else arr.tolist()
            names = list(cols)
            lists = [cols[nm] for nm in names]
            for j in pc.idx.tolist():
                t = k / hz
                kw = {nm: lst[j] for nm, lst in zip(names, lists)}
                records.append((t, Frame(is_race_on=True, recv_time=t, timestamp_ms=_ts(k, hz), **static, **kw)))
                rec_piece.append(pi)
                rec_j.append(j)
                k += 1
        if k > k0:
            phases.append({"phase": pc.label, "t_start": round(k0 / hz, 6), "t_end": round((k - 1) / hz, 6),
                           "records": k - k0})
    return _Assembled(records, np.array(rec_piece), np.array(rec_j), pieces, phases)


def _ts(k: int, hz: float) -> int:
    return (TIMESTAMP_START_MS + int(k * 1000.0 / hz)) % (1 << 32)


def _record_mask(asm: _Assembled, key: str) -> np.ndarray:
    """某个问题掩码映射到最终录像的每条记录上（空闲帧为 False）。"""
    out = np.zeros(len(asm.rec_piece), dtype=bool)
    for pi, pc in enumerate(asm.pieces):
        if pc.drive is not None and key in pc.drive.masks:
            sel = np.nonzero(asm.rec_piece == pi)[0]
            out[sel] = pc.drive.masks[key][asm.rec_j[sel]]
    return out


def _occurrences(asm: _Assembled, key: str, hz: float, *, run_of_piece: dict | None = None) -> list[dict]:
    """在最终录像里找某个问题“正在发生”的连续片段（倒带/重开造成的跳变处会断开）。"""
    on = _record_mask(asm, key)
    n = len(on)
    cont = np.zeros(n, dtype=bool)        # 和上一条记录是同一次驾驶的相邻基础帧
    cont[1:] = (asm.rec_piece[1:] == asm.rec_piece[:-1]) & (asm.rec_j[1:] == asm.rec_j[:-1] + 1)
    out = []
    for k0 in np.nonzero(on & ~(np.concatenate([[False], on[:-1]]) & cont))[0].tolist():
        k1 = k0
        while k1 + 1 < n and on[k1 + 1] and cont[k1 + 1]:
            k1 += 1
        pi = int(asm.rec_piece[k0])
        dr = asm.pieces[pi].drive
        j0, j1 = int(asm.rec_j[k0]), int(asm.rec_j[k1])
        o = {"lap_number": int(dr.cols["lap_number"][j0]), "t_start": round(k0 / hz, 6), "t_end": round(k1 / hz, 6),
             "duration_s": round((k1 - k0 + 1) / hz, 6),
             "race_time_start": round(float(dr.cols["race_time"][j0]), 6),
             "lap_dist_start_m": round(float(dr.d[j0]), 2), "lap_dist_end_m": round(float(dr.d[j1]), 2)}
        if run_of_piece and pi in run_of_piece:
            o["run"] = run_of_piece[pi]
        if dr.aborted:
            o["aborted_attempt"] = True
        out.append(o)
    return out


def _dist_range(occ: list[dict]) -> list[float] | None:
    if not occ:
        return None
    return [min(o["lap_dist_start_m"] for o in occ), max(o["lap_dist_end_m"] for o in occ)]


def _issue_truth(asm: _Assembled, track: Track, car: Car, hz: float, *, run_of_piece=None,
                 extra: dict | None = None) -> dict:
    """把每个问题的说明 + 在录像里实际出现的位置写成 truth["issues"]。"""
    def occ(key):
        return _occurrences(asm, key, hz, run_of_piece=run_of_piece)

    sw, hp, hb = track.role("sweeper"), track.role("hairpin"), track.role("heavy_brake")
    arc = sw.arc_end_m - sw.arc_start_m
    out: dict = {}
    if extra and "slow_corner" in extra:
        out["slow_corner"] = extra["slow_corner"]
        out["slow_corner"]["occurrences"] = occ("slow_corner")
    items = {
        "understeer": {
            "corner": sw.name, "phase": "mid", "laps": "all",
            "description": "长高速弯弯中转向不足：前轮侧偏角明显超过抓地极限，后轮还很富余，司机多打了约 30% 方向",
            "design_dist_m": [round(sw.arc_start_m + US_PHASE[0] * arc, 2), round(sw.arc_start_m + US_PHASE[1] * arc, 2)],
            "front_slip_angle_abs": [US_FRONT[1], US_FRONT[2]], "rear_slip_angle_abs": [US_REAR[1], US_REAR[2]],
            "steer_extra_factor": US_STEER_GAIN, "slip_angle_sign": sw.sign,
        },
        "oversteer_exit": {
            "corner": hp.name, "phase": "exit", "laps": "all",
            "description": "发卡弯出弯给油时转向过度（后轮侧偏大、前轮小），同时后轮空转；司机在反打方向",
            "starts_at_m": round(hp.arc_end_m, 2), "duration_s": OS_DURATION_S,
            "note": "occurrences 只记达到设定幅度的帧；窗口两头各有约 0.06 s 线性过渡",
            "rear_slip_angle_abs": [OS_REAR[1], OS_REAR[2]], "front_slip_angle_abs": [OS_FRONT[1], OS_FRONT[2]],
            "slip_angle_sign": hp.sign, "min_throttle": WS_MIN_THROTTLE,
            "wheelspin": {"rear_slip_ratio": [WS_RATIO[1], WS_RATIO[2]], "duration_s": WS_DURATION_S,
                          "occurrences": occ("wheelspin")},
        },
        "bottoming": {
            "corner": None, "location": "坑（bump）所在直道", "phase": "straight", "laps": "all",
            "description": "前悬太软，压过坑的底部时前轮悬挂压到底",
            "bump_dist_m": round(track.bump_dist_m, 3), "wheels": ["fl", "fr"], "susp_norm_min": 0.99,
            "typical_duration_s": 0.15,
        },
        "front_lock": {
            "corner": hb.name, "phase": "braking", "laps": "all",
            "description": "主直道末端重刹时前轮抱死，后轮正常",
            "front_slip_ratio": LOCK_FRONT_SR, "rear_slip_ratio": LOCK_REAR_SR,
            "starts_after_brake_onset_s": LOCK_DELAY_S, "duration_s": LOCK_DURATION_S,
        },
        "rev_limiter": {
            "corner": None, "location": "主直道末端（进 %s 前）" % hb.name, "phase": "straight", "laps": "all",
            "description": "齿比太短：最高挡在直道末端撞断油，刹车前持续 ≥ 1.5 s",
            "gear": car.top_gear, "rpm_threshold": REV_LIMIT_FRACTION * car.max_rpm,
            "final_drive": car.final_drive, "top_gear_redline_kmh": round(car.v_at_rpm(car.max_rpm, car.top_gear) * 3.6, 1),
            "min_duration_s": 1.5,
        },
    }
    for key, d in items.items():
        o = occ(key)
        if not o:
            continue        # 这次录像里根本没发生（比如漫游没开到那里），就不写
        d["occurrences"] = o
        d["dist_range_m"] = _dist_range(o)
        out[key] = d
    if "oversteer_exit" in out:
        out["oversteer_exit"]["wheelspin"]["dist_range_m"] = _dist_range(out["oversteer_exit"]["wheelspin"]["occurrences"])
    out["tire_temp_imbalance"] = {
        "corner": None, "phase": "all", "description": "左前胎明显比右前胎热（稳态差约 10 °C）",
        "steady_state_c": dict(zip(("fl", "fr", "rl", "rr"), TIRE_STEADY_ISSUE_C)),
        "start_temp_c": TIRE_START_C, "time_constant_s": TIRE_TAU_S,
        "note": "慢分量按 40 s 时间常数从起始胎温趋近稳态；另有长弯外侧轮胎的几 °C 快速升温。Frame 里是 °F",
    }
    return out


def _base_truth(scenario, family, hz, seed, issues, car: Car, track: Track, laps=None, rewind=False,
                restart=False) -> dict:
    return {
        "generator": "forza.fake", "version": 1,
        "scenario": scenario, "family": family, "hz": hz, "seed": seed, "issues_enabled": issues,
        "laps_requested": laps, "rewind": rewind, "restart": restart,
        "car": {**car.to_truth(), "tire_steady_c": dict(zip(("fl", "fr", "rl", "rr"),
                                                             TIRE_STEADY_ISSUE_C if issues else TIRE_STEADY_CLEAN_C))},
        "sign_conventions": {
            "steer": "+ = right", "accel_x": "+ = right turn", "curvature": "+ = right",
            "angvel_y": "+ = right turn（= v·κ + dβ/dt）", "slip_angle": "+ = right turn",
            "slip_ratio": "+ = 驱动打滑, - = 刹车抱死", "yaw": "0 = +z，右转增大；前进方向 (sin yaw, cos yaw)",
            "pitch": "+ = 抬头", "roll": "+ = 右弯（0.012 rad/G）",
            "yaw_vs_heading": "yaw = 赛道航向 + β（车身侧偏角，平时 < 1°，发卡弯出弯甩尾时约 3°）",
            "vel": "vel_x = -speed·sin β（右弯时略向左滑），vel_z = speed·cos β，vel_y = 0，speed == |vel|",
            "wheel_order": ["fl", "fr", "rl", "rr"], "tire_temp": "Frame 里 °F，truth 里 °C",
            "distance_m": "truth 里的距离都是沿中心线从起跑线（起点）量的米数",
        },
        "track": track.to_truth(),
        "laps": [], "runs": [], "issues": {}, "events": [], "notes": [],
    }


def _circuit(family, laps, hz, seed, issues, rewind, restart) -> tuple[list, dict]:
    if laps < 1:
        raise ValueError("circuit 至少跑 1 圈")
    if rewind and laps < 2:
        raise ValueError("rewind 发生在 lap_number=1，至少要 2 圈")
    track = build_circuit()
    car = _make_car(issues)
    slow = issues and laps > SLOW_LAP
    main, info = _race_drive("main", track, car, laps=laps, hz=hz, seed=seed, issues=issues, slow_corner=slow)
    L = info["L"]
    truth = _base_truth("circuit", family, hz, seed, issues, car, track, laps, rewind, restart)
    truth["track"]["track_ordinal"] = TRACK_ORDINAL
    pieces = [_Piece("menu", n_idle=round(MENU_S * hz))]
    events: list[dict] = []
    aborted = None
    if restart:
        aborted, ainfo = _race_drive("aborted", track, car, laps=2, hz=hz, seed=seed, issues=issues,
                                     slow_corner=False)
        aborted.aborted = True
        lapn = aborted.cols["lap_number"]
        j_cut = int(np.nonzero((lapn == 1) & (aborted.d >= RESTART_AT * L))[0][0])
        pieces.append(_Piece("race_aborted", aborted, np.arange(j_cut + 1)))
        events.append({"type": "restart", "_piece": len(pieces) - 1, "_j": j_cut,
                       "lap_number": 1, "lap_dist_m": round(float(aborted.d[j_cut]), 2),
                       "race_time_before_s": round(float(aborted.tau[j_cut]), 6),
                       "distance_before_m": round(float(aborted.s[j_cut]), 2),
                       "aborted_laps": [{"lap_number": 0, "lap_time": round(float(ainfo["lap_times"][0]), 6),
                                         "kind": "standing_start", "complete": True}],
                       "description": "第 1 圈 60% 处重开比赛：下一帧 race_time≈0、lap_number=0、distance≈0、车回到起跑线静止"})
    n = main.n
    if rewind:
        j_r = int(np.nonzero((main.cols["lap_number"] == 1) & (main.d >= REWIND_AT * L))[0][0])
        R = round(REWIND_S * hz)
        idx = np.concatenate([np.arange(j_r), np.arange(j_r - 1 - R, n)])
        pieces.append(_Piece("race", main, idx))
        events.append({"type": "rewind", "_piece": len(pieces) - 1, "_k_in_piece": j_r,
                       "lap_number": 1, "jump_s": round(R / hz, 6),
                       "race_time_before_s": round(float(main.tau[j_r - 1]), 6),
                       "race_time_after_s": round(float(main.tau[j_r - 1 - R]), 6),
                       "lap_dist_before_m": round(float(main.d[j_r - 1]), 2),
                       "lap_dist_after_m": round(float(main.d[j_r - 1 - R]), 2),
                       "description": "倒带：游戏字段整体回到 4 s 前那一帧（包括位置、距离、胎温），然后重新开；录像时间 t 照常往前"})
    else:
        pieces.append(_Piece("race", main, np.arange(n)))
    pieces.append(_Piece("results", n_idle=round(RESULTS_S * hz)))
    asm = _assemble(pieces, hz, _static_fields(car, family, TRACK_ORDINAL))

    # ---- 事件里的录像时间 ----
    starts = _piece_starts(asm)
    for ev in events:
        pi = ev.pop("_piece")
        if ev["type"] == "restart":
            ev["t"] = round((starts[pi] + ev.pop("_j") + 1) / hz, 6)      # 重开后第一帧
        else:
            ev["t"] = round((starts[pi] + ev.pop("_k_in_piece")) / hz, 6)  # 倒带后第一帧
    truth["events"] = events

    # ---- 圈 ----
    race_pi = next(i for i, p in enumerate(pieces) if p.label == "race")
    t_cross = info["t_cross"]
    laps_out = []
    prev_t = None
    for k in range(laps):
        rec_k = _first_record(asm, race_pi, main.cols["lap_number"] >= k + 1)
        j = asm.rec_j[rec_k]
        t_rec = rec_k / hz - (float(main.tau[j]) - float(t_cross[k]))
        lt = float(info["lap_times"][k])
        laps_out.append({
            "lap_number": k, "lap_time": round(lt, 6), "kind": "standing_start" if k == 0 else "flying",
            "complete": True, "t_start": round(prev_t if prev_t is not None else _race_start_t(asm, race_pi, hz), 6),
            "t_end": round(t_rec, 6), "race_time_end_s": round(float(t_cross[k]), 6),
            "corner_min_speed_kmh": _corner_min_speeds(main, track, k),
        })
        prev_t = t_rec
    truth["laps"] = laps_out
    truth["finish"] = {"lap_number_after_finish": laps, "cooldown_s": COOLDOWN_S,
                       "race_time_s": round(float(t_cross[-1]), 6), "t": laps_out[-1]["t_end"],
                       "note": "过终点后 lap_number = laps，race_time/current_lap 继续走，车减速 2 s，然后 1 s is_race_on=0"}

    # ---- 问题 ----
    if issues:
        extra = {}
        if slow:
            extra["slow_corner"] = _slow_corner_truth(track, info)
        truth["issues"] = _issue_truth(asm, track, car, hz, extra=extra)
        if not slow:
            truth["notes"].append(f"laps={laps} 不足 {SLOW_LAP + 1} 圈，没有埋 slow_corner")
    truth["phases"] = asm.phases
    truth["n_records"] = len(asm.records)
    return asm.records, truth


def _piece_starts(asm: _Assembled) -> list[int]:
    starts, k = [], 0
    for pc in asm.pieces:
        starts.append(k)
        k += pc.n_idle if pc.drive is None else len(pc.idx)
    return starts


def _first_record(asm: _Assembled, pi: int, cond: np.ndarray) -> int:
    """第 pi 段里第一条满足 cond（按基础帧下标的布尔数组）的记录序号。"""
    sel = np.nonzero(asm.rec_piece == pi)[0]
    return int(sel[cond[asm.rec_j[sel]]][0])


def _race_start_t(asm: _Assembled, pi: int, hz: float) -> float:
    return _piece_starts(asm)[pi] / hz


def _corner_min_speeds(dr: _Drive, track: Track, lap: int) -> dict:
    out = {}
    lapn = dr.cols["lap_number"]
    for c in track.corners:
        sel = (lapn == lap) & (dr.d >= c.start_m) & (dr.d <= c.end_m)
        if sel.any():
            out[c.name] = round(float(dr.cols["speed"][sel].min()) * 3.6, 2)
    return out


def _slow_corner_truth(track: Track, info: dict) -> dict:
    sc = track.role("slow_corner")
    L = info["L"]
    prof, ref, meta = info["slow_profile"], info["ref_profile"], info["slow_meta"]
    base = SLOW_LAP * L
    t_lap = prof.t[base + L] - prof.t[base]
    t_ref = ref.t[base + L] - ref.t[base]
    w0, w1 = base + int(meta["onset_slow_m"]) - 10, base + int(sc.end_m) + 150
    t_win = (prof.t[w1] - prof.t[w0]) - (ref.t[w1] - ref.t[w0])
    return {
        "lap_number": SLOW_LAP, "corner": sc.name, "phase": "entry+mid",
        "description": (f"第 {SLOW_LAP} 圈进 {sc.name} 提前 {SLOW_BRAKE_EARLIER_M:.0f} m 开始刹车（刹得更轻）、"
                        f"弯心最低速度只有平时的 {SLOW_SPEED_FACTOR:.0%}，这圈慢的时间几乎都丢在这个弯"),
        "brake_earlier_m": round(meta["onset_ref_m"] - meta["onset_slow_m"], 3),
        "min_speed_factor": round(meta["min_slow"] / meta["min_ref"], 4),
        "design": {"brake_earlier_m": SLOW_BRAKE_EARLIER_M, "min_speed_factor": SLOW_SPEED_FACTOR,
                   "brake_pressure_factor": round(meta["brake_factor"], 4)},
        "expected_time_loss_s": round(float(t_lap - t_ref), 4),
        "time_loss_in_window_s": round(float(t_win), 4),
        "loss_window_m": [round(meta["onset_slow_m"] - 10, 1), round(sc.end_m + 150, 1)],
        "ref_brake_point_m": meta["onset_ref_m"], "slow_brake_point_m": meta["onset_slow_m"],
        "ref_min_speed_kmh": round(meta["min_ref"] * 3.6, 2), "slow_min_speed_kmh": round(meta["min_slow"] * 3.6, 2),
        "reference": "同一次驾驶、同一套随机数、这个弯不埋问题的对照模拟（窗口外两者完全相同）",
    }


def _make_car(issues: bool) -> Car:
    return Car(final_drive=SHORT_FINAL_DRIVE if issues else LONG_FINAL_DRIVE)


# ================================================================ 场景：冲刺（Horizon）

_SPRINT_RUNS = (
    # (第几次, 司机节奏, 名次, 是否跑完)
    (1, Pace(grip=0.95, brake=0.95), 3, True),
    (2, Pace(grip=0.96, brake=0.97), 5, False),
    (3, Pace(grip=0.975, brake=1.0), 1, True),      # 第 3 次开得更快
)


def _sprint(hz, seed, issues) -> tuple[list, dict]:
    track = build_sprint_route()
    car = _make_car(issues)
    Lr = int(round(track.length_m))
    truth = _base_truth("sprint", "fh", hz, seed, issues, car, track)
    pieces: list[_Piece] = []
    run_of_piece: dict[int, int] = {}
    runs: list[dict] = []
    tire = (TIRE_START_C,) * 4
    for run, pace, pos, finish in _SPRINT_RUNS:
        rng, nrng = _sub_rng(seed, f"sprint{run}")
        n_nodes = int(track.geom_length)
        pin = _plan_input(track, car, pace, rng, n_nodes=n_nodes, i_finish=Lr)
        prof = _Profile.from_speeds(*_solve(car, pace, pin, i_finish=Lr, v0=0.0)[:2])
        t_fin = float(prof.t[Lr])
        if finish:
            t_end = t_fin + COOLDOWN_S
        else:
            t_quit = float(prof.t[int(SPRINT_ABANDON_AT * Lr)])
            t_end = t_quit + SPRINT_FREEROAM_S
        dr = _make_drive(f"sprint{run}", track, car, issues=issues, hz=hz, nrng=nrng, prof=prof, t_end=t_end,
                         tire_start_c=tire, fuel_len=Lr)
        tire = dr.tire_end_c                        # 下一次接着用这次结束时的胎温
        tau, n = dr.tau, dr.n
        if finish:
            race_time = np.minimum(tau, t_fin)            # 过终点计时就停了
            racing = np.ones(n, dtype=bool)
        else:
            racing = tau < t_quit
            race_time = np.where(racing, tau, 0.0)
        dr.cols.update({
            "lap_number": np.zeros(n, dtype=int), "current_lap": race_time.copy(), "race_time": race_time,
            "last_lap": np.zeros(n), "best_lap": np.zeros(n),
            "distance": np.where(racing, dr.s, 0.0), "race_position": np.where(racing, pos, 0).astype(int),
        })
        pieces.append(_Piece(f"pause_before_run{run}", n_idle=round(MENU_S * hz)))
        pieces.append(_Piece(f"run{run}", dr, np.arange(n)))
        run_of_piece[len(pieces) - 1] = run
        info = {"run": run, "finished": finish, "race_position": pos, "_pi": len(pieces) - 1}
        if finish:
            info.update({"time_s": round(t_fin, 6), "distance_m": float(Lr)})
        else:
            j_q = int(np.nonzero(~racing)[0][0])
            info.update({"time_s": None, "abandoned_at_m": round(float(dr.s[j_q - 1]), 2),
                         "abandoned_race_time_s": round(float(tau[j_q - 1]), 6),
                         "freeroam_after_s": SPRINT_FREEROAM_S, "_jq": j_q})
        runs.append(info)
    pieces.append(_Piece("results", n_idle=round(RESULTS_S * hz)))
    asm = _assemble(pieces, hz, _static_fields(car, "fh", None))
    starts = _piece_starts(asm)
    for info in runs:
        pi = info.pop("_pi")
        info["t_start"] = round(starts[pi] / hz, 6)
        if info["finished"]:
            info["t_finish"] = round(starts[pi] / hz + info["time_s"], 6)
        else:
            info["t_quit"] = round((starts[pi] + info.pop("_jq")) / hz, 6)
    truth["runs"] = runs
    truth["route"] = {"length_m": float(Lr), "start_pos": truth["track"]["start_pos"],
                      "finish_pos": truth["track"]["finish_pos"]}
    truth["notes"] += [
        "冲刺：lap_number 全程为 0；race_time 每次从 0 开始，current_lap = race_time；distance 每次从 0 开始",
        "跑完的那几次：过终点后计时停住（race_time 冻结在完赛时间），车继续减速 2 s，然后 is_race_on=0",
        "第 2 次在 50% 处退出：之后 race_position=0、race_time=0、distance=0，自由漫游 3 s 再 is_race_on=0",
        "三次都从同一个起点出发（pos_x/pos_z 相同）",
    ]
    if issues:
        truth["issues"] = _issue_truth(asm, track, car, hz, run_of_piece=run_of_piece)
    truth["phases"] = asm.phases
    truth["n_records"] = len(asm.records)
    return asm.records, truth


# ================================================================ 场景：自由漫游（Horizon）

FREEROAM_START_M = 2400.0          # 从环形赛道 T6 出口附近起步
FREEROAM_PACE = Pace(grip=0.85, brake=0.75, power=0.75)


def _freeroam(hz, seed, issues) -> tuple[list, dict]:
    track = build_circuit()
    car = _make_car(issues)
    rng, nrng = _sub_rng(seed, "freeroam")
    n_nodes = int(2 * track.length_m)
    pin = _plan_input(track, car, FREEROAM_PACE, rng, n_nodes=n_nodes, i_finish=n_nodes, d0=FREEROAM_START_M)
    prof = _Profile.from_speeds(*_solve(car, FREEROAM_PACE, pin, i_finish=n_nodes, v0=0.0)[:2])
    dr = _make_drive("freeroam", track, car, issues=issues, hz=hz, nrng=nrng, prof=prof,
                     t_end=FREEROAM_S - 1.0 / hz, d0=FREEROAM_START_M, tire_start_c=(60.0,) * 4)
    n = dr.n
    zeros = np.zeros(n)
    dr.cols.update({"lap_number": np.zeros(n, dtype=int), "current_lap": zeros, "race_time": zeros,
                    "last_lap": zeros, "best_lap": zeros, "distance": zeros,
                    "race_position": np.zeros(n, dtype=int)})
    asm = _assemble([_Piece("freeroam", dr, np.arange(n))], hz, _static_fields(car, "fh", None))
    truth = _base_truth("freeroam", "fh", hz, seed, issues, car, track)
    truth["expect_laps"] = False
    truth["freeroam"] = {"duration_s": FREEROAM_S, "start_dist_m": FREEROAM_START_M,
                         "driven_m": round(float(dr.s[-1]), 2)}
    truth["notes"] += ["自由漫游：不应切出任何圈；race_position/lap_number/race_time/current_lap/last_lap/best_lap/distance 全为 0",
                       "复用环形赛道几何，从 T6 出口附近静止起步，开得比比赛温柔"]
    if issues:
        truth["issues"] = _issue_truth(asm, track, car, hz)
    truth["phases"] = asm.phases
    truth["n_records"] = len(asm.records)
    return asm.records, truth


# ================================================================ 对外接口

@dataclass
class FakeResult:
    records: list[tuple[float, Frame]]   # (录像时间 s，从 0 开始、严格 1/hz 间隔, Frame)，包括菜单/暂停帧
    truth: dict                          # 标准答案（可直接 json.dump）

    @property
    def frames(self) -> list[Frame]:
        return [f for _, f in self.records]


def generate(scenario: str = "circuit", *, family: str = "fm", laps: int = 5, hz: float = 60.0,
             seed: int = 1, issues: bool = True, rewind: bool = False, restart: bool = False) -> FakeResult:
    """生成一段假遥测。

    scenario：
      - "circuit"：环形赛道，菜单 1 s → 站立起步跑 laps 圈 → 收车 2 s → 结算 1 s。FM / FH 都行。
        rewind=True：lap_number=1 跑到 55% 时倒带 4 s；restart=True：先跑一次在 lap_number=1 的 60% 处重开。
      - "sprint"：点对点冲刺（Horizon），三次：跑完 / 50% 退出 / 跑完且更快。
      - "freeroam"：自由漫游 60 s（Horizon），不应切出任何圈。
    sprint / freeroam 只有 Horizon 有，family 会强制成 "fh"。
    同一个 seed 结果完全一样。
    """
    if hz <= 0 or hz > 1000:
        raise ValueError(f"hz={hz} 不合理")
    if family not in ("fm", "fh"):
        raise ValueError(f"family 只能是 fm / fh，收到 {family!r}")
    if scenario == "circuit":
        records, truth = _circuit(family, laps, hz, seed, issues, rewind, restart)
    elif scenario in ("sprint", "freeroam"):
        if rewind or restart:
            raise ValueError("rewind / restart 只支持 circuit 场景")
        records, truth = (_sprint if scenario == "sprint" else _freeroam)(hz, seed, issues)
        if family != "fh":
            truth["notes"].append(f"{scenario} 只有 Horizon 有，family 已从 {family!r} 改成 'fh'")
    else:
        raise ValueError(f"未知场景 {scenario!r}（可选 circuit / sprint / freeroam）")
    if not issues:
        truth["issues"] = {}
    return FakeResult(records=records, truth=_jsonable(truth))


def truth_path_for(path) -> Path:
    """录像路径 → 标准答案 sidecar 路径：demo.frec → demo.truth.json（demo.frec.gz 也是 demo.truth.json）。"""
    p = Path(path)
    if p.suffix == ".gz":
        p = p.with_suffix("")
    return p.with_suffix("").with_name(p.with_suffix("").name + ".truth.json")


def write_fake_recording(path, fmt_key: str = "fm2023_dash", scenario: str = "circuit", *,
                         laps: int = 5, hz: float = 60.0, seed: int = 1, issues: bool = True,
                         rewind: bool = False, restart: bool = False, total_size: int | None = None) -> dict:
    """生成假数据并写成 .frec 录像 + 同名 .truth.json，返回 truth。

    fmt_key 决定包格式和游戏系列（fh / fh_legacy → Horizon，其余 → Motorsport）。
    total_size 比格式长时在每个包末尾补 0（模拟未知包长）。
    """
    if fmt_key not in FORMATS:
        raise ValueError(f"未知格式 {fmt_key!r}，可选：{', '.join(FORMATS)}")
    fmt = FORMATS[fmt_key]
    if total_size is not None and total_size < fmt.size:
        raise ValueError(f"total_size={total_size} 比 {fmt_key} 的包长 {fmt.size} 还短")
    family = fmt.family
    if scenario in ("sprint", "freeroam") and family != "fh":
        raise ValueError(f"{scenario} 只有 Horizon 有，请用 Horizon 格式（fmt_key='fh' 或 'fh_legacy'）")
    res = generate(scenario, family=family, laps=laps, hz=hz, seed=seed, issues=issues, rewind=rewind,
                   restart=restart)
    game = DEFAULT_GAME_BY_FORMAT.get(fmt_key, family)
    packets = [(t, encode(fr, fmt_key, total_size)) for t, fr in res.records]
    path = Path(path)
    tpath = truth_path_for(path)
    meta = {"source": "fake", "scenario": scenario, "fmt": fmt_key, "game": game, "seed": seed,
            "hz": hz, "issues": issues, "truth_file": tpath.name}
    write_recording(path, packets, meta)
    truth = dict(res.truth)
    truth.update({"fmt": fmt_key, "game": game, "total_size": total_size,
                  "packet_size": total_size if total_size and total_size > fmt.size else fmt.size,
                  "file": path.name})
    tpath.write_text(json.dumps(truth, ensure_ascii=False, indent=1), encoding="utf-8")
    return truth


def _jsonable(x):
    """把 numpy 类型转成普通 Python 类型，保证 truth 能直接 json.dump。"""
    if isinstance(x, dict):
        return {str(k): _jsonable(v) for k, v in x.items()}
    if isinstance(x, (list, tuple)):
        return [_jsonable(v) for v in x]
    if isinstance(x, np.ndarray):
        return [_jsonable(v) for v in x.tolist()]
    if isinstance(x, np.integer):
        return int(x)
    if isinstance(x, np.floating):
        return float(x)
    if isinstance(x, np.bool_):
        return bool(x)
    return x
