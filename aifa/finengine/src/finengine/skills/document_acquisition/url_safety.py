"""SSRF-защита для A5 (Срез 7, docs/build-plan/17-srez-07-otchetnost-iz-interneta.md).

`SandboxProfile.allowed_hosts` (`policy/sandbox_profiles.py`) — декларация для
`GraphExecutor`/реестра, НЕ рантайм-проверка сокета: `validate_sandbox_dispatch`
сверяет только `execution_kind` с категорией профиля, реального HTTP-запроса
не видит. Значит настоящую защиту от SSRF обязан делать сам вызывающий код —
это она и есть.

Проверка ОДНА и та же для первого URL и для КАЖДОГО redirect hop
(`validate_url_for_fetch`) — иначе первый адрес безопасен, а сервер
перенаправляет на `http://169.254.169.254/`, и загрузчик послушно идёт следом.

`resolver`/DI: как `ocr_pages` у `doc.textify` и `table_labeler` у `doc.index`
— реальный резолвинг (`socket.getaddrinfo`) живёт за инъекцируемой границей,
чтобы тесты проверяли решение (приватный адрес отвергнут), а не реальную сеть.
"""
from __future__ import annotations

import ipaddress
import socket
from dataclasses import dataclass
from typing import Callable
from urllib.parse import urlsplit

ALLOWED_SCHEMES = ("http", "https")

Resolver = Callable[[str, int], list[str]]


class UnsafeUrlError(Exception):
    """URL отвергнут проверкой безопасности A5. `.args[0]` — причина, без
    внутренних сетевых деталей сверх того, что и так есть в самом URL
    (решение бри: журнал/прогресс не должен раскрывать внутреннюю сеть)."""


@dataclass(frozen=True)
class SafeHost:
    scheme: str
    hostname: str
    port: int
    addresses: tuple[str, ...]  # все резолвнутые адреса, все проверены как публичные


def _is_public_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    return not (
        ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved
        or ip.is_multicast or ip.is_unspecified
    )


def _default_resolver(hostname: str, port: int) -> list[str]:
    infos = socket.getaddrinfo(hostname, port, proto=socket.IPPROTO_TCP)
    return [info[4][0] for info in infos]


def split_scheme_and_host(url: str) -> tuple[str, str, int]:
    """Схема + hostname + port. Отдельно отвергает URL с userinfo
    (`user:pass@host` / `host@ip`) — классическая обфускация SSRF
    (sec-006 в tests/golden/security/cases.json): `urlsplit` разбирает её
    правильно (hostname — то, что ПОСЛЕ `@`), но userinfo здесь в принципе
    не имеет легитимного применения для скачивания публичного PDF, поэтому
    отвергается сразу, а не полагается только на то, что IP-проверка ниже
    её тоже поймает."""
    parts = urlsplit(url)
    if parts.scheme not in ALLOWED_SCHEMES:
        raise UnsafeUrlError(f"схема {parts.scheme!r} не разрешена — только {ALLOWED_SCHEMES}")
    if parts.username or parts.password or "@" in (parts.netloc or ""):
        raise UnsafeUrlError("URL с userinfo (user:pass@host) отвергнут — обфускация SSRF")
    hostname = parts.hostname or ""
    if not hostname:
        raise UnsafeUrlError("URL без хоста")
    port = parts.port or (443 if parts.scheme == "https" else 80)
    return parts.scheme, hostname, port


def resolve_public_host(
    hostname: str, port: int, *, scheme: str = "https", resolver: Resolver = _default_resolver,
) -> SafeHost:
    """DNS-резолвинг + отбой, если ХОТЬ ОДИН из адресов приватный/loopback/
    link-local/reserved/multicast. Все адреса, не первый — атакующий может
    отдать смешанный ответ (часть публичных, часть приватных)."""
    try:
        raw_addrs = resolver(hostname, port)
    except (socket.gaierror, OSError) as exc:
        raise UnsafeUrlError(f"хост {hostname!r} не резолвится: {exc}") from exc
    if not raw_addrs:
        raise UnsafeUrlError(f"хост {hostname!r}: резолвер не вернул адресов")
    addrs: list[str] = []
    for raw in raw_addrs:
        try:
            ip = ipaddress.ip_address(raw)
        except ValueError as exc:
            raise UnsafeUrlError(f"хост {hostname!r}: резолвер вернул не-IP {raw!r}") from exc
        if not _is_public_ip(ip):
            raise UnsafeUrlError(
                f"хост {hostname!r} резолвится в приватный/служебный адрес {raw} — отвергнут"
            )
        addrs.append(raw)
    return SafeHost(scheme=scheme, hostname=hostname, port=port, addresses=tuple(addrs))


def validate_url_for_fetch(url: str, *, resolver: Resolver = _default_resolver) -> SafeHost:
    """Единая точка входа — вызывается и для исходного URL, и для КАЖДОГО
    redirect hop (`downloader.py`), с одной и той же строгостью."""
    scheme, hostname, port = split_scheme_and_host(url)
    return resolve_public_host(hostname, port, scheme=scheme, resolver=resolver)


def domain_matches(hostname: str, allowed_domain: str) -> bool:
    """Точное совпадение ИЛИ суффикс с точкой — НИКОГДА подстрока.

    `sec.gov.attacker.example` не должен совпасть с `sec.gov` (sec-007);
    `cbr.ru.evil.example` не должен совпасть с `cbr.ru` (sec-008);
    `www.sec.gov`/`sec.gov` — должны."""
    h = hostname.lower().rstrip(".")
    d = allowed_domain.lower().rstrip(".")
    return h == d or h.endswith("." + d)


__all__ = [
    "ALLOWED_SCHEMES", "Resolver", "UnsafeUrlError", "SafeHost",
    "split_scheme_and_host", "resolve_public_host", "validate_url_for_fetch", "domain_matches",
]
