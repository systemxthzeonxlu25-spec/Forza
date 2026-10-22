"""赛道固定弯道 / 分段表 + 理论最佳圈 + 逐段稳定性。

为什么要“固定”：对比页和诊断页原来每圈各自识别弯道，两边的 T5 不一定是同一个弯；
每条赛道定死一张表以后，弯道编号、名字、分段边界在所有圈里都一样，才能跨圈统计。

一张表（每条赛道一行，存在 track_layout 表里，第一次用到时自动建表）：
    corners  用这条赛道最快的干净完整圈跑一次 corners.detect_corners 得到的弯，
             位置按占圈长的比例存（start_frac / apex_frac / end_frac），另存弯心坐标（重算时按坐标找回旧名字）
    sectors  每个弯一段；相邻两弯之间的分界放在直道中点（上一个弯终点和下一个弯起点的中间），
             第一段从起点开始，最后一段到终点结束，所以各段首尾相接、加起来就是整圈
    名字     code（T1、T2…）固定不变，name 可以在网页上改（默认 = code）

用法：
    layout = ensure_layout(store, track_id)        # 没有就用最快的干净圈自动生成
    corners = layout_corners(store, lap)           # 把表映射到这圈自己的距离（给对比 / 诊断用）
    res = track_sectors(store, track_id, car_ordinal=..., game=...)   # 理论最佳、各段最快 / 中位数 / 标准差

哪些圈参与统计（干净圈）：完整圈；不是第一圈（发车格起步，起点和飞驰圈不一样；冲刺赛每次都是站立起步，不排除）；
没倒过带；不是放弃的冲刺赛。某一段用时偏离这段中位数超过 OUTLIER_RATIO 的不计入（多半是出事故 / 撞墙）。
"""
from __future__ import annotations

import json
import math
import threading
import weakref
from datetime import datetime

import numpy as np

from .corners import (
    _find_brake_start,
    _find_full_throttle,
    detect_corners,
    resample_by_distance,
)
from .lapdata import LapData

OUTLIER_RATIO = 0.20          # 某段用时偏离这段中位数超过 20% 就不计入（理论最佳 / 标准差都不用它）
MIN_LAPS_FOR_STD = 3          # 至少这么多圈才算标准差、挑“最不稳”的段
REMAP_RADIUS_M = 60.0         # 重算弯道表时，新弯心离旧弯心这么近就沿用旧名字
LAYOUT_LENGTH_TOL = 0.10      # 圈长和弯道表的圈长差超过 10%（没跑完 / 不是同一条路线）就不套用固定表
APEX_SEARCH_M = 200.0         # 按弯心坐标在本圈找弯：只在预计位置前后这么远内找（免得找到别处路过的同一个点）
APEX_MATCH_M = 40.0           # 本圈轨迹离表里的弯心坐标最近也超过这么远 = 没找到，用预计位置
NAME_MAX_LEN = 30
EXCLUDE_FLAGS = {"rewind": "倒过带", "abandoned": "中途放弃"}

SCHEMA = """
CREATE TABLE IF NOT EXISTS track_layout (
    track_id      INTEGER PRIMARY KEY,
    source_lap_id INTEGER,                -- 用哪一圈生成的
    length_m      REAL,                   -- 那一圈的长度
    data          TEXT NOT NULL,          -- JSON：{"corners": [...], "sectors": [...]}
    created_at    TEXT,
    updated_at    TEXT
);
"""

_ready: weakref.WeakSet = weakref.WeakSet()
_ready_lock = threading.Lock()
_times_cache: dict[tuple, dict] = {}
_CACHE_MAX = 5000


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _ensure_table(store) -> None:
    with _ready_lock:
        if store in _ready:
            return
        store.executescript(SCHEMA)
        _ready.add(store)


# ================================================================ 干净圈 / 圈长 / 距离→时间

def exclusion_reason(meta: dict) -> str | None:
    """这圈为什么不参与分段统计（None = 参与）。"""
    if not meta.get("is_complete"):
        return "不完整"
    flags = meta.get("flags") or []
    if "first_lap" in flags and meta.get("kind") != "sprint":
        return "第一圈，发车格起步"
    for f, why in EXCLUDE_FLAGS.items():
        if f in flags:
            return why
    lt = meta.get("lap_time")
    if not isinstance(lt, (int, float)) or not math.isfinite(lt) or lt <= 0:
        return "没有圈时"
    return None


