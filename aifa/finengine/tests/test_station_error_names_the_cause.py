"""Ошибка станции доезжает ПРИЧИНОЙ, а не заголовком traceback.

ЗАЧЕМ ЭТОТ ФАЙЛ (09.08.2026, куплен часом разбора живого прогона). В
`station_driver` стояло `stderr[:300]` — первые 300 знаков. У питоновского
traceback полезное в ПОСЛЕДНЕЙ строке: тип и текст исключения. Первые 300
знаков — это «Traceback (most recent call last)» и путь к файлу.

В тот же день лаунчер станции начал печатать строку с отпечатком исполняемого
файла (#181) — она встала первой и добила остаток бюджета. В логе осталось
ровно четыре бесполезные строки, а настоящая причина —
`ModuleNotFoundError: No module named 'yaml'` — не попала в сообщение вообще.
Разбор занял час вместо минуты и увёл по двум ложным следам.

⚠️ ПРАВИЛО ПЕРЕЕХАЛО (#166). Жило в `station_driver`, который поднимал комнату
станции сам; вместе со сносом монолитов драйвер умер. Приёмник отказа теперь
один — сток прогона (`station_run_sink`), туда правило и переехало. Удалять
сторож было нельзя: правило живое, менялось только место.

ПРОВЕРКА СТОРОЖА ОТКАТОМ: вернуть в `error_tail` срез с головы (`text[:limit]`)
— тесты обязаны покраснеть.
"""
from __future__ import annotations

from finengine.skills.station_run_sink import error_tail as _error_tail

# Похоже на то, что реально пришло 09.08: сначала отпечаток лаунчера, потом
# шапка traceback, и только в самом конце — причина.
STDERR = (
    "[aifa-analyze] исполняю ./.shelf/aifa_station_skills/aifa-analyze/scripts/"
    "analyze.py (24819 б, sha256 6faf6cddaf05)\n"
    "Traceback (most recent call last):\n"
    + "".join(f'  File "/tmp/ws/x{i}.py", line {i}, in _run\n    step_{i}()\n'
             for i in range(60))
    + "ModuleNotFoundError: No module named 'yaml'\n"
)


def test_the_cause_survives_truncation():
    """Главное: тип и текст исключения должны быть в сообщении."""
    assert len(STDERR) > 1200, "тестовые данные короче лимита — проверка пустая"
    got = _error_tail(STDERR)
    assert "ModuleNotFoundError: No module named 'yaml'" in got, (
        "причина не доехала — по такому сообщению дефект не найти:\n" + got
    )


def test_truncation_is_marked():
    """Обрезанный текст обязан выглядеть обрезанным, иначе он выглядит полным."""
    assert _error_tail(STDERR).startswith("…")


def test_short_output_is_not_touched():
    assert _error_tail("короткая ошибка") == "короткая ошибка"


def test_budget_is_big_enough_for_a_real_traceback():
    """300 знаков не хватало на traceback даже без строки отпечатка."""
    got = _error_tail(STDERR)
    assert len(got) >= 1000, f"бюджет {len(got)} — снова не хватит на traceback"
