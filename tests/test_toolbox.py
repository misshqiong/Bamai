from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from server.agent.tools import ToolError, ToolExecutor
from server.toolbox.base import ProbeParam, ProbeResult, ProbeSpec, ProbeValidationError
from server.toolbox.jobs import ProbeJobManager
from server.toolbox.probes import battery, capture, dns, memory_check, netquality, ping, port, traceroute, wifi
from server.toolbox.registry import ProbeRegistry, build_registry


PING_SAMPLE = """4 packets transmitted, 4 packets received, 25.0% packet loss
round-trip min/avg/max/stddev = 10.100/12.200/14.300/1.000 ms
"""
TRACEROUTE_SAMPLE = """traceroute to example.com (93.184.216.34), 20 hops max
 1  router.local (192.168.1.1)  1.234 ms  1.100 ms
 2  * * *
 3  93.184.216.34  22.500 ms  22.100 ms
"""
DIG_SAMPLE = "93.184.216.34\n93.184.216.35\n"
NETWORK_QUALITY_SAMPLE = """Uplink capacity: 24.750 Mbps
Downlink capacity: 98.500 Mbps
Responsiveness: High (321 RPM)
"""
MEMORY_PRESSURE_SAMPLE = "System-wide memory free percentage: 18%\n"
VM_STAT_SAMPLE = """Mach Virtual Memory Statistics: (page size of 16384 bytes)
Pages free:                               1000.
Pages inactive:                           500.
Pages speculative:                        100.
Pages purgeable:                           50.
"""
PMSET_SAMPLE = """Now drawing from 'Battery Power'
 -InternalBattery-0 (id=1234567)\t85%; discharging; 3:12 remaining present: true
"""
WIFI_JSON_SAMPLE = json.dumps({"SPAirPortDataType": [{
    "spairport_current_network_information": {
        "_name": "Home WiFi", "spairport_network_channel": "36 (5GHz, 80MHz)",
        "spairport_signal_noise": "-45 dBm / -92 dBm",
        "spairport_network_rate": 866,
    }
}]})
POWER_JSON_SAMPLE = json.dumps({"SPPowerDataType": [{
    "sppower_battery_cycle_count": 123, "sppower_battery_health": "Good",
}]})
TCPDUMP_SAMPLE = """1710000000.100000 IP 192.168.1.2.51515 > 1.1.1.1.53: UDP, length 32
1710000000.200000 IP 192.168.1.2.51515 > 1.1.1.1.53: UDP, length 48
1710000001.300000 IP 192.168.1.2.60000 > 93.184.216.34.443: Flags [S], length 0
"""


def result(stdout="", stderr="", returncode=0):
    return subprocess.CompletedProcess([], returncode, stdout, stderr)


def test_probe_framework_validates_and_runs_mock_runner():
    calls = []
    spec = ProbeSpec(
        id="mock", name_key="mock.name", desc_key="mock.desc", icon="M", timeout=2,
        params=(ProbeParam("count", "int", "mock.count", default=2, min=1, max=3),),
        runner=lambda params: calls.append(params) or ProbeResult({"count": params["count"]}),
    )
    registry = ProbeRegistry(); registry.register(spec)
    assert registry.run("mock", {}).summary == {"count": 2}
    assert calls == [{"count": 2}]
    assert registry.all()[0].to_dict()["params"][0]["max"] == 3
    with pytest.raises(ProbeValidationError):
        registry.run("mock", {"count": 4})
    with pytest.raises(ProbeValidationError):
        registry.run("mock", {"extra": 1})


def test_job_manager_runs_mock_runner_and_keeps_result():
    registry = ProbeRegistry()
    registry.register(ProbeSpec(
        id="mock", name_key="n", desc_key="d", icon="M", params=(), timeout=1,
        runner=lambda params: ProbeResult({"ok": True}, raw_output="x" * 9000),
    ))
    jobs = ProbeJobManager(registry)
    try:
        started = jobs.start("mock", {})
        for _ in range(100):
            job = jobs.get(started["job_id"])
            if job["status"] != "running":
                break
            time.sleep(0.01)
        assert job["status"] == "done"
        assert len(job["result"]["raw_output"]) == 8192
        assert jobs.latest("mock").summary == {"ok": True}
    finally:
        jobs.close()


