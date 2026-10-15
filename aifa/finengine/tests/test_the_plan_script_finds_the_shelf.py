"""Ручной/model-declared план не возвращается как execution source."""
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_model_declared_plan_runner_is_removed():
    assert not (ROOT / "station-skills" / "_runner" / "plan.py").exists()


def test_no_skill_body_declares_the_plan():
    """План принадлежит ХОДУ, а не навыку — решение владельца 29.08.2026.

    До этого дня шаг 0 порядка работ `aifa-extract` звал `_runner/plan.py`, и
    план рождался ВНУТРИ навыка — то есть уже после того, как навык выбран.
    Пользовательский ход обычно шире одного навыка: разобрать, чем пришли,
    отобрать документы, извлечь, посчитать. Плана на эту работу не было ни у
    кого, потому что объявить его было негде.

    `update_plan` может проецироваться на экран, но не исполняет бизнес-шаги.
    Порядок исполнения принадлежит `PlanGraph`.
    """
    корень = ROOT / "station-skills"
    виноватые = [тело.relative_to(корень.parent)
                 for тело in корень.glob("*/**/SKILL.md")
                 if "_runner/plan.py" in тело.read_text(encoding="utf-8")]
    assert not виноватые, (
        f"тела навыков снова объявляют план сами: {виноватые}. План — свойство "
        "хода, а не навыка: объявлять его должен ядровой update_plan, иначе "
        "работа шире одного навыка остаётся без плана вовсе")
