from __future__ import annotations

import re
import subprocess
from typing import Any
from urllib.parse import urlsplit

from ..base import ProbeParam, ProbeResult, ProbeSpec, ProbeValidationError
from .common import output_of, run_command

CURL_WRITE_FORMAT = (
    "time_namelookup=%{time_namelookup}|time_connect=%{time_connect}|"
    "time_appconnect=%{time_appconnect}|time_starttransfer=%{time_starttransfer}|"
    "time_total=%{time_total}|http_code=%{http_code}|remote_ip=%{remote_ip}\n"
)
TIMING_KEYS = {
    "time_namelookup",
    "time_connect",
    "time_appconnect",
    "time_starttransfer",
    "time_total",
    "http_code",
    "remote_ip",
}


def validate_url(url: str) -> None:
    if re.search(r"\s", url):
        raise ProbeValidationError("url 不能包含空白字符")
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ProbeValidationError("url 仅支持完整的 http:// 或 https:// 地址")


def validate_params(params: dict) -> None:
    validate_url(params["url"])


def _milliseconds(seconds: float) -> float:
    return round(seconds * 1000, 2)


def parse_curl_output(output: str, *, is_https: bool) -> dict[str, Any]:
    """解析 curl ``-w`` 的自定义稳定格式，并转换为分段毫秒。"""
    values: dict[str, str] = {}
    for part in output.strip().split("|"):
        key, separator, value = part.partition("=")
        if separator and key in TIMING_KEYS:
            values[key] = value.strip()
    missing = TIMING_KEYS - set(values)
    if missing:
        raise ValueError(f"curl 计时输出缺少字段: {', '.join(sorted(missing))}")

    try:
        lookup = float(values["time_namelookup"])
        connect = float(values["time_connect"])
        appconnect = float(values["time_appconnect"])
        starttransfer = float(values["time_starttransfer"])
        total = float(values["time_total"])
        http_code = int(values["http_code"])
    except ValueError as exc:
        raise ValueError("curl 计时输出包含无效数字") from exc

    tls_end = appconnect if is_https and appconnect > 0 else connect
    return {
        "dns_ms": _milliseconds(lookup),
        "connect_ms": _milliseconds(max(0.0, connect - lookup)),
        "tls_ms": (
            _milliseconds(max(0.0, appconnect - connect))
            if is_https and appconnect > 0 else None
        ),
        "ttfb_ms": _milliseconds(max(0.0, starttransfer - tls_end)),
        "total_ms": _milliseconds(total),
        "http_code": http_code,
        "remote_ip": values["remote_ip"] or None,
    }


def parse_scutil_proxies(output: str) -> dict[str, Any]:
    """解析 ``scutil --proxy``/旧规格中的 ``--proxies`` 字典输出。"""
    values: dict[str, str] = {}
    for line in output.splitlines():
        match = re.match(r"\s*(HTTPEnable|HTTPProxy|HTTPPort)\s*:\s*(.*?)\s*$", line)
        if match:
            values[match.group(1)] = match.group(2)
    try:
        port = int(values["HTTPPort"])
    except (KeyError, ValueError):
        port = None
    host = values.get("HTTPProxy", "").strip()
    return {
        "enabled": values.get("HTTPEnable") == "1",
        "host": host or None,
        "port": port if port is not None and 1 <= port <= 65535 else None,
    }


def _empty_timing(error: str) -> dict[str, Any]:
    return {
        "dns_ms": None,
        "connect_ms": None,
        "tls_ms": None,
        "ttfb_ms": None,
        "total_ms": None,
        "http_code": None,
        "remote_ip": None,
        "error": error,
    }


def _run_curl(url: str, mode: str, proxy_url: str | None = None) -> tuple[dict, str]:
    command = [
        "curl", "-o", "/dev/null", "-s", "-w", CURL_WRITE_FORMAT,
        "--max-time", "30",
    ]
    if mode == "direct":
        command.extend(["--noproxy", "*"])
    elif proxy_url is not None:
        command.extend(["--proxy", proxy_url])
    command.append(url)
    try:
        result = run_command(command, 30)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return _empty_timing(str(exc)), str(exc)
    raw = output_of(result)
    try:
        summary = parse_curl_output(result.stdout, is_https=url.startswith("https://"))
    except ValueError as exc:
        summary = _empty_timing(str(exc))
    if result.returncode != 0:
        summary["error"] = f"curl 退出码 {result.returncode}"
    return summary, raw


def _system_http_proxy() -> tuple[dict[str, Any], str]:
    outputs = []
    for command in (["scutil", "--proxies"], ["scutil", "--proxy"]):
        try:
            result = run_command(command, 5)
        except (OSError, subprocess.TimeoutExpired) as exc:
            outputs.append(str(exc))
            continue
        raw = output_of(result)
        outputs.append(raw)
        parsed = parse_scutil_proxies(result.stdout)
        if result.returncode == 0:
            return parsed, "\n\n".join(item for item in outputs if item)
    return {"enabled": False, "host": None, "port": None}, "\n\n".join(outputs)


def _proxy_url(proxy: dict[str, Any]) -> str | None:
    if not proxy["enabled"] or proxy["host"] is None or proxy["port"] is None:
        return None
    host = proxy["host"]
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    return f"http://{host}:{proxy['port']}"


def run(params: dict) -> ProbeResult:
    url = params["url"]
    mode = params.get("mode", "direct")
    if mode == "direct":
        summary, raw = _run_curl(url, "direct")
        return ProbeResult(summary, raw_output=raw)

    proxy, proxy_raw = _system_http_proxy()
    proxy_url = _proxy_url(proxy)
    if mode == "proxy" and proxy_url is None:
        return ProbeResult(
            {"proxy_available": False, "error": "未检测到有效的系统 HTTP 代理"},
            raw_output=proxy_raw,
        )
    if mode == "proxy":
        summary, raw = _run_curl(url, "proxy", proxy_url)
        summary["proxy_available"] = True
        return ProbeResult(summary, raw_output=f"{proxy_raw}\n\n{raw}".strip())

    direct, direct_raw = _run_curl(url, "direct")
    if proxy_url is None:
        proxy_result = _empty_timing("未检测到有效的系统 HTTP 代理")
    else:
        proxy_result, proxy_curl_raw = _run_curl(url, "proxy", proxy_url)
        proxy_raw = f"{proxy_raw}\n\n{proxy_curl_raw}".strip()
    summary = {
        **direct,
        "proxy_available": proxy_url is not None,
        "direct_total_ms": direct.get("total_ms"),
        "proxy_total_ms": proxy_result.get("total_ms"),
    }
    rows = [{"mode": "direct", **direct}, {"mode": "proxy", **proxy_result}]
    return ProbeResult(
        summary,
        rows=rows,
        raw_output=f"{direct_raw}\n\n{proxy_raw}".strip(),
    )


def get_spec() -> ProbeSpec:
    return ProbeSpec(
        id="http_timing",
        name_key="toolbox.probe.http_timing.name",
        desc_key="toolbox.probe.http_timing.desc",
        icon="HTTP",
        timeout=30,
        runner=run,
        validator=validate_params,
        params=(
            ProbeParam("url", "str", "toolbox.param.url", required=True, max=500),
            ProbeParam(
                "mode",
                "choice",
                "toolbox.param.mode",
                default="direct",
                choices=("direct", "proxy", "both"),
            ),
        ),
    )
