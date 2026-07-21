"""基于近期采样数据的规则引擎。Phase 1 仅生成事件，不做 AI 诊断。"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Callable

from . import config
from .db import Database
from .notify import send_notification


@dataclass(frozen=True)
class RuleResult:
    # None 表示采样窗口不足，既不触发也不错误地标记恢复。
    active: bool | None
    severity: str
    title: str
    detail: str


class RuleEngine:
    def __init__(
        self,
        db: Database,
        notifier: Callable[[str, str], bool] = send_notification,
    ) -> None:
        self.db = db
        self.notifier = notifier

    def evaluate(self, now: int | None = None) -> list[int]:
        now = int(time.time()) if now is None else int(now)
        results = {
            "cpu_high": self._cpu_high(now),
            "mem_pressure": self._mem_pressure(now),
            "disk_full": self._disk_full(),
            "net_spike": self._net_spike(now),
        }
        created: list[int] = []
        for kind, result in results.items():
            if result.active:
                if self.db.recent_event(kind, now - config.EVENT_DEDUP_SECONDS) is None:
                    event_id = self.db.create_event(
                        now, kind, result.severity, result.title, result.detail
                    )
                    created.append(event_id)
                    self.notifier(result.title, result.detail)
            elif result.active is False:
                self.db.resolve_events(kind, now)
        return created

    def _cpu_high(self, now: int) -> RuleResult:
        start = now - config.CPU_HIGH_DURATION_SECONDS
        rows = self.db.raw_metrics(start, now)
        avg = sum(float(row["cpu_percent"]) for row in rows) / len(rows) if rows else 0
        covered = self._covers_window(rows, start)
        sustained = avg > config.CPU_HIGH_PERCENT if covered else None
        return RuleResult(
            sustained,
            "warning",
            "CPU 持续高负载",
            f"最近 3 分钟 CPU 平均使用率为 {avg:.1f}%（阈值 85%）。",
        )

    def _mem_pressure(self, now: int) -> RuleResult:
        rows = self.db.raw_metrics(now - config.SWAP_WINDOW_SECONDS, now)
        latest_percent = float(rows[-1]["mem_percent"]) if rows else 0
        high_memory = latest_percent > config.MEM_HIGH_PERCENT
        swap_growth = 0
        covered = self._covers_window(rows, now - config.SWAP_WINDOW_SECONDS)
        if covered:
            swap_growth = int(rows[-1]["swap_used"]) - int(rows[0]["swap_used"])
        active = True if high_memory else (
            swap_growth > config.SWAP_GROWTH_BYTES if covered else None
        )
        reasons = []
        if high_memory:
            reasons.append(f"内存使用率 {latest_percent:.1f}%")
        if swap_growth > config.SWAP_GROWTH_BYTES:
            reasons.append(f"10 分钟 swap 增长 {swap_growth / 1024**3:.1f} GB")
        detail = "，".join(reasons) if reasons else "内存压力已恢复。"
        return RuleResult(active, "warning", "内存压力过高", detail)

    def _disk_full(self) -> RuleResult:
        rows = self.db.latest_disk_usage()
        if not rows:
            return RuleResult(None, "warning", "磁盘空间不足", "尚无磁盘容量采样。")
        over = [row for row in rows if float(row["percent"]) > config.DISK_WARNING_PERCENT]
        if not over:
            return RuleResult(False, "warning", "磁盘空间不足", "磁盘使用率已恢复。")
        worst = max(over, key=lambda row: float(row["percent"]))
        severity = (
            "critical" if float(worst["percent"]) > config.DISK_CRITICAL_PERCENT
            else "warning"
        )
        return RuleResult(
            True, severity, "磁盘空间不足",
            f"挂载点 {worst['mount']} 使用率为 {float(worst['percent']):.1f}%。",
        )

    def _net_spike(self, now: int) -> RuleResult:
        recent_start = now - config.NET_SPIKE_DURATION_SECONDS
        all_rows = self.db.raw_metrics(now - config.NET_BASELINE_SECONDS, now)
        recent = [row for row in all_rows if int(row["ts"]) >= recent_start]
        baseline = [row for row in all_rows if int(row["ts"]) < recent_start]
        recent_rates = [
            float(row["net_up_bps"]) + float(row["net_down_bps"]) for row in recent
        ]
        baseline_rates = [
            float(row["net_up_bps"]) + float(row["net_down_bps"]) for row in baseline
        ]
        baseline_avg = sum(baseline_rates) / len(baseline_rates) if baseline_rates else 0
        threshold = max(config.NET_SPIKE_MIN_BPS, baseline_avg * config.NET_SPIKE_MULTIPLIER)
        covered = (
            self._covers_window(all_rows, now - config.NET_BASELINE_SECONDS)
            and self._covers_window(recent, recent_start)
            and bool(baseline_rates)
        )
        active = all(rate > threshold for rate in recent_rates) if covered else None
        recent_avg = sum(recent_rates) / len(recent_rates) if recent_rates else 0
        return RuleResult(
            active, "info", "网络流量突增",
            f"最近 2 分钟平均流量 {recent_avg / 1024**2:.1f} MB/s，"
            f"过去基线 {baseline_avg / 1024**2:.1f} MB/s。",
        )

    @staticmethod
    def _covers_window(rows: list[dict], start: int) -> bool:
        return bool(rows) and int(rows[0]["ts"]) <= start + config.SAMPLE_INTERVAL_SECONDS * 2
