"""Agent 工具定义、参数校验与执行分发。"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable, Mapping

from ..apps import build_app_overview, list_application_processes
from ..collector import list_processes, search_running_processes
from ..db import METRIC_COLUMNS, Database
from ..search.files import find_large_files, mdfind_search
from ..toolbox.registry import ProbeRegistry, build_registry


def _function(name: str, description: str, properties: dict, required: list[str]) -> dict:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {
                "type": "object",
                "properties": properties,
                "required": required,
                "additionalProperties": False,
            },
        },
    }


AGENT_PROBE_ALLOWLIST = {
    "ping",
    "dns",
    "port",
    "wifi",
    "battery",
    "memory_check",
    "traceroute",
    "netquality",
    "http_timing",
    "whois_lookup",
    "tls_check",
}


TOOL_DEFINITIONS = [
    _function("get_current_stats", "获取当前 CPU、内存、磁盘和网络快照。", {}, []),
    _function("get_top_processes", "获取当前最消耗指定资源的进程。", {
        "sort_by": {"type": "string", "enum": ["cpu", "memory", "network"]},
        "limit": {"type": "integer", "minimum": 1, "maximum": 15},
    }, ["sort_by", "limit"]),
    _function("query_metrics", "查询一段时间内的系统指标历史和摘要。", {
        "metric": {"type": "string", "enum": sorted(METRIC_COLUMNS)},
        "start_ts": {"type": "integer"}, "end_ts": {"type": "integer"},
    }, ["metric", "start_ts", "end_ts"]),
    _function(
        "get_process_history",
        "聚合一段时间内的进程快照，找出资源消耗者。",
        {
            "start_ts": {"type": "integer"},
            "end_ts": {"type": "integer"},
            "name": {"type": "string", "description": "可选的进程名模糊过滤"},
        },
        ["start_ts", "end_ts"],
    ),
    _function("get_events", "查询最近的系统告警事件。", {
        "limit": {"type": "integer", "minimum": 1, "maximum": 50},
        "since_ts": {"type": "integer", "description": "可选的起始 Unix 秒"},
    }, ["limit"]),
    _function("find_large_files", "扫描指定目录中的大文件。", {
        "path": {"type": "string"}, "min_mb": {"type": "number", "minimum": 0},
        "limit": {"type": "integer", "minimum": 1, "maximum": 30},
    }, ["path", "min_mb", "limit"]),
    _function("search_files", "按文件名或 Spotlight 内容索引搜索本机文件。", {
        "query": {"type": "string"},
        "kind": {"type": "string", "enum": ["name", "content"]},
        "limit": {"type": "integer", "minimum": 1, "maximum": 20},
    }, ["query", "kind", "limit"]),
    _function("search_processes", "按名称或命令行关键词搜索运行中的进程。", {
        "keyword": {"type": "string"},
    }, ["keyword"]),
    _function("get_app_overview", "获取按应用聚合的当前资源占用（CPU、内存、网络、进程数）。", {
        "limit": {"type": "integer", "minimum": 1, "maximum": 30},
    }, ["limit"]),
    _function("get_app_history", "查询某应用（或其某个子进程）一段时间的 CPU、内存历史。", {
        "app": {"type": "string"},
        "start_ts": {"type": "integer"},
        "end_ts": {"type": "integer"},
        "process_name": {"type": "string", "description": "可选的子进程名称"},
    }, ["app", "start_ts", "end_ts"]),
    _function("get_app_connections", "查询某应用最近的网络连接、流量、RTT 和代理标记。", {
        "app": {"type": "string"},
    }, ["app"]),
    _function("run_probe", "运行一个安全的本机网络或系统诊断探测。", {
        "probe_id": {"type": "string", "enum": sorted(AGENT_PROBE_ALLOWLIST)},
        "params": {"type": "object", "additionalProperties": True},
    }, ["probe_id", "params"]),
]


class ToolError(ValueError):
    """模型工具名或参数不合法。"""


@dataclass(frozen=True)
class ToolResult:
    data: Any
    summary: str

    def as_json(self) -> str:
        return json.dumps(self.data, ensure_ascii=False, separators=(",", ":"))


class ToolExecutor:
    def __init__(
        self,
        db: Database,
        *,
        process_provider: Callable[[str, int], list[dict]] = list_processes,
        process_search: Callable[[str, int], list[dict]] = search_running_processes,
        file_search: Callable[..., list[dict]] = mdfind_search,
        large_file_search: Callable[..., dict] = find_large_files,
        application_process_provider: Callable[[], list[dict]] = list_application_processes,
        probe_registry: ProbeRegistry | None = None,
    ) -> None:
        self.db = db
        self.process_provider = process_provider
        self.process_search = process_search
        self.file_search = file_search
        self.large_file_search = large_file_search
        self.application_process_provider = application_process_provider
        self.probe_registry = probe_registry

    def execute(self, name: str, arguments: Mapping[str, Any] | None = None) -> ToolResult:
        args = dict(arguments or {})
        handlers = {
            "get_current_stats": self._current_stats,
            "get_top_processes": self._top_processes,
            "query_metrics": self._query_metrics,
            "get_process_history": self._process_history,
            "get_events": self._events,
            "find_large_files": self._large_files,
            "search_files": self._search_files,
            "search_processes": self._search_processes,
            "get_app_overview": self._app_overview,
            "get_app_history": self._app_history,
            "get_app_connections": self._app_connections,
            "run_probe": self._run_probe,
        }
        handler = handlers.get(name)
        if handler is None:
            raise ToolError(f"未知工具: {name}")
        try:
            return handler(args)
        except ToolError:
            raise
        except (TypeError, ValueError) as exc:
            raise ToolError(str(exc)) from exc

    def _current_stats(self, args: dict) -> ToolResult:
        self._only(args, set())
        metric = self.db.latest_metric()
        if metric is None:
            return ToolResult({"available": False}, "当前还没有系统指标采样")
        data = {
            "ts": metric["ts"], "cpu_percent": metric["cpu_percent"],
            "cpu_per_core": metric["cpu_per_core"], "load_1": metric["load_1"],
            "mem_used": metric["mem_used"], "mem_total": metric["mem_total"],
            "mem_percent": metric["mem_percent"], "swap_used": metric["swap_used"],
            "disk_read_bps": metric["disk_read_bps"],
            "disk_write_bps": metric["disk_write_bps"],
            "net_up_bps": metric["net_up_bps"], "net_down_bps": metric["net_down_bps"],
            "disks": self.db.latest_disk_usage(),
        }
        return ToolResult(
            _compact(data), f"查询了 {self._clock(metric['ts'])} 的当前系统状态"
        )

    def _top_processes(self, args: dict) -> ToolResult:
        self._only(args, {"sort_by", "limit"})
        sort_by = self._choice(args, "sort_by", {"cpu", "memory", "network"})
        limit = self._integer(args, "limit", 1, 15)
        rows = (
            self.db.latest_process_net()[:limit]
            if sort_by == "network" else self.process_provider(sort_by, limit)
        )
        label = {"cpu": "CPU", "memory": "内存", "network": "网络"}[sort_by]
        return ToolResult(_compact(rows), f"查询了按{label}排序的前 {len(rows)} 个进程")

    def _query_metrics(self, args: dict) -> ToolResult:
        self._only(args, {"metric", "start_ts", "end_ts"})
        metric = self._choice(args, "metric", METRIC_COLUMNS)
        start = self._integer(args, "start_ts")
        end = self._integer(args, "end_ts")
        if end < start:
            raise ToolError("end_ts 必须大于等于 start_ts")
        points = self.db.query_metrics(metric, start, end, max_points=50)
        values = [float(point["avg"]) for point in points]
        data = {
            "metric": metric, "start_ts": start, "end_ts": end,
            "summary": ({
                "min": min(values), "max": max(float(point["max"]) for point in points),
                "avg": sum(values) / len(values), "points": len(points),
            } if values else {"min": None, "max": None, "avg": None, "points": 0}),
            "series": points,
        }
        return ToolResult(
            _compact(data),
            f"查询了 {self._clock(start)}–{self._clock(end)} 的 {metric} 数据",
        )

    def _process_history(self, args: dict) -> ToolResult:
        self._only(args, {"start_ts", "end_ts", "name"})
        start = self._integer(args, "start_ts")
        end = self._integer(args, "end_ts")
        name = args.get("name")
        if name is not None and not isinstance(name, str):
            raise ToolError("name 必须是字符串")
        rows = self.db.process_history(start, end, name, limit=30)
        return ToolResult(
            _compact(rows), f"查询了 {self._clock(start)}–{self._clock(end)} 的进程历史"
        )

    def _events(self, args: dict) -> ToolResult:
        self._only(args, {"limit", "since_ts"})
        limit = self._integer(args, "limit", 1, 50)
        since = self._integer(args, "since_ts") if "since_ts" in args else None
        rows = self.db.list_events(limit, since)
        return ToolResult(_compact(rows), f"查询了 {len(rows)} 条告警事件")

    def _large_files(self, args: dict) -> ToolResult:
        self._only(args, {"path", "min_mb", "limit"})
        path = self._text(args, "path")
        min_mb = self._number(args, "min_mb", minimum=0)
        limit = self._integer(args, "limit", 1, 30)
        result = self.large_file_search(path, min_mb, limit)
        return ToolResult(_compact(result), f"扫描了 {path} 中大于 {min_mb:g} MB 的文件")

    def _search_files(self, args: dict) -> ToolResult:
        self._only(args, {"query", "kind", "limit"})
        query = self._text(args, "query")
        kind = self._choice(args, "kind", {"name", "content"})
        limit = self._integer(args, "limit", 1, 20)
        rows = self.file_search(query, kind, limit)
        label = "文件名" if kind == "name" else "内容"
        return ToolResult(
            _compact(rows), f"按{label}搜索了“{query}”，找到 {len(rows)} 项"
        )

    def _search_processes(self, args: dict) -> ToolResult:
        self._only(args, {"keyword"})
        keyword = self._text(args, "keyword")
        rows = self.process_search(keyword, 20)
        return ToolResult(
            _compact(rows), f"搜索了包含“{keyword}”的运行中进程，找到 {len(rows)} 项"
        )

    def _app_overview(self, args: dict) -> ToolResult:
        self._only(args, {"limit"})
        limit = self._integer(args, "limit", 1, 30)
        rows = build_app_overview(
            self.db,
            self.application_process_provider(),
            sort="cpu",
            limit=limit,
        )
        return ToolResult(_compact({"apps": rows}), f"查询了当前前 {len(rows)} 个应用的资源占用")

    def _app_history(self, args: dict) -> ToolResult:
        self._only(args, {"app", "start_ts", "end_ts", "process_name"})
        app = self._text(args, "app")
        start = self._integer(args, "start_ts")
        end = self._integer(args, "end_ts")
        if end < start:
            raise ToolError("end_ts 必须大于等于 start_ts")
        process_name = (
            self._text(args, "process_name") if "process_name" in args else None
        )
        rows = (
            self.db.app_process_history(
                app, process_name, start, end, max_points=30
            )
            if process_name is not None
            else self.db.app_history(app, start, end, max_points=30)
        )
        identity = {"app": app}
        subject = f"应用“{app}”"
        if process_name is not None:
            identity["process_name"] = process_name
            subject += f"的子进程“{process_name}”"
        if not rows:
            return ToolResult(
                {"available": False, **identity, "series": []},
                f"没有找到{subject}在指定时段的历史数据",
            )
        cpu_values = [float(row["cpu_percent"]) for row in rows]
        memory_values = [int(row["memory_rss"]) for row in rows]
        data = {
            "available": True,
            **identity,
            "start_ts": start,
            "end_ts": end,
            "summary": {
                "cpu_percent": {
                    "min": min(cpu_values),
                    "max": max(cpu_values),
                    "avg": sum(cpu_values) / len(cpu_values),
                },
                "memory_rss": {
                    "min": min(memory_values),
                    "max": max(memory_values),
                    "avg": sum(memory_values) / len(memory_values),
                },
            },
            "series": rows,
        }
        return ToolResult(
            _compact(data),
            f"查询了{subject}在 {self._clock(start)}–{self._clock(end)} 的历史基线",
        )

    def _app_connections(self, args: dict) -> ToolResult:
        self._only(args, {"app"})
        app = self._text(args, "app")
        rows = self.db.latest_app_connections(app, limit=30)
        data = {
            "app": app,
            "connections": rows,
            "note": "via_proxy=1 的连接 RTT 是到本地代理的，不代表真实网络延迟。",
        }
        return ToolResult(_compact(data), f"查询了应用“{app}”最近的 {len(rows)} 条网络连接")

    def _run_probe(self, args: dict) -> ToolResult:
        self._only(args, {"probe_id", "params"})
        probe_id = self._choice(args, "probe_id", AGENT_PROBE_ALLOWLIST)
        params = args.get("params")
        if not isinstance(params, dict):
            raise ToolError("params 必须是对象")
        if self.probe_registry is None:
            self.probe_registry = build_registry(self.db)
        try:
            result = self.probe_registry.run(probe_id, params)
        except (ValueError, PermissionError) as exc:
            raise ToolError(str(exc)) from exc
        data = _compact(result.to_dict())
        return ToolResult(data, f"运行了 {probe_id} 诊断探测")

    @staticmethod
    def _only(args: dict, allowed: set[str]) -> None:
        extra = set(args) - allowed
        if extra:
            raise ToolError(f"不支持的参数: {', '.join(sorted(extra))}")

    @staticmethod
    def _integer(
        args: dict,
        key: str,
        minimum: int | None = None,
        maximum: int | None = None,
    ) -> int:
        value = args.get(key)
        if isinstance(value, bool) or not isinstance(value, int):
            raise ToolError(f"{key} 必须是整数")
        if minimum is not None and value < minimum or maximum is not None and value > maximum:
            raise ToolError(f"{key} 超出允许范围")
        return value

    @staticmethod
    def _number(args: dict, key: str, minimum: float | None = None) -> float:
        value = args.get(key)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ToolError(f"{key} 必须是数字")
        number = float(value)
        if minimum is not None and number < minimum:
            raise ToolError(f"{key} 超出允许范围")
        return number

    @staticmethod
    def _text(args: dict, key: str) -> str:
        value = args.get(key)
        if not isinstance(value, str) or not value.strip():
            raise ToolError(f"{key} 必须是非空字符串")
        return value.strip()

    @classmethod
    def _choice(cls, args: dict, key: str, choices: set[str]) -> str:
        value = cls._text(args, key)
        if value not in choices:
            raise ToolError(f"{key} 的值不受支持")
        return value

    @staticmethod
    def _clock(timestamp: int | float) -> str:
        return datetime.fromtimestamp(timestamp).strftime("%m-%d %H:%M")


def _compact(value: Any) -> Any:
    """减少 4B 模型上下文占用：浮点取整，长文本做有界截断。"""
    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, float):
        return int(round(value))
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        return value if len(value) <= 300 else value[:297] + "..."
    if isinstance(value, Mapping):
        return {str(key): _compact(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_compact(item) for item in value]
    return str(value)
