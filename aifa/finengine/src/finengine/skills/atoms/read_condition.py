"""`read.condition` — карточка 19 (Срез 9, B10): извлечь договорное/
нормативное условие (ковенант) вместе с точным адресом источника. Тот же
паттерн извлечения с адресом, что `read.quote` (карточка 19 прямо называет
его похожим, 15b) — расширенный структурированными параметрами сравнения.

**Разделение слоёв (брифинг сессии).** `read.condition` локализует УСЛОВИЕ и
СТРУКТУРИРУЕТ его параметры (оператор/порог/единица/период/субъект/
определение/исключения) — он НЕ решает, соблюдается ли условие (это
`assess.rule`), и НЕ формулирует финансовый смысл порога (это
`assess.judgment`, по методике).

**Модель находит клаузу, код детерминированно разбирает её.** Модель
предлагает страницу, буквальную цитату и грубое структурное разбиение (текст
оператора, текст порога, единица, субъект, определение, исключения, период
действия). КОД детерминированно (1) проверяет, что нормализованный текст
цитаты — точная подстрока нормализованной страницы (та же техника, что
`_quote_evidence_for_topic` в `read_quote.py`; несовпадение — цитата не
создаётся вовсе, тест «wrong quote/page blocked»), и (2) разбирает текст
оператора/порога в закрытое перечисление/число (`parse_operator`/
`parse_threshold_value`, оба — чистые, отдельно тестируемые функции). Карточка:
«Оператор — закрытое перечисление; число порога разбирается детерминированно».

Незнакомая формулировка оператора или нечитаемое число порога оставляют
соответствующее поле `None` — условие остаётся НЕПОЛНЫМ доказательством
(`ConditionEvidence` с квотой/страницей, но без оператора/порога), а не
точным числовым правилом с угаданным значением (карточка: «неясное или
неполное условие не превращается в точное числовое правило»; тест
«incomplete condition»). `ref.norm` обязан отдельно отбраковывать такие
неполные условия перед использованием (см. `ref_norm.py`)."""
from __future__ import annotations

import asyncio
import math
import re
from collections.abc import Sequence

from finengine.contracts import ConditionEvidence

ATOM_VERSION = "1.0.0"

_PAGE_MARKER_RE = re.compile(r"===PAGE\s+(\d+)===\n?")
_LEADING_NUMBER_RE = re.compile(r"-?\d+(?:[.,]\d+)?")

# Явный, закрытый словарь фраз → оператор (RU + EN) — не NLP-угадывание, тот
# же уровень строгости, что `id_revision._MARKER_RE`/`read_entities.
# _parse_ownership_percent`. Порядок важен: более длинная/специфичная фраза
# идёт раньше более общей (иначе "не менее" никогда не совпадёт первым, если
# "менее" стоит выше в списке).
_OPERATOR_PHRASES: tuple[tuple[str, str], ...] = (
    ("не более", "<="), ("не выше", "<="), ("не должен превышать", "<="), ("не превышает", "<="),
    ("no more than", "<="), ("not to exceed", "<="), ("not exceed", "<="), ("at most", "<="),
    ("less than or equal", "<="),
    ("не менее", ">="), ("не ниже", ">="), ("at least", ">="), ("no less than", ">="),
    ("greater than or equal", ">="),
    ("менее", "<"), ("ниже", "<"), ("less than", "<"),
    ("более", ">"), ("выше", ">"), ("превышает", ">"), ("greater than", ">"), ("exceeds", ">"),
    ("не равен", "!="), ("not equal", "!="),
    ("равен", "=="), ("равно", "=="), ("составляет ровно", "=="), ("equal to", "=="), ("equals", "=="),
)


class ConditionResponseInvalid(RuntimeError):
    """Ответ модели не является объектом с полем `conditions` — `GraphExecutor`
    переспрашивает ВЕСЬ физический батч через свой `ExecutionBudget.max_retries`
    (тот же контракт, что `QuoteResponseInvalid` у `read.quote`)."""


def _pages_from_document_text(document_text: str) -> dict[int, str]:
    """Разбить текст документа на страницы по маркеру `===PAGE N===` — та же
    конвенция, что `read_quote.py::_pages_from_document_text`."""
    text = document_text or ""
    matches = list(_PAGE_MARKER_RE.finditer(text))
    if not matches:
        return {1: text}
    pages: dict[int, str] = {}
    for idx, match in enumerate(matches):
        start = match.end()
        end = matches[idx + 1].start() if idx + 1 < len(matches) else len(text)
        try:
            page_no = int(match.group(1))
        except ValueError:
            continue
        pages[page_no] = text[start:end]
    return pages


