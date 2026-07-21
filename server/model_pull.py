"""Background Ollama model downloads and their polling state."""

from __future__ import annotations

import json
import logging
import threading
from typing import Any

import httpx

from . import config

logger = logging.getLogger(__name__)

RECOMMENDED_MODELS = [
    {"name": "qwen3:4b", "size_gb": 3, "memory_gb": 8},
    {"name": "qwen3:8b", "size_gb": 6, "memory_gb": 16},
    {"name": "qwen3:14b", "size_gb": 10, "memory_gb": 24},
    {"name": "glm4:9b", "size_gb": 6, "memory_gb": 16},
    {"name": "mistral:7b", "size_gb": 4, "memory_gb": 8},
]


class ModelPullManager:
    def __init__(self, base_url: str = config.OLLAMA_BASE_URL) -> None:
        self.base_url = base_url.rstrip("/")
        self._lock = threading.Lock()
        self._state: dict[str, Any] = {"model": None, "status": "idle", "percent": 0}

    def status(self) -> dict[str, Any]:
        with self._lock:
            return self._state.copy()

    def start(self, model: str) -> bool:
        with self._lock:
            if self._state["status"] == "pulling":
                return False
            self._state = {"model": model, "status": "pulling", "percent": 0}
        threading.Thread(
            target=self._pull,
            args=(model,),
            name="bamai-model-pull",
            daemon=True,
        ).start()
        return True

    def _set(self, status: str, percent: int) -> None:
        with self._lock:
            self._state["status"] = status
            self._state["percent"] = max(0, min(100, percent))

    def _pull(self, model: str) -> None:
        try:
            with httpx.stream(
                "POST",
                f"{self.base_url}/api/pull",
                json={"model": model, "stream": True},
                timeout=None,
            ) as response:
                response.raise_for_status()
                for line in response.iter_lines():
                    if not line:
                        continue
                    payload = json.loads(line)
                    total = payload.get("total") or 0
                    completed = payload.get("completed") or 0
                    if total:
                        self._set("pulling", round(completed * 100 / total))
            self._set("done", 100)
        except Exception as exc:
            logger.warning("Ollama 模型 %s 下载失败: %s", model, exc)
            self._set("error", self.status()["percent"])