def _lap_length(lap: LapData) -> float:
    d = lap["dist"]
    d = d[np.isfinite(d)]
    return float(np.max(d)) if len(d) else 0.0


def _dist_time(lap: LapData) -> tuple[np.ndarray, np.ndarray]:
    """单调递增的 (距离, 时间)，用来在任意距离上插值时间（倒退 / 重复的点去掉）。"""
    d, t = lap["dist"], lap["t"]
    ok = np.isfinite(d) & np.isfinite(t)
    d, t = d[ok], t[ok]
    if len(d) > 1:
        keep = np.concatenate([[True], np.diff(np.maximum.accumulate(d)) > 1e-6])
        d, t = np.maximum.accumulate(d)[keep], t[keep]
    return d, t


# ================================================================ 生成 / 读写弯道表

def build_layout(lap: LapData) -> dict:
    """用一圈数据生成弯道 / 分段表（还没存库）。识别不出弯道时抛 ValueError。"""
    L = _lap_length(lap)
    if len(lap) < 10 or L < 100:
        raise ValueError("这圈数据太少，没法生成弯道表")
    corners = detect_corners(lap)
    if not corners:
        raise ValueError("这圈没识别出弯道（可能没有坐标数据）")
    keep = np.concatenate([[True], np.diff(lap["dist"]) > 1e-6]) if len(lap) > 1 else np.ones(len(lap), bool)
    dd = np.maximum.accumulate(lap["dist"][keep])

    def pos_at(m: float) -> tuple[float | None, float | None]:
        if not (lap.has("pos_x") and lap.has("pos_z")):
            return None, None
        x = float(np.interp(m, dd, lap["pos_x"][keep]))
        z = float(np.interp(m, dd, lap["pos_z"][keep]))
        return (round(x, 2), round(z, 2)) if math.isfinite(x) and math.isfinite(z) else (None, None)

    out_c = []
    for c in corners:
        ax, az = pos_at(c["apex_m"])
        out_c.append({
            "code": c["name"], "name": c["name"], "index": c["index"], "direction": c["direction"],
            "start_frac": c["start_m"] / L, "apex_frac": c["apex_m"] / L, "end_frac": c["end_m"] / L,
            "heading_change_deg": c["heading_change_deg"], "min_radius_m": round(c["min_radius_m"], 1),
            "apex_x": ax, "apex_z": az,
        })
    # 分段边界：相邻两弯之间直道的中点
    bounds = [0.0]
    for p, q in zip(out_c, out_c[1:]):
        bounds.append((p["end_frac"] + q["start_frac"]) / 2)
    bounds.append(1.0)
    sectors = [{"code": c["code"], "start_frac": bounds[i], "end_frac": bounds[i + 1]} for i, c in enumerate(out_c)]
    return {"corners": out_c, "sectors": sectors, "length_m": round(L, 1),
            "source_lap_id": lap.meta.get("id"), "kind": lap.meta.get("kind")}


def get_layout(store, track_id: int | None) -> dict | None:
    """读这条赛道保存的弯道表（没有就是 None）。返回 {track_id, source_lap_id, length_m, corners, sectors, ...}。"""
    if track_id is None:
        return None
    _ensure_table(store)
    rows = store.query("SELECT * FROM track_layout WHERE track_id=?", (track_id,))
    if not rows:
        return None
    r = rows[0]
    data = json.loads(r["data"])
    names = {c["code"]: c["name"] for c in data["corners"]}
    for s in data["sectors"]:
        s["name"] = names.get(s["code"], s["code"])
    return {"track_id": track_id, "source_lap_id": r["source_lap_id"], "length_m": r["length_m"],
            "created_at": r["created_at"], "updated_at": r["updated_at"], **data}


def save_layout(store, track_id: int, layout: dict) -> dict:
    _ensure_table(store)
    data = json.dumps({"corners": layout["corners"],
                       "sectors": [{k: s[k] for k in ("code", "start_frac", "end_frac")} for s in layout["sectors"]],
                       "kind": layout.get("kind")}, ensure_ascii=False)
    now = _now()
    store.execute(
        """INSERT INTO track_layout (track_id, source_lap_id, length_m, data, created_at, updated_at)
           VALUES (?,?,?,?,?,?)
           ON CONFLICT(track_id) DO UPDATE SET source_lap_id=excluded.source_lap_id,
             length_m=excluded.length_m, data=excluded.data, updated_at=excluded.updated_at""",
        (track_id, layout.get("source_lap_id"), layout.get("length_m"), data, now, now))
    return get_layout(store, track_id)


