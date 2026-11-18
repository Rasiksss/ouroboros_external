"""Разбор значения ячейки и единицы измерения — детерминированно, без модели.

Модель возвращает текст ячейки как написано («2 999,6», «(442,8)», «—»), а
число из него получает этот код. Арифметики в уме модели здесь нет по решению
владельца: код возит и считает числа, модель решает смысл.

⚠️ ТЕЛА ФУНКЦИЙ — ДОСЛОВНО ИЗ FINENGINE (#257, решение владельца 16.08:
«делай как у них»). Наш снимок был УЖЕ по всем семи расходившимся функциям:
не хватало разбора многовалютной шапки («millions of Korean won, thousands of
US dollars» — два курса в одной строке), процентных единиц и части масштабов.
Правки поверх этого файла делать только вместе со сверкой с их исходником,
иначе снимок снова начнёт расходиться (#258).
"""
from __future__ import annotations

import re
from typing import Optional

def _is_percentage_unit_text(text: object) -> bool:
    value = str(text or "").lower().replace("ё", "е")
    return "%" in value or "percent" in value or "процент" in value

def _parse_verbatim_value(text, *, decimal_comma_three: bool = False) -> Optional[float]:
    """Parse a verbatim cell text from PDF, RU/US locale aware.

    Used by the new verbatim-transcript extraction path (E-1 fix, 2026-05-05):
    LLM returns the cell text as-written ("2 999,6", "(442,8)", "—") and this
    function does deterministic numeric parsing — no LLM arithmetic involved.

      "2 999,6"   → 2999.6     (RU: space=thousands, comma=decimal)
      "60 855,1"  → 60855.1
      "(442,8)"   → -442.8     (parentheses = accounting negative)
      "1,234.56"  → 1234.56    (US fallback: comma=thousand, period=decimal)
      "—" "–" "-" "Н/Д" "" → None
      None / non-string → None
    """
    if text is None:
        return None
    if isinstance(text, (int, float)):
        return float(text)
    s = str(text).strip()
    if not s:
        return None
    # Sentinels for "no data"
    if s in ("—", "–", "−", "-", "Н/Д", "н/д", "n/a", "N/A", "NA", "null"):
        return None
    s = s.replace("\u00a0", " ").replace("\u2009", " ").replace("\u202f", " ")
    # Strip wrapping quotes / guillemets / footnote markers / trailing %
    s = s.strip("«»‹›\"'\u201c\u201d\u2018\u2019")
    s = re.sub(r"[¹²³⁴⁵⁶⁷⁸⁹⁰]", "", s)
    s = s.rstrip("%").strip()
    is_neg = False
    # Currency symbols/codes can be part of a verbatim table cell in US GAAP
    # reports ("$ 25,785", "USD 1,234"). They identify unit/currency, not
    # numeric scale, so strip them before deterministic numeric parsing.
    s = re.sub(r"^(?:[$€£¥₹₽]|USD|US\$|EUR|GBP|RUB|INR|RS\.?)\s*", "", s, flags=re.IGNORECASE)
    s = re.sub(r"\s*(?:[$€£¥₹₽]|USD|US\$|EUR|GBP|RUB|INR|RS\.?)$", "", s, flags=re.IGNORECASE)
    # Models sometimes repeat the scale already supplied in ``unit_text``
    # (for example, "$2.0 billion"). Keep the verbatim numeric magnitude;
    # _normalise_money_value applies the unit scale separately.
    s = re.sub(r"\s*(?:thousand|million|billion|trillion)\.?$", "", s, flags=re.IGNORECASE)
    # Parens = negative after currency stripping. Supports cells like "$ (0.01)".
    if s.startswith("(") and s.endswith(")"):
        is_neg = True
        s = s[1:-1].strip()
    # Optional explicit minus prefix, possibly after currency stripping.
    if s.startswith("-"):
        is_neg = not is_neg
        s = s[1:].strip()
    s = re.sub(r"^(?:[$€£¥₹₽]|USD|US\$|EUR|GBP|RUB|INR|RS\.?)\s*", "", s, flags=re.IGNORECASE)
    s = re.sub(r"\s*(?:[$€£¥₹₽]|USD|US\$|EUR|GBP|RUB|INR|RS\.?)$", "", s, flags=re.IGNORECASE)
    s = re.sub(r"\s*(?:thousand|million|billion|trillion)\.?$", "", s, flags=re.IGNORECASE)
    # 🔴 АПОСТРОФ — ТОЖЕ РАЗДЕЛИТЕЛЬ ТЫСЯЧ (30.08.2026, прогон по НОВАТЭКу).
    #
    # ЦЕНА ПРОПУСКА: 6 показателей из 122. Модель нашла ВСЁ и правильно —
    # `source_text: "1'156'724"` у выручки стоял верный, — но в число это не
    # разобралось, и строка вышла пустой. Отличить «движок не нашёл» от «нашёл
    # и не смог прочитать» по таблице было нельзя: и там и там прочерк.
    #
    # Выжили ровно те шесть, где апострофа не было: 144.23, 393, -804, 221 000.
    #
    # Так печатают отчётность швейцарской типографской традиции, и по-русски
    # тоже: у НОВАТЭКа так весь отчёт. Разбор ПЕРИОДОВ про апостроф знал давно
    # (`periods.py`: `.replace("'", "").replace("’", "")`), разбор ЧИСЕЛ — нет.
    # Со строки 78 апостроф снимался только по КРАЯМ ячейки, не в середине.
    #
    # Убираем ДО разбора запятых и точек: он ничего не значит для дробной части
    # и служит только группировке разрядов — как пробел.
    s = s.replace("'", "").replace("\u2019", "").replace("\u02bc", "")
    # Heuristic: RU locale (the common case in our corpus) — space is
    # thousand separator, comma is decimal. Strip spaces then comma→period.
    s_no_space = s.replace(" ", "")
    if not s_no_space:
        return None
    comma_count = s_no_space.count(",")
    period_count = s_no_space.count(".")
    if comma_count > 0 and period_count > 0:
        # US locale ("1,234.56") — strip commas
        s_no_space = s_no_space.replace(",", "")
    elif comma_count == 1:
        head, tail = s_no_space.split(",", 1)
        # Treat as decimal when:
        #  - head starts with 0 ("0,1612" = ratio) — always decimal
        #  - tail length 1-2 and head is numeric — RU decimal
        #  - tail length 3+ and head is numeric — usually US thousands
        #    ("42,245"), unless the caller knows this is a percentage cell
        #    from a Russian regulatory form ("7,377" / "7,4090")
        # Real US thousands ("1,234,567") have multiple commas → caught below.
        if head in ("", "0") or head.startswith("0"):
            s_no_space = head + "." + tail
        elif (
            1 <= len(tail) <= 2
            or (decimal_comma_three and len(tail) >= 3)
        ) and tail.isdigit() and head.replace(".", "").isdigit():
            s_no_space = head + "." + tail
        else:
            s_no_space = s_no_space.replace(",", "")
    elif comma_count > 1:
        # Multiple commas — US thousands grouping
        s_no_space = s_no_space.replace(",", "")
    # Now s_no_space should be a clean number with optional period
    try:
        n = float(s_no_space)
    except ValueError:
        return None
    return -n if is_neg else n

