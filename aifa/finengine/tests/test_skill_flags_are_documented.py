"""Каждый флаг скрипта назван в теле навыка — либо помечен внутренним (#239).

ЧТО ЭТОТ СТОРОЖ ЗАКРЫВАЕТ. Флаги добавляли в скрипт, а тело навыка про них не
знало. К 14.08.2026 набралось четыре расхождения, и каждое стоило бы прогона:

  • `--cell` и `--docs` у разбора: скрипт принимал, тело молчало, команду
    строил `station_driver.py` — и тоже их не передавал. Пока жив тул, разницы
    не видно; убери его — модель соберёт команду по телу навыка, БЕЗ ячейки.
    Классификация поедет второй раз и может уйти в другую ветку, чем
    извлечение: таблица из одной ячейки, вердикт из другой;
  • `--lens`: скрипт ждёт список через запятую, тело велело «по одному флагу на
    призму». Обе стороны выглядят разумно, работает только одна;
  • `--out-json` у расчёта: тело не называло вовсе.

ПОЧЕМУ ПРОВЕРЯЕМ ЧЕРЕЗ `--help`, А НЕ РЕГУЛЯРКОЙ ПО ИСХОДНИКУ. Список флагов
знает argparse, а не наш разбор текста: `add_argument` переносится по строкам,
имена собираются в переменных, часть флагов приходит из общего родителя. Запуск
`--help` спрашивает у САМОГО парсера — то есть у того, кто будет разбирать
аргументы в бою.

КАК ПОМЕТИТЬ ФЛАГ ВНУТРЕННИМ. Начать его `help` со слова «ВНУТРЕННИЙ». Пометка
живёт РЯДОМ С ФЛАГОМ, а не отдельным списком в тесте: список пришлось бы
править вторым местом, и он бы протух — ровно та болезнь, от которой сторож.

ПРОВЕРКА СТОРОЖА ОТКАТОМ: убрать `--cell` из тела `aifa-analyze/SKILL.md` —
тест обязан покраснеть и назвать флаг.

⚠️ ЧЕГО ЭТОТ СТОРОЖ НЕ ЛОВИТ (дописано ревизией 21.08.2026). Одну половину он
ИСПОЛНЯЕТ по-настоящему — список флагов спрашивает у самого argparse через
`--help` (сегодня это 28 флагов на четыре навыка и одну обёртку), — а вторую
только ищет подстрокой в тексте. Отсюда граница: он доказывает, что флаг
УПОМЯНУТ в теле, и ничего не говорит о том, упомянут ли он ВЕРНО. Флаг,
названный в прозе с неправильным значением, названный не в той секции,
названный без обязательности там, где он обязателен, — для сторожа
задокументирован. Ровно так и жил `--metrics`: он БЫЛ в теле шестым пунктом
списка необязательных, и три прогона подряд ушли полным реестром. Что тело
предписывает флаг, а не упоминает, сторожит отдельный сосед —
`test_skill_commands_fit_the_sandbox.py::test_the_body_demands_the_metrics_flag_when_the_user_names_metrics`.
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SKILLS_DIR = REPO / "station-skills"

# Навыки берутся ГЛОБОМ: новый навык обязан попасть под сторож сам, без правки
# теста. Список бы разъехался с папкой при первом же добавлении.
SKILLS = sorted(
    d for d in SKILLS_DIR.iterdir()
    if d.is_dir() and (d / "SKILL.md").is_file()
)

# Флаг помечен внутренним — его ставит код (бэкенд, доставка, лаунчер), и
# называть его модели незачем.
_INTERNAL = "ВНУТРЕННИЙ"


def _python_flags(script: Path) -> dict[str, str]:
    """{флаг: текст help} — спрашиваем у настоящего argparse через `--help`."""
    res = subprocess.run(
        [sys.executable, str(script), "--help"],
        capture_output=True, text=True, timeout=120,
        env={**os.environ, "PYTHONPATH": str(REPO / "src"), "COLUMNS": "200"},
    )
    assert res.returncode == 0, f"{script.name} не отдал --help:\n{res.stderr[-1500:]}"

    flags: dict[str, str] = {}
    current = ""
    for line in res.stdout.splitlines():
        # Строка опции: ровно два пробела отступа, дальше дефис.
        m = re.match(r"^ {2}(-{1,2}[^\s,]+)(.*)$", line)
        if m:
            current = m.group(1)
            if current.startswith("--"):
                flags[current] = m.group(2)
            continue
        # Продолжение help предыдущей опции — отступ больше двух пробелов.
        if current.startswith("--") and re.match(r"^ {3,}\S", line):
            flags[current] += " " + line.strip()
    flags.pop("--help", None)
    return flags


def _shell_flags(script: Path) -> set[str]:
    """Флаги обёртки: арм `case` вида `--doc-id) …`."""
    src = script.read_text(encoding="utf-8")
    return set(re.findall(r"^\s*(--[a-z][a-z0-9-]*)\)", src, re.MULTILINE))


def _body(skill: Path) -> str:
    """Тело навыка целиком: корень плюс ветки отрасли на уровень вглубь."""
    return "\n".join(
        p.read_text(encoding="utf-8")
        for p in sorted(skill.glob("SKILL.md")) + sorted(skill.glob("*/SKILL.md"))
    )


def _scripts(skill: Path) -> list[Path]:
    return sorted(skill.glob("scripts/*.py"))


def _wrappers(skill: Path) -> list[Path]:
    return sorted(skill.glob("scripts/*.sh"))


@pytest.mark.parametrize("skill", SKILLS, ids=lambda p: p.name)
def test_every_script_flag_is_named_in_the_body(skill: Path):
    """Флаг есть в скрипте → он назван в теле навыка либо помечен внутренним."""
    body = _body(skill)
    missing: list[str] = []

    for script in _scripts(skill):
        for flag, helptext in _python_flags(script).items():
            if _INTERNAL in helptext.upper():
                continue
            if not re.search(rf"{re.escape(flag)}(?![a-z0-9-])", body):
                missing.append(f"{script.name} {flag}")

    for wrapper in _wrappers(skill):
        for flag in _shell_flags(wrapper):
            if not re.search(rf"{re.escape(flag)}(?![a-z0-9-])", body):
                missing.append(f"{wrapper.name} {flag}")

    assert not missing, (
        f"навык «{skill.name}»: флаги есть в скрипте, но тела навыка про них нет "
        f"— модель их не поставит: {missing}. Либо назови флаг в SKILL.md, либо "
        f"начни его help словом «{_INTERNAL}»"
    )


@pytest.mark.parametrize("skill", SKILLS, ids=lambda p: p.name)
def test_command_lines_do_not_invent_flags(skill: Path):
    """Обратная сторона: тело не зовёт флаг, которого у скрипта нет.

    Проверяем только КОМАНДНЫЕ строки (те, где стоит путь запуска этого
    навыка): их модель копирует буквально. В прозе флаг соседнего навыка
    упоминать законно — классификация, например, объясняет, что ячейку дальше
    передают флагом `--cell`, и это не её флаг.
    """
    known: set[str] = set()
    for script in _scripts(skill):
        known |= set(_python_flags(script))
    for wrapper in _wrappers(skill):
        known |= _shell_flags(wrapper)

    # Команды бывают СОСТАВНЫЕ: извлечение и расчёт стоят в одной строке через
    # `;` — так их и надо запускать, иначе упавшее извлечение оставит ход без
    # завершающего события. Значит, разбирать строку целиком нельзя: `--final`
    # принадлежит расчёту, а не извлечению. Режем строку по путям запуска и
    # смотрим только тот кусок, что идёт после пути ЭТОГО навыка.
    others = [d.name for d in SKILLS if d.name != skill.name]
    invented: list[str] = []
    for line in _body(skill).splitlines():
        idx = max(line.find(f"{skill.name}/run.sh"), line.find(f"{skill.name}/scripts/"))
        if idx < 0:
            continue
        seg = line[idx:]
        cut = [p for p in (seg.find(f"{o}/") for o in others) if p > 0]
        if cut:
            seg = seg[: min(cut)]
        for flag in re.findall(r"(--[a-z][a-z0-9-]*)", seg):
            if flag not in known:
                invented.append(f"{flag} в строке: {line.strip()[:110]}")

    assert not invented, (
        f"навык «{skill.name}»: команда в теле зовёт флаг, которого у скрипта "
        f"нет — прогон упадёт на разборе аргументов: {invented}"
    )
