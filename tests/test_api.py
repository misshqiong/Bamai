from __future__ import annotations

from fastapi.testclient import TestClient

from server.agent.ollama_client import ChatResult
from server.main import create_app
from tests.conftest import metric


class FakeAgent:
    def __init__(self, *, available=True, model_pulled=True):
        self.status_value = {
            "available": available, "model_pulled": model_pulled, "model": "qwen3:4b"
        }
        self.received = None

    async def status(self):
        return self.status_value

    async def chat(self, messages, language="zh"):
        self.received = messages
        self.language = language
        return ChatResult("当前 CPU 正常。", [{
            "tool": "get_current_stats", "args": {}, "summary": "查询了当前系统状态"
        }])


def test_overview_metrics_events_and_websocket(db, monkeypatch):
    db.insert_metric(metric(100, cpu_percent=33))
    db.insert_disk_usage(100, [{"mount": "/", "total": 100, "used": 40, "percent": 40}])
    db.create_event(100, "example", "info", "示例", "详情")
    monkeypatch.setattr("server.main.list_processes", lambda sort, limit: [])
    with TestClient(create_app(db, collector_enabled=False, agent_client=FakeAgent())) as client:
        overview = client.get("/api/overview")
        assert overview.status_code == 200
        assert overview.json()["metric"]["cpu_percent"] == 33
        assert overview.json()["unresolved_events"] == 1
        assert overview.json()["ollama"]["model_pulled"] is True
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
    with TestClient(create_app(db, collector_enabled=False, agent_client=FakeAgent())) as client:
        assert client.get("/api/metrics", params={"metric": "invalid", "start": 0, "end": 1}).status_code == 422
        assert client.get("/api/processes", params={"sort": "network"}).status_code == 422
        found = client.get("/api/search/files", params={"q": "报告", "kind": "name"})
        assert found.status_code == 200
        assert found.json()["items"][0]["name"] == "报告"
        large = client.get("/api/search/large-files", params={"path": "~", "min_mb": 100})
        assert large.status_code == 200


def test_chat_and_ollama_status_with_mock_agent(db):
    agent = FakeAgent()
    with TestClient(create_app(db, collector_enabled=False, agent_client=agent)) as client:
        assert client.get("/api/ollama/status").json() == agent.status_value
        response = client.post("/api/chat", json={
            "messages": [{"role": "user", "content": "CPU 怎么样？"}]
        })
        assert response.status_code == 200
        assert response.json()["reply"] == "当前 CPU 正常。"
        assert response.json()["tool_trace"][0]["tool"] == "get_current_stats"
        assert agent.received == [{"role": "user", "content": "CPU 怎么样？"}]
        assert agent.language == "zh"


def test_chat_language_follows_accept_language_header(db):
    agent = FakeAgent()
    with TestClient(create_app(db, collector_enabled=False, agent_client=agent)) as client:
        response = client.post(
            "/api/chat",
            headers={"Accept-Language": "en-US"},
            json={"messages": [{"role": "user", "content": "How is my Mac?"}]},
        )
        assert response.status_code == 200
        assert agent.language == "en"


def test_chat_gracefully_returns_503_when_ollama_is_unavailable(db):
    agent = FakeAgent(available=False, model_pulled=False)
    with TestClient(create_app(db, collector_enabled=False, agent_client=agent)) as client:
        response = client.post("/api/chat", json={
            "messages": [{"role": "user", "content": "检查内存"}]
        })
        assert response.status_code == 503
        assert "brew install ollama && ollama pull qwen3:4b" in response.json()["detail"]
