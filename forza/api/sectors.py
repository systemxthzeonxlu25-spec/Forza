"""赛道固定弯道 / 分段表、理论最佳圈、逐段稳定性的路由（前缀 /api）。

    GET   /api/sectors?track_id=&car_ordinal=&game=&setup_id=   分段统计（没有弯道表时自动用最快的干净圈生成）
    GET   /api/sectors/{track_id}/layout                       只看弯道表本身
    POST  /api/sectors/{track_id}/rebuild   {"lap_id": 可选}    重算弯道表（改过的弯名按坐标就近沿用）
    PATCH /api/sectors/{track_id}/corners/{code}  {"name": "..."}  改弯名（空字符串 = 恢复编号）

计算全在 forza/sectors.py。统计要解压每一圈的数据，用普通 def 路由（FastAPI 放到线程池里跑，不卡事件循环）。
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from .. import sectors
from ..util import jsonable

router = APIRouter(prefix="/api")


class RebuildBody(BaseModel):
    lap_id: int | None = None


class CornerName(BaseModel):
    name: str | None = None


def _store(request: Request):
    return request.app.state.rt.store


def _track_or_404(store, track_id: int) -> dict:
    t = store.get_track(track_id)
    if t is None:
        raise HTTPException(404, f"赛道 #{track_id} 不存在")
    return t


@router.get("/sectors")
def get_sectors(request: Request, track_id: int, car_ordinal: int | None = None, game: str | None = None,
                setup_id: str | None = None):
    store = _store(request)
    track = _track_or_404(store, track_id)
    try:
        res = sectors.track_sectors(store, track_id, car_ordinal=car_ordinal, game=game or None,
                                    setup_id=setup_id or None)
    except LookupError as e:
        raise HTTPException(404, str(e)) from None
    res["track_name"] = track.get("name")
    res["track_kind"] = track.get("kind")
    return jsonable(res)


@router.get("/sectors/{track_id}/layout")
def get_layout(request: Request, track_id: int):
    store = _store(request)
    _track_or_404(store, track_id)
    layout = sectors.ensure_layout(store, track_id)
    if layout is None:
        raise HTTPException(404, "这条赛道还没有干净的完整圈，没法生成弯道表")
    return jsonable(layout)


@router.post("/sectors/{track_id}/rebuild")
def rebuild(request: Request, track_id: int, body: RebuildBody | None = None):
    store = _store(request)
    _track_or_404(store, track_id)
    try:
        layout, kept = sectors.rebuild_layout(store, track_id, body.lap_id if body else None)
    except ValueError as e:
        raise HTTPException(400, str(e)) from None
    return jsonable({"layout": layout, "kept_names": kept})


@router.patch("/sectors/{track_id}/corners/{code}")
def rename_corner(request: Request, track_id: int, code: str, body: CornerName):
    store = _store(request)
    _track_or_404(store, track_id)
    if body.name is not None and len(body.name.strip()) > sectors.NAME_MAX_LEN:
        raise HTTPException(400, f"名字太长了（最多 {sectors.NAME_MAX_LEN} 个字）")
    try:
        layout = sectors.rename_corner(store, track_id, code, body.name)
    except KeyError as e:
        raise HTTPException(404, str(e.args[0]) if e.args else "没有这个弯") from None
    return jsonable(layout)
