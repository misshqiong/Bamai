from __future__ import annotations

import re
import time

from ...db import Database
from ..base import ProbeParam, ProbeResult, ProbeSpec
from .common import output_of, run_command


def parse_memory_pressure_output(output: str) -> dict:
    free = re.search(r"System-wide memory free percentage:\s*(\d+)%", output, re.IGNORECASE)
    percent = int(free.group(1)) if free else None
    lowered = output.lower()
    if "critical" in lowered or (percent is not None and percent <= 10):
        level = "critical"
    elif "warn" in lowered or (percent is not None and percent <= 20):
        level = "warning"
    else:
        level = "normal"
    return {"pressure_level": level, "free_percent": percent}


def parse_vm_stat_output(output: str) -> dict:
    page = re.search(r"page size of (\d+) bytes", output)
    page_size = int(page.group(1)) if page else 4096
    values: dict[str, int] = {}
    for line in output.splitlines():
        match = re.match(r"([^:]+):\s*([\d.]+)\.?", line)
        if match:
            values[match.group(1).strip().lower()] = int(float(match.group(2)))
    reclaimable_pages = sum(values.get(key, 0) for key in (
        "pages free", "pages inactive", "pages speculative", "pages purgeable",
    ))
    return {"page_size": page_size, "reclaimable_bytes": reclaimable_pages * page_size}


def growing_processes(db: Database, start: int, end: int) -> list[dict]:
    with db.connect(read_only=True) as conn:
        rows = conn.execute(
            "WITH bounds AS ("
            " SELECT pid,name,MIN(ts) first_ts,MAX(ts) last_ts,COUNT(DISTINCT ts) samples"
            " FROM process_snapshots WHERE ts BETWEEN ? AND ? GROUP BY pid,name"
            ") SELECT b.pid,b.name,b.samples,f.memory_rss first_rss,l.memory_rss last_rss "
            "FROM bounds b "
            "JOIN process_snapshots f ON f.pid=b.pid AND f.name=b.name AND f.ts=b.first_ts "
            "JOIN process_snapshots l ON l.pid=b.pid AND l.name=b.name AND l.ts=b.last_ts "
            "WHERE b.samples>=2 AND l.memory_rss>f.memory_rss "
            "ORDER BY (l.memory_rss-f.memory_rss) DESC LIMIT 5",
            (start, end),
        ).fetchall()
    return [{
        "pid": row["pid"], "name": row["name"], "samples": row["samples"],
        "first_rss": row["first_rss"], "last_rss": row["last_rss"],
        "growth_bytes": row["last_rss"] - row["first_rss"],
    } for row in rows]


def make_runner(db: Database):
    def run(params: dict) -> ProbeResult:
        pressure_result = run_command(["memory_pressure"], 12)
        vm_result = run_command(["vm_stat"], 8)
        pressure_raw = output_of(pressure_result)
        vm_raw = output_of(vm_result)
        summary = parse_memory_pressure_output(pressure_raw)
        summary.update(parse_vm_stat_output(vm_raw))
        end = int(time.time())
        rows = growing_processes(db, end - params["window_min"] * 60, end)
        summary["growing_process_count"] = len(rows)
        return ProbeResult(summary, rows=rows, raw_output=f"{pressure_raw}\n\n{vm_raw}".strip())
    return run


def get_spec(db: Database) -> ProbeSpec:
    return ProbeSpec(
        id="memory_check", name_key="toolbox.probe.memory_check.name",
        desc_key="toolbox.probe.memory_check.desc", icon="▦", timeout=25,
        runner=make_runner(db),
        params=(ProbeParam(
            "window_min", "int", "toolbox.param.windowMinutes", default=15, min=5, max=60,
        ),),
    )
