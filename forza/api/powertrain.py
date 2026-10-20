"""实测功率曲线 / 各挡传动比 / 最佳换挡转速的路由（前缀 /api，逻辑在 forza/powertrain.py）。

    GET  /api/powertrain?game=&car_ordinal=&car_pi=
         200 {game, car_ordinal, car_pi, source_laps, redline_rpm, shift_rpm: {"1": 7350, ...}（只含算得出的挡）,
              ratios: {"1": 综合传动比, ...}, power_curve: [{rpm, power_kw, torque}], actual_upshift_rpm: {...},
              actual_upshift_drop_rpm, upshift_counts, gears: [每挡一行], wheel_radius_m, notes: [...]}
         这台车一点数据都没有 → 404 {detail}。参数省略时用实时那台车；car_pi 省略时用最近一圈的 PI。
    GET  /api/powertrain/final_drive?game=&car_ordinal=&car_pi=&final_now=&final_new=&track_id=
         改终传预测：各挡红线车速、直道末端会不会撞断油（终传要手填，Data Out 没有）
    POST /api/powertrain/rebuild?game=&car_ordinal=     清掉这台车的动力总成数据，用已存的圈重新算

第一次查某台车时会给最近的老圈补算（解压几十圈，可能要一两秒），所以用普通 def 路由（放线程池跑）。
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request

from .. import powertrain as pt

router = APIRouter(prefix="/api")


def _car(request: Request, game: str | None, car_ordinal: int | None) -> tuple[str, int, int | None]:
    """参数里的车；都省略时用实时那台车（返回 game, ordinal, 实时 PI）。"""
    if game and car_ordinal is not None:
        return game, car_ordinal, None
    if game or car_ordinal is not None:
        raise HTTPException(400, "game 和 car_ordinal 要一起给（或者都不给 = 实时那台车）")
    fr = request.app.state.rt.hub.latest
    if fr is None or not fr.car_ordinal:
        raise HTTPException(404, "还没收到游戏数据，不知道是哪台车")
    return fr.game, fr.car_ordinal, fr.car_pi or None


@router.get("/powertrain")
def get_powertrain(request: Request, game: str | None = None, car_ordinal: int | None = None,
                   car_pi: int | None = None):
    g, o, live_pi = _car(request, game, car_ordinal)
    s = pt.summary(request.app.state.rt.store, g, o, car_pi if car_pi is not None else live_pi)
    if s is None:
        raise HTTPException(404, f"这台车（{g} #{o}{f'，PI {car_pi}' if car_pi else ''}）还没有可用的数据："
                                 "要先存几圈，里面有全油门加速和各挡位的行驶")
    return s


@router.get("/powertrain/final_drive")
def final_drive(request: Request, final_now: float, final_new: float, game: str | None = None,
                car_ordinal: int | None = None, car_pi: int | None = None, track_id: int | None = None):
    g, o, live_pi = _car(request, game, car_ordinal)
    try:
        return pt.predict_final_drive(request.app.state.rt.store, g, o, final_now, final_new,
                                      car_pi if car_pi is not None else live_pi, track_id)
    except ValueError as e:
        raise HTTPException(400, str(e)) from None
    except LookupError as e:
        raise HTTPException(404, str(e)) from None


@router.post("/powertrain/rebuild")
def rebuild(request: Request, game: str, car_ordinal: int):
    n = pt.rebuild(request.app.state.rt.store, game, car_ordinal)
    return {"laps": n}
