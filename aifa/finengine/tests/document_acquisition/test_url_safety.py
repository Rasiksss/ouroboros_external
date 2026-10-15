"""C8: SSRF-защита A5 — url_safety.py.

Полностью офлайн: реального DNS/сети нет ни в одном тесте (резолвер —
инъекцируемый двойник, `sec.gov` и `169.254.169.254` — не настоящие
подключения). Кейсы sec-004..008 взяты дословно из
`tests/golden/security/cases.json` (`kind=ssrf_url`) — раньше проверялась
только форма набора, здесь — реальное поведение."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from finengine.skills.document_acquisition.url_safety import (
    UnsafeUrlError,
    domain_matches,
    resolve_public_host,
    split_scheme_and_host,
    validate_url_for_fetch,
)

_SECURITY_CASES = json.loads(
    (Path(__file__).parents[1] / "golden" / "security" / "cases.json").read_text(encoding="utf-8")
)


def _resolver_for(host_to_ip: dict[str, list[str]]):
    def _resolve(hostname: str, port: int) -> list[str]:
        if hostname not in host_to_ip:
            raise OSError(f"no such host in fake resolver: {hostname}")
        return host_to_ip[hostname]
    return _resolve


# ── схема/userinfo ───────────────────────────────────────────────────────


def test_rejects_non_http_scheme():
    with pytest.raises(UnsafeUrlError):
        split_scheme_and_host("ftp://example.com/report.pdf")


def test_rejects_file_scheme():
    with pytest.raises(UnsafeUrlError):
        split_scheme_and_host("file:///etc/passwd")


def test_accepts_https():
    scheme, host, port = split_scheme_and_host("https://www.moex.com/report.pdf")
    assert (scheme, host, port) == ("https", "www.moex.com", 443)


def test_accepts_http_with_explicit_port():
    scheme, host, port = split_scheme_and_host("http://example.com:8080/report.pdf")
    assert (scheme, host, port) == ("http", "example.com", 8080)


# sec-006: обфускация через userinfo-часть URL, имитирующую разрешённый домен.
def test_rejects_userinfo_obfuscation():
    with pytest.raises(UnsafeUrlError):
        split_scheme_and_host("http://example.com@169.254.169.254/")


# ── резолвинг / приватные адреса ─────────────────────────────────────────


def test_resolve_public_host_accepts_public_ip():
    resolver = _resolver_for({"reports.example.com": ["93.184.216.34"]})
    safe = resolve_public_host("reports.example.com", 443, resolver=resolver)
    assert safe.addresses == ("93.184.216.34",)


# sec-004: облачный metadata-эндпоинт (AWS/GCP link-local).
def test_rejects_cloud_metadata_ip():
    resolver = _resolver_for({"169.254.169.254": ["169.254.169.254"]})
    with pytest.raises(UnsafeUrlError):
        resolve_public_host("169.254.169.254", 80, resolver=resolver)


# sec-005: localhost/внутренний порт (Redis).
def test_rejects_loopback_ip():
    resolver = _resolver_for({"127.0.0.1": ["127.0.0.1"]})
    with pytest.raises(UnsafeUrlError):
        resolve_public_host("127.0.0.1", 6379, resolver=resolver)


def test_rejects_private_rfc1918_range():
    resolver = _resolver_for({"internal.example.com": ["10.0.0.5"]})
    with pytest.raises(UnsafeUrlError):
        resolve_public_host("internal.example.com", 443, resolver=resolver)


def test_rejects_when_any_resolved_address_is_private():
    """DNS может отдать смешанный ответ (часть публичных, часть приватных
    адресов) — отвергается весь хост, если хоть один адрес небезопасен."""
    resolver = _resolver_for({"mixed.example.com": ["93.184.216.34", "127.0.0.1"]})
    with pytest.raises(UnsafeUrlError):
        resolve_public_host("mixed.example.com", 443, resolver=resolver)


def test_rejects_unresolvable_host():
    resolver = _resolver_for({})
    with pytest.raises(UnsafeUrlError):
        resolve_public_host("does-not-resolve.example.com", 443, resolver=resolver)


def test_validate_url_for_fetch_combines_scheme_and_resolution():
    resolver = _resolver_for({"reports.example.com": ["93.184.216.34"]})
    safe = validate_url_for_fetch("https://reports.example.com/report.pdf", resolver=resolver)
    assert safe.hostname == "reports.example.com"
    assert safe.scheme == "https"


def test_validate_url_for_fetch_rejects_ssrf_target_end_to_end():
    resolver = _resolver_for({"trusted-looking.example.com": ["169.254.169.254"]})
    with pytest.raises(UnsafeUrlError):
        validate_url_for_fetch("http://trusted-looking.example.com/", resolver=resolver)


# ── суффикс домена — не подстрока (sec-007/sec-008) ──────────────────────


@pytest.mark.parametrize(
    ("hostname", "allowed", "expected"),
    [
        ("sec.gov", "sec.gov", True),
        ("www.sec.gov", "sec.gov", True),
        ("EDGAR.SEC.GOV", "sec.gov", True),
        ("sec.gov.attacker.example", "sec.gov", False),  # sec-007
        ("cbr.ru.evil.example", "cbr.ru", False),         # sec-008
        ("notcbr.ru", "cbr.ru", False),
        ("cbr.ru", "cbr.ru", True),
    ],
)
def test_domain_matches_is_suffix_not_substring(hostname, allowed, expected):
    assert domain_matches(hostname, allowed) is expected


# ── прогон реальных sec-* кейсов из golden security набора ──────────────
#
# sec-004/005/006 — про СЕТЕВОЙ уровень (IP-литерал приватный/loopback/
# link-local, userinfo-обфускация) — их место здесь. sec-007/008 — про
# ДОВЕРИЕ К ДОМЕНУ (поддомен-подделка резолвится в обычный публичный IP
# атакующего, сеть тут ни при чём) — их поведение проверяет
# `test_policy.py::test_golden_domain_spoof_cases_are_not_trusted_as_official`,
# не этот файл: `domain_matches`/`classify_source` — вот что их отбивает.


def _cases_of_kind(kind: str) -> list[dict]:
    return [c for c in _SECURITY_CASES if c["kind"] == kind]


_NETWORK_LEVEL_SSRF_IDS = {"sec-004", "sec-005", "sec-006"}


@pytest.mark.parametrize(
    "case",
    [c for c in _cases_of_kind("ssrf_url") if c["id"] in _NETWORK_LEVEL_SSRF_IDS],
    ids=lambda c: c["id"],
)
def test_golden_ssrf_cases_are_rejected_at_network_level(case):
    """sec-004..006: URL, чей хост — приватный/loopback/link-local IP-литерал
    (в открытую или спрятанный за userinfo), обязан быть отвергнут ДО любого
    подключения. Резолвер-двойник эхом возвращает буквальный IP из payload'а —
    ровно то, что вернул бы настоящий DNS для URL с IP-литералом хостом."""
    url = case["payload"]

    def _echo_ip_resolver(host: str, port: int) -> list[str]:
        return [host]

    with pytest.raises(UnsafeUrlError):
        validate_url_for_fetch(url, resolver=_echo_ip_resolver)
