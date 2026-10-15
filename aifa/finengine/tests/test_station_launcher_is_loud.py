"""Лаунчер станции не подменяет версию молча (#181).

ЗАЧЕМ ЭТОТ ФАЙЛ. `station-skills/*/run.sh` — единственная развилка между двумя
копиями нашего кода: свежей, приехавшей ПОЛКОЙ в комнату станции, и запечённой
в образ. До 09.08.2026 отсутствие полки уводило исполнение на запечённую копию
БЕЗ ЕДИНОГО СЛОВА В ЛОГ. Замер 05.08: полка 11 667 б, запечённая копия 8 784 б
от 29 июня — на четверть короче, без #147 и #141. Пользователь получил бы
таблицу, посчитанную позапрошлой методикой, и отличить её было бы не по чему.

С 09.08 методика вдобавок переехала в файлы `aifa-analyze/method/**.yaml`,
которых в июньском образе нет вовсе — то есть тихий откат стал не только
неверным, но и заведомо неработающим.

ЧТО ЗДЕСЬ ПРОВЕРЯЕТСЯ — на НАСТОЯЩЕМ `run.sh`, а не на его пересказе:
  1. полка на месте  → исполняется полка, отпечаток в stderr;
  2. полки нет       → ОТКАЗ ненулевым кодом, запечённая копия НЕ трогается;
  3. полка неполная  → тот же отказ (без method/ анализ упал бы через минуты);
  4. FE_SKILL_SHELF=0 → запечённая копия, но с громким «беру ЗАПЕЧЁННУЮ».

ПРОВЕРКА СТОРОЖА ОТКАТОМ: вернуть в run.sh прежний хвост
`exec python3 "$BAKED/$SKILL_REL" "$@"` вместо `exit 3` — тесты 2 и 3 обязаны
покраснеть (маркер запечённой копии появится в выводе).
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

# (каталог навыка, относительный путь скрипта, обязателен ли комплект method/)
SKILLS = [
    ("aifa-analyze", "aifa_station_skills/aifa-analyze/scripts/analyze.py", True),
    ("aifa-extract", "aifa_station_skills/aifa-extract/scripts/extract.py", False),
    ("aifa-calc", "aifa_station_skills/aifa-calc/scripts/calc_run.py", False),
]
IDS = [s[0] for s in SKILLS]

SHELF_MARKER = "ЗАПУЩЕНО-С-ПОЛКИ"
BAKED_MARKER = "ЗАПУЩЕНА-ЗАПЕЧЁННАЯ-КОПИЯ"


def _script(path: Path, marker: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"print({marker!r})\n", encoding="utf-8")


def _stand(tmp_path: Path, skill: str, rel: str, *,
           shelf_script: bool, method: bool) -> tuple[Path, Path]:
    """Готовит две копии кода: полку в комнате и запечённую в образ."""
    shelf, baked = tmp_path / "shelf", tmp_path / "baked"
    if shelf_script:
        _script(shelf / rel, SHELF_MARKER)
    if method:
        order = shelf / "aifa_station_skills" / skill / "method" / "_order.md"
        order.parent.mkdir(parents=True, exist_ok=True)
        order.write_text("catalogue: []\n", encoding="utf-8")
    _script(baked / rel, BAKED_MARKER)
    return shelf, baked


def _run(skill: str, shelf: Path, baked: Path, **env_extra) -> subprocess.CompletedProcess:
    env = {
        "PATH": "/usr/bin:/bin:/usr/local/bin",
        "FE_SHELF_DIR": str(shelf),
        "FE_SHELF_ZIP": str(shelf.parent / "нет-такого.zip"),
        "FE_BAKED_DIR": str(baked),
        **env_extra,
    }
    return subprocess.run(
        ["bash", str(ROOT / "station-skills" / skill / "run.sh")],
        capture_output=True, text=True, env=env, cwd=str(shelf.parent), timeout=60,
    )


@pytest.mark.parametrize("skill,rel,needs_method", SKILLS, ids=IDS)
def test_shelf_present_runs_the_shelf(tmp_path, skill, rel, needs_method):
    """Штатный путь: исполняется то, что приехало полкой."""
    shelf, baked = _stand(tmp_path, skill, rel, shelf_script=True, method=needs_method)
    r = _run(skill, shelf, baked)
    assert r.returncode == 0, r.stderr
    assert SHELF_MARKER in r.stdout
    assert BAKED_MARKER not in r.stdout


@pytest.mark.parametrize("skill,rel,needs_method", SKILLS, ids=IDS)
def test_shelf_present_prints_the_fingerprint(tmp_path, skill, rel, needs_method):
    """«Чем считали» обязано быть видно по логу постфактум (#155)."""
    import hashlib

    shelf, baked = _stand(tmp_path, skill, rel, shelf_script=True, method=needs_method)
    r = _run(skill, shelf, baked)
    digest = hashlib.sha256((shelf / rel).read_bytes()).hexdigest()[:12]
    assert digest in r.stderr, (
        f"в stderr нет отпечатка исполненного файла:\n{r.stderr}"
    )
    assert str(shelf) in r.stderr, "в логе не назван путь — версию не восстановить"


@pytest.mark.parametrize("skill,rel,needs_method", SKILLS, ids=IDS)
def test_missing_shelf_is_a_refusal_not_a_swap(tmp_path, skill, rel, needs_method):
    """ГЛАВНАЯ проверка: полки нет — отказ, а не тихая июньская копия."""
    shelf, baked = _stand(tmp_path, skill, rel, shelf_script=False, method=needs_method)
    r = _run(skill, shelf, baked)
    assert r.returncode != 0, "полки нет, а лаунчер отработал успешно"
    assert BAKED_MARKER not in r.stdout, (
        "исполнена ЗАПЕЧЁННАЯ копия — это и есть молчаливая подмена версии"
    )
    assert "ОТКАЗ" in r.stderr and "полк" in r.stderr, (
        f"причина отказа не названа словами:\n{r.stderr}"
    )


def test_incomplete_shelf_without_methodology_is_refused(tmp_path):
    """Скрипт есть, методики нет — падать надо сразу, а не через минуты работы.

    Каталог `method/` появился 09.08. Скрипт без него доходит до сборки промпта,
    тратит на дорогу OCR и время, и только там получает MethodologyError.
    """
    skill, rel = SKILLS[0][0], SKILLS[0][1]
    shelf, baked = _stand(tmp_path, skill, rel, shelf_script=True, method=False)
    r = _run(skill, shelf, baked)
    assert r.returncode != 0
    assert "_order.md" in r.stderr, f"не сказано, чего именно не хватает:\n{r.stderr}"
    assert BAKED_MARKER not in r.stdout


@pytest.mark.parametrize("skill,rel,needs_method", SKILLS, ids=IDS)
def test_explicit_stop_cock_takes_baked_but_says_so(tmp_path, skill, rel, needs_method):
    """Осознанный откат остаётся — но громкий и с отпечатком."""
    shelf, baked = _stand(tmp_path, skill, rel, shelf_script=True, method=needs_method)
    r = _run(skill, shelf, baked, FE_SKILL_SHELF="0")
    assert r.returncode == 0, r.stderr
    assert BAKED_MARKER in r.stdout
    assert "ЗАПЕЧЁННУЮ" in r.stderr, f"откат прошёл молча:\n{r.stderr}"
