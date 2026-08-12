"""Bamai FastAPI 应用、REST/WS 路由与静态文件服务。"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import time
from contextlib import asynccontextmanager
from pathlib import Path
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
from .agent.prompts import build_health_explanation_prompt, build_probe_explanation_prompt
from .agent.tools import ToolExecutor
from .apps import build_app_overview, group_captured_processes, list_application_processes
from .collector import Collector, list_processes
from .db import METRIC_COLUMNS, Database
from .localization import LanguageState
from .model_pull import RECOMMENDED_MODELS, ModelPullManager
from .rules import HealthEvaluator, RuleEngine
from .search.files import find_large_files, mdfind_search
from .settings import SettingsError, SettingsStore
from .toolbox.base import ProbeValidationError
from .toolbox.jobs import ProbeJobManager
from .toolbox.registry import ProbeRegistry, build_registry

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


EXPLAIN_TIMEOUT_MESSAGES = {
    "zh": "AI 解读超时。模型可能正在加载（闲置后需重新载入内存），请稍后重试。",
    "en": (
        "AI explanation timed out. The model may still be loading "
        "after being idle — please try again."
    ),
}


async def auto_pull_missing_model(agent: OllamaClient, pull_manager: ModelPullManager) -> None:
    """Ollama 可达但配置的模型缺失时，自动开始后台下载（进度可在设置页查看）。"""
    try:
        status = await agent.status()
    except Exception:  # 探测失败绝不能影响服务启动
        return
    if status["available"] and not status["model_pulled"]:
        if pull_manager.start(status["model"]):
            logger.info(
                "模型 %s 缺失，已自动开始后台下载（BAMAI_AUTO_PULL=0 可关闭）", status["model"]
            )


def create_app(
    database: Database | None = None,
    *,
    collector_enabled: bool | None = None,
    agent_client: OllamaClient | None = None,
    settings_store: SettingsStore | None = None,
    pull_manager: ModelPullManager | None = None,
    toolbox_registry: ProbeRegistry | None = None,
    toolbox_jobs: ProbeJobManager | None = None,
    captures_dir: str | Path = config.CAPTURES_DIR,
) -> FastAPI:
    if collector_enabled is None:
        collector_enabled = os.environ.get("BAMAI_DISABLE_COLLECTOR") != "1"

    @asynccontextmanager
    async def lifespan(application: FastAPI):
        if application.state.db is None:
            config.migrate_legacy_data_dir()
            application.state.db = Database()
        if application.state.toolbox_registry is None:
            application.state.toolbox_registry = build_registry(
                application.state.db, application.state.captures_dir
            )
        if application.state.toolbox_jobs is None:
            application.state.toolbox_jobs = ProbeJobManager(application.state.toolbox_registry)
        if application.state.agent is None:
            application.state.agent = OllamaClient(
                ToolExecutor(
                    application.state.db,
                    probe_registry=application.state.toolbox_registry,
                ),
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
        if collector_enabled and os.environ.get("BAMAI_AUTO_PULL") != "0":
            application.state.auto_pull_task = asyncio.create_task(
                auto_pull_missing_model(application.state.agent, application.state.pull_manager)
            )
        logger.info("Bamai 已启动: http://%s:%s", config.HOST, config.PORT)
        yield
        if application.state.collector is not None:
            application.state.collector.stop()
        if application.state.diagnoser is not None:
            application.state.diagnoser.close()
        if application.state.toolbox_jobs is not None:
            application.state.toolbox_jobs.close()

    application = FastAPI(title=config.APP_NAME, lifespan=lifespan)
    application.state.db = database
    application.state.collector = None
    application.state.agent = agent_client
    application.state.diagnoser = None
    application.state.settings = settings_store or SettingsStore()
    application.state.pull_manager = pull_manager or ModelPullManager()
    application.state.toolbox_registry = toolbox_registry
    application.state.toolbox_jobs = toolbox_jobs
    application.state.captures_dir = Path(captures_dir).expanduser()
    application.state.language = LanguageState(application.state.settings.read()["language"])

    @application.middleware("http")
    async def remember_interface_language(request: Request, call_next):
        requested = request.headers.get("accept-language")
        if requested:
            application.state.language.set(requested)
        return await call_next(request)

    @application.middleware("http")
    async def revalidate_static_assets(request: Request, call_next):
        # 无构建链的 ES 模块靠浏览器启发式缓存，升级后会新旧混载；
        # 强制每次带 ETag 重新验证（304 在本机代价可忽略）。
        response = await call_next(request)
        path = request.url.path
        if path == "/" or path.startswith("/static/"):
            response.headers["Cache-Control"] = "no-cache"
        return response

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
            client = OllamaClient(ToolExecutor(
                active_db(), probe_registry=active_toolbox_registry()
            ), settings_store=application.state.settings)
            application.state.agent = client
        return client

    def active_toolbox_registry() -> ProbeRegistry:
        registry = application.state.toolbox_registry
        if registry is None:
            registry = build_registry(active_db(), application.state.captures_dir)
            application.state.toolbox_registry = registry
        return registry

    def active_toolbox_jobs() -> ProbeJobManager:
        jobs = application.state.toolbox_jobs
        if jobs is None:
            jobs = ProbeJobManager(active_toolbox_registry())
            application.state.toolbox_jobs = jobs
        return jobs

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

    @application.get("/api/apps")
    def apps(
        sort: Literal["cpu", "memory", "network"] = "cpu",
        limit: int = Query(30, ge=1, le=100),
    ) -> dict:
        return {"apps": build_app_overview(
            active_db(), list_application_processes(), sort=sort, limit=limit
        )}

    @application.get("/api/apps/{app}/detail")
    def app_detail(
        app: str,
        window: int = Query(3600, ge=60, le=7 * 24 * 60 * 60),
    ) -> dict:
        db = active_db()
        live_processes = list_application_processes()
        groups = group_captured_processes(live_processes)
        pids = next(
            (set(group["pids"]) for group in groups if group["app"] == app),
            set(),
        )
        processes = [
            {
                "name": row["name"],
                "pid": row["pid"],
                "cpu_percent": row["cpu_percent"],
                "memory_rss": row["memory_rss"],
                "cmdline": row.get("cmdline", ""),
            }
            for row in live_processes if row["pid"] in pids
        ]
        processes.sort(key=lambda row: (row["cpu_percent"], row["memory_rss"]), reverse=True)
        end = int(time.time())
        connections = [
            {
                "domain": row["domain"],
                "remote_ip": row["remote_ip"],
                "port": row["remote_port"],
                "up_bps": row["up_bps"],
                "down_bps": row["down_bps"],
                "rtt_ms": row["rtt_ms"],
                "via_proxy": row["via_proxy"],
                "proxy_name": row["proxy_name"],
            }
            for row in db.latest_app_connections(app)
        ]
        return {
            "app": app,
            "processes": processes,
            "history": db.app_history(app, end - window, end),
            "process_names": db.app_process_names(app, end - window, end),
            "connections": connections,
        }

    @application.get("/api/apps/{app}/process-history")
    def app_process_history(
        app: str,
        name: str = Query(..., min_length=1, max_length=200),
        window: int = Query(3600, ge=60, le=7 * 24 * 60 * 60),
    ) -> dict:
        end = int(time.time())
        return {
            "app": app,
            "name": name,
            "history": active_db().app_process_history(
                app, name, end - window, end
            ),
        }

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
                raise HTTPException(
                    status_code=503, detail="无法连接 Ollama 以验证模型"
                ) from exc
            # Ollama 中不带 tag 的名字等价于 :latest（如 gemma3 == gemma3:latest），校验时同样等价
            if requested_model not in installed and f"{requested_model}:latest" not in installed:
                raise HTTPException(
                    status_code=422, detail="model 仅可选择已安装的 Ollama 模型"
                )
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

    @application.post("/api/ollama/delete")
    async def delete_model(payload: PullRequest) -> dict:
        # 停用（切换到其他模型）不删文件；删除是显式操作，且不允许删当前使用中的模型
        if payload.model == application.state.settings.read()["model"]:
            raise HTTPException(
                status_code=422, detail="不能删除当前使用中的模型，请先切换到其他模型"
            )
        try:
            installed = {item["name"] for item in await active_agent().list_models()}
        except (httpx.HTTPError, OllamaError) as exc:
            raise HTTPException(status_code=503, detail="Ollama 未就绪") from exc
        if payload.model not in installed and f"{payload.model}:latest" not in installed:
            raise HTTPException(status_code=404, detail="模型未安装")
        try:
            await active_agent().delete_model(payload.model)
        except (httpx.HTTPError, OllamaError) as exc:
            raise HTTPException(status_code=502, detail="Ollama 删除模型失败") from exc
        return {"deleted": payload.model}

    @application.post("/api/ollama/pull", status_code=202)
    def pull_model(payload: PullRequest) -> dict:
        if not application.state.pull_manager.start(payload.model):
            raise HTTPException(status_code=409, detail="已有模型正在下载")
        return application.state.pull_manager.status()

    @application.get("/api/ollama/pull/status")
    def pull_status() -> dict:
        return application.state.pull_manager.status()

    @application.get("/api/toolbox")
    def toolbox_specs() -> dict:
        return {"items": [spec.to_dict() for spec in active_toolbox_registry().all()]}

    @application.post("/api/toolbox/{probe_id}/run", status_code=202)
    def run_probe(probe_id: str, payload: dict) -> dict:
        params = (
            payload["params"]
            if set(payload) == {"params"} and isinstance(payload.get("params"), dict)
            else payload
        )
        try:
            return active_toolbox_jobs().start(probe_id, params)
        except ProbeValidationError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @application.get("/api/toolbox/jobs/{job_id}")
    def probe_job(job_id: str) -> dict:
        job = active_toolbox_jobs().get(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="探测任务不存在")
        return job

    @application.post("/api/toolbox/{probe_id}/explain")
    async def explain_probe(probe_id: str) -> dict:
        try:
            active_toolbox_registry().get(probe_id)
        except ProbeValidationError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        latest = active_toolbox_jobs().latest(probe_id)
        if latest is None:
            raise HTTPException(status_code=404, detail="该工具还没有可解读的结果")
        client = active_agent()
        status = await client.status()
        if not status["available"] or not status["model_pulled"]:
            raise HTTPException(
                status_code=503, detail=f"Ollama 未就绪。请运行：{INSTALL_GUIDE}"
            )
        language = application.state.language.get()
        prompt = build_probe_explanation_prompt(probe_id, latest.to_dict(), language)
        try:
            result = await asyncio.wait_for(
                client.chat([{"role": "user", "content": prompt}], language=language),
                timeout=config.HEALTH_EXPLAIN_TIMEOUT_SECONDS,
            )
        except OllamaUnavailable as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        except TimeoutError as exc:
            raise HTTPException(status_code=504, detail=EXPLAIN_TIMEOUT_MESSAGES[language]) from exc
        except OllamaError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        return {"reply": result.reply}

    @application.get("/api/toolbox/captures/{name}")
    def download_capture(name: str) -> FileResponse:
        if not re.fullmatch(r"[A-Za-z0-9._-]+\.pcap", name):
            raise HTTPException(status_code=404, detail="抓包文件不存在")
        captures = application.state.captures_dir.resolve()
        path = (captures / name).resolve()
        if path.parent != captures or not path.is_file():
            raise HTTPException(status_code=404, detail="抓包文件不存在")
        return FileResponse(
            path, filename=name, media_type="application/vnd.tcpdump.pcap"
        )

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
            raise HTTPException(status_code=504, detail=EXPLAIN_TIMEOUT_MESSAGES[language]) from exc
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
