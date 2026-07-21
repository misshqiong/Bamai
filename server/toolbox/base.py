"""Shared probe contracts and strict parameter validation."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Mapping

MAX_RAW_OUTPUT = 8 * 1024


class ProbeValidationError(ValueError):
    """Probe id or parameters are invalid."""


@dataclass(frozen=True)
class ProbeParam:
    name: str
    type: str
    label_key: str
    default: Any = None
    required: bool = False
    min: int | float | None = None
    max: int | float | None = None
    choices: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        payload = {
            "name": self.name,
            "type": self.type,
            "label_key": self.label_key,
            "default": self.default,
            "required": self.required,
        }
        if self.min is not None:
            payload["min"] = self.min
        if self.max is not None:
            payload["max"] = self.max
        if self.choices:
            payload["choices"] = list(self.choices)
        return payload


@dataclass
class ProbeResult:
    summary: dict[str, Any]
    rows: list[dict[str, Any]] | None = None
    raw_output: str = ""
    artifacts: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        encoded = self.raw_output.encode("utf-8")
        raw_output = (
            self.raw_output if len(encoded) <= MAX_RAW_OUTPUT
            else encoded[:MAX_RAW_OUTPUT].decode("utf-8", errors="ignore")
        )
        return {
            "summary": self.summary,
            "rows": self.rows,
            "raw_output": raw_output,
            "artifacts": self.artifacts,
        }


ProbeRunner = Callable[[dict[str, Any]], ProbeResult]
ParameterValidator = Callable[[dict[str, Any]], None]
AuthorizationCheck = Callable[[], bool]


@dataclass(frozen=True)
class ProbeSpec:
    id: str
    name_key: str
    desc_key: str
    params: tuple[ProbeParam, ...]
    timeout: int
    runner: ProbeRunner
    icon: str = "•"
    needs_authorization: bool = False
    validator: ParameterValidator | None = None
    authorization_check: AuthorizationCheck | None = None

    def __post_init__(self) -> None:
        normalized = tuple(
            item if isinstance(item, ProbeParam) else ProbeParam(**item)
            for item in self.params
        )
        object.__setattr__(self, "params", normalized)

    def validate(self, values: Mapping[str, Any] | None) -> dict[str, Any]:
        if values is None:
            values = {}
        if not isinstance(values, Mapping):
            raise ProbeValidationError("params 必须是对象")
        definitions = {item.name: item for item in self.params}
        extra = set(values) - set(definitions)
        if extra:
            raise ProbeValidationError(f"不支持的参数: {', '.join(sorted(extra))}")
        result: dict[str, Any] = {}
        for name, definition in definitions.items():
            if name not in values:
                if definition.required and definition.default is None:
                    raise ProbeValidationError(f"缺少参数: {name}")
                if definition.default is not None:
                    result[name] = definition.default
                continue
            value = values[name]
            if definition.type in {"str", "choice"}:
                if not isinstance(value, str):
                    raise ProbeValidationError(f"{name} 必须是字符串")
                value = value.strip()
                if not value and (definition.required or definition.type == "choice"):
                    raise ProbeValidationError(f"{name} 必须是非空字符串")
                if definition.max is not None and len(value) > definition.max:
                    raise ProbeValidationError(f"{name} 超出允许长度")
                if definition.type == "choice" and value not in definition.choices:
                    raise ProbeValidationError(f"{name} 的值不受支持")
            elif definition.type == "int":
                if isinstance(value, bool) or not isinstance(value, int):
                    raise ProbeValidationError(f"{name} 必须是整数")
                if definition.min is not None and value < definition.min:
                    raise ProbeValidationError(f"{name} 超出允许范围")
                if definition.max is not None and value > definition.max:
                    raise ProbeValidationError(f"{name} 超出允许范围")
            else:
                raise ProbeValidationError(f"未知参数类型: {definition.type}")
            result[name] = value
        if self.validator is not None:
            self.validator(result)
        return result

    def authorized(self) -> bool:
        if not self.needs_authorization:
            return True
        return bool(self.authorization_check and self.authorization_check())

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name_key": self.name_key,
            "desc_key": self.desc_key,
            "icon": self.icon,
            "params": [item.to_dict() for item in self.params],
            "timeout": self.timeout,
            "needs_authorization": self.needs_authorization,
            "authorized": self.authorized(),
        }
