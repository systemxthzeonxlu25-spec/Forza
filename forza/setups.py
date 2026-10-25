"""调校版本：给每套调校起个名字，之后存的圈自动归到这个版本；两个版本之间对比圈速（带噪音判断）和诊断。

Data Out 里没有任何调校数值，所以“版本”只是用户手填的名字 + 一行说明，系统负责：
- 记下每台车（game + car_ordinal）的“当前版本”，以及每次切换的时刻（setup_activations 表）；
- LapSaver 存圈时调用 tag_lap_meta()：按这圈**跑完的时刻**找当时生效的版本，往 meta 里写 setup_id / setup_name
  （存进 laps.meta JSON 列，storage.list_laps 会自动展开）。按时刻找而不是按“存档时的当前版本”，
  是因为最后一圈要等 12 s 挂起窗口或 30 s 空闲才落盘：跑完 → 进菜单 → 手机上记新版本，这圈仍算旧版本；
- 版本对比只用完整飞驰圈（排除站立起步的第一圈、倒过带的圈），慢于中位数 5% 以上的圈（失误圈）不计；
  噪音判断用标准误：|Δ中位数| < 2·sqrt(s1²/n1 + s2²/n2) 判“在噪音内”；每个版本少于 MIN_LAPS 圈直接说再跑几圈；
- 诊断对比：每个版本取最近几圈调 forza.tuning.diagnose（只读使用），按 finding.id 统计出现圈数和累计时长；
- car_pi / engine_max_rpm / num_cylinders 和记版本时不一样 → 提示“像是换了改装”（单纯改调校不会改 PI）。

表（CREATE TABLE IF NOT EXISTS，老库第一次用时自动建）：
    setups             id, game, car_ordinal, car_pi, engine_max_rpm, num_cylinders, name, note, created_at
    setup_activations  每次“设为当前”一行：game, car_ordinal, setup_id, at（time.time() 秒）
                       一台车的当前版本 = 它最新的那一行

对外函数（路由在 forza/api/setups.py）：
    tag_lap_meta(store, meta)                     存圈前给 meta 打版本标签
    create_setup(store, game, car_ordinal, name, note=None, car_pi=None, engine_max_rpm=None, num_cylinders=None)
    update_setup(store, setup_id, name=None, note=None) / activate(store, setup_id)
    get_setup(store, setup_id) / current_setup(store, game, car_ordinal) / setup_at(store, game, car_ordinal, at)
    list_setups(store, game=None, car_ordinal=None)   带圈数、最快、中位数、标准差
    compare_setups(store, a_id, b_id, track_id=None)  版本对比（一边可以是 UNTAGGED_ID = 没记版本的圈）
    car_hints(setup, observed)                    “像是换了改装”的提示
"""
from __future__ import annotations

import logging
import math
import statistics
import threading
import time
from collections import OrderedDict

from .util import now_iso, parse_flags

log = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS setups (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    game           TEXT NOT NULL,
    car_ordinal    INTEGER NOT NULL,
    car_pi         INTEGER,
    engine_max_rpm REAL,
    num_cylinders  INTEGER,
    name           TEXT NOT NULL,
    note           TEXT,
    created_at     TEXT
);
CREATE INDEX IF NOT EXISTS idx_setups_car ON setups(game, car_ordinal);
CREATE TABLE IF NOT EXISTS setup_activations (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    game        TEXT NOT NULL,
    car_ordinal INTEGER NOT NULL,
    setup_id    INTEGER NOT NULL,
    at          REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_setup_act_car ON setup_activations(game, car_ordinal, at);
"""

NAME_MAX_LEN = 60
NOTE_MAX_LEN = 300
MIN_LAPS = 3                 # 每个版本至少这么多圈完整飞驰圈才做噪音判断
OUTLIER_FRAC = 0.05          # 比这组中位数慢 5% 以上的圈当失误圈，不计入中位数 / 标准差
NOISE_K = 2.0                # 噪音阈值 = NOISE_K × 标准误
MIN_NOISE_S = 0.005          # 噪音阈值下限（两组圈时完全一样时别出现 0）
MAX_DIAG_LAPS = 8            # 诊断对比每个版本最多诊断最近这么多圈（每圈几十到几百毫秒）
LIST_LAP_LIMIT = 5000
# “像是换了改装”要看的字段：(字段, 中文名)
CAR_FIELDS = (("car_pi", "PI"), ("engine_max_rpm", "最高转速"), ("num_cylinders", "气缸数"))
UNTAGGED_ID = 0              # 版本对比里代表“没记版本的圈”（setup_id 为空）
UNTAGGED_NAME = "没记版本的圈"
ONE_CHANGE_TIP = "建议一次只改一项，改完记一个新版本、跑 5 圈以上再对比，这样才知道是哪一项起的作用。"


class SetupError(ValueError):
    """参数不对 / 找不到版本，message 是给用户看的中文提示。"""



def ensure_schema(store) -> None:
    """建表（每个 Store 只做一次）。"""
    if getattr(store, "_setups_ready", False):
        return
    store.executescript(SCHEMA)
    store._setups_ready = True


# ---------------------------------------------------------------- 增改查

def _row(store, setup_id: int) -> dict | None:
    rows = store.query("SELECT * FROM setups WHERE id=?", (setup_id,))
    return rows[0] if rows else None


def get_setup(store, setup_id: int) -> dict | None:
    ensure_schema(store)
    s = _row(store, setup_id)
    if s:
        cur = current_setup(store, s["game"], s["car_ordinal"])
        s["is_current"] = bool(cur and cur["id"] == s["id"])
    return s


def setup_at(store, game: str, car_ordinal: int, at: float) -> dict | None:
    """at 时刻（time.time() 秒）这台车生效的版本；那时还没记过版本返回 None。"""
    ensure_schema(store)
    rows = store.query(
        "SELECT s.* FROM setup_activations a JOIN setups s ON s.id = a.setup_id "
        "WHERE a.game=? AND a.car_ordinal=? AND a.at <= ? ORDER BY a.at DESC, a.id DESC LIMIT 1",
        (game, car_ordinal, float(at)))
    return rows[0] if rows else None


def current_setup(store, game: str, car_ordinal: int) -> dict | None:
    """这台车的当前版本（最近一次“设为当前”的那个）。"""
    ensure_schema(store)
    rows = store.query(
        "SELECT s.* FROM setup_activations a JOIN setups s ON s.id = a.setup_id "
        "WHERE a.game=? AND a.car_ordinal=? ORDER BY a.at DESC, a.id DESC LIMIT 1", (game, car_ordinal))
    return rows[0] if rows else None


def _clean_name(name) -> str:
    name = (name or "").strip() if isinstance(name, str) or name is None else str(name).strip()
    if not name:
        raise SetupError("版本名不能为空")
    if len(name) > NAME_MAX_LEN:
        raise SetupError(f"版本名太长了（最多 {NAME_MAX_LEN} 个字）")
    return name


def _clean_note(note) -> str | None:
    if note is None:
        return None
    note = str(note).strip()
    if len(note) > NOTE_MAX_LEN:
        raise SetupError(f"说明太长了（最多 {NOTE_MAX_LEN} 个字）")
    return note or None


def create_setup(store, game: str, car_ordinal: int, name: str, note: str | None = None, *,
                 car_pi: int | None = None, engine_max_rpm: float | None = None,
                 num_cylinders: int | None = None, at: float | None = None) -> dict:
    """新建一个版本，并设为这台车的当前版本（从 at 时刻起跑完的圈都归它，默认现在）。"""
    ensure_schema(store)
    if not game or car_ordinal is None:
        raise SetupError("不知道是哪台车（缺 game / car_ordinal）")
    name, note = _clean_name(name), _clean_note(note)
    sid = store.execute(
        "INSERT INTO setups (game, car_ordinal, car_pi, engine_max_rpm, num_cylinders, name, note, created_at) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (game, int(car_ordinal), car_pi, engine_max_rpm, num_cylinders, name, note, now_iso()))
    activate(store, sid, at=at)
    return get_setup(store, sid)


def update_setup(store, setup_id: int, *, name: str | None = None, note: str | None = None) -> dict:
    """改名 / 改说明（给了哪个改哪个；note 给空字符串 = 清空）。"""
    ensure_schema(store)
    if _row(store, setup_id) is None:
        raise SetupError(f"调校版本 #{setup_id} 不存在")
    if name is not None:
        store.execute("UPDATE setups SET name=? WHERE id=?", (_clean_name(name), setup_id))
    if note is not None:
        store.execute("UPDATE setups SET note=? WHERE id=?", (_clean_note(note), setup_id))
    return get_setup(store, setup_id)


def activate(store, setup_id: int, at: float | None = None) -> dict:
    """设为这台车的当前版本（换回以前的调校时用）。"""
    ensure_schema(store)
    s = _row(store, setup_id)
    if s is None:
        raise SetupError(f"调校版本 #{setup_id} 不存在")
    t = time.time() if at is None else float(at)
    # 同一时刻（同一毫秒）连点两次：后一次稍微往后挪一点，保证“最新一行”就是最后点的那个
    last = store.query("SELECT MAX(at) AS m FROM setup_activations WHERE game=? AND car_ordinal=?",
                       (s["game"], s["car_ordinal"]))[0]["m"]
    if last is not None and t <= last and at is None:
        t = last + 1e-3
    store.execute("INSERT INTO setup_activations (game, car_ordinal, setup_id, at) VALUES (?,?,?,?)",
                  (s["game"], s["car_ordinal"], setup_id, t))
    return get_setup(store, setup_id)


def tag_lap_meta(store, meta: dict) -> dict | None:
    """存圈前调用：按这圈跑完的时刻（meta["ended_epoch"]，没有就用现在）找当时生效的版本，
    往 meta 里写 setup_id / setup_name。没有版本时什么都不写，返回 None。"""
    game, car = meta.get("game"), meta.get("car_ordinal")
    if not game or car is None:
        return None
    at = meta.get("ended_epoch") or time.time()
    s = setup_at(store, game, car, at)
    if s:
        meta["setup_id"] = s["id"]
        meta["setup_name"] = s["name"]
    return s


# ---------------------------------------------------------------- 统计

def is_flying(lap: dict) -> bool:
    """完整的飞驰圈：赛道赛、跑完、不是站立起步的第一圈、没倒过带。"""
    return (lap.get("kind") == "lap" and bool(lap.get("is_complete")) and (lap.get("lap_time") or 0) > 0
            and not (set(parse_flags(lap.get("flags"))) & {"first_lap", "rewind"}))


def lap_time_stats(times: list[float]) -> dict:
    """一组圈时 → 中位数、标准差（去掉比中位数慢 5% 以上的失误圈后再算）。"""
    times = [float(t) for t in times if t and t > 0]
    if not times:
        return {"n": 0, "outliers": 0, "median_time": None, "mean_time": None, "std_time": None,
                "best_time": None, "kept": []}
    m0 = statistics.median(times)
    kept = [t for t in times if t <= m0 * (1 + OUTLIER_FRAC)]
    n = len(kept)
    return {
        "n": n, "outliers": len(times) - n,
        "median_time": round(statistics.median(kept), 3),
        "mean_time": round(statistics.fmean(kept), 3),
        "std_time": round(statistics.stdev(kept), 3) if n >= 2 else None,
        "best_time": round(min(times), 3),
        "kept": kept,
    }


def _car_laps(store, game: str, car_ordinal: int) -> list[dict]:
    return store.list_laps(game=game, car_ordinal=car_ordinal, limit=LIST_LAP_LIMIT)


def _main_track(laps: list[dict]) -> int | None:
    """飞驰圈最多的赛道（并列时取最近跑的）。"""
    count: dict = {}
    last: dict = {}
    for l in laps:
        if is_flying(l) and l.get("track_id") is not None:
            count[l["track_id"]] = count.get(l["track_id"], 0) + 1
            last[l["track_id"]] = max(last.get(l["track_id"], 0), l["id"])
    if not count:
        return None
    return max(count, key=lambda t: (count[t], last[t]))


def _setup_stats(laps: list[dict], track_id: int | None = None) -> dict:
    """一个版本的圈：圈数（全部）+ 某条赛道（默认飞驰圈最多的那条）上的最快 / 中位数 / 标准差。"""
    tid = track_id if track_id is not None else _main_track(laps)
    on_track = [l for l in laps if tid is not None and l.get("track_id") == tid]
    st = lap_time_stats([l["lap_time"] for l in on_track if is_flying(l)])
    complete = [l["lap_time"] for l in on_track if l.get("is_complete") and l.get("kind") == "lap" and l.get("lap_time")]
    tracks: dict = {}
    for l in laps:
        if l.get("track_id") is not None:
            e = tracks.setdefault(l["track_id"], {"track_id": l["track_id"], "track_name": l.get("track_name"),
                                                  "laps": 0, "flying_laps": 0})
            e["laps"] += 1
            e["flying_laps"] += 1 if is_flying(l) else 0
    return {
        "lap_count": len(laps),
        "flying_laps": st["n"],
        "outlier_laps": st["outliers"],
        "best_time": round(min(complete), 3) if complete else None,
        "median_time": st["median_time"],
        "std_time": st["std_time"],
        "track_id": tid,
        "track_name": next((l.get("track_name") for l in on_track), None),
        "tracks": sorted(tracks.values(), key=lambda e: -e["laps"]),
        "last_lap_at": max((l.get("created_at") or "" for l in laps), default=None) or None,
    }


def car_hints(setup: dict | None, observed: dict | None) -> list[str]:
    """记版本时的 PI / 最高转速 / 气缸数 和现在看到的不一样 → “像是换了改装”。"""
    if not setup or not observed:
        return []
    diffs = []
    for key, cn in CAR_FIELDS:
        a, b = setup.get(key), observed.get(key)
        if a in (None, 0) or b in (None, 0):
            continue
        if (abs(float(a) - float(b)) > 0.5) if key != "engine_max_rpm" else (abs(float(a) - float(b)) > 1.0):
            diffs.append(f"{cn} {_num(a)} → {_num(b)}")
    if not diffs:
        return []
    return [f"这台车的 {'、'.join(diffs)}，和记“{setup['name']}”时不一样，像是换了改装（单纯改调校不会改这些）。"
            "要不要记一个新版本？"]


def _num(x) -> str:
    try:
        f = float(x)
    except (TypeError, ValueError):
        return str(x)
    return str(int(round(f))) if abs(f - round(f)) < 1e-6 else f"{f:g}"


def _lap_car_values(laps: list[dict]) -> dict:
    """一组圈里各车辆字段出现过的值（去重、按出现顺序）。"""
    out: dict = {}
    for key, _ in CAR_FIELDS:
        vals = []
        for l in sorted(laps, key=lambda x: x["id"]):
            v = l.get(key)
            if v not in (None, 0) and v not in vals:
                vals.append(v)
        out[key] = vals
    return out


def _inner_hint(setup: dict, laps: list[dict]) -> str | None:
    """这个版本里的圈，PI 等字段前后不一致（中途换了改装却没记新版本）。"""
    vals = _lap_car_values(laps)
    bad = [f"{cn} {' / '.join(_num(v) for v in vals[key])}" for key, cn in CAR_FIELDS if len(vals[key]) > 1]
    if not bad:
        latest = max(laps, key=lambda x: x["id"]) if laps else None
        return (car_hints(setup, latest) or [None])[0]
    return f"这个版本的圈里出现了不同的{'、'.join(bad)}：像是中途换了改装，最好拆成两个版本。"


def list_setups(store, game: str | None = None, car_ordinal: int | None = None) -> list[dict]:
    """版本列表（新的在前），每个带圈数、最快、中位数、标准差（主赛道 = 这个版本飞驰圈最多的赛道）。"""
    ensure_schema(store)
    sql, args = "SELECT * FROM setups WHERE 1=1", []
    if game:
        sql += " AND game=?"
        args.append(game)
    if car_ordinal is not None:
        sql += " AND car_ordinal=?"
        args.append(car_ordinal)
    rows = store.query(sql + " ORDER BY id DESC", args)
    laps_by_car: dict = {}
    current: dict = {}
    out = []
    for s in rows:
        key = (s["game"], s["car_ordinal"])
        if key not in laps_by_car:
            laps_by_car[key] = _car_laps(store, *key)
            cur = current_setup(store, *key)
            current[key] = cur["id"] if cur else None
        laps = [l for l in laps_by_car[key] if l.get("setup_id") == s["id"]]
        out.append({**s, "is_current": current[key] == s["id"], **_setup_stats(laps),
                    "hint": _inner_hint(s, laps)})
    return out


def setup_with_stats(store, setup: dict | None) -> dict | None:
    if setup is None:
        return None
    laps = [l for l in _car_laps(store, setup["game"], setup["car_ordinal"]) if l.get("setup_id") == setup["id"]]
    cur = current_setup(store, setup["game"], setup["car_ordinal"])
    return {**setup, "is_current": bool(cur and cur["id"] == setup["id"]), **_setup_stats(laps),
            "hint": _inner_hint(setup, laps)}


# ---------------------------------------------------------------- 对比

_DIAG_CACHE: "OrderedDict[tuple, dict]" = OrderedDict()
_DIAG_CACHE_MAX = 256
_DIAG_LOCK = threading.Lock()           # 路由在线程池里跑，几个请求可能同时读写缓存


def _diagnose(store, lap_id: int) -> dict | None:
    """单圈诊断（只读调用 forza.tuning.diagnose；有固定弯道表时传进去，和诊断页一致），结果按圈缓存。"""
    from . import tuning
    try:
        rules_key = id(tuning.get_rules())
    except Exception:
        rules_key = None
    key = (id(store), lap_id, rules_key)
    with _DIAG_LOCK:
        if key in _DIAG_CACHE:
            _DIAG_CACHE.move_to_end(key)
            return _DIAG_CACHE[key]
    lap = store.get_lap(lap_id)
    if lap is None:
        return None
    corners = None
    try:
        from .sectors import layout_corners
        corners = layout_corners(store, lap)
    except Exception:
        corners = None
    try:
        res = tuning.diagnose(lap, corners=corners) if corners is not None else tuning.diagnose(lap)
    except TypeError:                     # 老版本 diagnose 不收 corners
        res = tuning.diagnose(lap)
    with _DIAG_LOCK:
        _DIAG_CACHE[key] = res
        while len(_DIAG_CACHE) > _DIAG_CACHE_MAX:
            _DIAG_CACHE.popitem(last=False)
    return res


def _findings_count(store, lap_ids: list[int]) -> tuple[dict, int]:
    """几圈的诊断按 finding.id 汇总：{id: {title, category_label, laps_with, total_s}}，以及成功诊断的圈数。"""
    out: dict = {}
    n = 0
    for lid in lap_ids:
        try:
            res = _diagnose(store, lid)
        except Exception:
            log.exception("诊断圈 #%s 出错（版本对比里跳过这圈）", lid)
            continue
        if not res or not res.get("usable", True):
            continue
        n += 1
        seen = set()
        for f in res.get("findings") or []:
            fid = f.get("id")
            if not fid:
                continue
            e = out.setdefault(fid, {"title": f.get("title"), "category_label": f.get("category_label"),
                                     "severity": f.get("severity"), "laps_with": 0, "total_s": None})
            if fid not in seen:
                e["laps_with"] += 1
                seen.add(fid)
            ts = (f.get("data") or {}).get("total_s")
            if isinstance(ts, (int, float)) and math.isfinite(ts):
                e["total_s"] = round((e["total_s"] or 0.0) + float(ts), 3)
            if (f.get("severity") == "alert") and e.get("severity") != "alert":
                e["severity"] = "alert"
    return out, n


def _pick_track(laps_a: list[dict], laps_b: list[dict], track_id: int | None) -> int | None:
    if track_id is not None:
        return track_id
    ca: dict = {}
    cb: dict = {}
    for l in laps_a:
        if is_flying(l):
            ca[l["track_id"]] = ca.get(l["track_id"], 0) + 1
    for l in laps_b:
        if is_flying(l):
            cb[l["track_id"]] = cb.get(l["track_id"], 0) + 1
    common = [t for t in ca if t in cb and t is not None]
    if not common:
        return None
    return max(common, key=lambda t: (min(ca[t], cb[t]), ca[t] + cb[t]))


def _side(setup: dict, laps: list[dict], track_id: int | None) -> dict:
    flying = sorted([l for l in laps if is_flying(l) and l.get("track_id") == track_id], key=lambda l: l["id"])
    st = lap_time_stats([l["lap_time"] for l in flying])
    kept = list(st.pop("kept"))
    lap_rows = []
    for l in flying:
        keep = l["lap_time"] in kept
        if keep:
            kept.remove(l["lap_time"])
        lap_rows.append({"id": l["id"], "lap_time": round(l["lap_time"], 3), "outlier": not keep,
                         "car_pi": l.get("car_pi")})
    return {"id": setup["id"], "name": setup["name"], "note": setup.get("note"), "car_pi": setup.get("car_pi"),
            "created_at": setup.get("created_at"), **st, "laps": lap_rows,
            "car_values": _lap_car_values(flying)}


def _untagged(other: dict) -> dict:
    """“没记版本的圈”当成一个虚拟版本（id = UNTAGGED_ID）：第一次记版本之前跑的圈都在这里，正好当改之前的基线。"""
    return {"id": UNTAGGED_ID, "game": other["game"], "car_ordinal": other["car_ordinal"], "car_pi": None,
            "name": UNTAGGED_NAME, "note": None, "created_at": None}


def _sector_compare(store, track_id: int, A: dict, B: dict, laps: list[dict]) -> dict | None:
    """两个版本按赛道固定弯道表逐段比：每段两边用时的中位数、差（B − A，负 = B 快）和噪音阈值（同圈速的 2 × 标准误）。
    只用两边没被当成失误圈的飞驰圈。没有弯道表（生成不了）时返回 None。"""
    from .sectors import _lap_stats, ensure_layout

    try:
        layout = ensure_layout(store, track_id)
    except Exception:
        log.exception("版本逐段对比：读取弯道表失败")
        return None
    if not layout or not layout.get("sectors"):
        return None
    meta = {l["id"]: l for l in laps}
    n_sec = len(layout["sectors"])

    def side_times(side: dict) -> list[list[float]]:
        out = []
        for r in side["laps"]:
            if r["outlier"] or r["id"] not in meta:
                continue
            st = _lap_stats(store, meta[r["id"]], layout)
            ts = st and st.get("times")
            if ts and len(ts) == n_sec and all(math.isfinite(x) and x > 0 for x in ts):
                out.append(ts)
        return out

    ta, tb = side_times(A), side_times(B)
    names = {c["code"]: c.get("name") or c["code"] for c in layout["corners"]}
    rows = []
    for i, sec in enumerate(layout["sectors"]):
        xa, xb = [t[i] for t in ta], [t[i] for t in tb]
        row = {"code": sec["code"], "name": names.get(sec["code"], sec["code"]), "n_a": len(xa), "n_b": len(xb),
               "a_median_s": round(statistics.median(xa), 3) if xa else None,
               "b_median_s": round(statistics.median(xb), 3) if xb else None,
               "delta_s": None, "noise_s": None, "verdict": "few"}
        if xa and xb:
            row["delta_s"] = round(row["b_median_s"] - row["a_median_s"], 3)
        if len(xa) >= MIN_LAPS and len(xb) >= MIN_LAPS:
            thr = max(NOISE_K * math.sqrt(statistics.stdev(xa) ** 2 / len(xa) + statistics.stdev(xb) ** 2 / len(xb)),
                      MIN_NOISE_S)
            row["noise_s"] = round(thr, 3)
            d = row["b_median_s"] - row["a_median_s"]
            row["verdict"] = "noise" if abs(d) < thr else ("faster" if d < 0 else "slower")
        rows.append(row)
    return {"laps": {"a": len(ta), "b": len(tb)}, "sectors": rows}


def compare_setups(store, a_id: int, b_id: int, track_id: int | None = None) -> dict:
    """版本 A（参考）对比版本 B。delta_median_s = B 中位数 − A 中位数（负 = B 快）。
    a_id / b_id 可以是 UNTAGGED_ID（0）= 这台车没记版本的圈（另一边必须是真实版本，用它确定是哪台车）。

    verdict："faster" = B 比 A 快、"slower" = B 比 A 慢、"noise" = 差别在噪音内（或圈数不够，enough_laps=false）。
    sectors：按赛道固定弯道表逐段比（见 _sector_compare），每段 verdict 多一种 "few" = 某边不到 MIN_LAPS 圈。
    """
    ensure_schema(store)
    if a_id == b_id:
        raise SetupError("选两个不同的版本来比")
    sa = _row(store, a_id) if a_id != UNTAGGED_ID else None
    sb = _row(store, b_id) if b_id != UNTAGGED_ID else None
    if a_id == UNTAGGED_ID and sb is not None:
        sa = _untagged(sb)
    if b_id == UNTAGGED_ID and sa is not None:
        sb = _untagged(sa)
    if sa is None or sb is None:
        raise SetupError(f"调校版本 #{a_id if sa is None else b_id} 不存在")
    if (sa["game"], sa["car_ordinal"]) != (sb["game"], sb["car_ordinal"]):
        raise SetupError("两个版本不是同一台车，没法比")
    car_laps = _car_laps(store, sa["game"], sa["car_ordinal"])
    laps_a = [l for l in car_laps if (l.get("setup_id") or UNTAGGED_ID) == a_id]
    laps_b = [l for l in car_laps if (l.get("setup_id") or UNTAGGED_ID) == b_id]
    tid = _pick_track(laps_a, laps_b, track_id)
    A, B = _side(sa, laps_a, tid), _side(sb, laps_b, tid)
    track_name = next((l.get("track_name") for l in laps_a + laps_b if l.get("track_id") == tid), None)
    out = {"a": A, "b": B, "track_id": tid, "track_name": track_name,
           "delta_median_s": None, "noise_threshold_s": None, "verdict": "noise", "enough_laps": False,
           "min_laps": MIN_LAPS, "message": "", "notes": [], "findings_by_id": [],
           "diag_laps": {"a": 0, "b": 0}, "tip": ONE_CHANGE_TIP,
           "method": (f"只用完整飞驰圈（不含站立起步的第一圈、倒过带的圈），比这组中位数慢 {OUTLIER_FRAC:.0%} 以上的失误圈不计。"
                      f"噪音阈值 = {NOISE_K:g}·√(s₁²/n₁ + s₂²/n₂)（两组圈速的标准误），|Δ中位数| 小于它就算在噪音内。")}
    if tid is None:
        out["message"] = "两个版本还没在同一条赛道上跑过完整飞驰圈，没法比。"
    elif A["n"] < MIN_LAPS or B["n"] < MIN_LAPS:
        out["message"] = (f"圈数太少（A {A['n']} 圈、B {B['n']} 圈完整飞驰圈），每个版本至少 {MIN_LAPS} 圈才能判断，"
                          "再跑几圈。")
        if A["median_time"] is not None and B["median_time"] is not None:
            out["delta_median_s"] = round(B["median_time"] - A["median_time"], 3)
    else:
        delta = B["median_time"] - A["median_time"]
        thr = max(NOISE_K * math.sqrt(A["std_time"] ** 2 / A["n"] + B["std_time"] ** 2 / B["n"]), MIN_NOISE_S)
        out.update(delta_median_s=round(delta, 3), noise_threshold_s=round(thr, 3), enough_laps=True)
        if abs(delta) < thr:
            out["verdict"] = "noise"
            out["message"] = (f"在噪音内：中位数差 {abs(delta):.3f} s，小于 ±{thr:.3f} s（两组圈速波动算出来的范围）。"
                              "再多跑几圈，或者就当两套差不多。")
        else:
            out["verdict"] = "faster" if delta < 0 else "slower"
            out["message"] = (f"“{sb['name']}”比“{sa['name']}”{'快' if delta < 0 else '慢'} {abs(delta):.3f} s（中位数），"
                              f"超出噪音范围 ±{thr:.3f} s。")
    # 车辆字段：两组圈的 PI 等不一样 → 圈速差别不只是调校造成的
    for key, cn in CAR_FIELDS:
        va, vb = A["car_values"].get(key) or [], B["car_values"].get(key) or []
        if va and vb and set(va) != set(vb):
            out["notes"].append(f"两个版本的{cn}不一样（{' / '.join(_num(v) for v in va)} → {' / '.join(_num(v) for v in vb)}）："
                                "像是换了改装，圈速差别不只是调校造成的。")
        for side, vals in (("A", va), ("B", vb)):
            if len(vals) > 1:
                out["notes"].append(f"版本 {side} 的圈里出现了不同的{cn}（{' / '.join(_num(v) for v in vals)}）：像是中途换了改装。")
    # 逐段对比：哪个弯快了、哪个弯慢了（赛道固定弯道表的分段，首尾相接、加起来就是整圈）
    out["sectors"] = _sector_compare(store, tid, A, B, laps_a + laps_b) if tid is not None else None
    # 诊断对比：每边最近 MAX_DIAG_LAPS 圈（失误圈不算）
    if tid is not None:
        ids_a = [l["id"] for l in A["laps"] if not l["outlier"]][-MAX_DIAG_LAPS:]
        ids_b = [l["id"] for l in B["laps"] if not l["outlier"]][-MAX_DIAG_LAPS:]
        fa, na = _findings_count(store, ids_a)
        fb, nb = _findings_count(store, ids_b)
        out["diag_laps"] = {"a": na, "b": nb}
        rows = []
        for fid in list(dict.fromkeys(list(fa) + list(fb))):
            ea, eb = fa.get(fid), fb.get(fid)
            base = ea or eb
            ra = (ea["laps_with"] / na) if ea and na else 0.0
            rb = (eb["laps_with"] / nb) if eb and nb else 0.0
            rows.append({
                "id": fid, "title": (eb or ea)["title"], "category_label": base.get("category_label"),
                "severity": "alert" if "alert" in ((ea or {}).get("severity"), (eb or {}).get("severity")) else base.get("severity"),
                "a": {"laps_with": ea["laps_with"] if ea else 0, "total_s": ea["total_s"] if ea else None},
                "b": {"laps_with": eb["laps_with"] if eb else 0, "total_s": eb["total_s"] if eb else None},
                "change": "fewer" if rb < ra - 1e-9 else "more" if rb > ra + 1e-9 else "same",
                "_rank": (-max(ra, rb), -abs(rb - ra)),
            })
        rows.sort(key=lambda r: r["_rank"])
        for r in rows:
            r.pop("_rank")
        out["findings_by_id"] = rows
    for side in (A, B):
        side.pop("car_values", None)
    return out
