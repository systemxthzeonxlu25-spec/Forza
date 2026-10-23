"""数据中枢：原始包 → (录制/转发/环形缓冲) → 解析 → 分发给各订阅者。

数据来源（UDP 实时 / 录像回放 / 离线导入）都只调用 hub.feed(data, t)，
下游（实时面板、切圈器……）用 hub.subscribe(fn) 拿到每一帧 Frame。

环形缓冲（PacketRing）：常驻内存，保留最近 N 分钟的原始包，网页上点“保存刚才 N 分钟”时直接写成 .frec。
包按 .frec 的记录格式（f64 时间 + u16 包长 + 原始字节）预先打包进一块块 bytearray，
每个包只多 10 字节开销（存成 Python 的 (float, bytes) 元组要多 100 多字节），FM 60 包/秒 × 10 分钟约 12 MB。
开始 / 停止录制（start_recording / stop_recording）和 feed 都要在同一个事件循环里调用，避免关文件时还在写。
"""
from __future__ import annotations

import logging
import socket
import struct
import time
from collections import Counter, deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterator

from .frame import Frame
from .packet import FORMATS, PacketDecoder
from .profiles import get_profile
from .recorder import Recorder, write_recording

log = logging.getLogger(__name__)

Subscriber = Callable[[Frame], None]

RING_CHUNK_BYTES = 256 * 1024        # 环形缓冲每块多大（按块丢弃最老的数据）
RING_MAX_BYTES = 96 * 1024 * 1024    # 环形缓冲内存上限（倍速回放之类包特别多时兜底）
_RING_HDR = struct.Struct("<dH")     # 和 .frec 的记录头一样：f64 时间 + u16 包长


@dataclass
class _Chunk:
    t0: float                        # 块里第一个包的时间
    t1: float                        # 块里最后一个包的时间
    buf: bytearray = field(default_factory=bytearray)
    n: int = 0


class PacketRing:
    """最近 seconds 秒原始包的环形缓冲（预打包）。

    append() 是 O(1)：往当前块的 bytearray 末尾追加；块写满才检查要不要丢掉最老的块，
    所以实际保留的时长会比 seconds 多出不到一块（几秒）。snapshot() 按时间精确截取。
    """

    def __init__(self, seconds: float, max_bytes: int = RING_MAX_BYTES, chunk_bytes: int = RING_CHUNK_BYTES):
        self.seconds = float(seconds)
        self.max_bytes = int(max_bytes)
        self.chunk_bytes = int(chunk_bytes)
        self._chunks: deque[_Chunk] = deque()
        self._bytes = 0                  # 已经写满、在 _chunks 里的块的总字节数（不含当前块）
        self._n = 0                      # 同上，包数

    def append(self, t: float, data: bytes) -> None:
        cur = self._chunks[-1] if self._chunks else None
        if cur is None or len(cur.buf) >= self.chunk_bytes:
            if cur is not None:
                self._bytes += len(cur.buf)
                self._n += cur.n
            cur = _Chunk(t, t)
            self._chunks.append(cur)
            self._trim(t)
        cur.buf += _RING_HDR.pack(t, len(data))
        cur.buf += data
        cur.n += 1
        cur.t1 = t

    def _trim(self, now: float) -> None:
        """丢掉整块都比 seconds 更老的块，以及超过内存上限的部分（只丢已写满的块）。"""
        ch = self._chunks
        while len(ch) > 1 and (now - ch[0].t1 > self.seconds or self._bytes > self.max_bytes):
            old = ch.popleft()
            self._bytes -= len(old.buf)
            self._n -= old.n

    def clear(self) -> None:
        self._chunks.clear()
        self._bytes = 0
        self._n = 0

    def stats(self) -> dict:
        """缓冲里有多少（包数按块统计，可能比 seconds 窗口多出最老那块里的一部分）。"""
        if not self._chunks:
            return {"packets": 0, "seconds": 0.0, "bytes": 0}
        cur = self._chunks[-1]
        t_first, t_last = self._chunks[0].t0, cur.t1
        span = min(max(t_last - t_first, 0.0), self.seconds)
        return {"packets": self._n + cur.n, "seconds": round(span, 1), "bytes": self._bytes + len(cur.buf)}

    def snapshot(self, seconds: float | None = None) -> tuple[bytes, int, float]:
        """最近 seconds 秒（默认整个窗口）的包：返回 (打包好的字节, 包数, 时长秒)。

        只做内存拷贝，很快，可以在事件循环里调用；写文件交给 write_packed（放后台线程）。
        """
        if not self._chunks:
            return b"", 0, 0.0
        win = self.seconds if seconds is None else min(float(seconds), self.seconds)
        t_last = self._chunks[-1].t1
        cutoff = t_last - win
        parts: list = []
        n = 0
        t_first = None
        for c in self._chunks:
            if c.t1 < cutoff:
                continue
            if c.t0 >= cutoff:
                parts.append(c.buf)          # 整块都要：直接交给 join，只拷贝一次
                n += c.n
                if t_first is None:
                    t_first = c.t0
                continue
            # 跨过截止时间的那一块：逐条找到第一个不早于 cutoff 的包
            # （memoryview 用完马上释放：bytearray 被 memoryview 引用着的时候不能再追加）
            with memoryview(c.buf) as mv:
                off = 0
                k = 0
                while off < len(mv):
                    t, ln = _RING_HDR.unpack_from(mv, off)
                    if t >= cutoff:
                        break
                    off += _RING_HDR.size + ln
                    k += 1
                if off < len(mv):
                    parts.append(bytes(mv[off:]))
                    n += c.n - k
                    if t_first is None:
                        t_first = _RING_HDR.unpack_from(mv, off)[0]
        packed = b"".join(parts)
        if not n:
            return b"", 0, 0.0
        return packed, n, round(t_last - (t_first if t_first is not None else t_last), 2)


