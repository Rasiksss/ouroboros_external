"""`read.entities` — критерий готовности атома (docs/build-plan/
15b-srez-05b-read-structure-quote-entities.md, карточка `read.entities`):

1. Не менее 95% сущностей/отношений имеют правильный тип и направление связи
   (замер на golden-наборе — вне этой сессии; здесь — точные примеры того же
   свойства).
2. Неупомянутая сущность не создаётся моделью (проверяется тем, что адаптер
   строго читает событие экстрактора, не домысливает сущностей).
3. Каждая связь имеет доказательство из документа (page/source_ref).
4. Дубли дедуплицируются без потери разных отношений (наследуется от
   `_normalise_consolidated_group_rows`, вызываемого без изменений).

🔴 Известное ограничение сессии (см. докстринг `read_entities.py`):
`_extract_consolidated_group_full_context` сегодня физически возвращает
только дочерние общества. Сценарии «совместное предприятие»/«ассоциированная
компания» поэтому проверяются на уровне адаптера
(`_entity_relations_from_group_event`) напрямую, а не через живой вызов
модели — адаптер не отбрасывает эти виды отношений молча, даже если
экстрактор их сегодня не находит."""
from __future__ import annotations

import pytest

from finengine.contracts import EntityRelation
from finengine.skills.bulk_extractor import consolidated_group
from finengine.skills.atoms.read_entities import _entity_relations_from_group_event, read_entities_batch


def _event(name, *, relation="дочерняя компания", period_values, page=17):
    return {
        "type": "metric", "enum": "CONSOLIDATED_GROUP_OWNERSHIP", "name_ru": name,
        "period_values": period_values, "page": page, "segment": name,
        "dimension": "consolidated_group", "group_relation": relation,
    }


def test_subsidiary_event_becomes_one_relation_per_period():
    event = _event("ООО Дочка", period_values=[
        {"period": "2024", "value": "100,00%", "page": 17},
        {"period": "2023", "value": "80,00%", "page": 17},
    ])
    relations = _entity_relations_from_group_event(
        event, object_entity="ПАО Ромашка", document_id="doc-1", document_version="v1",
    )
    assert {r.period_ref for r in relations} == {"2024", "2023"}
    by_period = {r.period_ref: r for r in relations}
    assert by_period["2024"].ownership_percent == pytest.approx(100.0)
    assert by_period["2023"].ownership_percent == pytest.approx(80.0)
    assert all(isinstance(r, EntityRelation) for r in relations)
    assert all(r.subject_entity == "ООО Дочка" and r.object_entity == "ПАО Ромашка" for r in relations)
    assert all(r.direction == "object_owns_subject" for r in relations)
    assert all(r.relation == "дочерняя компания" for r in relations)
    assert all(r.page == 17 for r in relations)


def test_ownership_above_100_percent_is_preserved_not_rejected():
    event = _event("ООО Сверхдоля", period_values=[{"period": "2024", "value": "100,01%", "page": 5}])
    [relation] = _entity_relations_from_group_event(
        event, object_entity="ПАО Ромашка", document_id="doc-1", document_version="v1",
    )
    assert relation.ownership_percent == pytest.approx(100.01)
    assert relation.ownership_percent_raw == "100,01%"


def test_missing_share_keeps_the_relation_without_a_number():
    event = _event("ООО Без доли", period_values=[{"period": "2024", "value": "-", "page": 5}])
    [relation] = _entity_relations_from_group_event(
        event, object_entity="ПАО Ромашка", document_id="doc-1", document_version="v1",
    )
    assert relation.ownership_percent is None
    assert relation.ownership_percent_raw == "-"


def test_associate_relation_kind_is_not_silently_dropped():
    event = _event(
        "ООО Ассоциат", relation="associate", period_values=[{"period": "2024", "value": "30,00%", "page": 12}],
    )
    [relation] = _entity_relations_from_group_event(
        event, object_entity="ПАО Ромашка", document_id="doc-1", document_version="v1",
    )
    assert relation.relation == "associate"


def test_joint_venture_relation_kind_is_not_silently_dropped():
    event = _event(
        "ООО СП", relation="joint_venture", period_values=[{"period": "2024", "value": "50,00%", "page": 12}],
    )
    [relation] = _entity_relations_from_group_event(
        event, object_entity="ПАО Ромашка", document_id="doc-1", document_version="v1",
    )
    assert relation.relation == "joint_venture"


def test_unknown_relation_kind_is_kept_with_its_own_label():
    event = _event("ООО Неясно", relation="", period_values=[{"period": "2024", "value": "10,00%", "page": 1}])
    [relation] = _entity_relations_from_group_event(
        event, object_entity="ПАО Ромашка", document_id="doc-1", document_version="v1",
    )
    assert relation.relation == "unknown"


def test_event_without_a_name_produces_no_relation():
    event = _event("", period_values=[{"period": "2024", "value": "10%"}])
    assert _entity_relations_from_group_event(
        event, object_entity="X", document_id="doc-1", document_version="v1",
    ) == []


def test_one_physical_call_reused_from_the_existing_extractor(monkeypatch):
    """`read_entities_batch` вызывает `_extract_consolidated_group_full_
    context` БЕЗ изменений — этот тест не подставляет отбор кандидатов, а
    даёт РЕАЛЬНЫЙ текст, который его штатная эвристика (`_flush_group_table_
    block`) сама выбирает как таблицу состава группы."""
    calls: list[dict] = []

    async def _fake(**kwargs):
        calls.append(kwargs)
        import json
        raw = json.dumps({"companies": [
            {"candidate_id": 1, "company_name": "ООО Дочка", "relation": "subsidiary",
             "values": {"31 декабря 2024 г.": "100,00%"}, "unit_text": "%", "page": 17},
        ]})
        return raw, {}

    monkeypatch.setattr(consolidated_group.llm, "complete_with_usage", _fake)

    document_text = (
        "===PAGE 17===\n"
        "Примечание. Основные дочерние общества группы\n"
        "| Наименование | Доля участия 31 декабря 2024 г. |\n"
        "| ООО Дочка | 100,00% |\n"
    )
    relations = read_entities_batch(
        document_id="doc-1", document_version="v1", document_text=document_text,
        period_current="31.12.2024", period_prev="31.12.2023", company_display="ПАО Ромашка",
    )
    assert len(calls) == 1, "read.entities обязан сделать РОВНО один физический вызов модели"
    assert len(relations) == 1
    assert relations[0].subject_entity == "ООО Дочка"
    assert relations[0].object_entity == "ПАО Ромашка"
    assert relations[0].ownership_percent == pytest.approx(100.0)
    assert relations[0].period_ref
