"""Срез 31, П5 на уровне графа: имя, синонимы и определение, которые придумала
модель, ДОЕЗЖАЮТ до двухстадийного экстрактора (`locate` → `read.value`).

Раньше экстрактор получал для ad-hoc показателя синтетическую строку
`{"enum": term, "name_ru": term, "name_en": term}` — искать в отчёте было нечем,
кроме дословной фразы пользователя. Теперь строка строится из спецификации,
которая едет через `NodeArgs.extra` и имеет ту же форму, что строка реестра.
"""
from __future__ import annotations

from types import SimpleNamespace

from finengine.contracts import AdHocConceptSpec, CandidateLocator, SourceLocator
from finengine.skills.atoms import graph_atoms
from finengine.skills.atoms.ad_hoc_concepts import EXTRA_KEY, dump_specs
from finengine.skills.spec.atom_registry import NodeArgs

_KEY = "AVG_LOAN_RATE"
_SPEC = AdHocConceptSpec(
    term="средняя ставка по кредитам", key=_KEY,
    name_ru="Средневзвешенная ставка по кредитам и займам", name_en="Weighted average interest rate",
    aliases_ru=["средняя ставка по кредитам"], aliases_en=["weighted average rate"],
    definition="Средняя процентная ставка по долговым обязательствам.",
    hint="Примечание о займах, проценты годовых.", value_kind="ratio",
)


def _passport():
    return SimpleNamespace(
        classification=SimpleNamespace(
            company_type="non_financial", standard="ifrs", language="ru", profile_id="test-cell",
        ),
        entity=SimpleNamespace(entity_id="ent-1"),
    )


def _ctx(dependencies=()):
    return SimpleNamespace(
        run_passport=_passport(), run_id="run-1",
        inputs=SimpleNamespace(document_text="| Средневзвешенная ставка | 9,5 | 8,7 |"),
        dependencies=lambda: tuple(dependencies),
    )


def _item(concept: str = _KEY, **extra) -> NodeArgs:
    return NodeArgs(
        concept=concept, document_version="v1",
        extra={EXTRA_KEY: dump_specs([_SPEC]), **extra},
    )


def _row(metrics: list[dict], enum: str) -> dict:
    return next(m for m in metrics if str(m["enum"]).upper() == enum.upper())


def test_locate_gets_the_registry_shaped_row_built_from_the_model_description(monkeypatch):
    captured: dict = {}

    def fake_locate_batch(concepts, **kwargs):
        captured["concepts"] = list(concepts)
        captured["metrics"] = kwargs["metrics"]
        return {c: [] for c in concepts}

    monkeypatch.setattr("finengine.skills.atoms.locate.locate_batch", fake_locate_batch)
    monkeypatch.setattr(graph_atoms, "registry_for_passport", lambda passport: {"extractable": []})

    graph_atoms._locate_batch_real([_item()], _ctx())

    row = _row(captured["metrics"], _KEY)
    assert row["name_ru"] == "Средневзвешенная ставка по кредитам и займам"
    assert row["name_en"] == "Weighted average interest rate"
    assert row["aliases_ru"] == ["средняя ставка по кредитам"]
    assert row["aliases_en"] == ["weighted average rate"]
    assert "долговым обязательствам" in row["description"]
    assert "проценты годовых" in row["hint"]
    assert row["period_type"] == ""


def test_read_value_gets_the_same_row_and_keeps_the_requested_concept_key(monkeypatch):
    captured: dict = {}

    def fake_read_value_batch(concepts, candidates_by_concept, **kwargs):
        captured["concepts"] = list(concepts)
        captured["metrics"] = kwargs["metrics"]
        return {c: [] for c in concepts}

    monkeypatch.setattr("finengine.skills.atoms.read_value.read_value_batch", fake_read_value_batch)
    monkeypatch.setattr(graph_atoms, "registry_for_passport", lambda passport: {"extractable": []})
    locator = CandidateLocator(
        produced_by_atom="locate", produced_by_atom_version="1.0.0", document_id="d", document_version="v1",
        concept=_KEY, locator=SourceLocator(page=1, text_fragment="x"), confidence=0.9, origin="model",
    )

    result = graph_atoms._read_value_batch_real([_item()], _ctx([locator]))

    row = _row(captured["metrics"], _KEY)
    assert row["aliases_ru"] == ["средняя ставка по кредитам"]
    assert list(result) == [_KEY], "результат раскладывается под запрошенным ключом узла"


def test_a_concept_without_a_spec_still_falls_back_to_the_bare_phrase(monkeypatch):
    """Старое поведение сохраняется: нет спецификации — строка по самой фразе."""
    captured: dict = {}

    def fake_locate_batch(concepts, **kwargs):
        captured["metrics"] = kwargs["metrics"]
        return {c: [] for c in concepts}

    monkeypatch.setattr("finengine.skills.atoms.locate.locate_batch", fake_locate_batch)
    monkeypatch.setattr(graph_atoms, "registry_for_passport", lambda passport: {"extractable": []})

    graph_atoms._locate_batch_real([NodeArgs(concept="площадь залов", document_version="v1")], _ctx())

    row = _row(captured["metrics"], "площадь залов")
    assert row["name_ru"] == "площадь залов" and row.get("aliases_ru", []) == []


def test_a_registry_concept_keeps_its_registry_row_even_if_a_spec_names_it(monkeypatch):
    """Строка реестра — выверенная; ad-hoc спецификация её не подменяет."""
    captured: dict = {}

    def fake_locate_batch(concepts, **kwargs):
        captured["metrics"] = kwargs["metrics"]
        return {c: [] for c in concepts}

    registry_row = {"enum": _KEY, "name_ru": "Строка реестра", "name_en": "Registry row",
                    "aliases_ru": ["реестровый синоним"], "aliases_en": []}
    monkeypatch.setattr("finengine.skills.atoms.locate.locate_batch", fake_locate_batch)
    monkeypatch.setattr(graph_atoms, "registry_for_passport", lambda passport: {"extractable": [registry_row]})

    graph_atoms._locate_batch_real([_item()], _ctx())

    rows = [m for m in captured["metrics"] if str(m["enum"]).upper() == _KEY]
    assert len(rows) == 1 and rows[0]["name_ru"] == "Строка реестра"
