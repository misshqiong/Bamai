from __future__ import annotations

import sqlite3

from server.db import Database


def test_events_params_migration_preserves_legacy_rows(tmp_path):
    path = tmp_path / "legacy.db"
    with sqlite3.connect(path) as conn:
        conn.execute(
            "CREATE TABLE events(id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER, "
            "kind TEXT, severity TEXT, title TEXT, detail TEXT, ai_analysis TEXT, "
            "resolved_ts INTEGER)"
        )
        conn.execute(
            "INSERT INTO events(ts,kind,severity,title,detail,ai_analysis,resolved_ts) "
            "VALUES(100,'cpu_high','warning','旧标题','旧详情',NULL,NULL)"
        )

    db = Database(path)
    events = db.list_events()
    assert len(events) == 1
    assert events[0]["title"] == "旧标题"
    assert events[0]["detail"] == "旧详情"
    assert events[0]["params"] is None
    with db.connect(read_only=True) as conn:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(events)")}
        assert "params" in columns
        assert conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 1

    db_again = Database(path)
    assert db_again.list_events()[0]["title"] == "旧标题"


def test_new_events_store_structured_params(db):
    db.create_event(
        100, "disk_full", "critical", "磁盘空间不足", "详情",
        {"mount": "/", "percent": 96.2},
    )
    assert db.list_events()[0]["params"] == {"mount": "/", "percent": 96.2}
