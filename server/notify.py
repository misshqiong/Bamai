"""macOS 系统通知。"""

from __future__ import annotations

import logging
import platform
import subprocess


logger = logging.getLogger(__name__)


def send_notification(title: str, message: str) -> bool:
    """通过 osascript 发送通知；失败只记日志，不影响监控。"""
    if platform.system() != "Darwin":
        return False
    script = "display notification " + _apple_string(message) + " with title " + _apple_string(title)
    try:
        subprocess.run(
            ["osascript", "-e", script], check=True, capture_output=True,
            text=True, timeout=5,
        )
        return True
    except (OSError, subprocess.SubprocessError) as exc:
        logger.warning("系统通知发送失败: %s", exc)
        return False


def _apple_string(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'

