"""SQLite 存储层：建表、写入、查询、降采样和保留期清理。"""

from __future__ import annotations

import json
import math
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from . import config


METRIC_COLUMNS = {
    "cpu_percent",
    "load_1",
    "mem_used",
    "mem_total",
    "mem_percent",
    "swap_used",
    "disk_read_bps",
    "disk_write_bps",
    "net_up_bps",
    "net_down_bps",
}

HOURLY_COLUMNS = tuple(sorted(METRIC_COLUMNS))


class Database:
    """轻量 SQLite 封装；每次操作使用独立连接，安全跨线程。"""

    def __init__(self, path: str | Path = config.DB_PATH) -> None:
        self.path = Path(path).expanduser()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._write_lock = threading.RLock()
        self.init_schema()

    def connect(self, *, read_only: bool = False) -> sqlite3.Connection:
        if read_only:
            uri = f"file:{self.path.resolve()}?mode=ro"
            conn = sqlite3.connect(uri, uri=True, timeout=10, check_same_thread=False)
        else:
            conn = sqlite3.connect(self.path, timeout=10, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=10000")
        return conn

    def init_schema(self) -> None:
        avg_max = ",\n".join(
            f"{name}_avg REAL, {name}_max REAL" for name in HOURLY_COLUMNS
        )
        schema = f"""
        PRAGMA journal_mode=WAL;
        PRAGMA synchronous=NORMAL;
        CREATE TABLE IF NOT EXISTS metrics(
            ts INTEGER NOT NULL,
            cpu_percent REAL NOT NULL,
            cpu_per_core TEXT NOT NULL,
            load_1 REAL NOT NULL,
            mem_used INTEGER NOT NULL,
            mem_total INTEGER NOT NULL,
            mem_percent REAL NOT NULL,
            swap_used INTEGER NOT NULL,
            disk_read_bps REAL NOT NULL,
            disk_write_bps REAL NOT NULL,
            net_up_bps REAL NOT NULL,
            net_down_bps REAL NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_metrics_ts ON metrics(ts);
        CREATE TABLE IF NOT EXISTS metrics_hourly(
            hour_ts INTEGER PRIMARY KEY,
            {avg_max}
        );
        CREATE TABLE IF NOT EXISTS process_snapshots(
            ts INTEGER NOT NULL, pid INTEGER NOT NULL, name TEXT NOT NULL,
            cpu_percent REAL NOT NULL, memory_rss INTEGER NOT NULL, cmdline TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_process_snapshots_ts ON process_snapshots(ts);
        CREATE TABLE IF NOT EXISTS process_net(
            ts INTEGER NOT NULL, pid INTEGER NOT NULL, name TEXT NOT NULL,
            up_bps REAL NOT NULL, down_bps REAL NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_process_net_ts ON process_net(ts);
        CREATE TABLE IF NOT EXISTS disk_usage(
            ts INTEGER NOT NULL, mount TEXT NOT NULL, total INTEGER NOT NULL,
            used INTEGER NOT NULL, percent REAL NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_disk_usage_ts ON disk_usage(ts);
        CREATE TABLE IF NOT EXISTS events(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts INTEGER NOT NULL, kind TEXT NOT NULL, severity TEXT NOT NULL,
            title TEXT NOT NULL, detail TEXT NOT NULL, ai_analysis TEXT,
            resolved_ts INTEGER
        );
        CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts);
        CREATE INDEX IF NOT EXISTS idx_events_kind_resolved ON events(kind, resolved_ts);
        """
        with self._write_lock, self.connect() as conn:
            conn.executescript(schema)
            columns = {row[1] for row in conn.execute("PRAGMA table_info(events)")}
            if "params" not in columns:
                # 存量表只增列，不重建、不复制，旧行自然保持 params=NULL。
                conn.execute("ALTER TABLE events ADD COLUMN params TEXT")

    def insert_metric(self, metric: Mapping[str, Any]) -> None:
        columns = (
            "ts", "cpu_percent", "cpu_per_core", "load_1", "mem_used", "mem_total",
            "mem_percent", "swap_used", "disk_read_bps", "disk_write_bps",
            "net_up_bps", "net_down_bps",
        )
        values = [metric[name] for name in columns]
        if not isinstance(values[2], str):
            values[2] = json.dumps(values[2], separators=(",", ":"))
        placeholders = ",".join("?" for _ in columns)
        with self._write_lock, self.connect() as conn:
            conn.execute(
                f"INSERT INTO metrics({','.join(columns)}) VALUES({placeholders})",
                values,
            )

    def insert_process_snapshots(self, ts: int, rows: Iterable[Mapping[str, Any]]) -> None:
        values = [
            (ts, int(row["pid"]), str(row["name"]), float(row["cpu_percent"]),
             int(row["memory_rss"]), str(row.get("cmdline", ""))[:200])
            for row in rows
        ]
        if not values:
            return
        with self._write_lock, self.connect() as conn:
            conn.executemany(
                "INSERT INTO process_snapshots VALUES(?,?,?,?,?,?)", values
            )

    def insert_process_net(self, ts: int, rows: Iterable[Mapping[str, Any]]) -> None:
        values = [
            (ts, int(row["pid"]), str(row["name"]), float(row["up_bps"]),
             float(row["down_bps"]))
            for row in rows
        ]
        if not values:
            return
        with self._write_lock, self.connect() as conn:
            conn.executemany("INSERT INTO process_net VALUES(?,?,?,?,?)", values)

    def insert_disk_usage(self, ts: int, rows: Iterable[Mapping[str, Any]]) -> None:
        values = [
            (ts, str(row["mount"]), int(row["total"]), int(row["used"]),
             float(row["percent"]))
            for row in rows
        ]
        if not values:
            return
        with self._write_lock, self.connect() as conn:
            conn.executemany("INSERT INTO disk_usage VALUES(?,?,?,?,?)", values)

    def latest_metric(self) -> dict[str, Any] | None:
        with self.connect(read_only=True) as conn:
            row = conn.execute("SELECT * FROM metrics ORDER BY ts DESC LIMIT 1").fetchone()
        return self._metric_row(row) if row else None

    @staticmethod
    def _metric_row(row: sqlite3.Row) -> dict[str, Any]:
        result = dict(row)
        try:
            result["cpu_per_core"] = json.loads(result["cpu_per_core"])
        except (TypeError, json.JSONDecodeError):
            result["cpu_per_core"] = []
        return result

    def raw_metrics(self, start: int, end: int) -> list[dict[str, Any]]:
        with self.connect(read_only=True) as conn:
            rows = conn.execute(
                "SELECT * FROM metrics WHERE ts BETWEEN ? AND ? ORDER BY ts", (start, end)
            ).fetchall()
        return [self._metric_row(row) for row in rows]

    def query_metrics(
        self, metric: str, start: int, end: int, max_points: int = config.MAX_CHART_POINTS
    ) -> list[dict[str, float | int]]:
        if metric not in METRIC_COLUMNS:
            raise ValueError(f"不支持的指标: {metric}")
        if end < start:
            raise ValueError("end 必须大于等于 start")
        max_points = max(1, min(int(max_points), 5000))
        span = max(1, end - start + 1)
        bucket = max(1, math.ceil(span / max_points))
        sql = f"""
            SELECT ((ts - ?) / ?) * ? + ? AS bucket_ts,
                   AVG({metric}) AS avg_value, MAX({metric}) AS max_value
            FROM metrics WHERE ts BETWEEN ? AND ?
            GROUP BY ((ts - ?) / ?) ORDER BY bucket_ts
        """
        with self.connect(read_only=True) as conn:
            rows = conn.execute(
                sql, (start, bucket, bucket, start, start, end, start, bucket)
            ).fetchall()
            # 48 小时以前的原始数据已归档到小时表；只补没有原始点的历史小时。
            hourly = conn.execute(
                f"SELECT hour_ts, {metric}_avg, {metric}_max FROM metrics_hourly "
                "WHERE hour_ts BETWEEN ? AND ? ORDER BY hour_ts",
                (start - start % 3600, end),
            ).fetchall()
        result = [
            {"ts": int(row["bucket_ts"]), "avg": float(row["avg_value"]),
             "max": float(row["max_value"])}
            for row in rows
        ]
        if hourly:
            result.extend(
                {"ts": int(row[0]), "avg": float(row[1]), "max": float(row[2])}
                for row in hourly if row[1] is not None
            )
            result.sort(key=lambda point: int(point["ts"]))
        return self._downsample_points(result, max_points)

    @staticmethod
    def _downsample_points(
        points: Sequence[dict[str, float | int]], max_points: int
    ) -> list[dict[str, float | int]]:
        if len(points) <= max_points:
            return list(points)
        size = math.ceil(len(points) / max_points)
        compact: list[dict[str, float | int]] = []
        for offset in range(0, len(points), size):
            group = points[offset:offset + size]
            compact.append({
                "ts": int(group[0]["ts"]),
                "avg": sum(float(item["avg"]) for item in group) / len(group),
                "max": max(float(item["max"]) for item in group),
            })
        return compact

    def latest_disk_usage(self) -> list[dict[str, Any]]:
        with self.connect(read_only=True) as conn:
            latest = conn.execute("SELECT MAX(ts) FROM disk_usage").fetchone()[0]
            if latest is None:
                return []
            rows = conn.execute(
                "SELECT * FROM disk_usage WHERE ts=? ORDER BY mount", (latest,)
            ).fetchall()
        return [dict(row) for row in rows]

    def latest_process_snapshots(self, limit: int = 5) -> list[dict[str, Any]]:
        with self.connect(read_only=True) as conn:
            latest = conn.execute("SELECT MAX(ts) FROM process_snapshots").fetchone()[0]
            if latest is None:
                return []
            rows = conn.execute(
                "SELECT * FROM process_snapshots WHERE ts=? "
                "ORDER BY cpu_percent DESC, memory_rss DESC LIMIT ?", (latest, limit)
            ).fetchall()
        return [dict(row) for row in rows]

    def latest_process_net(self) -> list[dict[str, Any]]:
        with self.connect(read_only=True) as conn:
            latest = conn.execute("SELECT MAX(ts) FROM process_net").fetchone()[0]
            if latest is None:
                return []
            rows = conn.execute(
                "SELECT * FROM process_net WHERE ts=? ORDER BY (up_bps+down_bps) DESC",
                (latest,),
            ).fetchall()
        return [dict(row) for row in rows]

    def process_history(
        self, start: int, end: int, name: str | None = None, limit: int = 30
    ) -> list[dict[str, Any]]:
        if end < start:
            raise ValueError("end 必须大于等于 start")
        pattern = f"%{name.strip()}%" if name and name.strip() else "%"
        with self.connect(read_only=True) as conn:
            rows = conn.execute(
                "SELECT name, COUNT(*) AS samples, AVG(cpu_percent) AS cpu_avg, "
                "MAX(cpu_percent) AS cpu_max, AVG(memory_rss) AS memory_avg, "
                "MAX(memory_rss) AS memory_max, MAX(ts) AS last_ts "
                "FROM process_snapshots WHERE ts BETWEEN ? AND ? AND name LIKE ? "
                "GROUP BY name ORDER BY cpu_max DESC, memory_max DESC LIMIT ?",
                (start, end, pattern, max(1, min(int(limit), 100))),
            ).fetchall()
        return [dict(row) for row in rows]

    def create_event(
        self, ts: int, kind: str, severity: str, title: str, detail: str,
        params: Mapping[str, Any] | None = None,
    ) -> int:
        encoded_params = (
            json.dumps(params, ensure_ascii=False, separators=(",", ":"))
            if params is not None else None
        )
        with self._write_lock, self.connect() as conn:
            cursor = conn.execute(
                "INSERT INTO events(ts,kind,severity,title,detail,ai_analysis,resolved_ts,params) "
                "VALUES(?,?,?,?,?,NULL,NULL,?)",
                (ts, kind, severity, title, detail, encoded_params),
            )
            return int(cursor.lastrowid)

    def recent_event(self, kind: str, since_ts: int) -> dict[str, Any] | None:
        with self.connect(read_only=True) as conn:
            row = conn.execute(
                "SELECT * FROM events WHERE kind=? AND ts>=? ORDER BY ts DESC LIMIT 1",
                (kind, since_ts),
            ).fetchone()
        return self._event_row(row) if row else None

    def resolve_events(self, kind: str, resolved_ts: int) -> int:
        with self._write_lock, self.connect() as conn:
            cursor = conn.execute(
                "UPDATE events SET resolved_ts=? WHERE kind=? AND resolved_ts IS NULL",
                (resolved_ts, kind),
            )
            return cursor.rowcount

    def list_events(
        self, limit: int = 50, since_ts: int | None = None
    ) -> list[dict[str, Any]]:
        with self.connect(read_only=True) as conn:
            if since_ts is None:
                rows = conn.execute(
                    "SELECT * FROM events ORDER BY ts DESC LIMIT ?", (limit,)
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM events WHERE ts>=? ORDER BY ts DESC LIMIT ?",
                    (int(since_ts), limit),
                ).fetchall()
        return [self._event_row(row) for row in rows]

    @staticmethod
    def _event_row(row: sqlite3.Row) -> dict[str, Any]:
        event = dict(row)
        encoded = event.get("params")
        if encoded is None:
            return event
        try:
            parsed = json.loads(encoded)
            event["params"] = parsed if isinstance(parsed, dict) else None
        except (TypeError, json.JSONDecodeError):
            event["params"] = None
        return event

    def update_event_ai_analysis(self, event_id: int, analysis: str) -> bool:
        with self._write_lock, self.connect() as conn:
            cursor = conn.execute(
                "UPDATE events SET ai_analysis=? WHERE id=?",
                (analysis.strip(), int(event_id)),
            )
            return cursor.rowcount > 0

    def latest_unresolved_ai_analysis(self) -> str | None:
        with self.connect(read_only=True) as conn:
            row = conn.execute(
                "SELECT ai_analysis FROM events WHERE resolved_ts IS NULL "
                "AND ai_analysis IS NOT NULL AND TRIM(ai_analysis)<>'' "
                "ORDER BY ts DESC LIMIT 1"
            ).fetchone()
        return str(row[0]) if row else None

    def unresolved_event_count(self) -> int:
        with self.connect(read_only=True) as conn:
            return int(conn.execute(
                "SELECT COUNT(*) FROM events WHERE resolved_ts IS NULL"
            ).fetchone()[0])

    def cleanup(self, now: int | None = None) -> None:
        now = int(time.time()) if now is None else int(now)
        metric_cutoff = now - config.METRICS_RETENTION_SECONDS
        # 只归档完整小时，避免后续清理用半小时数据覆盖同一整点聚合。
        archive_cutoff = (metric_cutoff // 3600) * 3600
        columns = []
        selects = []
        for name in HOURLY_COLUMNS:
            columns.extend((f"{name}_avg", f"{name}_max"))
            selects.extend((f"AVG({name})", f"MAX({name})"))
        with self._write_lock, self.connect() as conn:
            conn.execute(
                f"INSERT OR REPLACE INTO metrics_hourly(hour_ts,{','.join(columns)}) "
                f"SELECT (ts/3600)*3600,{','.join(selects)} FROM metrics "
                "WHERE ts < ? GROUP BY (ts/3600)*3600", (archive_cutoff,)
            )
            conn.execute("DELETE FROM metrics WHERE ts < ?", (archive_cutoff,))
            process_cutoff = now - config.PROCESS_RETENTION_SECONDS
            conn.execute("DELETE FROM process_snapshots WHERE ts < ?", (process_cutoff,))
            conn.execute("DELETE FROM process_net WHERE ts < ?", (process_cutoff,))
            conn.execute(
                "DELETE FROM disk_usage WHERE ts < ?", (now - config.DISK_RETENTION_SECONDS,)
            )
            conn.execute(
                "DELETE FROM events WHERE ts < ?", (now - config.EVENT_RETENTION_SECONDS,)
            )
