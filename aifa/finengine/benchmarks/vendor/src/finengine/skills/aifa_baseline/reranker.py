"""CatBoost-AutoML table reranker for AIFA baseline.

Picks the best top-N tables for a given metric using Расула's
`evolution_no_embs_extra_deep_classifier_evolute_features_automl.cbm`
(Финэкстрактор `dev` ветка). 39 features per (table, metric) pair, of which
38 are numeric and `metric` is a categorical (CatBoost matches features by
NAME — column order in the DataFrame doesn't matter).

Faithful port of:
- `Репа в репе/financier-extractor-dev/.../strategies/search_processors/table_scorer.py`
- `Репа в репе/financier-extractor-dev/.../strategies/search_processors/feature_engineering.py`
- `Репа в репе/financier-extractor-dev/.../strategies/search_processors/selector.py`

Behavioural quirks preserved on purpose (so inference matches the model's
training distribution):

1. `clean_markdown` is the upstream version (only strips `|` and `---+`),
   NOT our richer `aifa/text_utils.py` one.
2. `process_row_for_scores` uses the LAST name variation in the loop body
   for row n-gram intersect — the upstream `for ngrams in …: common_uni = …`
   overwrites instead of accumulating. Reproduced.
3. `keyword_density` denominator is `len(cleaned_text)`, not token count.

If the .cbm file isn't present (or load fails), `rerank_tables` returns
None and the caller falls back to the heuristic scorer.
"""
from __future__ import annotations

import re
import threading
from collections import defaultdict
from pathlib import Path
from typing import Any, Optional

from finengine.skills.aifa.ngrams import NGrams, PREPROCESSOR


# 39 features expected by the AutoML CatBoost binaries.
RERANKER_FEATURE_ORDER: tuple[str, ...] = (
    # rank features
    "unigrams_intersect_score",
    "bigrams_intersect_score",
    "trigrams_intersect_score",
    "row_unigrams_intersect_score",
    "row_bigrams_intersect_score",
    "row_trigrams_intersect_score",
    "max_fuzzy_score",
    "sum_fuzzy_score",
    "structural_bonus_score",
    "row_count",
    "max_row_len",
    "avg_row_len",
    "row_len_ratio",
    "fuzzy_score_stable",
    "row_len_norm",
    "unigrams_to_table_ratio",
    "bigrams_to_table_ratio",
    "trigrams_to_table_ratio",
    # legacy_optional
    "num_rows",
    "page",
    # tf-idf overlaps
    "tfidf_unigrams_overlap",
    "tfidf_bigrams_overlap",
    "tfidf_trigrams_overlap",
    "unigrams_ratio",
    "bigrams_ratio",
    "trigrams_ratio",
    # engineered (structural / token / name stats)
    "page_count",
    "table_size",
    "non_empty_cells",
    "non_empty_ratio",
    "header_count",
    "keyword_density",
    "avg_name_length",
    "max_name_length",
    "num_names",
    "avg_token_length",
    "avg_row_length",
    "column_count",
    # categorical
    "metric",
)


# Upstream clean_markdown — keep deliberately simple.
def _clean_md_upstream(text: str) -> str:
    text = re.sub(r"\|", " ", text)
    text = re.sub(r"---+", "", text)
    return text.strip()


# ──────────────────────── feature_engineering.py port ────────────────────────


def _extract_ngrams_with_tfidf(text: str) -> dict[str, dict[str, float]]:
    cleaned = re.sub(r"[^a-zA-Zа-яА-Я ]", " ", text.lower())
    tokens = re.findall(r"\w+", cleaned)
    term_frequencies: dict[str, int] = defaultdict(int)
    for token in tokens:
        term_frequencies[token] += 1
    max_tf = max(term_frequencies.values()) if term_frequencies else 1
    tf_weights = {tok: (count / max_tf) for tok, count in term_frequencies.items()}
    ngrams: dict[str, set[str]] = {
        "unigrams": set(tokens),
        "bigrams": set(" ".join(tokens[i : i + 2]) for i in range(len(tokens) - 1)),
        "trigrams": set(" ".join(tokens[i : i + 3]) for i in range(len(tokens) - 2)),
    }
    return {
        kind: {ng: sum(tf_weights.get(w, 0.0) for w in ng.split()) for ng in vals}
        for kind, vals in ngrams.items()
    }


