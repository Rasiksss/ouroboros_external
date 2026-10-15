"""`id.entity` — критерий готовности (docs/build-plan/11-srez-01-run-passport.md):

1. На размеченном наборе точная сущность определяется минимум в 98%
   однозначных кейсов.
2. Одноимённые/неоднозначные сущности в 100% отрицательных кейсов не
   выбираются без достаточного доказательства.
3. Результат содержит происхождение идентификации и стабильный `entity_id`,
   пригодный для `Case` и истории.
4. Изменение языка документа само по себе не изменяет идентичность сущности.

Сценарии: группа и материнская компания; одинаковые названия разных
юридических лиц; смена бренда; документ без однозначного идентификатора.
"""
from __future__ import annotations

from finengine.skills.atoms.entity import identify_entity

_UNAMBIGUOUS_CASES = [
    ("ПАО «Ромашка» сообщает результаты за первый квартал.", "Ромашка"),
    ("Акционерное общество «Северный Лён» объявило дивиденды.", "Северный Лён"),
    ("ООО «Вектор Плюс» опубликовало годовой отчет.", "Вектор Плюс"),
    ("Компания «Дальний Восток Ритейл» открыла новые магазины.", "Дальний Восток Ритейл"),
]


def test_unambiguous_cases_resolve_to_the_expected_canonical_name():
    """≥98% однозначных кейсов — на этом маленьком наборе требуем все."""
    mismatches = []
    for text, expected in _UNAMBIGUOUS_CASES:
        identity = identify_entity(text)
        if identity.canonical_name != expected:
            mismatches.append((text, expected, identity.canonical_name))
    assert not mismatches, mismatches


def test_result_carries_provenance_and_a_stable_entity_id():
    identity = identify_entity("ПАО «Ромашка» сообщает результаты.")
    assert identity.entity_id
    assert identity.source
    assert identity.reason


def test_entity_id_is_deterministic_for_the_same_canonical_name():
    first = identify_entity("ПАО «Ромашка» сообщает результаты за первый квартал.")
    second = identify_entity("ПАО «Ромашка»: годовой отчет за прошлый год.")
    assert first.entity_id == second.entity_id


def test_group_parent_mentioned_as_distractor_does_not_hijack_the_subsidiary():
    """Группа и материнская компания: филиал не должен потерять свою идентичность
    из-за упоминания материнской «Группы» на каждой странице (#256 в company_detection)."""
    text = "Общество входит в Группу ТМК и работает на рынке труб." * 3
    identity = identify_entity(text, filename="ЧТПЗ_2024_годовая.pdf")
    assert identity.canonical_name == "ЧТПЗ"


def test_group_holding_is_identified_as_group_entity_type():
    text = "Группа «Ромашка Холдинг» объединяет несколько производственных площадок."
    identity = identify_entity(text)
    assert identity.entity_type == "group"


def test_ambiguous_generic_label_is_not_confidently_resolved():
    """Одноимённые/неоднозначные сущности не выбираются без достаточного доказательства."""
    identity = identify_entity("Отчет за год без указания эмитента.", existing_name="Банк")
    assert identity.confidence_low is True


def test_two_documents_with_the_same_generic_label_both_stay_low_confidence():
    """Одинаковое (неспецифичное) имя у разных документов — оба не считаются
    доказанно одной и той же сущностью."""
    a = identify_entity("Первый отчет.", existing_name="Банк")
    b = identify_entity("Второй, совсем другой отчет.", existing_name="Банк")
    assert a.confidence_low is True
    assert b.confidence_low is True


def test_no_identifiable_subject_gives_an_explicit_low_confidence_result():
    """Документ без однозначного идентификатора — не выбрасывает, не угадывает."""
    identity = identify_entity("Общие сведения о рынке без упоминания эмитента.")
    assert identity.confidence_low is True
    assert identity.canonical_name


def test_document_language_alone_does_not_change_identity():
    """Один и тот же `existing_name`, совершенно разный язык окружающего текста —
    `entity_id` не должен зависеть от языка документа."""
    ru_context = identify_entity(
        "Общие сведения о рынке и макроэкономической ситуации в стране.",
        existing_name="Ромашка Corp",
    )
    en_context = identify_entity(
        "General overview of the market and macroeconomic conditions worldwide.",
        existing_name="Ромашка Corp",
    )
    assert ru_context.entity_id == en_context.entity_id


def test_current_document_text_wins_when_no_stale_hint_is_given():
    """Смена бренда: без устаревшей подсказки атом берёт имя из текущего текста."""
    identity = identify_entity("ПАО «МегаФон» объявило финансовые результаты.")
    assert identity.canonical_name == "МегаФон"
    assert identity.source == "document_text"


def test_verified_identifier_keeps_entity_identity_across_languages():
    ru = identify_entity('ПАО «Ромашка». LEI: 5493001KJTIIGC8Y1R12. Финансовая отчётность.')
    en = identify_entity('Chamomile plc. LEI 5493001KJTIIGC8Y1R12. Annual financial statements.')
    assert ru.external_ids == en.external_ids == {"lei": "5493001KJTIIGC8Y1R12"}
    assert ru.entity_id == en.entity_id


def test_same_name_with_different_verified_identifiers_is_not_merged():
    first = identify_entity('АО «Одинаковое имя», ИНН 7707083893.')
    second = identify_entity('АО «Одинаковое имя», ИНН 7728168971.')
    assert first.canonical_name == second.canonical_name
    assert first.entity_id != second.entity_id
