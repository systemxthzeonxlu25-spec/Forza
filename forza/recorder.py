"""原始 UDP 包录制 / 读取。

录的是原始字节，不是解析后的数据：以后解析逻辑改了，老录像照样能重新解析。

文件结构（.frec；文件名以 .gz 结尾时自动 gzip 压缩）：
    MAGIC (8 字节 b"FZAREC1\\n")
    u32  元数据 JSON 长度
    元数据 JSON（utf-8），例如 {"created": "...", "source": "udp:0.0.0.0:5555", "game": "fh6"}
    若干条记录，每条：
        f64  相对录制开始的时间（秒）
        u16  包长
        包的原始字节
程序崩溃时最后一条记录可能是半截，读取时会自动忽略。
"""
from __future__ import annotations

import gzip
import io
import json
import struct
import time
from pathlib import Path
from typing import Iterator

from .util import now_iso

MAGIC = b"FZAREC1\n"
_META_LEN = struct.Struct("<I")
_REC_HDR = struct.Struct("<dH")


def _open(path: Path, mode: str):
    if path.suffix == ".gz":
        return gzip.open(path, mode)
    return open(path, mode)


class Recorder:
    """把原始包追加写入文件。用法：

        with Recorder("x.frec", meta={"source": "udp"}) as rec:
            rec.write(data)          # 时间默认取当前时刻
            rec.write(data, t=1.23)  # 或者显式给相对时间
    """

    FLUSH_EVERY = 2.0  # 秒；定期 flush，程序被强杀也最多丢这么多数据

    def __init__(self, path: str | Path, meta: dict | None = None):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._f = _open(self.path, "wb")
        m = {"created": now_iso(), "version": 1}
        m.update(meta or {})
        mb = json.dumps(m, ensure_ascii=False).encode("utf-8")
        self._f.write(MAGIC + _META_LEN.pack(len(mb)) + mb)
        self._t0 = time.monotonic()
        self._last_flush = self._t0
        self.count = 0

    def write(self, data: bytes, t: float | None = None) -> None:
        now = time.monotonic()
        if t is None:
            t = now - self._t0
        self._f.write(_REC_HDR.pack(t, len(data)) + data)
        self.count += 1
        if now - self._last_flush > self.FLUSH_EVERY:
            self._f.flush()
            self._last_flush = now

    def close(self) -> None:
        if not self._f.closed:
            self._f.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


def read_meta(path: str | Path) -> dict:
    with _open(Path(path), "rb") as f:
        return _read_header(f)


def _read_header(f: io.BufferedIOBase) -> dict:
    magic = f.read(len(MAGIC))
    if magic != MAGIC:
        raise ValueError("不是 .frec 录像文件（文件头不对）")
    (n,) = _META_LEN.unpack(f.read(_META_LEN.size))
    return json.loads(f.read(n).decode("utf-8"))


def iter_records(path: str | Path) -> Iterator[tuple[float, bytes]]:
    """逐条读出 (相对时间秒, 原始包)。"""
    with _open(Path(path), "rb") as f:
        _read_header(f)
        while True:
            hdr = f.read(_REC_HDR.size)
            if len(hdr) < _REC_HDR.size:
                return
            t, n = _REC_HDR.unpack(hdr)
            data = f.read(n)
            if len(data) < n:
                return  # 半截记录，丢掉
            yield t, data


def write_recording(path: str | Path, records, meta: dict | None = None) -> int:
    """一次性写入 [(t, bytes), ...]，返回条数。"""
    with Recorder(path, meta) as rec:
        for t, data in records:
            rec.write(data, t)
        return rec.count
