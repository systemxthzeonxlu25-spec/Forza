"""把各个部件串起来：数据源 → Hub → 切圈/存档 + 实时推送 → 网页服务。

新圈的流向：
    LapDetector 切出一圈 → 存档前钩子（register_pre_save_hook 注册的，可往 meta 里加字段）
    → LapSaver 后台线程写库 → 存好后通知 LiveBroadcaster.on_lap_saved（算本圈小结、刷新 PB 缓存）

网页上开关录制 / 保存最近 N 分钟（api/record.py）也在这里：start_recording / stop_recording / snapshot。
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from pathlib import Path
from typing import Callable

from . import frame as frame_mod
from .config import Config
from .hub import Hub, write_packed
from .lapdata import LapData
from .laps import LapDetector, LapSaver
from .live import LiveBroadcaster
from .netinfo import lan_ips
from .recorder import Recorder
from .replay import replay_to_hub
from .storage import Store
from .udp import start_udp
from .util import now_iso

log = logging.getLogger(__name__)

# 存档前钩子：fn(rt, lap)，在切圈器交出新圈、写库之前调用（事件循环线程里，要快），可以改 lap.meta。
# 例如调校版本模块往 meta 里写 setup_id / setup_name。在模块导入时调 register_pre_save_hook(fn) 注册即可。
PRE_SAVE_HOOKS: list[Callable[["Runtime", LapData], None]] = []


def register_pre_save_hook(fn: Callable[["Runtime", LapData], None]) -> None:
    if fn not in PRE_SAVE_HOOKS:
        PRE_SAVE_HOOKS.append(fn)


class RecordError(Exception):
    """录制相关的操作没法做（参数不对 / 状态不对），message 是给用户看的中文提示。"""


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
        # 回放模式下“保存刚才 N 分钟”是禁用的（数据本来就来自录像），不开环形缓冲，省得白占内存
        ring_s = 0.0 if replay else max(0.0, float(cfg.record_buffer_minutes or 0)) * 60.0
        self.hub = Hub(cfg.game, recorder, cfg.forward, source, ring_seconds=ring_s)
        self.last_snapshot: dict | None = None     # 最近一次“保存刚才 N 分钟”的结果

        # 切圈 → （存档前钩子）→ 存档
        self.detector = LapDetector()
        self.saver = LapSaver(self.store, source)
        self.detector.add_listener(self._on_lap)
        self.detector.add_invalidation_listener(self.saver.invalidate)
        self.hub.subscribe(self.detector.on_frame)

        # 实时推送（手机面板）；新圈存好后算本圈小结
        self.live = LiveBroadcaster(self)
        self.hub.subscribe(self.live.on_frame)
        self.saver.add_listener(self.live.on_lap_saved)
        self.saver.add_invalidation_listener(self.live.on_lap_invalidated)

    def _on_lap(self, lap: LapData) -> None:
        for fn in list(PRE_SAVE_HOOKS):
            try:
                fn(self, lap)
            except Exception:   # 钩子出错不能影响存档
                log.exception("存档前钩子出错：%s", getattr(fn, "__qualname__", fn))
        self.saver(lap)

    # ---------------- 录制（网页上开关；都在事件循环里调用，和收包不会并发） ----------------
    def _rec_dir(self) -> Path:
        return self.cfg.resolve(self.cfg.rec_dir)

    def _record_path(self, name: str | None, default_suffix: str = "") -> Path:
        """录像文件放在 rec_dir 里：name 只能是文件名（可以带子目录，但不能是绝对路径、不能有 ..）。"""
        rec_dir = self._rec_dir()
        if name is None or not str(name).strip():
            stem = datetime.now().strftime("%Y%m%d_%H%M%S") + default_suffix
            path = rec_dir / f"{stem}.frec"
            k = 2
            while path.exists():               # 同一秒内点了两次
                path = rec_dir / f"{stem}_{k}.frec"
                k += 1
            return path
        p = Path(str(name).strip())
        if p.is_absolute() or ".." in p.parts or p.drive:
            raise RecordError(f"录像只能存到录像目录（{rec_dir}）里，请只填文件名")
        if not (p.name.endswith(".frec") or p.name.endswith(".frec.gz")):
            p = p.with_name(p.name + ".frec")
        path = rec_dir / p
        if path.exists():
            raise RecordError(f"文件已经存在：{path}，换个名字")
        return path

    def start_recording(self, name: str | None = None) -> dict:
        if self.hub.recorder is not None:
            raise RecordError(f"已经在录制了：{self.hub.recorder.path}")
        path = self._record_path(name)
        self.hub.start_recording(path, meta={"game": self.cfg.game, "started_from": "web"})
        self.live.invalidate()
        return self.record_status()

    def stop_recording(self) -> dict:
        rec = self.hub.stop_recording()
        self.live.invalidate()
        st = self.record_status()
        st["stopped"] = None if rec is None else {"path": str(rec.path), "packets": rec.count}
        return st

    async def snapshot(self, minutes: float | None = None) -> dict:
        """把环形缓冲里最近 minutes 分钟的原始包存成 .frec，返回 {path, packets, seconds}。"""
        if self.replay_path:
            raise RecordError("回放模式下不用保存：现在的数据本来就来自录像文件 " + str(self.replay_path))
        ring = self.hub.ring
        if ring is None:
            raise RecordError("没有开环形缓冲（config.toml 里 record_buffer_minutes = 0）")
        limit = ring.seconds / 60.0
        if minutes is None:
            minutes = limit
        try:
            minutes = float(minutes)
        except (TypeError, ValueError):
            raise RecordError("minutes 要是数字（分钟）") from None
        if not minutes > 0:
            raise RecordError("minutes 要大于 0")
        minutes = min(minutes, limit)
        packed, n, secs = ring.snapshot(minutes * 60.0)   # 只是内存拷贝，在事件循环里做，和收包不会打架
        if n == 0:
            raise RecordError("缓冲区里还没有收到过包")
        label = f"_last{minutes:g}min".replace(".", "_")
        path = self._record_path(None, default_suffix=label)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch()      # 先占住文件名：后台写完之前再点一次也不会撞名
        meta = {"source": self.hub.source, "game": self.cfg.game, "snapshot": True,
                "minutes": round(minutes, 2)}
        count = await asyncio.to_thread(write_packed, path, packed, meta)   # 写文件放后台线程
        self.last_snapshot = {"path": str(path), "packets": count, "seconds": secs,
                              "created_at": now_iso()}
        log.info("已保存最近 %.1f 分钟原始包：%s（%d 个包，%.0f s）", minutes, path, count, secs)
        return {"path": str(path), "packets": count, "seconds": secs}

    def record_status(self) -> dict:
        rec = self.hub.recorder
        ring = self.hub.ring.stats() if self.hub.ring is not None else {"packets": 0, "seconds": 0.0, "bytes": 0}
        return {
            "recording": rec is not None,
            "path": str(rec.path) if rec is not None else None,
            "packets": rec.count if rec is not None else 0,
            "buffer_seconds": ring["seconds"],
            "buffer_packets": ring["packets"],
            "buffer_bytes": ring["bytes"],
            "buffer_minutes": round(self.hub.ring.seconds / 60.0, 2) if self.hub.ring is not None else 0,
            "replay": bool(self.replay_path),
            "snapshot_disabled": ("回放模式下数据本来就来自录像文件" if self.replay_path else
                                  "没有开环形缓冲（record_buffer_minutes = 0）" if self.hub.ring is None else None),
            "rec_dir": str(self._rec_dir()),
            "last_snapshot": self.last_snapshot,
            "error": self.hub.record_error,          # 录制中途写失败时的原因（否则 None）
        }

    def status(self) -> dict:
        return {
            **self.hub.status(),
            "laps": self.detector.status(),
            "udp_port": self.cfg.udp_port,
            "http_port": self.cfg.http_port,
            # 用实时推送模块里后台刷新的缓存：这个方法在事件循环里跑（/api/status 是 async 路由），
            # 直接调 lan_ips() 的话缓存过期那一下会同步做 DNS 查询，卡住收包
            "lan_ips": self.live.lan_ips_cached(),
            "replay": self.replay_path,
            # 已经写进数据库的圈数 / 最新一圈 id：网页轮询发现变化就自动刷新圈列表
            "saved_count": len(self.saver.saved_ids),
            "last_saved_lap_id": self.saver.saved_ids[-1] if self.saver.saved_ids else None,
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
        self.live.close()
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
