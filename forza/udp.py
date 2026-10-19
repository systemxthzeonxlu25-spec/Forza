"""UDP 接收：把游戏发来的每个包交给 Hub。"""
from __future__ import annotations

import asyncio
import logging

from .hub import Hub

log = logging.getLogger(__name__)


class _Protocol(asyncio.DatagramProtocol):
    def __init__(self, hub: Hub):
        self.hub = hub

    def datagram_received(self, data: bytes, addr) -> None:
        self.hub.feed(data)

    def error_received(self, exc: Exception) -> None:
        # Windows 上偶尔会收到 ICMP 端口不可达之类的错误，忽略即可
        log.debug("UDP 错误：%s", exc)


async def start_udp(hub: Hub, host: str, port: int) -> asyncio.DatagramTransport:
    loop = asyncio.get_running_loop()
    transport, _ = await loop.create_datagram_endpoint(lambda: _Protocol(hub), local_addr=(host, port))
    log.info("UDP 监听 %s:%d", host, port)
    return transport
