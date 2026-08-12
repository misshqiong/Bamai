from __future__ import annotations

import json
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

from server.agent.tools import ToolError, ToolExecutor
from server.toolbox.base import ProbeParam, ProbeResult, ProbeSpec, ProbeValidationError
from server.toolbox.jobs import ProbeJobManager
from server.toolbox.probes import (
    battery,
    capture,
    dns,
    http_timing,
    memory_check,
    netquality,
    ping,
    port,
    tls_check,
    traceroute,
    whois_lookup,
    wifi,
)
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
CURL_TIMING_SAMPLE = (
    "time_namelookup=0.010000|time_connect=0.030000|time_appconnect=0.080000|"
    "time_starttransfer=0.180000|time_total=0.200000|http_code=200|"
    "remote_ip=93.184.216.34\n"
)
SCUTIL_PROXY_SAMPLE = """<dictionary> {
  HTTPEnable : 1
  HTTPPort : 7890
  HTTPProxy : 127.0.0.1
}
"""
RDAP_DOMAIN_SAMPLE = {
    "status": ["active"],
    "events": [
        {"eventAction": "registration", "eventDate": "1995-08-14T04:00:00Z"},
        {"eventAction": "expiration", "eventDate": "2027-08-13T04:00:00Z"},
    ],
    "entities": [{
        "roles": ["registrar"],
        "vcardArray": ["vcard", [["fn", {}, "text", "Example Registrar"]]],
    }],
}
RDAP_IP_SAMPLE = {
    "name": "EXAMPLE-NET", "startAddress": "192.0.2.0", "endAddress": "192.0.2.255",
    "country": "US", "entities": [{
        "roles": ["registrant"],
        "vcardArray": ["vcard", [["fn", {}, "text", "Example Holder"]]],
    }],
}
TLS_CERT_SAMPLE = {
    "issuer": ((('commonName', "Example CA"),),),
    "subject": ((('commonName', "example.com"),),),
    "notAfter": "Aug 13 04:00:00 2027 GMT",
    "subjectAltName": (("DNS", "example.com"), ("DNS", "www.example.com")),
}


def result(stdout="", stderr="", returncode=0):
    return subprocess.CompletedProcess([], returncode, stdout, stderr)


def test_probe_framework_validates_and_runs_mock_runner():
    calls = []
    spec = ProbeSpec(
        id="mock", name_key="mock.name", desc_key="mock.desc", icon="M", timeout=2,
        params=(ProbeParam("count", "int", "mock.count", default=2, min=1, max=3),),
        runner=lambda params: calls.append(params) or ProbeResult({"count": params["count"]}),
    )
    registry = ProbeRegistry()
    registry.register(spec)
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


def test_http_timing_parsers_and_validation():
    assert http_timing.parse_curl_output(CURL_TIMING_SAMPLE, is_https=True) == {
        "dns_ms": 10.0,
        "connect_ms": 20.0,
        "tls_ms": 50.0,
        "ttfb_ms": 100.0,
        "total_ms": 200.0,
        "http_code": 200,
        "remote_ip": "93.184.216.34",
    }
    assert http_timing.parse_scutil_proxies(SCUTIL_PROXY_SAMPLE) == {
        "enabled": True, "host": "127.0.0.1", "port": 7890,
    }
    assert http_timing.parse_scutil_proxies("<dictionary> {\n}")["enabled"] is False
    spec = http_timing.get_spec()
    assert spec.validate({"url": "https://example.com"})["mode"] == "direct"
    for invalid in ("file:///tmp/x", "https://example.com/a b", "https://"):
        with pytest.raises(ProbeValidationError):
            spec.validate({"url": invalid})


def test_http_timing_runner_uses_argument_lists_and_proxy_fallback(monkeypatch):
    calls = []

    def fake_run(command, timeout):
        calls.append((command, timeout))
        if command == ["scutil", "--proxies"]:
            return result(stderr="unsupported", returncode=64)
        if command == ["scutil", "--proxy"]:
            return result(SCUTIL_PROXY_SAMPLE)
        if command[0] == "curl":
            return result(CURL_TIMING_SAMPLE)
        raise AssertionError(command)

    monkeypatch.setattr(http_timing, "run_command", fake_run)
    outcome = http_timing.run({
        "url": "https://example.com", "mode": "both",
    })
    assert outcome.summary["proxy_available"] is True
    assert outcome.summary["direct_total_ms"] == 200.0
    assert [row["mode"] for row in outcome.rows] == ["direct", "proxy"]
    curl_commands = [command for command, _ in calls if command[0] == "curl"]
    assert ["--noproxy", "*"] == curl_commands[0][8:10]
    assert "--proxy" in curl_commands[1]


