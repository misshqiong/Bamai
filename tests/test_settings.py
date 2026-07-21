from __future__ import annotations

import json
import os

from fastapi.testclient import TestClient

from server.main import create_app
from server.settings import DEFAULT_SETTINGS, SettingsStore


class SettingsAgent:
    async def list_models(self):
        return [
            {"name": "qwen3:4b", "size": 3_000_000_000},
            {"name": "qwen3:8b", "size": 6_000_000_000},
        ]

    async def status(self):
        return {"available": True, "model_pulled": True, "model": "qwen3:4b"}


class FakePullManager:
    def __init__(self):
        self.state = {"model": None, "status": "idle", "percent": 0}

    def start(self, model):
        if self.state["status"] == "pulling":
            return False
        self.state = {"model": model, "status": "pulling", "percent": 0}
        return True

    def status(self):
        return self.state.copy()


def test_settings_get_post_persists_with_atomic_replace(db, tmp_path, monkeypatch):
    path = tmp_path / "state" / "config.json"
    store = SettingsStore(path)
    replacements = []
    original_replace = os.replace

    def tracking_replace(source, destination):
        replacements.append((source, destination))
        original_replace(source, destination)

    monkeypatch.setattr("server.settings.os.replace", tracking_replace)
    with TestClient(create_app(
        db,
        collector_enabled=False,
        agent_client=SettingsAgent(),
        settings_store=store,
    )) as client:
        assert client.get("/api/settings").json() == DEFAULT_SETTINGS
        response = client.post("/api/settings", json={
            "temperature": 0.2,
            "num_ctx": 16384,
            "language": "en",
        })
        assert response.status_code == 200
        assert response.json()["temperature"] == 0.2
        assert response.json()["num_ctx"] == 16384
        assert response.json()["language"] == "en"
        assert client.get("/api/settings").json() == response.json()

    persisted = json.loads(path.read_text(encoding="utf-8"))
    assert persisted["model"] == "qwen3:4b"
    assert persisted["temperature"] == 0.2
    assert len(replacements) == 1
    source, destination = replacements[0]
    assert os.path.dirname(source) == str(path.parent)
    assert destination == path
    assert not list(path.parent.glob("*.tmp"))


def test_settings_model_must_be_installed_and_values_are_validated(db, tmp_path):
    store = SettingsStore(tmp_path / "config.json")
    with TestClient(create_app(
        db,
        collector_enabled=False,
        agent_client=SettingsAgent(),
        settings_store=store,
    )) as client:
        selected = client.post("/api/settings", json={"model": "qwen3:8b"})
        assert selected.status_code == 200
        assert selected.json()["model"] == "qwen3:8b"
        assert client.post("/api/settings", json={"model": "missing:1b"}).status_code == 422
        assert client.post("/api/settings", json={"temperature": 2}).status_code == 422
        assert client.post("/api/settings", json={"unknown": True}).status_code == 422


def test_model_list_and_pull_endpoints(db, tmp_path):
    manager = FakePullManager()
    with TestClient(create_app(
        db,
        collector_enabled=False,
        agent_client=SettingsAgent(),
        settings_store=SettingsStore(tmp_path / "config.json"),
        pull_manager=manager,
    )) as client:
        models = client.get("/api/ollama/models")
        assert models.status_code == 200
        assert models.json()["installed"][1]["name"] == "qwen3:8b"
        assert [item["name"] for item in models.json()["recommended"]] == [
            "qwen3:4b", "qwen3:8b", "qwen3:14b"
        ]
        started = client.post("/api/ollama/pull", json={"model": "qwen3:14b"})
        assert started.status_code == 202
        assert client.get("/api/ollama/pull/status").json() == {
            "model": "qwen3:14b", "status": "pulling", "percent": 0
        }
        assert client.post("/api/ollama/pull", json={"model": "qwen3:8b"}).status_code == 409
