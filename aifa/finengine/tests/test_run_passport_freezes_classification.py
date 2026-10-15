"""Архитектурный страж Среза 1 (B1, docs/build-plan/11-srez-01-run-passport.md).

«Критерий готовности связки классификации», пункт 3: «После фиксации паспорта
в рабочем пути отсутствуют вызовы detect_profile() или другой функции,
способной изменить маршрутизирующую классификацию. Архитектурный тест
запрещает такой вызов из модулей извлечения/анализа.»

Проверка по AST (тот же приём, что `test_no_second_copy_of_station_script.py`
уже использует для этих же двух скриптов), а не по прогону: `extract.py`/
`analyze.py` не импортируют и не вызывают `detect_profile` вовсе.

ЗАКРЫТО (Срез 13.5): после перевода станционных скриптов на тонкие прослойки
над `production_runtime.py` классификация вызывается уже не станционным
скриптом напрямую, а единственным production ingress
(`production_runtime.prepare_production_context`) — тем же самым
`skills.atoms.frame.classify_frame`/`skills.atoms.run_passport.
classification_from_cell`, просто на один слой глубже. Инвариант «оба
станционных скрипта доходят до классификации только через этот единственный
ingress, не через отдельный путь» — держится проверкой того, что оба зовут
`prepare_production_context`, а сам `production_runtime.py` — единственное
место, которое реально называет `classify_frame`/`classification_from_cell`.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = [
    ROOT / "station-skills" / "aifa-extract" / "scripts" / "extract.py",
    ROOT / "station-skills" / "aifa-analyze" / "scripts" / "analyze.py",
]


def _referenced_names(script: Path) -> set[str]:
    tree = ast.parse(script.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            names.add(node.id)
        elif isinstance(node, ast.alias):
            names.add(node.asname or node.name.rsplit(".", 1)[-1])
        elif isinstance(node, ast.Attribute):
            names.add(node.attr)
    return names


@pytest.mark.parametrize("script", SCRIPTS, ids=lambda p: p.name)
def test_station_scripts_never_reference_detect_profile(script):
    names = _referenced_names(script)
    assert "detect_profile" not in names, (
        f"{script.name}: обнаружена ссылка на detect_profile — второй, "
        f"независимый источник классификации. Решение №2 "
        f"(01-nerushimye-resheniya.md) запрещает переопределять "
        f"зафиксированный RunPassport ниже по конвейеру; классификация — "
        f"только через skills.atoms.frame.classify_frame "
        f"/ skills.atoms.run_passport.classification_from_cell."
    )


@pytest.mark.parametrize("script", SCRIPTS, ids=lambda p: p.name)
def test_station_scripts_route_classification_through_the_single_ingress(script):
    names = _referenced_names(script)
    assert "prepare_production_context" in names, (
        f"{script.name}: не вызывает prepare_production_context — "
        f"классификация могла соскользнуть на отдельный, не единственный путь.")


def test_the_single_ingress_routes_through_the_frame_atom():
    production_runtime = ROOT / "src" / "finengine" / "skills" / "production_runtime.py"
    names = _referenced_names(production_runtime)
    assert {"classify_frame", "classification_from_cell"} & names, (
        "production_runtime.py — единственный production ingress — не "
        "вызывает ни classify_frame, ни classification_from_cell: "
        "классификация могла соскользнуть на старый путь")
