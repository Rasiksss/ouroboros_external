"""Станционные скрипты обязаны ЗАПУСКАТЬСЯ, а не только компилироваться.

ЗАЧЕМ ЭТОТ ТЕСТ. 31.07.2026 хелпер `_scope_with_calculated` был дописан в КОНЕЦ
файла — после `if __name__ == "__main__": main()`. Модуль исполняется сверху
вниз, поэтому `main()` отрабатывал РАНЬШЕ определения хелпера, и станционный
анализ падал с NameError на каждом прогоне. Правка прошла и ruff, и py_compile:
синтаксис-то верный. Не поймали, потому что ни разу не ЗАПУСТИЛИ скрипт.

ЧТО ЛОВИТ ЧТО. Проверено на нарочно сломанной копии:
- `--help` этот дефект НЕ ловит: argparse завершает процесс раньше, чем дело
  доходит до вызова хелпера. Оставлен как отдельная страховка от падения на
  импорте — но полагаться на него в этом классе ошибок нельзя;
- статическая проверка «ничего не определено ниже точки входа» ловит его прямо:
  на сломанной копии она называет `_scope_with_calculated`, на исправленной
  молчит.

ЧЕГО ЭТОТ ФАЙЛ НЕ ЛОВИТ (ревизия 21.08.2026). Статическая половина смотрит
только на `def` в НУЛЕВОЙ КОЛОНКЕ ниже точки входа: имя, использованное до
определения внутри функции, класса или через отложенный вызов, проходит мимо.
А `--help` до настоящей работы скрипта не доходит вовсе — ни один дефект
самого извлечения, расчёта или анализа здесь не виден. Оставлен как «О»:
предмет охраны — ПОРЯДОК определений в файле, и исполнить его нечем, потому
что argparse завершает процесс раньше.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SCRIPTS = [
    REPO / "station-skills" / "aifa-analyze" / "scripts" / "analyze.py",
    REPO / "station-skills" / "aifa-extract" / "scripts" / "extract.py",
    REPO / "station-skills" / "aifa-calc" / "scripts" / "calc_run.py",
    REPO / "station-skills" / "aifa-metric" / "scripts" / "metric_run.py",
]


@pytest.mark.parametrize("script", SCRIPTS, ids=lambda p: p.name)
def test_station_script_runs(script: Path):
    """Скрипт доходит до argparse — значит модуль исполнился целиком."""
    assert script.is_file(), f"нет файла {script}"
    res = subprocess.run(
        [sys.executable, str(script), "--help"],
        capture_output=True, text=True, timeout=120,
        env={**os.environ, "PYTHONPATH": str(REPO / "src")},
    )
    assert res.returncode == 0, f"{script.name} не запустился:\n{res.stderr[-1500:]}"
    assert "usage:" in res.stdout, f"{script.name} не дошёл до argparse:\n{res.stdout[:400]}"


@pytest.mark.parametrize("script", SCRIPTS, ids=lambda p: p.name)
def test_no_module_level_name_used_before_definition(script: Path):
    """Функции модуля определены ДО строки, где вызывается main().

    Прямая проверка того самого промаха: всё, что модуль зовёт на верхнем
    уровне, обязано быть определено выше по файлу.
    """
    lines = script.read_text(encoding="utf-8").splitlines()
    entry = next((i for i, ln in enumerate(lines) if ln.startswith("if __name__")), None)
    assert entry is not None, f"{script.name}: не нашёл точку входа"
    defined_after = [
        ln.split("(")[0].removeprefix("def ").strip()
        for ln in lines[entry:]
        if ln.startswith("def ")
    ]
    assert not defined_after, (
        f"{script.name}: определены ПОСЛЕ вызова main() и потому недоступны ему: "
        f"{defined_after}"
    )
