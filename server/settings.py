"""Runtime settings persisted atomically under the Bamai data directory."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any, Mapping

from . import config

DEFAULT_SETTINGS: dict[str, Any] = {
    "model": config.OLLAMA_MODEL,
    "temperature": config.OLLAMA_TEMPERATURE,
    "num_ctx": config.OLLAMA_CONTEXT_SIZE,
    "language": "zh",
    "max_tool_rounds": config.OLLAMA_MAX_TOOL_ROUNDS,
}


class SettingsError(ValueError):
    """A runtime setting has an invalid name or value."""


class SettingsStore:
    def __init__(self, path: str | Path = config.SETTINGS_PATH) -> None:
        self.path = Path(path).expanduser()

    def read(self) -> dict[str, Any]:
        settings = DEFAULT_SETTINGS.copy()
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return settings
        if not isinstance(payload, dict):
            return settings
        # Ignore unknown or invalid persisted values so a manually edited file
        # cannot prevent monitoring from starting.
        try:
            settings.update(self.validate(payload, partial=True))
        except SettingsError:
            for key, value in payload.items():
                try:
                    settings.update(self.validate({key: value}, partial=True))
                except SettingsError:
                    continue
        return settings

    def update(self, changes: Mapping[str, Any]) -> dict[str, Any]:
        validated = self.validate(changes, partial=True)
        settings = self.read()
        settings.update(validated)
        self.write(settings)
        return settings

    def write(self, settings: Mapping[str, Any]) -> None:
        normalized = DEFAULT_SETTINGS.copy()
        normalized.update(self.validate(settings, partial=False))
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                prefix=f".{self.path.name}.",
                suffix=".tmp",
                dir=self.path.parent,
                delete=False,
            ) as handle:
                temporary = Path(handle.name)
                json.dump(normalized, handle, ensure_ascii=False, indent=2)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
            temporary = None
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    @staticmethod
    def validate(values: Mapping[str, Any], *, partial: bool) -> dict[str, Any]:
        if not isinstance(values, Mapping):
            raise SettingsError("设置必须是 JSON 对象")
        unknown = set(values) - set(DEFAULT_SETTINGS)
        if unknown:
            raise SettingsError(f"不支持的设置字段: {', '.join(sorted(unknown))}")
        if not partial and set(values) != set(DEFAULT_SETTINGS):
            missing = set(DEFAULT_SETTINGS) - set(values)
            raise SettingsError(f"缺少设置字段: {', '.join(sorted(missing))}")

        result: dict[str, Any] = {}
        for key, value in values.items():
            if key == "model":
                if not isinstance(value, str) or not value.strip() or len(value) > 200:
                    raise SettingsError("model 必须是有效的模型名称")
                result[key] = value.strip()
            elif key == "temperature":
                if (
                    isinstance(value, bool)
                    or not isinstance(value, (int, float))
                    or not 0 <= value <= 1
                ):
                    raise SettingsError("temperature 必须在 0 到 1 之间")
                result[key] = float(value)
            elif key == "num_ctx":
                if (
                    isinstance(value, bool)
                    or not isinstance(value, int)
                    or not 512 <= value <= 131_072
                ):
                    raise SettingsError("num_ctx 必须是 512 到 131072 之间的整数")
                result[key] = value
            elif key == "language":
                if value not in {"zh", "en"}:
                    raise SettingsError("language 仅支持 zh 或 en")
                result[key] = value
            elif key == "max_tool_rounds":
                if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 20:
                    raise SettingsError("max_tool_rounds 必须是 1 到 20 之间的整数")
                result[key] = value
        return result
