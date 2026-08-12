from __future__ import annotations

from pathlib import Path

import pytest

from server.agent.tools import (
    AGENT_PROBE_ALLOWLIST,
    TOOL_DEFINITIONS,
    ToolError,
    ToolExecutor,
)
from server.collector import parse_nettop_output
from server.search.files import find_large_files, mdfind_search
from tests.conftest import metric


def test_parse_nettop_csv_with_dotted_process_name():
    sample = (
        "process,bytes_in,bytes_out\nSafari.123,2048,1024\n"
        "com.apple.helper.456,900,700\nbad-row,1,2\n"
    )
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
    hidden = tmp_path / ".hidden"
    hidden.mkdir()
    (hidden / "secret.bin").write_bytes(b"x" * 4096)
    package = tmp_path / "Demo.app"
    package.mkdir()
    (package / "inside.bin").write_bytes(b"x" * 4096)
    result = find_large_files(str(tmp_path), min_mb=0.001, limit=10, timeout=2)
    assert [item["name"] for item in result["items"]] == ["visible.bin"]
    assert result["truncated"] is False


def test_large_file_relative_escape_is_rejected():
    with pytest.raises(ValueError, match="不能越出"):
        find_large_files("../../../../tmp", min_mb=1)


def test_existing_agent_tools_dispatch_with_compact_results(db):
    db.insert_metric(metric(100, cpu_percent=42.6))
    db.insert_disk_usage(100, [{"mount": "/", "total": 1000, "used": 400, "percent": 40.4}])
    db.insert_process_snapshots(100, [{
        "pid": 7, "name": "worker", "cpu_percent": 12.7,
        "memory_rss": 123456, "cmdline": "worker --serve",
    }])
    db.insert_process_net(100, [{
        "pid": 7, "name": "worker", "up_bps": 2000.8, "down_bps": 3000.2,
    }])
    db.create_event(100, "cpu_high", "warning", "CPU 高", "详情")
    tools = ToolExecutor(
        db,
        process_provider=lambda sort, limit: [{
            "pid": 7, "name": "worker", "cpu_percent": 12.7,
            "memory_rss": 123456, "cmdline": "worker --serve",
        }],
        process_search=lambda keyword, limit: [{"pid": 7, "name": keyword}],
        file_search=lambda query, kind, limit: [{"path": f"/tmp/{query}", "size": 10.8}],
        large_file_search=lambda path, min_mb, limit: {
            "items": [{"path": f"{path}/large.bin", "size_mb": 150.6}], "truncated": False
        },
    )

    results = [
        tools.execute("get_current_stats"),
        tools.execute("get_top_processes", {"sort_by": "cpu", "limit": 5}),
        tools.execute("query_metrics", {"metric": "cpu_percent", "start_ts": 0, "end_ts": 200}),
        tools.execute("get_process_history", {"start_ts": 0, "end_ts": 200, "name": "work"}),
        tools.execute("get_events", {"limit": 10, "since_ts": 0}),
        tools.execute("find_large_files", {"path": "/tmp", "min_mb": 100, "limit": 10}),
        tools.execute("search_files", {"query": "报告", "kind": "name", "limit": 10}),
        tools.execute("search_processes", {"keyword": "worker"}),
    ]
    assert len(results) == 8
    assert results[0].data["cpu_percent"] == 43
    assert len(results[2].data["series"]) <= 50
    assert all(result.summary for result in results)