def _fastest_clean(store, track_id: int) -> dict | None:
    laps = [m for m in store.list_laps(track_id=track_id, complete_only=True, limit=5000)
            if exclusion_reason(m) is None]
    laps.sort(key=lambda m: m["lap_time"])
    return laps[0] if laps else None


def ensure_layout(store, track_id: int | None) -> dict | None:
    """有表就返回；没有就用这条赛道最快的干净完整圈生成并保存。没有干净圈 / 识别不出弯时返回 None。"""
    if track_id is None:
        return None
    layout = get_layout(store, track_id)
    if layout is not None:
        return layout
    best = _fastest_clean(store, track_id)
    if best is None:
        return None
    lap = store.get_lap(best["id"])
    try:
        built = build_layout(lap)
    except ValueError:
        return None
    return save_layout(store, track_id, built)


def rebuild_layout(store, track_id: int, lap_id: int | None = None) -> tuple[dict, list[dict]]:
    """重算弯道表（默认用最快的干净圈，也可以指定一圈）。改过的弯名按弯心坐标就近沿用。

    返回 (新表, 名字映射[{code, name, from_code, distance_m}])。没有可用的圈 / 识别不出弯时抛 ValueError。
    """
    if lap_id is None:
        best = _fastest_clean(store, track_id)
        if best is None:
            raise ValueError("这条赛道还没有干净的完整圈（排除第一圈、倒带、放弃和不完整的圈），没法生成弯道表")
        lap_id = best["id"]
    lap = store.get_lap(lap_id)
    if lap is None:
        raise ValueError(f"圈 #{lap_id} 不存在")
    if lap.meta.get("track_id") != track_id:
        raise ValueError(f"圈 #{lap_id} 不在这条赛道上")
    if _standing_first_lap(lap.meta):
        raise ValueError(f"圈 #{lap_id} 是发车格起步的第一圈，距离从线后算起，和飞驰圈对不上，不能用来生成弯道表")
    new = build_layout(lap)
    old = get_layout(store, track_id)
    kept: list[dict] = []
    if old:
        kept = _carry_names(old, new)
    return save_layout(store, track_id, new), kept


def _carry_names(old: dict, new: dict) -> list[dict]:
    """旧表里改过名字的弯，按弯心坐标（没有坐标时按占圈长比例换成米）就近映射到新表。"""
    renamed = [c for c in old["corners"] if c["name"] != c["code"]]
    if not renamed:
        return []
    L_old, L_new = old.get("length_m") or 1.0, new.get("length_m") or 1.0
    pairs = []
    for oc in renamed:
        for nc in new["corners"]:
            if None not in (oc.get("apex_x"), oc.get("apex_z"), nc.get("apex_x"), nc.get("apex_z")):
                d = math.hypot(oc["apex_x"] - nc["apex_x"], oc["apex_z"] - nc["apex_z"])
            else:
                d = abs(oc["apex_frac"] * L_old - nc["apex_frac"] * L_new)
            if d <= REMAP_RADIUS_M:
                pairs.append((d, oc, nc))
    pairs.sort(key=lambda p: p[0])
    used_old, used_new, kept = set(), set(), []
    for d, oc, nc in pairs:
        if oc["code"] in used_old or nc["code"] in used_new:
            continue
        used_old.add(oc["code"])
        used_new.add(nc["code"])
        nc["name"] = oc["name"]
        kept.append({"code": nc["code"], "name": oc["name"], "from_code": oc["code"], "distance_m": round(d, 1)})
    return kept


