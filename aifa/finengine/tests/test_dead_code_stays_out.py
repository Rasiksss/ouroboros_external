"""Рудименты не накапливаются: мёртвые импорты, переменные и константы.

РЕШЕНИЕ ВЛАДЕЛЬЦА 20.08.2026: «Снятые тулы — сносим если не используем, не
надо засирать код. Итак слишком много рудиментов оставляешь. Нужно будет все
почистить что не используешь, чтобы освободить контекст и не путаться».

ЦЕНА, КОТОРУЮ ПЛАТИТ РУДИМЕНТ. Не байты на диске. Мёртвый импорт и мёртвая
константа читаются следующим как действующая связь: свежий контекст видит
`from ...models import Category` в `render.py` и тратит ход на выяснение, где
же категория используется при отрисовке. Нигде. Разбор рудиментов 20.08 нашёл
две константы-фикстуры (`_RENAMED`, `_ENGLISH` в `test_methodology_guards.py`,
34 строки), пережившие переписывание тестов, которые их заводили.

ЧТО СТОРОЖ НЕ ЛОВИТ И ПОЧЕМУ ЭТО ВАЖНО ЗНАТЬ. Он не ловит НИ ОДНОГО дефекта
поведения: движок, считающий неверно, проходит его зелёным — предмет охраны
здесь гигиена, а не результат. И даже внутри гигиены он смотрит на статические
ссылки. Модуль, который грузят через `spec_from_file_location`, для него
невидим — так `data/metric_enrichment.py` (900 строк) выглядит мёртвым, а его
читает `tests/build_metric_registry.py`. Поэтому сторож держит СПИСОК
ИСКЛЮЧЕНИЙ с причиной у каждого, а не молчит про находку.

⚠️ ВТОРОЕ МЕСТО ТОГО ЖЕ ПРАВИЛА — `# noqa`. `LINT_EXCEPTIONS` требует причину
и охраняется парным тестом; строчное подавление `# noqa: F401` не требует
ничего. Ruff подчиняется ему МОЛЧА, поэтому первый тест видит крохотную долю
своего класса: 2 находки против 212 с `--ignore-noqa` (замер 20.08). Без
третьего теста дисциплина «исключение только с причиной» имеет чёрный ход, и
им в репозитории уже пользуются — шесть мёртвых импортов стояли под `# noqa`
в `bulk_extractor/__init__.py` и первому сторожу были невидимы.

Поэтому подавления посчитаны и заперты потолком (`NOQA_BUDGET`): новое
подавление в незаявленном файле и лишнее подавление в заявленном одинаково
краснеют. Потолок — не индульгенция: он обязан УМЕНЬШАТЬСЯ, и тест требует
править число вниз каждый раз, когда подавление снято.
"""
from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
TESTS = ROOT / "tests"

# Каждое исключение — с причиной. Пустая причина = рудимент, а не исключение.
LINT_EXCEPTIONS = {
    "src/finengine/skills/news/connectors/playwright_base.py:64": (
        "не рудимент: `import` внутри try/except ImportError — это ПРОБА "
        "наличия playwright, а рабочий импорт стоит ниже, в строке 88. "
        "Убрать «неиспользуемый» импорт = снять проверку доступности и "
        "получить ImportError вместо строки в журнале"
    ),
}

# Сколько находок F401/F841 файл прячет под `# noqa`. Замер 20.08 на
# `fix/x3-docs-numbers`; команда — в docs/РУДИМЕНТЫ.md §3.3.
NOQA_BUDGET = {
    "src/finengine/skills/bulk_extractor/__init__.py": (
        197,
        "публичный API пакета заморожен на время разбиения: имена уехали в "
        "подмодули, а `from finengine.skills.bulk_extractor import X` обязан "
        "работать ровно как до него (см. комментарий в самом файле). Число "
        "обязано падать по мере разбиения, а не расти"
    ),
    "src/finengine/skills/bulk_extractor/streams.py": (
        7,
        "тот же заморозок API на время разбиения bulk_extractor"
    ),
    "src/finengine/skills/factor_analysis/__init__.py": (
        2,
        "ре-экспорт из подмодулей пакета: имена берут снаружи как "
        "`from finengine.skills.factor_analysis import X`"
    ),
    "src/finengine/llm/gigachat.py": (
        1,
        "не рудимент: `import gigachat` в `_ensure_sdk` (строка 81) стоит "
        "внутри try/except ImportError и служит ПРОБОЙ наличия SDK — снять "
        "его значит вернуть невнятный ImportError вместо понятного текста"
    ),
}


def _lint(rules: str, target: Path, ignore_noqa: bool = False) -> list[str]:
    cmd = [sys.executable, "-m", "ruff", "check", "--select", rules, "--no-cache",
           "--output-format=concise"]
    if ignore_noqa:
        cmd.append("--ignore-noqa")
    out = subprocess.run(
        cmd + [str(target)], capture_output=True, text=True, cwd=ROOT,
    ).stdout
    found = []
    for line in out.splitlines():
        if ": F" not in line:
            continue
        path, row = line.split(":")[0], line.split(":")[1]
        relative = Path(path).resolve().relative_to(ROOT).as_posix()
        found.append(f"{relative}:{row}")
    return found


def test_no_dead_imports_or_locals_in_the_engine():
    """Мёртвые импорты и мёртвые локальные переменные в `src/` — под ноль.

    F401 — импорт, который никто не использует. F841 — присвоение, которое
    никто не читает: у второго класса цена выше, потому что это часто не
    мусор, а флаг, у которого увели читателя.
    """
    guilty = [h for h in _lint("F401,F841", SRC) if h not in LINT_EXCEPTIONS]
    assert not guilty, (
        "в движок вернулся мёртвый код:\n  " + "\n  ".join(guilty)
        + "\nЛибо снести, либо внести в LINT_EXCEPTIONS С ПРИЧИНОЙ.")