def test_app_diagnosis_tools_use_shared_live_and_database_data(db):
    db.insert_app_snapshots(100, [{
        "app": "Example", "kind": "app", "cpu_percent": 10,
        "memory_rss": 1000, "proc_count": 1, "up_bps": 20, "down_bps": 30,
    }])
    db.insert_app_snapshots(200, [{
        "app": "Example", "kind": "app", "cpu_percent": 30,
        "memory_rss": 3000, "proc_count": 1, "up_bps": 200, "down_bps": 300,
    }])
    db.insert_app_connections(200, [{
        "app": "Example", "remote_ip": "127.0.0.1", "remote_port": 7890,
        "domain": None, "proto": "tcp", "up_bps": 200, "down_bps": 300,
        "rtt_ms": 1.5, "via_proxy": 1, "proxy_name": "Proxy App",
    }])
    for ts, cpu, memory in ((100, 4, 400), (200, 8, 800)):
        db.insert_app_process_snapshots(ts, [{
            "app": "Example", "name": "Example Renderer", "cpu_percent": cpu,
            "memory_rss": memory, "proc_count": 2,
        }])
    live = [{
        "pid": 7, "ppid": 0, "name": "Example", "cpu_percent": 12.6,
        "memory_rss": 2500, "cmdline": "example", "argv0": "example",
        "exe": "/Applications/Example.app/Contents/MacOS/Example",
    }]
    tools = ToolExecutor(db, application_process_provider=lambda: live)

    overview = tools.execute("get_app_overview", {"limit": 5})
    assert overview.data["apps"] == [{
        "app": "Example", "kind": "app", "cpu_percent": 13,
        "memory_rss": 2500, "proc_count": 1, "up_bps": 200, "down_bps": 300,
    }]
    history = tools.execute("get_app_history", {
        "app": "Example", "start_ts": 0, "end_ts": 300,
    })
    assert history.data["available"] is True
    assert history.data["summary"]["cpu_percent"] == {"min": 10, "max": 30, "avg": 20}
    assert history.data["summary"]["memory_rss"] == {
        "min": 1000, "max": 3000, "avg": 2000,
    }
    process_history = tools.execute("get_app_history", {
        "app": "Example", "process_name": "Example Renderer",
        "start_ts": 0, "end_ts": 300,
    })
    assert process_history.data["process_name"] == "Example Renderer"
    assert process_history.data["summary"]["cpu_percent"] == {
        "min": 4, "max": 8, "avg": 6,
    }
    assert "子进程“Example Renderer”" in process_history.summary
    connections = tools.execute("get_app_connections", {"app": "Example"})
    assert connections.data["connections"][0]["remote_port"] == 7890
    assert "不代表真实网络延迟" in connections.data["note"]
    missing = tools.execute("get_app_history", {
        "app": "Missing", "start_ts": 0, "end_ts": 300,
    })
    assert missing.data == {"available": False, "app": "Missing", "series": []}


def test_app_diagnosis_tool_validation_and_probe_allowlist(db):
    tools = ToolExecutor(db, application_process_provider=lambda: [])
    invalid_calls = [
        ("get_app_overview", {"limit": 0}),
        ("get_app_history", {"app": "", "start_ts": 0, "end_ts": 1}),
        ("get_app_history", {"app": "Example", "start_ts": 2, "end_ts": 1}),
        ("get_app_connections", {"app": "Example", "extra": True}),
    ]
    for name, arguments in invalid_calls:
        with pytest.raises(ToolError):
            tools.execute(name, arguments)

    run_probe = next(
        definition["function"]
        for definition in TOOL_DEFINITIONS
        if definition["function"]["name"] == "run_probe"
    )
    assert set(run_probe["parameters"]["properties"]["probe_id"]["enum"]) == (
        AGENT_PROBE_ALLOWLIST
    )
    assert AGENT_PROBE_ALLOWLIST == {
        "ping", "dns", "port", "wifi", "battery", "memory_check", "traceroute",
        "netquality", "http_timing", "whois_lookup", "tls_check",
    }
    assert "capture" not in AGENT_PROBE_ALLOWLIST
    assert {definition["function"]["name"] for definition in TOOL_DEFINITIONS} >= {
        "get_app_overview", "get_app_history", "get_app_connections",
    }
    app_history = next(
        definition["function"]
        for definition in TOOL_DEFINITIONS
        if definition["function"]["name"] == "get_app_history"
    )
    assert "process_name" in app_history["parameters"]["properties"]
    assert app_history["parameters"]["required"] == ["app", "start_ts", "end_ts"]


def test_agent_tool_parameter_validation(db):
    tools = ToolExecutor(db)
    with pytest.raises(ToolError, match="超出允许范围"):
        tools.execute("get_top_processes", {"sort_by": "cpu", "limit": 16})
    with pytest.raises(ToolError, match="未知工具"):
        tools.execute("not_a_tool", {})
