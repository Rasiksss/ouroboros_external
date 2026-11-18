"""Document profile classifier — detects (company_type, standard, language).

Maps every uploaded report to one of 10 extractor-matrix cells:

  | profile_id        | standard | type | language |
  |-------------------|----------|------|----------|
  | ifrs_nf_ru        | IFRS     | NF   | RU       |
  | ifrs_bank_ru      | IFRS     | Bank | RU       |
  | rsbu_nf_ru        | RAS      | NF   | RU       |
  | rsbu_bank_ru      | RAS/CBR  | Bank | RU       |
  | us_gaap_nf_en     | US GAAP  | NF   | EN       |
  | us_gaap_bank_en   | US GAAP  | Bank | EN       |
  | ifrs_nf_en        | IFRS     | NF   | EN       |
  | ifrs_bank_en      | IFRS     | Bank | EN       |
  | ind_as_nf_en      | Ind AS   | NF   | EN       |
  | ind_gaap_bank_en  | Ind GAAP | Bank | EN       |

The classifier is deterministic and zero-cost. It uses contextual accounting
standard markers plus grouped business-model markers for bank vs
non-financial routing. The bank/NF decision deliberately avoids broad registry
aliases such as "other assets" or "operating expenses"; those are useful for
metric extraction but too noisy for profile classification.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import re
from typing import Iterable


PROFILE_IDS = (
    "ifrs_nf_ru",
    "ifrs_bank_ru",
    "rsbu_nf_ru",
    "rsbu_bank_ru",
    "us_gaap_nf_en",
    "us_gaap_bank_en",
    "ifrs_nf_en",
    "ifrs_bank_en",
    "ind_as_nf_en",
    "ind_gaap_bank_en",
)


@dataclass(frozen=True)
class Profile:
    profile_id: str
    standard: str
    company_type: str
    language: str
    confidence_low: bool = False
    reason: str = ""
    signals: dict = field(default_factory=dict)
    axis_confidence_low: dict[str, bool] = field(default_factory=dict)


_CYRILLIC_RE = re.compile(r"[А-Яа-яЁё]")
_HEAD_WINDOW = 200_000

_IFRS_BASIS_PATTERNS = (
    re.compile(r"\bprepared\s+in\s+accordance\s+with\s+ifrs\b", re.I),
    re.compile(r"\bin\s+accordance\s+with\s+ifrs\s+accounting\s+standards\b", re.I),
    re.compile(r"\bin\s+accordance\s+with\s+international\s+financial\s+reporting\s+standards\b", re.I),
    re.compile(r"\bifrs\s+as\s+(?:adopted|issued)\s+by\b", re.I),
    re.compile(r"\bмеждународн\w+\s+стандарт\w+\s+финансов\w+\s+отч[её]тност\w+\b", re.I),
    re.compile(r"\bв\s+соответствии\s+с\s+мсфо\b", re.I),
)

# A broad IFRS mention is useful routing evidence, but it is not necessarily
# the accounting basis of the document being classified. RAS accounting
# policies routinely refer to a particular IFRS/IAS standard for leases,
# impairment or financial instruments. These stricter patterns require the
# phrase to describe the preparation of financial statements/information and
# are therefore suitable for the confidence flag when RAS form evidence is
# also present.
_IFRS_REPORT_BASIS_PATTERNS = (
    re.compile(
        r"\b(?:настоящ\w+|данн\w+|прилагаем\w+|обобщенн\w+|консолидированн\w+|"
        r"промежуточн\w+|сокращенн\w+)(?:\s+\w+){0,8}\s+"
        r"(?:финансов\w+\s+отч[её]тност\w+|финансов\w+\s+информаци\w+)\b"
        r".{0,360}\b(?:подготовлен|составлен|представлен)\w*\b.{0,160}"
        r"\bв\s+соответствии\s+с\s+(?:мсфо\b|международн\w+\s+стандарт\w+\s+"
        r"финансов\w+\s+отч[её]тност\w+\b)",
        re.I | re.S,
    ),
)

_RSBU_BASIS_PATTERNS = (
    re.compile(r"\bв\s+соответствии\s+с\s+правилами\s+составления\s+бухгалтерской\s+отч[её]тности,?\s+установленными\s+в\s+российской\s+федерации\b", re.I),
    re.compile(r"\bроссийск\w+\s+стандарт\w+\s+бухгалтерск\w+\s+уч[её]та\b", re.I),
    re.compile(r"\bроссийск\w+\s+положени\w+\s+по\s+бухгалтерскому\s+уч[её]ту\b", re.I),
    re.compile(r"\bбухгалтерск\w+\s+отч[её]тност\w+\b.{0,240}\bроссийской\s+федерации\b", re.I | re.S),
    re.compile(r"\bнормативн\w+\s+акт\w+\s+банка\s+россии\b.{0,180}\bбухгалтерск\w+\s+отч[её]тност\w+\b", re.I | re.S),
    re.compile(r"\bбухгалтерск\w+\s+отч[её]тност\w+\s+кредитн\w+\s+организаци\w+\b.{0,180}\bнормативн\w+\s+акт\w+\s+банка\s+россии\b", re.I | re.S),
)

_RSBU_FORM_MARKERS = re.compile(
    r"\b(?:0710001|0710002|0710004|0409806|0409807|бухгалтерский\s+баланс|"
    r"отч[её]т\s+о\s+финансовых\s+результатах|отч[её]тность\s+кредитной\s+организации)\b",
    re.I,
)

_US_GAAP_BASIS_PATTERNS = (
    re.compile(r"\bprepared\s+in\s+accordance\s+with\s+(?:u\.s\.\s+|us\s+)?gaap\b", re.I),
    re.compile(r"\bin\s+accordance\s+with\s+(?:u\.s\.\s+|us\s+)?gaap\b", re.I),
    re.compile(r"\baccounting\s+principles\s+generally\s+accepted\s+in\s+the\s+united\s+states(?:\s+of\s+america)?\b", re.I),
    re.compile(r"\bgenerally\s+accepted\s+accounting\s+principles\s+in\s+the\s+united\s+states\b", re.I),
    re.compile(r"\bin\s+conformity\s+with\s+generally\s+accepted\s+accounting\s+principles\s+in\s+the\s+united\s+states\b", re.I),
)

_IND_AS_BASIS_PATTERNS = (
    re.compile(r"\bprepared\s+in\s+accordance\s+with\s+(?:the\s+)?Indian\s+Accounting\s+Standards\s*\(?['\"]?Ind[-\s]*AS['\"]?\)?\b", re.I),
    re.compile(r"\bprepared\s+in\s+compliance\s+with\s+(?:the\s+)?Indian\s+Accounting\s+Standards\s*\(?['\"]?Ind[-\s]*AS['\"]?\)?\b", re.I),
    re.compile(r"\bprepared\s+in\s+accordance\s+with\s+(?:the\s+)?recognition\s+and\s+measurement\s+principles\s+laid\s+down\s+in\s+(?:the\s+)?Indian\s+Accounting\s+Standard\s+\d+\b", re.I),
    re.compile(r"\bprepared\s+in\s+accordance\s+with\s+applicable\s+accounting\s+standards\s+i\.?e\.?\s+Ind[-\s]*AS\b", re.I),
    re.compile(r"\bin\s+conformity\s+with\s+(?:the\s+)?Indian\s+Accounting\s+Standards\s+(?:prescribed|specified)\s+under\s+Section\s+133\b", re.I),
    re.compile(r"\bin\s+conformity\s+with\s+(?:the\s+)?Indian\s+Accounting\s+Standards\s*\(['\"]?Ind[-\s]*AS['\"]?\)\s+specified\s+under\s+Section\s+133\b", re.I),
    re.compile(r"\bcomply\s+with\s+(?:the\s+)?Ind[-\s]*AS\s+specified\s+under\s+Section\s+133\b", re.I),
)

_IND_GAAP_BASIS_PATTERNS = (
    re.compile(r"\bGenerally\s+Accepted\s+Accounting\s+Principles\s*(?:\(\s*GAAP\s*\)\s*)?in\s+India\b(?:\s*\(\s*['\"]?Indian\s+GAAP['\"]?\s*\))?", re.I),
    re.compile(r"\baccounting\s+principles\s+generally\s+accepted\s+in\s+India,\s+including\s+(?:the\s+)?Accounting\s+Standards\s+specified\s+under\s+Section\s+133\b", re.I),
    re.compile(r"\bAccounting\s+Standard\s*\(\s*AS[-\s]?25\s*\)\s*,?\s*(?:specified|prescribed)\s+under\s+Section\s+133\b", re.I),
    re.compile(r"\bAccounting\s+Standards\s+notified\s+under\s+Section\s+133\s+of\s+the\s+Companies\s+Act\b", re.I),
    re.compile(r"\bAccounting\s+Standards\s*\(AS\)\s+and\s+pronouncements\s+issued\s+by\s+(?:the\s+)?Institute\s+of\s+Chartered\s+Accountants\s+of\s+India\b", re.I),
    re.compile(r"\bAccounting\s+Standard\s+21\b.*\bAccounting\s+Standard\s+23\b.*\bAccounting\s+Standard\s+27\b.*\bInstitute\s+of\s+Chartered\s+Accountants\s+of\s+India\b", re.I | re.S),
    re.compile(r"\bCompanies\s+\(Accounting\s+Standards?\)\s+Rules?\s+2021\b", re.I),
)

_IFRS_CODENAMES = re.compile(
    r"\b(?:IFRS\s*\d+|IAS\s*\d+|IFRIC\s*\d+|SIC\s*\d+|МСФО\s*\(?(?:IFRS|IAS)\)?\s*\d+)\b",
    re.I,
)
_US_GAAP_CODENAMES = re.compile(
    r"\b(?:ASC\s*\d+|ASU\s*\d{4}|FASB|Accounting\s+Standards\s+Codification|"
    r"Topic\s+\d{3}|Subtopic\s+\d{3}-\d{2})\b",
    re.I,
)
_US_GAAP_MENTION = re.compile(r"\b(?:U\.S\.\s+|US\s+)?GAAP\b", re.I)
_IND_AS_CODENAMES = re.compile(
    r"\b(?:Ind[-\s]*AS[-\s]*\d+|Indian\s+Accounting\s+Standard\s+\d+|"
    r"Companies\s+\(Indian\s+Accounting\s+Standards\)\s+Rules?)\b",
    re.I,
)
_IND_GAAP_CODENAMES = re.compile(
    r"\b(?:Indian\s+GAAP|Indian\s+Generally\s+Accepted\s+Accounting\s+Principles\s*\(\s*IGAAP\s*\)|"
    r"IGAAP|Generally\s+Accepted\s+Accounting\s+Principles\s*"
    r"(?:\(\s*GAAP\s*\)\s*)?in\s+India|Accounting\s+Standards\s*\(AS\)|"
    r"Companies\s+\(Accounting\s+Standard\)\s+Rules?)\b",
    re.I,
)
_FORM_10K_Q = re.compile(r"\bForm\s+10[-‒]?[KkQq]\b")
_FORM_20F = re.compile(r"\bForm\s+20[-‒]?F\b")


_MARKER_GROUPS: dict[str, tuple[str, ...]] = {
    "bank_interest": (
        "net interest income",
        "net interest margin",
        "interest-earning assets",
        "interest bearing deposits",
        "interest earned",
        "interest expended",
        "чистый процентный доход",
        "чистая процентная маржа",
        "процентные доходы",
        "процентные расходы",
    ),
    "bank_lending": (
        "loans and leases",
        "loans held for investment",
        "loans to customers",
        "total loans",
        "loan portfolio",
        "commercial loans",
        "consumer loans",
        "credit card loans",
        "mortgage loans",
        "finance lease receivables",
        "net investment in leases",
        "кредиты клиентам",
        "ссуды клиентам",
        "кредитный портфель",
    ),
    "bank_deposits": (
        "customer deposits",
        "total deposits",
        "deposits from customers",
        "brokered deposits",
        "deposit liabilities",
        "средства клиентов",
        "депозиты клиентов",
        "средства физических лиц",
        "средства юридических лиц",
    ),
    "bank_capital_regulation": (
        "common equity tier 1",
        "cet1",
        "tier 1 capital",
        "risk-weighted assets",
        "risk weighted assets",
        "basel iii",
        "capital adequacy",
        "норматив достаточности капитала",
        "базового капитала",
        "капитала первого уровня",
    ),
    "bank_credit_loss": (
        "provision for credit losses",
        "allowance for credit losses",
        "loan loss provision",
        "loan losses",
        "non-performing loans",
        "nonperforming loans",
        "расходы на создание резервов",
        "резерв под ожидаемые кредитные убытки",
        "обесцененные кредиты",
    ),
    "bank_regulator": (
        "federal reserve",
        "fdic",
        "office of the comptroller",
        "bank of russia",
        "банк россии",
        "центральный банк",
    ),
    "bank_leasing_activity": (
        "лизинг",
        "лизингов",
        "finance lease receivables",
        "net investment in leases",
    ),
    "nf_revenue_cost": (
        "gross profit",
        "cost of sales",
        "cost of revenue",
        "cost of revenues",
        "cost of goods sold",
        "revenue from contracts with customers",
        "sales revenue",
        "product revenue",
        "service revenue",
        "валовая прибыль",
        "себестоимость продаж",
        "себестоимость реализации",
        "выручка от реализации",
        "выручка по договорам с покупателями",
    ),
    "nf_inventory_trade": (
        "inventories",
        "inventory",
        "raw materials",
        "finished goods",
        "trade receivables",
        "trade and other receivables",
        "trade payables",
        "trade and other payables",
        "запасы",
        "готовая продукция",
        "сырье",
        "сырьё",
        "дебиторская задолженность",
        "кредиторская задолженность",
    ),
    "nf_operating_assets": (
        "property, plant and equipment",
        "right-of-use assets",
        "intangible assets",
        "основные средства",
        "нематериальные активы",
        "активы в форме права пользования",
    ),
    "nf_industry": (
        "manufacturing",
        "retail",
        "wholesale",
        "software",
        "semiconductor",
        "oil and gas",
        "mining",
        "transportation",
        "automotive",
        "pharmaceutical",
        "construction",
        "real estate",
        "производство",
        "розничная торговля",
        "добыча",
        "строительство",
        "недвижимость",
        "транспорт",
    ),
}


def _detect_language_details(text: str) -> tuple[str, bool, str, dict]:
    sample = text[:20_000]
    cyrillic = sum(1 for ch in sample if _CYRILLIC_RE.match(ch))
    latin = sum(1 for ch in sample if ch.isalpha() and not _CYRILLIC_RE.match(ch))
    total = cyrillic + latin
    if total == 0:
        return "en", True, "no alphabetic language evidence", {
            "cyrillic": 0,
            "latin": 0,
            "cyrillic_ratio": 0.0,
        }
    ratio = cyrillic / total
    language = "ru" if ratio >= 0.3 else "en"
    low = total < 50 or 0.2 <= ratio < 0.4
    reason = f"cyrillic ratio {ratio:.3f} over {total} letters"
    return language, low, reason, {
        "cyrillic": cyrillic,
        "latin": latin,
        "cyrillic_ratio": ratio,
    }


def detect_language(text: str) -> str:
    language, _, _, _ = _detect_language_details(text)
    return language


def _matches(patterns: Iterable[re.Pattern[str]], text: str) -> list[str]:
    hits: list[str] = []
    for pattern in patterns:
        match = pattern.search(text)
        if match:
            hits.append(match.group(0)[:120])
    return hits


def _count_phrase(text: str, phrase: str) -> int:
    phrase_cf = phrase.casefold()
    if re.search(r"[a-zA-Z]", phrase_cf):
        pattern = re.compile(rf"(?<![a-z0-9]){re.escape(phrase_cf)}(?![a-z0-9])", re.I)
        return len(pattern.findall(text))
    return text.count(phrase_cf)


def _group_hits(text: str) -> dict[str, dict[str, int]]:
    low = text.casefold()
    out: dict[str, dict[str, int]] = {}
    for group, phrases in _MARKER_GROUPS.items():
        hits = {phrase: count for phrase in phrases if (count := _count_phrase(low, phrase)) > 0}
        if hits:
            out[group] = hits
    return out


def _present_groups(hits: dict[str, dict[str, int]], prefix: str) -> set[str]:
    return {group for group in hits if group.startswith(prefix)}


def detect_standard(text: str) -> tuple[str, bool, str, dict]:
    head = text[:_HEAD_WINDOW]
    ifrs_basis = _matches(_IFRS_BASIS_PATTERNS, head)
    ifrs_report_basis = _matches(_IFRS_REPORT_BASIS_PATTERNS, head)
    rsbu_basis = _matches(_RSBU_BASIS_PATTERNS, head)
    us_basis = _matches(_US_GAAP_BASIS_PATTERNS, head)
    ind_as_basis = _matches(_IND_AS_BASIS_PATTERNS, head)
    ind_gaap_basis = _matches(_IND_GAAP_BASIS_PATTERNS, head)
    us_gaap_mentions = _matches((_US_GAAP_MENTION,), head)
    ifrs_codes = len(_IFRS_CODENAMES.findall(text))
    us_codes = len(_US_GAAP_CODENAMES.findall(text))
    ind_as_codes = len(_IND_AS_CODENAMES.findall(text))
    ind_gaap_codes = len(_IND_GAAP_CODENAMES.findall(text))
    form_10k_q = bool(_FORM_10K_Q.search(head))
    form_20f = bool(_FORM_20F.search(head))
    rsbu_form_markers = len(_RSBU_FORM_MARKERS.findall(head))
    indian_bank_statutory = bool(
        re.search(r"\breserve\s+bank\s+of\s+india\b", head, re.I)
        and re.search(r"\binterest\s+(?:earned|expended)\b", head, re.I)
        and re.search(
            r"\b(?:capital\s+adequacy|gross\s+npa|net\s+npa|advances|deposits)\b",
            head,
            re.I,
        )
    )
    signals = {
        "ifrs_basis": ifrs_basis,
        "ifrs_report_basis": ifrs_report_basis,
        "rsbu_basis": rsbu_basis,
        "rsbu_form_markers": rsbu_form_markers,
        "us_gaap_basis": us_basis,
        "ind_as_basis": ind_as_basis,
        "ind_gaap_basis": ind_gaap_basis,
        "us_gaap_mentions": us_gaap_mentions,
        "ifrs_codenames": ifrs_codes,
        "us_gaap_codenames": us_codes,
        "ind_as_codenames": ind_as_codes,
        "ind_gaap_codenames": ind_gaap_codes,
        "form_10k": form_10k_q,
        "form_10k_q": form_10k_q,
        "form_20f": form_20f,
        "indian_bank_statutory": indian_bank_statutory,
    }

    # Russian statutory forms often mention IFRS in an accounting-policy note
    # (for example for leases or impairment). Only a competing report-level
    # basis keeps confidence low; an isolated standard reference does not.
    if rsbu_form_markers >= 4 and _CYRILLIC_RE.search(head):
        report_basis_conflict = bool(
            ifrs_report_basis or us_basis or ind_as_basis or ind_gaap_basis
        )
        return (
            "rsbu",
            report_basis_conflict,
            "dense Russian statutory form markers",
            signals,
        )

    indian_basis_count = bool(ind_as_basis) + bool(ind_gaap_basis)
    global_basis_count = bool(ifrs_basis) + bool(us_basis) + bool(rsbu_basis)
    if rsbu_basis and not ifrs_basis and not us_basis and not indian_basis_count:
        return "rsbu", False, "strict Russian statutory accounting basis phrase", signals
    if rsbu_basis and (ifrs_basis or us_basis or indian_basis_count):
        if not (ifrs_report_basis or us_basis or indian_basis_count):
            return (
                "rsbu",
                False,
                "strict Russian statutory basis with incidental IFRS references",
                signals,
            )
        basis_scores = {
            "ifrs": len(ifrs_basis) + ifrs_codes,
            "us_gaap": len(us_basis) + us_codes + (3 if form_10k_q else 0),
            "ind_as": len(ind_as_basis) + ind_as_codes,
            "ind_gaap": len(ind_gaap_basis) + ind_gaap_codes,
            "rsbu": len(rsbu_basis) * 3 + rsbu_form_markers,
        }
        winner = max(basis_scores, key=basis_scores.get)
        return winner, True, "conflicting strict basis phrases", signals
    if ind_gaap_basis and not ind_as_basis and not global_basis_count:
        return "ind_gaap", False, "strict Indian GAAP basis phrase", signals
    if ind_as_basis and not ind_gaap_basis and not global_basis_count:
        return "ind_as", False, "strict Ind AS basis phrase", signals
    if indian_basis_count and not global_basis_count:
        winner = "ind_gaap" if ind_gaap_codes >= ind_as_codes else "ind_as"
        return winner, True, "conflicting Indian standard basis phrases", signals
    if indian_basis_count and global_basis_count:
        basis_scores = {
            "ifrs": len(ifrs_basis) + ifrs_codes,
            "us_gaap": len(us_basis) + us_codes + (3 if form_10k_q else 0),
            "ind_as": len(ind_as_basis) + ind_as_codes,
            "ind_gaap": len(ind_gaap_basis) + ind_gaap_codes,
        }
        winner = max(basis_scores, key=basis_scores.get)
        return winner, True, "conflicting strict basis phrases", signals

    if us_basis and not ifrs_basis:
        return "us_gaap", False, "strict US GAAP basis phrase", signals
    if ifrs_basis and us_gaap_mentions:
        return "ifrs", True, "strict IFRS basis phrase with US GAAP mention", signals
    if ifrs_basis and not us_basis:
        return "ifrs", False, "strict IFRS basis phrase", signals
    if us_basis and ifrs_basis:
        winner = "us_gaap" if us_codes > ifrs_codes or form_10k_q else "ifrs"
        return winner, True, "conflicting strict basis phrases", signals
    if indian_bank_statutory:
        return "ind_gaap", False, "Indian bank RBI statutory reporting format", signals
    if form_10k_q and us_codes >= max(1, ifrs_codes):
        return "us_gaap", False, "SEC 10-K/10-Q with US GAAP codename support", signals
    if form_20f and ifrs_codes >= us_codes:
        return "ifrs", False, "20-F with IFRS codename support", signals
    if us_codes >= max(ifrs_codes, ind_as_codes, ind_gaap_codes) + 3:
        return "us_gaap", False, "US GAAP codename majority", signals
    if ifrs_codes >= max(us_codes, ind_as_codes, ind_gaap_codes) + 3:
        return "ifrs", False, "IFRS codename majority", signals
    if ind_as_codes >= max(ifrs_codes, us_codes, ind_gaap_codes) + 3:
        return "ind_as", False, "Ind AS codename majority", signals
    if ind_gaap_codes >= max(ifrs_codes, us_codes, ind_as_codes) + 3:
        return "ind_gaap", False, "Indian GAAP codename majority", signals
    if rsbu_form_markers >= 2 and _CYRILLIC_RE.search(head):
        return "rsbu", True, "Russian statutory form markers without strict basis phrase", signals
    if form_10k_q:
        return "us_gaap", True, "SEC 10-K/10-Q without codename support", signals
    return "ifrs", True, "weak standard evidence, default IFRS", signals


def detect_company_type(text: str) -> tuple[str, bool, str, dict]:
    hits = _group_hits(text)
    bank_groups = _present_groups(hits, "bank_")
    nf_groups = _present_groups(hits, "nf_")
    leasing_hits = hits.get("bank_leasing_activity", {})
    strong_leasing_activity = (
        leasing_hits.get("лизинг", 0) >= 10
        or leasing_hits.get("лизингов", 0) >= 5
        or "finance lease receivables" in leasing_hits
        or "net investment in leases" in leasing_hits
    )

    has_bank_core = bool(
        bank_groups
        & {
            "bank_interest",
            "bank_lending",
            "bank_deposits",
            "bank_capital_regulation",
        }
    )
    has_bank_two_core = (
        len(
            bank_groups
            & {
                "bank_interest",
                "bank_lending",
                "bank_deposits",
                "bank_capital_regulation",
            }
        )
        >= 2
    )
    has_lending_business = bool(
        ("bank_lending" in bank_groups or strong_leasing_activity)
        and ("bank_interest" in bank_groups or "bank_credit_loss" in bank_groups)
    )
    has_regulated_bank = bool(
        "bank_capital_regulation" in bank_groups
        and ("bank_lending" in bank_groups or "bank_deposits" in bank_groups or "bank_interest" in bank_groups)
    )
    has_classic_bank = bool(
        ("bank_interest" in bank_groups)
        and ("bank_lending" in bank_groups or "bank_deposits" in bank_groups)
    )
    bank_interest_hits = hits.get("bank_interest", {})
    has_specific_bank_interest = bool(
        "net interest income" in bank_interest_hits
        or "net interest margin" in bank_interest_hits
        or "чистый процентный доход" in bank_interest_hits
        or "чистая процентная маржа" in bank_interest_hits
        or {"interest earned", "interest expended"} <= set(bank_interest_hits)
    )

    has_nf_core = bool("nf_revenue_cost" in nf_groups or "nf_inventory_trade" in nf_groups)
    has_nf_two_core = len(nf_groups & {"nf_revenue_cost", "nf_inventory_trade", "nf_operating_assets"}) >= 2

    bank_evidence = sum(
        [
            has_classic_bank,
            has_regulated_bank,
            has_lending_business,
            "bank_capital_regulation" in bank_groups and "bank_regulator" in bank_groups,
        ]
    )
    nf_evidence = sum(
        [
            has_nf_core,
            has_nf_two_core,
            "nf_industry" in nf_groups and ("nf_revenue_cost" in nf_groups or "nf_inventory_trade" in nf_groups),
        ]
    )

    signals = {
        "groups": hits,
        "bank_groups": sorted(bank_groups),
        "nf_groups": sorted(nf_groups),
        "has_classic_bank": has_classic_bank,
        "has_specific_bank_interest": has_specific_bank_interest,
        "has_regulated_bank": has_regulated_bank,
        "has_lending_business": has_lending_business,
        "has_bank_core": has_bank_core,
        "has_bank_two_core": has_bank_two_core,
        "strong_leasing_activity": strong_leasing_activity,
        "has_nf_core": has_nf_core,
        "has_nf_two_core": has_nf_two_core,
        "bank_evidence": bank_evidence,
        "nf_evidence": nf_evidence,
    }

    if strong_leasing_activity and "bank_interest" in bank_groups and "bank_credit_loss" in bank_groups:
        return "bank", False, f"financial leasing credit profile detected ({bank_evidence} vs {nf_evidence})", signals
    if (
        nf_evidence >= 3
        and bank_evidence <= 2
        and "bank_capital_regulation" not in bank_groups
        and "bank_credit_loss" not in bank_groups
        and not has_specific_bank_interest
        and not has_regulated_bank
    ):
        return "non_financial", False, f"strong non-financial operating profile detected ({nf_evidence} vs {bank_evidence})", signals
    if bank_evidence >= 1 and not (nf_evidence >= 2 and not has_bank_two_core):
        return "bank", False, f"bank group combination detected ({bank_evidence} vs {nf_evidence})", signals
    if nf_evidence >= 1 and not has_bank_two_core:
        return "non_financial", False, f"non-financial operating groups detected ({nf_evidence})", signals
    if has_bank_core and not has_nf_core:
        return "bank", True, "weak bank-only evidence", signals
    return "non_financial", True, "weak or conflicting company-type evidence", signals


def _compose_profile_id(standard: str, company_type: str, language: str) -> str:
    type_short = "bank" if company_type == "bank" else "nf"
    if standard == "rsbu":
        return f"rsbu_{type_short}_ru"
    if standard == "us_gaap":
        return f"us_gaap_{type_short}_en"
    if standard == "ind_as":
        return "ind_as_nf_en" if company_type != "bank" else "ifrs_bank_en"
    if standard == "ind_gaap":
        return "ind_gaap_bank_en" if company_type == "bank" else "ifrs_nf_en"
    return f"ifrs_{type_short}_{language}"


_STANDARD_OVERRIDE_ALIASES = {
    "ifrs": "ifrs",
    "us_gaap": "us_gaap",
    "usgaap": "us_gaap",
    "ind_as": "ind_as",
    "indas": "ind_as",
    "ind_gaap": "ind_gaap",
    "indgaap": "ind_gaap",
    "rsbu": "rsbu",
    "ras": "rsbu",
    "cbr": "rsbu",
    "рсбу": "rsbu",
}


def normalize_standard_override(value: str | None) -> str | None:
    """Normalize an explicit API standard override, or reject unknown values."""
    raw = str(value or "").strip().casefold().replace("-", "_").replace(" ", "_")
    if not raw:
        return None
    normalized = _STANDARD_OVERRIDE_ALIASES.get(raw)
    if normalized is None:
        allowed = ", ".join(sorted({"ifrs", "us_gaap", "ind_as", "ind_gaap", "rsbu"}))
        raise ValueError(f"Unknown reporting standard override {value!r}; expected one of: {allowed}")
    return normalized


def override_profile_standard(profile: Profile, standard: str) -> Profile:
    """Return a routing-only profile with the requested reporting standard."""
    normalized = normalize_standard_override(standard)
    if normalized is None:
        return profile
    axis_low = dict(profile.axis_confidence_low)
    axis_low["standard"] = False
    signals = dict(profile.signals)
    signals["standard_override"] = normalized
    return profile_from_axes(
        standard=normalized,
        company_type=profile.company_type,
        language="ru" if normalized == "rsbu" else profile.language,
        reason=f"explicit reporting-standard override: {normalized}; {profile.reason}",
        signals=signals,
        axis_confidence_low=axis_low,
    )


def profile_from_axes(
    *,
    standard: str,
    company_type: str,
    language: str,
    reason: str = "",
    signals: dict | None = None,
    axis_confidence_low: dict[str, bool] | None = None,
) -> Profile:
    axis_low = dict(axis_confidence_low or {})
    return Profile(
        profile_id=_compose_profile_id(standard, company_type, language),
        standard=standard,
        company_type=company_type,
        language=language,
        confidence_low=any(axis_low.values()),
        reason=reason,
        signals=signals or {},
        axis_confidence_low=axis_low,
    )


def detect_profile(text: str, *, standard_text: str | None = None) -> Profile:
    language, language_low, language_reason, language_signals = _detect_language_details(text)
    standard, standard_low, standard_reason, standard_signals = detect_standard(standard_text or text)
    company_type, type_low, type_reason, type_signals = detect_company_type(text)
    return profile_from_axes(
        standard=standard,
        company_type=company_type,
        language=language,
        reason=f"{standard_reason}; {type_reason}; {language_reason}",
        signals={
            "standard": standard_signals,
            "company_type": type_signals,
            "language": language_signals,
        },
        axis_confidence_low={
            "standard": standard_low,
            "company_type": type_low,
            "language": language_low,
        },
    )
