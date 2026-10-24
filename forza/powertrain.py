"""动力总成：实测功率曲线、反推各挡综合传动比、最佳升挡转速、实际换挡习惯、改终传预测。

全部从已存的圈里算（Data Out 没有齿比 / 终传数值）：
- 功率曲线：全油门（throttle ≥ 0.98）、离合 0、驱动轮 |slip_ratio| 小、power > 0 的采样，按 100 rpm 分箱，
  每圈每箱取 90 分位（上包络，去掉偶发尖刺），多圈再取最大（涡轮车有迟滞，多圈上包络更接近真实曲线）。
  按 (game, car_ordinal, car_pi) 存——换发动机 / 升级零件会改 PI；
- 综合传动比 R = rpm·2π/60 ÷ 驱动轮平均 wheel_speed（= 挡位齿比 × 终传）。离合接合时发动机和驱动轮是刚性连接的，
  打滑不影响这个比值。按 (game, car_ordinal) 缓存，**每圈重算**，和缓存差 > 2% 就更新——改齿比 / 终传属于调校，
  不会改 PI，按 PI 缓存会在调齿比之后过期，而那正是最常用的场景。几个挡同比例变了 = 改了终传，
  这圈没用到的挡按同一比例推算（标 scaled）；变得不一致时，没用到的挡标 stale（可能过期）；
- 最佳升挡转速：g 挡转速 x 时升到 g+1 挡，转速变成 x·R(g+1)/R(g)。同车速下轮上驱动力 ∝ 发动机功率，
  所以本挡功率 P(x) 第一次低于升挡后的功率 P(x·R(g+1)/R(g)) 的那个 x 就是最佳升挡点；曲线一直不交叉就在红线换。
  只在实测功率覆盖到的转速范围内判断，不外推；最高挡、没用到的挡算不出；
- 实际升挡：每次 g → g+1 且升挡前在踩油门，记升挡前的最高转速和升挡后 0.4 s 内掉到的最低转速；
- 改终传预测：用户手填当前终传和想改的终传（Data Out 没有终传值），按比例换算各挡红线车速，
  和这条赛道上实际跑到的最高车速比，判断直道末端会不会撞断油。

表（老库第一次用时自动建）：
    powertrain_laps    每圈的提取结果（JSON），lap_id 主键
    powertrain_ratios  每台车每挡的综合传动比缓存

对外函数：
    analyze_lap(lap) -> dict | None              单圈提取（纯计算）
    record_lap(store, lap)                       存圈后调用（LapSaver 已接好）：提取、入库、更新传动比缓存
    backfill(store, game, car_ordinal, limit)    给还没提取过的老圈补算
    summary(store, game, car_ordinal, car_pi=None) -> dict | None   C3 接口的内容；什么数据都没有返回 None
    predict_final_drive(store, game, car_ordinal, final_now, final_new, car_pi=None, track_id=None) -> dict
    shift_points(curve, ratios, redline) -> (shift_rpm, notes)
"""
from __future__ import annotations

import json
import logging
import math
import statistics

import numpy as np

from .lapdata import LapData
from .util import now_iso

log = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS powertrain_laps (
    lap_id         INTEGER PRIMARY KEY,
    game           TEXT NOT NULL,
    car_ordinal    INTEGER NOT NULL,
    car_pi         INTEGER,
    track_id       INTEGER,
    drivetrain     INTEGER,
    engine_max_rpm REAL,
    engine_idle_rpm REAL,
    created_at     TEXT,
    data           TEXT
);
CREATE INDEX IF NOT EXISTS idx_pt_laps_car ON powertrain_laps(game, car_ordinal, car_pi);
CREATE TABLE IF NOT EXISTS powertrain_ratios (
    game        TEXT NOT NULL,
    car_ordinal INTEGER NOT NULL,
    gear        INTEGER NOT NULL,
    ratio       REAL NOT NULL,
    samples     INTEGER,
    lap_id      INTEGER,
    scaled      INTEGER DEFAULT 0,   -- 1 = 这挡最近没开到，按终传变化比例推算的
    stale       INTEGER DEFAULT 0,   -- 1 = 别的挡变了、这挡没开到，可能已经过期
    updated_at  TEXT,
    PRIMARY KEY (game, car_ordinal, gear)
);
"""

# ---- 采样筛选 ----
FULL_THROTTLE = 0.98          # 油门 ≥ 这个算全油门
CLUTCH_MAX = 0.02             # 离合 ≤ 这个算接合（离合 0）
POWER_SLIP_MAX = 0.6          # 功率曲线：驱动轮 |slip_ratio| ≤ 这个（归一化，>1 = 失去抓地；牵引力控制介入时功率会被砍）
RPM_BIN = 100                 # 功率分箱宽度 rpm
BIN_MIN_SAMPLES = 3           # 每箱至少这么多个采样
BIN_PCT = 90.0                # 每圈每箱取这个分位做上包络
RATIO_MIN_SPEED = 8.0         # 反推传动比：车速 ≥ 8 m/s
RATIO_MIN_SAMPLES = 20        # 每挡至少这么多个采样（60 Hz 约 0.3 s）
RATIO_STABLE_N = 3            # 前后各这么多个采样挡位都一样（排除换挡瞬间）
RATIO_MAX_SPREAD = 0.03       # 每挡比值的四分位距 / 中位数 超过 3% 就不用（数据乱）
RATIO_LOCK_SLIP = -0.9        # 驱动轮滑移率低于这个（快抱死了）的采样不用
RATIO_UPDATE = 0.02           # 和缓存差 > 2% 才更新
SCALE_AGREE = 0.005           # 几个挡的变化比例相差 < 0.5% 算“同比例”（= 改了终传）
RPM_LIMIT_FRAC = 0.985        # 反推传动比时排除顶到断油的采样
UPSHIFT_LOOKBACK_S = 0.25     # 升挡前转速：升挡前这段时间里的最高转速
UPSHIFT_DROP_S = 0.40         # 升挡后转速：升挡后这段时间里的最低转速（升挡后马上松油 / 刹车的部分不算）
UPSHIFT_MIN_THROTTLE = 0.80   # 升挡前平均油门 ≥ 这个才算加速升挡
UPSHIFT_GAP_S = 0.5           # g → (空挡) → g+1 中间允许的空挡时长
WHEEL_R_MAX_SLIP = 0.05       # 估车轮半径：|slip_ratio| ≤ 这个的轮子
LIMITER_FRAC = 0.98           # 撞断油：转速 ≥ 最高转速 × 98%
# ---- 汇总 ----
POWER_LAPS = 40               # 功率曲线最多用最近这么多圈
ACTUAL_LAPS = 10              # 实际升挡转速用最近这么多圈
INTERP_MAX_GAP = 300          # 功率曲线分箱之间缺口 ≤ 这么多 rpm 才插值
REDLINE_COVER = 300           # 功率数据覆盖到离红线这么近，才敢说“不交叉 = 在红线换”
REDLINE_SHIFT_FRAC = LIMITER_FRAC   # “在红线换”的换挡点 = 最高转速 × 98%：断油器在最高转速以下来回跳，
                                    # 正好等于最高转速的转速几乎到不了；交点高于这个的也按红线换（98% 也是“撞断油”的判据）
SHIFT_SCAN_STEP = 10          # 找交点的转速步长
BACKFILL_LAPS = 40            # 第一次查某台车时，最多给最近这么多圈补算

DRIVEN = {0: ("fl", "fr"), 1: ("rl", "rr"), 2: ("fl", "fr", "rl", "rr")}
UNDRIVEN = {0: ("rl", "rr"), 1: ("fl", "fr"), 2: ("fl", "fr", "rl", "rr")}



def ensure_schema(store) -> None:
    if getattr(store, "_powertrain_ready", False):
        return
    store.executescript(SCHEMA)
    store._powertrain_ready = True


# ================================================================ 单圈提取

def _arr(lap: LapData, name: str) -> np.ndarray | None:
    a = lap.ch.get(name)
    return None if a is None else np.asarray(a, dtype=float)


def _stable(gear: np.ndarray, k: int) -> np.ndarray:
    """前后各 k 个采样挡位都和自己一样。"""
    n = len(gear)
    ok = np.ones(n, dtype=bool)
    for s in range(1, k + 1):
        ok[:s] = False
        ok[n - s:] = False
        ok[s:] &= gear[s:] == gear[:-s]
        ok[:-s] &= gear[:-s] == gear[s:]
    return ok


def analyze_lap(lap: LapData) -> dict | None:
    """从一圈里提取功率分箱、各挡综合传动比、升挡记录、车轮半径、最高车速。数据不够返回 None。

    返回：
        bins     {"7000": [功率 W, 扭矩 N·m, 采样数]}（键 = 分箱下沿 rpm）
        ratios   {"3": [综合传动比, 采样数]}
        upshifts [[g, 升挡前 rpm, 升挡后 rpm, 车速 m/s, 圈内距离 m]]
        wheel_radius_m, vmax, vmax_gear, vmax_rpm, vmax_dist_m, top_gear_used, limiter_s, gears_used, turbo
    """
    need = ("t", "rpm", "gear", "throttle", "speed")
    if len(lap) < 60 or any(not lap.has(k) for k in need):
        return None
    m = lap.meta or {}
    t = _arr(lap, "t")
    rpm = _arr(lap, "rpm")
    gear = np.round(_arr(lap, "gear")).astype(int)
    thr = _arr(lap, "throttle")
    spd = _arr(lap, "speed")
    clutch = _arr(lap, "clutch") if lap.has("clutch") else np.zeros(len(t))
    brake = _arr(lap, "brake")
    dist = _arr(lap, "dist") if lap.has("dist") else np.zeros(len(t))
    dtv = np.diff(t)
    dt = float(np.median(dtv[dtv > 0])) if np.any(dtv > 0) else 1 / 60
    dtrain = m.get("drivetrain")
    driven = DRIVEN.get(dtrain, DRIVEN[2])
    idle = float(m.get("engine_idle_rpm") or 0.0)
    redline = float(m.get("engine_max_rpm") or 0.0) or float(np.nanmax(rpm)) if len(rpm) else 0.0

    ws = [_arr(lap, f"wheel_speed_{w}") for w in driven]
    sr = [_arr(lap, f"slip_ratio_{w}") for w in driven]
    have_ws = all(a is not None for a in ws)
    have_sr = all(a is not None for a in sr)
    ws_mean = np.mean(np.abs(np.stack(ws)), axis=0) if have_ws else None
    slip_abs = np.max(np.abs(np.stack(sr)), axis=0) if have_sr else np.zeros(len(t))
    slip_min = np.min(np.stack(sr), axis=0) if have_sr else np.zeros(len(t))
    engaged = (clutch <= CLUTCH_MAX) & (gear >= 1)

    out: dict = {"bins": {}, "ratios": {}, "upshifts": [], "wheel_radius_m": None}

    # ---- 功率分箱（上包络）----
    power = _arr(lap, "power")
    torque = _arr(lap, "torque")
    if power is not None:
        if torque is None:
            with np.errstate(divide="ignore", invalid="ignore"):
                torque = np.where(rpm > 0, power / (rpm * 2 * math.pi / 60.0), 0.0)
        sel = engaged & (thr >= FULL_THROTTLE) & (power > 0) & (slip_abs <= POWER_SLIP_MAX) & (rpm > max(idle, 500.0))
        sel &= np.isfinite(power) & np.isfinite(rpm)
        if sel.any():
            b = (rpm[sel] // RPM_BIN).astype(int) * RPM_BIN
            pw, tq = power[sel], torque[sel]
            for k in np.unique(b):
                mk = b == k
                if mk.sum() >= BIN_MIN_SAMPLES:
                    out["bins"][str(int(k))] = [round(float(np.percentile(pw[mk], BIN_PCT)), 1),
                                                round(float(np.nan_to_num(np.percentile(tq[mk], BIN_PCT))), 2), int(mk.sum())]

    # ---- 各挡综合传动比 ----
    if ws_mean is not None:
        lim = redline * RPM_LIMIT_FRAC if redline > 0 else np.inf
        sel = (engaged & _stable(gear, RATIO_STABLE_N) & (spd >= RATIO_MIN_SPEED) & (ws_mean > 5.0)
               & (rpm > max(idle * 1.1, 500.0)) & (rpm < lim) & (slip_min > RATIO_LOCK_SLIP))
        with np.errstate(divide="ignore", invalid="ignore"):
            ratio = rpm * 2 * math.pi / 60.0 / ws_mean
        for g in np.unique(gear[sel]):
            r = ratio[sel & (gear == g)]
            r = r[np.isfinite(r)]
            if len(r) < RATIO_MIN_SAMPLES:
                continue
            med = float(np.median(r))
            q1, q3 = np.percentile(r, [25, 75])
            if med <= 0 or (q3 - q1) / med > RATIO_MAX_SPREAD:
                continue
            out["ratios"][str(int(g))] = [round(med, 4), int(len(r))]

    # ---- 车轮有效半径（不驱动的轮子不打滑时：车速 / 轮速）----
    rw = UNDRIVEN.get(dtrain, UNDRIVEN[2])
    wsu = [_arr(lap, f"wheel_speed_{w}") for w in rw]
    sru = [_arr(lap, f"slip_ratio_{w}") for w in rw]
    if all(a is not None for a in wsu) and all(a is not None for a in sru):
        wv = np.mean(np.abs(np.stack(wsu)), axis=0)
        ok = (spd > 10.0) & (wv > 5.0) & (np.max(np.abs(np.stack(sru)), axis=0) <= WHEEL_R_MAX_SLIP)
        if ok.sum() >= 30:
            out["wheel_radius_m"] = round(float(np.median(spd[ok] / wv[ok])), 4)

    # ---- 升挡 ----
    n = len(t)
    nb = max(1, int(round(UPSHIFT_LOOKBACK_S / dt)))
    na = max(1, int(round(UPSHIFT_DROP_S / dt)))
    ngap = max(1, int(round(UPSHIFT_GAP_S / dt)))
    i = 0
    while i < n - 1:
        g = gear[i]
        if g >= 1 and gear[i + 1] != g:
            j = i + 1
            while j < n and gear[j] == 0 and j - i <= ngap:      # 中间可能有几帧空挡
                j += 1
            if j < n and gear[j] == g + 1:
                lo = max(0, i - nb + 1)
                before = slice(lo, i + 1)
                if float(np.mean(thr[before])) >= UPSHIFT_MIN_THROTTLE:
                    after = np.arange(j, min(n, j + na))
                    after = after[gear[after] == g + 1]
                    # 升挡后马上松油 / 踩刹车的那部分不算（那是减速，不是换挡掉转速）
                    if brake is not None and len(after):
                        stop = np.nonzero((brake[after] > 0.05) | (thr[after] < 0.5))[0]
                        if len(stop):
                            after = after[:max(1, int(stop[0]))]
                    if len(after):
                        out["upshifts"].append([int(g), round(float(np.max(rpm[before])), 0),
                                                round(float(np.min(rpm[after])), 0), round(float(spd[i]), 2),
                                                round(float(dist[i]), 1)])
            i = j
            continue
        i += 1

    # ---- 最高车速 / 撞断油 ----
    k = int(np.argmax(spd))
    used = gear[gear >= 1]
    top = int(used.max()) if len(used) else None
    out.update({
        "vmax": round(float(spd[k]), 2), "vmax_gear": int(gear[k]), "vmax_rpm": round(float(rpm[k]), 0),
        "vmax_dist_m": round(float(dist[k]), 1), "top_gear_used": top,
        "gears_used": sorted({int(x) for x in np.unique(used)}),
        "limiter_s": round(float(np.sum((gear == top) & (rpm >= LIMITER_FRAC * redline)) * dt), 2)
        if top and redline > 0 else 0.0,
        "max_rpm_seen": round(float(np.max(rpm)), 0),
        "turbo": bool(lap.has("boost") and np.nanmax(np.asarray(lap["boost"], float)) > 1.0),
    })
    if not out["bins"] and not out["ratios"] and not out["upshifts"]:
        return None
    return out


# ================================================================ 入库 / 传动比缓存

def _cached_ratios(store, game: str, car_ordinal: int) -> dict[int, dict]:
    rows = store.query("SELECT * FROM powertrain_ratios WHERE game=? AND car_ordinal=? ORDER BY gear",
                       (game, car_ordinal))
    return {int(r["gear"]): r for r in rows}


def update_ratios(store, game: str, car_ordinal: int, lap_id: int | None, observed: dict) -> dict:
    """用这圈反推出的传动比更新缓存：和缓存差 > 2% 才更新（每圈都比一次）。

    observed：{"3": [ratio, samples]}。返回 {"updated": [挡位], "scaled": [挡位], "stale": [挡位], "factor": 终传变化比例}
    """
    ensure_schema(store)
    cache = _cached_ratios(store, game, car_ordinal)
    obs = {int(g): (float(v[0]), int(v[1])) for g, v in (observed or {}).items()}
    now = now_iso()
    changed: dict[int, float] = {}
    for g, (r, nsamp) in obs.items():
        old = cache.get(g)
        if old is None or abs(r - old["ratio"]) / old["ratio"] > RATIO_UPDATE:
            if old is not None:
                changed[g] = r / old["ratio"]
            store.execute(
                "INSERT INTO powertrain_ratios (game, car_ordinal, gear, ratio, samples, lap_id, scaled, stale, updated_at) "
                "VALUES (?,?,?,?,?,?,0,0,?) ON CONFLICT(game, car_ordinal, gear) DO UPDATE SET "
                "ratio=excluded.ratio, samples=excluded.samples, lap_id=excluded.lap_id, scaled=0, stale=0, "
                "updated_at=excluded.updated_at", (game, car_ordinal, g, r, nsamp, lap_id, now))
        elif old["scaled"] or old["stale"]:
            # 这挡这次开到了、和缓存一致：去掉“推算 / 可能过期”的标记
            store.execute("UPDATE powertrain_ratios SET scaled=0, stale=0, lap_id=?, updated_at=? "
                          "WHERE game=? AND car_ordinal=? AND gear=?", (lap_id, now, game, car_ordinal, g))
    res = {"updated": sorted(g for g in obs if g not in cache or g in changed), "scaled": [], "stale": [],
           "factor": None}
    if not changed:
        return res
    others = [g for g in cache if g not in obs]
    fs = list(changed.values())
    f = statistics.median(fs)
    unchanged_obs = [g for g in obs if g in cache and g not in changed]
    if len(fs) >= 2 and max(abs(x - f) for x in fs) / f < SCALE_AGREE and not unchanged_obs:
        # 开到的几个挡同比例变了 = 改了终传：没开到的挡按同一比例推算
        res["factor"] = round(f, 4)
        for g in others:
            store.execute("UPDATE powertrain_ratios SET ratio=?, scaled=1, stale=0, lap_id=?, updated_at=? "
                          "WHERE game=? AND car_ordinal=? AND gear=?",
                          (cache[g]["ratio"] * f, lap_id, now, game, car_ordinal, g))
        res["scaled"] = sorted(others)
    else:
        for g in others:
            store.execute("UPDATE powertrain_ratios SET stale=1 WHERE game=? AND car_ordinal=? AND gear=?",
                          (game, car_ordinal, g))
        res["stale"] = sorted(others)
    return res


def record_lap(store, lap: LapData) -> dict | None:
    """存圈之后调用：提取这圈的动力总成数据、入库、更新传动比缓存。返回提取结果（数据不够时 None）。"""
    ensure_schema(store)
    m = lap.meta or {}
    lap_id = m.get("id")
    if lap_id is None or not m.get("game") or m.get("car_ordinal") is None:
        return None
    a = analyze_lap(lap)
    if a is None:
        return None
    store.execute(
        "INSERT OR REPLACE INTO powertrain_laps (lap_id, game, car_ordinal, car_pi, track_id, drivetrain, "
        "engine_max_rpm, engine_idle_rpm, created_at, data) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (lap_id, m["game"], m["car_ordinal"], m.get("car_pi"), m.get("track_id"), m.get("drivetrain"),
         m.get("engine_max_rpm"), m.get("engine_idle_rpm"), m.get("created_at") or now_iso(),
         json.dumps(a, ensure_ascii=False)))
    if a["ratios"]:
        a["ratio_update"] = update_ratios(store, m["game"], m["car_ordinal"], lap_id, a["ratios"])
    return a


def backfill(store, game: str, car_ordinal: int, limit: int = BACKFILL_LAPS) -> int:
    """给这台车还没提取过的老圈（最近 limit 圈）补算，按时间先后处理（传动比缓存的更新顺序才对）。返回补了几圈。"""
    ensure_schema(store)
    done = {r["lap_id"] for r in store.query("SELECT lap_id FROM powertrain_laps WHERE game=? AND car_ordinal=?",
                                             (game, car_ordinal))}
    tried = store.__dict__.setdefault("_powertrain_tried", set())     # 提取不出东西的圈：本进程里别反复解压
    metas = store.list_laps(game=game, car_ordinal=car_ordinal, limit=limit)
    todo = sorted((m["id"] for m in metas if m["id"] not in done and m["id"] not in tried))
    n = 0
    for lid in todo:
        tried.add(lid)
        lap = store.get_lap(lid)
        if lap is None:
            continue
        try:
            if record_lap(store, lap) is not None:
                n += 1
        except Exception:
            log.exception("补算动力总成数据出错（圈 #%s）", lid)
    return n


def rebuild(store, game: str, car_ordinal: int, limit: int = BACKFILL_LAPS) -> int:
    """清掉这台车的动力总成数据重新算（例如改了这个文件里的阈值之后）。"""
    ensure_schema(store)
    store.execute("DELETE FROM powertrain_laps WHERE game=? AND car_ordinal=?", (game, car_ordinal))
    store.execute("DELETE FROM powertrain_ratios WHERE game=? AND car_ordinal=?", (game, car_ordinal))
    store.__dict__.setdefault("_powertrain_tried", set()).clear()
    return backfill(store, game, car_ordinal, limit)


# ================================================================ 汇总

ROWS_LIMIT = 300              # 汇总时最多读这台车最近这么多圈的提取结果


def _rows(store, game: str, car_ordinal: int) -> list[dict]:
    rows = store.query("SELECT * FROM powertrain_laps WHERE game=? AND car_ordinal=? ORDER BY lap_id DESC LIMIT ?",
                       (game, car_ordinal, ROWS_LIMIT))
    for r in rows:
        r["data"] = json.loads(r["data"] or "{}")
    return rows


def power_curve(rows: list[dict]) -> list[dict]:
    """多圈上包络：每个 100 rpm 箱取各圈 90 分位里的最大值。返回 [{rpm(箱中心), power_kw, torque, laps}]。"""
    best: dict[int, list] = {}
    for r in rows:
        for k, (p, tq, _n) in (r["data"].get("bins") or {}).items():
            k = int(k)
            e = best.setdefault(k, [p, tq, 0])
            e[0], e[1] = max(e[0], p), max(e[1], tq)
            e[2] += 1
    return [{"rpm": k + RPM_BIN // 2, "power_kw": round(v[0] / 1000.0, 2), "torque": round(v[1], 1), "laps": v[2]}
            for k, v in sorted(best.items())]


def _segments(curve: list[dict]) -> list[tuple[np.ndarray, np.ndarray]]:
    """功率曲线按缺口（> INTERP_MAX_GAP rpm）切成几段，每段内可以插值。"""
    segs, cur = [], []
    for p in curve:
        if cur and p["rpm"] - cur[-1]["rpm"] > INTERP_MAX_GAP:
            segs.append(cur)
            cur = []
        cur.append(p)
    if cur:
        segs.append(cur)
    return [(np.array([p["rpm"] for p in s], float), np.array([p["power_kw"] for p in s], float)) for s in segs]


def _p_at(segs, x: float) -> float | None:
    """转速 x 处的功率（只在有数据的段内插值，不外推；段两头放宽半个箱）。"""
    half = RPM_BIN / 2
    for xs, ys in segs:
        if xs[0] - half <= x <= xs[-1] + half:
            return float(np.interp(x, xs, ys))
    return None


def shift_points(curve: list[dict], ratios: dict[int, float], redline: float | None) -> tuple[dict, dict, list[str]]:
    """每挡最佳升挡转速。返回 (shift_rpm {"g": rpm}, how {"g": "cross"|"redline"}, notes)。

    g 挡转速 x 升到 g+1 挡后转速 = x·k，k = R(g+1)/R(g)。同车速下轮上驱动力 ∝ 功率，
    所以从低往高扫，第一个 P(x) < P(x·k) 的点（线性插值到交点）就是升挡点；
    在有数据的范围里一直不交叉、且数据覆盖到红线附近 → 在红线换。只在实测覆盖的转速内判断，不外推。
    “在红线换”给的转速是 红线 × REDLINE_SHIFT_FRAC（断油器在红线以下来回跳，正好等于红线的转速到不了，
    换挡灯按 ≥ shift_rpm 闪红就永远不闪）；交点高于这个转速的同样夹到这里，shift_how 记成 redline。
    """
    notes: list[str] = []
    out: dict = {}
    how: dict = {}
    if not curve:
        return out, how, ["还没有全油门的功率数据，算不出换挡点"]
    segs = _segments(curve)
    lo_cov = min(xs[0] for xs, _ in segs) - RPM_BIN / 2
    top_cov = max(xs[-1] for xs, _ in segs) + RPM_BIN / 2
    red = float(redline or 0) or top_cov
    cap = int(round(red * REDLINE_SHIFT_FRAC))
    clamped: list[str] = []
    for g in sorted(ratios):
        if g + 1 not in ratios:
            continue
        k = ratios[g + 1] / ratios[g]
        if not 0.3 < k < 1.0:
            notes.append(f"{g} 挡和 {g + 1} 挡的传动比不合理（{ratios[g]:.3f} / {ratios[g + 1]:.3f}），跳过")
            continue
        pts = []
        for x in np.arange(math.floor(lo_cov / k / SHIFT_SCAN_STEP) * SHIFT_SCAN_STEP, red + 1e-6, SHIFT_SCAN_STEP):
            pa, pb = _p_at(segs, float(x)), _p_at(segs, float(x) * k)
            if pa is not None and pb is not None:
                pts.append((float(x), pa - pb))
        if not pts:
            notes.append(f"{g} 挡：实测功率没覆盖到需要的转速范围，算不出最佳升挡点（多跑几圈、多踩全油门）")
            continue
        found = None
        for (x0, d0), (x1, d1) in zip(pts, pts[1:]):
            if x1 - x0 > SHIFT_SCAN_STEP * 1.5:      # 中间缺数据，不跨缺口找交点
                continue
            if d0 >= 0 > d1:
                found = x0 + (x1 - x0) * d0 / (d0 - d1)
                break
        if found is not None and found < cap:
            out[str(g)], how[str(g)] = int(round(found)), "cross"
        elif found is not None:
            out[str(g)], how[str(g)] = cap, "redline"
            clamped.append(f"{g} 挡（交点 {int(round(found))}）")
        elif pts[-1][1] >= 0 and pts[-1][0] >= red - REDLINE_COVER:
            out[str(g)], how[str(g)] = cap, "redline"
        elif all(d < 0 for _, d in pts):
            notes.append(f"{g} 挡：在有数据的转速里升挡后都更有劲，最佳升挡点在 {int(pts[0][0])} rpm 以下，"
                         "低转速段数据不够，没法确定")
        else:
            notes.append(f"{g} 挡：实测功率没覆盖到红线附近（只到 {int(pts[-1][0])} rpm），没法确定是不是该在红线换")
    if any(h == "redline" for h in how.values()):
        notes.append(f"标“红线”的挡位换挡点给的是 {cap} rpm（最高转速的 {REDLINE_SHIFT_FRAC:.0%}）：断油器在最高转速以下"
                     "来回跳，转速基本到不了最高转速，换挡灯要在撞断油之前亮"
                     + (f"；{'、'.join(clamped)}贴着断油，也按红线换" if clamped else ""))
    return out, how, notes


def _actual_upshifts(rows: list[dict]) -> tuple[dict, dict, dict]:
    before: dict[int, list] = {}
    after: dict[int, list] = {}
    for r in rows[:ACTUAL_LAPS]:
        for g, rb, ra, *_ in r["data"].get("upshifts") or []:
            before.setdefault(int(g), []).append(rb)
            after.setdefault(int(g), []).append(ra)
    mb = {str(g): int(round(statistics.fmean(v))) for g, v in sorted(before.items())}
    ma = {str(g): int(round(statistics.fmean(v))) for g, v in sorted(after.items())}
    cnt = {str(g): len(v) for g, v in sorted(before.items())}
    return mb, ma, cnt


def _redline(rows: list[dict]) -> float | None:
    for r in rows:
        if r.get("engine_max_rpm"):
            return float(r["engine_max_rpm"])
    seen = [r["data"].get("max_rpm_seen") for r in rows if r["data"].get("max_rpm_seen")]
    return float(max(seen)) if seen else None


def _wheel_radius(rows: list[dict]) -> float | None:
    v = [r["data"].get("wheel_radius_m") for r in rows[:POWER_LAPS] if r["data"].get("wheel_radius_m")]
    return round(float(statistics.median(v)), 4) if v else None


def summary(store, game: str, car_ordinal: int, car_pi: int | None = None, *, auto_backfill: bool = True) -> dict | None:
    """C3 接口的内容。这台车一圈数据都没有（补算后也没有）返回 None。"""
    ensure_schema(store)
    rows = _rows(store, game, car_ordinal)
    if not rows and auto_backfill:
        backfill(store, game, car_ordinal)
        rows = _rows(store, game, car_ordinal)
    if not rows:
        return None
    if car_pi is None:
        car_pi = rows[0]["car_pi"]
    pi_rows = [r for r in rows if r["car_pi"] == car_pi][:POWER_LAPS]
    cache = _cached_ratios(store, game, car_ordinal)
    if not pi_rows and not cache:
        return None
    notes: list[str] = []
    curve = power_curve(pi_rows)
    redline = _redline(pi_rows or rows)
    ratios = {g: float(r["ratio"]) for g, r in cache.items()}
    shift, how, n2 = shift_points(curve, ratios, redline)
    act_b, act_a, act_n = _actual_upshifts(pi_rows or rows)
    wr = _wheel_radius(pi_rows or rows)
    top_known = max(ratios) if ratios else None
    gear_rows = []
    for g in sorted(ratios):
        c = cache[g]
        v_red = (redline * 2 * math.pi / 60.0 / ratios[g] * wr * 3.6) if (redline and wr) else None
        kmh_1000 = (1000 * 2 * math.pi / 60.0 / ratios[g] * wr * 3.6) if wr else None
        gear_rows.append({
            "gear": g, "ratio": round(ratios[g], 4),
            "step_pct": round((ratios[g + 1] / ratios[g] - 1) * 100, 1) if g + 1 in ratios else None,
            "kmh_per_1000rpm": round(kmh_1000, 2) if kmh_1000 else None,
            "redline_kmh": round(v_red, 1) if v_red else None,
            "shift_rpm": shift.get(str(g)), "shift_how": how.get(str(g)),
            "after_shift_rpm": int(round(shift[str(g)] * ratios[g + 1] / ratios[g])) if str(g) in shift else None,
            "actual_upshift_rpm": act_b.get(str(g)), "actual_drop_rpm": act_a.get(str(g)),
            "upshift_count": act_n.get(str(g), 0),
            "scaled": bool(c["scaled"]), "stale": bool(c["stale"]), "lap_id": c["lap_id"],
        })
    if curve:
        lo, hi = curve[0]["rpm"] - RPM_BIN // 2, curve[-1]["rpm"] + RPM_BIN // 2
        notes.append(f"功率曲线来自 {len(pi_rows)} 圈全油门数据（PI {car_pi}），覆盖 {lo}–{hi} rpm；"
                     "只覆盖实际跑到的转速段")
        if any(r["data"].get("turbo") for r in pi_rows):
            notes.append("涡轮增压车：功率有迟滞，这里取的是多圈的上包络，圈数越多越准")
    elif pi_rows:
        notes.append(f"PI {car_pi} 还没有足够的全油门数据（油门 ≥ {FULL_THROTTLE:.0%}、离合 0、驱动轮不打滑），画不出功率曲线")
    else:
        notes.append(f"PI {car_pi} 还没有数据（换了改装？），换挡点要等这个 PI 跑几圈才有")
    if ratios:
        notes.append(f"传动比 = 挡位齿比 × 终传（Data Out 没有齿比数值，是用转速 ÷ 驱动轮轮速反推的），每圈重算，变了 > {RATIO_UPDATE:.0%} 就更新")
        missing = [g for g in range(1, top_known) if g not in ratios]
        if missing:
            notes.append(f"{'、'.join(map(str, missing))} 挡没开到（或数据不稳），传动比和换挡点算不出")
        notes.append(f"{top_known} 挡是开到过的最高挡：没有更高一挡，不给升挡点；车可能还有没用到的更高挡")
        if any(c["scaled"] for c in cache.values()):
            notes.append("标“推算”的挡位最近没开到：其他挡同比例变了（像是改了终传），按同一比例推算的")
        if any(c["stale"] for c in cache.values()):
            notes.append("标“可能过期”的挡位最近没开到，而其他挡的传动比变了，这挡的数值可能已经不对")
    notes.extend(n2)
    if redline:
        notes.append(f"红线 = 游戏给的最高转速 {int(redline)} rpm")
    return {
        "game": game, "car_ordinal": car_ordinal, "car_pi": car_pi,
        "source_laps": len(pi_rows), "total_laps": len(rows),
        "redline_rpm": int(round(redline)) if redline else None,
        "shift_rpm": shift, "shift_how": how,
        "ratios": {str(g): round(r, 4) for g, r in sorted(ratios.items())},
        "power_curve": [{"rpm": p["rpm"], "power_kw": p["power_kw"], "torque": p["torque"]} for p in curve],
        "peak_power": max(({"rpm": p["rpm"], "power_kw": p["power_kw"]} for p in curve), key=lambda p: p["power_kw"],
                          default=None),
        "peak_torque": max(({"rpm": p["rpm"], "torque": p["torque"]} for p in curve), key=lambda p: p["torque"],
                           default=None),
        "actual_upshift_rpm": act_b, "actual_upshift_drop_rpm": act_a, "upshift_counts": act_n,
        "wheel_radius_m": wr, "gears": gear_rows,
        "pi_list": sorted({r["car_pi"] for r in rows if r["car_pi"] is not None}),
        "drivetrain": next((r["drivetrain"] for r in rows if r["drivetrain"] is not None), None),
        "notes": notes,
    }


# ================================================================ 改终传预测

def predict_final_drive(store, game: str, car_ordinal: int, final_now: float, final_new: float,
                        car_pi: int | None = None, track_id: int | None = None) -> dict:
    """按比例换算改终传后的各挡红线车速，判断这条赛道上直道末端会不会撞断油。

    final_now / final_new 是用户手填的（Data Out 没有终传值）。track_id 默认 = 这台车最近跑的那条赛道。
    """
    if not (final_now and final_new) or final_now <= 0 or final_new <= 0:
        raise ValueError("当前终传和想改的终传都要填正数（游戏调校界面里“终传 / Final Drive”那一项）")
    s = summary(store, game, car_ordinal, car_pi)
    if s is None or not s["ratios"]:
        raise LookupError("这台车还没有反推出各挡传动比，先跑几圈")
    wr, red = s["wheel_radius_m"], s["redline_rpm"]
    rows = _rows(store, game, car_ordinal)
    if track_id is None:
        track_id = next((r["track_id"] for r in rows if r["track_id"] is not None), None)
    on_track = [r for r in rows if r["track_id"] == track_id]
    # 只看“现在这套齿比”跑的圈（这圈自己反推出的各挡传动比都和缓存差 ≤ 2%）；一圈都没有就退回最近几圈
    def same_gearing(r) -> bool:
        own = r["data"].get("ratios") or {}
        common = [g for g in own if g in s["ratios"]]
        return bool(common) and all(abs(own[g][0] / s["ratios"][g] - 1) <= RATIO_UPDATE for g in common)
    on_track = ([r for r in on_track if same_gearing(r)] or on_track)[:ACTUAL_LAPS]
    f = final_new / final_now
    gears = []
    for g, r in sorted(((int(k), v) for k, v in s["ratios"].items())):
        row = {"gear": g, "gear_ratio": round(r / final_now, 3), "ratio_now": round(r, 3), "ratio_new": round(r * f, 3)}
        if wr and red:
            row["redline_kmh_now"] = round(red * 2 * math.pi / 60 / r * wr * 3.6, 1)
            row["redline_kmh_new"] = round(red * 2 * math.pi / 60 / (r * f) * wr * 3.6, 1)
        gears.append(row)
    top = gears[-1]
    vmaxs = [r["data"].get("vmax") for r in on_track if r["data"].get("vmax")]
    lim = [r["data"].get("limiter_s") or 0.0 for r in on_track]
    out = {"final_now": final_now, "final_new": final_new, "change_pct": round((f - 1) * 100, 1),
           "track_id": track_id, "laps": len(on_track), "gears": gears, "top_gear": top["gear"],
           "vmax_kmh": round(max(vmaxs) * 3.6, 1) if vmaxs else None,
           "limiter_s_now": round(statistics.median(lim), 2) if lim else None,
           "verdict": None, "message": "", "notes": []}
    out["notes"].append("终传数字变大 = 齿比变短：每挡加速更猛、红线车速更低；变小反过来。")
    out["notes"].append("粗估：假设走线、换挡和最高车速不变；实际极速还受功率和风阻限制。")
    if not wr or not red or "redline_kmh_new" not in top:
        out["message"] = "没估出车轮半径或红线，只能给出传动比的变化"
        return out
    if out["vmax_kmh"] is None:
        out["message"] = "这条赛道还没有这台车的数据，没法判断会不会撞断油"
        return out
    v_new, v_now, vmax = top["redline_kmh_new"], top["redline_kmh_now"], out["vmax_kmh"]
    margin = v_new / vmax - 1
    if v_new <= vmax * LIMITER_FRAC:
        out["verdict"] = "limiter"
        out["message"] = (f"会撞断油：新终传下 {top['gear']} 挡红线车速 {v_new:.0f} km/h，"
                          f"比你在这条赛道跑到的最高车速 {vmax:.0f} km/h 还低。")
    elif margin < 0.03:
        out["verdict"] = "close"
        out["message"] = (f"很可能在直道末端撞断油：{top['gear']} 挡红线车速 {v_new:.0f} km/h，只比最高车速 {vmax:.0f} km/h 高 "
                          f"{margin:.1%}（车速一快就顶到了）。")
    else:
        out["verdict"] = "ok"
        was = (out["limiter_s_now"] or 0) > 0.3 or v_now <= vmax * 1.005
        extra = ""
        if margin > 0.15:
            extra = ("余量很大，最高挡可能用不满、加速变弱，可以考虑别改这么长。" if f < 1 else
                     "最高挡还是用不满，想要更猛的加速还可以再短一点。")
        out["message"] = (f"{'不会再撞断油' if was else '不会撞断油'}：{top['gear']} 挡红线车速 {v_new:.0f} km/h，"
                          f"比最高车速 {vmax:.0f} km/h 高 {margin:.1%}。" + extra)
    if (out["limiter_s_now"] or 0) > 0.3:
        out["notes"].append(f"现在每圈在 {top['gear']} 挡顶着断油约 {out['limiter_s_now']:.1f} s。")
    return out
