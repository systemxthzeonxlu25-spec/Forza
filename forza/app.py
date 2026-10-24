"""FastAPI 应用：挂各模块的 API 路由 + 静态网页。

各模块的路由分文件放在 forza/api/ 下，互不干扰：
    api/live.py    实时面板（WebSocket /ws/live）
    api/laps.py    圈速列表、圈数据、两圈对比
    api/tuning.py  调车诊断
    api/record.py  网页上开关录制 / 保存最近几分钟的原始包
    api/sectors.py 赛道固定弯道 / 分段表、理论最佳圈、逐段稳定性
    api/setups.py  调校版本记录与版本对比
    api/powertrain.py 实测功率曲线、各挡传动比、最佳换挡转速
路由函数里通过 request.app.state.rt 拿到 Runtime（hub / store / detector ...）。
"""
from __future__ import annotations

from urllib.parse import urlsplit

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from .api import laps as laps_api
from .api import live as live_api
from .api import powertrain as powertrain_api
from .api import record as record_api
from .api import sectors as sectors_api
from .api import setups as setups_api
from .api import tuning as tuning_api
from .config import WEB_DIR


def create_app(rt) -> FastAPI:
    app = FastAPI(title="Forza 遥测", docs_url="/api/docs", redoc_url=None)
    app.state.rt = rt

    @app.middleware("http")
    async def _same_origin_only(request: Request, call_next):
        """拦住别的网站发来的写操作（跨站请求伪造）。

        本工具在局域网里没有登录，任何网页都能让浏览器往 http://localhost:8000 发 POST：
        比如停掉录制、反复“保存刚才 10 分钟”把磁盘塞满。浏览器发跨站的 POST / PATCH / DELETE 时
        一定会带 Origin 头，所以只要 Origin 的主机和端口跟请求的 Host 对不上，就拒绝。
        自己的网页（同源）和 curl / 脚本（不带 Origin）都不受影响。
        """
        if request.method in ("POST", "PUT", "PATCH", "DELETE"):
            origin = request.headers.get("origin")
            if origin and urlsplit(origin).netloc != request.headers.get("host", ""):
                return JSONResponse({"detail": "不接受来自其他网站的修改请求"}, status_code=403)
        return await call_next(request)

    @app.exception_handler(OverflowError)
    async def _too_big(request: Request, exc: OverflowError):
        # id 之类的参数大到 SQLite 的 64 位整数装不下，当成参数错误而不是服务器错误
        return JSONResponse({"detail": "数值太大"}, status_code=400)

    # 注意要用 async def：状态里要读 hub / 切圈器的内部状态，它们只在事件循环线程里被修改。
    # 普通 def 路由会被 FastAPI 丢进线程池，和收包并发读写，偶尔会撞上“deque mutated during iteration”。
    @app.get("/api/status")
    async def status(request: Request):
        return request.app.state.rt.status()

    app.include_router(live_api.router)
    app.include_router(laps_api.router)
    app.include_router(tuning_api.router)
    app.include_router(record_api.router)
    app.include_router(sectors_api.router)
    app.include_router(setups_api.router)
    app.include_router(powertrain_api.router)
    # 静态网页放最后，html=True 让 / 自动返回 index.html
    app.mount("/", StaticFiles(directory=WEB_DIR, html=True), name="web")
    return app