def _tfidf_overlap_features(
    table_ngrams: dict[str, dict[str, float]],
    name_ngrams_list: list[dict[str, dict[str, float]]],
) -> dict[str, float]:
    sum_common: dict[str, float] = defaultdict(float)
    for nn in name_ngrams_list:
        for kind in table_ngrams:
            sum_common[kind] += sum(
                table_ngrams[kind][ng] * nn[kind][ng]
                for ng in table_ngrams[kind]
                if ng in nn[kind]
            )
    eps = 1e-6
    ratios = {
        f"{kind}_ratio": sum_common[kind] / (sum(table_ngrams[kind].values()) + eps)
        for kind in sum_common
    }
    return {
        "tfidf_unigrams_overlap": float(sum_common.get("unigrams", 0.0)),
        "tfidf_bigrams_overlap": float(sum_common.get("bigrams", 0.0)),
        "tfidf_trigrams_overlap": float(sum_common.get("trigrams", 0.0)),
        **{k: float(v) for k, v in ratios.items()},
    }


def _additional_features(table_text: str, names: list[str]) -> dict[str, float]:
    cleaned_text = re.sub(r"[^a-zA-Zа-яА-Я ]", " ", table_text.lower())
    tokens = re.findall(r"\w+", cleaned_text)
    table_ngrams = _extract_ngrams_with_tfidf(cleaned_text)
    name_ngrams_list = [
        _extract_ngrams_with_tfidf(re.sub(r"[^a-zA-Zа-яА-Я ]", " ", str(n).lower()))
        for n in names
    ]
    out = _tfidf_overlap_features(table_ngrams, name_ngrams_list)

    avg_token_length = (sum(len(t) for t in tokens) / len(tokens)) if tokens else 0.0
    page_count = float(len(re.findall(r"\bстраница\s+\d+", cleaned_text, flags=re.IGNORECASE)))
    table_size = float(len(re.findall(r"\|", cleaned_text)))
    non_empty_cells = float(len(re.findall(r"\|[^|]+\|", cleaned_text)))
    non_empty_ratio = non_empty_cells / table_size if table_size > 0 else 0.0
    header_count = float(len(re.findall(r"\n\s*#(.+)", cleaned_text)))
    keyword_density = (
        sum(
            cleaned_text.count(k)
            for k in ("страница", "page", "таблица", "table", "important", "важно")
        )
        / len(cleaned_text)
        if cleaned_text
        else 0.0
    )
    if names:
        avg_name_length = float(sum(len(n) for n in names) / len(names))
        max_name_length = float(max(len(n) for n in names))
        num_names = float(len(names))
    else:
        avg_name_length = max_name_length = num_names = 0.0
    rows = cleaned_text.split("\n") if cleaned_text else []
    avg_row_length = float(sum(len(r.split()) for r in rows) / len(rows)) if rows else 0.0
    column_count = float(len(re.findall(r"\|", cleaned_text)) // 2) if cleaned_text else 0.0

    out.update(
        {
            "page_count": page_count,
            "table_size": table_size,
            "non_empty_cells": non_empty_cells,
            "non_empty_ratio": float(non_empty_ratio),
            "header_count": header_count,
            "keyword_density": float(keyword_density),
            "avg_name_length": avg_name_length,
            "max_name_length": max_name_length,
            "num_names": num_names,
            "avg_token_length": float(avg_token_length),
            "avg_row_length": avg_row_length,
            "column_count": column_count,
        }
    )
    return out


# ─────────────────────────── table_scorer.py port ────────────────────────────


def _fuzzy_match_score(target_terms: list[str], text: str, threshold: int = 50) -> tuple[int, int]:
    """Best + sum fuzz ratio of any single token in `text` against `target_terms`."""
    from fuzzywuzzy import process as _fp

    best = 0
    total = 0
    words = text.split()
    if not words:
        return 0, 0
    for term in target_terms:
        match = _fp.extractOne(term, words)
        if match is None:
            continue
        _, ratio = match
        if ratio >= threshold:
            total += ratio
            if ratio > best:
                best = ratio
    return best, total


def _structural_bonus(table_text: str) -> int:
    low = table_text.lower()
    bonus = 0
    if "...." in low or "страница" in low or "Содержание" in table_text:
        bonus -= 10
    if "отчёт" in low or "отчет" in low:
        bonus += 2
    return bonus


def _max_row_intersect_scores(
    table_text: str, names_ngrams: list[NGrams]
) -> tuple[int, int, int]:
    """Per Расула's `process_row_for_scores`: scan rows, for each row compute
    n-gram intersect against ONLY THE LAST name variation (the `for` loop
    overwrites — preserved verbatim so features match training distribution).
    Then sort rows by (tri, bi, uni) descending and return the top row's tuple.
    """
    if not names_ngrams:
        return 0, 0, 0
    last = names_ngrams[-1]
    rows = table_text.split("\n")
    best: tuple[int, int, int] = (0, 0, 0)
    for row in rows:
        cleaned = _clean_md_upstream(row)
        rng = PREPROCESSOR.extract_ngrams(cleaned)
        common_uni = len(rng.unigrams & last.unigrams)
        common_bi = len(rng.bigrams & last.bigrams)
        common_tri = len(rng.trigrams & last.trigrams)
        # Compare lexicographically (tri, bi, uni) — same as Расула's sort key.
        score = (common_tri, common_bi, common_uni)
        if score > best:
            best = score
    return best  # (tri, bi, uni) — same order as Расула's TableRowScores


def _table_scorer_features(
    table_text: str, names: list[str], names_ngrams: list[NGrams]
) -> dict[str, float]:
    cleaned_text = _clean_md_upstream(table_text)
    table_ng = PREPROCESSOR.extract_ngrams(cleaned_text)

    max_uni = max_bi = max_tri = 0
    for nn in names_ngrams:
        max_uni = max(max_uni, len(table_ng.unigrams & nn.unigrams))
        max_bi = max(max_bi, len(table_ng.bigrams & nn.bigrams))
        max_tri = max(max_tri, len(table_ng.trigrams & nn.trigrams))

    fuzzy_score, fuzzy_sum_score = _fuzzy_match_score(names, cleaned_text)
    structural_bonus_score = _structural_bonus(table_text)

    row_tri, row_bi, row_uni = _max_row_intersect_scores(table_text, names_ngrams)

    rows = table_text.split("\n")
    row_count = float(len(rows))
    max_row_len = float(max((len(r) for r in rows), default=0))
    avg_row_len = float(sum(len(r) for r in rows) / row_count) if row_count else 0.0
    text_len = float(len(cleaned_text))
    eps = 1e-6

    return {
        "unigrams_intersect_score": float(max_uni),
        "bigrams_intersect_score": float(max_bi),
        "trigrams_intersect_score": float(max_tri),
        "row_unigrams_intersect_score": float(row_uni),
        "row_bigrams_intersect_score": float(row_bi),
        "row_trigrams_intersect_score": float(row_tri),
        "max_fuzzy_score": float(fuzzy_score),
        "sum_fuzzy_score": float(fuzzy_sum_score),
        "structural_bonus_score": float(structural_bonus_score),
        "row_count": row_count,
        "max_row_len": max_row_len,
        "avg_row_len": avg_row_len,
        "row_len_ratio": max_row_len / (text_len + eps),
        "fuzzy_score_stable": float(fuzzy_score) / (text_len + eps),
        "row_len_norm": max_row_len / (row_count + eps),
        "unigrams_to_table_ratio": float(max_uni) / (len(table_ng.unigrams) + eps),
        "bigrams_to_table_ratio": float(max_bi) / (len(table_ng.bigrams) + eps),
        "trigrams_to_table_ratio": float(max_tri) / (len(table_ng.trigrams) + eps),
    }


# ────────────────────────── name variations helper ───────────────────────────


def _metric_variations(metric: dict) -> list[str]:
    """Concatenate every textual handle the metric can appear under (RU + EN)."""
    out: list[str] = []
    for k in ("name_ru", "name_en"):
        v = metric.get(k)
        if v:
            out.append(v)
    for v in (metric.get("aliases_ru") or []) + (metric.get("aliases_en") or []):
        if v and v != "Нет":
            out.append(v)
    seen: set[str] = set()
    unique: list[str] = []
    for v in out:
        if v not in seen:
            seen.add(v)
            unique.append(v)
    return unique


# ────────────────────────────── model loading ────────────────────────────────


_load_lock = threading.Lock()
# Multiple binaries loaded by path. Each kind (non_financial / bank) keeps its
# own CatBoost instance — switching company_type doesn't unload the other.
_loaded_models: dict[str, Any] = {}
_last_load_error: Optional[str] = None


_BINARY_BY_KIND: dict[str, str] = {
    "non_financial": "evolution_no_embs_extra_deep_classifier_evolute_features_automl.cbm",
    "bank":          "evolution_no_embs_extra_deep_classifier_ngram_automl_v2_Basic_Banks.cbm",
}


def _resolve_binary_path(kind: str = "non_financial") -> Path:
    """Pick the first candidate that exists for the given metric set.

    Resolution order (per kind):
      1. Explicit `FINENGINE_AIFA_RERANKER_BINARY` setting only applies when
         kind == "non_financial" (operator override for the default model).
      2. `<data_dir>/aifa_models/<filename>` — canonical prod location.
      3. Расула's local repo at `<project_root>/Репа в репе/...` — dev.

    Returns the first PATH that EXISTS; if none exist, returns the prod
    canonical path (loader will log "binary not found at <path>").
    """
    from finengine.config import settings

    fname = _BINARY_BY_KIND.get(kind, _BINARY_BY_KIND["non_financial"])

    if kind == "non_financial":
        env_path = getattr(settings, "aifa_reranker_binary_path", "") or ""
        if env_path:
            return Path(env_path)

    prod_path = Path(settings.data_dir) / "aifa_models" / fname
    project_root = Path(__file__).resolve().parents[4]
    dev_path = (
        project_root
        / "Репа в репе/financier-extractor-dev/src/financier_extractor"
        / "strategies/search_processors/binaries"
        / fname
    )

    for candidate in (prod_path, dev_path):
        if candidate.exists():
            return candidate
    return prod_path


def _try_load_model(path: Optional[Path] = None, kind: str = "non_financial") -> Optional[Any]:
    global _last_load_error
    if path is None:
        path = _resolve_binary_path(kind)
    key = str(path)
    with _load_lock:
        cached = _loaded_models.get(key)
        if cached is not None:
            return cached
        if not path.exists():
            _last_load_error = f"reranker binary not found at {path}"
            return None
        try:
            from catboost import CatBoostClassifier
        except ImportError as e:
            _last_load_error = f"catboost not installed: {e}"
            return None
        try:
            m = CatBoostClassifier()
            m.load_model(str(path))
        except Exception as e:
            _last_load_error = f"failed to load model: {e}"
            return None
        _loaded_models[key] = m
        _last_load_error = None
        return m


def get_load_error() -> Optional[str]:
    return _last_load_error


# ────────────────────────────── public API ───────────────────────────────────


def rerank_tables(
    tables: list[dict],
    metric: dict,
    top_n: int = 3,
    binary_path: Optional[Path] = None,
    company_type: str = "non_financial",
) -> Optional[list[tuple[float, dict]]]:
    """Return top-N tables for the given metric using the AutoML CatBoost model.

    Args:
        tables: each dict must have keys `text` (string) and `page` (int).
        company_type: "non_financial" (default) → NF AutoML model;
            "bank" → bank reranker (Basic_Banks).
        metric: registry entry — needs `enum`, `name_ru`, optionally `name_en`,
                `aliases_ru`, `aliases_en`.
        top_n: how many top tables to return.
        binary_path: override; if None resolves via settings or default.

    Returns:
        list of (proba, table) sorted desc by proba, capped at top_n. Or None
        if the model couldn't be loaded — caller should fall back to heuristic.
    """
    if not tables:
        return None
    model = _try_load_model(binary_path, kind=company_type)
    if model is None:
        return None

    try:
        import pandas as pd
    except ImportError:
        return None

    names = _metric_variations(metric)
    if not names:
        return None
    # Use lemmatized n-grams (PREPROCESSOR) — same as upstream NgramPreprocessor.
    names_ngrams = [PREPROCESSOR.extract_ngrams(n) for n in names]

    rows: list[dict] = []
    for t in tables:
        text = t.get("text", "") or ""
        feat = _table_scorer_features(text, names, names_ngrams)
        feat.update(_additional_features(text, names))
        feat["num_rows"] = float(len(text.split("\n")))
        feat["page"] = float(t.get("page") or 0)
        feat["metric"] = metric.get("enum", "")
        rows.append(feat)

    df = pd.DataFrame(rows, columns=list(RERANKER_FEATURE_ORDER))
    df["metric"] = df["metric"].astype("category")
    try:
        proba = model.predict_proba(df)[:, 1]
    except Exception as e:
        global _last_load_error
        _last_load_error = f"predict_proba failed: {e}"
        return None

    scored = list(zip([float(p) for p in proba], tables))
    scored.sort(key=lambda x: x[0], reverse=True)
    return scored[:top_n]
