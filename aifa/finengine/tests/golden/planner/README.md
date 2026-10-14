# tests/golden/planner/

Эталонный набор для `GraphPlanner`/`PlanFeasibility` (Срез 3 и далее),
C0.1: «ручные графы для основных сценариев и генератор синтетических графов
для проверки свойств планировщика».

## `manual/*.json`

Шесть сценариев, каждый — `{scenario_id, description, target, plan_graph,
expected_feasibility, ...}`, где `target` и `plan_graph` десериализуются
напрямую в `finengine.contracts.TargetNode` / `PlanGraph`:

1. `01-single-scalar-metric` — линейная цепочка (точечный запрос).
2. `02-full-analysis-fan-in` — два `extract.scalar` сходятся в один расчёт
   (кандидат на пакетное объединение, C9).
3. `03-form-output-dgr` — форма с одной недоступной строкой, которая обязана
   остаться в выводе, а не выброситься (решение №8).
4. `04-classification-gated-expansion` — состав узлов ниже `identify.classify`
   зависит от результата классификации (02, §3).
5. `05-unreachable-target` — концепт без атома-производителя → `feasible=false`.
6. `06-would-be-cyclic-negative` — сознательно испорченный цикл `calc.a ↔
   calc.b`. `PlanGraph` как ТИП его не отвергает (Фаза 0 не проверяет
   ацикличность — это работа `GraphPlanner`), но `expected_feasibility.feasible`
   зафиксирован как `false`: это отрицательный фикстур для будущего теста
   планировщика, а не пример валидного плана.

## `synthetic_generator.py`

`generate_random_dag(num_nodes, seed)` — граф, ацикличный ПО ПОСТРОЕНИЮ
(каждый узел требует только уже созданные более ранние узлы).
`generate_cyclic_graph(seed)` — заведомо испорченный граф для проверки
самого детектора. `is_acyclic()` и `all_requires_resolve_to_known_nodes()` —
свойства, которые будущий `GraphPlanner` обязан поддерживать; сейчас они
проверяют сам генератор (`tests/test_golden_planner_set.py`), а не планировщик.

## Статус

6 ручных сценариев + генератор с самопроверкой на 3 инвариантах. Полностью
синтетический набор — не требует внешних отчётов.
