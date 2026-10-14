"""`id.revision` — карточка 18 (Срез 8, B9). Каждый пункт критерия готовности
получает отдельный тест (положительный/отрицательный), методология C0/C5:
детерминированный маркер сначала, модель под жёсткой схемой — только после
маркера, парсер отбрасывает то, что модель не имеет права утверждать."""
from __future__ import annotations

from finengine.contracts import RevisionInfo
from finengine.skills.atoms.id_revision import detect_revision, find_revision_markers


def test_plain_text_without_any_marker_has_no_revision():
    text = "Выручка за отчётный период составила 1000 млн руб. Прибыль выросла на 5%."
    assert find_revision_markers(text) == []
    assert detect_revision(text, document_id="doc-1") is None


def test_isolated_marker_far_from_comparative_context_is_not_tagged():
    """Карточка, краевой случай: «нельзя объявлять весь документ
    пересмотренным только из-за одного слова в отдалённом примечании» — слово
    "пересмотрен" встречается, но не рядом со сравнительным периодом/строкой
    отчётности, а в примечании про политику вознаграждения совета директоров."""
    text = (
        "Примечание 42. Совет директоров пересмотрел политику вознаграждения "
        "членов правления в текущем году."
    )
    assert find_revision_markers(text) == []

    def _llm_should_not_be_called(prompt, system):  # noqa: ARG001
        raise AssertionError("модель не должна вызываться без маркера в сравнительном контексте")

    assert detect_revision(text, document_id="doc-1", llm_json_call=_llm_should_not_be_called) is None


def test_marker_near_comparative_context_reaches_the_model_but_model_declines():
    text = (
        "Показатели за сравнительный период были пересчитаны в связи с "
        "изменением классификации, см. Примечание 3."
    )
    hits = find_revision_markers(text)
    assert len(hits) == 1
    assert "сравнительный период" in hits[0].context

    def _fake_llm(prompt, system):  # noqa: ARG001
        return {"is_revision": False, "reason": "маркер не привязан к конкретной статье"}

    assert detect_revision(text, document_id="doc-1", llm_json_call=_fake_llm) is None


def test_restated_document_references_a_known_prior_candidate():
    text = (
        "Сравнительные данные за 2023 год были пересчитаны (restated) в связи "
        "с исправлением ошибки в учёте резервов."
    )
    candidates = [{"document_id": "doc-2023", "canonical_name": "ПАО Ромашка", "period_ref": "FY2023"}]

    def _fake_llm(prompt, system):  # noqa: ARG001
        assert "doc-2023" in prompt
        return {
            "is_revision": True, "revision_kind": "restated",
            "supersedes_document_id": "doc-2023", "reason": "исправление ошибки в учёте резервов",
        }

    result = detect_revision(
        text, document_id="doc-2024", candidate_prior_documents=candidates, llm_json_call=_fake_llm,
    )
    assert result == RevisionInfo(
        revision_id="rev-doc-2024", document_id="doc-2024",
        revision_kind="restated", supersedes="doc-2023",
    )


def test_model_cannot_invent_a_supersedes_id_outside_the_candidate_list():
    """Вторая линия защиты: `supersedes_document_id`, не входящий в
    предложенные кандидаты, отбрасывается — модель не имеет права назвать
    документ, о котором вызывающий её не спрашивал."""
    text = "Данные за прошлый год переклассифицированы для сопоставимости показателей."

    def _fake_llm(prompt, system):  # noqa: ARG001
        return {
            "is_revision": True, "revision_kind": "reissued",
            "supersedes_document_id": "doc-invented-by-model", "reason": "переклассификация",
        }

    result = detect_revision(text, document_id="doc-1", candidate_prior_documents=[], llm_json_call=_fake_llm)
    assert result is not None
    assert result.supersedes is None


def test_partial_reclassification_without_a_specific_line_is_still_tagged():
    """Карточка, минимальный тестовый сценарий: «упоминание пересмотра без
    изменения конкретной строки» — пересмотр реален (модель подтверждает), но
    `supersedes`/конкретная строка неизвестны — это законный неполный
    результат, не повод его выбросить."""
    text = "В сравнительном периоде отдельные статьи были переклассифицированы без изменения итоговых сумм."

    def _fake_llm(prompt, system):  # noqa: ARG001
        return {
            "is_revision": True, "revision_kind": "reissued",
            "supersedes_document_id": None, "reason": "реклассификация статей без изменения итога",
        }

    result = detect_revision(text, document_id="doc-1", llm_json_call=_fake_llm)
    assert result is not None
    assert result.revision_kind == "reissued"
    assert result.supersedes is None


def test_unknown_revision_kind_from_the_model_is_rejected():
    text = "Показатели сравнительного периода были скорректированы."

    def _fake_llm(prompt, system):  # noqa: ARG001
        return {"is_revision": True, "revision_kind": "corrected", "supersedes_document_id": None}

    assert detect_revision(text, document_id="doc-1", llm_json_call=_fake_llm) is None


def test_model_failure_does_not_invent_a_revision():
    text = "Показатели сравнительного периода были пересмотрены."

    def _raising_llm(prompt, system):  # noqa: ARG001
        raise RuntimeError("модельный шлюз недоступен")

    assert detect_revision(text, document_id="doc-1", llm_json_call=_raising_llm) is None
