"""Скрипты навыков исполняются BUSYBOX — башизмов быть не может (#247).

ЗАЧЕМ ЭТОТ ФАЙЛ. Куплен живым ходом 15.08 (Лукойл, первый ход по навыкам после
сноса монолитов). `extract_run.sh` собирал аргументы массивом:

    ARGS=(--text ./ocr.txt --role "$ROLE")
    ...
    exec bash … "${ARGS[@]}"

В комнате станции `/bin/bash` и `/bin/sh` — ОДИН И ТОТ ЖЕ бинарь на 190 КБ,
busybox. Массивов в нём нет: скрипт падал синтаксической ошибкой ДО первой
полезной строки. Модель получила отказ на первом шаге, полчаса щупала версии
шелла («bash не поддерживает массивы — это busybox-ash»), убивала процессы и
обходила обёртку — всё это пользователь читал в чате как «агент делает что-то
не то».

ПОЧЕМУ ДЕФЕКТ НЕ ЛОВИЛСЯ РАНЬШЕ. Пока комнату поднимал драйвер бэкенда, скрипт
исполнялся настоящим bash СЕРВИСА станции, а не песочницы. Снос драйвера
перенёс исполнение в комнату — и башизм, который лежал там всё это время,
впервые оказался на боевом пути. Тестировать в контейнере станции бесполезно:
там bash настоящий, и проверка зелёная при сломанном проде.

ПРОВЕРКА СТОРОЖА ОТКАТОМ: вернуть `ARGS=(...)` в extract_run.sh — тест красный.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

# Каждый образец — башизм, которого busybox не понимает ИЛИ понимает иначе.
BASHISMS = (
    (re.compile(r"^\s*\w+=\(", re.M), "массив `NAME=(...)` — в busybox синтаксическая ошибка"),
    (re.compile(r"\$\{\w+\[[@*0-9]"), "обращение к элементу массива `${NAME[@]}`"),
    (re.compile(r"\w+\+=\("), "дозапись в массив `NAME+=(...)`"),
    (re.compile(r"\[\["), "`[[ ]]` — bash-условие, в busybox не команда"),
    (re.compile(r"<<<"), "here-string `<<<` — bash-only"),
    (re.compile(r"^\s*local\s", re.M), "`local` вне функции ведёт себя иначе"),
)

SCRIPTS = sorted(Path("station-skills").rglob("*.sh"))


def test_there_are_scripts_to_check():
    """Пустой список сделал бы тест ниже зелёным ни на чём."""
    assert len(SCRIPTS) >= 4, f"скриптов навыков нашлось {len(SCRIPTS)} — проверять нечего"


@pytest.mark.parametrize("script", SCRIPTS, ids=lambda p: str(p))
def test_script_has_no_bashisms(script: Path):
    """Ни одного башизма: комната исполняет это busybox'ом."""
    body = script.read_text(encoding="utf-8")
    # Комментарии выкидываем: в них башизмы ЦИТИРУЮТСЯ как объяснение дефекта.
    code = "\n".join(ln for ln in body.splitlines() if not ln.lstrip().startswith("#"))
    found = [why for rx, why in BASHISMS if rx.search(code)]
    assert not found, (
        f"{script}: {'; '.join(found)}.\n"
        "В комнате станции /bin/bash — это busybox (тот же бинарь, что /bin/sh). "
        "Скрипт упадёт синтаксической ошибкой до первой полезной строки, а модель "
        "будет полчаса выяснять, почему (живой ход 15.08)."
    )


def test_extraction_wrapper_runs_the_shelf_not_the_baked_copy():
    """Запечённая копия — путь снесённого драйвера, а не пути по навыкам.

    Обёртка звала `/usr/lib64/.../aifa_station_skills/aifa-extract/run.sh` —
    копию ОБРАЗА. Модель запускает всё с полки; смешивать источники значит
    получить разбор по одной редакции навыка и числа по другой.
    """
    body = Path("station-skills/aifa-extract/scripts/extract_run.sh").read_text(encoding="utf-8")
    code = "\n".join(ln for ln in body.splitlines() if not ln.lstrip().startswith("#"))
    assert "/usr/lib64" not in code, (
        "обёртка снова зовёт ЗАПЕЧЁННУЮ копию образа вместо полки — на пути по "
        "навыкам это разные редакции навыка")
