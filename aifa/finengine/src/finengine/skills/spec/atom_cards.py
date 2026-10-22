"""Карточки атомов для агента: что атом делает, что берёт и даёт, чего не умеет, и его исходный код (ADR-0064).

Отдельного каталога нет: карточка собирается из записи реестра (`AtomDef`) и из временных атомов `Case`. Технические поля —
версия, входы, выходы, гейты, параметры шага — берутся из определений; назначение (`ATOM_DESCRIPTIONS`), форматы, пояснения
к параметрам и ограничения (`ATOM_NOTES`) лежат рядом с ними в `atom_catalog.py` и проверяются тем же стражем.
"""
from __future__ import annotations

import ast
import functools
import inspect
import json
import textwrap
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from types import FunctionType

from finengine.skills.spec.atom_catalog import (
    ATOM_DESCRIPTIONS,
    ATOM_NOTES,
    NOT_PLANNABLE,
    plannable_atoms,
    produces_summary,
    requires_summary,
)
from finengine.skills.spec.atom_registry import AtomDef, AtomRegistry
from finengine.skills.spec.dynamic_run import ALLOWED_EXTRA_KEYS
from finengine.skills.spec.ephemeral import EphemeralAtom

BUILT_IN = "поставляемый"
TEMPORARY = "временный, этот Case"
NOT_STATED = "не указаны"
# Поля шага, которые задаёт агент; остальные поля идентичности узла (сущность, версия документа…) служба ставит сама.
_STEP_FIELDS = ("concept", "period_ref")
_ROOM_LIMITS = (
    "Код исполняется функцией run(inputs) в комнате без сети и окружения; видит только результаты входов из Case, "
    "не текст отчёта. Результат — по output_schema, у чисел есть x-unit."
)


@dataclass(frozen=True)
class AtomCard:
    """Карточка: поля одной строки каталога и то, что нужно для `get`."""

    name: str
    version: str
    scope: str
    purpose: str
    execution: str
    inputs: str
    outputs: str
    formats: tuple[str, ...]
    params: str
    limits: str
    gates: tuple[str, ...] = ()

    @property
    def ref(self) -> str:
        return f"{self.name}@{self.version}"

    def text(self) -> str:
        """Карточка в полях «Имя: …», как в постановке ADR-0064."""
        lines = [
            f"Имя: {self.name}",
            f"Версия: {self.version}",
            f"Область: {self.scope}",
            f"Назначение: {self.purpose}",
            f"Исполнение: {self.execution}",
            f"Входы: {self.inputs}",
            f"Выходы: {self.outputs}",
        ]
        if self.formats:
            lines.append(f"Форматы: {', '.join(self.formats)}")
        lines.append(f"Параметры: {self.params}")
        if self.gates:
            lines.append(f"Гейты (автоматически): {', '.join(self.gates)}")
        lines.append(f"Ограничения: {self.limits}")
        return "\n".join(lines)


@dataclass(frozen=True)
class AtomSource:
    """Исходный код атома и то, на что он опирается, не будучи приведённым."""

    lines: tuple[str, ...]
    helpers: tuple[str, ...]
    note: str = ""

    def helpers_text(self) -> str:
        if self.note:
            return self.note
        if not self.helpers:
            return (
                "Ссылок на вспомогательные функции службы в коде не найдено; методы объектов и вызовы, "
                "собранные во время работы, статически не определяются — фрагмент может быть неполным."
            )
        return (
            "Вспомогательные функции (код не приведён; фрагмент — не вся реализация): " + ", ".join(self.helpers) + "."
        )


def cards(registry: AtomRegistry, case_atoms: Mapping[str, EphemeralAtom]) -> list[AtomCard]:
    """Карточки поставляемых атомов, которые агент может планировать, затем временных атомов этого `Case`."""
    built_in = [_card_of(atom) for atom in plannable_atoms(registry) if not atom.name.startswith("eph.")]
    return [*built_in, *(_card_of_temporary(atom) for atom in case_atoms.values())]


def source_of(registry: AtomRegistry, case_atoms: Mapping[str, EphemeralAtom], name: str) -> AtomSource | None:
    """Исходный код атома по имени; `None` — такого атома в каталоге нет."""
    by_name = {atom.request.name: atom for atom in case_atoms.values()}
    if name in by_name:
        return _temporary_source(by_name[name])
    atom = registry.get(name)
    if atom is None or atom.name in NOT_PLANNABLE:
        return None
    return _built_in_source(atom)


def contract_of(atom: EphemeralAtom) -> str:
    """Контракт временного атома целиком: входы с концептами и схема результата."""
    request = atom.request
    requires = [{"input_type": spec.input_type, "concept": spec.concept} for spec in request.requires]
    # Схема — многострочным JSON: длинный контракт читается частями по строкам, а не режется.
    parts = [f"requires: {json.dumps(requires, ensure_ascii=False)}", f"output_type: {request.output_type}"]
    parts.append("output_schema: " + json.dumps(request.output_schema, ensure_ascii=False, indent=1))
    if request.is_download:
        parts.append(f"url: {request.url}")
    return "\n".join(parts)


