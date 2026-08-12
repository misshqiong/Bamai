from __future__ import annotations

import socket

from server.apps import extract_app_from_path, group_processes
from server.collector import Collector, ReverseDnsCache, parse_nettop_connections


def test_extract_app_from_path_variants():
    assert extract_app_from_path("/Applications/Slack.app/Contents/MacOS/Slack") == "Slack"
    assert extract_app_from_path(
        "/Applications/Google Chrome.app/Contents/Frameworks/"
        "Google Chrome Helper.app/Contents/MacOS/Google Chrome Helper"
    ) == "Google Chrome"
    assert extract_app_from_path("/usr/bin/python3") is None
    assert extract_app_from_path("") is None
    assert extract_app_from_path(
        "/System/Applications/Utilities/Terminal.app/Contents/MacOS/Terminal"
    ) == "Terminal"


def test_group_processes_uses_parent_chain_and_background_group():
    processes = [
        {
            "pid": 10, "name": "Chrome", "cpu_percent": 3.0, "memory_rss": 100,
            "cmdline": "", "exe": "/Applications/Google Chrome.app/Contents/MacOS/Chrome",
        },
        {
            "pid": 11, "name": "Chrome Helper", "cpu_percent": 2.0,
            "memory_rss": 50, "cmdline": "helper --type=gpu", "exe": "",
        },
        {
            "pid": 20, "name": "backupd", "cpu_percent": 1.0,
            "memory_rss": 25, "cmdline": "/usr/libexec/backupd", "exe": "/usr/libexec/backupd",
        },
    ]
    parents = {10: None, 11: 10, 20: None}

    groups = group_processes(processes, parent_lookup=parents.get)

    chrome = next(group for group in groups if group["app"] == "Google Chrome")
    assert chrome == {
        "app": "Google Chrome", "kind": "app", "cpu_percent": 5.0,
        "memory_rss": 150, "proc_count": 2, "pids": [10, 11],
    }
    background = next(group for group in groups if group["app"] == "backupd")
    assert background["kind"] == "background"
    assert background["pids"] == [20]


def test_parse_connection_nettop_with_process_rows_udp_and_rtt():
    sample = (
        "process,bytes_in,bytes_out,rtt_avg\n"
        "Safari.123,3000,2000,\n"
        "tcp4 10.0.0.5:52344<->93.184.216.34:443,2000,1000,12.5\n"
        "udp4 10.0.0.5:5353<->8.8.8.8:53,500,250,\n"
        "com.apple.helper.456,900,700,\n"
        "tcp6 fe80::aede:48ff%en0.50100<->2607:f8b0::200e.7890,100,50,0.8 ms\n"
        "udp6 *.5353<->*.*,10,5,\n"
    )
    rows = parse_nettop_connections(sample)
    assert rows[0] == {
        "pid": 123, "name": "Safari", "proto": "tcp",
        "local_ip": "10.0.0.5", "local_port": 52344,
        "remote_ip": "93.184.216.34", "remote_port": 443,
        "bytes_in": 2000.0, "bytes_out": 1000.0, "rtt_ms": 12.5,
    }
    assert rows[1]["proto"] == "udp"
    assert rows[1]["rtt_ms"] is None
    # 真实 nettop 的 IPv6 行用点分隔端口且带 %zone；通配监听行应被跳过。
    assert rows[2]["pid"] == 456
    assert rows[2]["local_ip"] == "fe80::aede:48ff"
    assert rows[2]["local_port"] == 50100
    assert rows[2]["remote_ip"] == "2607:f8b0::200e"
    assert rows[2]["remote_port"] == 7890
    assert rows[2]["rtt_ms"] == 0.8
    assert len(rows) == 3


def test_parse_connection_nettop_missing_rtt_and_headerless():
    missing_rtt = (
        "process,bytes_in,bytes_out\n"
        "curl.88,30,20\n"
        "tcp4 192.168.1.5:50000<->1.1.1.1:443,20,10\n"
    )
    assert parse_nettop_connections(missing_rtt)[0]["rtt_ms"] is None

    headerless = (
        "curl.88,30,20,\n"
        "tcp4 192.168.1.5:50000<->1.1.1.1:443,20,10,4.25\n"
    )
    row = parse_nettop_connections(headerless)[0]
    assert row["pid"] == 88
    assert row["rtt_ms"] == 4.25


def test_reverse_dns_cache_ttl_and_negative_cache(monkeypatch):
    now = [100.0]
    calls: list[str] = []

    def fake_getnameinfo(address, flags):
        calls.append(address[0])
        assert flags == socket.NI_NAMEREQD
        if address[0] == "1.1.1.1":
            return ("one.one.one.one.", "0")
        raise socket.gaierror("not found")

    monkeypatch.setattr("server.collector.socket.getnameinfo", fake_getnameinfo)
    cache = ReverseDnsCache(ttl_seconds=10, clock=lambda: now[0], start_worker=False)

    assert cache.request("1.1.1.1") is True
    assert cache.resolve_pending_once() is True
    assert cache.get("1.1.1.1") == "one.one.one.one"
    assert cache.request("1.1.1.1") is False

    assert cache.request("8.8.8.8") is True
    assert cache.resolve_pending_once() is True
    assert cache.get("8.8.8.8") is None
    assert cache.request("8.8.8.8") is False
    assert calls == ["1.1.1.1", "8.8.8.8"]

    now[0] = 111.0
    assert cache.get("1.1.1.1") is None
    assert cache.request("8.8.8.8") is True