def test_fixed_sample_parsers(db):
    assert ping.parse_ping_output(PING_SAMPLE) == {
        "packet_loss_percent": 25.0, "min_ms": 10.1, "avg_ms": 12.2, "max_ms": 14.3,
    }
    hops = traceroute.parse_traceroute_output(TRACEROUTE_SAMPLE)
    assert [row["hop"] for row in hops] == [1, 2, 3]
    assert hops[0]["ip"] == "192.168.1.1" and hops[1]["latency_ms"] is None
    assert dns.parse_dig_output(DIG_SAMPLE) == ["93.184.216.34", "93.184.216.35"]
    assert netquality.parse_network_quality_output(NETWORK_QUALITY_SAMPLE) == {
        "upload_mbps": 24.75, "download_mbps": 98.5, "responsiveness_rpm": 321.0,
    }
    assert memory_check.parse_memory_pressure_output(MEMORY_PRESSURE_SAMPLE) == {
        "pressure_level": "warning", "free_percent": 18,
    }
    assert memory_check.parse_vm_stat_output(VM_STAT_SAMPLE)["reclaimable_bytes"] == 1650 * 16384
    db.insert_process_snapshots(100, [{
        "pid": 7, "name": "growing", "cpu_percent": 1, "memory_rss": 100,
        "cmdline": "growing",
    }])
    db.insert_process_snapshots(200, [{
        "pid": 7, "name": "growing", "cpu_percent": 1, "memory_rss": 250,
        "cmdline": "growing",
    }])
    assert memory_check.growing_processes(db, 100, 200)[0]["growth_bytes"] == 150
    assert battery.parse_pmset_output(PMSET_SAMPLE) == {"percent": 85, "status": "discharging"}
    assert battery.parse_power_json(POWER_JSON_SAMPLE) == {"cycle_count": 123, "health": "Good"}
    wifi_summary = wifi.parse_wifi_json(WIFI_JSON_SAMPLE)
    assert wifi_summary["ssid"] == "Home WiFi"
    assert wifi_summary["rssi_dbm"] == -45 and wifi_summary["noise_dbm"] == -92
    capture_summary, sessions = capture.parse_tcpdump_output(TCPDUMP_SAMPLE)
    assert capture_summary["protocol_distribution"] == {"UDP": 2, "TCP": 1}
    assert sessions[0]["packets"] == 2 and sessions[0]["bytes"] == 80


def test_probe_commands_are_argument_lists(monkeypatch, tmp_path, db):
    calls = []

    def fake_run(command, timeout):
        assert isinstance(command, list)
        calls.append(command)
        if command[0] == "ping": return result(PING_SAMPLE)
        if command[0] == "traceroute": return result(TRACEROUTE_SAMPLE)
        if command[0] == "dig": return result(DIG_SAMPLE)
        if command[0] == "nc": return result(returncode=0)
        if command[:2] == ["networkQuality", "-v"]: return result(NETWORK_QUALITY_SAMPLE)
        if command == ["memory_pressure"]: return result(MEMORY_PRESSURE_SAMPLE)
        if command == ["vm_stat"]: return result(VM_STAT_SAMPLE)
        if command == ["pmset", "-g", "batt"]: return result(PMSET_SAMPLE)
        if command == ["system_profiler", "SPAirPortDataType", "-json"]: return result(WIFI_JSON_SAMPLE)
        if command == ["system_profiler", "SPPowerDataType", "-json"]: return result(POWER_JSON_SAMPLE)
        if command[0] == "tcpdump" and "-w" in command:
            Path(command[command.index("-w") + 1]).write_bytes(b"pcap")
            return result(stderr="captured 3 packets")
        if command[0] == "tcpdump": return result(TCPDUMP_SAMPLE)
        raise AssertionError(command)

    for module in (ping, traceroute, dns, port, netquality, memory_check, wifi, battery, capture):
        monkeypatch.setattr(module, "run_command", fake_run)
    ping.run({"host": "example.com", "count": 4})
    traceroute.run({"host": "example.com"})
    dns.run({"domain": "example.com", "type": "A"})
    port.run({"host": "example.com", "port": 443})
    netquality.run({})
    memory_check.make_runner(db)({"window_min": 5})
    wifi.run({})
    battery.run({})
    capture.make_runner(tmp_path)({
        "interface": "en0", "filter": "tcp port 443", "duration": 1, "max_packets": 10,
    })
    assert ["ping", "-c", "4", "example.com"] in calls
    capture_command = next(command for command in calls if command[0] == "tcpdump" and "-w" in command)
    assert capture_command[capture_command.index("-s") + 1] == "96"
    assert "-nn" in capture_command
    source = "\n".join(
        path.read_text() for path in (Path(__file__).parents[1] / "server/toolbox").rglob("*.py")
    )
    assert "shell=True" not in source


