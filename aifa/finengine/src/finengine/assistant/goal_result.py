"""Итог стандартного пути для `aifa_goal`: доменное значение, текст из него строит только `ouro_view`."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from finengine.skills.spec.dynamic_run import LoopSession


class Verdict(StrEnum):
    ANSWERED = "answered"  # результаты в Case или итог «в отчёте нет» — модель отвечает по ним
    CLARIFY = "clarify"  # цели не хватает данных; в `note` — вопрос пользователю
    NO_CALCULATION = "no_calculation"  # маршрутизатор решил: расчёт по отчёту не нужен
    NO_DOCUMENT = "no_document"  # у хода нет текста документа, по которому идти
    NEEDS_LOOP = "needs_loop"  # стандартный путь не справился: цикл открыт, ходы — вызовами инструментов


@dataclass(frozen=True)
class GoalResult:
    """`rows` — артефакты Case (`case_rows`); `note` — итог или вопрос, как их назвал конвейер;
    `loop` — открытая сессия цикла (только у `NEEDS_LOOP`)."""

    verdict: Verdict
    note: str = ""
    rows: tuple[dict[str, Any], ...] = ()
    loop: LoopSession | None = None