def _currency_from_unit_text(text: object) -> str:
    s = str(text or "").lower()
    if "₩" in s or "krw" in s or "korean won" in s:
        return "KRW"
    if "$" in s or "usd" in s or "dollar" in s or "долл" in s:
        return "USD"
    if "€" in s or "eur" in s or "euro" in s or "евро" in s:
        return "EUR"
    if "₹" in s or "inr" in s or "rupee" in s or re.search(r"\brs\.?\b", s):
        return "INR"
    if "руб" in s or "rouble" in s or "ruble" in s or "₽" in s or "rub" in s:
        return "руб."
    return ""

def _primary_currency_from_unit_text(text: object) -> str:
    """Return the first explicitly named currency in a multi-currency unit header."""
    s = str(text or "").lower()
    candidates = (
        ("KRW", ("₩", "krw", "korean won")),
        ("USD", ("$", "usd", "dollar", "долл")),
        ("EUR", ("€", "eur", "euro", "евро")),
        ("INR", ("₹", "inr", "rupee")),
        ("руб.", ("₽", "руб", "rouble", "ruble", "rub")),
    )
    matches = [
        (s.find(token), currency)
        for currency, tokens in candidates
        for token in tokens
        if s.find(token) >= 0
    ]
    return min(matches)[1] if matches else ""

