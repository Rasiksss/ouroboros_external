"""Выкатка прода не имеет прибитой ветки (17.08.2026).

МИНА, КОТОРАЯ СТОЯЛА ТИХО. В `ship.sh` было прибито
`git merge origin/feature/personal-sources-kb` — ветка, про которую в CLAUDE.md
написано, что она отстала на 148 коммитов и не трогалась с 27 июля. Любой, кто
запустил бы штатную выкатку прода, принёс бы туда мёртвый код, и заметили бы
это на живых пользователях.

Опасность была именно в тишине: `--prod` запускают редко, а при беглом чтении
строка выглядит осмысленной — ветка названа, мердж есть.

УМОЛЧАНИЯ НЕТ НАМЕРЕННО. Любое умолчание здесь однажды устареет ровно так же, а
цена ошибки — прод с живыми пользователями. Пусть выкатка требует назвать ветку
вслух.

⚠️ ЧЕГО ЭТОТ ФАЙЛ НЕ ЛОВИТ (ревизия 21.08.2026). Он ЧИТАЕТ ТЕКСТ `ship.sh` и
никогда его не запускает — и запускать не может: скрипт с первых строк ходит по
`ssh` на боевой VPS, а проверка `PROD_BRANCH` стоит только на 405-й, то есть
«исполнить отказ» в тесте значило бы выкатить прод. Отсюда граница: доказано
лишь то, что нужные строки СТОЯТ в исполняемой (не закомментированной) части.
НЕ доказано ничего из того, ради чего они там: что ветка `$PROD_BRANCH` вообще
существует на origin, что `exit 8` случается раньше первого ssh, что подсказки
попадают в stderr, а не тонут в выводе, и что `SHIP_FORCE` действительно
обходит защиту. Перестановка тех же строк в мёртвую ветку (`if false; then`)
оставит этот файл зелёным.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

_SHIP = Path(__file__).resolve().parents[1] / "deploy" / "skill-hub" / "ship.sh"


def _code_lines() -> list[str]:
    """Строки БЕЗ комментариев: в комментариях мёртвая ветка упомянута
    намеренно — там объясняется, что тут было и почему убрали."""
    out = []
    for ln in _SHIP.read_text(encoding="utf-8").splitlines():
        stripped = ln.strip()
        if stripped.startswith("#"):
            continue
        out.append(ln.split(" #")[0] if " #" in ln else ln)
    return out


def test_the_abandoned_branch_is_not_merged_anymore():
    """Отрицательный контроль: вернул `merge origin/feature/personal-sources-kb`
    в исполняемую строку — краснеет."""
    merges = [ln for ln in _code_lines() if "git merge" in ln]
    assert merges, "мердж в выкатке прода исчез — проверять нечего"
    for ln in merges:
        assert "personal-sources-kb" not in ln, (
            f"вернулась заброшенная ветка в мердж: {ln.strip()}")


def test_branch_comes_from_an_explicit_variable():
    """Ветка берётся из переменной, а не из литерала: литерал однажды
    устареет так же, как устарел прежний."""
    merges = [ln for ln in _code_lines() if "git merge" in ln and "origin/" in ln]
    for ln in merges:
        assert "$PROD_BRANCH" in ln, f"ветка прода задана литералом: {ln.strip()}"


def test_deploy_refuses_without_the_variable():
    """Молчаливое умолчание — то, с чего всё началось. Скрипт обязан
    остановиться и сказать, чего от него хотят."""
    src = _SHIP.read_text(encoding="utf-8")
    assert 'if [ -z "${PROD_BRANCH:-}" ]' in src, "проверка переменной пропала"
    assert "exit 8" in src, "отказ без кода возврата — вызывающий его не заметит"


@pytest.mark.parametrize("must", [
    "Назови ветку явно",           # что делать
    "PROD_BRANCH=",                # как именно
    "Доступные ветки",             # чем помочь
])
def test_refusal_tells_what_to_do(must):
    """Отказ без подсказки заставляет лезть в исходник — это тот же класс
    беды, что «agent ran more than 7 tool turns — aborted»."""
    assert must in _SHIP.read_text(encoding="utf-8")


def test_busy_prod_still_blocks_the_deploy():
    """Правка не должна была задеть прежнюю защиту: выкатка поверх живого
    прогона рвёт ход пользователя."""
    src = _SHIP.read_text(encoding="utf-8")
    assert "ПРОД занят живым прогоном" in src
    assert re.search(r"SHIP_FORCE", src), "аварийный обход исчез"
