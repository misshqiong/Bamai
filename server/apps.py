"""进程到 macOS 应用的归组逻辑。"""

from __future__ import annotations

import re
import shlex
from collections.abc import Callable
from typing import Any

import psutil

from .db import Database

ParentLookup = Callable[[int], int | None]

_APP_COMPONENT = re.compile(r"(?:^|/)([^/]+)\.app(?:/|$)", re.IGNORECASE)


def extract_app_from_path(path: str) -> str | None:
    """从路径中取最外层 ``.app`` bundle 名称。"""
    if not path:
        return None
    match = _APP_COMPONENT.search(path.strip())
    if match is None:
        return None
    name = match.group(1).strip()
    return name or None


def _command_path(cmdline: Any) -> str:
    if isinstance(cmdline, (list, tuple)):
        return str(cmdline[0]) if cmdline else ""
    if not isinstance(cmdline, str) or not cmdline.strip():
        return ""
    try:
        parts = shlex.split(cmdline)
    except ValueError:
        parts = cmdline.split(maxsplit=1)
    return parts[0] if parts else ""


def _direct_app(process: dict[str, Any]) -> str | None:
    return (
        extract_app_from_path(str(process.get("exe") or ""))
        or extract_app_from_path(str(process.get("argv0") or ""))
        or extract_app_from_path(_command_path(process.get("cmdline")))
    )


def _psutil_parent_lookup(pid: int) -> int | None:
    try:
        parent = psutil.Process(pid).parent()
        return parent.pid if parent is not None else None
    except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
        return None


def group_processes(
    procs: list[dict[str, Any]],
    parent_lookup: ParentLookup | None = None,
) -> list[dict[str, Any]]:
    """将进程按最外层应用 bundle 聚合；找不到 bundle 时保留后台进程组。"""
    lookup = parent_lookup or _psutil_parent_lookup
    by_pid = {int(process["pid"]): process for process in procs}
    direct_apps = {pid: _direct_app(process) for pid, process in by_pid.items()}
    groups: dict[str, dict[str, Any]] = {}

    for pid, process in by_pid.items():
        app = direct_apps[pid]
        if app is None:
            current_pid = pid
            seen = {pid}
            for _ in range(5):
                try:
                    parent_pid = lookup(current_pid)
                except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                    parent_pid = None
                if parent_pid is None or parent_pid in seen:
                    break
                seen.add(parent_pid)
                parent = by_pid.get(parent_pid)
                parent_app = direct_apps.get(parent_pid)
                if parent_app is None and parent is not None:
                    parent_app = _direct_app(parent)
                    direct_apps[parent_pid] = parent_app
                if parent_app is not None:
                    app = parent_app
                    break
                current_pid = parent_pid

        kind = "app" if app is not None else "background"
        app = app or str(process.get("name") or "未知")
        group = groups.setdefault(app, {
            "app": app,
            "kind": kind,
            "cpu_percent": 0.0,
            "memory_rss": 0,
            "proc_count": 0,
            "pids": [],
        })
        if kind == "app":
            group["kind"] = "app"
        group["cpu_percent"] += float(process.get("cpu_percent") or 0)
        group["memory_rss"] += int(process.get("memory_rss") or 0)
        group["proc_count"] += 1
        group["pids"].append(pid)

    for group in groups.values():
        group["pids"].sort()
    return sorted(groups.values(), key=lambda group: (-group["cpu_percent"], group["app"]))


def list_application_processes() -> list[dict[str, Any]]:
    """读取 API 与采集器共用的实时进程字段。"""
    rows: list[dict[str, Any]] = []
    attrs = ["pid", "ppid", "name", "cpu_percent", "memory_info", "cmdline", "exe"]
    for process in psutil.process_iter(attrs):
        try:
            info = process.info
            memory = info.get("memory_info")
            cmdline_parts = info.get("cmdline") or []
            rows.append({
                "pid": int(info["pid"]),
                "ppid": int(info.get("ppid") or 0),
                "name": info.get("name") or "未知",
                "cpu_percent": float(info.get("cpu_percent") or 0),
                "memory_rss": int(memory.rss if memory else 0),
                "cmdline": " ".join(cmdline_parts)[:200],
                "argv0": cmdline_parts[0] if cmdline_parts else "",
                "exe": info.get("exe") or "",
            })
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            continue
    return rows


def group_captured_processes(procs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """用同一轮捕获的 ppid 归组，避免再次访问易消失的进程。"""
    parents = {int(process["pid"]): int(process.get("ppid") or 0) or None for process in procs}
    return group_processes(procs, parent_lookup=parents.get)


def build_app_overview(
    db: Database,
    processes: list[dict[str, Any]],
    *,
    sort: str = "cpu",
    limit: int = 30,
) -> list[dict[str, Any]]:
    """聚合实时 CPU/内存与最近一轮应用网络数据，供 API 和 agent 共用。"""
    sort_keys = {
        "cpu": lambda row: row["cpu_percent"],
        "memory": lambda row: row["memory_rss"],
        "network": lambda row: row["up_bps"] + row["down_bps"],
    }
    if sort not in sort_keys:
        raise ValueError(f"不支持的应用排序: {sort}")
    latest_network = {
        row["app"]: (row["up_bps"], row["down_bps"])
        for row in db.latest_app_snapshots()
    }
    items = []
    for group in group_captured_processes(processes):
        up_bps, down_bps = latest_network.get(group["app"], (0.0, 0.0))
        items.append({
            "app": group["app"],
            "kind": group["kind"],
            "cpu_percent": group["cpu_percent"],
            "memory_rss": group["memory_rss"],
            "proc_count": group["proc_count"],
            "up_bps": up_bps,
            "down_bps": down_bps,
        })
    items.sort(key=sort_keys[sort], reverse=True)
    return items[:max(1, min(int(limit), 100))]
