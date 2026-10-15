"""`intent.parse` — пункты 1 и 2 критерия готовности атома
(docs/build-plan/14-srez-04-graf-istochnik-istiny.md), измеренные на ЖИВОЙ
модели через золотой набор (`tests/golden/intent/queries.json`, C0.1: ≥100
запросов, все 12 значений `IntentKind` — каталог 11 `goal_type` из
docs/build-plan/26-prilozhenie-ad-hoc-i-goalir-katalog.md + `form` — плюс
два свойства запроса `ambiguous`/`unknown_concept`).

Эти тесты идут в сеть (единый модельный шлюз, `finengine.llm.openrouter`) и
тратят реальные вызовы — 102 запроса на прогон. Пропускаются, если
`OPENROUTER_API_KEY` не настроен (CI без секрета, или эта сессия на Windows-
машине без окружения — см. память `aifa-claw-dev-environment-access`), а не
падают неопределённо. Карточка требует «минимум в трёх независимых прогонах»
для недетерминированных случаев (методология C6) — `N_RUNS` ниже задаёт это
число; на каждом прогоне пороги проверяются заново, ни один прогон не
усредняется в другой («оценивается не один удачный результат, а
распределение, медиана и худшие случаи»).
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from finengine.config import settings
from finengine.skills.atoms.intent import parse_intent

QUERIES_PATH = Path(__file__).resolve().parent.parent / "golden" / "intent" / "queries.json"
N_RUNS = 3
STRUCTURED_THRESHOLD = 0.97  # карточка, пункт 1
SAFETY_THRESHOLD = 1.0  # карточка, пункт 2 — «в 100% соответствующих тестов»

pytestmark = pytest.mark.skipif(
    not settings.openrouter_api_key,
    reason="OPENROUTER_API_KEY не настроен — живая оценка intent.parse пропущена",
)


def _load_queries() -> list[dict]:
    return json.loads(QUERIES_PATH.read_text(encoding="utf-8"))


def _structured_row_correct(goal, row: dict) -> bool:
    if goal.intent_kind != row["expected_category"]:
        return False
    expected_concepts = row.get("expected_concepts")
    if expected_concepts is not None:
        if {t.concept for t in goal.targets} != set(expected_concepts):
            return False
    expected_role = row.get("expected_role")
    if expected_role is not None and goal.role != expected_role:
        return False
    return True


def _safety_row_correct(goal) -> bool:
    """Пункт 2: ничего не стало выдуманным `TargetNode` — неважно, ушло ли
    непонятое в `unresolved` или в `ambiguities`, лишь бы не в `targets`."""
    return goal.targets == []


async def _run_once() -> dict[str, float]:
    rows = _load_queries()
    structured_rows = [r for r in rows if "expected_output_kind" in r]
    safety_rows = [r for r in rows if r["expected_category"] in ("ambiguous", "unknown_concept")]

    structured_correct = 0
    for row in structured_rows:
        goal = await parse_intent(row["query"], language=row["language"])
        if _structured_row_correct(goal, row):
            structured_correct += 1

    safety_correct = 0
    for row in safety_rows:
        goal = await parse_intent(row["query"], language=row["language"])
        if _safety_row_correct(goal):
            safety_correct += 1

    return {
        "structured_accuracy": structured_correct / len(structured_rows),
        "safety_rate": safety_correct / len(safety_rows),
    }


@pytest.mark.parametrize("run_index", range(N_RUNS))
async def test_golden_intent_accuracy_meets_readiness_thresholds(run_index):
    result = await _run_once()
    assert result["structured_accuracy"] >= STRUCTURED_THRESHOLD, (
        f"прогон {run_index}: точность типа задачи/вида результата/концептов "
        f"{result['structured_accuracy']:.1%} < порога {STRUCTURED_THRESHOLD:.0%}"
    )
    assert result["safety_rate"] >= SAFETY_THRESHOLD, (
        f"прогон {run_index}: доля запросов, где неизвестный/неоднозначный "
        f"концепт НЕ стал выдуманным TargetNode, {result['safety_rate']:.1%} "
        f"< порога {SAFETY_THRESHOLD:.0%}"
    )
