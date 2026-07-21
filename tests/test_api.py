from __future__ import annotations

from fastapi.testclient import TestClient

from server.main import create_app
from tests.conftest import metric


def test_overview_metrics_events_and_websocket(db, monkeypatch):
    db.insert_metric(metric(100, cpu_percent=33))
    db.insert_disk_usage(100, [{"mount": "/", "total": 100, "used": 40, "percent": 40}])
    db.create_event(100, "example", "info", "示例", "详情")
    monkeypatch.setattr("server.main.list_processes", lambda sort, limit: [])
    with TestClient(create_app(db, collector_enabled=False)) as client:
        overview = client.get("/api/overview")
        assert overview.status_code == 200
        assert overview.json()["metric"]["cpu_percent"] == 33
        assert overview.json()["unresolved_events"] == 1
        assert overview.json()["ollama"]["phase"] == 1
        history = client.get("/api/metrics", params={"metric": "cpu_percent", "start": 0, "end": 200})
        assert history.status_code == 200
        assert history.json()["points"][0]["avg"] == 33
        assert client.get("/api/events").json()["items"][0]["kind"] == "example"
        assert client.get("/").status_code == 200
        with client.websocket_connect("/ws/realtime") as websocket:
            assert websocket.receive_json()["ts"] == 100


def test_api_validation_and_search(db, monkeypatch):
    monkeypatch.setattr("server.main.mdfind_search", lambda q, kind, limit: [{"name": q, "path": "/tmp/x", "size": 1}])
    monkeypatch.setattr("server.main.find_large_files", lambda path, min_mb, limit: {"items": [], "truncated": False, "scanned_path": path})
    with TestClient(create_app(db, collector_enabled=False)) as client:
        assert client.get("/api/metrics", params={"metric": "invalid", "start": 0, "end": 1}).status_code == 422
        assert client.get("/api/processes", params={"sort": "network"}).status_code == 422
        found = client.get("/api/search/files", params={"q": "报告", "kind": "name"})
        assert found.status_code == 200
        assert found.json()["items"][0]["name"] == "报告"
        large = client.get("/api/search/large-files", params={"path": "~", "min_mb": 100})
        assert large.status_code == 200

