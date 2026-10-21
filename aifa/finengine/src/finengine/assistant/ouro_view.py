"""Единственный формирователь ответов инструментов `aifa-engine` (CONTRACTS.md §3.0, C3.2, ADR-0040).

Первая строка ответа — код; дальше — текст для модели ouroboros. Ни один ответ инструмента не
собирается в другом месте: `tool_api` отдаёт доменные значения, текст из них строится здесь.
"""

from __future__ import annotations

import json
from enum import StrEnum

from finengine.assistant.doc_pages import Match, Page
from finengine.assistant.goal_result import GoalResult, Verdict
from finengine.skills.spec.dynamic_run import (
    ARTIFACTS_HEADER,
    ARTIFACTS_TITLE,
    DynamicRunResult,
    LoopSession,
    MoveOutcome,
    artifacts_table,
    case_rows,
    state_sections,
)

# Подсказка про остаток результатов: сколько id просить за один вызов `aifa_state` (§3.1).
STATE_BATCH = 20
# Запас под строку об остатке: самый длинный вид — 20 id по 36 знаков и пояснение.
TAIL_RESERVE = 1_000
# Разделы состояния, которые уступают место таблице, когда состояние не помещается в предел (по порядку).
_SHRINKABLE = ("Реестр показателей", "Каталог атомов")
_SHRINK_FLOOR = 300
_REGISTER_PREVIEW = 400


class Code(StrEnum):
    READY = "ГОТОВО"
    WAIT = "ЖДИ"
    NEED_LOOP = "НУЖЕН_ЦИКЛ"
    CLARIFY = "УТОЧНИ"
    LIMIT = "ЛИМИТ"
    ACCEPTED = "ПРИНЯТО"
    REFUSE = "ОТКАЗ"
    SERVICE_FAULT = "СБОЙ_СЛУЖБЫ"


class Refusal(StrEnum):
    """Коды отказа §3.0. Закрытый список: новый код — новая ADR."""

    FOREIGN_RUN = "чужой_ход"
    NOT_THE_ROLE = "не_та_роль"
    NO_LOOP = "нет_цикла"
    LOOP_CLOSED = "цикл_закрыт"
    BAD_ARGUMENTS = "неверные_аргументы"
    REPEATED_DEAD_END = "повтор_тупика"
    NO_INPUTS = "нет_входов"
    NOT_DYN_TYPE = "не_dyn_тип"
    NO_UNIT = "нет_единицы"
    NO_DOCUMENT = "нет_документа"
    FILE_NOT_BUILT = "файл_не_собран"
    ATTEMPTS_LIMIT = "лимит_попыток"
    ATOMS_LIMIT = "лимит_атомов"
    REPORTS_LIMIT = "лимит_отчётов"
    JOB_NOT_FOUND = "задание_не_найдено"
    SUBTASK_SILENT = "подзадача_не_ответила"


def refusal(code: Refusal, why: str) -> str:
    return f"{Code.REFUSE} {code}\n{why}"


def waiting(job_id: str) -> str:
    return (
        f"{Code.WAIT} job={job_id}\n"
        "Задание идёт фоном. Вызови aifa_wait с этим job_id (wait_sec от 10 до 240) и жди итог."
    )


_SUBTASK_HINT = "Состояние подзадачи — get_task_result, снять её — cancel_task."


def waiting_subtask(task_id: str) -> str:
    return (
        f"{Code.WAIT} task={task_id}\n"
        "Подзадача ещё работает. Вызови aifa_wait с этим task_id (wait_sec от 10 до 240) и жди итог. " + _SUBTASK_HINT
    )


def subtask_silent(task_id: str, seconds: float) -> str:
    return refusal(
        Refusal.SUBTASK_SILENT,
        f"Подзадача {task_id} не ответила за {seconds:g} с. {_SUBTASK_HINT} Продолжай план без её итога.",
    )


def subtask_result(task_id: str, text: str, *, failed: bool, limit: int) -> str:
    """Итог подзадачи: её последнее сообщение или `note` её `aifa_report done`; упавшая — с пометкой."""
    title = f"Итог подзадачи {task_id}: подзадача упала" if failed else f"Итог подзадачи {task_id}"
    body = text if len(text) <= limit - TAIL_RESERVE else text[: limit - TAIL_RESERVE] + "…[итог обрезан]"
    return f"{Code.READY}\n## {title}\n{body}"


def registered(atom_name: str, version: str, output: dict | None) -> str:
    """Ответ `aifa_register_atom`: имя с версией и краткий итог проверочного прогона."""
    shown = json.dumps(output, ensure_ascii=False)[:_REGISTER_PREVIEW] if output is not None else "—"
    return (
        f"{Code.READY}\nАтом {atom_name}@{version} зарегистрирован.\n"
        f"Итог проверочного прогона на входах Case: {shown}\nСлужба исполнит его шагом aifa_move."
    )


def service_fault(why: str) -> str:
    return f"{Code.SERVICE_FAULT}: {why}; повтори вызов"


