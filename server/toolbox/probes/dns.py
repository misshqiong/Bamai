from __future__ import annotations

import subprocess
import time
from typing import Any

from ..base import ProbeParam, ProbeResult, ProbeSpec
from .common import output_of, run_command
from .ping import validate_host

DNS_TYPES = ("A", "AAAA", "CNAME", "MX", "TXT")
DNS_RESOLVERS = {
    "system": None,
    "ali": "223.5.5.5",
    "google": "8.8.8.8",
}


def validate_params(params: dict) -> None:
    validate_host(params["domain"])


def parse_dig_output(output: str) -> list[str]:
    return [
        line.strip()
        for line in output.splitlines()
        if line.strip() and not line.startswith(";")
    ]


def _query(
    domain: str, record_type: str, resolver: str, timeout: float
) -> tuple[list[dict[str, Any]], str, bool]:
    command = ["dig", "+short"]
    address = DNS_RESOLVERS[resolver]
    if address is not None:
        command.append(f"@{address}")
    command.extend([domain, record_type])
    try:
        result = run_command(command, timeout)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return [{"resolver": resolver, "record": None, "error": str(exc)}], str(exc), False
    raw = output_of(result)
    if result.returncode != 0:
        error = raw or f"dig 退出码 {result.returncode}"
        return [{"resolver": resolver, "record": None, "error": error}], raw, False
    records = parse_dig_output(result.stdout)
    return [{"resolver": resolver, "record": item} for item in records], raw, True


def run(params: dict) -> ProbeResult:
    started = time.monotonic()
    selected = params.get("resolver", "system")
    resolvers = list(DNS_RESOLVERS) if selected == "compare" else [selected]
    rows: list[dict[str, Any]] = []
    raw_parts = []
    succeeded: dict[str, bool] = {}
    for resolver in resolvers:
        elapsed = time.monotonic() - started
        timeout = min(10.0, max(0.1, 25.0 - elapsed)) if selected == "compare" else 10.0
        resolver_rows, raw, ok = _query(params["domain"], params["type"], resolver, timeout)
        rows.extend(resolver_rows)
        succeeded[resolver] = ok
        raw_parts.append(f"[{resolver}]\n{raw}".strip())

    elapsed_ms = round((time.monotonic() - started) * 1000, 1)
    records = [row for row in rows if row.get("record") is not None]
    summary: dict[str, Any] = {
        "record_count": len(records),
        "elapsed_ms": elapsed_ms,
        "resolver": selected,
    }
    if selected == "compare":
        if params["type"] in {"A", "AAAA"}:
            record_sets = [
                {row["record"] for row in rows if row["resolver"] == resolver and row["record"]}
                for resolver in resolvers
            ]
            summary["consistent"] = all(succeeded.values()) and all(
                records_for_resolver == record_sets[0]
                for records_for_resolver in record_sets[1:]
            )
        else:
            summary["consistent"] = None
    return ProbeResult(summary, rows=rows, raw_output="\n\n".join(raw_parts))


def get_spec() -> ProbeSpec:
    return ProbeSpec(
        id="dns", name_key="toolbox.probe.dns.name", desc_key="toolbox.probe.dns.desc",
        icon="DNS", timeout=25, runner=run, validator=validate_params,
        params=(
            ProbeParam("domain", "str", "toolbox.param.domain", required=True, max=253),
            ProbeParam(
                "type",
                "choice",
                "toolbox.param.recordType",
                default="A",
                choices=DNS_TYPES,
            ),
            ProbeParam(
                "resolver",
                "choice",
                "toolbox.param.resolver",
                default="system",
                choices=("system", "ali", "google", "compare"),
            ),
        ),
    )