def test_http_timing_proxy_mode_reports_missing_proxy(monkeypatch):
    monkeypatch.setattr(
        http_timing,
        "run_command",
        lambda command, timeout: result("<dictionary> {\n}\n"),
    )
    outcome = http_timing.run({"url": "http://example.com", "mode": "proxy"})
    assert outcome.summary["proxy_available"] is False
    assert "代理" in outcome.summary["error"]


def test_whois_rdap_extractors_validation_and_runner(monkeypatch):
    domain = whois_lookup.extract_rdap_summary(RDAP_DOMAIN_SAMPLE, "domain")
    assert domain == {
        "found": True,
        "registrar": "Example Registrar",
        "created_at": "1995-08-14T04:00:00Z",
        "expires_at": "2027-08-13T04:00:00Z",
        "status": ["active"],
    }
    ip = whois_lookup.extract_rdap_summary(RDAP_IP_SAMPLE, "ip")
    assert ip["range"] == "192.0.2.0 - 192.0.2.255"
    assert ip["holder"] == "Example Holder"
    spec = whois_lookup.get_spec()
    assert spec.validate({"query": "2001:db8::1"})["query"] == "2001:db8::1"
    with pytest.raises(ProbeValidationError):
        spec.validate({"query": "bad host/name"})

    captured = {}

    class Response:
        status_code = 200

        @staticmethod
        def json():
            return RDAP_DOMAIN_SAMPLE

    def fake_get(url, **kwargs):
        captured["url"] = url
        captured.update(kwargs)
        return Response()

    monkeypatch.setattr(whois_lookup.httpx, "get", fake_get)
    outcome = whois_lookup.run({"query": "example.com"})
    assert outcome.summary["registrar"] == "Example Registrar"
    assert outcome.summary["via_proxy"] is False
    assert captured == {
        "url": "https://rdap.org/domain/example.com",
        "follow_redirects": True,
        "timeout": 8,
    }


def test_whois_runner_falls_back_to_system_proxy(monkeypatch):
    calls = []

    class Response:
        status_code = 200

        @staticmethod
        def json():
            return RDAP_DOMAIN_SAMPLE

    def fake_get(url, **kwargs):
        calls.append(kwargs)
        if "proxy" not in kwargs:
            raise whois_lookup.httpx.ConnectTimeout("直连超时")
        return Response()

    monkeypatch.setattr(whois_lookup.httpx, "get", fake_get)
    monkeypatch.setattr(
        whois_lookup,
        "_system_http_proxy",
        lambda: ({"enabled": True, "host": "127.0.0.1", "port": 7890}, ""),
    )
    outcome = whois_lookup.run({"query": "example.com"})
    assert outcome.summary["found"] is True
    assert outcome.summary["via_proxy"] is True
    assert calls[1]["proxy"] == "http://127.0.0.1:7890"

    # 没有系统代理时直接返回失败原因，不再重试。
    monkeypatch.setattr(
        whois_lookup,
        "_system_http_proxy",
        lambda: ({"enabled": False, "host": None, "port": None}, ""),
    )
    failed = whois_lookup.run({"query": "example.com"})
    assert failed.summary["found"] is False
    assert "RDAP 请求失败" in failed.summary["reason"]


def test_whois_runner_returns_not_found_without_raising(monkeypatch):
    class Response:
        status_code = 404

    monkeypatch.setattr(whois_lookup.httpx, "get", lambda *args, **kwargs: Response())
    assert whois_lookup.run({"query": "example.invalid"}).summary["found"] is False


def test_tls_date_certificate_summary_and_validation():
    expiry = tls_check.parse_certificate_date("Aug 13 04:00:00 2027 GMT")
    assert expiry == datetime(2027, 8, 13, 4, tzinfo=timezone.utc)
    summary = tls_check.certificate_summary(
        TLS_CERT_SAMPLE,
        "TLSv1.3",
        now=datetime(2027, 8, 10, 4, tzinfo=timezone.utc),
    )
    assert summary == {
        "protocol": "TLSv1.3",
        "issuer": "Example CA",
        "subject": "example.com",
        "not_after": "2027-08-13T04:00:00Z",
        "days_remaining": 3,
        "san_count": 2,
    }
    spec = tls_check.get_spec()
    for params in ({"host": "bad host"}, {"host": "example.com", "port": 0}):
        with pytest.raises(ProbeValidationError):
            spec.validate(params)


