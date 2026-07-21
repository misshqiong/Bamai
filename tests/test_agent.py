from __future__ import annotations

import asyncio
import json

import httpx

from server.agent.ollama_client import ChatResult, EventDiagnoser, OllamaClient
from server.agent.tools import ToolResult


class FakeTools:
    def __init__(self):
        self.calls = []

    def execute(self, name, arguments):
        self.calls.append((name, arguments))
        return ToolResult({"cpu_percent": 37}, "查询了当前系统状态")


def test_ollama_tool_loop_sends_think_false_and_strips_think_tags():
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        requests.append(payload)
        if len(requests) == 1:
            return httpx.Response(200, json={"message": {
                "role": "assistant", "content": "",
                "tool_calls": [{"function": {
                    "name": "get_current_stats", "arguments": {},
                }}],
            }})
        assert payload["messages"][-1]["role"] == "tool"
        return httpx.Response(200, json={"message": {
            "role": "assistant", "content": "<think>内部推理不可见</think>当前 CPU 为 37%。",
        }})

    async def scenario():
        tools = FakeTools()
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
            client = OllamaClient(tools, http_client=http_client)
            result = await client.chat([{"role": "user", "content": "CPU 如何？"}])
        return tools, result

    tools, result = asyncio.run(scenario())
    assert result.reply == "当前 CPU 为 37%。"
    assert result.tool_trace == [{
        "tool": "get_current_stats", "args": {}, "summary": "查询了当前系统状态"
    }]
    assert tools.calls == [("get_current_stats", {})]
    assert requests[0]["think"] is False
    assert requests[0]["stream"] is False
    assert requests[0]["options"] == {"temperature": 0.7, "num_ctx": 8192}
    assert len(requests[0]["tools"]) == 8


def test_ollama_forces_direct_answer_after_six_tool_rounds():
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        requests.append(payload)
        if len(requests) <= 6:
            return httpx.Response(200, json={"message": {
                "role": "assistant", "content": "",
                "tool_calls": [{"function": {"name": "get_current_stats", "arguments": "{}"}}],
            }})
        assert "tools" not in payload
        return httpx.Response(200, json={"message": {
            "role": "assistant", "content": "基于已有数据直接回答。",
        }})

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
            return await OllamaClient(FakeTools(), http_client=http_client).chat([
                {"role": "user", "content": "持续检查"}
            ])

    result = asyncio.run(scenario())
    assert result.reply == "基于已有数据直接回答。"
    assert len(result.tool_trace) == 6
    assert len(requests) == 7


def test_ollama_status_uses_mocked_tags_endpoint():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/tags"
        return httpx.Response(200, json={"models": [{"name": "qwen3:4b"}]})

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
            return await OllamaClient(FakeTools(), http_client=http_client).status()

    assert asyncio.run(scenario()) == {
        "available": True, "model_pulled": True, "model": "qwen3:4b"
    }


def test_event_diagnosis_updates_database_without_real_ollama(db):
    event_id = db.create_event(100, "cpu_high", "warning", "CPU 高", "详情")

    class FakeAgent:
        async def status(self):
            return {"available": True, "model_pulled": True, "model": "qwen3:4b"}

        async def chat(self, messages):
            assert "cpu_high" in messages[0]["content"]
            return ChatResult("CPU 高负载可能由编译任务造成，建议先观察进程列表。", [])

    diagnoser = EventDiagnoser(db, FakeAgent())
    try:
        asyncio.run(diagnoser.diagnose(event_id, {
            "ts": 100, "kind": "cpu_high", "severity": "warning",
            "title": "CPU 高", "detail": "详情",
        }))
    finally:
        diagnoser.close()
    assert db.list_events()[0]["ai_analysis"].startswith("CPU 高负载")
