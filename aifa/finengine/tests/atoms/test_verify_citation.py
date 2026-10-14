"""`verify.citation` — критерий готовности атома (16a):

1. Неверная страница, неверный документ и изменённый текст отклоняются в 100%
   отрицательных тестов. Диапазон `range_start`/`range_end` не проверяется: только страница.
2. Корректная цитата проверяется по точной версии документа (страница текста
   передаётся вызывающим по `document_version`, не файлу с тем же именем —
   вне периметра этой чистой функции).
3. Проверка воспроизводима и не требует модельного решения.
4. Неуспешная проверка не удаляет цитату, а меняет её доверенный
   статус/создаёт finding.

Сценарии из карточки: неверная страница; одинаковая фраза на двух страницах;
OCR-различия (нормализация пробелов/кавычек/тире).
"""
from __future__ import annotations

from dataclasses import dataclass, field

from finengine.contracts import ConditionEvidence, QuoteEvidence
from finengine.skills.atoms.verify_citation import verify_citation, verify_citation_gate
from finengine.skills.spec.atom_registry import NodeArgs


def _quote(page: int, text: str, *, range_start=None, range_end=None) -> QuoteEvidence:
    return QuoteEvidence(
        produced_by_atom="read.quote", produced_by_atom_version="1.0.0", source_ref="doc-1",
        concept="капекс-политика", quote_text=text, page=page, range_start=range_start, range_end=range_end,
    )


def test_correct_page_and_text_passes():
    pages = {1: "Компания направила на капитальные затраты 500 млн руб. в отчётном периоде."}
    quote = _quote(1, "направила на капитальные затраты 500 млн руб.")
    finding = verify_citation(quote, pages=pages)
    assert finding.status == "pass"


def test_wrong_page_number_fails():
    pages = {1: "Текст страницы один без нужной фразы."}
    quote = _quote(2, "любая фраза")
    finding = verify_citation(quote, pages=pages)
    assert finding.status == "fail"


def test_same_phrase_on_a_different_page_than_claimed_fails():
    """Фраза реально есть — но на странице 3, а цитата заявляет страницу 2."""
    pages = {2: "На этой странице фразы нет вовсе.", 3: "Целевая фраза встречается здесь."}
    quote = _quote(2, "Целевая фраза встречается здесь.")
    finding = verify_citation(quote, pages=pages)
    assert finding.status == "fail"


def test_altered_text_fails():
    pages = {1: "Выручка выросла на 12% год к году."}
    quote = _quote(1, "Выручка выросла на 20% год к году.")
    finding = verify_citation(quote, pages=pages)
    assert finding.status == "fail"


def test_ocr_style_whitespace_and_dash_differences_are_tolerated():
    pages = {1: "чистый долг—EBITDA снизился  до   1,8x"}
    quote = _quote(1, "чистый долг-EBITDA снизился до 1,8x")
    finding = verify_citation(quote, pages=pages)
    assert finding.status == "pass"


def test_paraphrase_is_not_accepted_as_a_verified_quote():
    pages = {1: "Компания сократила операционные расходы на 5%."}
    quote = _quote(1, "Расходы компании упали на 5 процентов.")
    finding = verify_citation(quote, pages=pages)
    assert finding.status == "fail"


def test_range_is_not_checked_only_the_page_is():
    pages = {1: "Целевая фраза встречается ближе к концу страницы, а не в начале."}
    quote = _quote(1, "Целевая фраза встречается", range_start=0, range_end=5)
    finding = verify_citation(quote, pages=pages)
    assert finding.status == "pass"


def test_offsets_counted_in_whitespace_collapsed_text_do_not_fail_a_correct_quote():
    """Живой прогон на dev: `read.quote` считает диапазон в тексте со свёрнутыми пробелами, а гейт
    резал сырой текст — на странице с переносами верная цитата получала fail."""
    raw = "Бухгалтерская\n\n   отчетность\n\nОрганизация Публичное акционерное общество\n\nИНН 7708004767"
    collapsed = "Бухгалтерская отчетность Организация Публичное акционерное общество ИНН 7708004767"
    start = collapsed.find("ИНН 7708004767")
    quote = _quote(1, "ИНН 7708004767", range_start=start, range_end=start + len("ИНН 7708004767"))

    assert verify_citation(quote, pages={1: raw}).status == "pass"


def test_failed_verification_does_not_delete_the_quote_only_annotates_it():
    pages = {1: "Другой текст вообще."}
    quote = _quote(1, "Несуществующая фраза")
    finding = verify_citation(quote, pages=pages)
    assert finding.status == "fail"
    assert quote.quote_text == "Несуществующая фраза"  # цитата не тронута


# ---------------------------------------------------------------------------
# Уровень графового гейта (`verify_citation_gate`) — до Среза 9 не был
# покрыт ни одним тестом отдельно от чистой функции (см. докстринг модуля
# `verify_citation.py`): гейт сверял ЛЮБУЮ цитату только со страницей 1
# (`{1: text}`), что тихо проваливало верную цитату на любой другой странице
# многостраничного документа.
def _condition(page: int, text: str) -> ConditionEvidence:
    return ConditionEvidence(
        produced_by_atom="read.condition", produced_by_atom_version="1.0.0", source_ref="doc-1",
        concept="RATIO_NET_DEBT_EBITDA", condition_text=text, page=page,
        operator="<=", threshold_value=3.5, unit="x",
    )


@dataclass
class _Inputs:
    document_text: str = ""


@dataclass
class _Ctx:
    _deps: list
    inputs: _Inputs = field(default_factory=_Inputs)

    def dependencies(self):
        return tuple(self._deps)


_MULTI_PAGE_TEXT = (
    "===PAGE 1===\n"
    "Введение к отчёту.\n"
    "===PAGE 2===\n"
    "Целевая фраза встречается на второй странице документа.\n"
)


def test_gate_verifies_quote_on_a_page_other_than_one_using_real_page_markers():
    quote = _quote(2, "Целевая фраза встречается на второй странице документа.")
    ctx = _Ctx(_deps=[quote], inputs=_Inputs(document_text=_MULTI_PAGE_TEXT))
    result = verify_citation_gate(NodeArgs(), ctx)
    findings = [r for r in result if hasattr(r, "status")]
    assert len(findings) == 1
    assert findings[0].status == "pass"
    assert quote in result  # цитата не удаляется


def test_gate_also_verifies_condition_evidence():
    condition = _condition(2, "Целевая фраза встречается на второй странице документа.")
    ctx = _Ctx(_deps=[condition], inputs=_Inputs(document_text=_MULTI_PAGE_TEXT))
    result = verify_citation_gate(NodeArgs(), ctx)
    findings = [r for r in result if hasattr(r, "status")]
    assert len(findings) == 1
    assert findings[0].status == "pass"
    assert findings[0].source_ref == condition.evidence_id
    assert condition in result


def test_gate_blocks_condition_with_wrong_page_but_keeps_it_for_inspection():
    condition = _condition(1, "Целевая фраза встречается на второй странице документа.")
    ctx = _Ctx(_deps=[condition], inputs=_Inputs(document_text=_MULTI_PAGE_TEXT))
    result = verify_citation_gate(NodeArgs(), ctx)
    findings = [r for r in result if hasattr(r, "status")]
    assert len(findings) == 1
    assert findings[0].status == "fail"
    assert condition in result  # не удаляется — доверенный статус несёт finding


def test_gate_returns_empty_when_no_quotable_evidence():
    ctx = _Ctx(_deps=[], inputs=_Inputs(document_text=_MULTI_PAGE_TEXT))
    assert verify_citation_gate(NodeArgs(), ctx) == []