def _normalize_quote_text(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").replace(" ", " ")).strip()


def parse_operator(raw_text: str) -> str | None:
    """Детерминированный разбор текста оператора в закрытое перечисление
    (карточка: «оператор — закрытое перечисление»). Незнакомая формулировка
    → `None` — условие остаётся неполным, не получает угаданный оператор."""
    low = (raw_text or "").strip().lower()
    if not low:
        return None
    for phrase, op in _OPERATOR_PHRASES:
        if phrase in low:
            return op
    return None


def parse_threshold_value(raw_text: str) -> float | None:
    """Детерминированный разбор ведущего числа из текста порога — тот же
    уровень строгости, что `read_entities.py::_parse_ownership_percent` (не
    полноразмерный парсер чисел на естественном языке): десятичный разделитель
    "," нормализуется в ".", группы разрядов не разбираются. Число не найдено
    или не конечно → `None`."""
    if not raw_text:
        return None
    match = _LEADING_NUMBER_RE.search(raw_text)
    if not match:
        return None
    try:
        value = float(match.group(0).replace(",", "."))
    except ValueError:
        return None
    return value if math.isfinite(value) else None


def _condition_evidence_from_item(
    topic: str, raw_item: object, pages: dict[int, str], *, document_id: str, document_version: str,
) -> ConditionEvidence | None:
    if not isinstance(raw_item, dict):
        return None
    quote_text = str(raw_item.get("quote") or "").strip()
    try:
        page = int(raw_item.get("page"))
    except (TypeError, ValueError):
        return None
    quote_norm = _normalize_quote_text(quote_text)
    if not quote_text or not quote_norm or page not in pages:
        return None
    page_text_norm = _normalize_quote_text(pages[page])
    if quote_norm not in page_text_norm:
        # Не найдено буквально на заявленной странице: неверная страница,
        # перефразировка или клауза из другой версии документа — карточка
        # прямо запрещает таким текстам получать статус ConditionEvidence
        # (тест «wrong quote/page blocked»).
        return None

    operator = parse_operator(str(raw_item.get("operator_text") or ""))
    threshold_value = parse_threshold_value(str(raw_item.get("threshold_text") or ""))
    unit = str(raw_item.get("unit_text") or "").strip() or None
    subject = str(raw_item.get("subject") or "").strip() or None
    definition_text = str(raw_item.get("definition_text") or "").strip() or None
    exceptions = [str(e).strip() for e in (raw_item.get("exceptions") or []) if str(e or "").strip()]
    effective_period = str(raw_item.get("effective_period") or "").strip() or None

    return ConditionEvidence(
        produced_by_atom="read.condition", produced_by_atom_version=ATOM_VERSION,
        source_ref=f"{document_id}@page:{page}", document_version=document_version,
        concept=topic, period_ref=effective_period,
        condition_text=quote_text, page=page,
        operator=operator, threshold_value=threshold_value, unit=unit,
        subject=subject, definition_text=definition_text, exceptions=exceptions,
    )


def read_condition_batch(
    topics: Sequence[str],
    *,
    document_id: str,
    document_version: str,
    document_text: str,
    language: str = "ru",
    model_id: str | None = None,
) -> dict[str, list[ConditionEvidence]]:
    """Один физический вызов модели на ВСЕ `topics` сразу (пакетная
    экономика — тот же приём, что `read_quote_batch`). Каждая тема может дать
    НЕСКОЛЬКО клауз (тест «multiple covenants for the same metric» — например
    два разных договора займа с разными порогами по одному и тому же
    показателю) — модель возвращает список объектов на тему, не один. Тема
    без клаузы в тексте молча получает пустой список."""
    from finengine.skills.two_stage_extractor import _call_json_llm  # noqa: PLC0415

    result: dict[str, list[ConditionEvidence]] = {t: [] for t in topics}
    wanted = [t for t in topics if str(t or "").strip()]
    if not wanted:
        return result

    pages = _pages_from_document_text(document_text)
    system_prompt = (
        "You locate contractual/regulatory clauses (covenants, thresholds, ratios) in the document "
        "text that address the given topics. A topic may be addressed by MORE THAN ONE clause (e.g. "
        "two different loan agreements setting different thresholds for the same metric) — return ALL "
        "of them as a list. For each clause, quote the EXACT sentence(s) defining the condition (same "
        "words, same order, same punctuation — never paraphrase), the page it appears on, and structure "
        "what the clause says: the comparison phrase (e.g. 'not exceed', 'at least'), the threshold "
        "number as written, its unit (e.g. 'x', '%', currency code), who the condition applies to "
        "(subject), how the underlying term is defined in the clause if defined there (definition_text), "
        "any exceptions stated, and the effective period if stated. If a field is not present in the "
        "text, omit it — never invent a value. If no clause addresses a topic, omit that topic entirely."
    )
    topics_block = "\n".join(f"- {topic}" for topic in wanted)
    pages_block = "\n\n".join(f"===PAGE {page}===\n{text}" for page, text in sorted(pages.items()))
    user_prompt = (
        f"# Topics\n{topics_block}\n\n"
        f"# Document pages\n{pages_block}\n\n"
        "# Response format\n"
        "Return one valid JSON object only:\n"
        '{"conditions": {"<topic exactly as listed above>": [{"page": 1, '
        '"quote": "exact clause text from that page", "operator_text": "not exceed", '
        '"threshold_text": "3.5", "unit_text": "x", "subject": "the Borrower", '
        '"definition_text": "optional definition text", "exceptions": ["optional exception text"], '
        '"effective_period": "optional period text"}]}}\n'
    )
    parsed, _request, _raw = asyncio.run(_call_json_llm(
        prompt=user_prompt, system=system_prompt, model_id=model_id, stage="read_condition",
        debug_dir=None, prompt_name="read_condition_prompt", response_name="read_condition_response.json",
    ))
    if not isinstance(parsed, dict) or not isinstance(parsed.get("conditions"), dict):
        raise ConditionResponseInvalid("read.condition: ответ модели не содержит объект 'conditions'")

    for topic in wanted:
        raw_items = parsed["conditions"].get(topic) or []
        if isinstance(raw_items, dict):
            raw_items = [raw_items]
        if not isinstance(raw_items, list):
            continue
        evidences: list[ConditionEvidence] = []
        for raw_item in raw_items:
            try:
                evidence = _condition_evidence_from_item(
                    topic, raw_item, pages, document_id=document_id, document_version=document_version,
                )
            except Exception:  # noqa: BLE001 — сбой разбора ОДНОЙ клаузы не должен унести уже собранные соседние
                continue
            if evidence is not None:
                evidences.append(evidence)
        result[topic] = evidences
    return result


__all__ = [
    "ATOM_VERSION", "ConditionResponseInvalid", "read_condition_batch",
    "parse_operator", "parse_threshold_value",
]
