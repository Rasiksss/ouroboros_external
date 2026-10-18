"""История чата ouroboros и поиск в ней финала хода (CONTRACTS.md §2.6, §2.7)."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping, Sequence
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


# Сколько последних сообщений отдаёт история, как прежняя серверная сессия.
MESSAGE_LIMIT = 200
CONTENT_LIMIT = 20_000
SCAN_PAGES = 20           # страниц одного чата за запрос: прогресс и уведомления занимают места


@dataclass(frozen=True)
class TurnMessage:
    ts: str
    role: str
    content: str


def turn_messages(records: Sequence[HistoryRecord], roots: Mapping[str, str]) -> list[TurnMessage]:
    """Реплики ходов `roots` и финалы их корней по порядку времени (§2.6, ADR-0011).

    `roots` — `run_id` ходов сессии → корень (`""` — не назван). Реплика хода — запись пользователя с
    `client_message_id == run_id`, финал — запись корня (`_is_final`); корень не назван (ход оборвался до
    финала) — первая непрогрессная запись ассистента с `task_id`, как у `find_final`. Прогресс, уведомления,
    итоги подзадач и записи чужих ходов отбрасываются.
    """
    answered: set[str] = set()
    messages: list[TurnMessage] = []
    for record in records:
        root = roots.get(record.client_message_id)
        if root is None:
            continue
        if record.role == "user":
            messages.append(TurnMessage(record.ts, "user", record.text[:CONTENT_LIMIT]))
        elif record.client_message_id not in answered and _is_final(record, root):
            answered.add(record.client_message_id)
            messages.append(TurnMessage(record.ts, "assistant", record.text[:CONTENT_LIMIT]))
    return messages


async def chat_turn_messages(fetch: HistoryFetch, roots: Mapping[str, str]) -> list[TurnMessage]:
    """Сообщения ходов `roots` из одного чата: страницы от новых к старым, пока не найдены реплики всех
    этих ходов, не набралось сообщений или история не кончилась."""
    pages: list[tuple[HistoryRecord, ...]] = []
    messages: list[TurnMessage] = []
    before_ts = ""
    for _ in range(SCAN_PAGES):
        page = await fetch(before_ts)
        pages.append(page.records)
        ordered = [r for older in reversed(pages) for r in older]
        messages = turn_messages(ordered, roots)
        asked = {r.client_message_id for r in ordered if r.role == "user"}
        if len(messages) >= MESSAGE_LIMIT or roots.keys() <= asked or not page.next_before_ts:
            break
        before_ts = page.next_before_ts
    return messages


async def session_messages(chats: Sequence[tuple[HistoryFetch, Mapping[str, str]]]) -> list[dict[str, str]]:
    """История сессии в формате экрана `{role, content}`: ходы из всех её чатов, по времени, последние
    `MESSAGE_LIMIT`. `chats` — страницы чата и ходы сессии в нём (`run_id` → корень)."""
    found: list[TurnMessage] = []
    for fetch, roots in chats:
        found += await chat_turn_messages(fetch, roots)
    found.sort(key=lambda message: message.ts)
    return [{"role": m.role, "content": m.content} for m in found[-MESSAGE_LIMIT:]]
