"""Heuristic table scorer (CatBoost-free).

Mirrors the feature signals computed by AIFA's `TableScorer` —
n-gram intersect counts, fuzzy-match score, structural bonuses — but
aggregates them with a hand-tuned weighted sum instead of CatBoost
predict_proba. Once the LFS-stored cb model is bundled, swap
`score_table` for `cb.predict_proba(features)[:, 1]` in selector.py.
"""
from __future__ import annotations

from dataclasses import dataclass

from fuzzywuzzy import fuzz, process

from finengine.skills.aifa.ngrams import NGrams, PREPROCESSOR
from finengine.skills.aifa.text_utils import clean_markdown


@dataclass
class TableScores:
    unigrams_intersect_score: int
    bigrams_intersect_score: int
    trigrams_intersect_score: int
    max_fuzzy_score: int
    sum_fuzzy_score: int
    structural_bonus_score: int


def fuzzy_match_score(target_terms: list[str], text: str, threshold: int = 50) -> tuple[int, int]:
    """Best + sum fuzz ratio of any single token in `text` against `target_terms`."""
    best = 0
    total = 0
    words = text.split()
    if not words:
        return 0, 0
    for term in target_terms:
        match = process.extractOne(term, words, scorer=fuzz.ratio)
        if match is None:
            continue
        _, ratio = match
        if ratio >= threshold:
            total += ratio
            if ratio > best:
                best = ratio
    return best, total


def structural_bonus(text: str) -> int:
    """Penalize TOC-like pages, reward financial-statement headers."""
    low = text.lower()
    bonus = 0
    if "...." in low or "страница" in low or "содержание" in text:
        bonus -= 10
    if "отчёт" in low or "отчет" in low:
        bonus += 2
    if "итого" in low:
        bonus += 1
    return bonus


def score_table(table_text: str, metric_names: list[str], metric_ngrams: NGrams) -> TableScores:
    """Score how relevant a table is for a given metric.

    Args:
        table_text: full text of the table (cells joined as in pdfplumber output)
        metric_names: list of variations to fuzzy-match (name_ru, name_en, aliases)
        metric_ngrams: pre-computed NGrams of the metric (union over its variations)
    """
    cleaned = clean_markdown(table_text)
    table_ngrams = PREPROCESSOR.extract_ngrams(cleaned)
    common_uni = len(metric_ngrams.unigrams & table_ngrams.unigrams)
    common_bi = len(metric_ngrams.bigrams & table_ngrams.bigrams)
    common_tri = len(metric_ngrams.trigrams & table_ngrams.trigrams)
    max_fz, sum_fz = fuzzy_match_score(metric_names, cleaned)
    bonus = structural_bonus(cleaned)
    return TableScores(
        unigrams_intersect_score=common_uni,
        bigrams_intersect_score=common_bi,
        trigrams_intersect_score=common_tri,
        max_fuzzy_score=max_fz,
        sum_fuzzy_score=sum_fz,
        structural_bonus_score=bonus,
    )


def aggregate_score(s: TableScores) -> float:
    """Weighted sum that mimics what the CatBoost head learned to upweight.

    Empirically: trigrams matter most (precise terminology hits), then bigrams,
    then fuzzy match for typos. Unigrams are noisy — small weight. Structural
    bonuses are the smallest factor.
    """
    return (
        4.0 * s.trigrams_intersect_score
        + 2.0 * s.bigrams_intersect_score
        + 0.5 * s.unigrams_intersect_score
        + 0.05 * s.max_fuzzy_score
        + 0.01 * s.sum_fuzzy_score
        + 1.0 * s.structural_bonus_score
    )
