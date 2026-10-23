"""自动切圈：把帧流切成一圈一圈（或一次次冲刺赛），存进 SQLite。

切圈规则全部基于游戏自己给的字段：
- IsRaceOn = 0：菜单 / 暂停，跳过这些帧（不结束当前比赛，恢复后接着记）。
  暂停期间游戏还在发包（FM 会发 IsRaceOn = 0 的包），只要还收得到同一台车的包就不算“空闲”；
  连续暂停超过 PAUSE_MAX_S 才结束这场比赛（多半是退出到菜单了）
- CurrentRaceTime <= 0：没有比赛时钟（FH 自由漫游），结束当前比赛
- LapNumber + 1：上一圈结束。圈时取游戏的 LastLap，不自己算
- CurrentRaceTime 小幅回退：倒带。删掉“被倒掉”的那段采样，接着记；倒过了起终点线就把上一圈拿回来
- CurrentRaceTime 回到接近 0（之前 > 3 s）：重开，没跑完的那圈丢弃
- 换车 / 换游戏格式：结束当前比赛
- FH 冲刺赛：LapNumber 全程为 0 且有名次。比赛结束时整段存成一条 kind="sprint" 的记录，
  是否跑完靠“和这条路线已知最长距离比，>= 95%”判断（第一次跑时只能先当作跑完）
- 比赛最后一圈：如果游戏在冲线时没让 LapNumber + 1，就在比赛结束时按距离 >= 本场整圈的 95% 判为完整圈

一圈结束后先挂起 PENDING_WINDOW_S 秒再落盘，防止刚过线就倒带回去。
"""
from __future__ import annotations

import logging
import statistics
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Callable

import numpy as np

from .frame import Frame
from .lapdata import LapData, frame_sample
from .util import now_iso

log = logging.getLogger(__name__)

PENDING_WINDOW_S = 12.0        # 过线后等这么久（新一圈的圈内时间）才落盘上一圈
MIN_SAMPLE_DT = 1.0 / 60.0     # 采样上限 60Hz（FH 帧率高时能到 140 包/秒，没必要全存）
REWIND_EPS = 0.05              # 比赛时间回退超过这个值才算倒带
RESTART_RT = 1.5               # 比赛时间回到这个值以下……
RESTART_PREV_RT = 3.0          # ……且之前超过这个值，判定为重开
FINAL_LAP_RATIO = 0.95         # 最后一圈距离达到整圈的这个比例才算完整
SPRINT_FINISH_RATIO = 0.95     # 冲刺赛距离达到路线长度的这个比例才算跑完
DIST_MISMATCH = 0.2            # DistanceTraveled 和速度积分差超过 20% 就改用积分距离
MIN_SPRINT_M = 300.0           # 短于这个距离的“冲刺赛”直接丢弃（多半是赛道赛第一圈就重开了）
IDLE_S = 30.0                  # 完全收不到包这么久（游戏关了 / FH6 进菜单停发）就结束当前比赛
PAUSE_MAX_S = 180.0            # 还在收包、但连续暂停（IsRaceOn = 0）这么久，也结束当前比赛
PARTIAL_START_S = 1.0          # 开始记录时这圈已经跑了超过这么久 = 中途接入（缺前段数据）
UNTAGGED_SOURCES = ("import:", "replay:")   # 这些来源（导入 / 回放录像）的圈默认不归调校版本，见 LapSaver