def rename_corner(store, track_id: int, code: str, name: str | None) -> dict:
    """改弯名（name 为空 = 恢复成编号）。没有表 / 没有这个弯时抛 KeyError。"""
    layout = get_layout(store, track_id)
    if layout is None:
        raise KeyError("这条赛道还没有弯道表")
    name = (name or "").strip()[:NAME_MAX_LEN] or code
    for c in layout["corners"]:
        if c["code"] == code:
            c["name"] = name
            break
    else:
        raise KeyError(f"没有 {code} 这个弯")
    _ensure_table(store)
    data = json.dumps({"corners": layout["corners"],
                       "sectors": [{k: s[k] for k in ("code", "start_frac", "end_frac")} for s in layout["sectors"]],
                       "kind": layout.get("kind")}, ensure_ascii=False)
    store.execute("UPDATE track_layout SET data=?, updated_at=? WHERE track_id=?", (data, _now(), track_id))
    return get_layout(store, track_id)


# ================================================================ 映射到某一圈

def _standing_first_lap(meta: dict) -> bool:
    """赛道赛的第一圈：从起终点线后的发车格起步，距离从发车格算起（冲刺赛每次都这样，表本身就是这么来的，不算）。"""
    return "first_lap" in (meta.get("flags") or []) and meta.get("kind") != "sprint"


def _has_xy(rs: dict) -> bool:
    """重采样后的坐标能不能用（有通道、没有 NaN、不是全 0）。"""
    if "pos_x" not in rs or "pos_z" not in rs:
        return False
    x, z = rs["pos_x"], rs["pos_z"]
    return bool(np.isfinite(x).all() and np.isfinite(z).all() and np.ptp(x) + np.ptp(z) >= 1.0)


def _locate_apexes(rs: dict, corners: list[dict], expected: list[float]) -> list[float] | None:
    """按表里的弯心坐标在本圈轨迹上找各弯弯心的距离（1 m 网格）。

    每个弯只在预计位置 ±APEX_SEARCH_M 里找离弯心坐标最近的点；最近点也超过 APEX_MATCH_M 的弯用预计位置。
    本圈或表里没有坐标 → None。找出来的顺序乱了（不该发生）→ None，调用方退回预计位置。
    """
    if not _has_xy(rs):
        return None
    pts = [(c.get("apex_x"), c.get("apex_z")) for c in corners]
    if any(x is None or z is None for x, z in pts):
        return None
    xs, zs = np.array([p[0] for p in pts], float), np.array([p[1] for p in pts], float)
    if np.ptp(xs) + np.ptp(zs) < 1.0:                  # 表里坐标全一样（录的时候没坐标）
        return None
    n = len(rs["dist"])
    out = []
    for (ax, az), exp in zip(pts, expected):
        lo = int(max(0, math.floor(exp - APEX_SEARCH_M)))
        hi = int(min(n - 1, math.ceil(exp + APEX_SEARCH_M)))
        if hi <= lo:
            out.append(exp)
            continue
        dd = np.hypot(rs["pos_x"][lo:hi + 1] - ax, rs["pos_z"][lo:hi + 1] - az)
        k = int(np.argmin(dd))
        out.append(float(rs["dist"][lo + k]) if dd[k] <= APEX_MATCH_M else exp)
    if any(b <= a for a, b in zip(out, out[1:])):
        return None
    return out


