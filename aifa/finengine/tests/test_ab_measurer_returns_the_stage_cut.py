"""Замерщик A/B возвращает постадийный разрез, а не одно число (Б7, 19.08.2026).

ЗАЧЕМ. `ab_extract.py` — единственный инструмент, которым сравнивают две схемы
извлечения на ОДНОМ тексте. До сих пор он возвращал `elapsed_s` всего прогона и
выбрасывал терминальное событие движка целиком, кроме трёх полей. По одному
числу нельзя отличить экономию на префилле (промпт короче) от экономии на
генерации (просим меньше) — а решения по ускорению принимаются ровно между
этими двумя, и цена у них разная.

Проверка ФУНКЦИОНАЛЬНАЯ, а не по исходнику: движок подставной, разрез
сравнивается с тем, что он отдал в `done['_diag']`.
"""
from __future__ import annotations

import asyncio
import importlib.util
from pathlib import Path

import pytest

from finengine.config import settings
from finengine.skills import two_stage_extractor as ts

_AB = Path(__file__).resolve().parents[1] / "benchmarks" / "ab_extract.py"

_DIAG = {
    "llm_requests": [
        {"stage": "main_compression", "elapsed_s": 214.3, "tokens_in": 136484,
         "tokens_out": 7199, "tokens_cached": 0},
        {"stage": "main_extract_batch_001", "elapsed_s": 88.1, "tokens_in": 41000,
         "tokens_out": 12000, "tokens_cached": 0},
    ],
    "wall_s": 407.9,
}


def _ab_module():
    spec = importlib.util.spec_from_file_location("ab_extract_under_test", _AB)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def measured(monkeypatch, tmp_path):
    """Один прогон замерщика на подставном движке."""

    async def _fake_engine(*_args, **_kw):
        yield {"type": "metric", "enum": "REVENUE",
               "period_values": [{"period": "2024", "value": 1.0}]}
        yield {"type": "done", "period_current": "2024", "period_prev": "2023",
               "company_type": "non_financial", "_diag": _DIAG}

    monkeypatch.setattr(settings, "docs_dir", tmp_path)
    monkeypatch.setattr(ts, "extract_stream_finengine_dev_two_stage", _fake_engine)
    ab = _ab_module()
    return asyncio.run(ab._run("two_stage", "===PAGE 1===\nВыручка 1 000\n",
                               "ifrs_nf_ru", "non_financial", "other",
                               tmp_path / "ev.jsonl"))


def test_each_stage_comes_back_with_its_seconds(measured):
    calls = measured["stages"]["calls"]
    assert [c["stage"] for c in calls] == ["main_compression", "main_extract_batch_001"]
    assert [c["elapsed_s"] for c in calls] == [214.3, 88.1]


def test_tokens_come_back_apart_from_seconds(measured):
    """Без токенов по стадиям экономия на префилле неотличима от экономии на
    генерации — а это разные правки с разной ценой для качества."""
    calls = measured["stages"]["calls"]
    assert [c["tokens_in"] for c in calls] == [136484, 41000]
    assert [c["tokens_out"] for c in calls] == [7199, 12000]


def test_the_old_fields_are_still_there(measured):
    """Разрез ДОБАВЛЕН, а не заменил собой сводку: её читают глазами."""
    assert measured["engine"] == "two_stage"
    assert measured["metric_events"] == 1
    assert measured["written"] == 1
    assert measured["period_current"] == "2024"
