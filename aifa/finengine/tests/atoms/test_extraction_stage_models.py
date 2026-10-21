"""Модели двух стадий извлечения — свои поля настроек (решение владельца 09.10.2026).

`locate` (поиск кандидатов) и `read.value` (извлечение значений) зовут модель,
названную в `settings.locate_model` / `settings.read_value_model`. Пустое поле —
модель бэкенда по умолчанию, как было: до модельной границы уходит `None`.
"""
from __future__ import annotations

import json
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


@pytest.mark.parametrize(("configured", "expected"), [("low", "low"), ("", None)])
def test_locate_asks_the_model_with_the_reasoning_level_from_its_setting(monkeypatch, configured, expected):
    """Рассуждение `high` у GLM на полном отчёте съедало весь вывод, ответа не оставалось (dev, 20.10.2026)."""
    import asyncio

    from finengine.skills import two_stage_extractor
    from finengine.skills.atoms import locate

    seen: dict = {}

    async def fake_call_json_llm(**kwargs):
        seen["reasoning_effort"] = kwargs.get("reasoning_effort")
        return {}, {}, "{}"

    monkeypatch.setattr(two_stage_extractor, "_call_json_llm", fake_call_json_llm)
    monkeypatch.setattr(two_stage_extractor, "_build_main_compression_prompt", lambda **kwargs: "prompt")
    monkeypatch.setattr(
        two_stage_extractor, "_enrich_compression_with_alias_table_matches", lambda compression, **kwargs: compression,
    )
    monkeypatch.setattr(locate.settings, "locate_reasoning_effort", configured)

    asyncio.run(locate._locate_shard(
        asyncio.Semaphore(1), [_ROW], document_text="x", tables=[], system_prompt="s", period_current="FY 2021",
        period_prev="FY 2020", periods_detected=[], language="ru", model_id=None,
    ))

    assert seen == {"reasoning_effort": expected}


@pytest.mark.parametrize(("configured", "expected"), [("minimal", "minimal"), ("", None)])
def test_read_value_asks_the_model_with_the_reasoning_level_from_its_setting(monkeypatch, configured, expected):
    """Без своей настройки `read.value` шёл с умолчанием `high`: 41 показатель ЛУКОЙЛа читался 6 мин (dev, 21.10.2026)."""
    from finengine.skills import two_stage_extractor
    from finengine.skills.atoms import read_value

    seen: list = []

    answer = {_KEY: {"values": {"2024": "500"}, "unit_text": "RUB"}}

    async def fake_call_json_llm(**kwargs):
        seen.append(kwargs.get("reasoning_effort"))
        return answer, {}, json.dumps(answer)

    monkeypatch.setattr(two_stage_extractor, "_call_json_llm", fake_call_json_llm)
    monkeypatch.setattr(read_value.settings, "read_value_reasoning_effort", configured)
    locator = CandidateLocator(
        produced_by_atom="locate", produced_by_atom_version="1.0.0", document_id="d", document_version="v1",
        concept=_KEY, locator=SourceLocator(page=1, text_fragment="x"), confidence=0.9, origin="model",
    )

    read_value.read_value_batch(
        [_KEY], {_KEY: [locator]}, metrics=[_ROW], document_version="v1", period_current="2024", period_prev="2023",
    )

    assert seen and set(seen) == {expected}


def test_a_json_call_passes_its_reasoning_level_to_the_model(monkeypatch):
    import asyncio

    from finengine.skills import two_stage_extractor

    seen: dict = {}

    async def fake_complete_with_usage(**kwargs):
        seen["reasoning_effort"] = kwargs.get("reasoning_effort")
        return "{}", {}

    monkeypatch.setattr(two_stage_extractor.llm, "complete_with_usage", fake_complete_with_usage)

    asyncio.run(two_stage_extractor._call_json_llm(
        prompt="json", system="s", model_id=None, stage="locate", debug_dir=None,
        prompt_name="p", response_name="r.json", reasoning_effort="low",
    ))

    assert seen == {"reasoning_effort": "low"}
