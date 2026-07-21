"""Spotlight 文件搜索与限时大文件扫描。"""

from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path
from typing import Any

from .. import config


PACKAGE_SUFFIXES = (".app", ".framework")


def mdfind_search(
    query: str,
    kind: str = "name",
    limit: int = 20,
    timeout: float = config.SEARCH_TIMEOUT_SECONDS,
) -> list[dict[str, Any]]:
    query = query.strip()
    if not query:
        return []
    if kind not in {"name", "content"}:
        raise ValueError("kind 必须是 name 或 content")
    limit = max(1, min(int(limit), 100))
    command = ["mdfind"] + (["-name", query] if kind == "name" else [query])
    try:
        completed = subprocess.run(
            command, capture_output=True, text=True, timeout=timeout, check=False
        )
    except FileNotFoundError as exc:
        raise RuntimeError("当前系统没有 mdfind，文件搜索仅支持 macOS") from exc
    except subprocess.TimeoutExpired as exc:
        raise TimeoutError("Spotlight 搜索超时") from exc
    if completed.returncode != 0:
        raise RuntimeError(completed.stderr.strip() or "mdfind 搜索失败")
    results = []
    for line in completed.stdout.splitlines():
        path = line.strip()
        if not path or path == "/Library" or path.startswith("/Library/"):
            continue
        if path == "/System" or path.startswith("/System/"):
            continue
        try:
            stat = os.stat(path)
            size = stat.st_size if os.path.isfile(path) else None
        except OSError:
            size = None
        results.append({"path": path, "name": os.path.basename(path) or path, "size": size})
        if len(results) >= limit:
            break
    return results


def find_large_files(
    path: str = "~",
    min_mb: float = 100,
    limit: int = 50,
    timeout: float = config.LARGE_FILE_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    if min_mb < 0:
        raise ValueError("min_mb 不能为负数")
    limit = max(1, min(int(limit), 500))
    root = _resolve_scan_path(path)
    if not root.exists():
        raise ValueError(f"路径不存在: {root}")
    if not root.is_dir():
        raise ValueError("扫描路径必须是目录")

    deadline = time.monotonic() + timeout
    minimum = int(min_mb * 1024**2)
    found: list[dict[str, Any]] = []
    truncated = False
    for current_root, directories, filenames in os.walk(root, topdown=True):
        if time.monotonic() >= deadline:
            truncated = True
            break
        directories[:] = [
            name for name in directories
            if not name.startswith(".") and not name.lower().endswith(PACKAGE_SUFFIXES)
        ]
        for filename in filenames:
            if time.monotonic() >= deadline:
                truncated = True
                break
            if filename.startswith("."):
                continue
            file_path = Path(current_root) / filename
            try:
                if file_path.is_symlink():
                    continue
                size = file_path.stat().st_size
            except OSError:
                continue
            if size >= minimum:
                found.append({
                    "path": str(file_path), "name": filename,
                    "size": size, "size_mb": round(size / 1024**2, 1),
                })
    found.sort(key=lambda item: item["size"], reverse=True)
    return {"items": found[:limit], "truncated": truncated, "scanned_path": str(root)}


def _resolve_scan_path(value: str) -> Path:
    """相对路径只允许落在用户目录；显式绝对路径按规格允许。"""
    value = value.strip() or "~"
    home = Path.home().resolve()
    raw = Path(value)
    # 只有原始输入就是绝对路径时，才视为用户显式允许扫描用户目录以外。
    if raw.is_absolute():
        return raw.resolve()
    expanded = raw.expanduser()
    resolved = expanded.resolve() if expanded.is_absolute() else (home / expanded).resolve()
    try:
        resolved.relative_to(home)
    except ValueError as exc:
        raise ValueError("相对扫描路径不能越出用户目录") from exc
    return resolved
