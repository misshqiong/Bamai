"""后台系统指标采集线程。"""

from __future__ import annotations

import csv
import io
import ipaddress
import logging
import os
import queue
import re
import socket
import subprocess
import threading
import time
from collections import OrderedDict
from collections.abc import Callable
from typing import Any

import psutil

from . import config
from .apps import group_captured_processes, list_application_processes
from .db import Database
from .rules import RuleEngine

logger = logging.getLogger(__name__)


def _number(value: str, *, optional: bool = False) -> float | None:
    text = value.strip().lower()
    if not text or text in {"-", "n/a", "nan"}:
        return None if optional else 0.0
    match = re.search(r"-?\d+(?:\.\d+)?", text)
    if match is None:
        return None if optional else 0.0
    try:
        return float(match.group(0))
    except ValueError:
        return None if optional else 0.0


def _endpoint(value: str) -> tuple[str, int] | None:
    text = value.strip()
    if text.startswith("[") and "]:" in text:
        host, port_text = text[1:].rsplit("]:", 1)
    elif text.count(":") >= 2 and "." in text:
        # IPv6 地址本身含冒号，nettop 实测用最后一个点分隔端口（fe80::1.443）。
        host, port_text = text.rsplit(".", 1)
    elif ":" in text:
        host, port_text = text.rsplit(":", 1)
    else:
        return None
    try:
        port = int(port_text)
    except ValueError:
        return None
    return host.split("%", 1)[0], port


def _connection_label(value: str) -> dict[str, Any] | None:
    match = re.match(r"^(tcp|udp)(?:[46])?\s+(.+?)<->(.+)$", value.strip(), re.IGNORECASE)
    if match is None:
        return None
    local = _endpoint(match.group(2))
    remote = _endpoint(match.group(3))
    if local is None or remote is None:
        return None
    return {
        "proto": match.group(1).lower(),
        "local_ip": local[0],
        "local_port": local[1],
        "remote_ip": remote[0],
        "remote_port": remote[1],
    }


