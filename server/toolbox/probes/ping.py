from __future__ import annotations

import re

from ..base import ProbeParam, ProbeResult, ProbeSpec, ProbeValidationError
from .common import output_of, run_command

HOST_PATTERN = re.compile(r"^[A-Za-z0-9.:-]{1,253}$")


def validate_host(host: str) -> None:
    if not HOST_PATTERN.fullmatch(host):
        raise ProbeValidationError("host 只能包含字母、数字、点、连字符和冒号")


def validate_params(params: dict) -> None:
    validate_host(params["host"])


def parse_ping_output(output: str) -> dict:
    loss = re.search(r"([\d.]+)% packet loss", output)
    timing = re.search(
        r"(?:round-trip|rtt) min/avg/max/(?:stddev|mdev) = "
        r"([\d.]+)/([\d.]+)/([\d.]+)/[\d.]+ ms",
        output,
    )
    return {
        "packet_loss_percent": float(loss.group(1)) if loss else 100.0,
        "min_ms": float(timing.group(1)) if timing else None,
        "avg_ms": float(timing.group(2)) if timing else None,
        "max_ms": float(timing.group(3)) if timing else None,
    }


def run(params: dict) -> ProbeResult:
    result = run_command(["ping", "-c", str(params["count"]), params["host"]], 20)
    raw = output_of(result)
    return ProbeResult(parse_ping_output(raw), raw_output=raw)


def get_spec() -> ProbeSpec:
    return ProbeSpec(
        id="ping", name_key="toolbox.probe.ping.name", desc_key="toolbox.probe.ping.desc",
        icon="↔", timeout=20, runner=run, validator=validate_params,
        params=(
            ProbeParam("host", "str", "toolbox.param.host", required=True, max=253),
            ProbeParam("count", "int", "toolbox.param.count", default=4, min=1, max=10),
        ),
    )
