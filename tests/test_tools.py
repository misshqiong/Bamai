from __future__ import annotations

from pathlib import Path

import pytest

from server.collector import parse_nettop_output
from server.search.files import find_large_files, mdfind_search


def test_parse_nettop_csv_with_dotted_process_name():
    sample = "process,bytes_in,bytes_out\nSafari.123,2048,1024\ncom.apple.helper.456,900,700\nbad-row,1,2\n"
    assert parse_nettop_output(sample) == [
        {"pid": 123, "name": "Safari", "bytes_in": 2048.0, "bytes_out": 1024.0},
        {"pid": 456, "name": "com.apple.helper", "bytes_in": 900.0, "bytes_out": 700.0},
    ]


def test_mdfind_command_and_filter(monkeypatch):
    class Result:
        returncode = 0
        stderr = ""
        stdout = "/System/private.txt\n/Library/a.txt\n/Users/test/report.txt\n"
    captured = {}
    def fake_run(command, **kwargs):
        captured["command"] = command
        return Result()
    monkeypatch.setattr("server.search.files.subprocess.run", fake_run)
    items = mdfind_search("report", "name", 20)
    assert captured["command"] == ["mdfind", "-name", "report"]
    assert [item["path"] for item in items] == ["/Users/test/report.txt"]


def test_find_large_files_skips_hidden_and_packages(tmp_path: Path):
    (tmp_path / "visible.bin").write_bytes(b"x" * 2048)
    (tmp_path / "small.txt").write_text("small")
    hidden = tmp_path / ".hidden"; hidden.mkdir(); (hidden / "secret.bin").write_bytes(b"x" * 4096)
    package = tmp_path / "Demo.app"; package.mkdir(); (package / "inside.bin").write_bytes(b"x" * 4096)
    result = find_large_files(str(tmp_path), min_mb=0.001, limit=10, timeout=2)
    assert [item["name"] for item in result["items"]] == ["visible.bin"]
    assert result["truncated"] is False


def test_large_file_relative_escape_is_rejected():
    with pytest.raises(ValueError, match="不能越出"):
        find_large_files("../../../../tmp", min_mb=1)

