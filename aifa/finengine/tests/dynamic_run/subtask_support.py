"""Подзадача автора в тестах цикла (ADR-0054): код сдаётся настоящим `register_atom` между ходами.

В продукте атом с кодом в `new_atoms` отклоняется, а код сдаёт подзадача ouroboros, пока планировщик ждёт её итог:
вызов `aifa_register_atom` приходит между двумя ходами цикла. Тесты правил цикла (тупики, лимиты, файл, события хода)
ведут тот же порядок: `Visit` — «подзадача сдаёт эти коды перед N-м вопросом к планировщику». Подставлен только тот,
кто сдаёт; приёмка (`LoopSession.register_atom` → `AtomIntake.submit`), `_ensure_atom` и сам цикл — продуктовые.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from functools import partial, partialmethod

import pytest

from finengine.skills.spec.atom_intake import Refused, Registered, StationDown
from finengine.skills.spec.dynamic_run import DynamicRun, LoopSession, parse_atom_request
from finengine.skills.spec.ephemeral import AtomRequest

Outcome = Registered | Refused | StationDown


@dataclass
class Visit:
    """Подзадача сдаёт `codes` атома `request` по порядку перед `before`-м вопросом к планировщику (1 — первый).

    `outcomes` — ответы приёмки на каждую сдачу: по ним тест видит то, что увидела бы подзадача."""

    request: AtomRequest
    codes: tuple[str, ...]
    before: int = 2
    outcomes: list[Outcome] = field(default_factory=list)

    @classmethod
    def of(cls, doc: dict, *codes: str, before: int = 2) -> Visit:
        """Контракт атома — словарём, как его пишет планировщик в решении (`name`, `purpose`, `requires`, …)."""
        return cls(parse_atom_request(doc, "задание подзадаче"), codes, before)

    def submit(self, session: LoopSession) -> None:
        for code in self.codes:
            self.outcomes.append(session.register_atom(self.request, code))

    @property
    def kinds(self) -> list[str]:
        """Что ответила приёмка: `принят`, `станция` или код отказа."""
        return [
            "принят" if isinstance(item, Registered) else "станция" if isinstance(item, StationDown) else item.code
            for item in self.outcomes
        ]


def _ask_after_visits(
    session: DynamicRun, visits: tuple[Visit, ...], original: Callable, asked: dict[int, int],
    feedback: str, *, last_word: bool = False,
):
    """Вопрос к планировщику, перед которым подзадача успевает сдать код."""
    asked[id(session)] = asked.get(id(session), 0) + 1
    for visit in visits:
        if visit.before == asked[id(session)]:
            visit.submit(session)
    return original(session, feedback, last_word=last_word)


def delegate(session: DynamicRun, *visits: Visit) -> DynamicRun:
    """Подзадача этой сессии сдаёт код между ходами; сам вопрос к планировщику вызывается как есть."""
    session._ask_planner = partial(  # type: ignore[method-assign]
        _ask_after_visits, session, visits, type(session)._ask_planner, {},
    )
    return session


def patch_subtask(monkeypatch: pytest.MonkeyPatch, *visits: Visit) -> None:
    """То же для всех сессий теста: цикл создаёт сам продукт (`production_runtime`)."""
    if not visits:
        return
    monkeypatch.setattr(
        DynamicRun, "_ask_planner", partialmethod(_ask_after_visits, visits, DynamicRun._ask_planner, {}),
    )
