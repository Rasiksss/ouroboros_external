"""Приёмка временного атома: единственное место, где проверяют и регистрируют его (ADR-0041 п. 4, ADR-0043 п. 6).

Код атома сдают двое — автор службы (`atom_author.py`, до шага 17) и подзадача ouroboros (`aifa_register_atom`);
проверка у них одна: `check_code` (контракт — `check_request`, один прогон в комнате на входах `Case`, результат по
`output_schema`, файлы из `out/`). Попытки обоих нумеруются одним счётчиком на пару (ход цикла, имя), поэтому номера
на экране не сталкиваются (ADR-0044 п. 1).
"""
from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol

from finengine.skills.result_events import author_attempt_event
from finengine.skills.spec import sealed_room
from finengine.skills.spec.atom_registry import AtomRegistry
from finengine.skills.spec.ephemeral import AtomRequest, EphemeralAtom, check_request, schema_problem
from finengine.skills.spec.room_files import FileSpec, expected_files_problem, files_problem

# Неудачных регистраций одного имени за ход цикла (ADR-0043 п. 6): как три попытки автора на запись.
REGISTRATION_FAILURES = 3
_NO_ATOMS_LEFT = "исчерпан лимит новых атомов — используй существующие"

# Правило атома, из-за которого ход не исполнялся, — по тексту отказа; коды — закрытый список §3.0.
_REFUSAL_MARKS = (
    ("входов атома", "нет_входов"), ("лимит новых атомов", "лимит_атомов"),
    ("только dyn:*", "не_dyn_тип"), ("без x-unit", "нет_единицы"), ("контракт отклонён", "неверные_аргументы"),
)


def refusal_code(problem: str) -> str:
    return next((code for mark, code in _REFUSAL_MARKS if mark in problem), "")


@dataclass(frozen=True)
class CodeVerdict:
    """Итог одной проверки кода: `error` пуст — код годен; `station_fault` — чинить нечего, станция не исполнила."""

    error: str | None = None
    station_fault: bool = False
    output: dict | None = None            # результат прогона — для краткого ответа сдавшему


def check_code(
    request: AtomRequest, code: str, inputs: dict, *, expected_files: tuple[FileSpec, ...] = (), trace_id: str = "",
) -> CodeVerdict:
    """Один прогон кода в разовой комнате на входах и разбор результата. Файлы из `out/` проверяются тем же
    правилом, что в узле графа: повреждённый файл — ошибка автора здесь, а не лишний ход планировщика."""
    result = sealed_room.run_code(code, inputs, collect_files=True, trace_id=trace_id)
    if result.station_fault:
        return CodeVerdict(result.error, station_fault=True)
    error = (
        result.error or schema_problem(result.output, request.output_schema) or files_problem(result.files)
        or expected_files_problem(result.files, expected_files)
    )
    return CodeVerdict(error, output=result.output)


