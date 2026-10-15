"""AIFA-style extraction pipeline.

Adapted from `Репа в репе/financier-extractor`. AIFA's full pipeline is:
  PDF → IDP-OCR → structured tables → CatBoost reranker (per metric) →
  top-3 tables → LLM extract.

This package ports the per-metric reranking + LLM extraction stages. The
CatBoost binary itself is in Git LFS in the upstream repo and isn't
bundled here yet — we use a heuristic scorer (n-gram intersect + fuzzy
match + structural bonuses) over the same feature signals the CatBoost
model would have consumed. When the LFS binary is brought in, we can swap
the heuristic for `cb.predict_proba` in one place.

Why a separate pipeline (not just our existing full-context):
  Per-metric search-then-extract is more focused — model sees a small
  set of relevant tables instead of the whole PDF, which the AIFA team
  found gives higher precision on Russian non-financial IFRS reports
  (their training data). Foreign reports / banks aren't supported.
"""
from finengine.skills.aifa.classifier import classify_supported, AIFAUnsupportedError
# Движок aifa снят 21.08.2026 (легаси, пятая копия реестра, числа
# в сто раз меньше). В пакете остаётся то, чем пользуется БОЕВОЙ путь.

__all__ = ["classify_supported", "AIFAUnsupportedError"]
