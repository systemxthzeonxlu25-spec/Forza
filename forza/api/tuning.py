"""调车诊断的路由（前缀 /api）。

    GET /api/laps/{id}/diagnose        单圈诊断（结构见 forza.tuning.diagnose）
    GET /api/diagnose?ids=1,2,3        多圈合并诊断（同车同赛道的几圈，见 forza.tuning.diagnose_many）
    GET /api/tuning/rules              当前生效的阈值（只读；改 forza/tuning_rules.toml）
    GET /api/tuning/outline/{id}       赛道小地图坐标（降采样）。前端优先用圈速模块的 /api/laps/{id}，那个不可用时才用这个

诊断是纯计算（几十毫秒），用普通 def 路由，FastAPI 会放到线程池里跑，不卡事件循环。
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query, Request

from .. import tuning

router = APIRouter(prefix="/api")

MAX_MULTI_LAPS = 20   # 一次最多合并这么多圈


def _get_lap(request: Request, lap_id: int):
    lap = request.app.state.rt.store.get_lap(lap_id)
    if lap is None:
        raise HTTPException(status_code=404, detail=f"找不到第 {lap_id} 号圈（可能已删除）")
    return lap


@router.get("/laps/{lap_id}/diagnose")
def diagnose_lap(lap_id: int, request: Request):
    return tuning.diagnose(_get_lap(request, lap_id))


@router.get("/diagnose")
def diagnose_laps(request: Request, ids: str = Query(..., description="逗号分隔的圈 id，如 1,2,3")):
    try:
        id_list = [int(x) for x in ids.replace("，", ",").split(",") if x.strip()]
    except ValueError:
        raise HTTPException(status_code=400, detail=f"ids 格式不对：{ids!r}，应该是逗号分隔的数字，如 1,2,3") from None
    id_list = list(dict.fromkeys(id_list))   # 去重、保持顺序
    if not id_list:
        raise HTTPException(status_code=400, detail="至少要给一个圈 id")
    if len(id_list) > MAX_MULTI_LAPS:
        raise HTTPException(status_code=400, detail=f"一次最多合并 {MAX_MULTI_LAPS} 圈")
    laps = [_get_lap(request, i) for i in id_list]
    return tuning.diagnose_many(laps)


@router.get("/tuning/rules")
def tuning_rules():
    info = tuning.rules_info()
    return {"source": info["source"], "error": info["error"], "path_hint": "forza/tuning_rules.toml",
            "rules": info["rules"]}


@router.get("/tuning/outline/{lap_id}")
def tuning_outline(lap_id: int, request: Request, points: int = Query(600, ge=50, le=5000)):
    return tuning.track_outline(_get_lap(request, lap_id), points)
