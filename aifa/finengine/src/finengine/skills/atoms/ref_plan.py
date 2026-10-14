"""`ref.plan` — карточка 19 (Срез 9, B10): плановое/целевое значение
заказчика как типизированная база сравнения — с точным источником, версией,
периодом, единицей, без подмены прошлым периодом/нормативом/типичным
значением при отсутствии плана (карточка: «отсутствующий параметр в 100%
отрицательных тестов не заменяется типичным значением»).

**Почему не поиск среди `ctx.dependencies()`, в отличие от `ref.prior`.**
`ref.prior` находит вторую сторону среди уже извлечённых `MetricEvidence`
ТОГО ЖЕ прогона, потому что текущий и предыдущий период одного показателя
почти всегда приходят ИЗ ОДНОГО документа (см. докстринг `ref_prior.py`).
План заказчика — как правило, ДРУГОЙ физический документ, а Срез 8 уже
честно зафиксировал архитектурный предел: каждый документ проходит СВОЙ
ОТДЕЛЬНЫЙ однодокументный прогон, агрегируемый постфактум (`document_set.py`,
Nedodelano.md «Срез 8», п.2) — `ExecutionContext` одного прогона не видит
evidence другого документа. Настоящая интеграция «извлечь план из его
собственного документа и передать сюда напрямую» требует того же
межпрогонного связывания, которое Срез 8 сознательно отложил до
персистентного `Case` (Срез 10) — не переизобретаем это здесь.

**Что делает атом сегодня.** Принимает УЖЕ ИЗВЛЕЧЁННОЕ (обычными атомами
чтения в отдельном прогоне документа плана — карточка: «если источник
текстовый, локализация и чтение идут обычными атомами») плановое значение как
готовые данные вызывающего (`NodeArgs.extra["plan_entry"]`, JSON) — тот же
приём, что `id.revision` получает `candidate_prior_documents`: атом сам
ничего не ищет, ему называют уже готовый результат. Отсутствие записи или её
неполнота (нет источника/версии) — явное «план отсутствует», не эвристика."""
from __future__ import annotations

import json

from finengine.contracts import ReferenceValue, RunPassport
from finengine.skills.spec.atom_registry import NodeArgs
from finengine.skills.spec.target import InputSpec

ATOM_VERSION = "1.0.0"


def ref_plan_requires(node_args: NodeArgs, run_passport: RunPassport | None) -> tuple[InputSpec, ...]:
    if run_passport is None or not node_args.concept:
        return ()
    return ()  # плановое значение приходит через NodeArgs.extra, не граф-зависимость


def ref_plan_executor(args: NodeArgs, ctx) -> list:
    if ctx.run_passport is None:
        raise ValueError("ref.plan: RunPassport не зафиксирован — узел не должен был стать ready")
    concept = args.concept or ""
    raw = args.extra.get("plan_entry") or ""
    if not raw:
        # Ни один документ прогона не назначен планом — план отсутствует,
        # не подменяется угадыванием (карточка: «missing plan is missing»).
        return []
    try:
        entry = json.loads(raw)
    except ValueError:
        return []
    if not isinstance(entry, dict):
        return []

    entry_policy_version = str(entry.get("policy_version") or "")
    if entry_policy_version and entry_policy_version != (args.policy_version or ""):
        # Чужая версия политики заказчика не подмешивается (тест «other
        # customer's policy never used»).
        return []

    value = entry.get("value")
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return []

    source_ref = str(entry.get("source_ref") or "").strip()
    version = str(entry.get("version") or "").strip()
    if not source_ref or not version:
        # Карточка: «источник и версия планового значения сохраняются в
        # доказательной цепочке» — без них план не признаётся достаточно
        # документированным, чтобы стать `ReferenceValue`.
        return []

    return [ReferenceValue(
        concept=concept, source_kind="plan", period_ref=entry.get("period_ref"),
        unit=entry.get("unit"), value=float(value), basis=entry.get("basis"),
        source_citation=source_ref, source_version=version,
        selection_reason=f"плановое значение заказчика из {source_ref!r}, версия {version!r}",
    )]


__all__ = ["ATOM_VERSION", "ref_plan_requires", "ref_plan_executor"]
