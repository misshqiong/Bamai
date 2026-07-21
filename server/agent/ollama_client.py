"""Ollama HTTP 客户端、工具调用循环和事件后台诊断。"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Sequence

import httpx

from .. import config
from ..db import Database
from ..settings import SettingsStore
from .prompts import build_event_diagnosis_prompt, build_system_prompt
from .tools import TOOL_DEFINITIONS, ToolExecutor

logger = logging.getLogger(__name__)
INSTALL_GUIDE = "brew install ollama && ollama pull qwen3:4b"
THINK_PATTERN = re.compile(r"<think\b[^>]*>.*?</think>", re.IGNORECASE | re.DOTALL)


class OllamaUnavailable(RuntimeError):
    """Ollama 服务或指定模型当前不可用。"""


class OllamaError(RuntimeError):
    """Ollama 返回了无法处理的响应。"""


@dataclass(frozen=True)
class ChatResult:
    reply: str
    tool_trace: list[dict[str, Any]]


class OllamaClient:
    def __init__(
        self,
        tools: ToolExecutor,
        *,
        base_url: str = config.OLLAMA_BASE_URL,
        model: str | None = None,
        settings_store: SettingsStore | None = None,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self.tools = tools
        self.base_url = base_url.rstrip("/")
        self._model_override = model
        self.settings_store = settings_store or SettingsStore()
        self.http_client = http_client

    def current_settings(self) -> dict[str, Any]:
        """Read from disk on every use so changes need no process restart."""
        settings = self.settings_store.read()
        if self._model_override is not None:
            settings["model"] = self._model_override
        return settings

    @property
    def model(self) -> str:
        return str(self.current_settings()["model"])

    async def list_models(self) -> list[dict[str, Any]]:
        response = await self._request(
            "GET", "/api/tags", timeout=config.OLLAMA_HEALTH_TIMEOUT_SECONDS
        )
        payload = response.json()
        models = payload.get("models") if isinstance(payload, dict) else None
        if not isinstance(models, list):
            raise OllamaError("Ollama tags 响应格式无效")
        result = []
        for item in models:
            if not isinstance(item, dict):
                continue
            name = item.get("name") or item.get("model")
            if isinstance(name, str) and name:
                result.append({"name": name, "size": int(item.get("size") or 0)})
        return result

    async def status(self) -> dict[str, Any]:
        model = self.model
        try:
            models = await self.list_models()
            names = {item["name"] for item in models}
            return {
                "available": True,
                "model_pulled": model in names,
                "model": model,
            }
        except (httpx.HTTPError, OllamaError, ValueError, TypeError):
            return {"available": False, "model_pulled": False, "model": model}

    async def chat(
        self, messages: Sequence[Mapping[str, Any]], language: str = "zh"
    ) -> ChatResult:
        conversation = [{"role": "system", "content": build_system_prompt(language=language)}]
        conversation.extend(self._clean_messages(messages))
        trace: list[dict[str, Any]] = []

        rounds = 0
        while rounds < int(self.current_settings()["max_tool_rounds"]):
            rounds += 1
            message = await self._chat_request(conversation, tools=TOOL_DEFINITIONS)
            tool_calls = message.get("tool_calls") or []
            if not tool_calls:
                return ChatResult(self._reply_content(message), trace)
            conversation.append(
                {
                    "role": "assistant",
                    "content": message.get("content") or "",
                    "tool_calls": tool_calls,
                }
            )
            for call in tool_calls:
                function = call.get("function") or {}
                name = function.get("name") or ""
                arguments = self._arguments(function.get("arguments"))
                try:
                    # 文件扫描等工具可能运行数十秒，不能阻塞 FastAPI 的 WebSocket 事件循环。
                    result = await asyncio.to_thread(self.tools.execute, name, arguments)
                    content = result.as_json()
                    summary = result.summary
                except Exception as exc:
                    content = json.dumps({"error": str(exc)}, ensure_ascii=False)
                    summary = f"工具 {name or '未知'} 执行失败：{exc}"
                trace.append({"tool": name, "args": arguments, "summary": summary})
                conversation.append({"role": "tool", "tool_name": name, "content": content})

        conversation.append(
            {
                "role": "system",
                "content": (
                    f"工具调用已达到 {rounds} 轮上限。请停止调用工具，"
                    "基于已有结果直接回答；数据不足时明确说明。"
                ),
            }
        )
        final_message = await self._chat_request(conversation, tools=None)
        return ChatResult(self._reply_content(final_message), trace)

    async def _chat_request(self, messages: list[dict], tools: list[dict] | None) -> dict:
        settings = self.current_settings()
        payload: dict[str, Any] = {
            "model": settings["model"],
            "messages": messages,
            "stream": False,
            "think": False,
            "options": {
                "temperature": settings["temperature"],
                "num_ctx": settings["num_ctx"],
            },
        }
        if tools is not None:
            payload["tools"] = tools
        try:
            response = await self._request(
                "POST",
                "/api/chat",
                json=payload,
                timeout=config.OLLAMA_CHAT_TIMEOUT_SECONDS,
            )
        except (httpx.ConnectError, httpx.ConnectTimeout, httpx.ReadTimeout) as exc:
            raise OllamaUnavailable(f"Ollama 不可用。请运行：{INSTALL_GUIDE}") from exc
        except httpx.HTTPError as exc:
            raise OllamaError(f"Ollama 请求失败: {exc}") from exc
        try:
            message = response.json()["message"]
        except (ValueError, KeyError, TypeError) as exc:
            raise OllamaError("Ollama 返回了无效响应") from exc
        if not isinstance(message, dict):
            raise OllamaError("Ollama 响应中缺少 message")
        return message

    async def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        if self.http_client is not None:
            response = await self.http_client.request(method, f"{self.base_url}{path}", **kwargs)
        else:
            async with httpx.AsyncClient() as client:
                response = await client.request(method, f"{self.base_url}{path}", **kwargs)
        response.raise_for_status()
        return response

    @staticmethod
    def _clean_messages(messages: Sequence[Mapping[str, Any]]) -> list[dict[str, str]]:
        cleaned = []
        for message in messages:
            role = message.get("role")
            content = message.get("content")
            if (
                role not in {"user", "assistant"}
                or not isinstance(content, str)
                or not content.strip()
            ):
                raise ValueError("聊天消息必须包含有效的 user/assistant role 和 content")
            cleaned.append({"role": role, "content": content.strip()})
        if not cleaned:
            raise ValueError("messages 不能为空")
        return cleaned

    @staticmethod
    def _arguments(value: Any) -> dict[str, Any]:
        if value is None:
            return {}
        if isinstance(value, dict):
            return value
        if isinstance(value, str):
            try:
                parsed = json.loads(value)
            except json.JSONDecodeError:
                return {"_invalid_json": value}
            return parsed if isinstance(parsed, dict) else {"_invalid_arguments": parsed}
        return {"_invalid_arguments": value}

    @staticmethod
    def _reply_content(message: Mapping[str, Any]) -> str:
        content = message.get("content")
        if not isinstance(content, str):
            raise OllamaError("Ollama 最终回复缺少文本内容")
        reply = THINK_PATTERN.sub("", content)
        # qwen3 经 Ollama 输出时可能缺失开头的 <think> 标签，只留结尾 </think>，
        # 此时成对匹配的正则不生效，直接丢弃最后一个 </think> 之前的全部内容。
        if "</think>" in reply:
            reply = reply.rsplit("</think>", 1)[1]
        reply = reply.strip()
        return reply or "Ollama 未返回可显示的内容。"


class EventDiagnoser:
    """用独立线程运行异步 Agent，避免规则评估阻塞采集线程。"""

    def __init__(
        self,
        db: Database,
        client: OllamaClient,
        language_provider: Callable[[], str] = lambda: "zh",
    ) -> None:
        self.db = db
        self.client = client
        self.language_provider = language_provider
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="bamai-ai")

    def schedule(self, event_id: int, event: dict[str, Any]) -> Future:
        return self._executor.submit(self._run, event_id, event)

    def _run(self, event_id: int, event: dict[str, Any]) -> None:
        asyncio.run(self.diagnose(event_id, event))

    async def diagnose(self, event_id: int, event: dict[str, Any]) -> None:
        try:
            status = await self.client.status()
            if not status["available"] or not status["model_pulled"]:
                return
            language = event.get("language") or self.language_provider()
            result = await self.client.chat(
                [
                    {
                        "role": "user",
                        "content": build_event_diagnosis_prompt(event, language),
                    }
                ],
                language=language,
            )
            self.db.update_event_ai_analysis(event_id, result.reply)
        except Exception as exc:  # AI 诊断绝不能影响事件和采集线程
            logger.warning("事件 %s 的 AI 诊断失败: %s", event_id, exc)

    def close(self) -> None:
        self._executor.shutdown(wait=False, cancel_futures=True)
