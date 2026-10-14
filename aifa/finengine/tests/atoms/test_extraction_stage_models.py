"""Модели двух стадий извлечения — свои поля настроек (решение владельца 09.10.2026).

`locate` (поиск кандидатов) и `read.value` (извлечение значений) зовут модель,
названную в `settings.locate_model` / `settings.read_value_model`. Пустое поле —
модель бэкенда по умолчанию, как было: до модельной границы уходит `None`.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from finengine.contracts import CandidateLocator, SourceLocator
from finengine.skills.atoms import graph_atoms
from finengine.skills.spec.atom_registry import NodeArgs

_KEY = "REVENUE"
_ROW = {"enum": _KEY, "name_ru": "Выручка", "name_en": "Revenue", "aliases_ru": [], "aliases_en": []}


def _ctx(dependencies=()):
    passport = SimpleNamespace(
        classification=SimpleNamespace(
            company_type="non_financial", standard="ifrs", language="ru", profile_id="test-cell",
        ),
        entity=SimpleNamespace(entity_id="ent-1"),
    )
    return SimpleNamespace(
        run_passport=passport, run_id="run-1",
        inputs=SimpleNamespace(document_text="| Выручка | 500 | 400 |"),
        dependencies=lambda: tuple(dependencies),
    )


@pytest.mark.parametrize(("configured", "expected"), [("xiaomi/mimo-v2.5-pro", "xiaomi/mimo-v2.5-pro"), ("", None)])
def test_locate_calls_the_model_named_in_its_setting(monkeypatch, configured, expected):
    seen: dict = {}

    def fake_locate_batch(concepts, **kwargs):
        seen["model_id"] = kwargs.get("model_id")
        return {c: [] for c in concepts}

    monkeypatch.setattr("finengine.skills.atoms.locate.locate_batch", fake_locate_batch)
    monkeypatch.setattr(graph_atoms, "registry_for_passport", lambda passport: {"extractable": [_ROW]})
    monkeypatch.setattr(graph_atoms._settings, "locate_model", configured)

    graph_atoms._locate_batch_real([NodeArgs(concept=_KEY, document_version="v1")], _ctx())

    assert seen == {"model_id": expected}


@pytest.mark.parametrize(("configured", "expected"), [("xiaomi/mimo-v2.5-pro", "xiaomi/mimo-v2.5-pro"), ("", None)])
def test_read_value_calls_the_model_named_in_its_setting(monkeypatch, configured, expected):
    seen: dict = {}

    def fake_read_value_batch(concepts, candidates_by_concept, **kwargs):
        seen["model_id"] = kwargs.get("model_id")
        return {c: [] for c in concepts}

    monkeypatch.setattr("finengine.skills.atoms.read_value.read_value_batch", fake_read_value_batch)
    monkeypatch.setattr(graph_atoms, "registry_for_passport", lambda passport: {"extractable": [_ROW]})
    monkeypatch.setattr(graph_atoms._settings, "read_value_model", configured)
    locator = CandidateLocator(
        produced_by_atom="locate", produced_by_atom_version="1.0.0", document_id="d", document_version="v1",
        concept=_KEY, locator=SourceLocator(page=1, text_fragment="x"), confidence=0.9, origin="model",
    )

    graph_atoms._read_value_batch_real([NodeArgs(concept=_KEY, document_version="v1")], _ctx([locator]))

    assert seen == {"model_id": expected}
