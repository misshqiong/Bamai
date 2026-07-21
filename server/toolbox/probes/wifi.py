from __future__ import annotations

import json
import re
from typing import Any

from ..base import ProbeResult, ProbeSpec
from .common import output_of, run_command


def _walk(value: Any):
    if isinstance(value, dict):
        yield value
        for item in value.values():
            yield from _walk(item)
    elif isinstance(value, list):
        for item in value:
            yield from _walk(item)


def parse_wifi_json(output: str) -> dict:
    payload = json.loads(output)
    network = None
    for item in _walk(payload):
        candidate = item.get("spairport_current_network_information")
        if isinstance(candidate, dict):
            network = candidate
            break
    if network is None:
        return {"connected": False, "ssid": None, "channel": None, "rssi_dbm": None,
                "noise_dbm": None, "tx_rate_mbps": None}
    signal = str(network.get("spairport_signal_noise") or "")
    numbers = [int(value) for value in re.findall(r"-?\d+", signal)]
    return {
        "connected": True,
        "ssid": network.get("_name") or network.get("spairport_network_ssid"),
        "channel": network.get("spairport_network_channel"),
        "rssi_dbm": numbers[0] if numbers else None,
        "noise_dbm": numbers[1] if len(numbers) > 1 else None,
        "tx_rate_mbps": network.get("spairport_network_rate"),
    }


def run(params: dict) -> ProbeResult:
    result = run_command(["system_profiler", "SPAirPortDataType", "-json"], 30)
    raw = output_of(result)
    return ProbeResult(parse_wifi_json(result.stdout), raw_output=raw)


def get_spec() -> ProbeSpec:
    return ProbeSpec(
        id="wifi", name_key="toolbox.probe.wifi.name", desc_key="toolbox.probe.wifi.desc",
        icon="⌁", params=(), timeout=30, runner=run,
    )
