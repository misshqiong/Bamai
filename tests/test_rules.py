from __future__ import annotations

from server import config
from server.rules import RuleEngine
from tests.conftest import metric


def engine(db):
    notifications = []
    rules = RuleEngine(db, lambda title, detail: notifications.append((title, detail)) or True)
    return rules, notifications


def insert_range(db, start, end, **values):
    for ts in range(start, end + 1, config.SAMPLE_INTERVAL_SECONDS):
        db.insert_metric(metric(ts, **values))


def test_cpu_trigger_deduplicate_and_recover(db):
    now = 100_000
    insert_range(db, now - config.CPU_HIGH_DURATION_SECONDS, now, cpu_percent=92)
    rules, notifications = engine(db)
    assert len(rules.evaluate(now)) == 1
    assert db.list_events()[0]["kind"] == "cpu_high"
    assert len(notifications) == 1
    assert rules.evaluate(now + 60) == []
    insert_range(db, now + 61, now + 61 + config.CPU_HIGH_DURATION_SECONDS, cpu_percent=10)
    recovered_at = now + 61 + config.CPU_HIGH_DURATION_SECONDS
    rules.evaluate(recovered_at)
    assert db.list_events()[0]["resolved_ts"] == recovered_at


def test_memory_percent_and_swap_growth_rules(db):
    now = 200_000
    db.insert_metric(metric(now, mem_percent=91))
    rules, _ = engine(db)
    rules.evaluate(now)
    assert db.list_events()[0]["kind"] == "mem_pressure"
    db.resolve_events("mem_pressure", now + 1)

    start = now + 2000 - config.SWAP_WINDOW_SECONDS
    insert_range(db, start, now + 2000 - config.SAMPLE_INTERVAL_SECONDS, swap_used=0)
    db.insert_metric(metric(now + 2000, swap_used=config.SWAP_GROWTH_BYTES + 1))
    rules.evaluate(now + 2000)
    assert db.list_events()[0]["kind"] == "mem_pressure"


def test_disk_warning_critical_and_recovery(db):
    now = 300_000
    db.insert_disk_usage(now, [{"mount": "/", "total": 100, "used": 96, "percent": 96}])
    rules, _ = engine(db)
    rules.evaluate(now)
    event = db.list_events()[0]
    assert (event["kind"], event["severity"]) == ("disk_full", "critical")
    db.insert_disk_usage(now + 60, [{"mount": "/", "total": 100, "used": 50, "percent": 50}])
    rules.evaluate(now + 60)
    assert db.list_events()[0]["resolved_ts"] == now + 60


def test_network_spike_requires_baseline_and_sustained_window(db):
    now = 400_000
    baseline_start = now - config.NET_BASELINE_SECONDS
    recent_start = now - config.NET_SPIKE_DURATION_SECONDS
    for ts in range(baseline_start, recent_start, config.SAMPLE_INTERVAL_SECONDS):
        db.insert_metric(metric(ts, net_up_bps=100_000, net_down_bps=100_000))
    insert_range(db, recent_start, now, net_up_bps=7 * 1024**2, net_down_bps=7 * 1024**2)
    rules, _ = engine(db)
    rules.evaluate(now)
    assert db.list_events()[0]["kind"] == "net_spike"


def test_incomplete_windows_do_not_resolve_existing_events(db):
    now = 500_000
    db.create_event(now - 60, "cpu_high", "warning", "CPU 持续高负载", "仍需观察")
    db.insert_metric(metric(now, cpu_percent=10))
    rules, _ = engine(db)
    rules.evaluate(now)
    assert db.list_events()[0]["resolved_ts"] is None


def test_new_event_schedules_ai_diagnosis_without_changing_event_creation(db):
    now = 600_000
    db.insert_disk_usage(now, [{"mount": "/", "total": 100, "used": 96, "percent": 96}])
    scheduled = []
    rules = RuleEngine(
        db,
        notifier=lambda *_: True,
        diagnoser=lambda event_id, event: scheduled.append((event_id, event)),
    )
    created = rules.evaluate(now)
    assert created == [scheduled[0][0]]
    assert scheduled[0][1]["kind"] == "disk_full"
    assert db.list_events()[0]["ai_analysis"] is None


def test_event_and_notification_follow_current_language(db):
    now = 700_000
    db.insert_disk_usage(now, [{"mount": "/", "total": 100, "used": 96, "percent": 96}])
    notifications = []
    rules = RuleEngine(
        db,
        notifier=lambda title, detail: notifications.append((title, detail)) or True,
        language_provider=lambda: "en",
    )
    rules.evaluate(now)
    event = db.list_events()[0]
    assert event["title"] == "Disk space is running low"
    assert event["params"] == {"mount": "/", "percent": 96.0}
    assert notifications[0][0] == "Disk space is running low"
