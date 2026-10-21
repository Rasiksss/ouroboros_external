"""Подзадача автора в тестах цикла (ADR-0054): откуда у сессии берётся атом с кодом.

В продукте атом с кодом в `new_atoms` отклоняется, а код сдаёт подзадача ouroboros через `LoopSession.register_atom`
(`aifa_register_atom`). Тесты правил цикла (тупики, лимиты, файл, события хода) раньше получали такой атом от автора
службы внутри хода; `delegate` подставляет вместо отказа «подзадачу», которая в тот же момент сдаёт скриптовый код
тем же входом `register_atom`, — путь приёмки настоящий, подставлен только тот, кто сдаёт.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

import pytest

from finengine.skills.spec.atom_intake import REGISTRATION_FAILURES, Refused, Registered, StationDown
from finengine.skills.spec.dynamic_run import LoopSession
from finengine.skills.spec.ephemeral import AtomRequest, EphemeralAtom


class Writer(Protocol):
    """Кто сдаёт код: ответ `(атом, None)` — принят, `(None, причина)` — не вышло, `StationFault` — станция."""

    def write(self, request: AtomRequest, **kwargs: Any) -> tuple[EphemeralAtom | None, str | None]: ...


class StationFault(str):
    """Причина: станция не исполнила код, чинить нечего."""


@dataclass
class SubtaskCodes:
    """Подзадача, которая по очереди сдаёт скриптовый код через `register_atom`: до `REGISTRATION_FAILURES` попыток."""

    codes: list[str]
    session: LoopSession | None = None
    submitted: list[str] = field(default_factory=list)

    def write(self, request: AtomRequest, **_: Any) -> tuple[EphemeralAtom | None, str | None]:
        assert self.session is not None, "delegate(session, subtask) не вызван"
        reason = None
        for _attempt in range(REGISTRATION_FAILURES):
            if not self.codes:
                break
            code = self.codes.pop(0)
            self.submitted.append(code)
            outcome = self.session.register_atom(request, code)
            if isinstance(outcome, Registered):
                return outcome.atom, None
            if isinstance(outcome, StationDown):
                return None, StationFault(outcome.why)
            assert isinstance(outcome, Refused)
            reason = outcome.why
            if outcome.code != "неверные_аргументы":
                break
        return None, reason


def _ask(session: LoopSession, subtask: Writer, product, request: AtomRequest) -> str | None:
    """Вместо отказа `_ensure_atom` просит атом с кодом у `subtask`. Загрузки и уже принятые атомы — как в продукте."""
    if isinstance(subtask, SubtaskCodes):
        subtask.session = session
    known = session.atoms.get(request.name)
    if request.is_download or (known and known.request == request and request.name not in session.failures):
        return product(session, request)
    refused = session.intake.admission(request)
    if refused is not None:
        return refused.why
    real = session._real_inputs(request)
    if real is None:
        return f"входов атома {request.name} в Case ещё нет — получи их сценарием или шагом"
    atom, error = subtask.write(
        request, real_inputs=real, previous_code=known.code if known else None,
        error=session.failures.get(request.name),
    )
    if isinstance(error, StationFault):
        return f"комната станции недоступна, код не проверить: {error}"
    if atom is None:
        return f"подзадача не сдала рабочий код: {error}"
    session.intake.register(atom)
    return None


def delegate(session: LoopSession, subtask: Writer) -> LoopSession:
    """Одна сессия просит атом с кодом у `subtask` вместо отказа."""
    product = LoopSession._ensure_atom
    session._ensure_atom = lambda request: _ask(session, subtask, product, request)  # type: ignore[method-assign]
    return session


def patch_subtask(monkeypatch: pytest.MonkeyPatch, subtask: Writer) -> None:
    """Все сессии теста (цикл создаёт сам продукт: `production_runtime`) просят атом с кодом у `subtask`."""
    product = LoopSession._ensure_atom
    monkeypatch.setattr(
        LoopSession, "_ensure_atom", lambda session, request: _ask(session, subtask, product, request)
    )