def layout_corners(store, lap: LapData) -> list[dict] | None:
    """把这条赛道保存的固定弯道表映射到这圈自己的距离。

    映射方法：先按位置预计每个弯的弯心（飞驰圈按圈长比例缩放；发车格起步的第一圈 = 表里的距离 + 发车格偏移，
    偏移 = 这圈圈长 − 表的圈长，因为多出来的那段全在过线之前），再按表里存的弯心坐标在预计位置附近找本圈
    离它最近的点当弯心；弯起点 / 终点按表里和弯心的相对距离放。没有坐标时就用预计位置。

    字段同 corners.detect_corners：name（= 编号 T1…，固定不变）, index, start_m, apex_m, end_m, direction,
    min_speed（本圈 m/s）, min_radius_m, heading_change_deg, brake_start_m / full_throttle_m（本圈，可能为 None），
    另加 code（同 name）, label（显示名，可能被用户改过）, sector_start_m / sector_end_m（这个弯所在分段的范围：
    相邻两弯之间直道的中点，第一段从 0 开始，最后一段到圈长）。
    没有保存的表 / 这圈没有 track_id / 数据太少 / 圈长和表差超过 LAYOUT_LENGTH_TOL（没跑完）→ None。
    """
    tid = lap.meta.get("track_id")
    if tid is None or len(lap) < 10 or not lap.has("dist"):
        return None
    layout = get_layout(store, tid)
    if layout is None or not layout["corners"]:
        return None
    L = _lap_length(lap)
    ref_len = layout.get("length_m") or 0.0
    if L <= 0 or (ref_len > 0 and abs(L - ref_len) / ref_len > LAYOUT_LENGTH_TOL):
        return None
    if _standing_first_lap(lap.meta) and ref_len > 0:
        scale, shift = ref_len, L - ref_len            # 第一圈：整体平移（发车格到线的那段），不拉伸
    else:
        scale, shift = L, 0.0                          # 飞驰圈：走线不同造成的长短差摊到全圈
    rs = resample_by_distance(lap, 1.0, ["speed", "brake", "throttle", "pos_x", "pos_z"], length=L)
    for ch in ("speed", "brake", "throttle"):
        if ch not in rs:
            rs[ch] = np.full_like(rs["dist"], np.nan)
    n = len(rs["dist"])
    cs = layout["corners"]
    expected = [c["apex_frac"] * scale + shift for c in cs]
    apexes = _locate_apexes(rs, cs, expected) or expected

    def clip(x: float) -> float:
        return float(min(L, max(0.0, x)))

    spans = []
    for c, apex in zip(cs, apexes):
        start = clip(apex + (c["start_frac"] - c["apex_frac"]) * scale)
        end = clip(apex + (c["end_frac"] - c["apex_frac"]) * scale)
        spans.append((start, clip(apex), end))
    bounds = [0.0] + [(p[2] + q[0]) / 2 for p, q in zip(spans, spans[1:])] + [L]

    out = []
    for i, (c, (start, apex, end)) in enumerate(zip(cs, spans)):
        a, ap, b = (int(min(n - 1, max(0, round(x)))) for x in (start, apex, end))
        v = rs["speed"][a:b + 1]
        vmin = float(np.nanmin(v)) if len(v) and np.isfinite(v).any() else None
        brake_start = full_thr = None
        if np.isfinite(rs["brake"]).all():
            brake_start = _find_brake_start(rs, a, ap, 1.0)
        if np.isfinite(rs["throttle"]).all():
            full_thr = _find_full_throttle(rs, ap, 1.0)
        out.append({
            "name": c["code"], "code": c["code"], "label": c["name"], "index": c["index"],
            "direction": c["direction"], "start_m": start, "apex_m": apex, "end_m": end,
            "min_speed": vmin, "min_radius_m": c.get("min_radius_m"),
            "heading_change_deg": c.get("heading_change_deg"),
            "brake_start_m": brake_start, "full_throttle_m": full_thr,
            "sector_start_m": bounds[i], "sector_end_m": bounds[i + 1],
        })
    return out


def lap_sector_times(lap: LapData, layout: dict) -> list[float] | None:
    """这圈在各段的用时（s）。边界按圈长比例换到这圈的距离，在边界处插值时间；
    起点记 0、终点记游戏给的圈时，所以各段加起来正好等于圈时。"""
    if len(lap) < 10 or not lap.has("t") or not lap.has("dist"):
        return None
    d, t = _dist_time(lap)
    if len(d) < 2:
        return None
    L = _lap_length(lap)
    fr = [layout["sectors"][0]["start_frac"]] + [s["end_frac"] for s in layout["sectors"]]
    tb = np.interp(np.array(fr) * L, d, t)
    tb[0] = 0.0
    lt = lap.meta.get("lap_time")
    if isinstance(lt, (int, float)) and math.isfinite(lt) and lt > 0:
        tb[-1] = float(lt)
    return [float(x) for x in np.diff(tb)]


def _corner_samples(lap: LapData, store) -> list[dict] | None:
    """这圈各弯的刹车点（这圈自己的距离）和弯心最低速，给离散度统计用。"""
    cs = layout_corners(store, lap)
    if cs is None:
        return None
    return [{"brake_m": c["brake_start_m"], "min_kmh": None if c["min_speed"] is None else c["min_speed"] * 3.6}
            for c in cs]


