"""后端事件文本与当前界面语言状态。"""

from __future__ import annotations

import threading
from typing import Any, Mapping

EVENT_TEXTS = {
    "zh": {
        "cpu_high": (
            "CPU 持续高负载",
            "最近 3 分钟 CPU 平均使用率为 {percent}%（阈值 85%）。",
        ),
        "mem_pressure": (
            "内存占用较高",
            "内存使用率 {percent}%，10 分钟内系统借用磁盘空间增加 "
            "{growth_gb} GB。",
        ),
        "disk_full": (
            "磁盘空间不足",
            "磁盘 {mount} 已使用 {percent}%。",
        ),
        "net_spike": (
            "网络流量突增",
            "最近 2 分钟平均流量 {recent_mbps} MB/s，平时约 {baseline_mbps} MB/s。",
        ),
    },
    "en": {
        "cpu_high": (
            "CPU has stayed busy",
            "Average CPU usage was {percent}% over the last 3 minutes (threshold: 85%).",
        ),
        "mem_pressure": (
            "Memory usage is high",
            "Memory usage is {percent}%; borrowed disk space grew by {growth_gb} GB in 10 minutes.",
        ),
        "disk_full": (
            "Disk space is running low",
            "Disk {mount} is {percent}% used.",
        ),
        "net_spike": (
            "Network activity increased sharply",
            "Traffic averaged {recent_mbps} MB/s over 2 minutes, versus about "
            "{baseline_mbps} MB/s normally.",
        ),
    },
}


def normalize_language(value: str | None) -> str:
    return "zh" if value and value.lower().startswith("zh") else "en"


class LanguageState:
    """本地单用户应用中，记录最近一次界面 API 请求的语言。"""

    def __init__(self, initial: str = "zh") -> None:
        self._value = normalize_language(initial)
        self._lock = threading.Lock()

    def get(self) -> str:
        with self._lock:
            return self._value

    def set(self, value: str | None) -> str:
        language = normalize_language(value)
        with self._lock:
            self._value = language
        return language


def render_event_text(
    kind: str, params: Mapping[str, Any], language: str
) -> tuple[str, str]:
    table = EVENT_TEXTS[normalize_language(language)]
    title, detail = table.get(kind, (kind, kind))
    safe_params = _FormatParams({key: _display(value) for key, value in params.items()})
    return title.format_map(safe_params), detail.format_map(safe_params)


class _FormatParams(dict):
    def __missing__(self, _: str) -> str:
        return "—"


def _display(value: Any) -> Any:
    if isinstance(value, float):
        return round(value, 1)
    return value
