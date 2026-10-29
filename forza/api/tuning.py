"""调车诊断的路由（前缀 /api）。

    GET /api/laps/{id}/diagnose?abs=&aero=        单圈诊断（结构见 forza.tuning.diagnose）
    GET /api/diagnose?ids=1,2,3&abs=&aero=        多圈合并诊断（同车同赛道的几圈，见 forza.tuning.diagnose_many）
    GET /api/tuning/rules                         当前生效的阈值（只读；改 forza/tuning_rules.toml）
    GET /api/tuning/outline/{id}                  赛道小地图坐标（降采样）。前端优先用圈速模块的 /api/laps/{id}，那个不可用时才用这个

abs / aero：页面上用户勾选的“我开了 ABS”“这车有可调空力”。1 = 是，0 = 否，不给 = 没说（结论里会提醒）。
弯道：赛道有固定弯道表（forza/sectors.py）时用表里的弯（编号和圈速对比页一致）；还没有表时先按最快的干净圈生成一张，
生成不了 / 出错就退回每圈自己识别弯道。

诊断是纯计算（几十毫秒），用普通 def 路由，FastAPI 会放到线程池里跑，不卡事件循环。
"""
from __future__ import annotations

import logging
import sys

from fastapi import APIRouter, HTTPException, Query, Request

from .. import tuning

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api")

MAX_MULTI_LAPS = 20   # 一次最多合并这么多圈
_TRUE = {"1", "true", "yes", "on", "y"}
_FALSE = {"0", "false", "no", "off", "n"}


def _get_lap(request: Request, lap_id: int):
    lap = request.app.state.rt.store.get_lap(lap_id)
    if lap is None:
        raise HTTPException(status_code=404, detail=f"找不到第 {lap_id} 号圈（可能已删除）")
    return lap


def _flag(value: str | None, what: str) -> bool | None:
    if value is None or str(value).strip() == "":
        return None
    v = str(value).strip().lower()
    if v in _TRUE:
        return True
    if v in _FALSE:
        return False
    raise HTTPException(status_code=400, detail=f"{what} 只能是 1（是）、0（否）或者不给（没说），收到 {value!r}")


def _layout_corners(store, lap):
    """这圈对应的固定弯道表（映射到这圈自己的距离）；没有表 / 生成不了 / 出错时返回 None（诊断自己识别弯道）。"""
    try:
        from ..sectors import ensure_layout, layout_corners
    except Exception:  # noqa: BLE001  分段模块不可用时照常诊断
        return None
    try:
        ensure_layout(store, lap.meta.get("track_id"))
        return layout_corners(store, lap)
    except Exception:  # noqa: BLE001
        log.exception("读取固定弯道表失败（圈 #%s），改用自动识别的弯道", lap.meta.get("id"))
        return None


@router.get("/laps/{lap_id}/diagnose")
def diagnose_lap(lap_id: int, request: Request, abs: str | None = Query(None, description="我开了 ABS：1 / 0"),
                 aero: str | None = Query(None, description="这车有可调空力：1 / 0")):
    abs_on, aero_on = _flag(abs, "abs"), _flag(aero, "aero")
    lap = _get_lap(request, lap_id)
    return tuning.diagnose(lap, corners=_layout_corners(request.app.state.rt.store, lap), abs_on=abs_on, aero=aero_on)


@router.get("/diagnose")
def diagnose_laps(request: Request, ids: str = Query(..., description="逗号分隔的圈 id，如 1,2,3"),
                  abs: str | None = Query(None, description="我开了 ABS：1 / 0"),
                  aero: str | None = Query(None, description="这车有可调空力：1 / 0")):
    abs_on, aero_on = _flag(abs, "abs"), _flag(aero, "aero")
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
    store = request.app.state.rt.store
    return tuning.diagnose_many(laps, corners=[_layout_corners(store, l) for l in laps], abs_on=abs_on, aero=aero_on)


@router.get("/tuning/rules")
def tuning_rules():
    info = tuning.rules_info()
    return {"source": info["source"], "error": info["error"], "path_hint": "程序（Forza.exe / Forza）旁边的 tuning_rules.toml" if getattr(sys, "frozen", False) else "forza/tuning_rules.toml",
            "rules": info["rules"]}


@router.get("/tuning/outline/{lap_id}")
def tuning_outline(lap_id: int, request: Request, points: int = Query(600, ge=50, le=5000)):
    return tuning.track_outline(_get_lap(request, lap_id), points)
