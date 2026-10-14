"""Библиотека, которую код требует в станции, объявлена в образе станции.

ЗАЧЕМ ЭТОТ ФАЙЛ (09.08.2026). Анализ исполняется ВНУТРИ запечатанной станции,
у которой свой набор библиотек — не тот, что у бэкенда и не тот, что у dev-venv.
В этот день база методики переехала в YAML, `methodology.py` начал импортировать
pyyaml, тесты локально позеленели, выкатка прошла, сторожа доставки отчитались —
и КАЖДЫЙ анализ у пользователя падал `ModuleNotFoundError: No module named
'yaml'`. Файлы доехали; библиотеки — нет.

Это ровно «два места, обновляется одно» (#180, #181, #187): импорт живёт в
питоне, набор библиотек — в Dockerfile станции, и разъехаться они могут молча.

Отдельно на стенде `ship.sh --station` просит станцию ПРОЧИТАТЬ методику её
собственным питоном: этот тест ловит расхождение до выкатки, тот — после
сборки образа.

🔴 ОБЛАСТЬ РАСШИРЕНА РЕВИЗИЕЙ 21.08.2026, ПОТОМУ ЧТО СТОРОЖ БЫЛ ПУСТ.
До этого дня он смотрел ТОЛЬКО `src/finengine/skills/analysis/*.py` — девять
модулей, у которых сегодня НИ ОДНОГО импорта третьей стороны на уровне модуля.
Проверяемое множество было пустым, и обещание в этой же шапке — «убрать pyyaml
из pip-строки → тест покраснеет» — было неправдой: снял pyyaml из ОБОИХ
Dockerfile, прогон дал 11 passed. Тот самый `methodology.py`, ради которого
файл заведён, свой `import yaml` с тех пор потерял.

Теперь смотрим весь код, который едет в комнату полкой и там исполняется:
скрипты навыков, `finengine/skills` с подпакетами, роли и станционные модули
`llm/`. Пустоту множества проверяет `test_there_is_something_to_check` — она и
была дырой: список ПАКЕТОВ образа от пустоты сторожили, список ИМПОРТОВ нет.

⚠️ ЧЕГО ЭТОТ СТОРОЖ НЕ ЛОВИТ. Только импорты УРОВНЯ МОДУЛЯ. Ленивый
`import` внутри функции (а у нас так написана добрая половина) случается в
момент вызова, и его отсутствие в образе всплывёт прогоном у пользователя, а
не здесь. И он ничего не знает о том, что реально установлено в собранном
образе, — он сверяет исходник с pip-строкой Dockerfile, а не с контейнером.

ПРОВЕРКА СТОРОЖА ОТКАТОМ: убрать `httpx` из pip-строк обоих Dockerfile —
`skills/*.py` краснеют (проверено мутацией 21.08.2026).
"""
from __future__ import annotations

import ast
import re
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DOCKERFILES = [ROOT / "deploy" / "workstation-aifa" / "Dockerfile",
               ROOT / "deploy" / "workstation-aifa" / "Dockerfile.shelf"]

# Код, исполняемый В КОМНАТЕ: то, что приезжает полкой и запускается моделью.
# Каталоги, а не список файлов: новый модуль обязан попасть под сторож сам.
STATION_CODE = [
    ROOT / "station-skills",
    ROOT / "src" / "finengine" / "skills",
    ROOT / "src" / "finengine" / "roles",
]
# Из `llm/` в станции живут только мост и прогресс; остальное — бэкендовое.
STATION_LLM = sorted((ROOT / "src" / "finengine" / "llm").glob("station_*.py"))

# 🔴 ИЗВЕСТНЫЙ РАЗРЫВ, НЕ ЗАКРЫТЫЙ НА 21.08.2026. Список не «исключения», а
# список названных дыр: каждая строка обязана быть ПРАВДОЙ — сторож ниже
# краснеет и когда дыра выросла, и когда её закрыли, а строку не сняли.
KNOWN_GAP = {
    # `skills/spec/runner.py` и `skills/factor_analysis/analyzer.py` держат
    # `import yaml` уровня модуля, а pyyaml в
    # pip-строках образа станции НЕТ. Это ровно рецидив инцидента 09.08, из-за
    # которого файл и заведён, — только на ЗАПАСНОМ пути (спека, #154):
    # пока ход идёт скриптом навыка, разрыв не виден, а переключат на спеку —
    # каждый прогон умрёт `ModuleNotFoundError: yaml`.
    # Найдено ревизией тестов 21.08.2026, чинить отдельной задачей: правка
    # Dockerfile — это src, а не тесты.
    ("runner.py", "yaml"),
    ("analyzer.py", "yaml"),
}

# Имя пакета на PyPI ≠ имя модуля при импорте.
DIST_OF_MODULE = {"yaml": "pyyaml", "dateutil": "python-dateutil",
                  "Levenshtein": "python-levenshtein", "fitz": "pymupdf"}


