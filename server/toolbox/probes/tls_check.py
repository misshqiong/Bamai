from __future__ import annotations

import math
import socket
import ssl
import tempfile
from datetime import datetime, timezone
from typing import Any

from ..base import ProbeParam, ProbeResult, ProbeSpec
from .ping import validate_host


def validate_params(params: dict) -> None:
    validate_host(params["host"])


def parse_certificate_date(value: str) -> datetime:
    """解析 ``ssl.getpeercert`` 使用的 GMT 证书时间。"""
    parsed = datetime.strptime(value, "%b %d %H:%M:%S %Y %Z")
    return parsed.replace(tzinfo=timezone.utc)


def _distinguished_name(value: Any) -> str | None:
    fields: list[tuple[str, str]] = []
    if not isinstance(value, (list, tuple)):
        return None
    for group in value:
        if not isinstance(group, (list, tuple)):
            continue
        for item in group:
            if (
                isinstance(item, (list, tuple))
                and len(item) == 2
                and isinstance(item[0], str)
            ):
                fields.append((item[0], str(item[1])))
    for preferred in ("commonName", "organizationName"):
        match = next((field_value for key, field_value in fields if key == preferred), None)
        if match:
            return match
    return ", ".join(f"{key}={field_value}" for key, field_value in fields) or None


def _decode_der_certificate(der: bytes | None) -> dict[str, Any]:
    """CERT_NONE 时标准库只给 DER；仍用 Python ssl 解码，不调用 openssl。"""
    if not der:
        return {}
    decoder = getattr(getattr(ssl, "_ssl", None), "_test_decode_cert", None)
    if decoder is None:
        return {}
    try:
        pem = ssl.DER_cert_to_PEM_cert(der)
        with tempfile.NamedTemporaryFile("w", suffix=".pem") as handle:
            handle.write(pem)
            handle.flush()
            decoded = decoder(handle.name)
    except (OSError, ValueError, ssl.SSLError):
        return {}
    return decoded if isinstance(decoded, dict) else {}


def _fetch_certificate(
    context: ssl.SSLContext, host: str, port: int
) -> tuple[str | None, dict[str, Any], bytes | None]:
    with socket.create_connection((host, port), timeout=10) as connection:
        with context.wrap_socket(connection, server_hostname=host) as tls_socket:
            certificate = tls_socket.getpeercert()
            der = tls_socket.getpeercert(binary_form=True)
            return tls_socket.version(), certificate, der


def certificate_summary(
    certificate: dict[str, Any],
    protocol: str | None,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    not_after_value = certificate.get("notAfter")
    not_after = None
    days_remaining = None
    if isinstance(not_after_value, str):
        try:
            expiry = parse_certificate_date(not_after_value)
            current = now or datetime.now(timezone.utc)
            if current.tzinfo is None:
                current = current.replace(tzinfo=timezone.utc)
            not_after = expiry.isoformat().replace("+00:00", "Z")
            days_remaining = math.floor((expiry - current).total_seconds() / 86400)
        except ValueError:
            not_after = not_after_value
    sans = certificate.get("subjectAltName")
    return {
        "protocol": protocol,
        "issuer": _distinguished_name(certificate.get("issuer")),
        "subject": _distinguished_name(certificate.get("subject")),
        "not_after": not_after,
        "days_remaining": days_remaining,
        "san_count": len(sans) if isinstance(sans, (list, tuple)) else 0,
    }


def run(params: dict) -> ProbeResult:
    host = params["host"]
    port = params.get("port", 443)
    try:
        protocol, certificate, _ = _fetch_certificate(
            ssl.create_default_context(), host, port
        )
    except ssl.SSLCertVerificationError as exc:
        summary: dict[str, Any] = {"valid": False, "error": str(exc)}
        try:
            protocol, certificate, der = _fetch_certificate(
                ssl._create_unverified_context(), host, port
            )
            if not certificate:
                certificate = _decode_der_certificate(der)
            summary.update(certificate_summary(certificate, protocol))
        except (OSError, ssl.SSLError, ValueError) as fallback_exc:
            summary["certificate_error"] = str(fallback_exc)
        return ProbeResult(summary)
    except (OSError, ssl.SSLError, ValueError) as exc:
        return ProbeResult({"valid": False, "error": str(exc)})

    return ProbeResult({"valid": True, **certificate_summary(certificate, protocol)})


def get_spec() -> ProbeSpec:
    return ProbeSpec(
        id="tls_check",
        name_key="toolbox.probe.tls_check.name",
        desc_key="toolbox.probe.tls_check.desc",
        icon="TLS",
        timeout=10,
        runner=run,
        validator=validate_params,
        params=(
            ProbeParam("host", "str", "toolbox.param.host", required=True, max=253),
            ProbeParam("port", "int", "toolbox.param.port", default=443, min=1, max=65535),
        ),
    )
