from __future__ import annotations

import ipaddress
import json
from typing import Any
from urllib.parse import quote

import httpx

from ..base import ProbeParam, ProbeResult, ProbeSpec
from .ping import validate_host


def validate_params(params: dict) -> None:
    query = params["query"]
    try:
        ipaddress.ip_address(query)
    except ValueError:
        validate_host(query)


def _vcard_name(entity: Any) -> str | None:
    if not isinstance(entity, dict):
        return None
    vcard = entity.get("vcardArray")
    if not isinstance(vcard, list) or len(vcard) < 2 or not isinstance(vcard[1], list):
        return None
    for field in vcard[1]:
        if (
            isinstance(field, list)
            and len(field) >= 4
            and field[0] == "fn"
            and isinstance(field[3], str)
        ):
            return field[3]
    return None


def _entity_name(payload: dict[str, Any], role: str | None = None) -> str | None:
    entities = payload.get("entities")
    if not isinstance(entities, list):
        return None
    for entity in entities:
        if not isinstance(entity, dict):
            continue
        roles = entity.get("roles")
        if role is not None and (not isinstance(roles, list) or role not in roles):
            continue
        name = _vcard_name(entity)
        if name:
            return name
    return None


def _event_date(payload: dict[str, Any], action: str) -> str | None:
    events = payload.get("events")
    if not isinstance(events, list):
        return None
    for event in events:
        if isinstance(event, dict) and event.get("eventAction") == action:
            value = event.get("eventDate")
            return value if isinstance(value, str) else None
    return None


def extract_rdap_summary(
    payload: dict[str, Any], query_type: str | None = None
) -> dict[str, Any]:
    """从 RDAP JSON 中提取稳定、紧凑的域名或 IP 摘要。"""
    if query_type is None:
        query_type = "ip" if "startAddress" in payload or "endAddress" in payload else "domain"
    if query_type == "ip":
        start = payload.get("startAddress")
        end = payload.get("endAddress")
        return {
            "found": True,
            "name": payload.get("name"),
            "start_address": start,
            "end_address": end,
            "range": f"{start} - {end}" if start and end else None,
            "country": payload.get("country"),
            "holder": _entity_name(payload),
        }
    statuses = payload.get("status")
    return {
        "found": True,
        "registrar": _entity_name(payload, "registrar"),
        "created_at": _event_date(payload, "registration"),
        "expires_at": _event_date(payload, "expiration"),
        "status": statuses if isinstance(statuses, list) else [],
    }


def run(params: dict) -> ProbeResult:
    query = params["query"]
    try:
        ipaddress.ip_address(query)
        query_type = "ip"
    except ValueError:
        query_type = "domain"
    url = f"https://rdap.org/{query_type}/{quote(query, safe='')}"
    try:
        response = httpx.get(url, follow_redirects=True, timeout=15)
    except httpx.HTTPError as exc:
        return ProbeResult({"found": False, "reason": f"RDAP 请求失败: {exc}"})
    if response.status_code == 404:
        return ProbeResult({"found": False, "reason": "未找到 RDAP 记录（HTTP 404）"})
    if response.status_code < 200 or response.status_code >= 300:
        return ProbeResult({
            "found": False,
            "reason": f"RDAP 服务返回 HTTP {response.status_code}",
        })
    try:
        payload = response.json()
    except ValueError:
        return ProbeResult({"found": False, "reason": "RDAP 服务返回了无效 JSON"})
    if not isinstance(payload, dict):
        return ProbeResult({"found": False, "reason": "RDAP 响应格式无效"})
    return ProbeResult(
        extract_rdap_summary(payload, query_type),
        raw_output=json.dumps(payload, ensure_ascii=False, indent=2),
    )


def get_spec() -> ProbeSpec:
    return ProbeSpec(
        id="whois_lookup",
        name_key="toolbox.probe.whois_lookup.name",
        desc_key="toolbox.probe.whois_lookup.desc",
        icon="WHOIS",
        timeout=15,
        runner=run,
        validator=validate_params,
        params=(
            ProbeParam("query", "str", "toolbox.param.query", required=True, max=253),
        ),
    )
