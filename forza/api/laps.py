"""圈速列表 / 圈数据 / 两圈对比的路由（前缀 /api）。

    GET    /api/laps?track_id=&car_ordinal=&game=&complete_only=&limit=   圈列表（带“与最佳圈差”和 is_best）
    GET    /api/laps/{id}?step=&channels=a,b                              一圈的原始通道数据
    PATCH  /api/laps/{id}            {"note": "...", "is_complete": true}  改备注 / 标记完整
    DELETE /api/laps/{id}
    GET    /api/tracks               PATCH /api/tracks/{id}            {"name": "..."}
    GET    /api/cars                 PATCH /api/cars/{game}/{ordinal}  {"name": "..."}
    GET    /api/sessions
    GET    /api/compare?a=ID&b=ID&step=2     a = 参考圈，b = 对比圈
    GET    /api/compare?a=ID                 不给 b：拿 a 和同赛道同车的最快完整圈比（a 本身最快就和第二快比）

比较逻辑全在 forza/analysis.py，这里只管取数据、校验参数、出错时给中文提示。
"""
from __future__ import annotations

import math

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from ..analysis import compare as compare_laps, jsonable

router = APIRouter(prefix="/api")

NAME_MAX_LEN = 60
STEP_RANGE = (0.5, 20.0)      # 对比网格步长（米）允许范围
MAX_RAW_STEP = 1000


class LapPatch(BaseModel):
    note: str | None = None
    is_complete: bool | None = None


class NamePatch(BaseModel):
    name: str


def _store(request: Request):
    return request.app.state.rt.store


def _lap_meta_or_404(store, lap_id: int) -> dict:
    meta = store.get_lap_meta(lap_id)
    if meta is None:
        raise HTTPException(404, f"圈 #{lap_id} 不存在（可能已经删掉了）")
    return meta


def _clean_name(name: str) -> str:
    name = (name or "").strip()
    if not name:
        raise HTTPException(400, "名字不能为空")
    if len(name) > NAME_MAX_LEN:
        raise HTTPException(400, f"名字太长了（最多 {NAME_MAX_LEN} 个字）")
    return name


def _parse_id(value: str | None, what: str) -> int:
    if value is None or str(value).strip() == "":
        raise HTTPException(400, f"缺少参数 {what}")
    try:
        return int(str(value).strip())
    except ValueError:
        raise HTTPException(400, f"参数 {what} 必须是圈的编号（整数），收到的是 {value!r}") from None


def _add_best_info(store, laps: list[dict]) -> list[dict]:
    """给每圈加上：同赛道同车的最快完整圈 id / 时间、与它的差、is_best。"""
    cache: dict[tuple, dict | None] = {}
    for lap in laps:
        key = (lap.get("track_id"), lap.get("car_ordinal"), lap.get("game"))
        if key not in cache:
            cache[key] = store.best_lap(key[0], key[1], key[2]) if key[0] is not None else None
        best = cache[key]
        lap["best_lap_id"] = best["id"] if best else None
        lap["best_time"] = best["lap_time"] if best else None
        lap["is_best"] = bool(best and best["id"] == lap["id"])
        ok = best and lap.get("is_complete") and lap.get("lap_time") is not None
        lap["delta_to_best"] = round(lap["lap_time"] - best["lap_time"], 3) if ok else None
    return laps


# ---------------------------------------------------------------- 圈

@router.get("/laps")
def list_laps(request: Request, track_id: int | None = None, car_ordinal: int | None = None,
              game: str | None = None, session_id: int | None = None, complete_only: bool = False,
              limit: int = 500):
    if not 1 <= limit <= 5000:
        raise HTTPException(400, "limit 要在 1~5000 之间")
    store = _store(request)
    laps = store.list_laps(track_id=track_id, car_ordinal=car_ordinal, game=game or None,
                           session_id=session_id, complete_only=complete_only, limit=limit)
    return jsonable(_add_best_info(store, laps))


@router.get("/laps/{lap_id}")
def get_lap(request: Request, lap_id: int, step: int = 1, channels: str | None = None):
    if not 1 <= step <= MAX_RAW_STEP:
        raise HTTPException(400, f"step 要在 1~{MAX_RAW_STEP} 之间（每隔几个采样点取一个）")
    lap = _store(request).get_lap(lap_id)
    if lap is None:
        raise HTTPException(404, f"圈 #{lap_id} 不存在（可能已经删掉了）")
    names = [c.strip() for c in channels.split(",") if c.strip()] if channels else None
    out = lap.to_json(step, names)
    out["meta"] = jsonable(out["meta"])
    out["available_channels"] = list(lap.ch.keys())
    return out


