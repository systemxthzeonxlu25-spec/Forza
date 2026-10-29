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

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from .api import laps as laps_api
from .api import live as live_api
from .api import powertrain as powertrain_api
from .api import record as record_api
from .api import sectors as sectors_api
from .api import setups as setups_api
from .access import AccessGuard, can_write, is_local_client
from .api import tuning as tuning_api
from .config import WEB_DIR


def create_app(rt) -> FastAPI:
    app = FastAPI(title="Forza 遥测", docs_url="/api/docs", redoc_url=None)
    app.state.rt = rt
    # 防 DNS 重绑定 + 跨站请求伪造 + 局域网设备改数据要钥匙（见 forza/access.py）
    extra_hosts = frozenset(h.strip().lower().rstrip(".") for h in (rt.cfg.allowed_hosts or []))
    app.add_middleware(AccessGuard, get_settings=lambda: (rt.access_key, rt.cfg.lan_access, extra_hosts))

    @app.exception_handler(OverflowError)
    async def _too_big(request: Request, exc: OverflowError):
        # id 之类的参数大到 SQLite 的 64 位整数装不下，当成参数错误而不是服务器错误
        return JSONResponse({"detail": "数值太大"}, status_code=400)

    # 注意要用 async def：状态里要读 hub / 切圈器的内部状态，它们只在事件循环线程里被修改。
    # 普通 def 路由会被 FastAPI 丢进线程池，和收包并发读写，偶尔会撞上“deque mutated during iteration”。
    @app.get("/api/status")
    async def status(request: Request):
        st = request.app.state.rt.status()
        st["can_write"] = can_write(request)
        st["lan_access"] = rt.cfg.lan_access
        # 钥匙只告诉本机打开的网页（“连接手机”页拿它生成二维码），局域网设备拿不到
        if is_local_client(request.client.host if request.client else None):
            st["access_key"] = rt.access_key
        return st

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
