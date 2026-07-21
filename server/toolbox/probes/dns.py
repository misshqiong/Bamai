from __future__ import annotations

import time

from ..base import ProbeParam, ProbeResult, ProbeSpec
from .common import output_of, run_command
from .ping import validate_host

DNS_TYPES = ("A", "AAAA", "CNAME", "MX", "TXT")


def validate_params(params: dict) -> None:
    validate_host(params["domain"])


def parse_dig_output(output: str) -> list[str]:
    return [
        line.strip()
        for line in output.splitlines()
        if line.strip() and not line.startswith(";")
    ]


def run(params: dict) -> ProbeResult:
    started = time.monotonic()
    result = run_command(["dig", "+short", params["domain"], params["type"]], 10)
    elapsed = round((time.monotonic() - started) * 1000, 1)
    raw = output_of(result)
    records = parse_dig_output(result.stdout)
    return ProbeResult(
        {"record_count": len(records), "elapsed_ms": elapsed},
        rows=[{"record": item} for item in records], raw_output=raw,
    )


def get_spec() -> ProbeSpec:
    return ProbeSpec(
        id="dns", name_key="toolbox.probe.dns.name", desc_key="toolbox.probe.dns.desc",
        icon="DNS", timeout=10, runner=run, validator=validate_params,
        params=(
            ProbeParam("domain", "str", "toolbox.param.domain", required=True, max=253),
            ProbeParam(
                "type",
                "choice",
                "toolbox.param.recordType",
                default="A",
                choices=DNS_TYPES,
            ),
        ),
    )
