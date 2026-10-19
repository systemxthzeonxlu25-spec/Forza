"""测试用的小工具：快速造一串“绕圆圈跑”的帧，不依赖假数据生成器。"""
from __future__ import annotations

import math

from forza.frame import Frame


class CircleStream:
    """在半径 R 的圆上匀速跑，按游戏字段规则生成帧（圈数从 0 开始、过线 +1、LastLap 等）。"""

    def __init__(self, *, radius=200.0, speed=40.0, hz=60.0, fmt="fm2023_dash", family="fm",
                 game="fm2023", position=1, center=(0.0, 0.0), car=100, track_ordinal=7):
        self.R, self.v, self.dt = radius, speed, 1.0 / hz
        self.fmt, self.family, self.game = fmt, family, game
        self.position, self.center, self.car, self.track_ordinal = position, center, car, track_ordinal
        self.lap_len = 2 * math.pi * radius
        self.reset()

    def reset(self):
        self.rt = 0.0          # 比赛时间
        self.s = 0.0           # 累计距离
        self.lap = 0
        self.lap_start_rt = 0.0
        self.last_lap = 0.0
        self.best = 0.0
        self.wall = 0.0

    def _frame(self, race_on=True) -> Frame:
        a = (self.s % self.lap_len) / self.R
        cx, cz = self.center
        return Frame(
            fmt=self.fmt, family=self.family, game=self.game, size=331, has_dash=True,
            recv_time=self.wall, is_race_on=race_on,
            vel_z=self.v, speed=self.v, accel_x=self.v ** 2 / self.R, angvel_y=self.v / self.R,
            pos_x=cx + self.R * math.sin(a), pos_z=cz - self.R * math.cos(a),
            distance=self.s, race_time=self.rt, current_lap=self.rt - self.lap_start_rt,
            last_lap=self.last_lap, best_lap=self.best, lap_number=self.lap, race_position=self.position,
            throttle=200, gear=4, rpm=6000, engine_max_rpm=8000, car_ordinal=self.car,
            track_ordinal=self.track_ordinal, tire_temp=(180.0, 180.0, 180.0, 180.0),
        )

    def step(self) -> Frame:
        """前进一帧，过线时按游戏规则更新圈数和圈时。"""
        self.rt += self.dt
        self.wall += self.dt
        prev_lap_idx = int(self.s // self.lap_len)
        self.s += self.v * self.dt
        if int(self.s // self.lap_len) > prev_lap_idx:
            self.last_lap = self.rt - self.lap_start_rt
            self.best = self.last_lap if not self.best else min(self.best, self.last_lap)
            self.lap += 1
            self.lap_start_rt = self.rt
        return self._frame()

    def run(self, seconds: float) -> list[Frame]:
        return [self.step() for _ in range(int(round(seconds / self.dt)))]

    def run_dist(self, meters: float) -> list[Frame]:
        return self.run(meters / self.v)

    def paused(self, seconds: float) -> list[Frame]:
        out = []
        for _ in range(int(seconds / self.dt)):
            self.wall += self.dt
            out.append(self._frame(race_on=False))
        return out

    def rewind(self, seconds: float) -> None:
        """倒带：游戏状态整体回退 seconds 秒（墙钟照走）。"""
        back = self.v * seconds
        new_s = self.s - back
        # 倒过了起终点线
        while self.lap > 0 and new_s < self.lap * self.lap_len:
            self.lap -= 1
            self.lap_start_rt = self.lap_start_rt - self.lap_len / self.v  # 恒速：每圈用时一样
        self.s = new_s
        self.rt -= seconds
