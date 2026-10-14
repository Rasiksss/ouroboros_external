"""`verify.citation` — карточка 16a: проверить, что цитата/условие
действительно присутствует по указанному адресу источника — детерминированно,
после того как модель уже выбрала цитату (карточка: «Цитирование проверяется
детерминированно после смыслового выбора модели»).

Не требует нового planner-механизма: текст документа сегодня приходит во
входы прогона (`ExecutionContext.inputs.document_text`), тем же путём, каким
уже пользуются `read.quote`/`read.value`/`locate` (докстринг `graph_atoms.py`)
— не через граф-зависимость от `ingest.document`/`doc.textify`. `verify.
citation` читает то же самое поле, не заводя отдельного требования.

Срез 9: гейт теперь ТАКЖЕ собирает `ConditionEvidence` из зависимостей узла
(`read.condition`, `19-srez-09-...md`) — `verify_citation()` уже была готова
принять его с самого начала (см. её сигнатуру ниже), не хватало только сбора
на границе графа. Постраничная разбивка `ctx.inputs.document_text` теперь
идёт ПО РЕАЛЬНЫМ маркерам `===PAGE N===` (`_pages_from_document_text`, та же
функция, что `read_quote.py`/`read_condition.py` уже используют при
извлечении) — раньше гейт сверял ЛЮБУЮ цитату только со страницей 1
(`{1: text}`), что для документа с несколькими страницами тихо проваливало
верную цитату с `page != 1`; ни один существующий тест этого не ловил, потому
что все они вызывали чистую функцию `verify_citation()` напрямую со своим
`pages`, минуя эту границу графа (см. `tests/atoms/test_verify_citation.py`)."""
from __future__ import annotations

import re

from finengine.contracts import ConditionEvidence, QuoteEvidence, VerificationFinding
from finengine.skills.spec.atom_registry import NodeArgs

ATOM_VERSION = "1.0.0"
CHECK_KIND = "citation"

_PAGE_MARKER_RE = re.compile(r"===PAGE\s+(\d+)===\n?")


def _pages_from_document_text(document_text: str) -> dict[int, str]:
    """Та же конвенция постраничной разбивки, что `read_quote.py`/
    `read_condition.py` — дублируется здесь намеренно (тот же стиль, что уже
    даёт этот модуль со своей отдельной `_normalize`), не выносится в общий
    хелпер ради одной короткой функции."""
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


_WS_RE = re.compile(r"\s+")
_QUOTE_CHARS = str.maketrans({"«": '"', "»": '"', "“": '"', "”": '"', "‘": "'", "’": "'"})
_DASH_CHARS = str.maketrans({"‐": "-", "‑": "-", "‒": "-", "–": "-", "—": "-"})


def _normalize(text: str) -> str:
    """Нормализация пробелов/кавычек/тире — допустимое форматирование, а не
    смысловое расхождение. НЕ токенное/семантическое сходство: перефраз не
    становится проверенной цитатой (инженерный принцип карточки)."""
    text = text.translate(_QUOTE_CHARS).translate(_DASH_CHARS)
    return _WS_RE.sub(" ", text).strip().casefold()


def verify_citation(
    evidence: QuoteEvidence | ConditionEvidence, *, pages: dict[int, str],
) -> VerificationFinding:
    """`pages` — текст СОГЛАСОВАННОЙ версии документа по номеру страницы (не
    вся история ревизий — карточка требует сверку «по точной версии
    документа, а не по файлу с тем же именем», версию несёт уже
    `EvidenceRecord.document_version` цитаты, вне этой функции)."""
    text = evidence.quote_text if isinstance(evidence, QuoteEvidence) else evidence.condition_text
    page = evidence.page
    common = dict(
        produced_by_atom="verify.citation", produced_by_atom_version=ATOM_VERSION,
        source_ref=evidence.evidence_id, concept=evidence.concept, period_ref=evidence.period_ref,
        check_kind=CHECK_KIND,
    )
    if page is None or page not in pages:
        return VerificationFinding(**common, status="fail", detail=f"нет текста для страницы {page!r}")

    page_text = _normalize(pages[page])
    needle = _normalize(text)
    if not needle:
        return VerificationFinding(**common, status="fail", detail="пустая цитата не может быть проверена")

    idx = page_text.find(needle)
    if idx < 0:
        return VerificationFinding(
            **common, status="fail", detail=f"цитата не найдена на странице {page} согласованной версии документа",
        )

    # Диапазон `range_start`/`range_end` не сверяем: `read.quote` считает его в тексте страницы со
    # свёрнутыми пробелами, а здесь страница сырая — смещения не совпадали и верная цитата получала fail.
    return VerificationFinding(**common, status="pass", detail=f"цитата подтверждена на странице {page}")


# ---------------------------------------------------------------------------
# Graph wiring — гейт на `read.quote`/`read.condition` (оба — producer'ы
# цитируемых доказательств, Срез 5b/9).
def verify_citation_gate(args: NodeArgs, ctx) -> list:
    dependencies = list(ctx.dependencies())
    quotables = [e for e in dependencies if isinstance(e, (QuoteEvidence, ConditionEvidence))]
    if not quotables:
        # When used as a mandatory publication gate, citation may be not
        # applicable. Preserve the gated typed result instead of replacing it
        # with an empty successful node.
        return dependencies
    pages = _pages_from_document_text(ctx.inputs.document_text)
    out: list = []
    for item in quotables:
        finding = verify_citation(item, pages=pages)
        out.append(finding)
        out.append(item)  # цитата/условие не удаляется — доверенный статус несёт finding, не мутация
    return out


__all__ = ["verify_citation", "verify_citation_gate", "ATOM_VERSION", "CHECK_KIND"]
