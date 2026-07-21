"""后台系统指标采集线程。"""

from __future__ import annotations

import csv
import io
import logging
import os
import subprocess
import threading
import time
from typing import Any

import psutil

from . import config
from .db import Database
from .rules import RuleEngine


logger = logging.getLogger(__name__)


def parse_nettop_output(output: str) -> list[dict[str, Any]]:
    """解析 nettop CSV，返回进程累计收发字节。"""
    rows = list(csv.reader(io.StringIO(output)))
    if not rows:
        return []
    header = [cell.strip().lower() for cell in rows[0]]
    process_aliases = {"process", "name", "command"}
    process_index = next((i for i, value in enumerate(header) if value in process_aliases), 0)
    try:
        in_index = next(i for i, value in enumerate(header) if value in {"bytes_in", "bytes in"})
        out_index = next(i for i, value in enumerate(header) if value in {"bytes_out", "bytes out"})
        data_rows = rows[1:]
    except StopIteration:
        # 部分系统版本在 -x 输出中省略表头，列顺序固定为进程、入、出。
        process_index, in_index, out_index, data_rows = 0, 1, 2, rows
    result: list[dict[str, Any]] = []
    for row in data_rows:
        if len(row) <= max(process_index, in_index, out_index):
            continue
        process = row[process_index].strip()
        if "." not in process:
            continue
        name, pid_text = process.rsplit(".", 1)
        try:
            pid = int(pid_text)
            bytes_in = float(row[in_index] or 0)
            bytes_out = float(row[out_index] or 0)
        except ValueError:
            continue
        result.append({
            "pid": pid, "name": name, "bytes_in": bytes_in, "bytes_out": bytes_out
        })
    return result


