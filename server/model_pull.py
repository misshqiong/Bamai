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
    # 注意：glm4:9b 与 mistral:7b 虽然都声明 tools 能力，但 Ollama 模板下中文场景实测
    # 均不发出结构化 tool_calls（glm4 声称"无法访问外部系统"并编造数据；mistral 把工具
    # 调用写成代码文本），不能用于 Bamai 的工具循环，故只推荐实测可靠的 qwen3 系列。
    # 新增推荐前必须实测：单工具直连 Ollama + Bamai 完整链路两个测试都要求发出 tool_calls。
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
