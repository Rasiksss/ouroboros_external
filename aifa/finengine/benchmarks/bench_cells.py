"""Ячейки бенчмарка: маппинг golden-ячейка → датасет скорера → параметры движка.

Имена ячеек = имена папок в tests/test_benchmark/data/golden/ репозитория
Finengine И имена наших реестров src/finengine/data/registry/<cell>.json
(у нас и в апстриме реестры совпадают; дрейф банков ±2 enum — см. README).
"""

# cell → (dataset для скорера, company_type движка, language)
CELLS: dict[str, dict] = {
    "ifrs_nf_ru":      {"dataset": "IFRS_NF_RU",      "company_type": "non_financial", "language": "ru"},
    "ifrs_bank_ru":    {"dataset": "IFRS_BANK_RU",    "company_type": "bank",          "language": "ru"},
    "ifrs_nf_en":      {"dataset": "IFRS_NF_EN",      "company_type": "non_financial", "language": "en"},
    "ifrs_bank_en":    {"dataset": "IFRS_BANK_EN",    "company_type": "bank",          "language": "en"},
    "us_gaap_nf_en":   {"dataset": "US_GAAP_NF_ENG",  "company_type": "non_financial", "language": "en"},
    "us_gaap_bank_en": {"dataset": "US_GAAP_BANK_EN", "company_type": "bank",          "language": "en"},
}

# golden-папки в чекауте Finengine называются чуть иначе, чем реестры
# (banks vs bank). Ключ — наша ячейка, значение — имя папки в golden/.
GOLDEN_DIR: dict[str, str] = {
    "ifrs_nf_ru":      "ifrs_nf_ru",
    "ifrs_bank_ru":    "ifrs_banks_ru",
    "ifrs_nf_en":      "ifrs_nf_en",
    "ifrs_bank_en":    "ifrs_banks_en",
    "us_gaap_nf_en":   "us_gaap_nf_en",
    "us_gaap_bank_en": "us_gaap_banks_en",
}
