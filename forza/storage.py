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
import threading
from datetime import datetime
from pathlib import Path

from .lapdata import LapData

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
    created_at    TEXT
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

# 赛道指纹匹配阈值
TRACK_START_RADIUS_M = 60.0   # 起点坐标相距这么近算同一个起点
TRACK_LENGTH_TOL = 0.04       # 圈长差在 4% 以内算同一条赛道（区分共用起点线的不同布局）


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


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
            self._db.commit()

    def close(self) -> None:
        self._db.close()

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
            (game, car_ordinal, car_class, car_pi, drivetrain, num_cylinders, _now()),
        )

    def set_car_name(self, game: str, car_ordinal: int, name: str) -> None:
        self._x("UPDATE cars SET name=? WHERE game=? AND car_ordinal=?", (name, game, car_ordinal))

    def list_cars(self) -> list[dict]:
        return [dict(r) for r in self._q("SELECT * FROM cars ORDER BY first_seen DESC")]

    # ---------------- 赛道 ----------------
    def find_or_create_track(self, *, family: str, game: str, kind: str, track_ordinal: int | None,
                             start_x: float, start_z: float, length_m: float | None) -> int:
        """找到（或新建）这一圈所在的赛道，返回 track id。

        - 有 TrackOrdinal（FM2023）：按 ordinal 识别
        - 否则按起点坐标（相距 < 60 m）+ 圈长（差 < 4%）识别；冲刺赛只看起点
        """
        with self._lock:
            if track_ordinal:
                rows = self._q("SELECT id FROM tracks WHERE family=? AND track_ordinal=? AND kind=?",
                               (family, track_ordinal, kind))
                if rows:
                    return rows[0]["id"]
            else:
                rows = self._q("SELECT * FROM tracks WHERE family=? AND kind=? AND track_ordinal IS NULL",
                               (family, kind))
                for r in rows:
                    if r["start_x"] is None:
                        continue
                    if math.hypot(r["start_x"] - start_x, r["start_z"] - start_z) > TRACK_START_RADIUS_M:
                        continue
                    if kind == "circuit" and length_m and r["ref_length_m"]:
                        if abs(r["ref_length_m"] - length_m) / r["ref_length_m"] > TRACK_LENGTH_TOL:
                            continue
                    return r["id"]
            n = self._q("SELECT COUNT(*) AS c FROM tracks WHERE kind=?", (kind,))[0]["c"] + 1
            default_name = f"{'赛道' if kind == 'circuit' else '冲刺路线'} {n}"
            if track_ordinal:
                default_name = f"赛道 #{track_ordinal}"
            return self._x(
                """INSERT INTO tracks (family, game, track_ordinal, kind, name, start_x, start_z, ref_length_m, created_at)
                   VALUES (?,?,?,?,?,?,?,?,?)""",
                (family, game, track_ordinal, kind, default_name, start_x, start_z, length_m, _now()),
            )

    def update_track_length(self, track_id: int, length_m: float) -> None:
        """记录参考长度（冲刺路线取跑过的最长距离；赛道取第一次完整圈）。"""
        self._x("UPDATE tracks SET ref_length_m = MAX(COALESCE(ref_length_m, 0), ?) WHERE id=?",
                (length_m, track_id))

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
            (_now(), game, fmt, source, car_ordinal, track_id),
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
             json.dumps(extra, ensure_ascii=False), m.get("note"), len(lap), m.get("created_at") or _now(),
             lap.to_blob()),
        )
        lap.meta["id"] = lap_id
        return lap_id

    @staticmethod
    def _row_meta(r: sqlite3.Row) -> dict:
        d = {k: r[k] for k in r.keys() if k not in ("samples", "meta", "flags")}
        d.update(json.loads(r["meta"] or "{}"))
        d["flags"] = json.loads(r["flags"] or "[]")
        d["is_complete"] = bool(r["is_complete"])
        return d

    def list_laps(self, *, track_id: int | None = None, car_ordinal: int | None = None,
                  game: str | None = None, session_id: int | None = None,
                  complete_only: bool = False, limit: int = 500) -> list[dict]:
        sql = ("SELECT l.id, l.session_id, l.track_id, l.game, l.family, l.fmt, l.car_ordinal, l.car_class, "
               "l.car_pi, l.lap_number, l.kind, l.lap_time, l.time_source, l.distance_m, l.max_speed, "
               "l.is_complete, l.flags, l.meta, l.note, l.sample_count, l.created_at, "
               "t.name AS track_name, c.name AS car_name "
               "FROM laps l LEFT JOIN tracks t ON t.id = l.track_id "
               "LEFT JOIN cars c ON c.game = l.game AND c.car_ordinal = l.car_ordinal WHERE 1=1")
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
        rows = self._q(
            f"""SELECT l.*, t.name AS track_name, c.name AS car_name FROM laps l
                LEFT JOIN tracks t ON t.id = l.track_id
                LEFT JOIN cars c ON c.game = l.game AND c.car_ordinal = l.car_ordinal
                WHERE l.id IN ({','.join('?' * len(ids))})""", ids)
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

    def update_lap(self, lap_id: int, *, note: str | None = None, is_complete: bool | None = None) -> None:
        if note is not None:
            self._x("UPDATE laps SET note=? WHERE id=?", (note, lap_id))
        if is_complete is not None:
            self._x("UPDATE laps SET is_complete=? WHERE id=?", (1 if is_complete else 0, lap_id))

    def delete_lap(self, lap_id: int) -> None:
        self._x("DELETE FROM laps WHERE id=?", (lap_id,))
