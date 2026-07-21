"""Bamai 的集中配置与旧数据迁移。"""

from __future__ import annotations

import os
import logging
import shutil
import tempfile
from pathlib import Path


logger = logging.getLogger(__name__)

APP_NAME = "Bamai"
APP_NAME_ZH = "把脉"
HOST = "127.0.0.1"
PORT = 8737

PROJECT_ROOT = Path(__file__).resolve().parent.parent
WEB_DIR = PROJECT_ROOT / "web"
LEGACY_DATA_DIR = Path("~/.macpilot").expanduser()
DATA_DIR = Path(os.environ.get("BAMAI_DATA_DIR", "~/.bamai")).expanduser()
DB_PATH = Path(os.environ.get("BAMAI_DB_PATH", DATA_DIR / "data.db")).expanduser()
SETTINGS_PATH = Path(os.environ.get("BAMAI_CONFIG_PATH", DATA_DIR / "config.json")).expanduser()

SAMPLE_INTERVAL_SECONDS = 3
SLOW_SAMPLE_TICKS = 20
SLOW_SAMPLE_INTERVAL_SECONDS = SAMPLE_INTERVAL_SECONDS * SLOW_SAMPLE_TICKS
CLEANUP_INTERVAL_SECONDS = 60 * 60
WEBSOCKET_INTERVAL_SECONDS = 3

METRICS_RETENTION_SECONDS = 48 * 60 * 60
PROCESS_RETENTION_SECONDS = 7 * 24 * 60 * 60
DISK_RETENTION_SECONDS = 90 * 24 * 60 * 60
EVENT_RETENTION_SECONDS = 90 * 24 * 60 * 60
EVENT_DEDUP_SECONDS = 30 * 60

CPU_HIGH_PERCENT = 85.0
CPU_HIGH_DURATION_SECONDS = 3 * 60
MEM_HIGH_PERCENT = 90.0
SWAP_GROWTH_BYTES = 2 * 1024**3
SWAP_WINDOW_SECONDS = 10 * 60
DISK_WARNING_PERCENT = 90.0
DISK_CRITICAL_PERCENT = 95.0
NET_SPIKE_MIN_BPS = 10 * 1024**2
NET_SPIKE_MULTIPLIER = 10.0
NET_SPIKE_DURATION_SECONDS = 2 * 60
NET_BASELINE_SECONDS = 60 * 60

PROCESS_SNAPSHOT_LIMIT = 15
NETTOP_TIMEOUT_SECONDS = 5
SEARCH_TIMEOUT_SECONDS = 10
LARGE_FILE_TIMEOUT_SECONDS = 30
MAX_CHART_POINTS = 500

DEFAULT_DISK_MOUNTS = ("/", "/System/Volumes/Data")

OLLAMA_BASE_URL = os.environ.get("BAMAI_OLLAMA_URL", "http://localhost:11434").rstrip("/")
OLLAMA_MODEL = "qwen3:4b"
OLLAMA_CHAT_TIMEOUT_SECONDS = 120
OLLAMA_HEALTH_TIMEOUT_SECONDS = 3
OLLAMA_MAX_TOOL_ROUNDS = 6
OLLAMA_TEMPERATURE = 0.7
OLLAMA_CONTEXT_SIZE = 8192
HEALTH_EXPLAIN_TIMEOUT_SECONDS = 30


def migrate_legacy_data_dir(
    old_dir: str | Path = LEGACY_DATA_DIR,
    new_dir: str | Path = DATA_DIR,
) -> bool:
    """首次启动时复制旧数据目录；保留旧目录作为可恢复备份。"""
    old_path = Path(old_dir).expanduser()
    new_path = Path(new_dir).expanduser()
    if not old_path.is_dir() or new_path.exists():
        return False
    new_path.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=".bamai-migrate-", dir=new_path.parent))
    staged_data = staging / "data"
    try:
        shutil.copytree(old_path, staged_data, copy_function=shutil.copy2, symlinks=True)
        staged_data.rename(new_path)
    finally:
        if staging.exists():
            shutil.rmtree(staging)
    if not new_path.is_dir():
        raise RuntimeError(f"Bamai 数据迁移后目录不可读: {new_path}")
    logger.info("已将旧数据目录 %s 迁移到 %s；旧目录保留为备份", old_path, new_path)
    return True
