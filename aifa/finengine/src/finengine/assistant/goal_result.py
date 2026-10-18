"""Итог стандартного пути для `aifa_goal`: доменное значение, текст из него строит только `ouro_view`."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class Verdict(StrEnum):
    ANSWERED = "answered"  # результаты в Case или итог «в отчёте нет» — модель отвечает по ним
    CLARIFY = "clarify"  # цели не хватает данных; в `note` — вопрос пользователю
    NO_CALCULATION = "no_calculation"  # маршрутизатор решил: расчёт по отчёту не нужен
    NO_DOCUMENT = "no_document"  # у хода нет текста документа, по которому идти


@dataclass(frozen=True)
class GoalResult:
    """`rows` — артефакты Case (`case_rows`); `note` — итог или вопрос, как их назвал конвейер."""

    verdict: Verdict
    note: str = ""
    rows: tuple[dict, ...] = ()