def should_use_integrated_distance(d_field, d_int) -> bool:
    """这一圈的距离通道该不该改用“车速积分距离”。

    游戏给的 DistanceTraveled 一般可靠，但有两种情况不能用：
    1. 不单调（倒带、某些模式下会跳回去）；
    2. 和车速积分出来的距离差太多（超过 DIST_MISMATCH，比如跨场次累计、被重置过）。
    积分距离太短（< 50 m）时比例不可靠，不判。

    切圈器存圈（LapDetector._emit）和实时面板算差距（live.LiveBroadcaster._reference）都用这一个函数，
    保证同一圈在两边用的是同一种距离口径。
    """
    d_field = np.asarray(d_field, dtype=np.float64)
    d_int = np.asarray(d_int, dtype=np.float64)
    total_int = float(d_int[-1]) if len(d_int) else 0.0
    if total_int <= 50:
        return False
    total_field = float(d_field[-1]) if len(d_field) else 0.0
    monotonic = bool(np.all(np.diff(d_field) >= -0.5)) if len(d_field) > 1 else True
    return not monotonic or abs(total_field - total_int) / total_int > DIST_MISMATCH


@dataclass
class _LapBuf:
    lap_number: int
    start_dist: float
    start_rt: float
    start_x: float
    start_z: float
    rows: list[list[float]] = field(default_factory=list)
    idist: list[float] = field(default_factory=list)   # 每行对应的速度积分距离
    int_dist: float = 0.0
    last_row_t: float = -1.0
    last_t: float = 0.0          # 最近一帧的圈内时间（采样被限频跳过的帧也算，量圈时用它）
    last_rt: float = 0.0
    max_speed: float = 0.0
    flags: set[str] = field(default_factory=set)
    use_rt_clock: bool = False   # 游戏不给 CurrentLap 时，改用比赛时间算圈内时间

    def lap_t(self, fr: Frame) -> float:
        if not self.use_rt_clock and fr.current_lap <= 0 and fr.race_time - self.start_rt > 1.0:
            self.use_rt_clock = True
        # 头一秒还判断不出游戏给不给 CurrentLap（点到点赛事常常一直是 0），
        # 这期间先用比赛时间顶上，免得这一秒的采样时间全是 0
        return fr.race_time - self.start_rt if self.use_rt_clock or fr.current_lap <= 0 else fr.current_lap

    def truncate(self, t: float) -> None:
        """倒带：删掉圈内时间 > t 的采样。"""
        k = len(self.rows)
        while k and self.rows[k - 1][0] > t + 1e-6:
            k -= 1
        del self.rows[k:]
        del self.idist[k:]
        self.int_dist = self.idist[-1] if self.idist else 0.0
        self.last_row_t = self.rows[-1][0] if self.rows else -1.0
        self.last_t = max(self.last_row_t, 0.0)
        self.max_speed = max((row[2] for row in self.rows), default=0.0)
        self.flags.add("rewind")


@dataclass
class _Event:
    """一场比赛（或一次冲刺赛）。"""
    event_id: int
    family: str
    game: str
    fmt: str
    car_ordinal: int
    start_x: float
    start_z: float
    line_x: float | None = None        # 第一次过起终点线的位置（赛道指纹用）
    line_z: float | None = None
    saw_position: bool = False         # 有没有出现过名次 > 0
    boundaries: int = 0                # 过线次数
    lap_lengths: list[float] = field(default_factory=list)
    best_time: float | None = None
    best_lap: LapData | None = None
    # 本场已经交出去的圈的 meta（只存元数据，不多占采样内存）：倒带跨回已落盘的圈时，用它把那些圈作废
    emitted_meta: list[dict] = field(default_factory=list)