def _card_of(atom: AtomDef) -> AtomCard:
    notes = ATOM_NOTES.get(atom.name)
    return AtomCard(
        name=atom.name, version=atom.version, scope=BUILT_IN,
        purpose=ATOM_DESCRIPTIONS.get(atom.name, "—"),
        execution=f"{atom.execution_kind}, {atom.cost_class}",
        inputs=requires_summary(atom), outputs=produces_summary(atom),
        formats=notes.formats if notes else (),
        params=_params_of(atom, notes.params if notes else ""),
        limits=notes.limits if notes and notes.limits else NOT_STATED,
        gates=atom.mandatory_gates,
    )


def _params_of(atom: AtomDef, note: str) -> str:
    """Поля шага, которые влияют на атом (из `key_fields`), и пояснение к ним."""
    named = [field for field in _STEP_FIELDS if field in atom.key_fields]
    named += [f"extra.{field}" for field in atom.key_fields if field in ALLOWED_EXTRA_KEYS]
    technical = ", ".join(named) or "—"
    return f"{technical}. {note}" if note else technical


def _card_of_temporary(atom: EphemeralAtom) -> AtomCard:
    request = atom.request
    requires = ", ".join(f"{spec.input_type}[{spec.concept or '*'}]" for spec in request.requires) or "—"
    limits = (
        f"Скачивает один адрес {request.url} (публичный); результат — url, content_type, text; разбора нет."
        if request.is_download else _ROOM_LIMITS
    )
    return AtomCard(
        name=request.name, version=atom.version, scope=TEMPORARY, purpose=request.purpose,
        execution="io, cheap" if request.is_download else "generated, cheap",
        inputs=requires, outputs=request.output_type, formats=(),
        params="нет: входы берутся из Case по requires", limits=limits,
    )


def _temporary_source(atom: EphemeralAtom) -> AtomSource:
    if atom.request.is_download:
        return AtomSource((), (), note="Кода у атома-загрузки нет: он скачивает адрес из контракта.")
    return AtomSource(
        tuple(atom.code.splitlines()), (),
        note="Вспомогательных функций службы нет: код самодостаточен, библиотеки — те, что есть в комнате.",
    )


def _built_in_source(atom: AtomDef) -> AtomSource:
    function = _function_of(atom.executor)
    if function is None:
        return AtomSource((), (), note="Исходный код недоступен: исполнитель не функция Python.")
    try:
        text = textwrap.dedent(inspect.getsource(function))
    except (OSError, TypeError):
        return AtomSource((), (), note="Исходный код недоступен: исходники службы не найдены.")
    try:
        helpers = tuple(sorted(_helpers_of(function, text)))
    except (SyntaxError, ValueError, TypeError):
        # Лямбда или выражение внутри вызова: фрагмент не разбирается, ссылок назвать нельзя.
        return AtomSource(
            tuple(text.splitlines()), (),
            note="Вспомогательные функции не определены (фрагмент не разбирается как код): он может быть неполным.",
        )
    return AtomSource(tuple(text.splitlines()), helpers)


def _function_of(executor: Callable) -> FunctionType | None:
    while True:
        if isinstance(executor, functools.partial):
            executor = executor.func
        elif inspect.ismethod(executor):
            executor = executor.__func__
        else:
            break
    return executor if isinstance(executor, FunctionType) else None


def _parsed(source: str) -> ast.AST:
    """Исходник функции как дерево; отступ первой строки (вложенная функция) не мешает."""
    try:
        return ast.parse(source)
    except SyntaxError:
        return ast.parse("if 1:\n" + source)


def _helpers_of(function: FunctionType, source: str) -> set[str]:
    """Функции службы, которые код вызывает или передаёт, в том числе из вложенных функций: прямые зависимости."""
    scope = {**function.__globals__, **inspect.getclosurevars(function).nonlocals}
    found: set[str] = set()
    for node in ast.walk(_parsed(source)):
        if isinstance(node, ast.Name):
            found.add(_reference(scope.get(node.id), function))
        elif isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
            module = scope.get(node.value.id)
            if inspect.ismodule(module):
                found.add(_reference(getattr(module, node.attr, None), function))
    found.discard("")
    return found


def _reference(target: object, own: FunctionType) -> str:
    """`модуль.имя` функции службы; пусто — это не она (константа, класс, чужая библиотека, сам исполнитель)."""
    if not isinstance(target, FunctionType) or target is own:
        return ""
    if not target.__module__.startswith("finengine."):
        return ""
    return f"{target.__module__}.{target.__qualname__}"


__all__ = [
    "AtomCard", "AtomSource", "BUILT_IN", "NOT_STATED", "TEMPORARY", "cards", "contract_of", "source_of",
]
