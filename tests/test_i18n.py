from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def load_dictionaries():
    source = (ROOT / "web/js/i18n.js").read_text()
    match = re.search(
        r"/\* i18n-dictionaries-start \*/\s*(\{.*?\})\s*/\* i18n-dictionaries-end \*/",
        source,
        re.DOTALL,
    )
    assert match, "i18n 字典标记缺失"
    return json.loads(match.group(1))


def test_i18n_languages_have_identical_keys():
    dictionaries = load_dictionaries()
    assert set(dictionaries) == {"zh", "en"}
    assert set(dictionaries["zh"]) == set(dictionaries["en"])


def test_every_html_data_i18n_key_exists():
    dictionaries = load_dictionaries()
    html = (ROOT / "web/index.html").read_text()
    used = set(re.findall(r'data-i18n(?:-placeholder|-title)?="([^"]+)"', html))
    assert used
    assert used <= set(dictionaries["zh"])


def test_health_and_event_templates_exist_in_both_languages():
    dictionaries = load_dictionaries()
    required = {
        *(
            f"health.{kind}.{field}"
            for kind in ("cpu", "memory", "disk", "network", "swap")
            for field in ("headline", "advice")
        ),
        *(
            f"events.{kind}.{field}"
            for kind in ("cpu_high", "mem_pressure", "disk_full", "net_spike")
            for field in ("title", "detail")
        ),
    }
    for language in ("zh", "en"):
        assert required <= set(dictionaries[language])


def test_settings_page_keys_exist_in_both_languages():
    dictionaries = load_dictionaries()
    required = {
        "nav.settings", "settings.title", "settings.model", "settings.language",
        "settings.temperature", "settings.contextLength", "settings.download",
        "settings.downloading", "settings.save", "settings.saved",
    }
    for language in ("zh", "en"):
        assert required <= set(dictionaries[language])


def test_toolbox_keys_exist_in_both_languages():
    dictionaries = load_dictionaries()
    required = {
        "nav.toolbox", "toolbox.title", "toolbox.run", "toolbox.running",
        "toolbox.result", "toolbox.raw", "toolbox.explain", "toolbox.auth.title",
        *(f"toolbox.probe.{probe}.{field}" for probe in (
            "ping", "traceroute", "dns", "port", "netquality", "memory_check",
            "wifi", "battery", "capture", "http_timing", "whois_lookup", "tls_check",
        ) for field in ("name", "desc")),
        *(f"toolbox.param.{name}" for name in (
            "host", "count", "domain", "recordType", "port", "windowMinutes",
            "interface", "filter", "duration", "maxPackets", "url", "mode", "query",
            "resolver",
        )),
    }
    for language in ("zh", "en"):
        assert required <= set(dictionaries[language])


def test_apps_view_keys_exist_in_both_languages():
    dictionaries = load_dictionaries()
    required = {
        "nav.apps", "apps.title", "apps.name", "apps.cpu", "apps.memory",
        "apps.network", "apps.processCount", "apps.background", "apps.history",
        "apps.processes", "apps.connections", "apps.connectionMode",
        "apps.destination", "apps.rtt", "apps.proxy",
        "apps.diagnose", "apps.diagnosePrompt",
    }
    for language in ("zh", "en"):
        assert required <= set(dictionaries[language])
