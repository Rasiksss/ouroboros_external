"""Общие условия тестов гибридного планировщика (срез 34).

Комната станции на машине разработчика недоступна. Код атомов в тестах исполняет
фикстура `local_room`: она подменяет `sealed_room.run_code` запуском той же точки
входа `_room_entry.py` локальным интерпретатором. Это тестовая подмена, а не запасной
путь продукта: в самом `sealed_room` без станции есть только честная ошибка.
"""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from finengine.skills.spec import sealed_room
from tests.integration.conftest import GOLDEN_CLASSIFICATION

ENTRY_PATH = Path(sealed_room.__file__).with_name("_room_entry.py")


def run_entry(request: dict, *, cwd: str | None = None) -> subprocess.CompletedProcess:
    """Точка входа комнаты локальным интерпретатором: JSON в stdin, как в `exec` станции.

    `-X utf8` нужен только на Windows: на станции (Linux) UTF-8 уже стандарт, а `-I`
    отключает `PYTHONUTF8`, поэтому включаем режим ключом командной строки."""
    return subprocess.run(
        [sys.executable, "-X", "utf8", "-I", "-c", ENTRY_PATH.read_text(encoding="utf-8")],
        input=json.dumps(request, ensure_ascii=False).encode(),
        capture_output=True, timeout=30, check=False, cwd=cwd,
    )


def local_run_code(code: str, inputs: dict, *, timeout: int = sealed_room.TIMEOUT_SECONDS,
                   transport=None, collect_files: bool = False, trace_id: str = "") -> sealed_room.CodeRun:
    """Подмена `sealed_room.run_code`: та же точка входа, тот же разбор ответа. Рабочий каталог — временный,
    как `/workspace` комнаты: файлы из его `out/` отдаются при `collect_files` с теми же пределами."""
    with tempfile.TemporaryDirectory() as workdir:
        done = run_entry({"code": code, "inputs": inputs}, cwd=workdir)
        reply = {"exitCode": done.returncode, "stdout": done.stdout.decode("utf-8"),
                 "stderr": done.stderr.decode("utf-8"), "timedOut": False}
        run = sealed_room._read_reply(reply, timeout)
        if collect_files and run.error is None:
            run = _local_files(Path(workdir) / "out", run)
    return run


def _local_files(out: Path, run: sealed_room.CodeRun) -> sealed_room.CodeRun:
    if not out.is_dir():
        return run
    found = [{"name": path.name, "bytes": path.stat().st_size} for path in sorted(out.iterdir()) if path.is_file()]
    nodes = [node for node in found if not sealed_room._is_service_file(node["name"])]
    problem = sealed_room._name_problem(found) or sealed_room._limits_problem(nodes)
    if problem:
        return sealed_room.CodeRun(None, problem)
    files = []
    for node in nodes:
        content = (out / node["name"]).read_bytes()
        if not content:
            return sealed_room.CodeRun(None, f"файл out/{node['name']} пуст: код должен записать содержимое")
        files.append(sealed_room.RoomFile(node["name"], content))
    return sealed_room.CodeRun(run.output, None, files=tuple(files))


@pytest.fixture
def local_room(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sealed_room, "run_code", local_run_code)


@pytest.fixture(scope="module")
def severstal_text() -> str:
    """Реальный excerpt МСФО-отчёта (golden `cls-023`) — тот же, что в tests/integration."""
    cases = json.loads(GOLDEN_CLASSIFICATION.read_text(encoding="utf-8"))
    return next(case for case in cases if case["id"] == "cls-023")["excerpt"]


@dataclass
class CountingRoom:
    """`local_room` со счётчиком: `codes` — код каждого запуска, в порядке вызовов."""
    codes: list[str] = field(default_factory=list)

    def __call__(self, code: str, inputs: dict, **kwargs) -> sealed_room.CodeRun:
        self.codes.append(code)
        return local_run_code(code, inputs, **kwargs)


@pytest.fixture
def counting_room(monkeypatch: pytest.MonkeyPatch) -> CountingRoom:
    room = CountingRoom()
    monkeypatch.setattr(sealed_room, "run_code", room)
    return room


@pytest.fixture
def room_entry():
    """Запуск точки входа локально: вызов `room_entry(request) -> CompletedProcess`."""
    return run_entry


@pytest.fixture
def entry_text() -> str:
    return ENTRY_PATH.read_text(encoding="utf-8")


# Рантайм с Case и подставными locate/read.value определён в test_ephemeral_in_graph; отсюда он
# доступен всем файлам каталога (импорт фикстур в тестовый файл ruff считает переопределением).
from test_ephemeral_in_graph import base, read_value  # noqa: E402, F401
