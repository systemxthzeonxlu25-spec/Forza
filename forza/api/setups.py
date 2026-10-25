"""调校版本的路由（前缀 /api，逻辑在 forza/setups.py）。

    GET   /api/setups?game=&car_ordinal=            版本列表（新的在前），每个带圈数、最快、中位数、标准差；
                                                   两个参数都不给 = 所有车的版本
    GET   /api/setups/current?game=&car_ordinal=    {setup: {...}|null, car: {game, car_ordinal, car_pi}|null, hints: [...]}
                                                   参数省略时用实时那台车（rt.hub.latest）；没有版本时也是 200（setup = null）
    POST  /api/setups  {name, note?, game?, car_ordinal?, car_pi?}   新建并设为这台车的当前版本；
                                                   车辆字段省略时用实时那台车，都没有 → 400
    PATCH /api/setups/{id}  {name?, note?}          改名 / 改说明
    POST  /api/setups/{id}/activate                 设为当前版本（换回以前的调校）
    GET   /api/setups/compare?a=&b=&track_id=       版本对比：{a, b, delta_median_s, noise_threshold_s,
                                                   verdict: faster|slower|noise（B 相对 A）, enough_laps, message,
                                                   findings_by_id: [{id, title, a: {laps_with, total_s}, b: {...}}], notes}

版本对比要诊断几圈（每圈几十到几百毫秒），所以都用普通 def 路由（FastAPI 放进线程池跑，不卡收包）。
“像是换了改装”的提示：car_pi / engine_max_rpm / num_cylinders 和记版本时不一样（hints 字段）。
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from .. import setups as su

router = APIRouter(prefix="/api")


class SetupBody(BaseModel):
    name: str | None = None
    note: str | None = None
    game: str | None = None
    car_ordinal: int | None = None
    car_pi: int | None = None


class SetupPatch(BaseModel):
    name: str | None = None
    note: str | None = None


def _rt(request: Request):
    return request.app.state.rt


def _live_car(rt) -> dict | None:
    """实时那台车（最近一帧）；还没收到带车辆信息的帧时 None。"""
    fr = getattr(getattr(rt, "hub", None), "latest", None)
    if fr is None or not getattr(fr, "car_ordinal", 0):
        return None
    return {"game": fr.game, "car_ordinal": fr.car_ordinal, "car_pi": fr.car_pi or None,
            "engine_max_rpm": fr.engine_max_rpm or None, "num_cylinders": fr.num_cylinders or None}


def _observed(rt, game: str, car_ordinal: int) -> dict | None:
    """这台车“现在”的 PI / 最高转速 / 气缸数：实时帧是这台车就用实时的，否则用最近一圈记下的。"""
    live = _live_car(rt)
    if live and live["game"] == game and live["car_ordinal"] == car_ordinal:
        return live
    laps = rt.store.list_laps(game=game, car_ordinal=car_ordinal, limit=1)
    if laps:
        m = laps[0]
        return {"game": game, "car_ordinal": car_ordinal, "car_pi": m.get("car_pi"),
                "engine_max_rpm": m.get("engine_max_rpm"), "num_cylinders": m.get("num_cylinders")}
    return None


def _setup_or_404(store, setup_id: int) -> dict:
    s = su.get_setup(store, setup_id)
    if s is None:
        raise HTTPException(404, f"调校版本 #{setup_id} 不存在")
    return s


@router.get("/setups")
def list_setups(request: Request, game: str | None = None, car_ordinal: int | None = None):
    return su.list_setups(_rt(request).store, game or None, car_ordinal)


@router.get("/setups/current")
def current_setup(request: Request, game: str | None = None, car_ordinal: int | None = None):
    rt = _rt(request)
    if game and car_ordinal is not None:
        car = {"game": game, "car_ordinal": car_ordinal, "car_pi": None}
    elif game or car_ordinal is not None:
        raise HTTPException(400, "game 和 car_ordinal 要一起给（或者都不给 = 实时那台车）")
    else:
        live = _live_car(rt)
        if live is None:
            return {"setup": None, "car": None, "hints": []}
        car = {"game": live["game"], "car_ordinal": live["car_ordinal"], "car_pi": live["car_pi"]}
    obs = _observed(rt, car["game"], car["car_ordinal"])
    if obs and obs.get("car_pi"):
        car["car_pi"] = obs["car_pi"]
    cur = su.current_setup(rt.store, car["game"], car["car_ordinal"])
    setup = su.setup_with_stats(rt.store, cur)
    return {"setup": setup, "car": car, "hints": su.car_hints(cur, obs)}


@router.post("/setups")
def create_setup(request: Request, body: SetupBody):
    rt = _rt(request)
    if body.game and body.car_ordinal is not None:
        game, ordinal = body.game, body.car_ordinal
    elif body.game or body.car_ordinal is not None:
        raise HTTPException(400, "game 和 car_ordinal 要一起给（或者都不给 = 实时那台车）")
    else:
        live = _live_car(rt)
        if live is None:
            raise HTTPException(400, "还没收到游戏数据，不知道是哪台车：先开起来，或者在请求里给 game 和 car_ordinal")
        game, ordinal = live["game"], live["car_ordinal"]
    obs = _observed(rt, game, ordinal) or {}
    try:
        return su.create_setup(rt.store, game, ordinal, body.name, body.note,
                               car_pi=body.car_pi if body.car_pi is not None else obs.get("car_pi"),
                               engine_max_rpm=obs.get("engine_max_rpm"), num_cylinders=obs.get("num_cylinders"))
    except su.SetupError as e:
        raise HTTPException(400, str(e)) from None


@router.get("/setups/compare")
def compare_setups(request: Request, a: int, b: int, track_id: int | None = None):
    """a / b 其中一个可以是 0（su.UNTAGGED_ID）= 这台车没记版本的圈，当改之前的基线。"""
    store = _rt(request).store
    for sid in (a, b):
        if sid != su.UNTAGGED_ID:
            _setup_or_404(store, sid)
    try:
        return su.compare_setups(store, a, b, track_id)
    except su.SetupError as e:
        raise HTTPException(400, str(e)) from None


@router.patch("/setups/{setup_id}")
def patch_setup(request: Request, setup_id: int, body: SetupPatch):
    store = _rt(request).store
    _setup_or_404(store, setup_id)
    try:
        return su.update_setup(store, setup_id, name=body.name, note=body.note)
    except su.SetupError as e:
        raise HTTPException(400, str(e)) from None


@router.post("/setups/{setup_id}/activate")
def activate_setup(request: Request, setup_id: int):
    store = _rt(request).store
    _setup_or_404(store, setup_id)
    return su.activate(store, setup_id)