class Collector:
    def __init__(self, db: Database, rules: RuleEngine | None = None) -> None:
        self.db = db
        self.rules = rules or RuleEngine(db)
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._disk_prev: tuple[float, Any] | None = None
        self._net_prev: tuple[float, Any] | None = None
        self._process_net_prev: dict[tuple[int, str], tuple[float, float, float]] = {}
        self._last_cleanup = 0.0

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run, name="macpilot-collector", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=config.SAMPLE_INTERVAL_SECONDS + 2)

    def _run(self) -> None:
        # 首轮即采集慢速项目，之后保持每 60 秒一次。
        tick = config.SLOW_SAMPLE_TICKS - 1
        logger.info("监控采集线程已启动，采样间隔 %s 秒", config.SAMPLE_INTERVAL_SECONDS)
        while not self._stop_event.is_set():
            started = time.monotonic()
            now = int(time.time())
            self._safe("基础指标采集", self._collect_metric, now)
            tick += 1
            if tick % config.SLOW_SAMPLE_TICKS == 0:
                self._safe("进程快照", self._collect_processes, now)
                self._safe("磁盘容量", self._collect_disk_usage, now)
                self._safe("进程网络", self._collect_process_net, now)
                self._safe("规则检测", self.rules.evaluate, now)
            if time.monotonic() - self._last_cleanup >= config.CLEANUP_INTERVAL_SECONDS:
                self._safe("数据清理", self.db.cleanup, now)
                self._last_cleanup = time.monotonic()
            wait = max(0.0, config.SAMPLE_INTERVAL_SECONDS - (time.monotonic() - started))
            self._stop_event.wait(wait)
        logger.info("监控采集线程已停止")

    @staticmethod
    def _safe(label: str, operation: Any, *args: Any) -> None:
        try:
            operation(*args)
        except Exception as exc:  # 单项采集必须隔离所有第三方/系统异常
            logger.warning("%s失败: %s", label, exc, exc_info=True)

    def _collect_metric(self, ts: int) -> None:
        cpu_per_core = psutil.cpu_percent(interval=None, percpu=True)
        cpu_percent = psutil.cpu_percent(interval=None)
        load_1 = os.getloadavg()[0]
        memory = psutil.virtual_memory()
        swap = psutil.swap_memory()
        disk_read_bps, disk_write_bps = self._disk_rates()
        net_up_bps, net_down_bps = self._network_rates()
        self.db.insert_metric({
            "ts": ts,
            "cpu_percent": cpu_percent,
            "cpu_per_core": cpu_per_core,
            "load_1": load_1,
            "mem_used": memory.used,
            "mem_total": memory.total,
            "mem_percent": memory.percent,
            "swap_used": swap.used,
            "disk_read_bps": disk_read_bps,
            "disk_write_bps": disk_write_bps,
            "net_up_bps": net_up_bps,
            "net_down_bps": net_down_bps,
        })

    def _disk_rates(self) -> tuple[float, float]:
        current_time = time.monotonic()
        counters = psutil.disk_io_counters()
        if counters is None or self._disk_prev is None:
            self._disk_prev = (current_time, counters)
            return 0.0, 0.0
        previous_time, previous = self._disk_prev
        self._disk_prev = (current_time, counters)
        if previous is None:
            return 0.0, 0.0
        elapsed = max(0.001, current_time - previous_time)
        return (
            max(0.0, (counters.read_bytes - previous.read_bytes) / elapsed),
            max(0.0, (counters.write_bytes - previous.write_bytes) / elapsed),
        )

    def _network_rates(self) -> tuple[float, float]:
        current_time = time.monotonic()
        counters = psutil.net_io_counters()
        if self._net_prev is None:
            self._net_prev = (current_time, counters)
            return 0.0, 0.0
        previous_time, previous = self._net_prev
        self._net_prev = (current_time, counters)
        elapsed = max(0.001, current_time - previous_time)
        return (
            max(0.0, (counters.bytes_sent - previous.bytes_sent) / elapsed),
            max(0.0, (counters.bytes_recv - previous.bytes_recv) / elapsed),
        )

    def _collect_processes(self, ts: int) -> None:
        processes: list[dict[str, Any]] = []
        for process in psutil.process_iter(
            ["pid", "name", "cpu_percent", "memory_info", "cmdline"]
        ):
            try:
                info = process.info
                memory_info = info.get("memory_info")
                cmdline = " ".join(info.get("cmdline") or [])[:200]
                processes.append({
                    "pid": info["pid"],
                    "name": info.get("name") or "未知",
                    "cpu_percent": float(info.get("cpu_percent") or 0),
                    "memory_rss": int(memory_info.rss if memory_info else 0),
                    "cmdline": cmdline,
                })
            except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                continue
        top_cpu = sorted(processes, key=lambda row: row["cpu_percent"], reverse=True)[
            :config.PROCESS_SNAPSHOT_LIMIT
        ]
        top_memory = sorted(processes, key=lambda row: row["memory_rss"], reverse=True)[
            :config.PROCESS_SNAPSHOT_LIMIT
        ]
        merged = {row["pid"]: row for row in top_cpu + top_memory}
        self.db.insert_process_snapshots(ts, merged.values())

    def _collect_disk_usage(self, ts: int) -> None:
        rows = []
        seen = set()
        for mount in config.DEFAULT_DISK_MOUNTS:
            if mount in seen or not os.path.exists(mount):
                continue
            seen.add(mount)
            usage = psutil.disk_usage(mount)
            rows.append({
                "mount": mount, "total": usage.total, "used": usage.used,
                "percent": usage.percent,
            })
        self.db.insert_disk_usage(ts, rows)

    def _collect_process_net(self, ts: int) -> None:
        completed = subprocess.run(
            ["nettop", "-P", "-L", "1", "-x", "-J", "bytes_in,bytes_out"],
            capture_output=True, text=True, timeout=config.NETTOP_TIMEOUT_SECONDS,
            check=False,
        )
        if completed.returncode != 0:
            raise RuntimeError(completed.stderr.strip() or f"nettop 退出码 {completed.returncode}")
        now_mono = time.monotonic()
        current = parse_nettop_output(completed.stdout)
        rates = []
        next_previous: dict[tuple[int, str], tuple[float, float, float]] = {}
        for row in current:
            key = (row["pid"], row["name"])
            bytes_in = float(row["bytes_in"])
            bytes_out = float(row["bytes_out"])
            previous = self._process_net_prev.get(key)
            next_previous[key] = (now_mono, bytes_in, bytes_out)
            if previous is None:
                continue
            elapsed = max(0.001, now_mono - previous[0])
            rates.append({
                "pid": row["pid"], "name": row["name"],
                "up_bps": max(0.0, (bytes_out - previous[2]) / elapsed),
                "down_bps": max(0.0, (bytes_in - previous[1]) / elapsed),
            })
        self._process_net_prev = next_previous
        self.db.insert_process_net(ts, rates)


def list_processes(sort: str = "cpu", limit: int = 20) -> list[dict[str, Any]]:
    """API 使用的实时进程列表。"""
    rows: list[dict[str, Any]] = []
    for process in psutil.process_iter(["pid", "name", "cpu_percent", "memory_info", "cmdline"]):
        try:
            info = process.info
            memory = info.get("memory_info")
            rows.append({
                "pid": int(info["pid"]), "name": info.get("name") or "未知",
                "cpu_percent": round(float(info.get("cpu_percent") or 0), 1),
                "memory_rss": int(memory.rss if memory else 0),
                "cmdline": " ".join(info.get("cmdline") or [])[:200],
            })
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            continue
    key = "cpu_percent" if sort == "cpu" else "memory_rss"
    return sorted(rows, key=lambda row: row[key], reverse=True)[:limit]