class LapDetector:
    """有状态的切圈器。每帧调用 on_frame()；切出来的圈通过 on_lap 回调交出去。"""

    def __init__(self, on_lap: Callable[[LapData], None] | None = None):
        self._listeners: list[Callable[[LapData], None]] = [on_lap] if on_lap else []
        self._invalidation_listeners: list[Callable[[dict], None]] = []
        self.event: _Event | None = None
        self.cur: _LapBuf | None = None
        self.pending: _LapBuf | None = None
        self._pending_end: Frame | None = None   # 挂起圈结束时的帧
        self._last_frame: Frame | None = None
        self._last_rt = 0.0
        self._last_race_wall = 0.0                # 最近一次比赛帧的 monotonic 时刻
        self._last_paused_wall = 0.0              # 最近一次“同一台车的暂停帧”（IsRaceOn = 0）的 monotonic 时刻
        self._last_race_epoch = 0.0               # 同一时刻的 time.time()（写进 meta["ended_epoch"]）
        self._pending_end_epoch: float | None = None   # 挂起圈过线时的 time.time()
        self._event_seq = 0
        self.laps_emitted = 0
        self.dropped_partial = 0

    def add_listener(self, fn: Callable[[LapData], None]) -> None:
        self._listeners.append(fn)

    def add_invalidation_listener(self, fn: Callable[[dict], None]) -> None:
        self._invalidation_listeners.append(fn)

    # ------------------------------------------------------------------
    def on_frame(self, fr: Frame) -> None:
        if not fr.has_dash:          # Sled 格式没有圈数信息
            return
        if not fr.is_race_on:        # 菜单 / 暂停：不切圈，但记下“还在收包”，暂停再久也不当成游戏关了
            if self.event and fr.car_ordinal == self.event.car_ordinal:
                self._last_paused_wall = time.monotonic()
            return
        self._last_race_wall = time.monotonic()
        self._last_race_epoch = time.time()
        rt = fr.race_time

        if rt <= 0:                  # 没有比赛时钟：自由漫游 / 退出比赛
            if self.event:
                self._end_event("no_clock")
            self._last_frame = fr
            return

        ev = self.event
        if ev and (fr.car_ordinal != ev.car_ordinal or fr.fmt != ev.fmt):
            self._end_event("car_or_format_changed")
            ev = None
        if ev and rt < RESTART_RT and self._last_rt > RESTART_PREV_RT:
            self._end_event("restart")
            ev = None

        if ev is None:
            self._start_event(fr)
        else:
            if rt + REWIND_EPS < self._last_rt:
                self._rewind(fr)
            if fr.lap_number == self.cur.lap_number + 1:
                self._boundary(fr)
            elif fr.lap_number != self.cur.lap_number:
                # 圈数乱跳（不是 +1，也不是倒带能解释的）：当作新比赛
                log.info("圈数从 %d 跳到 %d，重新开始记录", self.cur.lap_number, fr.lap_number)
                self._end_event("lap_jump")
                self._start_event(fr)

        if fr.race_position > 0:
            self.event.saw_position = True
        self._append(fr)
        self._last_rt = rt
        self._last_frame = fr

        # 挂起的圈：新一圈跑了足够久，没被倒带拿回去，就落盘
        if self.pending and self.cur.lap_t(fr) > PENDING_WINDOW_S:
            self._commit_pending()

    def tick(self, idle_s: float = IDLE_S, pause_max_s: float = PAUSE_MAX_S) -> None:
        """定期调用（例如每秒），处理“没有比赛帧”的几种情况：

        1. 刚过线就暂停 / 进菜单：挂起的圈等不到新一圈跑满 PENDING_WINDOW_S，
           那就按墙钟等 PENDING_WINDOW_S 后直接落盘（暂停中不会倒带）。
        2. 完全收不到包超过 idle_s（游戏关了，或 FH6 进菜单停发）：结束当前比赛。
        3. 还在收暂停帧，但连续暂停超过 pause_max_s：多半是退出比赛了，也结束。
        """
        if not self.event:
            return
        now = time.monotonic()
        since_race = now - self._last_race_wall
        if self.pending and since_race > PENDING_WINDOW_S:
            self._commit_pending()
        since_any = now - max(self._last_race_wall, self._last_paused_wall)
        if since_any > idle_s:
            self._end_event("idle")
        elif since_race > pause_max_s:
            self._end_event("paused_too_long")

    def flush(self) -> None:
        """程序退出 / 导入结束时调用：收尾当前比赛。"""
        if self.event:
            self._end_event("flush")

    # ------------------------------------------------------------------
    def _start_event(self, fr: Frame) -> None:
        self._event_seq += 1
        self.event = _Event(self._event_seq, fr.family, fr.game, fr.fmt, fr.car_ordinal, fr.pos_x, fr.pos_z)
        self.cur = self._new_buf(fr, fr.lap_number)
        self.cur.flags.add("first_lap")
        # 开始记录时这圈已经跑了一段（比赛中途才开本工具、换车 / 圈数乱跳后重新记）：
        # 缺前段数据，过线时只能存成不完整圈，也不能拿它的长度去认赛道
        if fr.current_lap > PARTIAL_START_S or (fr.current_lap <= 0 and fr.race_time > PARTIAL_START_S):
            self.cur.flags.add("partial_start")
        self.pending = None
        self._pending_end = None

    def _new_buf(self, fr: Frame, lap_number: int) -> _LapBuf:
        return _LapBuf(lap_number=lap_number, start_dist=fr.distance, start_rt=fr.race_time,
                       start_x=fr.pos_x, start_z=fr.pos_z, last_rt=fr.race_time)

    def _append(self, fr: Frame) -> None:
        cur = self.cur
        t = cur.lap_t(fr)
        dt = fr.race_time - cur.last_rt
        if 0 < dt < 1.0:
            cur.int_dist += fr.speed * dt
        cur.last_rt = fr.race_time
        cur.last_t = t
        if cur.rows and t - cur.last_row_t < MIN_SAMPLE_DT - 1e-6:
            return
        cur.rows.append(frame_sample(fr, t, fr.distance - cur.start_dist))
        cur.idist.append(cur.int_dist)
        cur.last_row_t = t
        cur.max_speed = max(cur.max_speed, fr.speed)

    def _boundary(self, fr: Frame) -> None:
        """过线：当前圈结束，挂起等待落盘；开新的一圈。"""
        ev = self.event
        if self.pending:
            self._commit_pending()
        ev.boundaries += 1
        if ev.line_x is None:
            ev.line_x, ev.line_z = fr.pos_x, fr.pos_z
        self.pending = self.cur
        self._pending_end = fr
        self._pending_end_epoch = time.time()
        self.cur = self._new_buf(fr, fr.lap_number)

    def _rewind(self, fr: Frame) -> None:
        """倒带：游戏的比赛时间往回退了。"""
        # 倒带可能退得比挂起窗口（PENDING_WINDOW_S）还远，退回到已经落盘的圈里：
        # 那几圈的数据保留（方便回看），但成绩作废（标不完整 + rewound_after_save），并通知存档方改库
        ev = self.event
        for meta in ev.emitted_meta:
            if meta["lap_number"] >= fr.lap_number and "rewound_after_save" not in meta["flags"]:
                meta["is_complete"] = False
                # 换一个新列表而不是原地 append：存档线程可能正在序列化这份 meta
                meta["flags"] = [*meta["flags"], "rewound_after_save"]
                for fn in self._invalidation_listeners:
                    try:
                        fn(meta)
                    except Exception:
                        log.exception("使倒带前的圈失效时出错")
        # 本场最快圈被作废了就不能再当实时差距的参照；整圈长度表也按剩下的有效圈重算
        if ev.best_lap is not None and not ev.best_lap.meta["is_complete"]:
            ev.best_lap, ev.best_time = None, None
        ev.lap_lengths = [m["distance_m"] for m in ev.emitted_meta
                          if m["is_complete"] and m["kind"] == "lap"]
        # 退到挂起圈之前（连挂起的那圈都被倒掉了）：挂起圈直接扔掉
        if self.pending and fr.lap_number < self.pending.lap_number:
            self.pending = None
            self._pending_end = None
            self._pending_end_epoch = None
        if self.pending and fr.lap_number == self.pending.lap_number and fr.lap_number == self.cur.lap_number - 1:
            # 倒过了起终点线：扔掉新开的这圈，把上一圈拿回来接着记
            self.cur = self.pending
            self.pending = None
            self._pending_end = None
            self.event.boundaries -= 1
        if fr.lap_number == self.cur.lap_number:
            self.cur.truncate(self.cur.lap_t(fr))
            self.cur.last_rt = fr.race_time

    def _commit_pending(self) -> None:
        buf, end, end_epoch = self.pending, self._pending_end, self._pending_end_epoch
        self.pending = None
        self._pending_end = None
        self._pending_end_epoch = None
        if buf is None:
            return
        last = self._last_frame
        # 圈时以游戏给的 LastLap 为准（取最新帧里的值，此时 LapNumber 应该正好比挂起圈大 1）
        lap_time = None
        if last is not None and last.lap_number == buf.lap_number + 1 and last.last_lap > 0:
            lap_time = last.last_lap
        elif end is not None and end.last_lap > 0:
            lap_time = end.last_lap
        source = "game"
        if lap_time is None:
            lap_time = buf.last_t
            source = "measured"
        self._emit(buf, kind="lap", lap_time=lap_time, time_source=source,
                   complete="partial_start" not in buf.flags, ended_epoch=end_epoch)

    def _end_event(self, reason: str) -> None:
        ev, cur = self.event, self.cur
        if self.pending:
            self._commit_pending()
        interrupted = reason in {"restart", "car_or_format_changed", "lap_jump"}
        if ev and cur and cur.rows:
            dist = self.buf_distance(cur)
            if (ev.boundaries == 0 and ev.family == "fh" and ev.saw_position and cur.lap_number == 0
                    and dist >= MIN_SPRINT_M):
                # FH 冲刺赛：整段存成一条记录，是否跑完由保存方按路线长度判断
                # 跑完冲刺赛马上点“重来”，和中途重开在时钟上看不出区别：这里不能因为 restart 就判成中断，
                # 否则“跑完 → 重试”这种最常见的练习流程里，前面每次跑完的成绩都会作废。
                # 是否跑完交给保存方按路线长度判断（短于已知最长距离的 95% 会标“放弃”）。
                cur.flags.add("finish_inferred")
                self._emit(cur, kind="sprint", lap_time=cur.last_t, time_source="measured",
                           complete="partial_start" not in cur.flags, reason=reason,
                           ended_epoch=self._last_race_epoch or None)
            elif not interrupted and ev.lap_lengths and dist >= FINAL_LAP_RATIO * statistics.median(ev.lap_lengths):
                # 最后一圈游戏没给 LapNumber + 1，但距离够了，算完整圈（圈时自己量）
                cur.flags.add("finish_inferred")
                self._emit(cur, kind="lap", lap_time=cur.last_t, time_source="measured",
                           complete="partial_start" not in cur.flags, reason=reason,
                           ended_epoch=self._last_race_epoch or None)
            else:
                self.dropped_partial += 1
                log.info("丢弃未跑完的圈：第 %d 圈，%.0f m（%s）", cur.lap_number, dist, reason)
        self.event = None
        self.cur = None
        self._last_rt = 0.0

    @staticmethod
    def buf_distance(buf: _LapBuf) -> float:
        """一圈缓冲目前跑了多远：游戏给的距离和速度积分距离取大的那个（两者有一个不可靠时更稳）。"""
        if not buf.rows:
            return 0.0
        return max(buf.rows[-1][1], buf.idist[-1])

    def _emit(self, buf: _LapBuf, *, kind: str, lap_time: float, time_source: str,
              complete: bool, reason: str = "", ended_epoch: float | None = None) -> None:
        """交出一圈。ended_epoch = 这圈跑完（过线 / 最后一帧比赛帧）的 time.time()，
        调校版本按它找“当时生效的版本”（跑完后进菜单再记新版本，这圈仍算旧版本）。"""
        ev = self.event
        meta_fr = self._last_frame
        lap = LapData.from_rows({}, buf.rows)
        # 距离通道：游戏给的距离不可靠时改用车速积分（规则见 should_use_integrated_distance）
        if should_use_integrated_distance(lap.ch["dist"], buf.idist):
            lap.ch["dist"] = np.asarray(buf.idist, dtype=np.float64)
            buf.flags.add("dist_integrated")
        distance = float(lap.ch["dist"][-1]) if len(lap) else 0.0

        if kind == "lap" and complete:
            ev.lap_lengths.append(distance)
        line_x = ev.line_x if ev.line_x is not None else buf.start_x
        line_z = ev.line_z if ev.line_z is not None else buf.start_z
        if kind == "sprint":
            line_x, line_z = ev.start_x, ev.start_z
        lap.meta = {
            "event_id": ev.event_id,
            "game": ev.game, "family": ev.family, "fmt": ev.fmt,
            "car_ordinal": ev.car_ordinal,
            "car_class": meta_fr.car_class if meta_fr else None,
            "car_pi": meta_fr.car_pi if meta_fr else None,
            "drivetrain": meta_fr.drivetrain if meta_fr else None,
            "num_cylinders": meta_fr.num_cylinders if meta_fr else None,
            "engine_max_rpm": meta_fr.engine_max_rpm if meta_fr else None,
            "engine_idle_rpm": meta_fr.engine_idle_rpm if meta_fr else None,
            "track_ordinal": meta_fr.track_ordinal if meta_fr else None,
            "lap_number": buf.lap_number,
            "kind": kind,
            "lap_time": round(float(lap_time), 3),
            "time_source": time_source,
            "distance_m": round(distance, 1),
            "max_speed": round(buf.max_speed, 2),
            "is_complete": complete,
            "flags": sorted(buf.flags),
            "start_x": line_x, "start_z": line_z,
            "end_reason": reason,
            "ended_epoch": round(ended_epoch if ended_epoch else time.time(), 3),
            "created_at": now_iso(),
        }
        if complete and kind == "lap" and (ev.best_time is None or lap_time < ev.best_time):
            ev.best_time = lap_time
            ev.best_lap = lap
        self.laps_emitted += 1
        ev.emitted_meta.append(lap.meta)
        for fn in self._listeners:
            try:
                fn(lap)
            except Exception:
                log.exception("处理新圈出错")

    # ------------------------------------------------------------------
    def event_kind(self) -> str | None:
        """当前这场比赛是什么类型（给实时面板 / 状态接口看）：

        None = 不在比赛里；"circuit" = 已经过过线的赛道赛；
        "sprint?" = FH 有名次、还没过过线（多半是冲刺赛，跑完才能确定）；"lap" = 其余（第一圈还没跑完）。
        """
        ev = self.event
        if ev is None:
            return None
        if ev.boundaries:
            return "circuit"
        return "sprint?" if ev.family == "fh" and ev.saw_position else "lap"

    def status(self) -> dict:
        """切圈器状态快照（只读，不改任何内部状态）。"""
        ev, cur, fr = self.event, self.cur, self._last_frame
        return {
            "in_event": ev is not None,
            "event_kind": self.event_kind(),
            "lap_number": cur.lap_number if cur else None,
            "lap_time": round(cur.last_t, 3) if cur and cur.rows else None,
            "lap_distance": round(self.buf_distance(cur), 1) if cur else None,
            "last_lap": fr.last_lap if fr else None,
            "best_lap": fr.best_lap if fr else None,
            "event_best": ev.best_time if ev else None,
            "laps_emitted": self.laps_emitted,
            "pending": self.pending.lap_number if self.pending else None,
        }


