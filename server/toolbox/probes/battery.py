from __future__ import annotations

import json
import re
from typing import Any

from ..base import ProbeResult, ProbeSpec
from .common import output_of, run_command


def _find_key(value: Any, names: set[str]) -> Any:
    if isinstance(value, dict):
        for key, item in value.items():
            if key in names:
                return item
            found = _find_key(item, names)
            if found is not None:
                return found
    elif isinstance(value, list):
        for item in value:
            found = _find_key(item, names)
            if found is not None:
                return found
    return None


def parse_pmset_output(output: str) -> dict:
    percent = re.search(r"(\d+)%", output)
    status = re.search(r"\d+%;\s*([^;\n]+)", output)
    return {
        "percent": int(percent.group(1)) if percent else None,
        "status": status.group(1).strip() if status else None,
    }


def parse_power_json(output: str) -> dict:
    payload = json.loads(output)
    return {
        "cycle_count": _find_key(payload, {"sppower_battery_cycle_count", "cycle_count"}),
        "health": _find_key(payload, {
            "sppower_battery_health", "sppower_battery_condition", "condition",
        }),
    }


def run(params: dict) -> ProbeResult:
    batt = run_command(["pmset", "-g", "batt"], 8)
    power = run_command(["system_profiler", "SPPowerDataType", "-json"], 30)
    summary = parse_pmset_output(batt.stdout)
    summary.update(parse_power_json(power.stdout))
    raw = f"{output_of(batt)}\n\n{output_of(power)}".strip()
    return ProbeResult(summary, raw_output=raw)


def get_spec() -> ProbeSpec:
    return ProbeSpec(
        id="battery", name_key="toolbox.probe.battery.name",
        desc_key="toolbox.probe.battery.desc", icon="▰", params=(), timeout=35, runner=run,
    )
