from __future__ import annotations

import re

from ..base import ProbeParam, ProbeResult, ProbeSpec
from .common import output_of, run_command
from .ping import validate_host


def validate_params(params: dict) -> None:
    validate_host(params["host"])


def parse_traceroute_output(output: str) -> list[dict]:
    rows = []
    for line in output.splitlines():
        match = re.match(r"\s*(\d+)\s+(.+)$", line)
        if not match:
            continue
        hop = int(match.group(1))
        rest = match.group(2)
        if rest.strip().startswith("*"):
            rows.append({"hop": hop, "host": "*", "ip": None, "latency_ms": None})
            continue
        host_match = re.match(r"([^\s(]+)(?:\s+\(([^)]+)\))?", rest)
        latency = re.search(r"([\d.]+)\s*ms", rest)
        if host_match:
            host = host_match.group(1)
            rows.append({
                "hop": hop,
                "host": host,
                "ip": host_match.group(2) or (host if re.fullmatch(r"[\d.:]+", host) else None),
                "latency_ms": float(latency.group(1)) if latency else None,
            })
    return rows


def run(params: dict) -> ProbeResult:
    result = run_command(["traceroute", "-m", "20", "-w", "2", params["host"]], 45)
    raw = output_of(result)
    rows = parse_traceroute_output(raw)
    reached = bool(rows and rows[-1]["host"] != "*")
    return ProbeResult({"hops": len(rows), "reached": reached}, rows=rows, raw_output=raw)


def get_spec() -> ProbeSpec:
    return ProbeSpec(
        id="traceroute", name_key="toolbox.probe.traceroute.name",
        desc_key="toolbox.probe.traceroute.desc", icon="⇢", timeout=45,
        runner=run, validator=validate_params,
        params=(ProbeParam("host", "str", "toolbox.param.host", required=True, max=253),),
    )
