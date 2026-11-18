"""Official-source policy — Срез 7 A5, «приоритет официального источника».

Явная таблица уровней доверия, а не догадка модели: политика data-driven
(нерушимое решение №7 — заказчик/источник-специфика становится данными, не
веткой кода) и НАМЕРЕННО маленькая — расширять список известных регуляторов/
бирж, когда появится реальная потребность, а не заранее (CLAUDE.md §2).

⚠️ УРОВЕНЬ ДОВЕРИЯ ВЛИЯЕТ ТОЛЬКО НА ПОРЯДОК ПОПЫТОК, НЕ НА БЕЗОПАСНОСТЬ.
`unverified`-кандидат обрабатывается той же проверкой `url_safety` и той же
проверкой байтов после скачивания (`downloader.py`), что и `official_*` —
«официальный источник» никогда не значит «пропустить проверку»."""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from .url_safety import UnsafeUrlError, domain_matches, split_scheme_and_host


class SourceTier(str, Enum):
    OFFICIAL_REGULATOR = "official_regulator"  # регулятор/биржа — фиксированный список ниже
    OFFICIAL_ISSUER = "official_issuer"        # домен самого эмитента — только если вызывающий его назвал явно
    UNVERIFIED = "unverified"                  # всё остальное — не значит «отвергнут», значит «не в приоритете»


# Регуляторы/биржи, чья роль как первоисточника финансовой отчётности не
# зависит от конкретного эмитента. Суффиксное сравнение (`domain_matches`) —
# не substring: поддомен атакующего вида `sec.gov.attacker.example` сюда не
# попадает (см. tests/golden/security/cases.json sec-007/sec-008).
KNOWN_REGULATOR_DOMAINS: tuple[str, ...] = (
    "cbr.ru",           # Банк России
    "e-disclosure.ru",  # Центр раскрытия корпоративной информации (РФ)
    "moex.com",         # Московская биржа
    "sec.gov",          # SEC EDGAR (США)
)

_TIER_WEIGHT: dict[SourceTier, float] = {
    SourceTier.OFFICIAL_REGULATOR: 2.0,
    SourceTier.OFFICIAL_ISSUER: 1.0,
    SourceTier.UNVERIFIED: 0.0,
}


@dataclass(frozen=True)
class SearchCandidate:
    """Один результат поиска кандидатов (`search.py`) до проверки безопасности."""

    url: str
    title: str = ""
    snippet: str = ""
    search_score: float = 0.0  # 0..1, релевантность от поисковика


@dataclass(frozen=True)
class RankedCandidate:
    candidate: SearchCandidate
    hostname: str
    tier: SourceTier
    rank_score: float  # выше — пробуем раньше


def classify_source(hostname: str, *, known_issuer_domains: tuple[str, ...] = ()) -> SourceTier:
    """Уровень доверия ПО ДОМЕНУ, детерминированно — без обращения к модели."""
    for domain in KNOWN_REGULATOR_DOMAINS:
        if domain_matches(hostname, domain):
            return SourceTier.OFFICIAL_REGULATOR
    for domain in known_issuer_domains:
        if domain_matches(hostname, domain):
            return SourceTier.OFFICIAL_ISSUER
    return SourceTier.UNVERIFIED


def rank_candidates(
    candidates: list[SearchCandidate], *, known_issuer_domains: tuple[str, ...] = (),
) -> list[RankedCandidate]:
    """Отсортировать кандидатов по приоритету источника, затем по релевантности
    поиска. Малформед/неразбираемый URL молча выпадает из ранжирования — он
    всё равно был бы отвергнут `url_safety` на шаге скачивания, здесь нет
    смысла дублировать ту диагностику."""
    ranked: list[RankedCandidate] = []
    for c in candidates:
        try:
            _scheme, hostname, _port = split_scheme_and_host(c.url)
        except UnsafeUrlError:
            continue
        tier = classify_source(hostname, known_issuer_domains=known_issuer_domains)
        score = _TIER_WEIGHT[tier] + max(0.0, min(1.0, c.search_score))
        ranked.append(RankedCandidate(candidate=c, hostname=hostname, tier=tier, rank_score=score))
    ranked.sort(key=lambda r: r.rank_score, reverse=True)
    return ranked


__all__ = [
    "SourceTier", "KNOWN_REGULATOR_DOMAINS", "SearchCandidate", "RankedCandidate",
    "classify_source", "rank_candidates",
]