def goal(result: GoalResult, *, limit: int) -> str:
    """Ответ `aifa_goal` и итог его задания (`aifa_wait`)."""
    if result.verdict is Verdict.NEEDS_LOOP and result.loop is not None:
        return f"{Code.NEED_LOOP}\n{_state(result.loop, limit - len(Code.NEED_LOOP) - 1)}"
    if result.verdict is Verdict.CLARIFY:
        return f"{Code.CLARIFY}\n{result.note}"
    if result.verdict is Verdict.NO_DOCUMENT:
        return refusal(Refusal.NO_DOCUMENT, result.note)
    if result.verdict is Verdict.NO_CALCULATION:
        return f"{Code.READY}\nРасчёт по отчёту не нужен: ответь пользователю сам.\n{result.note}".rstrip()
    return _answered(result, limit)


def _answered(result: GoalResult, limit: int) -> str:
    note = result.note or "Стандартный путь отработал."
    if len(note) > limit // 2:
        # Предел ответа жёсткий (§3.0); итог не должен вытеснить таблицу целиком.
        note = note[: limit // 2] + "…[итог обрезан]"
    head = f"{Code.READY}\n## Итог\n{note}\n\n"
    head += f"## {ARTIFACTS_TITLE}\n{ARTIFACTS_HEADER}\n"
    lines = artifacts_table(list(result.rows)).split("\n")
    whole = head + "\n".join(lines)
    if len(whole) <= limit:
        return whole
    shown: list[str] = []
    size = len(head)
    for line in lines:
        if size + len(line) + 1 > limit - TAIL_RESERVE:
            break
        shown.append(line)
        size += len(line) + 1
    return head + "\n".join(shown) + ("\n" if shown else "") + _tail(lines, len(shown))


def _tail(lines: list[str], shown: int) -> str:
    ids = [line.split(" | ", 1)[0] for line in lines[shown:]]
    ids = [item for item in ids if item != "—"][:STATE_BATCH]
    return (
        f"Показано {shown} из {len(lines)} результатов. Остальные — aifa_state с artifact_ids "
        f"(не больше {STATE_BATCH} за вызов), начиная с: {', '.join(ids)}"
    )


def state(session: LoopSession, *, limit: int) -> str:
    """Ответ `aifa_state` без артефактов: состояние открытого цикла (§3.1)."""
    return f"{Code.READY}\n{_state(session, limit - len(Code.READY) - 1)}"


def _state(session: LoopSession, limit: int) -> str:
    """Состояние цикла, как его видит модель службы; не помещается — таблица артефактов даёт место, затем
    уступают разделы справочников, остаток таблицы читается `aifa_state(artifact_ids)`."""
    pairs = state_sections(session, session.feedback)
    order = [title for title, _ in pairs]
    sections = {title: body for title, body in pairs if title != ARTIFACTS_TITLE}
    over = sum(len(title) + len(body) + 6 for title, body in sections.items()) + TAIL_RESERVE + 300 - limit
    for title in _SHRINKABLE:
        if over <= 0:
            break
        body = sections[title]
        cut = min(over, max(0, len(body) - _SHRINK_FLOOR))
        sections[title] = body[: len(body) - cut] + "…[обрезано]" if cut else body
        over -= cut
    fixed = sum(len(title) + len(body) + 6 for title, body in sections.items())
    lines = artifacts_table(case_rows(session.runtime.store, session.owner_id, session.case_id)).split("\n")
    room = limit - fixed - len(ARTIFACTS_HEADER) - len(ARTIFACTS_TITLE) - 8 - TAIL_RESERVE
    shown: list[str] = []
    size = 0
    for line in lines:
        if size + len(line) + 1 > room:
            break
        shown.append(line)
        size += len(line) + 1
    table = "\n".join(shown) if len(shown) == len(lines) else "\n".join([*shown, _tail(lines, len(shown))])
    sections[ARTIFACTS_TITLE] = f"{ARTIFACTS_HEADER}\n{table}"
    return "\n\n".join(f"## {title}\n{sections[title]}" for title in order)


def artifacts(found: list[tuple[str, str]], missing: list[str], *, limit: int) -> str:
    """Ответ `aifa_state(artifact_ids)`: полное содержимое найденных артефактов, `found` — пары (id, текст)."""
    head = f"{Code.READY}\n## Артефакты\n"
    parts: list[str] = []
    size = len(head)
    for index, (artifact_id, content) in enumerate(found):
        part = f"### {artifact_id}\n{content}\n"
        room = limit - size - TAIL_RESERVE
        if len(part) > room:
            if not parts:
                part = part[: max(room, 0)] + f"…[обрезан: всего {len(content)} знаков]\n"
            else:
                rest = [artifact_id for artifact_id, _ in found[index:]][:STATE_BATCH]
                parts.append(f"Показано {index} из {len(found)}. Остальные — aifa_state с artifact_ids: {', '.join(rest)}\n")
                break
        parts.append(part)
        size += len(part)
    text = head + "".join(parts)
    if missing:
        text += f"Не найдены в Case этого хода: {', '.join(missing)}\n"
    return text.rstrip("\n")


def shown_ids(session: LoopSession) -> frozenset[str]:
    """id артефактов, которые сейчас видны в таблице состояния: по ним `moved` отделяет новое."""
    rows = case_rows(session.runtime.store, session.owner_id, session.case_id)
    return frozenset(line.split(" | ", 1)[0] for line in artifacts_table(rows).split("\n"))


def moved(
    session: LoopSession, outcome: MoveOutcome, known: frozenset[str], *, limit: int, exhausted: str | None = None,
) -> str:
    """Ответ `aifa_move`: итог хода, новые артефакты со значениями, остаток лимитов (§3.1).
    Ход исчерпал предел цикла (`exhausted`) — ответ начинается с `ЛИМИТ`."""
    rows = case_rows(session.runtime.store, session.owner_id, session.case_id)
    fresh = [line for line in artifacts_table(rows).split("\n") if line.split(" | ", 1)[0] not in known | {"—"}]
    left = dict(state_sections(session, session.feedback))["Осталось"]
    code = Code.LIMIT if exhausted else Code.READY
    parts = [f"{code}\n## Итог хода {session.turns}\n{outcome.headline}\n{outcome.details}".rstrip()]
    if exhausted:
        parts.append(f"Цикл исчерпан: {exhausted}. Новые ходы нельзя — принимается только aifa_finish.")
    parts.append(f"## Новые артефакты\n{ARTIFACTS_HEADER}\n" + ("\n".join(fresh) if fresh else "—"))
    parts.append(f"## Осталось\n{left}")
    text = "\n\n".join(parts)
    if len(text) > limit:
        text = text[: limit - TAIL_RESERVE // 2] + "\n…[ответ обрезан: полное — aifa_state]\n\n" + f"## Осталось\n{left}"
    return text


def limited(reason: str) -> str:
    return f"{Code.LIMIT}\nЦикл исчерпан: {reason}. Исполнять новые ходы нельзя — принимается только aifa_finish."


def accepted(result: DynamicRunResult, *, reason: str = "", unfinished: list[str] | None = None) -> str:
    """Ответ `aifa_finish`: цикл закрыт; текст ответа пользователю пишет ouroboros (ADR-0044 п. 2).

    `unfinished` — подзадачи хода, которых служба не видела завершёнными (ADR-0043 п. 8): их снимает `cancel_task`."""
    lines = [f"{Code.ACCEPTED}", "Цикл закрыт."]
    if unfinished:
        lines.append(f"Подзадачи хода, которые ещё не закончили: {', '.join(unfinished)} — сними их cancel_task.")
    if result.deliverables:
        lines.append("Файлы опубликованы: " + ", ".join(file.filename for file in result.deliverables) + ".")
    if reason:
        lines.append(f"Твой итог не принят: {reason}. Ответь пользователю по полученному:\n{result.answer}")
    return "\n".join(lines)


def documents(listed: list[tuple[str, str, int]]) -> str:
    """Ответ `aifa_doc` без аргументов: документы хода (id, имя, число страниц)."""
    lines = [f"{doc_id} | {name} | страниц: {pages}" for doc_id, name, pages in listed]
    return f"{Code.READY}\n## Документы хода\ndoc_id | имя | страницы\n" + ("\n".join(lines) or "—")


def doc_pages(doc_id: str, shown: list[Page], wanted: str, *, limit: int) -> str:
    """Ответ `aifa_doc(doc_id, pages)`: текст страниц частями до предела, с просьбой про следующую."""
    head = f"{Code.READY}\n## {doc_id}, страницы {wanted}\n"
    parts: list[str] = []
    size = len(head)
    for index, page in enumerate(shown):
        part = f"===PAGE {page.number}===\n{page.text.rstrip()}\n"
        room = limit - size - TAIL_RESERVE // 2
        if len(part) > room:
            if not parts:
                part = part[: max(room, 0)] + "\n…[страница обрезана: нужное найди через query]\n"
            else:
                rest = shown[index:]
                parts.append(
                    f"Показано страниц: {index} из {len(shown)}. Следующие: aifa_doc doc_id={doc_id} "
                    f"pages={rest[0].number}-{rest[-1].number}\n"
                )
                break
        parts.append(part)
        size += len(part)
    return (head + "".join(parts)).rstrip("\n")


def doc_matches(doc_id: str, query: str, matches: list[Match]) -> str:
    """Ответ `aifa_doc(doc_id, query)`: страница и фрагмент каждого найденного места."""
    if not matches:
        return f"{Code.READY}\n## {doc_id}, поиск «{query}»\nНичего не найдено."
    lines = [f"стр. {match.page}: …{match.fragment}…" for match in matches]
    return f"{Code.READY}\n## {doc_id}, поиск «{query}»\n" + "\n".join(lines)
