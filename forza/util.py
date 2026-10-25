"""几个模块都要用的小工具，集中放这里（以前每个模块各抄一份）。

    now_iso()              当前本地时间的 ISO 字符串（精确到秒），写进数据库的 created_at 之类
    parse_flags(v)         圈的 flags：库里是 JSON 文本，meta 里是列表 / 集合，统一成列表
    lap_has_flag(m, name)  某一圈（meta / 数据库行）有没有这个标记
    is_standing_first_lap  赛道赛站立起步的第一圈（起点和距离零点都和飞驰圈不一样）
    is_imported_lap(m)     导入 / 回放录像得到的圈（created_at、ended_epoch 是导入那一刻，不是录像当时）
    round_or_none(x, nd)   数值取舍；NaN / inf / 不是数 → None（JSON 里不能出现 NaN）
    jsonable(x)            递归转成能直接 json.dumps 的普通 Python 类型（numpy → Python，NaN / inf → None）

圈的 flags 都有哪些（谁加的、什么意思）：
    first_lap           本场第一圈（laps.LapDetector）：赛道赛是从发车格站立起步的
    partial_start       中途接入：开始记录时这圈已经跑了一段，缺前段数据
    rewind              这圈中途倒过带
    rewound_after_save  落盘之后又被倒带跨回来，成绩作废
    dist_integrated     游戏给的距离不可靠，改用车速积分距离
    finish_inferred     没收到游戏的过线信号，按距离推定跑完
    interrupted         重开 / 换车前的记录
    abandoned           冲刺赛中途放弃（比这条路线已知最长距离短一截）
"""
from __future__ import annotations

import json
import math
from datetime import datetime

import numpy as np

IMPORT_SOURCES = ("import:", "replay:")   # 会话来源以这些开头 = 导入 / 回放录像（见 laps.LapSaver）


def now_iso() -> str:
    """当前本地时间，如 "2026-10-23T21:05:09"。"""
    return datetime.now().isoformat(timespec="seconds")


def parse_flags(v) -> list[str]:
    """把各种形式的 flags 统一成列表：JSON 文本（数据库里的列）/ 列表 / 集合 / None。

    JSON 坏了不抛异常，当作没有标记（坏一圈的标记不该让整个页面出错）。
    """
    if v is None:
        return []
    if isinstance(v, str):
        try:
            v = json.loads(v or "[]")
        except ValueError:
            return []
    if isinstance(v, (set, frozenset)):
        return sorted(v)
    return list(v) if isinstance(v, (list, tuple)) else []


def lap_has_flag(lap: dict, name: str) -> bool:
    """lap 是圈的 meta 或数据库行（dict），看它的 flags 里有没有 name。"""
    return name in parse_flags(lap.get("flags"))


def is_standing_first_lap(lap: dict) -> bool:
    """赛道赛站立起步的第一圈：从起终点线后的发车格起步，距离零点和飞驰圈不一样，不适合拿来当参照。

    冲刺赛每次都是从起点静止起步，这是正常情况，不算。
    """
    return lap_has_flag(lap, "first_lap") and lap.get("kind") != "sprint"


def round_or_none(x, nd: int = 2) -> float | None:
    """数值取舍；NaN / inf / None / 不是数 → None。

    给前端的 JSON 里不能出现 NaN（浏览器的 JSON.parse 会直接失败），所以凡是可能不是有限数的地方都过一遍这个。
    """
    try:
        x = float(x)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(x):
        return None
    return round(x, nd)


def jsonable(x):
    """递归转成能直接 json.dumps 的普通 Python 类型：numpy 数组 / 标量 → list / int / float / bool，
    NaN / inf → None，dict 的键转成字符串。

    接口返回、诊断结果、假数据的标准答案都用这一个（以前三个模块各写了一份，处理的类型还不一样）。
    """
    if isinstance(x, dict):
        return {str(k): jsonable(v) for k, v in x.items()}
    if isinstance(x, (list, tuple)):
        return [jsonable(v) for v in x]
    if isinstance(x, np.ndarray):
        return jsonable(x.tolist())
    if isinstance(x, (bool, np.bool_)):          # bool 要排在 int 前面：bool 是 int 的子类
        return bool(x)
    if isinstance(x, (int, np.integer)):
        return int(x)
    if isinstance(x, (float, np.floating)):
        f = float(x)
        return f if math.isfinite(f) else None
    return x


def is_imported_lap(meta: dict) -> bool:
    """导入 / 回放录像得到的圈：它的 created_at / ended_epoch 是导入那一刻的时间，不能拿来按日期或调校版本归类。
    看会话来源（storage.list_laps 带出来的 session_source）。"""
    return str(meta.get("session_source") or "").startswith(IMPORT_SOURCES)
