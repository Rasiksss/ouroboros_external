"""policy.py — приоритет официального источника. Уровень доверия влияет
ТОЛЬКО на порядок попыток, не на безопасность (та проверяется отдельно,
одинаково для всех уровней, в url_safety.py/downloader.py)."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from finengine.skills.document_acquisition.policy import (
    KNOWN_REGULATOR_DOMAINS,
    SearchCandidate,
    SourceTier,
    classify_source,
    rank_candidates,
)

_SECURITY_CASES = json.loads(
    (Path(__file__).parents[1] / "golden" / "security" / "cases.json").read_text(encoding="utf-8")
)


def test_known_regulator_domain_is_official():
    assert classify_source("cbr.ru") is SourceTier.OFFICIAL_REGULATOR
    assert classify_source("www.moex.com") is SourceTier.OFFICIAL_REGULATOR
    assert classify_source("www.sec.gov") is SourceTier.OFFICIAL_REGULATOR


def test_unknown_domain_is_unverified():
    assert classify_source("random-blog.example.com") is SourceTier.UNVERIFIED


def test_known_issuer_domain_is_official_when_caller_names_it():
    assert classify_source("ir.severstal.com", known_issuer_domains=("severstal.com",)) \
        is SourceTier.OFFICIAL_ISSUER
    # без явного названия того же домена вызывающим — не официальный по умолчанию
    assert classify_source("ir.severstal.com") is SourceTier.UNVERIFIED


# sec-007/sec-008: поддомен-подделка НЕ должен пройти как доверенный источник.
@pytest.mark.parametrize(
    "case_id",
    ["sec-007", "sec-008"],
)
def test_golden_domain_spoof_cases_are_not_trusted_as_official(case_id):
    case = next(c for c in _SECURITY_CASES if c["id"] == case_id)
    url = case["payload"]
    hostname = url.split("://", 1)[1].split("/", 1)[0]
    tier = classify_source(hostname)
    assert tier is SourceTier.UNVERIFIED, (
        f"{case_id}: {hostname!r} не должен классифицироваться как official_regulator "
        f"({case['detail']})"
    )


def test_rank_candidates_prefers_official_tier_over_search_score():
    candidates = [
        SearchCandidate(url="https://random-blog.example.com/report.pdf", search_score=0.99),
        SearchCandidate(url="https://www.cbr.ru/report.pdf", search_score=0.10),
    ]
    ranked = rank_candidates(candidates)
    assert ranked[0].candidate.url == "https://www.cbr.ru/report.pdf"
    assert ranked[0].tier is SourceTier.OFFICIAL_REGULATOR
    assert ranked[1].tier is SourceTier.UNVERIFIED


def test_rank_candidates_breaks_ties_within_tier_by_search_score():
    candidates = [
        SearchCandidate(url="https://a.example.com/x.pdf", search_score=0.2),
        SearchCandidate(url="https://b.example.com/y.pdf", search_score=0.8),
    ]
    ranked = rank_candidates(candidates)
    assert ranked[0].candidate.url == "https://b.example.com/y.pdf"


def test_rank_candidates_drops_unparseable_urls_without_crashing():
    candidates = [
        SearchCandidate(url="not a url at all"),
        SearchCandidate(url="https://www.cbr.ru/report.pdf"),
    ]
    ranked = rank_candidates(candidates)
    assert len(ranked) == 1
    assert ranked[0].candidate.url == "https://www.cbr.ru/report.pdf"


def test_official_tier_is_not_a_safety_bypass_by_construction():
    """Документирующий тест: RankedCandidate не несёт никакого поля вроде
    `skip_validation`/`trusted` — единственное, что делает tier, это меняет
    порядок в списке. Регрессия здесь означала бы, что кто-то потом добавил
    короткий путь в downloader.py по tier'у, обходя url_safety."""
    candidates = [SearchCandidate(url="https://www.cbr.ru/report.pdf")]
    ranked = rank_candidates(candidates)
    fields = set(ranked[0].__dataclass_fields__)
    assert fields == {"candidate", "hostname", "tier", "rank_score"}


def test_known_regulator_domains_is_a_small_explicit_list():
    """Регресс-страховка: список — данные, не догадка модели, и не должен
    тихо разрастись/потеряться при рефакторинге."""
    assert set(KNOWN_REGULATOR_DOMAINS) == {"cbr.ru", "e-disclosure.ru", "moex.com", "sec.gov"}
