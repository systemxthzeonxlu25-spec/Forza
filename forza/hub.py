"""数据中枢：原始包 → (录制/转发) → 解析 → 分发给各订阅者。

数据来源（UDP 实时 / 录像回放 / 离线导入）都只调用 hub.feed(data, t)，
下游（实时面板、切圈器……）用 hub.subscribe(fn) 拿到每一帧 Frame。
"""
from __future__ import annotations

import logging
import socket
import time
from collections import Counter, deque
from typing import Callable

from .frame import Frame
from .packet import FORMATS, PacketDecoder
from .profiles import get_profile
from .recorder import Recorder

log = logging.getLogger(__name__)

Subscriber = Callable[[Frame], None]


class Hub:
    def __init__(self, game_override: str | None = None, recorder: Recorder | None = None,
                 forward: list[str] | None = None, source: str = ""):
        self.decoder = PacketDecoder(game_override)
        self.recorder = recorder
        self.source = source
        self._subs: list[Subscriber] = []
        self.latest: Frame | None = None
        self.packets = 0
        self.sizes: Counter[int] = Counter()
        self.last_packet_at: float | None = None   # time.monotonic()
        self._recent: deque[float] = deque(maxlen=2000)  # 最近的到达时刻，算每秒包数（倍速回放时可能远超 144 包/秒）
        self._fwd_sock: socket.socket | None = None
        self._fwd_addrs: list[tuple[str, int]] = []
        for item in forward or []:
            host, _, port = item.rpartition(":")
            self._fwd_addrs.append((host or "127.0.0.1", int(port)))
        if self._fwd_addrs:
            self._fwd_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

    def subscribe(self, fn: Subscriber) -> None:
        self._subs.append(fn)

    def feed(self, data: bytes, t: float | None = None) -> Frame | None:
        """喂一个原始包。t = 包的时间（秒）；实时接收时不传，用 monotonic。"""
        now = time.monotonic()
        if t is None:
            t = now
        self.packets += 1
        self.sizes[len(data)] += 1
        self.last_packet_at = now
        self._recent.append(now)
        if self.recorder is not None:
            self.recorder.write(data)
        for addr in self._fwd_addrs:
            try:
                self._fwd_sock.sendto(data, addr)  # type: ignore[union-attr]
            except OSError:
                pass
        frame = self.decoder.decode(data, t)
        if frame is None:
            return None
        self.latest = frame
        for fn in self._subs:
            try:
                fn(frame)
            except Exception:  # 某个订阅者出错不能影响其他人
                log.exception("订阅者处理帧出错：%s", getattr(fn, "__qualname__", fn))
        return frame

    def pps(self) -> float:
        """最近 1 秒的每秒包数。"""
        now = time.monotonic()
        return float(sum(1 for x in self._recent if now - x <= 1.0))

    def status(self) -> dict:
        f = self.latest
        now = time.monotonic()
        prof = get_profile(f.game) if f else None
        return {
            "source": self.source,
            "packets": self.packets,
            "pps": self.pps(),
            "since_last_packet": None if self.last_packet_at is None else round(now - self.last_packet_at, 2),
            "sizes": {str(k): v for k, v in self.sizes.items()},
            "format": f.fmt if f else None,
            "format_label": FORMATS[f.fmt].label if f and f.fmt in FORMATS else None,
            "game": f.game if f else None,
            "game_name": prof.name if prof else None,
            "game_note": prof.note if prof else None,
            "has_dash": f.has_dash if f else None,
            "recording": str(self.recorder.path) if self.recorder else None,
            "warnings": list(self.decoder.warnings.values()),
        }
