from __future__ import annotations

import time

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
        history = client.get(
            "/api/metrics",
            params={"metric": "cpu_percent", "start": 0, "end": 200},
        )
        assert history.status_code == 200
        assert history.json()["points"][0]["avg"] == 33
        assert client.get("/api/events").json()["items"][0]["kind"] == "example"
        index = client.get("/")
        assert index.status_code == 200
        assert index.headers["cache-control"] == "no-cache"
        asset = client.get("/static/js/app.js")
        assert asset.status_code == 200
        assert asset.headers["cache-control"] == "no-cache"
        with client.websocket_connect("/ws/realtime") as websocket:
            assert websocket.receive_json()["ts"] == 100


def test_api_validation_and_search(db, monkeypatch):
    monkeypatch.setattr(
        "server.main.mdfind_search",
        lambda q, kind, limit: [{"name": q, "path": "/tmp/x", "size": 1}],
    )
    monkeypatch.setattr(
        "server.main.find_large_files",
        lambda path, min_mb, limit: {
            "items": [],
            "truncated": False,
            "scanned_path": path,
        },
    )
    with TestClient(create_app(db, collector_enabled=False, agent_client=FakeAgent())) as client:
        response = client.get(
            "/api/metrics", params={"metric": "invalid", "start": 0, "end": 1}
        )
        assert response.status_code == 422
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


def test_auto_pull_starts_only_when_ollama_up_and_model_missing():
    import asyncio

    from server.main import auto_pull_missing_model

    class Agent:
        def __init__(self, available, pulled):
            self._status = {"available": available, "model_pulled": pulled, "model": "qwen3:4b"}

        async def status(self):
            return self._status

    class Manager:
        def __init__(self):
            self.started = []

        def start(self, model):
            self.started.append(model)
            return True

    missing = Manager()
    asyncio.run(auto_pull_missing_model(Agent(True, False), missing))
    assert missing.started == ["qwen3:4b"]

    for agent in (Agent(True, True), Agent(False, False)):
        untouched = Manager()
        asyncio.run(auto_pull_missing_model(agent, untouched))
        assert untouched.started == []


def test_apps_list_and_detail_endpoints(db, monkeypatch):
    now = int(time.time())
    live = [
        {
            "pid": 10, "ppid": 0, "name": "Chrome", "cpu_percent": 12.5,
            "memory_rss": 1000, "cmdline": "chrome",
            "exe": "/Applications/Google Chrome.app/Contents/MacOS/Chrome",
        },
        {
            "pid": 11, "ppid": 10, "name": "Chrome Helper", "cpu_percent": 2.5,
            "memory_rss": 500, "cmdline": "helper", "exe": "",
        },
        {
            "pid": 20, "ppid": 0, "name": "backupd", "cpu_percent": 1.0,
            "memory_rss": 250, "cmdline": "backupd", "exe": "/usr/libexec/backupd",
        },
    ]
    monkeypatch.setattr("server.main.list_application_processes", lambda: live)
    db.insert_app_snapshots(now, [
        {
            "app": "Google Chrome", "kind": "app", "cpu_percent": 15,
            "memory_rss": 1500, "proc_count": 2, "up_bps": 100, "down_bps": 500,
        },
        {
            "app": "backupd", "kind": "background", "cpu_percent": 1,
            "memory_rss": 250, "proc_count": 1, "up_bps": 900, "down_bps": 100,
        },
    ])
    db.insert_app_connections(now, [{
        "app": "Google Chrome", "remote_ip": "1.1.1.1", "remote_port": 443,
        "domain": "one.one.one.one", "proto": "tcp", "up_bps": 100,
        "down_bps": 500, "rtt_ms": 12, "via_proxy": 0, "proxy_name": None,
    }])
    db.insert_app_process_snapshots(now, [{
        "app": "Google Chrome", "name": "Chrome Helper",
        "cpu_percent": 2.5, "memory_rss": 500, "proc_count": 1,
    }])

    with TestClient(create_app(db, collector_enabled=False, agent_client=FakeAgent())) as client:
        response = client.get("/api/apps", params={"sort": "network", "limit": 2})
        assert response.status_code == 200
        assert [item["app"] for item in response.json()["apps"]] == [
            "backupd", "Google Chrome",
        ]
        chrome = response.json()["apps"][1]
        assert chrome["proc_count"] == 2
        assert chrome["down_bps"] == 500

        detail = client.get("/api/apps/Google%20Chrome/detail", params={"window": 3600})
        assert detail.status_code == 200
        body = detail.json()
        assert body["app"] == "Google Chrome"
        assert [process["pid"] for process in body["processes"]] == [10, 11]
        assert body["history"][0]["cpu_percent"] == 15
        assert body["process_names"] == [{
            "name": "Chrome Helper", "peak_cpu": 2.5, "peak_memory_rss": 500,
        }]
        assert body["connections"][0] == {
            "domain": "one.one.one.one", "remote_ip": "1.1.1.1", "port": 443,
            "up_bps": 100.0, "down_bps": 500.0, "rtt_ms": 12.0,
            "via_proxy": 0, "proxy_name": None,
        }

        assert client.get("/api/apps", params={"sort": "invalid"}).status_code == 422
        invalid_window = client.get(
            "/api/apps/Google%20Chrome/detail", params={"window": 59}
        )
        assert invalid_window.status_code == 422

        process_history = client.get(
            "/api/apps/Google%20Chrome/process-history",
            params={"name": "Chrome Helper", "window": 3600},
        )
        assert process_history.status_code == 200
        assert process_history.json() == {
            "app": "Google Chrome",
            "name": "Chrome Helper",
            "history": [{
                "ts": now, "app": "Google Chrome", "name": "Chrome Helper",
                "cpu_percent": 2.5, "memory_rss": 500, "proc_count": 1,
            }],
        }
        assert client.get(
            "/api/apps/Google%20Chrome/process-history"
        ).status_code == 422
        for window in (59, 7 * 24 * 60 * 60 + 1):
            invalid = client.get(
                "/api/apps/Google%20Chrome/process-history",
                params={"name": "Chrome Helper", "window": window},
            )
            assert invalid.status_code == 422
