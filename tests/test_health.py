from __future__ import annotations

from fastapi.testclient import TestClient

from server import config
from server.agent.ollama_client import ChatResult
from server.main import create_app
from tests.conftest import metric


NOW = 1_000_000


class HealthAgent:
    def __init__(self, ready=False):
        self.ready = ready
        self.language = None

    async def status(self):
        return {
            "available": self.ready, "model_pulled": self.ready, "model": "qwen3:4b"
        }

    async def chat(self, messages, language="zh"):
        self.language = language
        assert "health" in messages[0]["content"].lower() or "健康" in messages[0]["content"]
        return ChatResult("Your Mac only needs a little attention.", [])


def populate_hour(db, *, recent_cpu=20, mem_percent=40):
    for ts in range(NOW - config.NET_BASELINE_SECONDS, NOW + 1, config.SAMPLE_INTERVAL_SECONDS):
        cpu = recent_cpu if ts >= NOW - config.CPU_HIGH_DURATION_SECONDS else 20
        db.insert_metric(metric(
            ts, cpu_percent=cpu, mem_percent=mem_percent,
            net_up_bps=100_000, net_down_bps=100_000, swap_used=0,
        ))


def health_response(db, monkeypatch, agent=None):
    monkeypatch.setattr("server.rules.time.time", lambda: NOW)
    with TestClient(create_app(
        db, collector_enabled=False, agent_client=agent or HealthAgent()
    )) as client:
        return client.get("/api/health")


def test_health_ok_when_all_checks_are_normal(db, monkeypatch):
    populate_hour(db)
    db.insert_disk_usage(NOW, [{
        "mount": "/", "total": 1000, "used": 500, "percent": 50,
    }])
    response = health_response(db, monkeypatch)
    assert response.status_code == 200
    body = response.json()
    assert body["level"] == "ok"
    assert {check["kind"] for check in body["checks"]} == {
        "cpu", "memory", "disk", "network", "swap"
    }
    assert all(check["level"] == "ok" for check in body["checks"])
    assert body["ai_analysis"] is None


def test_health_warn_when_cpu_is_sustained_high(db, monkeypatch):
    populate_hour(db, recent_cpu=92)
    db.insert_disk_usage(NOW, [{
        "mount": "/", "total": 1000, "used": 500, "percent": 50,
    }])
    body = health_response(db, monkeypatch).json()
    assert body["level"] == "warn"
    cpu = next(check for check in body["checks"] if check["kind"] == "cpu")
    assert cpu["level"] == "warn"
    assert cpu["params"]["percent"] > 85


def test_health_critical_when_disk_is_over_95_percent(db, monkeypatch):
    populate_hour(db)
    db.insert_disk_usage(NOW, [{
        "mount": "/", "total": 1000, "used": 960, "percent": 96,
    }])
    body = health_response(db, monkeypatch).json()
    assert body["level"] == "critical"
    disk = next(check for check in body["checks"] if check["kind"] == "disk")
    assert disk["level"] == "critical"
    assert disk["params"]["percent"] == 96


def test_health_explain_uses_agent_and_interface_language(db, monkeypatch):
    populate_hour(db)
    agent = HealthAgent(ready=True)
    monkeypatch.setattr("server.rules.time.time", lambda: NOW)
    with TestClient(create_app(db, collector_enabled=False, agent_client=agent)) as client:
        response = client.post("/api/health/explain", headers={"Accept-Language": "en-US"})
    assert response.status_code == 200
    assert response.json() == {"reply": "Your Mac only needs a little attention."}
    assert agent.language == "en"
