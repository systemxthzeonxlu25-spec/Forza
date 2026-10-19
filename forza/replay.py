"""录像回放。

三种用法：
- replay_to_hub：在本进程里按原始时序把包喂给 Hub（serve --replay 用，不走网络）
- replay_udp：按原始时序用 UDP 发出去，假装自己是游戏（测试整条网络链路用）
- import_file：不等待、全速喂给 Hub（离线导入录像生成圈速存档用）
"""
from __future__ import annotations

import asyncio
import logging
import socket
import time
from pathlib import Path

from .hub import Hub
from .recorder import iter_records

log = logging.getLogger(__name__)


async def replay_to_hub(path: str | Path, hub: Hub, speed: float = 1.0, loop: bool = False) -> None:
    """speed = 回放倍速；<= 0 表示不等待、尽快喂完。"""
    while True:
        start = time.monotonic()
        t0 = None
        n = 0
        for t, data in iter_records(path):
            if t0 is None:
                t0 = t
            if speed > 0:
                due = start + (t - t0) / speed
                delay = due - time.monotonic()
                if delay > 0:
                    await asyncio.sleep(delay)
            elif n % 500 == 0:
                await asyncio.sleep(0)  # 全速模式也要偶尔让出事件循环
            hub.feed(data)
            n += 1
        log.info("回放结束：%s（%d 个包）", path, n)
        if not loop:
            return


def replay_udp(path: str | Path, host: str = "127.0.0.1", port: int = 5555,
               speed: float = 1.0, loop: bool = False) -> int:
    """按原始时序把录像用 UDP 发出去，返回发送的包数。"""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sent = 0
    try:
        while True:
            start = time.perf_counter()
            t0 = None
            for t, data in iter_records(path):
                if t0 is None:
                    t0 = t
                if speed > 0:
                    delay = start + (t - t0) / speed - time.perf_counter()
                    if delay > 0:
                        time.sleep(delay)
                try:
                    sock.sendto(data, (host, port))
                    sent += 1
                except OSError:
                    pass  # Windows 上对方没在听时可能报错，忽略
            if not loop:
                return sent
    finally:
        sock.close()


def import_file(path: str | Path, hub: Hub) -> int:
    """全速把录像喂给 Hub（同步），返回包数。帧时间用录像里的相对时间。"""
    n = 0
    for t, data in iter_records(path):
        hub.feed(data, t)
        n += 1
    return n
