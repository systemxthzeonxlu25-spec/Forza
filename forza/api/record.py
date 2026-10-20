"""网页上开关录制 / 保存最近几分钟原始包的路由（前缀 /api）。

    POST /api/record/start     {"path": "可选，文件名"}     开始录原始包（存到 rec_dir 里）
    POST /api/record/stop                                   停止录制，关闭文件
    POST /api/record/snapshot  {"minutes": 10}              把内存里最近 N 分钟的原始包存成 .frec → {path, packets, seconds}
    GET  /api/record/status                                 {recording, path, packets, buffer_seconds, buffer_packets, ...}

收包（Hub.feed）跑在 asyncio 主循环里，所以这几个路由都用 async def：和收包在同一个事件循环里执行，
不会出现“刚关掉文件、收包协程还在往里写”的竞态（普通 def 会被放进线程池，和 recorder.write 并发）。
快照写文件放在后台线程里（只写缓冲区的一份拷贝，不碰正在用的对象）。
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from ..runtime import RecordError

router = APIRouter(prefix="/api")


class StartBody(BaseModel):
    path: str | None = None


class SnapshotBody(BaseModel):
    minutes: float | None = None


def _rt(request: Request):
    return request.app.state.rt


@router.get("/record/status")
async def record_status(request: Request):
    return _rt(request).record_status()


@router.post("/record/start")
async def record_start(request: Request, body: StartBody | None = None):
    try:
        return _rt(request).start_recording(body.path if body else None)
    except RecordError as e:
        raise HTTPException(400, str(e)) from None
    except OSError as e:
        raise HTTPException(500, f"没法创建录像文件：{e}") from None


@router.post("/record/stop")
async def record_stop(request: Request):
    return _rt(request).stop_recording()


@router.post("/record/snapshot")
async def record_snapshot(request: Request, body: SnapshotBody | None = None):
    try:
        return await _rt(request).snapshot(body.minutes if body else None)
    except RecordError as e:
        raise HTTPException(400, str(e)) from None
    except OSError as e:
        raise HTTPException(500, f"写录像文件失败：{e}") from None