@pytest.mark.parametrize("probe_id,params", [
    ("ping", {"host": "example.com; rm -rf /", "count": 1}),
    ("dns", {"domain": "example.com && whoami", "type": "A"}),
    ("capture", {"interface": "en0;id", "filter": "ip", "duration": 1, "max_packets": 1}),
    ("capture", {"interface": "en0", "filter": "tcp; whoami", "duration": 1, "max_packets": 1}),
])
def test_probe_parameter_validation_rejects_injection(db, tmp_path, monkeypatch, probe_id, params):
    monkeypatch.setattr(capture, "list_interfaces", lambda: ("en0",))
    monkeypatch.setattr(capture, "capture_authorized", lambda: True)
    specs = {
        "ping": ping.get_spec(), "dns": dns.get_spec(),
        "capture": capture.get_spec(tmp_path),
    }
    with pytest.raises(ProbeValidationError):
        specs[probe_id].validate(params)


def test_capture_authorization_detection_is_mockable(monkeypatch, tmp_path):
    monkeypatch.setattr(capture.os, "access", lambda path, mode: False)
    assert capture.capture_authorized() is False
    monkeypatch.setattr(capture, "list_interfaces", lambda: ("en0",))
    monkeypatch.setattr(capture, "capture_authorized", lambda: False)
    spec = capture.get_spec(tmp_path)
    assert spec.needs_authorization is True
    assert spec.authorized() is False
    registry = ProbeRegistry(); registry.register(spec)
    jobs = ProbeJobManager(registry)
    try:
        started = jobs.start("capture", {})
        assert started["status"] == "needs_auth"
        assert jobs.get(started["job_id"])["status"] == "needs_auth"
    finally:
        jobs.close()


def test_default_registry_contains_all_nine_probes(db, tmp_path, monkeypatch):
    monkeypatch.setattr(capture, "list_interfaces", lambda: ("en0",))
    registry = build_registry(db, tmp_path)
    assert [spec.id for spec in registry.all()] == [
        "ping", "traceroute", "dns", "port", "netquality", "memory_check",
        "wifi", "battery", "capture",
    ]


def test_agent_run_probe_is_allowlisted_and_uses_same_validation(db):
    registry = ProbeRegistry()
    ping_spec = ping.get_spec()
    registry.register(ProbeSpec(
        id="ping", name_key=ping_spec.name_key, desc_key=ping_spec.desc_key,
        params=ping_spec.params, timeout=ping_spec.timeout, validator=ping_spec.validator,
        runner=lambda params: ProbeResult({"host": params["host"]}),
    ))
    tools = ToolExecutor(db, probe_registry=registry)
    assert tools.execute("run_probe", {
        "probe_id": "ping", "params": {"host": "example.com", "count": 1},
    }).data["summary"]["host"] == "example.com"
    with pytest.raises(ToolError):
        tools.execute("run_probe", {"probe_id": "capture", "params": {}})
    with pytest.raises(ToolError):
        tools.execute("run_probe", {
            "probe_id": "ping", "params": {"host": "example.com;id", "count": 1},
        })
