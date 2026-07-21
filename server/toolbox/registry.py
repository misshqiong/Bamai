"""Probe registration and lookup."""

from __future__ import annotations

from pathlib import Path

from .. import config
from ..db import Database
from .base import ProbeResult, ProbeSpec, ProbeValidationError


class ProbeRegistry:
    def __init__(self) -> None:
        self._specs: dict[str, ProbeSpec] = {}

    def register(self, spec: ProbeSpec) -> None:
        if spec.id in self._specs:
            raise ValueError(f"重复 probe id: {spec.id}")
        self._specs[spec.id] = spec

    def all(self) -> list[ProbeSpec]:
        return list(self._specs.values())

    def get(self, probe_id: str) -> ProbeSpec:
        try:
            return self._specs[probe_id]
        except KeyError as exc:
            raise ProbeValidationError(f"未知探测工具: {probe_id}") from exc

    def run(self, probe_id: str, params: dict | None = None) -> ProbeResult:
        spec = self.get(probe_id)
        if spec.needs_authorization and not spec.authorized():
            raise PermissionError("needs_auth")
        return spec.runner(spec.validate(params))


def build_registry(
    db: Database,
    captures_dir: str | Path = config.CAPTURES_DIR,
) -> ProbeRegistry:
    from .probes import battery, capture, dns, memory_check, netquality, ping, port, traceroute, wifi

    registry = ProbeRegistry()
    for spec in (
        ping.get_spec(), traceroute.get_spec(), dns.get_spec(), port.get_spec(),
        netquality.get_spec(), memory_check.get_spec(db), wifi.get_spec(), battery.get_spec(),
        capture.get_spec(Path(captures_dir)),
    ):
        registry.register(spec)
    return registry
