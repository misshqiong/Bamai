from __future__ import annotations

import time

from fastapi.testclient import TestClient

from server.agent.ollama_client import ChatResult
from server.main import create_app
from server.toolbox.base import ProbeResult, ProbeSpec
from server.toolbox.jobs import ProbeJobManager
from server.toolbox.registry import ProbeRegistry


class ExplainAgent:
    async def status(self):
        return {"available": True, "model_pulled": True, "model": "qwen3:4b"}

    async def chat(self, messages, language="zh"):
        assert "mock" in messages[0]["content"]
        return ChatResult("这项检查结果正常。", [])


def make_toolbox(authorized=True):
    registry = ProbeRegistry()
    registry.register(ProbeSpec(
        id="mock", name_key="mock.name", desc_key="mock.desc", icon="M",
        params=(), timeout=1, runner=lambda params: ProbeResult(
            {"ok": True}, rows=[{"value": 1}], raw_output="sample"
        ),
    ))
    registry.register(ProbeSpec(
        id="capture", name_key="capture.name", desc_key="capture.desc", icon="C",
        params=(), timeout=1, runner=lambda params: ProbeResult({}),
        needs_authorization=True, authorization_check=lambda: authorized,
    ))
    return registry, ProbeJobManager(registry)


def test_toolbox_api_run_poll_explain_and_capture_download(db, tmp_path):
    registry, jobs = make_toolbox()
    capture_file = tmp_path / "captures" / "123.pcap"
    capture_file.parent.mkdir()
    capture_file.write_bytes(b"pcap-data")
    with TestClient(create_app(
        db, collector_enabled=False, agent_client=ExplainAgent(),
        toolbox_registry=registry, toolbox_jobs=jobs, captures_dir=capture_file.parent,
    )) as client:
        listed = client.get("/api/toolbox")
        assert listed.status_code == 200
        assert [item["id"] for item in listed.json()["items"]] == ["mock", "capture"]
        started = client.post("/api/toolbox/mock/run", json={"params": {}})
        assert started.status_code == 202
        for _ in range(100):
            job = client.get(f"/api/toolbox/jobs/{started.json()['job_id']}").json()
            if job["status"] != "running":
                break
            time.sleep(0.01)
        assert job["status"] == "done" and job["result"]["summary"] == {"ok": True}
        explanation = client.post("/api/toolbox/mock/explain")
        assert explanation.status_code == 200
        assert explanation.json()["reply"] == "这项检查结果正常。"
        downloaded = client.get("/api/toolbox/captures/123.pcap")
        assert downloaded.status_code == 200 and downloaded.content == b"pcap-data"
        assert client.get("/api/toolbox/captures/../data.db").status_code == 404


def test_toolbox_api_validation_unknown_jobs_and_capture_auth(db, tmp_path):
    registry, jobs = make_toolbox(authorized=False)
    with TestClient(create_app(
        db, collector_enabled=False, agent_client=ExplainAgent(),
        toolbox_registry=registry, toolbox_jobs=jobs, captures_dir=tmp_path,
    )) as client:
        assert client.post("/api/toolbox/missing/run", json={"params": {}}).status_code == 422
        assert client.get("/api/toolbox/jobs/missing").status_code == 404
        blocked = client.post("/api/toolbox/capture/run", json={"params": {}})
        assert blocked.status_code == 202
        assert blocked.json()["status"] == "needs_auth"
