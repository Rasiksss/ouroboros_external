"""Скрипты навыков не читают необъявленных переменных при `set -u`.

🔴 КУПЛЕНО ПРОГОНОМ БСПБ 26.08.2026. В `extract_run.sh` завели флаг
`--extra-calculated` и переменную `XCALC` (коммит 2c2aa19), а в строку
объявления её не добавили. При `set -euo pipefail` это не «пустая строка», а
СМЕРТЬ скрипта:

    bash: line 218: XCALC: unbound variable

Падал КАЖДЫЙ прогон без этого флага — то есть почти каждый, — и падал ДО
запуска движка. Модель прочитала отчёт об ошибке верно («отсутствует параметр
XCALC, строка 218»), полезла править наш скрипт тулом `edit`, получила ENOENT
и потратила ход на это.

⚠️ ЧТО ИМЕННО ПРОВЕРЯЕТСЯ. Не орфография и не формулировки: берутся имена
переменных, которые скрипт ЧИТАЕТ (`$ИМЯ` / `${ИМЯ}`), и сверяются с теми, что
он где-либо ПРИСВАИВАЕТ. Читает, но нигде не присваивает — при `set -u` это
бомба с часовым механизмом: сработает на первом же прогоне, где ветка не
задала переменную.

Проверка идёт только по скриптам с `set -u`: без него необъявленная переменная
законно раскрывается в пустую строку, и запрещать это нечего.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

СКРИПТЫ = sorted((Path(__file__).resolve().parents[1] / "station-skills").rglob("*.sh"))

# Читаемые: $ИМЯ и ${ИМЯ...}. Формы с умолчанием (`${ИМЯ:-…}`) и проверкой
# (`${ИМЯ+…}`) при `set -u` законны — их исключаем.
_ЧТЕНИЕ = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)[}:]|\$([A-Za-z_][A-Za-z0-9_]*)")
_ЗАПИСЬ = re.compile(r"(?:^|[;&|]\s*|\b(?:export|local|readonly)\s+)([A-Za-z_][A-Za-z0-9_]*)=",
                     re.MULTILINE)
_ЦИКЛ = re.compile(r"\bfor\s+([A-Za-z_][A-Za-z0-9_]*)\s+in\b")

# Даёт окружение, а не скрипт: часть приезжает от хоста комнаты.
ИЗВНЕ = {
    "HOME", "PATH", "PWD", "USER", "SHELL", "TMPDIR", "LANG", "IFS", "OLDPWD",
    "FE_PROGRESS_NONCE", "FE_RPC_DIR", "FE_STATION_TOKEN", "FE_BACKEND_URL",
    "OPENROUTER_API_KEY", "SKILL", "SKILL_REL", "PYTHONPATH", "PYTHONUNBUFFERED",
    "BASH_SOURCE", "FUNCNAME", "LINENO", "RANDOM", "SECONDS", "PPID",
}


def test_there_are_scripts_to_check():
    """Сторож без предмета — не сторож."""
    assert len(СКРИПТЫ) >= 4, f"скриптов найдено {len(СКРИПТЫ)} — проверять нечего"


@pytest.mark.parametrize("скрипт", СКРИПТЫ, ids=lambda p: f"{p.parent.parent.name}/{p.name}")
def test_no_variable_is_read_without_ever_being_assigned(скрипт):
    """🔴 ГЛАВНОЕ. Мутация: убрать `XCALC=""` из объявления — тест краснеет."""
    текст = скрипт.read_text(encoding="utf-8")
    if not re.search(r"set\s+-\w*u", текст):
        return  # без `set -u` необъявленная переменная законно пуста

    # Комментарии выкидываем: там имена переменных упоминаются в разборе
    # инцидентов, а исполняется оттуда ничего.
    код = "\n".join(с.split("#", 1)[0] for с in текст.splitlines())

    присвоено = {m.group(1) for m in _ЗАПИСЬ.finditer(код)}
    присвоено |= {m.group(1) for m in _ЦИКЛ.finditer(код)}
    прочитано = {m.group(1) or m.group(2) for m in _ЧТЕНИЕ.finditer(код)}

    сироты = sorted(и for и in прочитано - присвоено - ИЗВНЕ
                    if not и.isdigit() and len(и) > 1)
    assert not сироты, (
        f"{скрипт.name}: читает, но нигде не присваивает: {сироты}. При `set -u` "
        "это не пустая строка, а падение скрипта («unbound variable») — и падает "
        "он до запуска работы, на первой же ветке, которая переменную не задала.")