def _unit_text_for_currency(text: object, currency: str, default_currency: str = "") -> str:
    """Extract the unit clause corresponding to a currency from a mixed header.

    Financial statements can provide a primary currency and a convenience
    translation in one header, e.g. ``millions of Korean won, thousands of US
    dollars``.  A metric value inherits the currency from its column header,
    not from the aggregate header.
    """
    raw = str(text or "").strip()
    if not raw or not currency:
        return _parse_unit_text(raw, default_currency)
    clauses = [part.strip() for part in re.split(r"[,;]", raw) if part.strip()]
    for clause in clauses:
        if _currency_from_unit_text(clause) == currency:
            return _parse_unit_text(clause, currency) or clause
    return _parse_unit_text(raw, currency) or raw

def _default_unit_for_standard(standard: str) -> str:
    if standard in {"ifrs_en", "us_gaap_en"}:
        return "млн. $"
    if standard in {"ind_as_en", "ind_gaap_en"}:
        return "INR"
    return "млн. руб."

def _canonical_money_unit(scale: str, currency: str) -> str:
    if currency == "руб.":
        return {"трлн": "трлн руб.", "млрд": "млрд руб.", "млн": "млн. руб.", "тыс": "тыс. руб.", "one": "руб."}[scale]
    if currency == "USD":
        return {"трлн": "трлн $", "млрд": "млрд $", "млн": "млн. $", "тыс": "тыс. $", "one": "$"}[scale]
    if currency == "INR":
        return {"трлн": "трлн INR", "млрд": "млрд INR", "млн": "млн INR", "тыс": "тыс INR", "one": "INR"}[scale]
    if currency:
        return {"трлн": f"трлн {currency}", "млрд": f"млрд {currency}", "млн": f"млн {currency}", "тыс": f"тыс {currency}", "one": currency}[scale]
    return {"трлн": "трлн", "млрд": "млрд", "млн": "млн", "тыс": "тыс", "one": ""}[scale]

def _parse_unit_text(text, default_currency: str = "") -> str:
    """Map verbatim unit text from PDF to canonical unit label.

    Used by the verbatim extraction path. Returns one of:
      "млрд руб.", "млн. руб.", "тыс. руб.", "руб.", "млн INR",
      "crore INR", "lakh INR", "INR", "%", or "" (unknown).
    Downstream `_normalise_money_value` converts money scales to millions
    while preserving the detected currency where possible.
    """
    if not text:
        return ""
    s = str(text).lower().strip()
    if not s:
        return ""
    # % / ratios
    if "%" in s or "коэффициент" in s or "коэф." in s or "ratio" in s or "доля" in s:
        return "%"
    currency = _currency_from_unit_text(s) or default_currency
    # Order matters: млрд before млн (substring), миллиард before миллион
    if (
        "млрд" in s
        or "миллиард" in s
        or "billion" in s
        or re.search(r"(?<![a-z])bn\.?(?![a-z])", s)
        or "трлн" in s
        or "триллион" in s
        or re.search(r"(?<![a-z])tn\.?(?![a-z])", s)
    ):
        # Treat трлн = 1000 × млрд by mapping to млрд (factor in _MONEY_SCALE_FACTORS handles it)
        return _canonical_money_unit("трлн", currency) if (
            "трлн" in s or "триллион" in s or re.search(r"(?<![a-z])tn\.?(?![a-z])", s)
        ) else _canonical_money_unit("млрд", currency)
    if "arab" in s or "arabs" in s:
        return f"arab {currency or 'INR'}".strip()
    if "crore" in s or "crores" in s or re.search(r"(?<![a-z])cr\.?(?![a-z])", s):
        return f"crore {currency or 'INR'}".strip()
    if "lakh" in s or "lakhs" in s or "lac" in s or "lacs" in s:
        return f"lakh {currency or 'INR'}".strip()
    if "тыс" in s or "thous" in s or re.search(r"(?<![a-z])th\.?(?![a-z])", s):
        return _canonical_money_unit("тыс", currency)
    if (
        "млн" in s
        or "миллион" in s
        or "million" in s
        or re.search(r"(?<![a-z])m(?:n|ln)?\.?(?![a-z])", s)
    ):
        return _canonical_money_unit("млн", currency)
    if "₹" in s or "inr" in s or "rupee" in s or re.search(r"\brs\.?\b", s):
        return "INR"
    if "руб" in s or "rouble" in s or "ruble" in s or "₽" in s or "rub" in s:
        return "руб."
    if "$" in s or "usd" in s or "dollar" in s or "долл" in s:
        return "USD"
    if "€" in s or "eur" in s or "euro" in s or "евро" in s:
        return "EUR"
    return ""

