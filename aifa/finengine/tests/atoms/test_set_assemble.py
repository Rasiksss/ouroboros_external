"""`set.assemble` — карточка 18 (Срез 8, B9). Каждый пункт критерия готовности
получает отдельный тест; минимальный набор сценариев из карточки: один файл
с двумя периодами против двух файлов, два эмитента, исправленная отчётность,
отчёт + пресс-релиз, случайно приложенный документ другого эмитента."""
from __future__ import annotations

from finengine.contracts import DocumentCandidate, EntityIdentity, RevisionInfo
from finengine.skills.atoms.document_set import assemble_document_set


def _entity(name: str, *, entity_id: str | None = None, confidence_low: bool = False) -> EntityIdentity:
    return EntityIdentity(
        entity_id=entity_id or f"ent-{name.lower()}",
        canonical_name=name, raw_name=name, confidence_low=confidence_low,
    )


def _doc(doc_id: str, entity: EntityIdentity, *, period_ref: str | None, doc_type: str = "report",
         revision: RevisionInfo | None = None, version: str | None = None) -> DocumentCandidate:
    return DocumentCandidate(
        document_id=doc_id, document_version=version or f"{doc_id}-v1", entity=entity,
        period_ref=period_ref, doc_type=doc_type, revision=revision,
    )


def test_ordinary_single_report_analysis_needs_only_one_document():
    severstal = _entity("Северсталь")
    candidates = [_doc("doc-1", severstal, period_ref="FY2024")]

    result = assemble_document_set(candidates, run_id="run-1", required_entities=1)

    assert result.completeness == "complete"
    assert [m.document_id for m in result.members] == ["doc-1"]
    assert result.rejected == []


def test_one_file_with_two_periods_is_not_confused_with_two_corroborating_documents():
    """Обязательный тест карточки: «один файл, содержащий два периода» против
    «два файла». Один документ с одним `period_ref` (даже если внутри него
    есть сравнительная колонка на уровне отдельных фактов) не удовлетворяет
    требованию ДВУХ независимых документов на разные периоды — это разные
    инварианты, и `set.assemble` не должен их путать."""
    severstal = _entity("Северсталь")
    one_file = [_doc("doc-1", severstal, period_ref="FY2024")]

    result_one_file = assemble_document_set(
        one_file, run_id="run-1", required_entities=1, required_period_count=2,
    )
    assert result_one_file.completeness == "incomplete"
    assert any("период" in m for m in result_one_file.missing)

    two_files = [
        _doc("doc-1", severstal, period_ref="FY2024"),
        _doc("doc-2", severstal, period_ref="FY2023"),
    ]
    result_two_files = assemble_document_set(
        two_files, run_id="run-1", required_entities=1, required_period_count=2,
    )
    assert result_two_files.completeness == "complete"
    assert len(result_two_files.members) == 2


def test_two_issuers_are_grouped_as_two_distinct_entities():
    severstal, nlmk = _entity("Северсталь"), _entity("НЛМК")
    candidates = [
        _doc("doc-1", severstal, period_ref="FY2024"),
        _doc("doc-2", nlmk, period_ref="FY2024"),
    ]

    result = assemble_document_set(
        candidates, run_id="run-1", expected_entity_refs=("Северсталь", "НЛМК"), required_entities=2,
    )

    assert result.completeness == "complete"
    assert {m.entity_id for m in result.members} == {severstal.entity_id, nlmk.entity_id}
    assert result.rejected == []


def test_report_and_press_release_of_the_same_entity_are_both_kept():
    severstal = _entity("Северсталь")
    candidates = [
        _doc("doc-1", severstal, period_ref="FY2024", doc_type="report"),
        _doc("doc-2", severstal, period_ref="FY2024", doc_type="press_release"),
    ]

    result = assemble_document_set(
        candidates, run_id="run-1", required_entities=1, required_doc_types=("report", "press_release"),
    )

    assert result.completeness == "complete"
    assert {m.doc_type for m in result.members} == {"report", "press_release"}


