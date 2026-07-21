"""Bamai FastAPI 应用、REST/WS 路由与静态文件服务。"""

from __future__ import annotations

import asyncio
import logging
import os
from contextlib import asynccontextmanager
from typing import Literal

import httpx
from fastapi import FastAPI, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field

from . import config
from .agent.ollama_client import (
    INSTALL_GUIDE,
    EventDiagnoser,
    OllamaClient,
    OllamaError,
    OllamaUnavailable,
)
from .agent.prompts import build_health_explanation_prompt
from .agent.tools import ToolExecutor
from .collector import Collector, list_processes
from .db import Database, METRIC_COLUMNS
from .localization import LanguageState
from .model_pull import ModelPullManager, RECOMMENDED_MODELS
from .rules import HealthEvaluator, RuleEngine
from .search.files import find_large_files, mdfind_search
from .settings import SettingsError, SettingsStore


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)
logger = logging.getLogger(__name__)


class ChatMessage(BaseModel):
    role: Literal["user", "assistant"]
    content: str = Field(min_length=1, max_length=20_000)


class ChatRequest(BaseModel):
    messages: list[ChatMessage] = Field(min_length=1, max_length=50)


class SettingsPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    model: str | None = Field(default=None, min_length=1, max_length=200)
    temperature: float | None = Field(default=None, ge=0, le=1)
    num_ctx: int | None = Field(default=None, ge=512, le=131_072)
    language: Literal["zh", "en"] | None = None
    max_tool_rounds: int | None = Field(default=None, ge=1, le=20)


class PullRequest(BaseModel):
    model: str = Field(min_length=1, max_length=200)


