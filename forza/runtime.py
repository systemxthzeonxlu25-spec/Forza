"""把各个部件串起来：数据源 → Hub → 切圈/存档 + 实时推送 → 网页服务。"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from pathlib import Path

from . import frame as frame_mod
from .config import Config
from .hub import Hub
from .laps import LapDetector, LapSaver
from .live import LiveBroadcaster
from .netinfo import lan_ips
from .recorder import Recorder
from .replay import replay_to_hub
from .storage import Store
from .udp import start_udp

log = logging.getLogger(__name__)


class Runtime:
    def __init__(self, cfg: Config, *, replay: str | None = None, speed: float = 1.0,
                 loop: bool = False, record: str | None = None):
        self.cfg = cfg
        frame_mod.TIRE_TEMP_UNIT = cfg.tire_temp_unit
        self.replay_path = replay
        self.replay_speed = speed
        self.replay_loop = loop
        self.store = Store(cfg.resolve(cfg.db_path))

        recorder = None
        if record is not None:
            path = Path(record) if record else cfg.resolve(cfg.rec_dir) / (
                datetime.now().strftime("%Y%m%d_%H%M%S") + ".frec")
            recorder = Recorder(path, meta={"source": f"udp:{cfg.udp_host}:{cfg.udp_port}", "game": cfg.game})
        source = f"replay:{Path(replay).name}" if replay else f"udp:{cfg.udp_port}"
        self.hub = Hub(cfg.game, recorder, cfg.forward, source)

        # 切圈 → 存档
        self.detector = LapDetector()
        self.saver = LapSaver(self.store, source)
        self.detector.add_listener(self.saver)
        self.hub.subscribe(self.detector.on_frame)

        # 实时推送（手机面板）
        self.live = LiveBroadcaster(self)
        self.hub.subscribe(self.live.on_frame)

    def status(self) -> dict:
        return {
            **self.hub.status(),
            "laps": self.detector.status(),
            "udp_port": self.cfg.udp_port,
            "http_port": self.cfg.http_port,
            "lan_ips": lan_ips(),
            "replay": self.replay_path,
        }

    async def _tick(self) -> None:
        while True:
            await asyncio.sleep(1.0)
            self.detector.tick()

    async def run(self) -> None:
        import uvicorn

        from .app import create_app

        app = create_app(self)
        server = uvicorn.Server(uvicorn.Config(app, host=self.cfg.http_host, port=self.cfg.http_port,
                                               log_level="warning"))
        tasks: list[asyncio.Task] = [asyncio.create_task(self._tick())]
        transport = None
        if self.replay_path:
            tasks.append(asyncio.create_task(
                replay_to_hub(self.replay_path, self.hub, self.replay_speed, self.replay_loop)))
        else:
            transport = await start_udp(self.hub, self.cfg.udp_host, self.cfg.udp_port)
        tasks.append(asyncio.create_task(self.live.run()))
        self._print_banner()
        try:
            await server.serve()
        finally:
            for t in tasks:
                t.cancel()
            if transport:
                transport.close()
            self.shutdown()

    def shutdown(self) -> None:
        self.detector.flush()
        self.saver.wait()
        if self.hub.recorder:
            self.hub.recorder.close()
            log.info("录像已保存：%s（%d 个包）", self.hub.recorder.path, self.hub.recorder.count)

    def _print_banner(self) -> None:
        c = self.cfg
        ips = lan_ips()
        lines = ["", "=" * 56, " Forza 遥测已启动"]
        if self.replay_path:
            lines.append(f"  数据来源：回放 {self.replay_path}（{self.replay_speed}x）")
        else:
            lines.append(f"  UDP 监听：{c.udp_host}:{c.udp_port}")
            lines.append(f"  游戏 Data Out 填：IP = 127.0.0.1 或 {ips[0] if ips else '本机局域网 IP'}，端口 = {c.udp_port}")
        lines.append(f"  电脑浏览器：http://localhost:{c.http_port}")
        for ip in ips:
            lines.append(f"  手机浏览器：http://{ip}:{c.http_port}")
        if self.hub.recorder:
            lines.append(f"  正在录制：{self.hub.recorder.path}")
        lines += ["  按 Ctrl+C 退出", "=" * 56, ""]
        print("\n".join(lines), flush=True)