def test_reverse_dns_queue_has_per_round_limit_and_skips_private_addresses():
    cache = ReverseDnsCache(queue_size=30, start_worker=False)
    public = [f"8.8.4.{number}" for number in range(1, 26)]
    assert cache.request_many(public, limit=20) == 20
    assert cache.request("127.0.0.1") is False
    assert cache.request("192.168.1.2") is False


def test_collector_uses_one_connection_nettop_call_and_aggregates_apps(db, monkeypatch):
    samples = iter([
        (
            "process,bytes_in,bytes_out,rtt_avg\n"
            "Chrome.10,100,50,\n"
            "tcp4 127.0.0.1:50000<->127.0.0.1:7890,100,50,1.0\n"
            "ClashX.20,100,100,\n"
            "tcp4 127.0.0.1:7890<->127.0.0.1:50000,100,100,1.0\n"
        ),
        (
            "process,bytes_in,bytes_out,rtt_avg\n"
            "Chrome.10,300,150,\n"
            "tcp4 127.0.0.1:50000<->127.0.0.1:7890,300,150,2.0\n"
            "ClashX.20,250,200,\n"
            "tcp4 127.0.0.1:7890<->127.0.0.1:50000,250,200,2.0\n"
        ),
    ])
    commands = []

    class Result:
        returncode = 0
        stderr = ""

        def __init__(self):
            self.stdout = next(samples)

    def fake_run(command, **kwargs):
        commands.append(command)
        return Result()

    collector = Collector(db)
    collector._reverse_dns.close()
    collector._reverse_dns = ReverseDnsCache(start_worker=False)
    collector._latest_processes = [
        {
            "pid": 10, "ppid": 0, "name": "Chrome", "cpu_percent": 5,
            "memory_rss": 100, "cmdline": "chrome",
            "exe": "/Applications/Google Chrome.app/Contents/MacOS/Chrome",
        },
        {
            "pid": 20, "ppid": 0, "name": "ClashX", "cpu_percent": 1,
            "memory_rss": 50, "cmdline": "clashx",
            "exe": "/Applications/ClashX.app/Contents/MacOS/ClashX",
        },
    ]
    monkeypatch.setattr("server.collector.subprocess.run", fake_run)
    moments = iter([100.0, 110.0])
    monkeypatch.setattr("server.collector.time.monotonic", lambda: next(moments))

    collector._collect_process_net(100)
    collector._collect_process_net(110)
    collector._collect_apps(110)

    expected_command = [
        "nettop", "-L", "1", "-x", "-J", "bytes_in,bytes_out,rtt_avg",
    ]
    assert commands == [expected_command, expected_command]
    assert db.latest_process_net()[0]["pid"] == 10
    chrome = next(
        row for row in db.latest_app_snapshots() if row["app"] == "Google Chrome"
    )
    assert chrome["up_bps"] == 10.0
    assert chrome["down_bps"] == 20.0
    connection = db.latest_app_connections("Google Chrome")[0]
    assert connection["via_proxy"] == 1
    assert connection["proxy_name"] == "ClashX"
    collector._reverse_dns.close()


def test_collect_apps_writes_process_name_aggregates():
    class RecordingDb:
        def __init__(self):
            self.process_rows = []

        def insert_app_snapshots(self, ts, rows):
            pass

        def insert_app_process_snapshots(self, ts, rows):
            self.process_rows = list(rows)

        def insert_app_connections(self, ts, rows):
            pass

    recording_db = RecordingDb()
    collector = Collector(recording_db, rules=object())
    collector._reverse_dns.close()
    collector._reverse_dns = ReverseDnsCache(start_worker=False)
    collector._latest_processes = [
        {
            "pid": 10, "ppid": 0, "name": "Chrome", "cpu_percent": 5,
            "memory_rss": 100, "cmdline": "chrome",
            "exe": "/Applications/Google Chrome.app/Contents/MacOS/Chrome",
        },
        {
            "pid": 11, "ppid": 10, "name": "Chrome Helper (Renderer)",
            "cpu_percent": 2, "memory_rss": 50, "cmdline": "renderer", "exe": "",
        },
        {
            "pid": 12, "ppid": 10, "name": "Chrome Helper (Renderer)",
            "cpu_percent": 3, "memory_rss": 60, "cmdline": "renderer", "exe": "",
        },
        {
            "pid": 13, "ppid": 10, "name": "Chrome Helper (GPU)",
            "cpu_percent": 4, "memory_rss": 70, "cmdline": "gpu", "exe": "",
        },
    ]

    collector._collect_apps(100)

    rows = {row["name"]: row for row in recording_db.process_rows}
    assert rows == {
        "Chrome": {
            "app": "Google Chrome", "name": "Chrome", "cpu_percent": 5.0,
            "memory_rss": 100, "proc_count": 1,
        },
        "Chrome Helper (Renderer)": {
            "app": "Google Chrome", "name": "Chrome Helper (Renderer)",
            "cpu_percent": 5.0, "memory_rss": 110, "proc_count": 2,
        },
        "Chrome Helper (GPU)": {
            "app": "Google Chrome", "name": "Chrome Helper (GPU)",
            "cpu_percent": 4.0, "memory_rss": 70, "proc_count": 1,
        },
    }
    collector._reverse_dns.close()
