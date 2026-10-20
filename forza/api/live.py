"""实时面板的路由：WebSocket 推送 + 调试用快照。

    WS  /ws/live?hz=20        实时推送（消息格式见 forza/live.py 的模块说明）
    GET /api/live/snapshot    和 WebSocket 一条消息完全相同的 JSON（调试 / 测试用）
    GET /api/live/summaries   最近几圈的本圈小结（新的在前），实时面板菜单里翻看用
"""
from __future__ import annotations

from fastapi import APIRouter, Request, Response, WebSocket

router = APIRouter()


@router.websocket("/ws/live")
async def ws_live(ws: WebSocket):
    await ws.app.state.rt.live.serve_client(ws)


@router.get("/api/live/snapshot")
def live_snapshot(request: Request):
    text = request.app.state.rt.live.message_text()
    return Response(text, media_type="application/json", headers={"Cache-Control": "no-store"})


@router.get("/api/live/summaries")
def live_summaries(request: Request):
    return {"items": request.app.state.rt.live.recent_summaries()}
