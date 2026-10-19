"""假数据用的赛道几何：闭合赛道 Fake Ring（circuit / freeroam）和点对点路线 Fake Sprint（sprint）。

做法：赛道 = 直道 + 弯道 拼起来。
每个弯道 = 进弯缓和曲线（曲率从 0 线性涨到 1/R）+ 定半径圆弧 + 出弯缓和曲线，
所以曲率处处连续（分段线性），横向加速度不会突变。

闭合赛道要求终点回到起点：
- 航向：所有弯的转角加起来正好 +360°（顺时针跑一圈），航向自然连续；
- 位置：标了 adjust=True 的三段直道长度会被微调，让终点和起点重合，并且一圈总长正好是整数米
  （直道长度对终点位置是线性的，解一个 3x3 线性方程就行）。

坐标约定（和游戏一致）：
- x = 东 / 车身右侧，z = 北 / 车头前方，y = 海拔（向上）。
- 航向 heading：0 = 朝 +z，右转时增大；前进方向 = (sin h, cos h)（分别对应 x、z）。
- 曲率 curvature：+ = 右弯，- = 左弯，单位 1/m。

想改赛道：改 CIRCUIT_SEGMENTS / SPRINT_SEGMENTS 就行（闭合赛道转角之和必须是 +360°）。
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from functools import lru_cache

import numpy as np

_SUB = 20  # 积分位置时每米细分的步数（0.05 m 一步，误差远小于 1 mm）


# ---------------------------------------------------------------- 赛道“积木”

@dataclass(frozen=True)
class Straight:
    """直道。

    adjust=True：闭合赛道求解时允许微调这段的长度（必须正好三段，且方向互不平行）。
    bump_at：在这段直道起点之后多少米放一个“坑”（用来做悬挂触底）。
    """
    length: float
    adjust: bool = False
    bump_at: float | None = None


@dataclass(frozen=True)
class Turn:
    """弯道：缓和曲线 + 圆弧 + 缓和曲线。

    angle_deg：总转角，+ = 右转，- = 左转。
    transition：单侧缓和曲线长度（m），必须 < 半径 × 转角。
    kind：corner / hairpin / sweeper / chicane（只是标签，写进 truth）。
    """
    name: str
    angle_deg: float
    radius: float
    transition: float
    kind: str = "corner"

    @property
    def length(self) -> float:
        # 两段缓和曲线各转 transition/(2R)，圆弧转剩下的角度
        return self.radius * math.radians(abs(self.angle_deg)) + self.transition


@dataclass
class Corner:
    """生成好的弯道信息（距离都是从起跑线/起点沿中心线量的米数）。"""
    name: str
    index: int            # 在弯道列表里的序号，从 0 开始（T1 = 0）
    direction: str        # "left" / "right"
    kind: str
    angle_deg: float      # 转角绝对值
    min_radius_m: float
    start_m: float        # 进弯缓和曲线起点
    arc_start_m: float    # 圆弧起点
    apex_m: float         # 圆弧中点（当作弯心）
    arc_end_m: float      # 圆弧终点
    end_m: float          # 出弯缓和曲线终点

    @property
    def sign(self) -> int:
        """右弯 +1，左弯 -1（和曲率、横向加速度、方向盘的符号一致）。"""
        return 1 if self.direction == "right" else -1

    def to_dict(self) -> dict:
        return {
            "name": self.name, "index": self.index, "direction": self.direction, "kind": self.kind,
            "angle_deg": round(self.angle_deg, 3), "min_radius_m": round(self.min_radius_m, 3),
            "start_m": round(self.start_m, 3), "arc_start_m": round(self.arc_start_m, 3),
            "apex_m": round(self.apex_m, 3), "arc_end_m": round(self.arc_end_m, 3),
            "end_m": round(self.end_m, 3),
        }


# ---------------------------------------------------------------- 赛道对象

@dataclass
class Track:
    """一条赛道 / 路线。逐米数组 s/x/z/y/heading/curvature 等长，s = 0, 1, 2, ... 米。

    闭合赛道：数组长度 = 一圈米数 + 1，最后一个点和第一个点重合。
    点对点路线：数组覆盖 起点 → 终点 → 终点后的缓冲区（runoff，收车减速用），length_m 只算到终点。
    """
    name: str
    closed: bool
    length_m: float
    s: np.ndarray
    x: np.ndarray
    z: np.ndarray
    y: np.ndarray
    heading: np.ndarray        # 连续展开的航向（不折回 ±π），用的时候自己 wrap
    curvature: np.ndarray
    corners: list[Corner]
    bump_dist_m: float         # 坑的中心距离
    bump_width_m: float
    bump_depth_m: float
    roles: dict                # 埋问题用的弯道角色，如 {"hairpin": "T5", ...}
    straights: list[dict]      # 每段直道 {"start_m","end_m","length_m","heading_deg"}
    main_straight: dict | None
    start_heading: float
    kb_s: np.ndarray           # 曲率断点（曲率在断点间线性变化，用来精确求曲率）
    kb_k: np.ndarray
    hills: tuple               # 起伏：((振幅 m, 周期 m, 相位 rad), ...)
    base_alt: float            # 基准海拔 m

    # ---------- 基本查询 ----------
    @property
    def geom_length(self) -> float:
        """几何数组覆盖的长度（路线含终点后的缓冲区）。"""
        return float(self.s[-1])

    @property
    def start_pos(self) -> tuple[float, float]:
        return float(self.x[0]), float(self.z[0])

    def wrap(self, d):
        """把距离折回到赛道范围内：闭合赛道取模，路线夹在 [0, 几何长度]。"""
        d = np.asarray(d, dtype=float)
        if self.closed:
            return np.mod(d, self.length_m)
        return np.clip(d, 0.0, self.geom_length)

    def curvature_at(self, d):
        """精确曲率（1/m，+ = 右弯）。"""
        return np.interp(self.wrap(d), self.kb_s, self.kb_k)

    def elevation(self, d, deriv: int = 0):
        """海拔及其对距离的导数（deriv = 0/1/2），解析公式，坑也在里面。"""
        d = self.wrap(d)
        out = np.zeros_like(d) + (self.base_alt if deriv == 0 else 0.0)
        for amp, period, phase in self.hills:
            w = 2 * math.pi / period
            arg = w * d + phase
            if deriv == 0:
                out += amp * np.sin(arg)
            elif deriv == 1:
                out += amp * w * np.cos(arg)
            else:
                out -= amp * w * w * np.sin(arg)
        # 坑：宽 w、深 depth 的余弦形下凹。底部是向上的“压缩”冲击，两头是“抛起”
        u = d - (self.bump_dist_m - self.bump_width_m / 2)
        inside = (u >= 0) & (u <= self.bump_width_m)
        k = 2 * math.pi / self.bump_width_m
        h = self.bump_depth_m / 2
        if deriv == 0:
            out -= np.where(inside, h * (1 - np.cos(k * u)), 0.0)
        elif deriv == 1:
            out -= np.where(inside, h * k * np.sin(k * u), 0.0)
        else:
            out -= np.where(inside, h * k * k * np.cos(k * u), 0.0)
        return out

    def sample(self, d) -> dict:
        """在任意距离上取中心线状态（数组进数组出）。"""
        dw = self.wrap(d)
        return {
            "x": np.interp(dw, self.s, self.x),
            "z": np.interp(dw, self.s, self.z),
            "heading": np.interp(dw, self.s, self.heading),
            "curvature": np.interp(dw, self.kb_s, self.kb_k),
            "y": self.elevation(dw),
            "dy": self.elevation(dw, 1),
            "ddy": self.elevation(dw, 2),
        }

    def corner(self, name: str) -> Corner:
        for c in self.corners:
            if c.name == name:
                return c
        raise KeyError(name)

    def role(self, role: str) -> Corner:
        """按角色取弯道，如 role("hairpin")。"""
        return self.corner(self.roles[role])

    def corner_index_at(self, d) -> np.ndarray:
        """每个距离落在哪个弯道里（Corner.index），不在弯里为 -1。"""
        dw = self.wrap(d)
        out = np.full(dw.shape, -1, dtype=int)
        for c in self.corners:
            out[(dw >= c.start_m) & (dw < c.end_m)] = c.index
        return out

    def to_truth(self) -> dict:
        d = {
            "name": self.name,
            "length_m": round(self.length_m, 3),
            "closed": self.closed,
            "bump_dist_m": round(self.bump_dist_m, 3),
            "bump": {"dist_m": round(self.bump_dist_m, 3), "width_m": self.bump_width_m,
                     "depth_m": self.bump_depth_m, "shape": "余弦形下凹，底部压缩"},
            "corners": [c.to_dict() for c in self.corners],
            "roles": self.roles,
            "straights": self.straights,
            "start_pos": [round(self.start_pos[0], 3), round(self.start_pos[1], 3)],
            "start_heading_rad": round(self.start_heading, 6),
            "base_altitude_m": self.base_alt,
        }
        if self.main_straight:
            d["main_straight"] = self.main_straight
        if not self.closed:
            i = int(round(self.length_m))
            d["finish_pos"] = [round(float(self.x[i]), 3), round(float(self.z[i]), 3)]
        return d


# ---------------------------------------------------------------- 构建

def _breakpoints(segments):
    """把积木列表变成曲率断点 + 弯道/直道信息。"""
    pts_s, pts_k = [0.0], [0.0]
    s = 0.0
    corners: list[Corner] = []
    straights: list[tuple[int, float, float]] = []   # (积木序号, 起点, 长度)
    bump = None
    for i, seg in enumerate(segments):
        if isinstance(seg, Straight):
            if seg.length <= 0:
                raise ValueError(f"第 {i} 段直道长度 {seg.length} <= 0")
            straights.append((i, s, seg.length))
            if seg.bump_at is not None:
                bump = s + seg.bump_at
            s += seg.length
            pts_s.append(s)
            pts_k.append(0.0)
            continue
        sign = 1 if seg.angle_deg > 0 else -1
        k = sign / seg.radius
        arc = seg.radius * math.radians(abs(seg.angle_deg)) - seg.transition
        if arc <= 0:
            raise ValueError(f"{seg.name}：缓和曲线太长，圆弧长度 {arc:.1f} <= 0")
        s1, s2, s3 = s + seg.transition, s + seg.transition + arc, s + 2 * seg.transition + arc
        pts_s += [s1, s2, s3]
        pts_k += [k, k, 0.0]
        corners.append(Corner(
            name=seg.name, index=len(corners), direction="right" if sign > 0 else "left", kind=seg.kind,
            angle_deg=abs(seg.angle_deg), min_radius_m=seg.radius,
            start_m=s, arc_start_m=s1, apex_m=(s1 + s2) / 2, arc_end_m=s2, end_m=s3,
        ))
        s = s3
    return np.array(pts_s), np.array(pts_k), corners, straights, bump, s


def _integrate(kb_s, kb_k, total, x0, z0, h0):
    """沿曲率积分出航向和位置（梯形积分，步长 1/_SUB 米）。"""
    n = int(round(total * _SUB))
    ss = np.linspace(0.0, total, n + 1)
    kk = np.interp(ss, kb_s, kb_k)
    ds = np.diff(ss)
    hh = h0 + np.concatenate([[0.0], np.cumsum((kk[1:] + kk[:-1]) / 2 * ds)])
    sx, cz = np.sin(hh), np.cos(hh)
    xx = x0 + np.concatenate([[0.0], np.cumsum((sx[1:] + sx[:-1]) / 2 * ds)])
    zz = z0 + np.concatenate([[0.0], np.cumsum((cz[1:] + cz[:-1]) / 2 * ds)])
    return ss, kk, hh, xx, zz


def _close_loop(segments, x0, z0, h0):
    """调整三段 adjust 直道的长度，让赛道首尾相接、总长为整数米。返回新的积木列表。"""
    adj = [i for i, seg in enumerate(segments) if isinstance(seg, Straight) and seg.adjust]
    if len(adj) != 3:
        raise ValueError("闭合赛道需要正好 3 段 adjust=True 的直道")
    segs = list(segments)
    for _ in range(3):  # 本来一次就够（线性），多迭代两次消掉积分舍入
        kb_s, kb_k, _, straights, _, total = _breakpoints(segs)
        ss, _, hh, xx, zz = _integrate(kb_s, kb_k, total, x0, z0, h0)
        gx, gz = xx[-1] - x0, zz[-1] - z0
        start_of = {i: st for i, st, _ in straights}
        th = [float(np.interp(start_of[i], ss, hh)) for i in adj]
        rows = np.array([[math.sin(t) for t in th], [math.cos(t) for t in th]])
        # 先求一个最小改动的解，看看总长大概是多少，再取整
        d0 = np.linalg.lstsq(rows, -np.array([gx, gz]), rcond=None)[0]
        target = round(total + d0.sum())
        mat = np.vstack([rows, np.ones(3)])
        delta = np.linalg.solve(mat, np.array([-gx, -gz, target - total]))
        for i, dl in zip(adj, delta):
            new_len = segs[i].length + float(dl)
            if new_len < 20:
                raise ValueError(f"闭合求解失败：第 {i} 段直道会变成 {new_len:.1f} m，请调整赛道积木")
            segs[i] = Straight(new_len, adjust=True, bump_at=segs[i].bump_at)
    return segs


def _build(name, segments, *, x0, z0, heading_deg, closed, hills, base_alt,
           bump_width, bump_depth, roles, runoff=0.0, main_straight=None) -> Track:
    h0 = math.radians(heading_deg)
    segs = list(segments)
    if closed:
        segs = _close_loop(segs, x0, z0, h0)
        route_len = None
    else:
        # 路线：最后一段直道补齐到整数米 = 终点；再加一段缓冲区
        _, _, _, _, _, total = _breakpoints(segs)
        last = segs[-1]
        assert isinstance(last, Straight), "路线最后一段必须是直道（终点在上面）"
        segs[-1] = Straight(last.length + (math.ceil(total) - total), bump_at=last.bump_at)
        route_len = float(math.ceil(total))
        segs.append(Straight(float(runoff)))
    kb_s, kb_k, corners, straights, bump, total = _breakpoints(segs)
    total = float(round(total))
    ss, kk, hh, xx, zz = _integrate(kb_s, kb_k, total, x0, z0, h0)
    sl = slice(None, None, _SUB)
    s_m = np.round(ss[sl], 6)
    length = total if closed else route_len
    if closed:
        # 起伏用一圈的整数分之一当周期，保证首尾海拔、坡度都连续
        hills = tuple((a, length / n, ph) for a, n, ph in hills)
    else:
        hills = tuple((a, p, ph) for a, p, ph in hills)
    st_info = []
    for i, st, ln in straights:
        if not closed and st >= route_len:
            continue
        st_info.append({"start_m": round(st, 3), "end_m": round(st + ln, 3), "length_m": round(ln, 3),
                        "heading_deg": round(math.degrees(float(np.interp(st, ss, hh))) % 360, 3)})
    trk = Track(
        name=name, closed=closed, length_m=float(length), s=s_m, x=xx[sl], z=zz[sl], y=np.zeros_like(s_m),
        heading=hh[sl], curvature=kk[sl], corners=corners,
        bump_dist_m=float(bump), bump_width_m=bump_width, bump_depth_m=bump_depth,
        roles=roles, straights=st_info, main_straight=None, start_heading=h0,
        kb_s=kb_s, kb_k=kb_k, hills=hills, base_alt=base_alt,
    )
    trk.y = trk.elevation(s_m)
    if main_straight:
        trk.main_straight = main_straight(trk)
    return trk


# ---------------------------------------------------------------- 两条具体的赛道

# 闭合赛道 Fake Ring：顺时针，约 3.4 km。起跑线在主直道上（s = 0），朝北（+z）。
# 大致形状：西边是主直道（向北）→ T1 右转向东 → 北边（T2 高速左弯）→ T3 拐到东边向南 → 后直道（有坑）
# → T4 右转进入向西伸进内场的“手指” → T5 左发卡掉头 → T6 右转回到东边向南 → T7 右手长高速弯转向西
# → 南边 T8/T9 左右连续弯（chicane）→ T10 右转回到主直道。
CIRCUIT_SEGMENTS = [
    Straight(650),                              # 起跑线 → T1（主直道后半段，末端重刹）
    Turn("T1", +90, 40, 25),                    # 重刹进弯（埋：前轮抱死）
    Straight(300, adjust=True),                 # 北侧直道
    Turn("T2", -30, 180, 30),                   # 高速左弯，轻点刹车
    Straight(150),                              # T3 的刹车区
    Turn("T3", +120, 55, 30),                   # 中速弯（埋：第 2 圈这里慢了）
    Straight(380, bump_at=190),                 # 后直道，中间有个坑（埋：前悬触底）
    Turn("T4", +90, 45, 20),
    Straight(160),
    Turn("T5", -180, 30, 20, "hairpin"),        # 发卡弯（埋：出弯转向过度 + 后轮空转）
    Straight(160),
    Turn("T6", +90, 45, 20),
    Straight(320),
    Turn("T7", +95, 200, 40, "sweeper"),        # 长高速弯（埋：弯中转向不足）
    Straight(120, adjust=True),                 # 南侧直道
    Turn("T8", -45, 35, 12, "chicane"),         # 左右连续弯：先左
    Straight(15),
    Turn("T9", +45, 35, 12, "chicane"),         # 再右
    Straight(120),
    Turn("T10", +85, 60, 25),                   # 最后一个弯，出弯接主直道
    Straight(280, adjust=True),                 # T10 → 起跑线（主直道前半段）
]

CIRCUIT_ROLES = {
    "heavy_brake": "T1",    # 主直道末端的重刹弯
    "slow_corner": "T3",    # 第 2 圈慢了的弯
    "hairpin": "T5",
    "sweeper": "T7",
    "chicane": ["T8", "T9"],
}

# 点对点路线 Fake Sprint：约 3.4 km，不闭合，有自己的几何（起点、朝向都和环形赛道不同）。
# 大致形状（起步朝东北偏东）：S 弯 → 右发卡掉头往回跑（在去程外侧，不交叉）→ 左手长高速弯转向南
# → 长直道（有坑）→ 重刹左转 → 左右连续弯 → 终点。
SPRINT_SEGMENTS = [
    Straight(250),
    Turn("T1", +55, 70, 25),
    Straight(200),
    Turn("T2", -55, 60, 25),
    Straight(260),
    Turn("T3", +180, 28, 18, "hairpin"),        # 掉头，回程在去程外侧
    Straight(520),
    Turn("T4", -100, 220, 40, "sweeper"),
    Straight(800, bump_at=380),                 # 长直道，中间有坑
    Turn("T5", -80, 40, 25),                    # 长直道末端重刹
    Straight(200),
    Turn("T6", -40, 35, 12, "chicane"),         # 左右连续弯：先左
    Straight(15),
    Turn("T7", +40, 35, 12, "chicane"),         # 再右
    Straight(300),                              # 终点在这段末尾
]

SPRINT_ROLES = {
    "heavy_brake": "T5",
    "hairpin": "T3",
    "sweeper": "T4",
    "chicane": ["T6", "T7"],
}


def _circuit_main_straight(trk: Track) -> dict:
    last = trk.corners[-1]
    first = trk.corners[0]
    return {"start_m": round(last.end_m, 3), "end_m": round(first.start_m, 3),
            "length_m": round(trk.length_m - last.end_m + first.start_m, 3),
            "note": "主直道跨过起跑线：从最后一个弯出口到 T1 入口"}


@lru_cache(maxsize=None)
def build_circuit() -> Track:
    """闭合赛道 Fake Ring（结果会缓存，别改返回对象里的数组）。"""
    return _build(
        "Fake Ring", CIRCUIT_SEGMENTS, x0=-420.0, z0=260.0, heading_deg=0.0, closed=True,
        hills=((4.0, 1, 0.3), (1.5, 2, 1.1)),     # (振幅, 一圈内的周期数, 相位)
        base_alt=35.0, bump_width=24.0, bump_depth=0.29, roles=CIRCUIT_ROLES,
        main_straight=_circuit_main_straight,
    )


@lru_cache(maxsize=None)
def build_sprint_route() -> Track:
    """点对点路线 Fake Sprint（结果会缓存）。"""
    return _build(
        "Fake Sprint", SPRINT_SEGMENTS, x0=1850.0, z0=-640.0, heading_deg=75.0, closed=False,
        hills=((6.0, 2600.0, 0.0), (2.0, 900.0, 0.7)),
        base_alt=120.0, bump_width=24.0, bump_depth=0.20, roles=SPRINT_ROLES, runoff=400.0,
    )
