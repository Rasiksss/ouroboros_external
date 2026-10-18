"""История чата ouroboros и поиск в ней финала хода (CONTRACTS.md §2.6, §2.7)."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

# Дальше в прошлое ход не уходит: запись ищется от конца истории, и свежий ход лежит на первых
# страницах.
MAX_PAGES = 5


@dataclass(frozen=True)
class HistoryRecord:
    role: str
    text: str
    is_progress: bool
    client_message_id: str
    task_id: str
    ts: str


@dataclass(frozen=True)
class HistoryPage:
    """Записи страницы по возрастанию времени; `next_before_ts` пуст на самой старой странице."""

    records: tuple[HistoryRecord, ...]
    next_before_ts: str


HistoryFetch = Callable[[str], Awaitable[HistoryPage]]


def parse_page(payload: Any) -> HistoryPage:
    raw = payload.get("messages") if isinstance(payload, dict) else None
    records = tuple(_parse_record(item) for item in raw or [] if isinstance(item, dict))
    more = isinstance(payload, dict) and bool(payload.get("has_more"))
    return HistoryPage(records, str(payload.get("next_before_ts") or "") if more else "")


def _parse_record(item: dict[str, Any]) -> HistoryRecord:
    return HistoryRecord(
        role=str(item.get("role") or ""),
        text=str(item.get("text") or ""),
        is_progress=bool(item.get("is_progress")),
        client_message_id=str(item.get("client_message_id") or ""),
        task_id=str(item.get("task_id") or ""),
        ts=str(item.get("ts") or ""),
    )


def _is_final(record: HistoryRecord, root_task_id: str) -> bool:
    # Корень неизвестен (до шага 09 — ход без инструментов): первая запись с `task_id`, как у кадров.
    return (
        record.role == "assistant"
        and not record.is_progress
        and bool(record.task_id)
        and (not root_task_id or record.task_id == root_task_id)
    )


async def find_final(
    fetch: HistoryFetch, *, run_id: str, root_task_id: str
) -> HistoryRecord | None:
    """Финальный ответ корня хода `run_id` или `None`: записи хода несут `client_message_id == run_id`."""
    before_ts = ""
    for _ in range(MAX_PAGES):
        page = await fetch(before_ts)
        mine = [r for r in page.records if r.client_message_id == run_id]
        final = next((r for r in mine if _is_final(r, root_task_id)), None)
        if final is not None:
            return final
        # Реплика хода найдена, а ответа рядом нет: старее искать нечего.
        if any(r.role == "user" for r in mine) or not page.next_before_ts:
            return None
        before_ts = page.next_before_ts
    return None
