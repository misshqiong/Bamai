from __future__ import annotations

import time

from ..base import ProbeParam, ProbeResult, ProbeSpec
from .common import output_of, run_command
from .ping import validate_host


def validate_params(params: dict) -> None:
    validate_host(params["host"])


def run(params: dict) -> ProbeResult:
    started = time.monotonic()
    result = run_command(["nc", "-z", "-G", "5", params["host"], str(params["port"])], 7)
    elapsed = round((time.monotonic() - started) * 1000, 1)
    return ProbeResult(
        {"status": "open" if result.returncode == 0 else "closed", "elapsed_ms": elapsed},
        raw_output=output_of(result),
    )


def get_spec() -> ProbeSpec:
    return ProbeSpec(
        id="port", name_key="toolbox.probe.port.name", desc_key="toolbox.probe.port.desc",
        icon="⚯", timeout=7, runner=run, validator=validate_params,
        params=(
            ProbeParam("host", "str", "toolbox.param.host", required=True, max=253),
            ProbeParam("port", "int", "toolbox.param.port", required=True, min=1, max=65535),
        ),
    )
