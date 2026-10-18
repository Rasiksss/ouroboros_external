"""Единственный формирователь ответов инструментов `aifa-engine` (CONTRACTS.md §3.0, C3.2, ADR-0040).

Первая строка ответа — код; дальше — текст для модели ouroboros. Ни один ответ инструмента не
собирается в другом месте: `tool_api` отдаёт доменные значения, текст из них строится здесь.
"""

from __future__ import annotations

from enum import StrEnum

from finengine.assistant.goal_result import GoalResult, Verdict
from finengine.skills.spec.dynamic_run import artifacts_table

# Подсказка про остаток результатов: сколько id просить за один вызов `aifa_state` (§3.1).
STATE_BATCH = 20
# Запас под строку об остатке: самый длинный вид — 20 id по 36 знаков и пояснение.
TAIL_RESERVE = 1_000


class Code(StrEnum):
    READY = "ГОТОВО"
    WAIT = "ЖДИ"
    CLARIFY = "УТОЧНИ"
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


def service_fault(why: str) -> str:
    return f"{Code.SERVICE_FAULT}: {why}; повтори вызов"


def goal(result: GoalResult, *, limit: int) -> str:
    """Ответ `aifa_goal` и итог его задания (`aifa_wait`)."""
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
    head += "## Полученные артефакты\nid | тип | концепт | период | значение\n"
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
