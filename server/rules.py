"""基于近期采样数据的规则引擎与异步 AI 诊断触发。"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Callable

from . import config
from .db import Database
from .localization import normalize_language, render_event_text
from .notify import send_notification

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RuleResult:
    # None 表示采样窗口不足，既不触发也不错误地标记恢复。
    active: bool | None
    severity: str
    params: dict


class RuleEngine:
    def __init__(
        self,
        db: Database,
        notifier: Callable[[str, str], bool] = send_notification,
        diagnoser: Callable[[int, dict], object] | None = None,
        language_provider: Callable[[], str] = lambda: "zh",
    ) -> None:
        self.db = db
        self.notifier = notifier
        self.diagnoser = diagnoser
        self.language_provider = language_provider

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
                    language = normalize_language(self.language_provider())
                    title, detail = render_event_text(kind, result.params, language)
                    event_id = self.db.create_event(
                        now, kind, result.severity, title, detail, result.params
                    )
                    created.append(event_id)
                    try:
                        self.notifier(title, detail)
                    except Exception as exc:
                        logger.warning("事件 %s 的系统通知失败: %s", event_id, exc)
                    if self.diagnoser is not None:
                        try:
                            self.diagnoser(event_id, {
                                "ts": now, "kind": kind, "severity": result.severity,
                                "title": title, "detail": detail,
                                "params": result.params, "language": language,
                            })
                        except Exception as exc:
                            logger.warning("事件 %s 的 AI 诊断调度失败: %s", event_id, exc)
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
            {"percent": round(avg, 1)},
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
        return RuleResult(active, "warning", {
            "percent": round(latest_percent, 1),
            "growth_gb": round(max(0, swap_growth) / 1024**3, 1),
        })

    def _disk_full(self) -> RuleResult:
        rows = self.db.latest_disk_usage()
        if not rows:
            return RuleResult(None, "warning", {"mount": "—", "percent": 0})
        over = [row for row in rows if float(row["percent"]) > config.DISK_WARNING_PERCENT]
        if not over:
            return RuleResult(False, "warning", {"mount": "—", "percent": 0})
        worst = max(over, key=lambda row: float(row["percent"]))
        severity = (
            "critical" if float(worst["percent"]) > config.DISK_CRITICAL_PERCENT
            else "warning"
        )
        return RuleResult(
            True, severity,
            {"mount": worst["mount"], "percent": round(float(worst["percent"]), 1)},
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
            active, "info", {
                "recent_mbps": round(recent_avg / 1024**2, 1),
                "baseline_mbps": round(baseline_avg / 1024**2, 1),
            },
        )

    @staticmethod
    def _covers_window(rows: list[dict], start: int) -> bool:
        return bool(rows) and int(rows[0]["ts"]) <= start + config.SAMPLE_INTERVAL_SECONDS * 2


class HealthEvaluator:
    """独立于事件去重的当前健康计算，不调用 AI。"""

    def __init__(self, db: Database) -> None:
        self.db = db

    def evaluate(self, now: int | None = None) -> dict:
        now = int(time.time()) if now is None else int(now)
        snapshots = self.db.latest_process_snapshots(30)
        checks = [
            self._cpu(now, snapshots),
            self._memory(now, snapshots),
            self._disk(),
            self._network(now),
            self._swap(now),
        ]
        levels = {check["level"] for check in checks}
        level = "critical" if "critical" in levels else "warn" if "warn" in levels else "ok"
        return {
            "level": level,
            "checks": checks,
            "ai_analysis": self.db.latest_unresolved_ai_analysis(),
        }

    def _cpu(self, now: int, snapshots: list[dict]) -> dict:
        start = now - config.CPU_HIGH_DURATION_SECONDS
        rows = self.db.raw_metrics(start, now)
        average = sum(float(row["cpu_percent"]) for row in rows) / len(rows) if rows else 0
        active = RuleEngine._covers_window(rows, start) and average > config.CPU_HIGH_PERCENT
        top = max(snapshots, key=lambda row: float(row["cpu_percent"]), default=None)
        return self._check("cpu", "warn" if active else "ok", {
            "percent": round(average, 1),
            "top_name": top["name"] if top else None,
        })

    def _memory(self, now: int, snapshots: list[dict]) -> dict:
        latest = self.db.latest_metric()
        percent = float(latest["mem_percent"]) if latest else 0
        top = max(snapshots, key=lambda row: int(row["memory_rss"]), default=None)
        return self._check("memory", "warn" if percent > config.MEM_HIGH_PERCENT else "ok", {
            "percent": round(percent, 1),
            "top_name": top["name"] if top else None,
            "top_gb": round(int(top["memory_rss"]) / 1024**3, 1) if top else 0,
        })

    def _disk(self) -> dict:
        disks = self.db.latest_disk_usage()
        worst = max(disks, key=lambda row: float(row["percent"]), default=None)
        percent = float(worst["percent"]) if worst else 0
        level = (
            "critical" if percent > config.DISK_CRITICAL_PERCENT
            else "warn" if percent > config.DISK_WARNING_PERCENT else "ok"
        )
        free_gb = (
            round((int(worst["total"]) - int(worst["used"])) / 1024**3, 1)
            if worst else 0
        )
        return self._check("disk", level, {
            "percent": round(percent, 1), "mount": worst["mount"] if worst else None,
            "free_gb": free_gb,
        })

    def _network(self, now: int) -> dict:
        recent_start = now - config.NET_SPIKE_DURATION_SECONDS
        rows = self.db.raw_metrics(now - config.NET_BASELINE_SECONDS, now)
        recent = [row for row in rows if int(row["ts"]) >= recent_start]
        baseline = [row for row in rows if int(row["ts"]) < recent_start]
        recent_rates = [float(row["net_up_bps"]) + float(row["net_down_bps"]) for row in recent]
        baseline_rates = [float(row["net_up_bps"]) + float(row["net_down_bps"]) for row in baseline]
        recent_avg = sum(recent_rates) / len(recent_rates) if recent_rates else 0
        baseline_avg = sum(baseline_rates) / len(baseline_rates) if baseline_rates else 0
        covered = (
            RuleEngine._covers_window(rows, now - config.NET_BASELINE_SECONDS)
            and RuleEngine._covers_window(recent, recent_start) and bool(baseline_rates)
        )
        threshold = max(config.NET_SPIKE_MIN_BPS, baseline_avg * config.NET_SPIKE_MULTIPLIER)
        active = covered and all(rate > threshold for rate in recent_rates)
        process_rows = self.db.latest_process_net()
        top = process_rows[0] if process_rows else None
        return self._check("network", "warn" if active else "ok", {
            "mbps": round(recent_avg / 1024**2, 1),
            "baseline_mbps": round(baseline_avg / 1024**2, 1),
            "top_name": top["name"] if top else None,
        })

    def _swap(self, now: int) -> dict:
        start = now - config.SWAP_WINDOW_SECONDS
        rows = self.db.raw_metrics(start, now)
        covered = RuleEngine._covers_window(rows, start)
        growth = int(rows[-1]["swap_used"]) - int(rows[0]["swap_used"]) if covered else 0
        used = int(rows[-1]["swap_used"]) if rows else 0
        return self._check("swap", "warn" if growth > config.SWAP_GROWTH_BYTES else "ok", {
            "growth_gb": round(max(0, growth) / 1024**3, 1),
            "used_gb": round(used / 1024**3, 1),
        })

    @staticmethod
    def _check(kind: str, level: str, params: dict) -> dict:
        return {"kind": kind, "level": level, "params": params}