def test_restated_report_coexists_with_the_original_not_overwritten():
    severstal = _entity("Северсталь")
    original = _doc("doc-2023", severstal, period_ref="FY2023")
    restated = _doc(
        "doc-2024", severstal, period_ref="FY2023", doc_type="report",
        revision=RevisionInfo(revision_id="rev-1", document_id="doc-2024",
                               revision_kind="restated", supersedes="doc-2023"),
    )

    result = assemble_document_set([original, restated], run_id="run-1", required_entities=1)

    assert result.completeness == "complete"
    assert len(result.members) == 2, "исправленная версия не должна вытеснять исходную из набора"
    by_id = {m.document_id: m for m in result.members}
    assert by_id["doc-2023"].revision is None
    assert by_id["doc-2024"].revision.revision_kind == "restated"
    assert by_id["doc-2024"].revision.supersedes == "doc-2023"


def test_accidentally_attached_other_issuer_document_is_rejected_not_joined():
    severstal, unrelated = _entity("Северсталь"), _entity("Совершенно Другая Компания")
    candidates = [
        _doc("doc-1", severstal, period_ref="FY2024"),
        _doc("doc-2", unrelated, period_ref="FY2024"),
    ]

    result = assemble_document_set(candidates, run_id="run-1", expected_entity_refs=("Северсталь",))

    assert [m.document_id for m in result.members] == ["doc-1"]
    assert len(result.rejected) == 1
    assert result.rejected[0].document_id == "doc-2"
    assert result.rejected[0].reason == "entity_mismatch"


def test_low_confidence_identity_is_rejected_not_silently_joined():
    """Карточка, краевой случай: «документ с неопределённой сущностью должен
    создать конфликт/необходимость разрешения, а не молча присоединиться»."""
    unsure = _entity("Отчёт", confidence_low=True)
    candidates = [_doc("doc-1", unsure, period_ref="FY2024")]

    result = assemble_document_set(candidates, run_id="run-1", required_entities=1)

    assert result.members == []
    assert result.rejected[0].reason == "unknown_identity"
    assert result.completeness == "incomplete"


def test_arrival_order_does_not_decide_membership_no_first_pdf_heuristic():
    """Не найдено ни одной реальной эвристики «первый прикреплённый PDF» в
    рабочем пути репозитория (Nedodelano.md, Срез 8) — вместо удаления
    несуществующего кода этот тест фиксирует свойство, которое такую
    эвристику делает невозможной здесь: ПЕРВЫЙ по порядку кандидат из
    ожидаемого набора отклоняется, если он не подтверждён, а совпадающий с
    ожиданием документ, поданный ПОЗЖЕ, всё равно становится членом набора —
    порядок поступления не участвует в решении вовсе."""
    severstal, unrelated = _entity("Северсталь"), _entity("Совершенно Другая Компания")
    candidates = [
        _doc("doc-first", unrelated, period_ref="FY2024"),
        _doc("doc-second", severstal, period_ref="FY2024"),
    ]

    result = assemble_document_set(candidates, run_id="run-1", expected_entity_refs=("Северсталь",))

    assert [m.document_id for m in result.members] == ["doc-second"]
    assert result.rejected[0].document_id == "doc-first"


def test_facts_from_different_documents_do_not_overwrite_each_other_by_concept_key():
    """Обязательный тест карточки: «факты не перезаписывают друг друга по
    ключу концепта». `DocumentSet.members` — список, не словарь `{concept:
    value}`: два документа одного эмитента и периода, но разной ревизии, оба
    остаются адресуемыми по своему `document_id`."""
    severstal = _entity("Северсталь")
    original = _doc("doc-2023", severstal, period_ref="FY2023")
    restated = _doc(
        "doc-2024r", severstal, period_ref="FY2023",
        revision=RevisionInfo(revision_id="rev-2", document_id="doc-2024r",
                               revision_kind="restated", supersedes="doc-2023"),
    )

    result = assemble_document_set([original, restated], run_id="run-1", required_entities=1)

    document_ids = [m.document_id for m in result.members]
    assert len(document_ids) == len(set(document_ids)) == 2
