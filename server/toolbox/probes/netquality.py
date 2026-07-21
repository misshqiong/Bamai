from __future__ import annotations

import re

from ..base import ProbeResult, ProbeSpec
from .common import output_of, run_command


def _number(pattern: str, output: str) -> float | None:
    match = re.search(pattern, output, re.IGNORECASE)
    return float(match.group(1)) if match else None


def parse_network_quality_output(output: str) -> dict:
    return {
        "upload_mbps": _number(r"(?:Uplink capacity|Upload capacity):\s*([\d.]+)\s*Mbps", output),
        "download_mbps": _number(r"(?:Downlink capacity|Download capacity):\s*([\d.]+)\s*Mbps", output),
        "responsiveness_rpm": _number(r"Responsiveness:.*?\(([\d.]+)\s*RPM\)", output),
    }


def run(params: dict) -> ProbeResult:
    result = run_command(["networkQuality", "-v"], 90)
    raw = output_of(result)
    return ProbeResult(parse_network_quality_output(raw), raw_output=raw)


def get_spec() -> ProbeSpec:
    return ProbeSpec(
        id="netquality", name_key="toolbox.probe.netquality.name",
        desc_key="toolbox.probe.netquality.desc", icon="⌁", params=(), timeout=90, runner=run,
    )
