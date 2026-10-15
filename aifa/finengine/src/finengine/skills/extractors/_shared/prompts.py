"""Shared prompt fragments — building blocks cells compose into their own
system prompts. Source of truth still in bulk_extractor.py for now;
Phase D will pull these into _shared/ proper as the legacy file shrinks.
"""

from __future__ import annotations

from finengine.skills.bulk_extractor import (
    _actual_values_only_rules_en,
    _actual_values_only_rules_ru,
    _financial_analyst_intro_en,
    _financial_analyst_intro_ru,
    _period_rules_en,
    _period_rules_ru,
    _verbatim_value_rules_en,
    _verbatim_value_rules_ru,
)

__all__ = [
    "financial_analyst_intro_en",
    "financial_analyst_intro_ru",
    "actual_values_only_rules_en",
    "actual_values_only_rules_ru",
    "period_rules_en",
    "period_rules_ru",
    "verbatim_value_rules_en",
    "verbatim_value_rules_ru",
]


financial_analyst_intro_en = _financial_analyst_intro_en
financial_analyst_intro_ru = _financial_analyst_intro_ru
actual_values_only_rules_en = _actual_values_only_rules_en
actual_values_only_rules_ru = _actual_values_only_rules_ru
period_rules_en = _period_rules_en
period_rules_ru = _period_rules_ru
verbatim_value_rules_en = _verbatim_value_rules_en
verbatim_value_rules_ru = _verbatim_value_rules_ru