def test_tls_runner_uses_ssl_and_socket_without_openssl(monkeypatch):
    calls = []

    class Connection:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

    class TLSSocket(Connection):
        def version(self):
            return "TLSv1.3"

        def getpeercert(self, binary_form=False):
            return b"der" if binary_form else TLS_CERT_SAMPLE

    class Context:
        def wrap_socket(self, connection, server_hostname):
            calls.append((connection, server_hostname))
            return TLSSocket()

    monkeypatch.setattr(
        tls_check.socket, "create_connection", lambda address, timeout: Connection()
    )
    monkeypatch.setattr(tls_check.ssl, "create_default_context", lambda: Context())
    outcome = tls_check.run({"host": "example.com", "port": 443})
    assert outcome.summary["valid"] is True
    assert outcome.summary["subject"] == "example.com"
    assert calls[0][1] == "example.com"


def test_dns_compare_consistency_and_failure_tolerance(monkeypatch):
    answers = {
        "system": result("1.1.1.1\n"),
        "ali": result("1.1.1.1\n"),
        "google": result("1.1.1.1\n"),
    }

    def fake_run(command, timeout):
        resolver = (
            "ali" if "@223.5.5.5" in command
            else "google" if "@8.8.8.8" in command else "system"
        )
        return answers[resolver]

    monkeypatch.setattr(dns, "run_command", fake_run)
    params = {"domain": "example.com", "type": "A", "resolver": "compare"}
    assert dns.run(params).summary["consistent"] is True
    answers["google"] = result("8.8.8.8\n")
    assert dns.run(params).summary["consistent"] is False
    answers["google"] = result(stderr="timeout", returncode=9)
    failed = dns.run(params)
    assert failed.summary["consistent"] is False
    assert next(row for row in failed.rows if row["resolver"] == "google")["error"]
    params["type"] = "MX"
    assert dns.run(params).summary["consistent"] is None
    assert dns.get_spec().timeout == 25


def test_probe_commands_are_argument_lists(monkeypatch, tmp_path, db):
    calls = []

    def fake_run(command, timeout):
        assert isinstance(command, list)
        calls.append(command)
        if command[0] == "ping":
            return result(PING_SAMPLE)
        if command[0] == "traceroute":
            return result(TRACEROUTE_SAMPLE)
        if command[0] == "dig":
            return result(DIG_SAMPLE)
        if command[0] == "nc":
            return result(returncode=0)
        if command[:2] == ["networkQuality", "-v"]:
            return result(NETWORK_QUALITY_SAMPLE)
        if command == ["memory_pressure"]:
            return result(MEMORY_PRESSURE_SAMPLE)
        if command == ["vm_stat"]:
            return result(VM_STAT_SAMPLE)
        if command == ["pmset", "-g", "batt"]:
            return result(PMSET_SAMPLE)
        if command == ["system_profiler", "SPAirPortDataType", "-json"]:
            return result(WIFI_JSON_SAMPLE)
        if command == ["system_profiler", "SPPowerDataType", "-json"]:
            return result(POWER_JSON_SAMPLE)
        if command[0] == "tcpdump" and "-w" in command:
            Path(command[command.index("-w") + 1]).write_bytes(b"pcap")
            return result(stderr="captured 3 packets")
        if command[0] == "tcpdump":
            return result(TCPDUMP_SAMPLE)
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
    capture_command = next(
        command for command in calls if command[0] == "tcpdump" and "-w" in command
    )
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
    registry = ProbeRegistry()
    registry.register(spec)
    jobs = ProbeJobManager(registry)
    try:
        started = jobs.start("capture", {})
        assert started["status"] == "needs_auth"
        assert jobs.get(started["job_id"])["status"] == "needs_auth"
    finally:
        jobs.close()


def test_default_registry_contains_all_twelve_probes(db, tmp_path, monkeypatch):
    monkeypatch.setattr(capture, "list_interfaces", lambda: ("en0",))
    registry = build_registry(db, tmp_path)
    assert [spec.id for spec in registry.all()] == [
        "ping", "traceroute", "dns", "http_timing", "whois_lookup", "tls_check",
        "port", "netquality", "memory_check",
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