def _parse_nettop_rows(
    output: str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    rows = list(csv.reader(io.StringIO(output)))
    if not rows:
        return [], []
    header = [cell.strip().lower().replace(" ", "_") for cell in rows[0]]
    process_aliases = {"process", "name", "command"}
    process_index = next((i for i, value in enumerate(header) if value in process_aliases), 0)
    try:
        in_index = header.index("bytes_in")
        out_index = header.index("bytes_out")
        rtt_index = header.index("rtt_avg") if "rtt_avg" in header else None
        data_rows = rows[1:]
    except ValueError:
        # 部分系统版本在 -x 输出中省略表头，顺序与 -J 参数一致。
        process_index, in_index, out_index, rtt_index, data_rows = 0, 1, 2, 3, rows
    processes: list[dict[str, Any]] = []
    connections: list[dict[str, Any]] = []
    owner: tuple[int, str] | None = None
    for row in data_rows:
        if len(row) <= process_index:
            continue
        label = row[process_index].strip()
        connection = _connection_label(label)
        if connection is not None:
            if owner is None or len(row) <= max(in_index, out_index):
                continue
            connection.update({
                "pid": owner[0],
                "name": owner[1],
                "bytes_in": float(_number(row[in_index]) or 0),
                "bytes_out": float(_number(row[out_index]) or 0),
                # nettop 的 rtt_avg 展示值按毫秒保存；UDP 或缺列为 NULL。
                "rtt_ms": (
                    _number(row[rtt_index], optional=True)
                    if rtt_index is not None and len(row) > rtt_index else None
                ),
            })
            connections.append(connection)
            continue
        match = re.match(r"^(.+)\.(\d+)$", label)
        if match is None:
            continue
        name, pid_text = match.groups()
        try:
            pid = int(pid_text)
        except ValueError:  # pragma: no cover - 正则已限制为数字
            continue
        owner = (pid, name)
        if len(row) <= max(in_index, out_index):
            continue
        processes.append({
            "pid": pid,
            "name": name,
            "bytes_in": float(_number(row[in_index]) or 0),
            "bytes_out": float(_number(row[out_index]) or 0),
        })
    return processes, connections


def parse_nettop_output(output: str) -> list[dict[str, Any]]:
    """兼容旧调用：解析 nettop CSV 中的进程累计收发字节。"""
    return _parse_nettop_rows(output)[0]


def parse_nettop_connections(output: str) -> list[dict[str, Any]]:
    """解析进程行与连接行混排的 nettop CSV。"""
    return _parse_nettop_rows(output)[1]


class ReverseDnsCache:
    """异步反向 DNS 的 TTL/LRU 缓存，包含失败结果的负缓存。"""

    def __init__(
        self,
        *,
        ttl_seconds: float = 3600,
        max_entries: int = 512,
        queue_size: int = 256,
        clock: Callable[[], float] = time.monotonic,
        resolver: Callable[[str], str | None] | None = None,
        start_worker: bool = True,
    ) -> None:
        self.ttl_seconds = float(ttl_seconds)
        self.max_entries = max(1, int(max_entries))
        self._clock = clock
        self._resolver = resolver
        self._cache: OrderedDict[str, tuple[float, str | None]] = OrderedDict()
        self._pending: set[str] = set()
        self._queue: queue.Queue[str | None] = queue.Queue(maxsize=max(1, queue_size))
        self._lock = threading.RLock()
        self._thread: threading.Thread | None = None
        if start_worker:
            self._thread = threading.Thread(
                target=self._run, name="bamai-reverse-dns", daemon=True
            )
            self._thread.start()

    @staticmethod
    def _is_public(ip: str) -> bool:
        try:
            address = ipaddress.ip_address(ip)
        except ValueError:
            return False
        return not (
            address.is_private or address.is_loopback or address.is_link_local
            or address.is_multicast or address.is_reserved or address.is_unspecified
        )

    def get(self, ip: str) -> str | None:
        with self._lock:
            entry = self._cache.get(ip)
            if entry is None:
                return None
            if entry[0] <= self._clock():
                del self._cache[ip]
                return None
            self._cache.move_to_end(ip)
            return entry[1]

    def request(self, ip: str) -> bool:
        if not self._is_public(ip):
            return False
        with self._lock:
            entry = self._cache.get(ip)
            if entry is not None and entry[0] > self._clock():
                self._cache.move_to_end(ip)
                return False
            if entry is not None:
                del self._cache[ip]
            if ip in self._pending:
                return False
            try:
                self._queue.put_nowait(ip)
            except queue.Full:
                return False
            self._pending.add(ip)
            return True

    def request_many(self, ips: list[str] | set[str], *, limit: int = 20) -> int:
        accepted = 0
        for ip in ips:
            if accepted >= max(0, int(limit)):
                break
            if self.request(ip):
                accepted += 1
        return accepted

    def _lookup(self, ip: str) -> str | None:
        if self._resolver is not None:
            return self._resolver(ip)
        sockaddr: tuple[Any, ...] = (ip, 0, 0, 0) if ":" in ip else (ip, 0)
        try:
            host = socket.getnameinfo(sockaddr, socket.NI_NAMEREQD)[0]
        except (OSError, socket.gaierror):
            return None
        return host.rstrip(".") or None

    def _resolve(self, ip: str) -> None:
        try:
            domain = self._lookup(ip)
        except Exception:  # 解析器异常同样写负缓存，不能终止工作线程
            domain = None
        with self._lock:
            self._pending.discard(ip)
            self._cache[ip] = (self._clock() + self.ttl_seconds, domain)
            self._cache.move_to_end(ip)
            while len(self._cache) > self.max_entries:
                self._cache.popitem(last=False)

    def resolve_pending_once(self) -> bool:
        """同步消费一个排队项，供确定性测试使用。"""
        try:
            ip = self._queue.get_nowait()
        except queue.Empty:
            return False
        if ip is None:
            return False
        self._resolve(ip)
        self._queue.task_done()
        return True

    def _run(self) -> None:
        while True:
            ip = self._queue.get()
            if ip is None:
                self._queue.task_done()
                return
            self._resolve(ip)
            self._queue.task_done()

    def close(self) -> None:
        if self._thread is None:
            return
        try:
            self._queue.put_nowait(None)
        except queue.Full:
            return
        self._thread.join(timeout=1)
        self._thread = None


class Collector:
    def __init__(self, db: Database, rules: RuleEngine | None = None) -> None:
        self.db = db
        self.rules = rules or RuleEngine(db)
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._disk_prev: tuple[float, Any] | None = None
        self._net_prev: tuple[float, Any] | None = None
        self._connection_prev: dict[tuple[Any, ...], tuple[float, float, float]] = {}
        self._latest_processes: list[dict[str, Any]] = []
        self._latest_connection_rates: list[dict[str, Any]] = []
        self._reverse_dns = ReverseDnsCache()
        self._last_cleanup = 0.0

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run, name="bamai-collector", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=config.SAMPLE_INTERVAL_SECONDS + 2)
        self._reverse_dns.close()

    def _run(self) -> None:
        # 首轮即采集慢速项目，之后保持每 60 秒一次。
        tick = config.SLOW_SAMPLE_TICKS - 1
        logger.info(
            "监控采集线程已启动，采样间隔 %s 秒", config.SAMPLE_INTERVAL_SECONDS
        )
        while not self._stop_event.is_set():
            started = time.monotonic()
            now = int(time.time())
            self._safe("基础指标采集", self._collect_metric, now)
            tick += 1
            if tick % config.SLOW_SAMPLE_TICKS == 0:
                self._safe("进程快照", self._collect_processes, now)
                self._safe("磁盘容量", self._collect_disk_usage, now)
                self._safe("进程网络", self._collect_process_net, now)
                self._safe("应用快照", self._collect_apps, now)
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
        processes = list_application_processes()
        self._latest_processes = processes
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
        self._latest_connection_rates = []
        completed = subprocess.run(
            ["nettop", "-L", "1", "-x", "-J", "bytes_in,bytes_out,rtt_avg"],
            capture_output=True, text=True, timeout=config.NETTOP_TIMEOUT_SECONDS,
            check=False,
        )
        if completed.returncode != 0:
            raise RuntimeError(
                completed.stderr.strip() or f"nettop 退出码 {completed.returncode}"
            )
        now_mono = time.monotonic()
        current = parse_nettop_connections(completed.stdout)
        rates: list[dict[str, Any]] = []
        next_previous: dict[tuple[Any, ...], tuple[float, float, float]] = {}
        for row in current:
            key = (
                row["pid"], row["proto"], row["local_ip"], row["local_port"],
                row["remote_ip"], row["remote_port"],
            )
            bytes_in = float(row["bytes_in"])
            bytes_out = float(row["bytes_out"])
            previous = self._connection_prev.get(key)
            next_previous[key] = (now_mono, bytes_in, bytes_out)
            if previous is None:
                continue
            elapsed = max(0.001, now_mono - previous[0])
            rates.append({**row,
                "up_bps": max(0.0, (bytes_out - previous[2]) / elapsed),
                "down_bps": max(0.0, (bytes_in - previous[1]) / elapsed),
            })
        self._connection_prev = next_previous

        loopback_owners: dict[tuple[str, int], tuple[int, str]] = {}
        for row in current:
            if self._is_loopback(row["local_ip"]):
                loopback_owners[(row["local_ip"], row["local_port"])] = (
                    row["pid"], row["name"]
                )
        for row in rates:
            row["via_proxy"] = int(self._is_loopback(row["remote_ip"]))
            row["proxy_name"] = None
            if row["via_proxy"]:
                owner = loopback_owners.get((row["remote_ip"], row["remote_port"]))
                if owner is not None and owner[0] != row["pid"]:
                    row["proxy_name"] = owner[1]

        process_rates: dict[tuple[int, str], dict[str, Any]] = {}
        for row in rates:
            key = (row["pid"], row["name"])
            item = process_rates.setdefault(key, {
                "pid": row["pid"], "name": row["name"],
                "up_bps": 0.0, "down_bps": 0.0,
            })
            item["up_bps"] += row["up_bps"]
            item["down_bps"] += row["down_bps"]
        self._latest_connection_rates = rates
        self.db.insert_process_net(ts, process_rates.values())

    @staticmethod
    def _is_loopback(ip: str) -> bool:
        try:
            return ipaddress.ip_address(ip).is_loopback
        except ValueError:
            return False

    def _collect_apps(self, ts: int) -> None:
        groups = group_captured_processes(self._latest_processes)
        processes_by_pid = {
            int(process["pid"]): process for process in self._latest_processes
        }
        pid_apps = {
            pid: group["app"]
            for group in groups
            for pid in group["pids"]
        }
        aggregated: dict[tuple[str, str, int], dict[str, Any]] = {}
        lookup_ips: set[str] = set()
        for row in self._latest_connection_rates:
            app = pid_apps.get(row["pid"])
            if app is None:
                continue
            remote_ip = row["remote_ip"]
            domain = self._reverse_dns.get(remote_ip)
            if domain is None:
                lookup_ips.add(remote_ip)
            key = (app, remote_ip, row["remote_port"])
            item = aggregated.setdefault(key, {
                "app": app,
                "remote_ip": remote_ip,
                "remote_port": row["remote_port"],
                "domain": domain,
                "proto": row["proto"],
                "up_bps": 0.0,
                "down_bps": 0.0,
                "rtt_weighted": 0.0,
                "rtt_weight": 0.0,
                "rtt_total": 0.0,
                "rtt_count": 0,
                "via_proxy": int(row["via_proxy"]),
                "proxy_name": row["proxy_name"],
            })
            if item["domain"] is None and domain is not None:
                item["domain"] = domain
            item["up_bps"] += row["up_bps"]
            item["down_bps"] += row["down_bps"]
            item["via_proxy"] = max(item["via_proxy"], int(row["via_proxy"]))
            if item["proxy_name"] is None and row["proxy_name"]:
                item["proxy_name"] = row["proxy_name"]
            if row["rtt_ms"] is not None:
                weight = row["up_bps"] + row["down_bps"]
                item["rtt_total"] += float(row["rtt_ms"])
                item["rtt_count"] += 1
                if weight > 0:
                    item["rtt_weighted"] += float(row["rtt_ms"]) * weight
                    item["rtt_weight"] += weight
        self._reverse_dns.request_many(sorted(lookup_ips), limit=20)

        connections: list[dict[str, Any]] = []
        app_network: dict[str, tuple[float, float]] = {}
        for item in aggregated.values():
            if item["rtt_weight"] > 0:
                rtt_ms = item["rtt_weighted"] / item["rtt_weight"]
            elif item["rtt_count"]:
                rtt_ms = item["rtt_total"] / item["rtt_count"]
            else:
                rtt_ms = None
            connections.append({
                "app": item["app"], "remote_ip": item["remote_ip"],
                "remote_port": item["remote_port"], "domain": item["domain"],
                "proto": item["proto"], "up_bps": item["up_bps"],
                "down_bps": item["down_bps"], "rtt_ms": rtt_ms,
                "via_proxy": item["via_proxy"], "proxy_name": item["proxy_name"],
            })
            up, down = app_network.get(item["app"], (0.0, 0.0))
            app_network[item["app"]] = (
                up + item["up_bps"], down + item["down_bps"]
            )

        snapshots = []
        process_snapshots = []
        for group in groups:
            up_bps, down_bps = app_network.get(group["app"], (0.0, 0.0))
            snapshots.append({
                **group,
                "up_bps": up_bps,
                "down_bps": down_bps,
            })
            by_name: dict[str, dict[str, Any]] = {}
            for pid in group["pids"]:
                process = processes_by_pid.get(pid)
                if process is None:
                    continue
                name = str(process.get("name") or "未知")
                item = by_name.setdefault(name, {
                    "app": group["app"],
                    "name": name,
                    "cpu_percent": 0.0,
                    "memory_rss": 0,
                    "proc_count": 0,
                })
                item["cpu_percent"] += float(process.get("cpu_percent") or 0)
                item["memory_rss"] += int(process.get("memory_rss") or 0)
                item["proc_count"] += 1
            process_snapshots.extend(by_name.values())
        self.db.insert_app_snapshots(ts, snapshots)
        self.db.insert_app_process_snapshots(ts, process_snapshots)
        self.db.insert_app_connections(ts, connections)


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


def search_running_processes(keyword: str, limit: int = 20) -> list[dict[str, Any]]:
    """按名称或命令行模糊匹配当前进程。"""
    needle = keyword.strip().lower()
    if not needle:
        return []
    matched: list[dict[str, Any]] = []
    for process in psutil.process_iter(["pid", "name", "cpu_percent", "memory_info", "cmdline"]):
        try:
            info = process.info
            name = info.get("name") or "未知"
            cmdline = " ".join(info.get("cmdline") or [])[:200]
            if needle not in name.lower() and needle not in cmdline.lower():
                continue
            memory = info.get("memory_info")
            matched.append({
                "pid": int(info["pid"]), "name": name,
                "cpu_percent": float(info.get("cpu_percent") or 0),
                "memory_rss": int(memory.rss if memory else 0), "cmdline": cmdline,
            })
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            continue
    matched.sort(key=lambda row: (row["cpu_percent"], row["memory_rss"]), reverse=True)
    return matched[:max(1, min(int(limit), 50))]
