"""AIFA training-set scope.

Расула's AutoML CatBoost reranker (`evolute_features_automl.cbm`) was trained
on the **55-метричный реестр** for non-financial reports — same set as the
validation Excel `Валидация 10 нефинан 55 метрик.xlsx`. Metrics outside this
set are unseen by the model's `metric` categorical feature, so reranking them
yields uncalibrated probabilities.

Until Расул retrains on the full 77, AIFA baseline AND AIFA Dev should both
restrict their extractable set to these 55 — keeps the two arches aligned
with what the upstream команда AIFA actually outputs in their product.

Source of truth: `tests/Валидация 10 нефинансовых отчетов/Разметка для
валидации 10 нефинансовых.csv` column `MetricEnum`.

When the v2 reranker arrives — bump the set to the 77 and delete this file
(callers can then go back to using `EXTRACTABLE_NON_FINANCIAL` directly).
"""
from __future__ import annotations


TRAINED_NF_55: frozenset[str] = frozenset({
    "MODIFIED_AUDITORS_OPINION",
    "LONG_TERM_DEBT",
    "LONG_TERM_LEASE_LIABILITIES",
    "SHORT_TERM_BORROWINGS",
    "SHORT_TERM_LEASE_LIABILITIES",
    "CASH_EQUIVALENTS",
    "BANK_DEPOSITS",
    "PERPETUAL_BONDS",
    "ACCOUNTS_PAYABLE",
    "ACCOUNTS_RECEIVABLE",
    "INVENTORIES",
    "TOTAL_CURRENT_ASSETS",
    "TOTAL_CURRENT_LIABILITIES",
    "TOTAL_ASSETS",
    "PPE",
    "DEFERRED_TAX_ASSETS",
    "DEFERRED_TAX_LIABILITIES",
    "INTANGIBLE_ASSETS",
    "GOODWILL",
    "TOTAL_EQUITY",
    "RIGHT_OF_USE_ASSETS",
    "SALES_REVENUE",
    "OPERATING_PROFIT",
    "COSTS_EXPENSES",
    "NET_INCOME",
    "EBT",
    "CAPITALIZED_INTEREST",
    "GROSS_PROFIT",
    "OPERATING_EXPENSES",
    "DEPRECIATION_AMORTIZATION",
    "CAPEX",
    "INTEREST_PAID",
    "INTEREST_RECEIVED",
    "DIVIDENDS_SHAREHOLDERS",
    "DIVIDENDS_PAID_NONCONTROLLING_INTERESTS",
    "DIVIDENDS",
    "SHARES_BUYBACK",
    "NET_OPERATING_CASH_FLOW",
    "SUBSIDIARY_ACQUISITIONS",
    "ASSOCIATE_INVESTMENTS",
    "ASSOCIATE_INCOMES",
    "ASSET_PURCHASES",
    "INTANGIBLE_ASSET_PURCHASES",
    "COMBINED_ASSET_PURCHASES",
    "ASSET_SALES_PROCEEDS",
    "FINANCIAL_INCOME",
    "FINANCIAL_EXPENSES",
    "PRINCIPAL_REPAYMENT_OF_LEASE_LIABILITIES",
    "ADJUSTMENTS_OPERATING_CASH_FLOWS",
    "CHANGES_OCF_ASSETS_LIABILITIES",
    "EBITDA",
    "GUARANTEES",
    "COMMITTED_CREDIT_FACILITIES",
    "FUTURE_CAPEX_LIABILITIES",
    "DOUBTFUL_ACCOUNTS_ALLOWANCE",
})


def filter_to_trained(metrics: list[dict]) -> list[dict]:
    """Keep only metrics whose `enum` is in `TRAINED_NF_55`. Preserves order."""
    return [m for m in metrics if m["enum"] in TRAINED_NF_55]