@router.patch("/laps/{lap_id}")
def patch_lap(request: Request, lap_id: int, body: LapPatch):
    store = _store(request)
    _lap_meta_or_404(store, lap_id)
    if body.note is None and body.is_complete is None:
        raise HTTPException(400, "没有要改的内容（可以改 note 或 is_complete）")
    note = body.note.strip() if body.note is not None else None
    store.update_lap(lap_id, note=note, is_complete=body.is_complete)
    return jsonable(_add_best_info(store, [store.get_lap_meta(lap_id)])[0])


@router.delete("/laps/{lap_id}")
def delete_lap(request: Request, lap_id: int):
    store = _store(request)
    _lap_meta_or_404(store, lap_id)
    store.delete_lap(lap_id)
    return {"ok": True, "id": lap_id}


# ---------------------------------------------------------------- 赛道 / 车 / 会话

@router.get("/tracks")
def list_tracks(request: Request):
    return jsonable(_store(request).list_tracks())


@router.patch("/tracks/{track_id}")
def rename_track(request: Request, track_id: int, body: NamePatch):
    store = _store(request)
    if store.get_track(track_id) is None:
        raise HTTPException(404, f"赛道 #{track_id} 不存在")
    store.set_track_name(track_id, _clean_name(body.name))
    return jsonable(store.get_track(track_id))


@router.get("/cars")
def list_cars(request: Request):
    return jsonable(_store(request).list_cars())


@router.patch("/cars/{game}/{car_ordinal}")
def rename_car(request: Request, game: str, car_ordinal: int, body: NamePatch):
    store = _store(request)
    if not any(c["game"] == game and c["car_ordinal"] == car_ordinal for c in store.list_cars()):
        raise HTTPException(404, f"没有这辆车（{game} #{car_ordinal}）")
    store.set_car_name(game, car_ordinal, _clean_name(body.name))
    car = next(c for c in store.list_cars() if c["game"] == game and c["car_ordinal"] == car_ordinal)
    return jsonable(car)


@router.get("/sessions")
def list_sessions(request: Request, limit: int = 100):
    if not 1 <= limit <= 5000:
        raise HTTPException(400, "limit 要在 1~5000 之间")
    return jsonable(_store(request).list_sessions(limit))


# ---------------------------------------------------------------- 对比

def default_reference(store, lap_meta: dict) -> dict | None:
    """同赛道、同车、同游戏里最快的完整圈（排除 lap_meta 自己；它本身最快时就是第二快）。"""
    if lap_meta.get("track_id") is None:
        return None
    laps = store.list_laps(track_id=lap_meta["track_id"], car_ordinal=lap_meta.get("car_ordinal"),
                           game=lap_meta.get("game"), complete_only=True, limit=5000)
    laps = [l for l in laps if l["id"] != lap_meta["id"] and l.get("lap_time")]
    laps.sort(key=lambda l: l["lap_time"])
    return laps[0] if laps else None


@router.get("/compare")
def compare(request: Request, a: str | None = None, b: str | None = None, step: float = 2.0):
    """a = 参考圈，b = 对比圈。不给 b：a 当对比圈，参考圈自动选同赛道同车最快的完整圈。"""
    if not math.isfinite(step) or not STEP_RANGE[0] <= step <= STEP_RANGE[1]:
        raise HTTPException(400, f"step 要在 {STEP_RANGE[0]}~{STEP_RANGE[1]} 米之间")
    store = _store(request)
    a_id = _parse_id(a, "a")
    a_meta = _lap_meta_or_404(store, a_id)
    auto = b is None or str(b).strip() == ""
    if auto:
        best = default_reference(store, a_meta)
        if best is None:
            raise HTTPException(404, f"圈 #{a_id} 所在的赛道 + 车还没有别的完整圈，没法自动选参考圈。"
                                     "再跑一圈，或者手动指定 b")
        ref_id, other_id = best["id"], a_id
    else:
        b_id = _parse_id(b, "b")
        _lap_meta_or_404(store, b_id)
        if b_id == a_id:
            raise HTTPException(400, "A 和 B 是同一圈，请选两圈不同的")
        ref_id, other_id = a_id, b_id

    ref, other = store.get_lap(ref_id), store.get_lap(other_id)
    try:
        res = compare_laps(ref, other, step=step)
    except ValueError as e:
        raise HTTPException(400, f"没法对比：{e}") from None
    res["auto_reference"] = auto
    return res