def test_every_lint_exception_still_describes_something_real():
    """Исключение, под которым больше нет находки, — враньё в списке.

    Без этой половины список исключений растёт и никогда не убывает: причина
    остаётся написанной после того, как код починили, и следующий читатель
    верит, что дефект жив.
    """
    live = set(_lint("F401,F841", SRC))
    stale = sorted(set(LINT_EXCEPTIONS) - live)
    assert not stale, (
        f"исключения описывают несуществующие места: {stale} — "
        "код починили, а объяснительная осталась")
    for place, why in LINT_EXCEPTIONS.items():
        assert len(why) > 40, f"у исключения {place} нет внятной причины"


def _suppressed_by_noqa() -> dict[str, int]:
    """Сколько находок F401/F841 каждый файл `src/` прячет под `# noqa`."""
    visible: dict[str, int] = {}
    for hit in _lint("F401,F841", SRC):
        visible[hit.split(":")[0]] = visible.get(hit.split(":")[0], 0) + 1
    hidden: dict[str, int] = {}
    for hit in _lint("F401,F841", SRC, ignore_noqa=True):
        f = hit.split(":")[0]
        hidden[f] = hidden.get(f, 0) + 1
    return {f: n - visible.get(f, 0) for f, n in hidden.items()
            if n - visible.get(f, 0) > 0}


def test_noqa_suppressions_are_declared_and_do_not_grow():
    """`# noqa` — обход первого сторожа, и без потолка он бесконтрольный.

    Первый тест видит 2 находки, а с `--ignore-noqa` их 212: подавления
    прячут 99% класса. Здесь они посчитаны пофайлово и заперты числом —
    подавление в незаявленном файле краснеет, лишнее в заявленном тоже.
    """
    actual = _suppressed_by_noqa()
    undeclared = sorted(f for f in actual if f not in NOQA_BUDGET)
    assert not undeclared, (
        "новое подавление `# noqa` в незаявленном файле:\n  "
        + "\n  ".join(f"{f} ({actual[f]})" for f in undeclared)
        + "\nЛибо снести мёртвое имя, либо внести файл в NOQA_BUDGET С ПРИЧИНОЙ.")
    grew = sorted(f for f, (cap, _) in NOQA_BUDGET.items()
                  if actual.get(f, 0) > cap)
    assert not grew, (
        "подавлений стало больше объявленного: "
        + ", ".join(f"{f}: {actual.get(f, 0)} > {NOQA_BUDGET[f][0]}" for f in grew)
        + "\nПотолок обязан падать, а не расти.")
    shrank = sorted(f for f, (cap, _) in NOQA_BUDGET.items()
                    if actual.get(f, 0) < cap)
    assert not shrank, (
        "подавлений стало меньше объявленного — опустите число в NOQA_BUDGET: "
        + ", ".join(f"{f}: {actual.get(f, 0)} < {NOQA_BUDGET[f][0]}" for f in shrank)
        + "\nИначе потолок перестаёт запирать: под ним снова есть место.")
    for place, (_, why) in NOQA_BUDGET.items():
        assert len(why) > 40, f"у потолка {place} нет внятной причины"


def _unused_module_constants(path: Path) -> list[str]:
    """Приватные константы уровня модуля, на которые в файле нет ни ссылки."""
    text = path.read_text(encoding="utf-8", errors="ignore")
    tree = ast.parse(text)
    declared = {}
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        for t in node.targets:
            if isinstance(t, ast.Name) and t.id.startswith("_") and t.id.isupper():
                declared[t.id] = node.lineno
    if not declared:
        return []
    used = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
            used.add(node.id)
        elif isinstance(node, ast.Attribute):
            used.add(node.attr)
    return [f"{path.relative_to(ROOT)}:{ln} {name}"
            for name, ln in sorted(declared.items()) if name not in used]


def test_no_orphan_fixtures_left_in_the_tests():
    """Константа-фикстура, пережившая свой тест, — ложный след.

    `_RENAMED` и `_ENGLISH` пролежали в `test_methodology_guards.py` мёртвыми:
    отчёт-образец на 34 строки, который читается как вход действующей
    проверки. ⚠️ Смотрим РАЗОБРАННЫМ ast, а не поиском по тексту: имя
    константы встречается в комментариях, и текстовый поиск зеленел бы от
    объяснительной записки.
    """
    guilty: list[str] = []
    for p in sorted(TESTS.glob("test_*.py")):
        guilty += _unused_module_constants(p)
    assert not guilty, (
        "в тестах лежат константы, которых никто не читает:\n  "
        + "\n  ".join(guilty)
        + "\nОни переживают переписанный тест и читаются как действующий вход.")


def test_replaced_one_shot_planner_stays_deleted_and_unimported():
    """После cutover старый модуль и промпт не должны стать вторым путём истины."""
    module_stem = "dynamic_" + "planner"
    prompt_name = "planner_" + "skill.md"
    banned_symbols = ("Dynamic" + "Planner", "Dynamic" + "PlanError", "plan_" + "dynamic")
    assert not (SRC / "finengine" / "skills" / "spec" / f"{module_stem}.py").exists()
    assert not (SRC / "finengine" / "skills" / "spec" / prompt_name).exists()

    offenders: list[str] = []
    for path in SRC.rglob("*.py"):
        text = path.read_text(encoding="utf-8", errors="ignore")
        if module_stem in text or any(symbol in text for symbol in banned_symbols):
            offenders.append(path.relative_to(ROOT).as_posix())
    assert not offenders, "удалённый одноходовый путь снова упомянут в src/: " + ", ".join(offenders)
