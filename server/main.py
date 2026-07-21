"""MacPilot FastAPI 应用、REST/WS 路由与静态文件服务。"""

from __future__ import annotations

import asyncio
import logging
import os
from contextlib import asynccontextmanager
from typing import Literal

from fastapi import FastAPI, HTTPException, Query, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from . import config
from .collector import Collector, list_processes
from .db import Database, METRIC_COLUMNS
from .rules import RuleEngine
from .search.files import find_large_files, mdfind_search


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)
logger = logging.getLogger(__name__)


def create_app(
    database: Database | None = None,
    *,
    collector_enabled: bool | None = None,
) -> FastAPI:
    if collector_enabled is None:
        collector_enabled = os.environ.get("MACPILOT_DISABLE_COLLECTOR") != "1"

    @asynccontextmanager
    async def lifespan(application: FastAPI):
        if application.state.db is None:
            application.state.db = Database()
        if collector_enabled:
            rules = RuleEngine(application.state.db)
            collector = Collector(application.state.db, rules)
            application.state.collector = collector
            collector.start()
        logger.info("MacPilot 已启动: http://%s:%s", config.HOST, config.PORT)
        yield
        if application.state.collector is not None:
            application.state.collector.stop()

    application = FastAPI(title=config.APP_NAME, lifespan=lifespan)
    application.state.db = database
    application.state.collector = None

    def active_db() -> Database:
        db = application.state.db
        if db is None:
            # 仅在绕过 ASGI lifespan 的直接函数调用中作为安全兜底。
            db = Database()
            application.state.db = db
        return db

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
    def overview() -> dict:
        db = active_db()
        top = db.latest_process_snapshots(5)
        if not top:
            top = list_processes("cpu", 5)
        return {
            "metric": db.latest_metric(),
            "disks": db.latest_disk_usage(),
            "top_processes": top,
            "unresolved_events": db.unresolved_event_count(),
            # Phase 1 明确不接入 Ollama，但保持 overview 的既定字段形状。
            "ollama": {"available": False, "model_pulled": False, "model": "qwen3:4b", "phase": 1},
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