def create_app(
    database: Database | None = None,
    *,
    collector_enabled: bool | None = None,
    agent_client: OllamaClient | None = None,
    settings_store: SettingsStore | None = None,
    pull_manager: ModelPullManager | None = None,
) -> FastAPI:
    if collector_enabled is None:
        collector_enabled = os.environ.get("BAMAI_DISABLE_COLLECTOR") != "1"

    @asynccontextmanager
    async def lifespan(application: FastAPI):
        if application.state.db is None:
            config.migrate_legacy_data_dir()
            application.state.db = Database()
        if application.state.agent is None:
            application.state.agent = OllamaClient(
                ToolExecutor(application.state.db),
                settings_store=application.state.settings,
            )
        if collector_enabled:
            diagnoser = EventDiagnoser(
                application.state.db, application.state.agent,
                language_provider=application.state.language.get,
            )
            application.state.diagnoser = diagnoser
            rules = RuleEngine(
                application.state.db, diagnoser=diagnoser.schedule,
                language_provider=application.state.language.get,
            )
            collector = Collector(application.state.db, rules)
            application.state.collector = collector
            collector.start()
        logger.info("Bamai 已启动: http://%s:%s", config.HOST, config.PORT)
        yield
        if application.state.collector is not None:
            application.state.collector.stop()
        if application.state.diagnoser is not None:
            application.state.diagnoser.close()

    application = FastAPI(title=config.APP_NAME, lifespan=lifespan)
    application.state.db = database
    application.state.collector = None
    application.state.agent = agent_client
    application.state.diagnoser = None
    application.state.settings = settings_store or SettingsStore()
    application.state.pull_manager = pull_manager or ModelPullManager()
    application.state.language = LanguageState(application.state.settings.read()["language"])

    @application.middleware("http")
    async def remember_interface_language(request: Request, call_next):
        requested = request.headers.get("accept-language")
        if requested:
            application.state.language.set(requested)
        return await call_next(request)

    def active_db() -> Database:
        db = application.state.db
        if db is None:
            # 仅在绕过 ASGI lifespan 的直接函数调用中作为安全兜底。
            db = Database()
            application.state.db = db
        return db

    def active_agent() -> OllamaClient:
        client = application.state.agent
        if client is None:
            client = OllamaClient(
                ToolExecutor(active_db()), settings_store=application.state.settings
            )
            application.state.agent = client
        return client

    @application.get("/", include_in_schema=False)
    def index() -> FileResponse:
        return FileResponse(config.WEB_DIR / "index.html")

    @application.websocket("/ws/realtime")
    async def realtime(websocket: WebSocket) -> None:
        await websocket.accept()
        try:
            while True:
                metric = active_db().latest_metric()
                if metric is not None:
                    await websocket.send_json(metric)
                await asyncio.sleep(config.WEBSOCKET_INTERVAL_SECONDS)
        except (WebSocketDisconnect, RuntimeError):
            return

    @application.get("/api/overview")
    async def overview() -> dict:
        db = active_db()
        top = db.latest_process_snapshots(5)
        if not top:
            top = list_processes("cpu", 5)
        ollama = await active_agent().status()
        return {
            "metric": db.latest_metric(),
            "disks": db.latest_disk_usage(),
            "top_processes": top,
            "unresolved_events": db.unresolved_event_count(),
            "ollama": ollama,
        }

    @application.get("/api/metrics")
    def metrics(
        metric: str,
        start: int,
        end: int,
    ) -> dict:
        if metric not in METRIC_COLUMNS:
            raise HTTPException(status_code=422, detail=f"不支持的指标: {metric}")
        try:
            points = active_db().query_metrics(metric, start, end)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return {"metric": metric, "start": start, "end": end, "points": points}

    @application.get("/api/processes")
    def processes(
        sort: Literal["cpu", "memory"] = "cpu",
        limit: int = Query(20, ge=1, le=100),
    ) -> dict:
        return {"items": list_processes(sort, limit), "sort": sort}

    @application.get("/api/processes/net")
    def processes_net() -> dict:
        items = active_db().latest_process_net()
        return {"items": items, "ts": items[0]["ts"] if items else None}

    @application.get("/api/events")
    def events(limit: int = Query(50, ge=1, le=200)) -> dict:
        return {"items": active_db().list_events(limit)}

    @application.get("/api/ollama/status")
    async def ollama_status() -> dict:
        return await active_agent().status()

    @application.get("/api/settings")
    def get_settings() -> dict:
        return application.state.settings.read()

    @application.post("/api/settings")
    async def update_settings(payload: SettingsPatch) -> dict:
        changes = payload.model_dump(exclude_none=True)
        requested_model = changes.get("model")
        if requested_model is not None:
            try:
                installed = {item["name"] for item in await active_agent().list_models()}
            except (httpx.HTTPError, OllamaError) as exc:
                raise HTTPException(status_code=503, detail="无法连接 Ollama 以验证模型") from exc
            if requested_model not in installed:
                raise HTTPException(status_code=422, detail="model 仅可选择已安装的 Ollama 模型")
        try:
            settings = application.state.settings.update(changes)
        except SettingsError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        application.state.language.set(settings["language"])
        return settings

    @application.get("/api/ollama/models")
    async def ollama_models() -> dict:
        try:
            installed = await active_agent().list_models()
        except (httpx.HTTPError, OllamaError) as exc:
            raise HTTPException(status_code=503, detail="Ollama 未就绪") from exc
        return {"installed": installed, "recommended": RECOMMENDED_MODELS}

    @application.post("/api/ollama/pull", status_code=202)
    def pull_model(payload: PullRequest) -> dict:
        if not application.state.pull_manager.start(payload.model):
            raise HTTPException(status_code=409, detail="已有模型正在下载")
        return application.state.pull_manager.status()

    @application.get("/api/ollama/pull/status")
    def pull_status() -> dict:
        return application.state.pull_manager.status()

    @application.post("/api/chat")
    async def chat(payload: ChatRequest, request: Request) -> dict:
        client = active_agent()
        status = await client.status()
        if not status["available"] or not status["model_pulled"]:
            raise HTTPException(
                status_code=503,
                detail=f"Ollama 或模型 {status['model']} 未就绪。请运行：{INSTALL_GUIDE}",
            )
        messages = [message.model_dump() for message in payload.messages]
        language = application.state.language.get()
        try:
            result = await asyncio.wait_for(
                client.chat(messages, language=language),
                timeout=config.OLLAMA_CHAT_TIMEOUT_SECONDS,
            )
        except OllamaUnavailable as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        except TimeoutError as exc:
            raise HTTPException(status_code=504, detail="Ollama 回答超时（120 秒）") from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except OllamaError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        return {"reply": result.reply, "tool_trace": result.tool_trace}

    @application.get("/api/health")
    def health() -> dict:
        return HealthEvaluator(active_db()).evaluate()

    @application.post("/api/health/explain")
    async def explain_health(request: Request) -> dict:
        client = active_agent()
        status = await client.status()
        if not status["available"] or not status["model_pulled"]:
            raise HTTPException(
                status_code=503,
                detail=f"Ollama 或模型 {status['model']} 未就绪。请运行：{INSTALL_GUIDE}",
            )
        language = application.state.language.get()
        current_health = HealthEvaluator(active_db()).evaluate()
        prompt = build_health_explanation_prompt(current_health, language)
        try:
            result = await asyncio.wait_for(
                client.chat([{"role": "user", "content": prompt}], language=language),
                timeout=config.HEALTH_EXPLAIN_TIMEOUT_SECONDS,
            )
        except OllamaUnavailable as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        except TimeoutError as exc:
            raise HTTPException(status_code=504, detail="AI health explanation timed out") from exc
        except OllamaError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        return {"reply": result.reply}

    @application.get("/api/search/files")
    def search_files(
        q: str = Query(min_length=1),
        kind: Literal["name", "content"] = "name",
        limit: int = Query(20, ge=1, le=100),
    ) -> dict:
        try:
            return {"items": mdfind_search(q, kind, limit)}
        except TimeoutError as exc:
            raise HTTPException(status_code=504, detail=str(exc)) from exc
        except (RuntimeError, ValueError) as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc

    @application.get("/api/search/large-files")
    def large_files(
        path: str = "~",
        min_mb: float = Query(100, ge=0),
        limit: int = Query(50, ge=1, le=500),
    ) -> dict:
        try:
            return find_large_files(path, min_mb, limit)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    application.mount("/static", StaticFiles(directory=config.WEB_DIR), name="static")
    return application


app = create_app()