def iter_packed(packed: bytes) -> Iterator[tuple[float, bytes]]:
    """逐条读出预打包缓冲里的 (时间, 原始包)。"""
    mv = memoryview(packed)
    off = 0
    hs = _RING_HDR.size
    while off + hs <= len(mv):
        t, ln = _RING_HDR.unpack_from(mv, off)
        off += hs
        if off + ln > len(mv):
            return
        yield t, bytes(mv[off:off + ln])
        off += ln


def write_packed(path: str | Path, packed: bytes, meta: dict | None = None) -> int:
    """把预打包缓冲写成 .frec（时间换成相对第一个包的秒数），返回包数。会做文件 IO，放后台线程里调用。"""
    t0 = None

    def records():
        nonlocal t0
        for t, data in iter_packed(packed):
            if t0 is None:
                t0 = t
            yield t - t0, data

    return write_recording(path, records(), meta)


class Hub:
    def __init__(self, game_override: str | None = None, recorder: Recorder | None = None,
                 forward: list[str] | None = None, source: str = "", ring_seconds: float = 0.0):
        self.decoder = PacketDecoder(game_override)
        self.recorder = recorder
        self.source = source
        # 最近几分钟原始包的环形缓冲；ring_seconds <= 0 = 不开（离线导入 / 只录制时用不着）
        self.ring: PacketRing | None = PacketRing(ring_seconds) if ring_seconds and ring_seconds > 0 else None
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
        if self.ring is not None:
            self.ring.append(t, data)
        if self.recorder is not None:
            try:
                self.recorder.write(data)
            except OSError:
                # 磁盘写满 / 文件被占用：停掉录制，别让它拖垮后面的解码和切圈
                log.exception("写录像失败，已停止录制：%s", self.recorder.path)
                rec, self.recorder = self.recorder, None
                try:
                    rec.close()
                except OSError:
                    pass
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

    # ---------------- 网页上开关录制（和 feed 在同一个事件循环里调用） ----------------
    def start_recording(self, path: str | Path, meta: dict | None = None) -> Recorder:
        """开始把原始包录到 path。已经在录时抛 RuntimeError。"""
        if self.recorder is not None:
            raise RuntimeError(f"已经在录制：{self.recorder.path}")
        self.recorder = Recorder(path, meta={"source": self.source, **(meta or {})})
        log.info("开始录制：%s", self.recorder.path)
        return self.recorder

    def stop_recording(self) -> Recorder | None:
        """停止录制并关闭文件，返回刚才的 Recorder（没在录返回 None）。"""
        rec, self.recorder = self.recorder, None
        if rec is not None:
            rec.close()
            log.info("录像已保存：%s（%d 个包）", rec.path, rec.count)
        return rec

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
