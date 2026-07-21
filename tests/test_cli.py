from __future__ import annotations

import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def test_cli_scripts_have_valid_bash_syntax_and_compatibility_wrapper():
    subprocess.run(["bash", "-n", "./bamai"], cwd=ROOT, check=True)
    subprocess.run(["bash", "-n", "run.sh"], cwd=ROOT, check=True)
    subprocess.run(["bash", "-n", "scripts/enable-capture.sh"], cwd=ROOT, check=True)
    subprocess.run(["bash", "-n", "scripts/disable-capture.sh"], cwd=ROOT, check=True)
    assert os.access(ROOT / "bamai", os.X_OK)
    lines = (ROOT / "run.sh").read_text().splitlines()
    assert lines == [
        "#!/usr/bin/env bash",
        'exec "$(dirname "$0")/bamai" start --foreground "$@"',
    ]


def test_cli_exposes_all_commands_and_bilingual_output(tmp_path):
    result = subprocess.run(
        [str(ROOT / "bamai"), "--help"],
        cwd=ROOT,
        env={**os.environ, "BAMAI_DATA_DIR": str(tmp_path / "data")},
        text=True,
        capture_output=True,
        check=True,
    )
    for command in ("start", "stop", "restart", "status", "logs", "model", "autostart"):
        assert command in result.stdout
    assert all(" / " in line for line in result.stdout.splitlines())
