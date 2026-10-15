"""`doc.index` — семантическая карта документа, построенная один раз и
независимая от конкретного показателя (карточка `doc.index`, 15a).

Физическое обнаружение структуры (таблицы, диапазоны страниц, заголовки) —
детерминированный код, переиспользующий уже существующий разбор таблиц
`two_stage_extractor._extract_tables_from_text` (тот же самый каталог таблиц,
которым сегодня пользуется первый этап извлечения — не второй парсер той же
задачи). Смысловая разметка (какая таблица — баланс, а какая — ОПУ) — под
жёсткой схемой поверх уже найденных блоков; код и модель разделены по
инженерному принципу карточки: «физическое обнаружение структуры и её
смысловая разметка должны быть разделены».
"""
from __future__ import annotations

import re
from collections.abc import Callable, Sequence

from finengine.contracts import DocumentIndex, DocumentIndexEntry, DocumentText

INDEXER_VERSION = "1.0.0"

# Закрытый список меток — «неизвестный раздел сохраняется как неизвестный, а
# не принудительно относится к известному типу» (карточка `doc.index`).
KNOWN_TABLE_LABELS: tuple[str, ...] = (
    "balance_sheet",
    "income_statement",
    "cash_flow_statement",
    "equity_statement",
    "notes",
    "unknown",
)

# entry_id -> метка из KNOWN_TABLE_LABELS (или "unknown"/отсутствие ключа —
# оба читаются как "unknown"). Реальный путь — вызов модели под жёсткой
# схемой; тесты подставляют детерминированный дубль на этой ОДНОЙ границе,
# как и другие модельные атомы в этом репозитории.
TableLabelerFn = Callable[[Sequence[dict]], dict[str, str]]

_HEADING_LINE = re.compile(r"^[A-ZА-ЯЁ0-9 №.,'\"()/-]{6,100}$")


def _default_table_labeler(table_summaries: Sequence[dict]) -> dict[str, str]:
    """Реальный путь: одна модельная просьба под жёсткой схемой на ВЕСЬ
    каталог сразу (пакетная экономика — один вызов на документ, не на
    таблицу). Любой сбой разбора ответа — не падение атома: непомеченная
    таблица остаётся `unknown`, что и так безопасный дефолт вызывающего кода.
    """
    if not table_summaries:
        return {}
    import asyncio

    from finengine.skills.two_stage_extractor import _call_json_llm, _json_dump

    catalog = _json_dump([
        {"entry_id": item["entry_id"], "title": item.get("title", ""), "sample": item.get("sample", "")}
        for item in table_summaries
    ])
    prompt = (
        "You are given a catalogue of tables detected in a financial report. "
        "For each entry_id, classify it into exactly one of: "
        f"{', '.join(KNOWN_TABLE_LABELS)}. "
        "If you cannot confidently classify a table, use \"unknown\" — never guess.\n\n"
        f"Tables:\n{catalog}\n\n"
        'Return JSON: {"labels": {"<entry_id>": "<label>", ...}} for every entry_id given above.'
    )
    try:
        parsed, _request, _raw = asyncio.run(_call_json_llm(
            prompt=prompt, system="Return valid JSON only.", model_id=None,
            stage="doc_index_labeling", debug_dir=None,
            prompt_name="doc_index_labeling_prompt", response_name="doc_index_labeling_response.json",
        ))
    except Exception:  # noqa: BLE001 — модель недоступна/ответила мусором: всё остаётся unknown, атом не падает
        return {}
    labels = parsed.get("labels") if isinstance(parsed, dict) else None
    if not isinstance(labels, dict):
        return {}
    out: dict[str, str] = {}
    for entry_id, label in labels.items():
        label_str = str(label or "").strip().lower()
        if label_str in KNOWN_TABLE_LABELS:
            out[str(entry_id)] = label_str
    return out


def _joined_text_with_page_markers(pages: Sequence[DocumentText]) -> str:
    ordered = sorted((p for p in pages if p.status == "ok"), key=lambda p: p.page)
    return "\n\n".join(f"===PAGE {p.page}===\n{p.text}" for p in ordered)


def _heading_entries(pages: Sequence[DocumentText]) -> list[DocumentIndexEntry]:
    entries: list[DocumentIndexEntry] = []
    for page in pages:
        if page.status != "ok":
            continue
        for raw_line in page.text.splitlines():
            line = raw_line.strip()
            if 5 < len(line) < 100 and _HEADING_LINE.match(line) and not line.startswith("|"):
                entries.append(DocumentIndexEntry(
                    kind="heading", label="", title=line, page_start=page.page, page_end=page.page,
                ))
    return entries


def build_document_index(
    pages: Sequence[DocumentText],
    *,
    document_id: str,
    document_version: str,
    total_pages: int,
    indexer_version: str = INDEXER_VERSION,
    table_labeler: TableLabelerFn | None = None,
) -> DocumentIndex:
    """Построить `DocumentIndex` по уже распознанному постраничному тексту
    (`doc.textify`). Чистая функция от `(pages, document_version,
    indexer_version)` — тот же вход даёт тот же индекс, что и есть
    кэшируемость по паре версий без отдельного слоя хранения в этой сессии
    (сам слой кэша — Срез 10, здесь только чистота функции, на которой он
    будет держаться).
    """
    table_labeler = table_labeler or _default_table_labeler
    joined_text = _joined_text_with_page_markers(pages)

    from finengine.skills.two_stage_extractor import _extract_tables_from_text

    tables = _extract_tables_from_text(joined_text)
    table_entries = [
        DocumentIndexEntry(
            kind="table", label="", title=table.title,
            page_start=table.page or 1, page_end=table.page or 1,
        )
        for table in tables
    ]

    labels = table_labeler([
        {"entry_id": entry.entry_id, "title": entry.title, "sample": table.text[:300]}
        for entry, table in zip(table_entries, tables)
    ])
    labeled_tables = [
        entry.model_copy(update={"label": labels.get(entry.entry_id, "")})
        for entry in table_entries
    ]

    entries = [*labeled_tables, *_heading_entries(pages)]
    return DocumentIndex(
        document_id=document_id, document_version=document_version, indexer_version=indexer_version,
        total_pages=total_pages, entries=entries,
    )


__all__ = [
    "INDEXER_VERSION",
    "KNOWN_TABLE_LABELS",
    "TableLabelerFn",
    "build_document_index",
]