class AttemptLog:
    """Номера попыток и счёт неудачных регистраций на пару (ход цикла, имя). Потоки — рабочие потоки хода и
    цикл событий, поэтому увеличение под замком."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._numbers: dict[tuple[int, str], int] = {}
        self._failures: dict[tuple[int, str], int] = {}

    def number(self, move: int, name: str) -> int:
        with self._lock:
            self._numbers[(move, name)] = taken = self._numbers.get((move, name), 0) + 1
            return taken

    def failures(self, move: int, name: str) -> int:
        with self._lock:
            return self._failures.get((move, name), 0)

    def fail(self, move: int, name: str) -> int:
        with self._lock:
            self._failures[(move, name)] = failed = self._failures.get((move, name), 0) + 1
            return failed

    def to_doc(self) -> list[list[Any]]:
        with self._lock:
            return [
                [move, name, taken, self._failures.get((move, name), 0)]
                for (move, name), taken in self._numbers.items()
            ]

    def restore(self, doc: list[list[Any]]) -> None:
        with self._lock:
            self._numbers = {(move, name): taken for move, name, taken, _ in doc}
            self._failures = {(move, name): failed for move, name, _, failed in doc if failed}


@dataclass(frozen=True)
class Registered:
    atom: EphemeralAtom
    output: dict | None = None


@dataclass(frozen=True)
class Refused:
    """Бизнес-отказ приёмки: `code` — код отказа §3.0, `why` — причина для того, кто сдавал."""

    code: str
    why: str


@dataclass(frozen=True)
class StationDown:
    """Сбой станции: код не проверен, попытка не потрачена."""

    why: str


class AtomHost(Protocol):
    """Что приёмке нужно от цикла: его атомы, неудачи, тупики и реестр для проверки входов."""

    atoms: dict[str, EphemeralAtom]
    failures: dict[str, str]
    dead_end_actions: dict[tuple[str, ...], int]
    created: int
    expected_files: tuple[FileSpec, ...]

    @property
    def registry(self) -> AtomRegistry: ...

    @property
    def max_new_atoms(self) -> int: ...

    @property
    def trace_id(self) -> str: ...

    @property
    def last_started_move(self) -> int: ...


def inputs_missing(request: AtomRequest) -> str:
    needs = ", ".join(f"{spec.input_type}:{spec.concept or '*'}" for spec in request.requires)
    return f"входов атома {request.name} в Case ещё нет (нужны: {needs})"


class AtomIntake:
    def __init__(
        self, host: AtomHost, emit: Callable[[dict], None], keep_source: Callable[[EphemeralAtom], None],
    ) -> None:
        self._host = host
        self._emit = emit
        self._keep_source = keep_source
        self.log = AttemptLog()

    def admission(self, request: AtomRequest) -> Refused | None:
        """Контракт и лимит новых атомов: что можно решить, не трогая станцию."""
        if request.name not in self._host.atoms and self._host.created >= self._host.max_new_atoms:
            return Refused("лимит_атомов", _NO_ATOMS_LEFT)
        problems = check_request(request, self._host.registry)
        if problems:
            why = "контракт отклонён: " + "; ".join(problems)
            return Refused(refusal_code(why) or "неверные_аргументы", why)
        return None

    def report(self, name: str, status: str, error: str | None, code: str, *, move: int | None = None) -> int:
        """Попытка — в ленту хода (`author_attempt`); номер берёт счётчик, а не тот, кто сдаёт."""
        at = move if move is not None else self._host.last_started_move
        number = self.log.number(at, name)
        self._emit(author_attempt_event(move=at, atom=name, attempt=number, status=status, error=error, code=code))
        return number

    def submit(
        self, request: AtomRequest, code: str, real_inputs: Callable[[], dict | None],
    ) -> Registered | Refused | StationDown:
        """Сдача атома с кодом: контракт, пределы, один прогон в комнате, регистрация."""
        refused = self.admission(request)
        if refused is not None:
            return refused
        move = self._host.last_started_move
        if self.log.failures(move, request.name) >= REGISTRATION_FAILURES:
            return Refused(
                "лимит_попыток",
                f"{REGISTRATION_FAILURES} неудачных регистрации {request.name} за этот ход цикла: сообщи итог "
                "планировщику, на следующем ходе цикла счёт заново",
            )
        inputs = real_inputs()
        if inputs is None:
            return Refused("нет_входов", inputs_missing(request) + " — получи их ходом aifa_move и сдай атом снова")
        verdict = check_code(
            request, code, inputs, expected_files=self._host.expected_files, trace_id=self._host.trace_id,
        )
        if verdict.station_fault:
            self.report(request.name, "station_fault", verdict.error, code, move=move)
            return StationDown(verdict.error or "станция не исполнила код")
        if verdict.error is not None:
            self.report(request.name, "error", verdict.error, code, move=move)
            left = REGISTRATION_FAILURES - self.log.fail(move, request.name)
            return Refused("неверные_аргументы", f"{verdict.error}\nосталось попыток: {left}")
        self.report(request.name, "ok", None, code, move=move)
        atom = EphemeralAtom(request=request, code=code)
        self.register(atom)
        return Registered(atom, verdict.output)

    def register(self, atom: EphemeralAtom) -> None:
        """Атом принят: он в цикле, его исходник в `Case`, а тупики его шагов сняты — это уже другой атом."""
        name = atom.request.name
        self._host.failures.pop(name, None)
        if name not in self._host.atoms:
            self._host.created += 1
        self._host.atoms[name] = atom
        self._keep_source(atom)
        for signature in [item for item in self._host.dead_end_actions if item[:1] == ("step",) and item[1] == name]:
            del self._host.dead_end_actions[signature]
