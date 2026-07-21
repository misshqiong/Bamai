from __future__ import annotations

from server import config
from tests.conftest import metric


def test_schema_and_round_trip(db):
    db.insert_metric(metric(100, cpu_percent=42.5, cpu_per_core=[40, 45]))
    latest = db.latest_metric()
    assert latest is not None
    assert latest["cpu_percent"] == 42.5
    assert latest["cpu_per_core"] == [40, 45]
    with db.connect(read_only=True) as conn:
        tables = {row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )}
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
    expected = {
        "metrics",
        "metrics_hourly",
        "process_snapshots",
        "process_net",
        "disk_usage",
        "events",
    }
    assert expected <= tables
    assert mode.lower() == "wal"


def test_query_metrics_downsamples_to_requested_limit(db):
    for ts in range(1000, 2000):
        db.insert_metric(metric(ts, cpu_percent=float(ts % 100)))
    points = db.query_metrics("cpu_percent", 1000, 1999, max_points=50)
    assert 1 < len(points) <= 50
    assert all(set(point) == {"ts", "avg", "max"} for point in points)
    assert all(point["max"] >= point["avg"] for point in points)


def test_query_rejects_unknown_metric(db):
    try:
        db.query_metrics("drop_table", 0, 1)
    except ValueError as exc:
        assert "不支持" in str(exc)
    else:
        raise AssertionError("未知指标应被拒绝")


def test_cleanup_aggregates_and_applies_retention(db):
    now = 1_000_000
    old_metric_ts = now - config.METRICS_RETENTION_SECONDS - 3600
    db.insert_metric(metric(old_metric_ts, cpu_percent=20))
    db.insert_metric(metric(old_metric_ts + 10, cpu_percent=40))
    db.insert_metric(metric(now - 10, cpu_percent=80))
    db.insert_process_snapshots(now - config.PROCESS_RETENTION_SECONDS - 1, [{
        "pid": 1, "name": "old", "cpu_percent": 1, "memory_rss": 1, "cmdline": "old"
    }])
    db.insert_process_net(now - config.PROCESS_RETENTION_SECONDS - 1, [{
        "pid": 1, "name": "old", "up_bps": 1, "down_bps": 1
    }])
    db.insert_disk_usage(now - config.DISK_RETENTION_SECONDS - 1, [{
        "mount": "/", "total": 100, "used": 50, "percent": 50
    }])
    db.create_event(now - config.EVENT_RETENTION_SECONDS - 1, "old", "info", "old", "old")

    db.cleanup(now)

    with db.connect(read_only=True) as conn:
        assert conn.execute("SELECT COUNT(*) FROM metrics").fetchone()[0] == 1
        hourly = conn.execute(
            "SELECT cpu_percent_avg,cpu_percent_max FROM metrics_hourly"
        ).fetchone()
        assert tuple(hourly) == (30.0, 40.0)
        assert conn.execute("SELECT COUNT(*) FROM process_snapshots").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM process_net").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM disk_usage").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 0