def _normalise_eps_unit(unit_text: object, default_unit: str = "") -> str:
    """EPS is per-share even when the table header only says rubles/kopecks."""
    s = str(unit_text or default_unit or "").lower().strip()
    if "коп" in s or "kopeck" in s or "kopey" in s:
        return "коп./акция"
    if "$" in s or "usd" in s or "dollar" in s or "долл" in s:
        return "$/share"
    if "€" in s or "eur" in s or "euro" in s or "евро" in s:
        return "EUR/share"
    if "₹" in s or "inr" in s or "rupee" in s or re.search(r"\brs\.?\b", s):
        return "INR/share"
    if "руб" in s or "₽" in s or "rub" in s or "rouble" in s or "ruble" in s:
        return "руб/акция"
    return "руб/акция"

# ── Unit normalisation ─────────────────────────────────────────────────
# All consumers (chains, quick_synthesis, calculated metrics, the metrics
# table in the UI) are simpler when every value is in the same scale.
# We pick `млн руб.` as canonical: human-readable for big banks/corps and
# avoids float precision issues (millions of millions kept tidy).
#
# When the model reports "млрд руб." (billions) we multiply by 1000.
# "тыс. руб." → divide by 1000. Anything we can't classify as a money
# scale (%, шт., тонн, etc.) is passed through unchanged — we don't try
# to convert non-money units, and the unit string is preserved so the UI
# can label the column correctly.
_MONEY_SCALE_FACTORS: dict[str, float] = {
    "трлн": 1_000_000.0,  # rare but possible
    "arab": 1000.0,
    "arabs": 1000.0,
    "crore": 10.0,
    "crores": 10.0,
    "lakh": 0.1,
    "lakhs": 0.1,
    "lac": 0.1,
    "lacs": 0.1,
    "миллиард": 1000.0,
    "млрд": 1000.0,
    "billion": 1000.0,
    "млн": 1.0,
    "миллион": 1.0,
    "million": 1.0,
    "тыс": 0.001,
    "thousand": 0.001,
}

def _normalise_money_value(
    value: Optional[float],
    unit: Optional[str],
) -> tuple[Optional[float], str]:
    """Convert (value, unit) into million-scale money while preserving currency.

    If `unit` is empty / not money (e.g. "%"), passes through.
    Currency-only Indian units such as "INR" or "Rs." are treated as one
    rupee and converted to millions with a 1e-6 factor. Existing RUB/USD
    currency-only behavior is preserved.
    Returns the canonical unit label so the metric record is consistent.
    """
    if value is None:
        return None, (unit or "")
    if not unit:
        return value, ""
    u = unit.lower().strip()
    # Non-money: %, шт., тонн, дней, … — leave as-is, propagate unit.
    if not any(kw in u for kw in (
        "arab", "arabs",
        "crore", "crores", "lakh", "lakhs", "lac", "lacs",
        "трлн", "миллиард", "млрд", "billion",
        "млн", "миллион", "million",
        "тыс", "thousand",
        "₹", "inr", "rupee", "rs.",
        "руб", "₽", "rub",
    )):
        return value, unit
    factor: float | None = None
    for kw, f in _MONEY_SCALE_FACTORS.items():
        if kw in u:
            factor = f
            break
    currency = _currency_from_unit_text(unit)
    if factor is None:
        factor = 0.000001 if currency == "INR" else 1.0
    return value * factor, _canonical_money_unit("млн", currency)

