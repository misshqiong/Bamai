from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from server.db import Database


@pytest.fixture
def db(tmp_path: Path) -> Database:
    return Database(tmp_path / "data.db")


def metric(ts: int, **overrides: Any) -> dict[str, Any]:
    row = {
        "ts": ts,
        "cpu_percent": 10.0,
        "cpu_per_core": [10.0, 10.0],
        "load_1": 1.0,
        "mem_used": 4 * 1024**3,
        "mem_total": 16 * 1024**3,
        "mem_percent": 25.0,
        "swap_used": 0,
        "disk_read_bps": 100.0,
        "disk_write_bps": 200.0,
        "net_up_bps": 1000.0,
        "net_down_bps": 2000.0,
    }
    row.update(overrides)
    return row

