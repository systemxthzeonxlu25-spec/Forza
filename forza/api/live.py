"""实时面板的 WebSocket 路由。（骨架版本）"""
from __future__ import annotations

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

router = APIRouter()


@router.websocket("/ws/live")
async def ws_live(ws: WebSocket):
    await ws.accept()
    live = ws.app.state.rt.live
    live.clients.add(ws)
    try:
        while True:
            await ws.receive_text()  # 客户端可以发消息（暂时不用），这里只是保持连接
    except WebSocketDisconnect:
        pass
    finally:
        live.clients.discard(ws)
