"""`ref.norm` — карточка 19 (Срез 9, B10): нормативный/договорный порог как
типизированная база сравнения, с явным приоритетом источника, когда доступно
несколько (карточка: «нельзя смешивать эти источники без явного приоритета»).

**Два источника, оба — уже существующие механизмы, не изобретены здесь.**

1. **Проверенное `ConditionEvidence`** (`read.condition` + обязательный гейт
   `verify.citation`, брифинг сессии: «перед доверенным использованием
   ConditionEvidence проходит verify.citation») — ПРИОРИТЕТНЫЙ источник, когда
   условие ПОЛНОЕ (оператор и порог разобраны детерминированно, см.
   `read_condition.py`) и его цитата ПОДТВЕРЖДЕНА (`VerificationFinding(
   check_kind="citation", status="pass")` на тот же `evidence_id` — гейт
   вставляется планировщиком автоматически через `mandatory_gates`
   `read.condition`, см. `graph_atoms.py`).
2. **Версионированные данные заказчика** (методика ИЛИ справочник — карточка
   не разводит их механически, оба «уже готовые версionированные данные, не
   документ этого прогона»), переданные вызывающим как JSON в `NodeArgs.
   extra["norm_registry"]` — тот же приём, что `id.revision` получает
   `candidate_prior_documents`: постоянного реестра норм в этой сессии нет,
   атом ничего не ищет во внешнем хранилище сам.

Явный приоритет (не смешивание): если для концепта нашлось хотя бы одно
подтверждённое, полное `ConditionEvidence`, версионированный справочник для
ЭТОГО ЖЕ концепта не используется вовсе в том же вызове — оба источника не
складываются в одну неразличимую массу.

Область действия версионированного источника ограничена `NodeArgs.
policy_version` (запись без совпадающей версии политики отбрасывается —
тест «other customer's policy never used») и юрисдикцией: `jurisdiction_of`
(`analysis/external.py`) переиспользуется как есть — тот же предел точности
(регистрационные номера из текста, стандарт, ячейка профиля; не проверка по
внешнему реестру эмитентов, см. Nedodelano.md «Срез
6b», п.8), не новая догадка."""
from __future__ import annotations

import json

from finengine.contracts import ConditionEvidence, ReferenceValue, RunPassport, VerificationFinding
from finengine.skills.analysis.external import jurisdiction_of
from finengine.skills.spec.atom_registry import NodeArgs
from finengine.skills.spec.target import InputSpec

ATOM_VERSION = "1.0.0"

_KNOWN_OPERATORS = ("<", "<=", ">", ">=", "==", "!=")


def ref_norm_requires(node_args: NodeArgs, run_passport: RunPassport | None) -> tuple[InputSpec, ...]:
    if run_passport is None or not node_args.concept:
        return ()
    return (InputSpec(input_type="ConditionEvidence", concept=node_args.concept),)


def _is_numeric_complete(condition: ConditionEvidence) -> bool:
    return condition.operator is not None and condition.threshold_value is not None


def _citation_verified(condition: ConditionEvidence, findings: list[VerificationFinding]) -> bool:
    return any(
        f.check_kind == "citation" and f.source_ref == condition.evidence_id and f.status == "pass"
        for f in findings
    )


def _period_applies(source_period_ref: str | None, target_period_ref: str | None) -> bool:
    if not source_period_ref:
        return True  # период действия не указан в источнике — норма без временнОго ограничения
    return not target_period_ref or source_period_ref == target_period_ref


def _registry_entries(args: NodeArgs, concept: str) -> list[dict]:
    raw = args.extra.get("norm_registry") or ""
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except ValueError:
        return []
    if not isinstance(data, dict):
        return []
    entries = data.get(concept) or []
    if isinstance(entries, dict):
        entries = [entries]
    return [e for e in entries if isinstance(e, dict)] if isinstance(entries, list) else []


def ref_norm_executor(args: NodeArgs, ctx) -> list:
    passport = ctx.run_passport
    if passport is None:
        raise ValueError("ref.norm: RunPassport не зафиксирован — узел не должен был стать ready")
    concept = args.concept or ""
    deps = ctx.dependencies()
    conditions = [e for e in deps if isinstance(e, ConditionEvidence) and e.concept == concept]
    findings = [e for e in deps if isinstance(e, VerificationFinding)]
    entity_jurisdiction = jurisdiction_of(passport)

    from_condition: list[ReferenceValue] = []
    for condition in conditions:
        if not _is_numeric_complete(condition):
            continue  # неполное условие не становится числовым порогом
        if not _citation_verified(condition, findings):
            continue  # цитата не подтверждена verify.citation — не доверяем
        if not _period_applies(condition.period_ref, args.period_ref):
            continue
        from_condition.append(ReferenceValue(
            concept=concept, source_kind="norm", period_ref=condition.period_ref or args.period_ref,
            unit=condition.unit, operator=condition.operator, value=condition.threshold_value,
            basis=condition.basis, source_evidence_id=condition.evidence_id,
            source_version=condition.document_version, jurisdiction=entity_jurisdiction,
            selection_reason=(
                f"договорное/нормативное условие {condition.evidence_id!r}, цитата подтверждена verify.citation"
            ),
        ))

    if from_condition:
        # Явный приоритет (карточка: «нельзя смешивать источники без явного
        # приоритета») — подтверждённое ConditionEvidence предпочтено
        # версionированному справочнику для этого концепта в этом вызове.
        return from_condition

    out: list[ReferenceValue] = []
    for entry in _registry_entries(args, concept):
        entry_policy_version = str(entry.get("policy_version") or "")
        if entry_policy_version and entry_policy_version != (args.policy_version or ""):
            continue  # чужая версия политики заказчика не подмешивается
        entry_jurisdiction = str(entry.get("jurisdiction") or "") or entity_jurisdiction
        if entity_jurisdiction and entry_jurisdiction and entry_jurisdiction != entity_jurisdiction:
            continue  # норма другой юрисдикции не используется
        operator = entry.get("operator")
        value = entry.get("value")
        if operator not in _KNOWN_OPERATORS or not isinstance(value, (int, float)) or isinstance(value, bool):
            continue
        entry_period = entry.get("period_ref")
        if not _period_applies(entry_period, args.period_ref):
            continue
        version = str(entry.get("version") or "").strip()
        if not version:
            continue  # версия нормы обязательна (карточка: «источник, дату/версию»)
        out.append(ReferenceValue(
            concept=concept, source_kind="norm", period_ref=entry_period, unit=entry.get("unit"),
            operator=operator, value=float(value), basis=entry.get("basis"),
            source_citation=str(entry.get("source_ref") or "") or None, source_version=version,
            jurisdiction=entry_jurisdiction or None,
            selection_reason=f"версионированная норма заказчика/справочника, версия {version!r}",
        ))
    return out


__all__ = ["ATOM_VERSION", "ref_norm_requires", "ref_norm_executor"]