def _lap_stats(store, meta: dict, layout: dict) -> dict | None:
    """一圈的分段用时 + 各弯刹车点 / 最低速（带缓存：同一圈 + 同一版弯道表只算一次）。"""
    shape = tuple(round(x, 6) for c in layout["corners"] for x in (c["start_frac"], c["apex_frac"], c["end_frac"]))
    key = (str(getattr(store, "path", id(store))), meta["id"], meta.get("lap_time"), shape,
           tuple(round(x["end_frac"], 6) for x in layout["sectors"]))
    hit = _times_cache.get(key)
    if hit is not None:
        return hit
    lap = store.get_lap(meta["id"])
    if lap is None:
        return None
    res = {"times": lap_sector_times(lap, layout), "corners": _corner_samples(lap, store)}
    if len(_times_cache) >= _CACHE_MAX:
        _times_cache.clear()
    _times_cache[key] = res
    return res


# ================================================================ 理论最佳 / 稳定性

def track_sectors(store, track_id: int, *, car_ordinal: int | None = None, game: str | None = None,
                  setup_id=None) -> dict:
    """这条赛道 + 这辆车的分段统计。没有弯道表又生成不了时抛 LookupError。

    car_ordinal / game 不给：取这条赛道最近一圈的那辆车（不同车混在一起没有意义）。
    setup_id：只统计这个调校版本的圈（"none" = 没标记版本的圈）。

    返回：
        track_id, car_ordinal, game, setup_id, layout{source_lap_id, length_m, updated_at}
        segments[{code, name, index, direction, start_frac, end_frac, start_m, end_m,
                  best_s, best_lap_id, best_lap_number, median_s, mean_s, std_s, n, outliers,
                  brake_std_m, min_speed_std_kmh, min_speed_median_kmh}]
        laps[{id, lap_number, lap_time, kind, created_at, setup_id, setup_name, used, reason,
              times[], outlier[], delta_to_best[]}]   按圈号 / 时间顺序
        theoretical_best{time_s, fastest_lap_id, fastest_lap_number, fastest_lap_time, gain_s,
                         from[{code, name, lap_id, lap_number, time_s}]} 或 None
        consistency{most_inconsistent{code, name, std_s} 或 None, summary}
        notes[]
    """
    layout = ensure_layout(store, track_id)
    if layout is None:
        raise LookupError("这条赛道还没有干净的完整圈（排除第一圈、倒带、放弃和不完整的圈），没法生成弯道表。"
                          "再跑一圈完整的飞驰圈就有了")
    all_laps = store.list_laps(track_id=track_id, limit=5000)
    if car_ordinal is None and all_laps:
        car_ordinal, game = all_laps[0]["car_ordinal"], game or all_laps[0]["game"]
    laps = [m for m in all_laps if (car_ordinal is None or m["car_ordinal"] == car_ordinal)
            and (game is None or m["game"] == game)]
    if setup_id is not None and str(setup_id) != "":
        if str(setup_id) == "none":
            laps = [m for m in laps if m.get("setup_id") is None]
        else:
            laps = [m for m in laps if str(m.get("setup_id")) == str(setup_id)]
    laps.sort(key=lambda m: m["id"])

    secs = layout["sectors"]
    k = len(secs)
    rows = []
    for m in laps:
        reason = exclusion_reason(m)
        st = None if reason else _lap_stats(store, m, layout)
        times = st["times"] if st else None
        if reason is None and times is None:
            reason = "数据太少"
        rows.append({"id": m["id"], "lap_number": m.get("lap_number"), "lap_time": m.get("lap_time"),
                     "kind": m.get("kind"), "created_at": m.get("created_at"), "setup_id": m.get("setup_id"),
                     "setup_name": m.get("setup_name"), "used": reason is None, "reason": reason,
                     "times": [round(x, 3) for x in times] if times else None,
                     "outlier": [False] * k if times else None, "_corners": st["corners"] if st else None})
    used = [r for r in rows if r["used"]]

    segments = []
    for j, s in enumerate(secs):
        vals = [(r["times"][j], r) for r in used]
        med = float(np.median([v for v, _ in vals])) if vals else None
        good = []
        for v, r in vals:
            if med and med > 0 and abs(v - med) / med > OUTLIER_RATIO:
                r["outlier"][j] = True
            else:
                good.append((v, r))
        seg = {"code": s["code"], "name": s.get("name", s["code"]), "index": j,
               "direction": next((c["direction"] for c in layout["corners"] if c["code"] == s["code"]), None),
               "start_frac": round(s["start_frac"], 6), "end_frac": round(s["end_frac"], 6),
               "start_m": round(s["start_frac"] * layout["length_m"], 1),
               "end_m": round(s["end_frac"] * layout["length_m"], 1),
               "n": len(good), "outliers": len(vals) - len(good),
               "best_s": None, "best_lap_id": None, "best_lap_number": None,
               "median_s": None, "mean_s": None, "std_s": None,
               "brake_std_m": None, "min_speed_std_kmh": None, "min_speed_median_kmh": None}
        if good:
            gv = np.array([v for v, _ in good])
            bi = int(np.argmin(gv))
            seg.update(best_s=round(float(gv[bi]), 3), best_lap_id=good[bi][1]["id"],
                       best_lap_number=good[bi][1]["lap_number"], median_s=round(float(np.median(gv)), 3),
                       mean_s=round(float(np.mean(gv)), 3))
            if len(gv) >= 2:
                seg["std_s"] = round(float(np.std(gv, ddof=1)), 3)
            # 刹车点 / 弯心最低速的离散度（只用这段没被剔除的圈）
            br = [r["_corners"][j]["brake_m"] for _, r in good
                  if r["_corners"] and j < len(r["_corners"]) and r["_corners"][j]["brake_m"] is not None]
            mn = [r["_corners"][j]["min_kmh"] for _, r in good
                  if r["_corners"] and j < len(r["_corners"]) and r["_corners"][j]["min_kmh"] is not None]
            if len(br) >= 2:
                seg["brake_std_m"] = round(float(np.std(br, ddof=1)), 1)
            if len(mn) >= 2:
                seg["min_speed_std_kmh"] = round(float(np.std(mn, ddof=1)), 1)
            if mn:
                seg["min_speed_median_kmh"] = round(float(np.median(mn)), 1)
        segments.append(seg)

    for r in rows:
        r.pop("_corners", None)
        if r["times"]:
            r["delta_to_best"] = [None if segments[j]["best_s"] is None else round(r["times"][j] - segments[j]["best_s"], 3)
                                  for j in range(k)]
        else:
            r["delta_to_best"] = None

    theo = None
    if used and all(s["best_s"] is not None for s in segments):
        fastest = min(used, key=lambda r: r["lap_time"])
        total = sum(s["best_s"] for s in segments)
        theo = {"time_s": round(total, 3), "fastest_lap_id": fastest["id"],
                "fastest_lap_number": fastest["lap_number"], "fastest_lap_time": fastest["lap_time"],
                "gain_s": round(fastest["lap_time"] - total, 3),
                "from": [{"code": s["code"], "name": s["name"], "lap_id": s["best_lap_id"],
                          "lap_number": s["best_lap_number"], "time_s": s["best_s"]} for s in segments]}

    cand = [s for s in segments if s["std_s"] is not None and s["n"] >= MIN_LAPS_FOR_STD]
    worst = max(cand, key=lambda s: s["std_s"]) if cand else None
    consistency = {
        "most_inconsistent": {"code": worst["code"], "name": worst["name"], "std_s": worst["std_s"]} if worst else None,
        "summary": (f"{worst['name']} 最不稳 ±{worst['std_s']:.2f} s" if worst
                    else f"干净圈不到 {MIN_LAPS_FOR_STD} 圈，先不算稳定性"),
    }
    notes = [f"参与统计 {len(used)} 圈，排除 {len(rows) - len(used)} 圈（第一圈、倒带、放弃、不完整）",
             f"某段用时偏离这段中位数超过 {OUTLIER_RATIO:.0%} 的不计入",
             "理论最佳 = 各段最快之和，可能来自不同的圈"]
    if any("dist_integrated" in (m.get("flags") or []) for m in laps):
        notes.append("有的圈距离是按车速积分估算的，分段边界可能差几米")
    return {
        "track_id": track_id, "car_ordinal": car_ordinal, "game": game,
        "setup_id": None if setup_id in (None, "") else setup_id,
        "layout": {"source_lap_id": layout["source_lap_id"], "length_m": layout["length_m"],
                   "created_at": layout.get("created_at"), "updated_at": layout.get("updated_at"),
                   "corner_count": len(layout["corners"])},
        "segments": segments, "laps": rows, "theoretical_best": theo, "consistency": consistency,
        "notes": notes, "rules": {"outlier_ratio": OUTLIER_RATIO, "min_laps_for_std": MIN_LAPS_FOR_STD},
    }
