"""Fallback period-type contract for metric registry rebuilds.

`metrics.json` is the runtime source of truth. This module is only a fallback
for older Excel registries that do not yet have a period-type column.
"""

POINT_PERIOD_TYPE = "point"
DURATION_PERIOD_TYPE = "duration"

POINT_IN_TIME_METRIC_ENUMS = frozenset(
    {
        "ACCOUNTS_PAYABLE",
        "ACCOUNTS_RECEIVABLE",
        "ASSOCIATE_INVESTMENTS",
        "BANK_DEPOSITS",
        "CASH_AND_CASH_BALANCES_WITH_CB",
        "CASH_EQUIVALENTS",
        "CET1_CAPITAL",
        "CET1_RATIO_DISCLOSED",
        "CLIENT_DEPOSITS",
        "COMMITTED_CREDIT_FACILITIES",
        "COMMON_EQUITY_TIER_ONE_CAPITAL",
        "CORPORATE_DEPOSITS",
        "CORPORATE_LOANS",
        "CUSTOMER_DEPOSITS",
        "DEBT_SECURITIES_ISSUED",
        "DEFERRED_TAX_ASSETS",
        "DEFERRED_TAX_LIABILITIES",
        "DERIVATIVE_LIABILITIES",
        "DOUBTFUL_ACCOUNTS_ALLOWANCE",
        "ECL_ALLOWANCE_STAGE1",
        "ECL_ALLOWANCE_STAGE2",
        "ECL_ALLOWANCE_STAGE3",
        "FUNDS_FROM_CENTRAL_BANK",
        "FUTURE_CAPEX_LIABILITIES",
        "GOODWILL",
        "GOVERNMENTAL_DEPOSITS",
        "GUARANTEES",
        "INDIVIDUALS_DEPOSITS",
        "INDIVIDUALS_LOANS",
        "INSURANCE_LIABILITIES",
        "INTERBANK_DEPOSITS",
        "INTERBANK_LOANS",
        "INTANGIBLE_ASSETS",
        "INVENTORIES",
        "JUNIOR_DEBT",
        "LEASE_LIABILITIES_BANKS",
        "LOANS_STAGE1_GROSS",
        "LOANS_STAGE2_GROSS",
        "LOANS_STAGE3_GROSS",
        "LOANS_TO_CUSTOMERS",
        "LONG_TERM_DEBT",
        "LONG_TERM_LEASE_LIABILITIES",
        "LONG_TERM_ROU",
        "MODIFIED_AUDITORS_OPINION",
        "NON_CONTROL_INTEREST",
        "NPL_TOTAL",
        "OTHER_ASSETS",
        "OTHER_LIABILITIES",
        "PERPETUAL_BONDS",
        "PPE",
        "TOTAL_CREDIT_ALLOWANCE",
        "REPO_PLEDGED_SECURITIES",
        "RETAINED_EARNINGS",
        "RETAIL_LOANS",
        "RIGHT_OF_USE_ASSETS",
        "RISK_WEIGHTED_ASSETS",
        "SECURITIES_AMORTISED_COST",
        "SECURITIES_ASSETS",
        "SECURITIES_FVOCI",
        "SECURITIES_FVPL",
        "SECURITIES_LIABILITIES",
        "SHARE_CAPITAL",
        "SHARE_PREMIUM",
        "SHORT_TERM_BORROWINGS",
        "SHORT_TERM_LEASE_LIABILITIES",
        "SHORT_TERM_ROU",
        "TIER1_CAPITAL",
        "TIER1_RATIO_DISCLOSED",
        "TOP_BORROWERS",
        "TOTAL_ASSETS",
        "TOTAL_ASSOCIATE_INVESTMENTS",
        "TOTAL_CURRENT_ASSETS",
        "TOTAL_CURRENT_LIABILITIES",
        "TOTAL_EQUITY",
        "TOTAL_LOANS_TO_CLIENTS",
        "TREASURY_SHARES",
    }
)

DEFAULT_METRIC_PERIOD_TYPES = {
    enum: POINT_PERIOD_TYPE for enum in POINT_IN_TIME_METRIC_ENUMS
}


def fallback_period_type_for_enum(enum: object) -> str:
    """Return fallback period type for old registries without explicit data."""
    value = str(enum or "").strip().upper()
    return DEFAULT_METRIC_PERIOD_TYPES.get(value, DURATION_PERIOD_TYPE)
