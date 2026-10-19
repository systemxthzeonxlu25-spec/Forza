"""实时推送：把最新一帧按固定频率（默认 20Hz）推给所有连着的浏览器（WebSocket）。

（骨架版本，实时面板模块会在这里补全推送内容）
"""
from __future__ import annotations

import asyncio
import json
import logging

from fastapi import WebSocket

from .frame import Frame

log = logging.getLogger(__name__)


class LiveBroadcaster:
    def __init__(self, rt):
        self.rt = rt
        self.latest: Frame | None = None
        self._dirty = False
        self.clients: set[WebSocket] = set()

    def on_frame(self, fr: Frame) -> None:
        self.latest = fr
        self._dirty = True

    def snapshot(self) -> dict:
        fr = self.latest
        if fr is None:
            return {"type": "frame", "frame": None}
        return {"type": "frame", "frame": {"speed_kmh": fr.speed_kmh, "rpm": fr.rpm, "gear": fr.gear_label}}

    async def run(self) -> None:
        period = 1.0 / max(self.rt.cfg.live_hz, 1.0)
        while True:
            await asyncio.sleep(period)
            if not self.clients or not self._dirty:
                continue
            self._dirty = False
            msg = json.dumps(self.snapshot(), ensure_ascii=False)
            for ws in list(self.clients):
                try:
                    await ws.send_text(msg)
                except Exception:
                    self.clients.discard(ws)
