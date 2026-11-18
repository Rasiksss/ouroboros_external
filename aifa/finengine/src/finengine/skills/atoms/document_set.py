"""`set.assemble` — карточка 18 (Срез 8, B9, docs/build-plan/
18-srez-08-struktury-i-neskolko-dokumentov.md): собрать типизированный
`DocumentSet` по подтверждённой сущности/периоду/типу — не по имени файла или
порядку прикрепления — и явно проверить его полноту/конфликты.

**Полнота — декларативная, не «если два документа».** Три независимых
параметра (`required_entities`/`required_doc_types`/`required_period_count`)
задаются вызывающим (Target/GoalType — «сравнение двух сущностей требует
двух субъектов, сверка — двух источников, обычный анализ — одного документа»,
карточка `set.assemble`) и применяются ОДНОЙ и той же функцией для любого их
сочетания — не отдельной веткой кода на каждый сценарий.

**Отказ — явная запись, не молчание.** Документ с неподтверждённой
(`confidence_low`) идентичностью или с идентичностью, не входящей в ожидаемых
субъектов, попадает в `DocumentSet.rejected` с указанной причиной — карточка
прямо запрещает «молча присоединиться» ИЛИ «молча пропасть», и оба случая
здесь дают наблюдаемую запись."""
from __future__ import annotations

from collections.abc import Sequence

from finengine.contracts import DocumentCandidate, DocumentSet, DocumentSetMember, DocumentSetRejection

ATOM_VERSION = "1.0.0"


def _match_entity_ref(ref: str, entity) -> bool:
    """Свободный текстовый хинт (`Target.entity_ref`/`params`) против
    установленной идентичности документа — тот же принцип нестрогого
    сопоставления, что уже применяет `identify_entity`/`_prefer_detected_
    company_name` для выбора между известным и найденным именем, не новый
    fuzzy-matcher: сравнение без учёта регистра, по вхождению в любую сторону,
    чтобы «Северсталь» совпало с «ПАО Северсталь» и наоборот."""
    ref_cf = ref.strip().casefold()
    if not ref_cf:
        return False
    for candidate_name in (entity.canonical_name, entity.raw_name):
        name_cf = candidate_name.strip().casefold()
        if name_cf and (ref_cf in name_cf or name_cf in ref_cf):
            return True
    return False


def assemble_document_set(
    candidates: Sequence[DocumentCandidate],
    *,
    run_id: str,
    expected_entity_refs: Sequence[str] = (),
    required_entities: int = 1,
    required_doc_types: Sequence[str] = (),
    required_period_count: int = 1,
) -> DocumentSet:
    """Группировать `candidates` по подтверждённой сущности/периоду/типу и
    проверить полноту. `expected_entity_refs` пуст → группировка идёт по
    голому `entity.entity_id` без внешнего хинта (нет обязанности отклонять
    незнакомую сущность — вызывающий не назвал, кого ждёт); непуст → любой
    кандидат, не совпавший ни с одним ожидаемым субъектом, отклоняется явно
    (случайно приложенный документ другого эмитента, карточка `set.assemble`).
    """
    members: list[DocumentSetMember] = []
    rejected: list[DocumentSetRejection] = []

    for cand in candidates:
        if cand.entity.confidence_low:
            rejected.append(DocumentSetRejection(
                document_id=cand.document_id, document_version=cand.document_version,
                reason="unknown_identity",
                detail=f"идентичность субъекта не подтверждена: {cand.entity.reason}",
            ))
            continue
        if expected_entity_refs and not any(
            _match_entity_ref(ref, cand.entity) for ref in expected_entity_refs
        ):
            rejected.append(DocumentSetRejection(
                document_id=cand.document_id, document_version=cand.document_version,
                reason="entity_mismatch",
                detail=(
                    f"{cand.entity.canonical_name!r} не входит в ожидаемых субъектов "
                    f"{list(expected_entity_refs)!r}"
                ),
            ))
            continue
        members.append(DocumentSetMember(
            document_id=cand.document_id, document_version=cand.document_version,
            entity_id=cand.entity.entity_id, canonical_name=cand.entity.canonical_name,
            period_ref=cand.period_ref, doc_type=cand.doc_type, revision=cand.revision,
        ))

    by_entity: dict[str, list[DocumentSetMember]] = {}
    for member in members:
        by_entity.setdefault(member.entity_id, []).append(member)

    missing: list[str] = []
    distinct_entities = len(by_entity)
    if distinct_entities < required_entities:
        missing.append(
            f"нужно ещё {required_entities - distinct_entities} подтверждённых субъект(а/ов) "
            f"(сейчас {distinct_entities} из {required_entities})"
        )

    for group in by_entity.values():
        name = group[0].canonical_name
        if required_doc_types:
            present_types = {m.doc_type for m in group}
            for wanted in required_doc_types:
                if wanted not in present_types:
                    missing.append(f"{name}: отсутствует документ типа {wanted!r}")
        if required_period_count > 1:
            distinct_periods = {m.period_ref for m in group if m.period_ref}
            if len(distinct_periods) < required_period_count:
                missing.append(
                    f"{name}: нужно ещё {required_period_count - len(distinct_periods)} период(а/ов) "
                    f"(сейчас {len(distinct_periods)} из {required_period_count})"
                )

    return DocumentSet(
        run_id=run_id, members=members, rejected=rejected,
        required_entities=required_entities,
        completeness="complete" if not missing else "incomplete",
        missing=missing,
    )


__all__ = ["ATOM_VERSION", "assemble_document_set"]
