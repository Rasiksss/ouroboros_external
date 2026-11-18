"""`intent.contract` — механическая проверка достижимости цели (карточка в
docs/build-plan/13-srez-03-graph-planner-executor.md, разделы «Атом, вводимый
здесь»). Отвечает «можно ли собрать», не «что финансово хорошо или плохо» —
код не выбирает финансово «лучший» путь и не превращает `needs_input` в отказ
пользователю (инженерный принцип карточки).

«Проверка достижимости и `GraphPlanner` должны использовать один и тот же
механизм контрактов» (раздел «Как встроить») — здесь это не пожелание, а
буквально одна функция, целиком построенная поверх `GraphPlanner.plan()`. Нет
отдельного дерева правил «нужен ли этот вход» — если бы оно было, оно рано или
поздно разошлось бы с тем, что реально строит планировщик (ровно тот класс
дефекта, который уже случился в `RunContext`/`validate_spec`, см. `skills/spec/
runner.py`, и который эта сессия обязана не повторить).
"""
from __future__ import annotations

from finengine.contracts import PlanFeasibility, PlanPolicy, RunPassport
from finengine.skills.spec.atom_registry import AtomRegistry
from finengine.skills.spec.graph_planner import AvailableIndex, GraphPlanner
from finengine.skills.spec.target import Target


def check_feasibility(
    goal: Target, *, registry: AtomRegistry, policy: PlanPolicy,
    available: AvailableIndex | None = None, run_passport: RunPassport | None = None,
) -> PlanFeasibility:
    """`PlanFeasibility` для одной цели, без исполнения ни одного атома и без
    модельных вызовов (критерий готовности атома, пункт «результат одинаков
    для одинаковых входов... модельные вызовы отсутствуют»).

    `reason`, когда `feasible=False`, начинается с одной из трёх машинных
    категорий карточки — `needs_input:`, `ambiguous_producer:` или `cycle:` —
    так вызывающий код может ветвиться по категории, не парся свободный
    текст целиком. `PlanFeasibility` (A0) — плоский контракт без отдельного
    поля статуса; кодировать категорию в начале `reason` дешевле, чем менять
    форму контракта, от которой уже зависят golden-фикстуры Фазы 0.
    """
    result = GraphPlanner(registry=registry, policy=policy).plan(goal, available=available, run_passport=run_passport)
    if result.feasibility.feasible:
        return result.feasibility

    if result.unreachable:
        detail = "; ".join(f"{u.concept}: {u.reason}" for u in result.unreachable)
        reason = f"needs_input: {detail}"
    elif result.choice_points:
        detail = "; ".join(f"{c.concept} -> {', '.join(c.candidate_atoms)}" for c in result.choice_points)
        reason = f"ambiguous_producer: {detail}"
    else:
        # Ни недостижимых концептов, ни точек выбора — значит план не построился
        # из-за цикла (`GraphPlanner.plan()` сам подставляет `feasible=False` с
        # человекочитаемой причиной в этом случае, пустые unreachable/choice_points).
        reason = f"cycle: {result.feasibility.reason}"

    return PlanFeasibility(
        feasible=False, unreachable_targets=result.feasibility.unreachable_targets, reason=reason,
    )


__all__ = ["check_feasibility"]
