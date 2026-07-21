"""Bounded in-memory toolbox job execution."""

from __future__ import annotations

import threading
import uuid
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from .base import ProbeResult
from .registry import ProbeRegistry


class ProbeJobManager:
    def __init__(self, registry: ProbeRegistry, *, max_workers: int = 3, keep: int = 50) -> None:
        self.registry = registry
        self.keep = keep
        self._executor = ThreadPoolExecutor(
            max_workers=max_workers, thread_name_prefix="bamai-probe"
        )
        self._lock = threading.Lock()
        self._jobs: OrderedDict[str, dict[str, Any]] = OrderedDict()
        self._latest: dict[str, ProbeResult] = {}

    def start(self, probe_id: str, params: dict | None) -> dict[str, Any]:
        spec = self.registry.get(probe_id)
        validated = spec.validate(params)
        job_id = uuid.uuid4().hex
        if spec.needs_authorization and not spec.authorized():
            job = {"status": "needs_auth", "probe_id": probe_id}
            with self._lock:
                self._jobs[job_id] = job
                self._trim()
            return {"job_id": job_id, "status": "needs_auth"}
        with self._lock:
            self._jobs[job_id] = {"status": "running", "probe_id": probe_id}
            self._trim()
        self._executor.submit(self._run, job_id, probe_id, validated)
        return {"job_id": job_id}

    def _run(self, job_id: str, probe_id: str, params: dict[str, Any]) -> None:
        try:
            result = self.registry.run(probe_id, params)
        except Exception as exc:
            with self._lock:
                if job_id in self._jobs:
                    self._jobs[job_id] = {
                        "status": "error", "probe_id": probe_id, "error": str(exc),
                    }
            return
        with self._lock:
            self._latest[probe_id] = result
            if job_id in self._jobs:
                self._jobs[job_id] = {
                    "status": "done", "probe_id": probe_id, "result": result.to_dict(),
                }

    def get(self, job_id: str) -> dict[str, Any] | None:
        with self._lock:
            job = self._jobs.get(job_id)
            return job.copy() if job is not None else None

    def latest(self, probe_id: str) -> ProbeResult | None:
        with self._lock:
            return self._latest.get(probe_id)

    def _trim(self) -> None:
        while len(self._jobs) > self.keep:
            removable = next(
                (key for key, value in self._jobs.items() if value["status"] != "running"),
                None,
            )
            if removable is None:
                removable = next(iter(self._jobs))
            self._jobs.pop(removable, None)

    def close(self) -> None:
        self._executor.shutdown(wait=False, cancel_futures=True)