def _station_packages() -> set[str]:
    """Пакеты, объявленные в pip-строках образов станции."""
    out: set[str] = set()
    for df in DOCKERFILES:
        # Склеиваем переносы строк ДО поиска: pip-строка в Dockerfile почти
        # всегда многострочная через «\» в конце.
        text = df.read_text(encoding="utf-8").replace("\\\n", " ")
        # Только исполняемые строки: `pip install` встречается и в комментарии
        # («where UBI8 system pip installs by default»), и оттуда в список
        # пакетов уезжали слова by/so/the/force — сторож считал их пакетами.
        for m in re.finditer(r"^\s*RUN\b[^\n]*?pip install[^\n]*", text, re.MULTILINE):
            for tok in re.split(r"\s+", m.group(0)):
                tok = tok.strip('"').strip("'")
                if not tok or tok.startswith("-") or tok in {"pip", "install"}:
                    continue
                name = re.split(r"[<>=!]", tok)[0].strip()
                if name and re.fullmatch(r"[A-Za-z0-9_.-]+", name):
                    out.add(name.lower())
    return out


def _toplevel_imports(tree: ast.Module) -> set[str]:
    """Импорты уровня модуля — те, что случаются при `import`, а не по вызову."""
    found: set[str] = set()
    for node in tree.body:  # именно body: вложенные (в функциях) не считаем
        if isinstance(node, ast.Import):
            found |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            found.add(node.module.split(".")[0])
    return found


MODULES = sorted(
    {p for root in STATION_CODE for p in root.rglob("*.py")} | set(STATION_LLM))


def test_there_is_something_to_check(движок_ast):
    """ДЫРА, ИЗ-ЗА КОТОРОЙ СТОРОЖ ПРОСТОЯЛ ПУСТЫМ: проверяемых импортов ноль.

    Модули были, пакеты образа разбирались, отрицательный контроль на пустой
    список пакетов стоял — а множество импортов третьей стороны, ради которого
    всё затевалось, оказалось пустым, и `missing` не мог наполниться ничем.
    """
    assert MODULES, "не найдено ни одного модуля станции — тест устарел"
    third = {m for path in MODULES for m in _toplevel_imports(движок_ast[path])
             if m not in sys.stdlib_module_names
             and m not in {"finengine", "mmar_carl", "mmar_utils"}}
    assert third, (
        "ни один модуль станции не импортирует третьих сторон на уровне "
        "модуля — значит проверка ниже зелена ни о чём, как это и было с "
        "09.08 по 21.08.2026")


def test_station_package_list_was_parsed():
    """Пустой список молча сделал бы проверку ниже зелёной."""
    pkgs = _station_packages()
    assert "httpx" in pkgs and "pydantic" in pkgs, (
        f"не разобрал pip-строки образов станции, вышло: {sorted(pkgs)}"
    )


def test_shelf_overlay_materializes_sqlalchemy_for_sandbox_python():
    """Регрессия живой станции 24.09.2026: наличие SQLAlchemy в
    `/opt/venv` не помогает комнате — её `/usr/bin/python3.12` видит только
    `/usr/lib64`. Overlay обязан физически перенести пакет и greenlet туда,
    а не полагаться на декларацию в полной Dockerfile."""
    text = DOCKERFILES[1].read_text(encoding="utf-8")
    assert "/opt/venv/lib64/python3.12/site-packages/sqlalchemy" in text
    assert "/opt/venv/lib64/python3.12/site-packages/greenlet" in text
    assert "/usr/lib64/python3.12/site-packages/" in text


def test_third_party_imports_are_available_in_the_station(движок_ast):
    """Одним проходом по всем модулям станции, а не тестом на модуль:
    сто сорок узлов сбора ради одного правила — это шум в отчёте, а не
    подробность. Виновника называет сообщение."""
    pkgs = _station_packages()
    missing: list[str] = []
    stale: list[str] = []
    for path in MODULES:
        for mod in sorted(_toplevel_imports(движок_ast[path])):
            if mod in sys.stdlib_module_names or mod in {
                    "finengine", "mmar_carl", "mmar_utils"}:
                continue
            dist = DIST_OF_MODULE.get(mod, mod).lower()
            if (path.name, mod) in KNOWN_GAP:
                # Обратная сторона списка названных дыр: закрыли — вычеркните.
                if dist in pkgs:
                    stale.append(f"{path.name}: {mod}")
                continue
            if dist not in pkgs:
                missing.append(f"{path.relative_to(ROOT)} → {mod} (пакет {dist})")
    assert not stale, (
        f"дыры закрыты, а строки в KNOWN_GAP остались: {stale} — вычеркните их, "
        f"иначе список охраняет прошлое")
    assert not missing, (
        "код станции импортирует то, чего в её образе нет:\n  "
        + "\n  ".join(missing) + "\n"
        "Код исполняется В СТАНЦИИ — значит упадёт у пользователя после того, "
        "как полка доедет и все сторожа доставки отчитаются. Добавить пакет в "
        "pip-строку образа станции. ⚠️ На 21.08.2026 pip-строка есть ТОЛЬКО в "
        "deploy/workstation-aifa/Dockerfile; в Dockerfile.shelf её нет вовсе — "
        "прежняя редакция этого сообщения посылала править несуществующую строку.")