class LapSaver:
    """把 LapDetector 交出来的圈存进 SQLite（赛道识别、会话、车辆信息、调校版本）。

    background=True 时在后台线程里写库，不卡实时面板。
    tag_setup=True：交圈那一刻就查这台车的当前调校版本，往 meta 里写 setup_id / setup_name
    （在切圈器线程里查，不等后台排队，免得刚记了新版本、排队中的上一圈被归到新版本里）。
    tag_setup=None（默认）：按来源定——导入 / 回放录像（source 以 "import:" / "replay:" 开头）不打标签，
    因为切圈器的 ended_epoch 是导入 / 回放那一刻的时间，不是录像当时的时间，会把老录像的圈算进现在的版本；
    其余（实时收包）打标签。
    存好之后顺手更新这台车的动力总成数据（功率曲线 / 传动比，见 forza/powertrain.py），再通知监听者。
    """

    def __init__(self, store, source: str = "", background: bool = True, tag_setup: bool | None = None):
        self.store = store
        self.source = source
        if tag_setup is None:
            tag_setup = not str(source or "").startswith(UNTAGGED_SOURCES)
        self.tag_setup = tag_setup
        self._sessions: dict[int, int] = {}        # event_id -> session_id
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="lap-saver") if background else None
        self._lock = threading.Lock()
        self.saved_ids: list[int] = []
        self._listeners: list[Callable[[LapData], None]] = []
        self._invalidation_listeners: list[Callable[[dict], None]] = []

    def add_listener(self, fn: Callable[[LapData], None]) -> None:
        """存好之后通知（meta 里已经有 id / track_id / session_id）。"""
        self._listeners.append(fn)

    def add_invalidation_listener(self, fn: Callable[[dict], None]) -> None:
        self._invalidation_listeners.append(fn)

    def invalidate(self, meta: dict) -> None:
        # 和 _save 走同一个单线程队列：就算这圈还在排队没写库，作废操作也一定排在写库之后，
        # 不会和 INSERT 抢跑（抢跑的话 meta 里还没有 id，改不到那一行）
        if self._pool:
            self._pool.submit(self._invalidate, meta)
        else:
            self._invalidate(meta)

    def _invalidate(self, meta: dict) -> None:
        if meta.get("id") is None:
            return
        self.store.update_lap(meta["id"], is_complete=False, flags=meta["flags"])
        # 作废的圈可能正是定赛道参考圈长的飞驰圈之一：和删圈 / 标为不完整一样，重算一遍
        if meta.get("kind") == "lap" and meta.get("track_id") is not None:
            self.store.refresh_circuit_length(meta["track_id"], clear_if_empty=True)
        for fn in self._invalidation_listeners:
            fn(meta)

    def __call__(self, lap: LapData) -> None:
        if self.tag_setup:
            self._tag_setup(lap)
        if self._pool:
            self._pool.submit(self._save, lap)
        else:
            self._save(lap)

    def _tag_setup(self, lap: LapData) -> None:
        """按这圈跑完的时刻找当时生效的调校版本（见 forza/setups.py），写 meta 的 setup_id / setup_name。
        runtime 的存档前钩子（register_pre_save_hook）不再重复打这个标签。"""
        if "setup_id" in lap.meta:          # 调用方已经标好了
            return
        try:
            from . import setups
            setups.tag_lap_meta(self.store, lap.meta)
        except Exception:      # 查不到版本不影响存圈
            log.exception("给新圈标调校版本出错")

    def _save(self, lap: LapData) -> None:
        try:
            m = lap.meta
            st = self.store
            st.upsert_car(m["game"], m["car_ordinal"], m.get("car_class") or 0, m.get("car_pi") or 0,
                          m.get("drivetrain") or 0, m.get("num_cylinders") or 0)
            kind = "sprint" if m["kind"] == "sprint" else "circuit"
            flags = set(m.get("flags") or [])
            first_lap = "first_lap" in flags
            track_id = self._match_track(m, kind, first_lap, partial="partial_start" in flags)
            m["track_id"] = track_id
            track = st.get_track(track_id) if track_id is not None else None
            ref = track.get("ref_length_m") if track else None
            if kind == "sprint" and track_id is not None:
                # 冲刺赛：比已知最长距离短一截 = 中途放弃
                if ref and m["distance_m"] < SPRINT_FINISH_RATIO * ref:
                    m["is_complete"] = False
                    m.setdefault("flags", []).append("abandoned")
                if not ref or m["distance_m"] > ref:
                    st.update_track_length(track_id, m["distance_m"])
                    if ref and ref < SPRINT_FINISH_RATIO * m["distance_m"]:
                        self._mark_short_sprints(track_id, m["distance_m"])
            with self._lock:
                sid = self._sessions.get(m["event_id"])
                if sid is None:
                    sid = st.create_session(m["game"], m["fmt"], self.source, m["car_ordinal"], track_id)
                    self._sessions[m["event_id"]] = sid
                elif track_id is not None:
                    # 本场第一圈是中途接入、没认出赛道（track_id = None）时，会话用后面第一圈认出来的赛道补上
                    st.execute("UPDATE sessions SET track_id=? WHERE id=? AND track_id IS NULL", (track_id, sid))
            m["session_id"] = sid
            lap_id = st.save_lap(lap)
            if kind == "circuit" and track_id is not None and m.get("is_complete") and not first_lap:
                # 赛道参考圈长 = 飞驰圈距离的中位数（站立起步的第一圈不算，它可能多跑了发车格到线的那段）
                st.refresh_circuit_length(track_id)
            self.saved_ids.append(lap_id)
            log.info("已存档：#%d %s 第 %d 圈 %.3f s（%s）", lap_id, m["kind"], m["lap_number"],
                     m["lap_time"], "完整" if m["is_complete"] else "不完整")
        except Exception:
            log.exception("存档失败")
            return
        self._after_save(lap)

    def _match_track(self, m: dict, kind: str, first_lap: bool, partial: bool) -> int | None:
        """找（或新建）这一圈所在的赛道，返回 track id。

        中途接入的圈（partial_start）只有后半段数据，距离是半截的：
        - 赛道赛：起终点线位置是对的（过线时记下的），但不能拿半截长度去比圈长、更不能用它建暂定圈长，
          所以只看位置：附近正好一条赛道就归它；附近有好几条（共用起终点线的不同布局，光看位置分不出来）
          就不归到任何赛道（track_id = None），免得随便挂到一个错的布局上；附近一条都没有就建一条不带长度的赛道，
          等飞驰圈来了再定参考圈长。
        - 冲刺赛：连起点都是中途的某个位置，认不出是哪条路线，干脆不归到任何路线（track_id = None）。
        有赛道 ID（FM2023）的游戏不受影响：按 ID 认，和长度无关。
        """
        st = self.store
        if partial and not m.get("track_ordinal"):
            if kind == "sprint":
                return None
            near = st.nearby_track_ids(family=m["family"], kind=kind, start_x=m["start_x"], start_z=m["start_z"])
            if len(near) > 1:
                return None
            if len(near) == 1:
                return near[0]
        length = None if partial else m["distance_m"]
        return st.find_or_create_track(
            family=m["family"], game=m["game"], kind=kind, track_ordinal=m.get("track_ordinal"),
            start_x=m["start_x"], start_z=m["start_z"], length_m=length, first_lap=first_lap and not partial)

    def _after_save(self, lap: LapData) -> None:
        """存好之后的附加处理和监听：每个单独 try，出错只记日志，不算“存档失败”，也不影响别的监听。"""
        try:
            from . import powertrain
            powertrain.record_lap(self.store, lap)
        except Exception:
            log.exception("更新动力总成数据出错（圈 #%s）", lap.meta.get("id"))
        for fn in list(self._listeners):
            try:
                fn(lap)
            except Exception:
                log.exception("新圈监听出错：%s", getattr(fn, "__qualname__", fn))

    def _mark_short_sprints(self, track_id: int, length: float) -> None:
        """发现这条路线其实更长：之前当成跑完的短记录，改标为放弃。"""
        for lap in self.store.list_laps(track_id=track_id):
            if lap["kind"] == "sprint" and lap["is_complete"] and lap["distance_m"] < SPRINT_FINISH_RATIO * length:
                self.store.update_lap(lap["id"], is_complete=False)

    def wait(self) -> None:
        """等后台写完（测试 / 导入用）。"""
        if self._pool:
            self._pool.shutdown(wait=True)
            self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="lap-saver")
