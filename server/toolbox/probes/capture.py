from __future__ import annotations

import os
import re
import subprocess
import time
from collections import Counter, defaultdict
from pathlib import Path

from ..base import ProbeParam, ProbeResult, ProbeSpec, ProbeValidationError
from .common import output_of, run_command

FILTER_PATTERN = re.compile(r"^[\w\s.():\-&|!]*$")
INTERFACE_PATTERN = re.compile(r"^[A-Za-z0-9._-]{1,64}$")


def capture_authorized(path: str | Path = "/dev/bpf0") -> bool:
    return os.access(path, os.R_OK)


def list_interfaces() -> tuple[str, ...]:
    try:
        result = run_command(["tcpdump", "-D"], 5)
    except (OSError, subprocess.TimeoutExpired):
        return ()
    interfaces = []
    for line in result.stdout.splitlines():
        match = re.match(r"\d+\.\s*([^\s]+)", line)
        if match and INTERFACE_PATTERN.fullmatch(match.group(1)):
            interfaces.append(match.group(1))
    return tuple(interfaces)


def validate_params(params: dict) -> None:
    if not INTERFACE_PATTERN.fullmatch(params["interface"]):
        raise ProbeValidationError("interface 格式无效")
    if not FILTER_PATTERN.fullmatch(params.get("filter", "")):
        raise ProbeValidationError("filter 包含不允许的字符")


def _endpoint(value: str) -> tuple[str, int | None]:
    match = re.match(r"(.+)\.(\d+)$", value)
    return (match.group(1), int(match.group(2))) if match else (value, None)


def parse_tcpdump_output(output: str) -> tuple[dict, list[dict]]:
    protocols: Counter[str] = Counter()
    sessions: dict[tuple, dict] = defaultdict(lambda: {"packets": 0, "bytes": 0})
    timestamps: list[float] = []
    for line in output.splitlines():
        match = re.match(r"(\d+(?:\.\d+)?)\s+(IP6?|ARP)\s+(.+)$", line.strip())
        if not match:
            continue
        timestamps.append(float(match.group(1)))
        network_proto, rest = match.group(2), match.group(3)
        if network_proto == "ARP" or " > " not in rest:
            protocols[network_proto] += 1
            continue
        source_text, destination_and_detail = rest.split(" > ", 1)
        destination_text, _, detail = destination_and_detail.partition(": ")
        source, source_port = _endpoint(source_text)
        destination, destination_port = _endpoint(destination_text.rstrip(":"))
        if "UDP" in detail:
            protocol = "UDP"
        elif "ICMP6" in detail:
            protocol = "ICMP6"
        elif "ICMP" in detail:
            protocol = "ICMP"
        else:
            protocol = "TCP"
        length = re.search(r"length\s+(\d+)", detail)
        byte_count = int(length.group(1)) if length else 0
        protocols[protocol] += 1
        key = (source, source_port, destination, destination_port, protocol)
        sessions[key]["packets"] += 1
        sessions[key]["bytes"] += byte_count
    rows = [{
        "source": key[0], "source_port": key[1], "destination": key[2],
        "destination_port": key[3], "protocol": key[4], **values,
    } for key, values in sessions.items()]
    rows.sort(key=lambda item: (item["bytes"], item["packets"]), reverse=True)
    summary = {
        "packet_count": sum(protocols.values()),
        "protocol_distribution": dict(protocols),
        "start_ts": min(timestamps) if timestamps else None,
        "end_ts": max(timestamps) if timestamps else None,
    }
    return summary, rows[:20]


def make_runner(captures_dir: Path):
    def run(params: dict) -> ProbeResult:
        captures_dir.mkdir(parents=True, exist_ok=True)
        name = f"{time.time_ns()}.pcap"
        path = captures_dir / name
        command = [
            "tcpdump", "-i", params["interface"], "-s", "96", "-nn",
            "-c", str(params["max_packets"]), "-G", str(params["duration"]),
            "-W", "1", "-w", str(path),
        ]
        if params.get("filter"):
            command.append(params["filter"])
        capture_result = run_command(command, params["duration"] + 8)
        parsed = run_command(["tcpdump", "-nn", "-tt", "-r", str(path)], 20)
        summary, rows = parse_tcpdump_output(parsed.stdout)
        raw = f"{output_of(capture_result)}\n{output_of(parsed)}".strip()
        artifacts = [{
            "name": name, "kind": "pcap",
            "download_url": f"/api/toolbox/captures/{name}",
        }] if path.is_file() else []
        return ProbeResult(summary, rows=rows, raw_output=raw, artifacts=artifacts)
    return run


def get_spec(captures_dir: Path) -> ProbeSpec:
    interfaces = list_interfaces()
    default_interface = interfaces[0] if interfaces else "en0"
    choices = interfaces or (default_interface,)
    return ProbeSpec(
        id="capture", name_key="toolbox.probe.capture.name",
        desc_key="toolbox.probe.capture.desc", icon="◉", timeout=90,
        runner=make_runner(captures_dir), validator=validate_params,
        needs_authorization=True, authorization_check=capture_authorized,
        params=(
            ProbeParam("interface", "choice", "toolbox.param.interface",
                       default=default_interface, choices=choices),
            ProbeParam("filter", "str", "toolbox.param.filter", default="ip", max=200),
            ProbeParam("duration", "int", "toolbox.param.duration", default=10, min=1, max=60),
            ProbeParam("max_packets", "int", "toolbox.param.maxPackets",
                       default=500, min=1, max=2000),
        ),
    )
