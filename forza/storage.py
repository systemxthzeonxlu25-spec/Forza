"""SQLite 存储：车辆、赛道、会话、每圈数据。

表结构：
    cars      (game, car_ordinal) 主键；name 可在网页上改
    tracks    赛道 / 冲刺路线。FM2023 用 TrackOrdinal 识别；其他游戏包里没有赛道 ID，
              用“起点坐标 + 圈长”做指纹识别，名字可在网页上改
    sessions  一次运行（serve / import）里同一辆车同一条赛道的一段连续驾驶
    laps      每圈一行，元数据是普通列，采样数据压缩后放 samples BLOB（见 lapdata.LapData.to_blob）
"""
from __future__ import annotations

import json
import math
import sqlite3
import statistics
import threading
from pathlib import Path

from .lapdata import LapData
from .util import now_iso, parse_flags

SCHEMA = """
CREATE TABLE IF NOT EXISTS cars (
    game          TEXT NOT NULL,
    car_ordinal   INTEGER NOT NULL,
    name          TEXT,
    car_class     INTEGER,
    car_pi        INTEGER,
    drivetrain    INTEGER,
    num_cylinders INTEGER,
    first_seen    TEXT,
    PRIMARY KEY (game, car_ordinal)
);
CREATE TABLE IF NOT EXISTS tracks (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    family        TEXT NOT NULL,          -- fm / fh
    game          TEXT,
    track_ordinal INTEGER,                -- 只有 FM2023 有
    kind          TEXT NOT NULL,          -- circuit / sprint
    name          TEXT,
    start_x       REAL,
    start_z       REAL,
    ref_length_m  REAL,                   -- 参考圈长 / 路线长度
    created_at    TEXT,
    provisional_length_m REAL             -- 只有起步圈时的暂定圈长（起步圈里最短的），有了 ref_length_m 就不用它
);
CREATE TABLE IF NOT EXISTS sessions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at  TEXT,
    game        TEXT,
    fmt         TEXT,
    source      TEXT,
    car_ordinal INTEGER,
    track_id    INTEGER,
    note        TEXT
);
CREATE TABLE IF NOT EXISTS laps (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id    INTEGER,
    track_id      INTEGER,
    game          TEXT,
    family        TEXT,
    fmt           TEXT,
    car_ordinal   INTEGER,
    car_class     INTEGER,
    car_pi        INTEGER,
    lap_number    INTEGER,
    kind          TEXT,                   -- lap / sprint
    lap_time      REAL,
    time_source   TEXT,                   -- game = 游戏给的 LastLap；measured = 自己量的
    distance_m    REAL,
    max_speed     REAL,
    is_complete   INTEGER,
    flags         TEXT,                   -- JSON 数组，如 ["first_lap", "rewind"]
    meta          TEXT,                   -- 其余元数据 JSON
    note          TEXT,
    sample_count  INTEGER,
    created_at    TEXT,
    samples       BLOB
);
CREATE INDEX IF NOT EXISTS idx_laps_track ON laps(track_id, car_ordinal, lap_time);
CREATE INDEX IF NOT EXISTS idx_laps_session ON laps(session_id);
"""

# laps 表里直接作为列存的元数据字段（其余进 meta JSON）
_LAP_COLUMNS = ["session_id", "track_id", "game", "family", "fmt", "car_ordinal", "car_class", "car_pi",
                "lap_number", "kind", "lap_time", "time_source", "distance_m", "max_speed", "is_complete"]

# 查圈元数据时用的 SELECT（不带 samples 大字段）
_META_SELECT = (
    "l.id, l.session_id, l.track_id, l.game, l.family, l.fmt, l.car_ordinal, l.car_class, "
    "l.car_pi, l.lap_number, l.kind, l.lap_time, l.time_source, l.distance_m, l.max_speed, "
    "l.is_complete, l.flags, l.meta, l.note, l.sample_count, l.created_at, "
    "t.name AS track_name, c.name AS car_name "
    "FROM laps l LEFT JOIN tracks t ON t.id = l.track_id "
    "LEFT JOIN cars c ON c.game = l.game AND c.car_ordinal = l.car_ordinal"
)

