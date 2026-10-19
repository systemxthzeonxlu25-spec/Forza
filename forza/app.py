"""FastAPI 应用：挂各模块的 API 路由 + 静态网页。

各模块的路由分文件放在 forza/api/ 下，互不干扰：
    api/live.py    实时面板（WebSocket /ws/live）
    api/laps.py    圈速列表、圈数据、两圈对比
    api/tuning.py  调车诊断
路由函数里通过 request.app.state.rt 拿到 Runtime（hub / store / detector ...）。
"""
from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles

from .api import laps as laps_api
from .api import live as live_api
from .api import tuning as tuning_api
from .config import WEB_DIR


def create_app(rt) -> FastAPI:
    app = FastAPI(title="Forza 遥测", docs_url="/api/docs", redoc_url=None)
    app.state.rt = rt

    @app.get("/api/status")
    def status(request: Request):
        return request.app.state.rt.status()

    app.include_router(live_api.router)
    app.include_router(laps_api.router)
    app.include_router(tuning_api.router)
    # 静态网页放最后，html=True 让 / 自动返回 index.html
    app.mount("/", StaticFiles(directory=WEB_DIR, html=True), name="web")
    return app