# 赛道指纹匹配阈值
TRACK_START_RADIUS_M = 60.0   # 起点坐标相距这么近算同一个起点
TRACK_LENGTH_TOL = 0.04       # 圈长差在 4% 以内算同一条赛道（区分共用起点线的不同布局）
TRACK_FIRST_LAP_MAX_EXTRA = 0.15   # 站立起步的第一圈最多比飞驰圈长这么多（发车格在起终点线后面，只会更长）
CIRCUIT_REF_RECENT = 20       # 赛道参考圈长取最近这么多圈飞驰圈的中位数



class Store:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        if str(path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        # 切圈存档在后台线程里做，所以允许跨线程，用锁串行化
        self._db = sqlite3.connect(str(path), check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        with self._lock:
            self._db.execute("PRAGMA journal_mode=WAL")
            self._db.executescript(SCHEMA)
            self._migrate()
            self._db.commit()

    def _migrate(self) -> None:
        """老库补列。tracks.provisional_length_m：只存过起步圈的赛道，用已存起步圈里最短的距离补上暂定圈长。"""
        cols = {r["name"] for r in self._db.execute("PRAGMA table_info(tracks)").fetchall()}
        if "provisional_length_m" not in cols:
            self._db.execute("ALTER TABLE tracks ADD COLUMN provisional_length_m REAL")
            self._db.execute(
                """UPDATE tracks SET provisional_length_m = (
                       SELECT MIN(l.distance_m) FROM laps l
                       WHERE l.track_id = tracks.id AND l.kind = 'lap' AND l.distance_m > 0)
                   WHERE kind = 'circuit' AND ref_length_m IS NULL AND track_ordinal IS NULL""")

    def close(self) -> None:
        self._db.close()

    # ---------------- 通用 SQL（新模块可以自己建表 / 查表，不用改这个文件） ----------------
    def query(self, sql: str, args=()) -> list[dict]:
        """执行查询，返回 dict 列表。"""
        return [dict(r) for r in self._q(sql, args)]

    def execute(self, sql: str, args=()) -> int:
        """执行写操作并提交，返回 lastrowid。"""
        return self._x(sql, args)

    def executescript(self, sql: str) -> None:
        """执行多条语句（建表用，CREATE TABLE IF NOT EXISTS 对老库也安全）。"""
        with self._lock:
            self._db.executescript(sql)
            self._db.commit()

    def _q(self, sql: str, args=()) -> list[sqlite3.Row]:
        with self._lock:
            return self._db.execute(sql, args).fetchall()

    def _x(self, sql: str, args=()) -> int:
        with self._lock:
            cur = self._db.execute(sql, args)
            self._db.commit()
            return cur.lastrowid

    # ---------------- 车辆 ----------------
    def upsert_car(self, game: str, car_ordinal: int, car_class: int, car_pi: int,
                   drivetrain: int, num_cylinders: int) -> None:
        self._x(
            """INSERT INTO cars (game, car_ordinal, car_class, car_pi, drivetrain, num_cylinders, first_seen)
               VALUES (?,?,?,?,?,?,?)
               ON CONFLICT(game, car_ordinal) DO UPDATE SET
                 car_class=excluded.car_class, car_pi=excluded.car_pi,
                 drivetrain=excluded.drivetrain, num_cylinders=excluded.num_cylinders""",
            (game, car_ordinal, car_class, car_pi, drivetrain, num_cylinders, now_iso()),
        )

    def set_car_name(self, game: str, car_ordinal: int, name: str) -> None:
        self._x("UPDATE cars SET name=? WHERE game=? AND car_ordinal=?", (name, game, car_ordinal))

    def list_cars(self) -> list[dict]:
        return [dict(r) for r in self._q("SELECT * FROM cars ORDER BY first_seen DESC")]

    # ---------------- 赛道 ----------------
    @staticmethod
    def _circuit_length_score(r, length_m: float, first_lap: bool) -> tuple[int, float] | None:
        """赛道赛按圈长判断候选赛道 r 像不像这一圈：像 → (优先级, 圈长相对差)，越小越像；不像 → None。

        - 有参考圈长 ref（飞驰圈定的）：飞驰圈要求差 ≤ TRACK_LENGTH_TOL；
          起步圈只会更长（发车格在线后），要求 ref×(1−TOL) ≤ 圈长 ≤ ref×(1+TRACK_FIRST_LAP_MAX_EXTRA)
        - 只有暂定圈长 p（只存过起步圈，p = 其中最短的）：飞驰圈要求 p×(1−EXTRA) ≤ 圈长 ≤ p×(1+TOL)；
          起步圈之间发车位置不同，要求 p÷(1+EXTRA) ≤ 圈长 ≤ p×(1+EXTRA)
        - 两个都没有（老库里圈都删了的赛道）：不看圈长，优先级最低
        """
        ref, prov = r["ref_length_m"], r["provisional_length_m"]
        tol, extra = TRACK_LENGTH_TOL, TRACK_FIRST_LAP_MAX_EXTRA
        if ref:
            ok = (ref * (1 - tol) <= length_m <= ref * (1 + extra)) if first_lap else abs(length_m - ref) <= ref * tol
            return (0, abs(length_m - ref) / ref) if ok else None
        if prov:
            ok = (prov / (1 + extra) <= length_m <= prov * (1 + extra)) if first_lap \
                else (prov * (1 - extra) <= length_m <= prov * (1 + tol))
            return (1, abs(length_m - prov) / prov) if ok else None
        return (2, 0.0)

    def find_track(self, *, family: str, kind: str, track_ordinal: int | None, start_x: float, start_z: float,
                   length_m: float | None, first_lap: bool = False) -> int | None:
        """按 find_or_create_track 的规则找已有赛道，找不到返回 None（只查不建）。"""
        with self._lock:
            if track_ordinal:
                rows = self._q("SELECT id FROM tracks WHERE family=? AND track_ordinal=? AND kind=? ORDER BY id",
                               (family, track_ordinal, kind))
                return rows[0]["id"] if rows else None
            rows = self._q("SELECT * FROM tracks WHERE family=? AND kind=? AND track_ordinal IS NULL ORDER BY id",
                           (family, kind))
            cands = [r for r in rows if r["start_x"] is not None and r["start_z"] is not None
                     and math.hypot(r["start_x"] - start_x, r["start_z"] - start_z) <= TRACK_START_RADIUS_M]
            if not cands:
                return None
            if kind != "circuit" or not length_m or length_m <= 0:
                return cands[0]["id"]
            scored = []
            for r in cands:
                sc = self._circuit_length_score(r, float(length_m), first_lap)
                if sc is not None:
                    # 起步圈取圈长最接近的；飞驰圈在同一优先级里取最早建的（和以前一样）
                    scored.append(((sc[0], sc[1] if first_lap else 0.0, r["id"]), r))
            return min(scored, key=lambda e: e[0])[1]["id"] if scored else None

    def find_or_create_track(self, *, family: str, game: str, kind: str, track_ordinal: int | None,
                             start_x: float, start_z: float, length_m: float | None,
                             first_lap: bool = False) -> int:
        """找到（或新建）这一圈所在的赛道，返回 track id。

        - 有 TrackOrdinal（FM2023）：按 ordinal 识别
        - 否则按起点坐标（相距 < 60 m）+ 圈长识别（规则见 _circuit_length_score）；冲刺赛只看起点
        - first_lap=True（站立起步的第一圈）：发车格在线后，这圈只会比飞驰圈长，最多长 TRACK_FIRST_LAP_MAX_EXTRA。
          新建赛道时不拿它当参考圈长（ref_length_m 先空着，等飞驰圈来了再定，见 refresh_circuit_length），
          只记成暂定圈长 provisional_length_m，免得别的布局的飞驰圈不看圈长就并进来
        """
        with self._lock:
            tid = self.find_track(family=family, kind=kind, track_ordinal=track_ordinal, start_x=start_x,
                                  start_z=start_z, length_m=length_m, first_lap=first_lap)
            provisional = kind == "circuit" and first_lap and not track_ordinal and bool(length_m and length_m > 0)
            if tid is not None:
                if provisional:
                    # 暂定圈长取起步圈里最短的（发车位置越靠前越接近真实圈长）
                    self._x("UPDATE tracks SET provisional_length_m = MIN(COALESCE(provisional_length_m, ?), ?) "
                            "WHERE id=? AND ref_length_m IS NULL", (length_m, length_m, tid))
                return tid
            n = self._q("SELECT COUNT(*) AS c FROM tracks WHERE kind=?", (kind,))[0]["c"] + 1
            default_name = f"{'赛道' if kind == 'circuit' else '冲刺路线'} {n}"
            if track_ordinal:
                default_name = f"赛道 #{track_ordinal}"
            ref = None if (kind == "circuit" and first_lap) else length_m
            return self._x(
                """INSERT INTO tracks (family, game, track_ordinal, kind, name, start_x, start_z, ref_length_m,
                                      provisional_length_m, created_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (family, game, track_ordinal, kind, default_name, start_x, start_z, ref,
                 length_m if provisional else None, now_iso()),
            )

    def update_track_length(self, track_id: int, length_m: float) -> None:
        """冲刺路线的参考长度：只增不减，取跑过的最长距离（赛道赛用 refresh_circuit_length）。"""
        self._x("UPDATE tracks SET ref_length_m = MAX(COALESCE(ref_length_m, 0), ?) WHERE id=?",
                (length_m, track_id))

    def set_track_length(self, track_id: int, length_m: float | None) -> None:
        """直接改参考圈长（可变长也可变短）。"""
        self._x("UPDATE tracks SET ref_length_m = ? WHERE id=?", (length_m, track_id))

    def refresh_circuit_length(self, track_id: int, recent: int = CIRCUIT_REF_RECENT, *,
                               clear_if_empty: bool = False) -> float | None:
        """赛道赛的参考圈长 = 最近 recent 圈完整飞驰圈（排除站立起步的第一圈）距离的中位数。

        返回新的参考圈长；没有飞驰圈时返回 None，参考圈长：
        - clear_if_empty=False（默认，存新圈时用）：不动。
        - clear_if_empty=True（删圈 / 标为不完整 / 倒带作废之后用）：清空。原来定参考圈长的那些圈已经不算数了，
          留着旧值会让后面的圈拿一个已经不存在的圈长去匹配赛道。清空后按暂定圈长（或不看圈长）匹配，
          等下一圈飞驰圈存进来再重新定。
        """
        # 先在 SQL 里排除起步圈再 LIMIT：要是先取最近 N 行再过滤，最近全是起步圈（反复单圈重开）时
        # 会一圈都剩不下，误以为没有飞驰圈（clear_if_empty 时还会把有效的参考圈长清掉）。
        # flags 存的是 JSON 数组文本（如 ["first_lap", "rewind"]），按带引号的整词匹配。
        rows = self._q("SELECT distance_m FROM laps WHERE track_id=? AND kind='lap' AND is_complete=1 "
                       "AND distance_m > 0 AND COALESCE(flags, '') NOT LIKE '%\"first_lap\"%' "
                       "ORDER BY id DESC LIMIT ?", (track_id, recent))
        dists = [r["distance_m"] for r in rows]
        if not dists:
            if clear_if_empty:
                self.set_track_length(track_id, None)
            return None
        ref = round(float(statistics.median(dists)), 1)
        self.set_track_length(track_id, ref)
        return ref

    def get_track(self, track_id: int) -> dict | None:
        rows = self._q("SELECT * FROM tracks WHERE id=?", (track_id,))
        return dict(rows[0]) if rows else None

    def set_track_name(self, track_id: int, name: str) -> None:
        self._x("UPDATE tracks SET name=? WHERE id=?", (name, track_id))

    def list_tracks(self) -> list[dict]:
        return [dict(r) for r in self._q(
            """SELECT t.*, COUNT(l.id) AS lap_count, MIN(CASE WHEN l.is_complete THEN l.lap_time END) AS best_time
               FROM tracks t LEFT JOIN laps l ON l.track_id = t.id
               GROUP BY t.id ORDER BY t.id DESC""")]

    # ---------------- 会话 ----------------
    def create_session(self, game: str, fmt: str, source: str, car_ordinal: int | None,
                       track_id: int | None) -> int:
        return self._x(
            "INSERT INTO sessions (started_at, game, fmt, source, car_ordinal, track_id) VALUES (?,?,?,?,?,?)",
            (now_iso(), game, fmt, source, car_ordinal, track_id),
        )

    def list_sessions(self, limit: int = 100) -> list[dict]:
        return [dict(r) for r in self._q("SELECT * FROM sessions ORDER BY id DESC LIMIT ?", (limit,))]

    # ---------------- 圈 ----------------
    def save_lap(self, lap: LapData) -> int:
        m = lap.meta
        cols = {k: m.get(k) for k in _LAP_COLUMNS}
        cols["is_complete"] = 1 if m.get("is_complete") else 0
        extra = {k: v for k, v in m.items() if k not in _LAP_COLUMNS and k not in ("id", "flags", "note")}
        lap_id = self._x(
            f"""INSERT INTO laps ({', '.join(_LAP_COLUMNS)}, flags, meta, note, sample_count, created_at, samples)
                VALUES ({', '.join('?' * len(_LAP_COLUMNS))}, ?, ?, ?, ?, ?, ?)""",
            (*cols.values(), json.dumps(m.get("flags", []), ensure_ascii=False),
             json.dumps(extra, ensure_ascii=False), m.get("note"), len(lap), m.get("created_at") or now_iso(),
             lap.to_blob()),
        )
        lap.meta["id"] = lap_id
        return lap_id

    @staticmethod
    def _row_meta(r: sqlite3.Row) -> dict:
        d = {k: r[k] for k in r.keys() if k not in ("samples", "meta", "flags")}
        d.update(json.loads(r["meta"] or "{}"))
        d["flags"] = parse_flags(r["flags"])
        d["is_complete"] = bool(r["is_complete"])
        return d

    def list_laps(self, *, track_id: int | None = None, car_ordinal: int | None = None,
                  game: str | None = None, session_id: int | None = None,
                  complete_only: bool = False, limit: int = 500) -> list[dict]:
        sql = f"SELECT {_META_SELECT} WHERE 1=1"
        args: list = []
        for col, val in (("l.track_id", track_id), ("l.car_ordinal", car_ordinal), ("l.game", game),
                         ("l.session_id", session_id)):
            if val is not None:
                sql += f" AND {col} = ?"
                args.append(val)
        if complete_only:
            sql += " AND l.is_complete = 1"
        sql += " ORDER BY l.id DESC LIMIT ?"
        args.append(limit)
        return [self._row_meta(r) for r in self._q(sql, args)]

    def get_lap_meta(self, lap_id: int) -> dict | None:
        rows = self.list_laps_by_ids([lap_id])
        return rows[0] if rows else None

    def list_laps_by_ids(self, ids: list[int]) -> list[dict]:
        if not ids:
            return []
        rows = self._q(f"SELECT {_META_SELECT} WHERE l.id IN ({','.join('?' * len(ids))})", ids)
        return [self._row_meta(r) for r in rows]

    def get_lap(self, lap_id: int) -> LapData | None:
        rows = self._q(
            """SELECT l.*, t.name AS track_name, c.name AS car_name FROM laps l
               LEFT JOIN tracks t ON t.id = l.track_id
               LEFT JOIN cars c ON c.game = l.game AND c.car_ordinal = l.car_ordinal
               WHERE l.id = ?""", (lap_id,))
        if not rows:
            return None
        return LapData.from_blob(self._row_meta(rows[0]), rows[0]["samples"])

    def best_lap(self, track_id: int, car_ordinal: int | None = None, game: str | None = None) -> dict | None:
        sql = "SELECT id FROM laps WHERE track_id=? AND is_complete=1"
        args: list = [track_id]
        if car_ordinal is not None:
            sql += " AND car_ordinal=?"
            args.append(car_ordinal)
        if game is not None:
            sql += " AND game=?"
            args.append(game)
        rows = self._q(sql + " ORDER BY lap_time ASC LIMIT 1", args)
        return self.get_lap_meta(rows[0]["id"]) if rows else None

    def default_reference(self, lap_meta: dict) -> dict | None:
        """一圈的默认参考圈：同赛道、同车、同游戏里最快的完整圈。

        排除 lap_meta 自己——它本身就是最快的话，参考圈就是第二快。
        对比页（只选了一圈时）和实时面板的“本圈小结”都用它，两边选出来的参考圈保证一样。
        """
        if lap_meta.get("track_id") is None:
            return None
        laps = self.list_laps(track_id=lap_meta["track_id"], car_ordinal=lap_meta.get("car_ordinal"),
                              game=lap_meta.get("game"), complete_only=True, limit=5000)
        laps = [l for l in laps if l["id"] != lap_meta.get("id") and l.get("lap_time")]
        return min(laps, key=lambda l: l["lap_time"]) if laps else None

    def update_lap(self, lap_id: int, *, note: str | None = None, is_complete: bool | None = None,
                   flags: list[str] | None = None) -> None:
        if note is not None:
            self._x("UPDATE laps SET note=? WHERE id=?", (note, lap_id))
        if is_complete is not None:
            self._x("UPDATE laps SET is_complete=? WHERE id=?", (1 if is_complete else 0, lap_id))
        if flags is not None:
            self._x("UPDATE laps SET flags=? WHERE id=?", (json.dumps(flags, ensure_ascii=False), lap_id))

    def delete_lap(self, lap_id: int) -> None:
        self._x("DELETE FROM laps WHERE id=?", (lap_id,))
