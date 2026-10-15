"""Общие помощники для сквозных сценариев Среза 3 (B3): точечный показатель,
полный анализ, форма роли — «через настоящий `PlanGraph`+`GraphExecutor` в
тестовом маршруте» (Definition of Done). Реальные атомы (`skills.atoms.
graph_atoms`), реальный `GraphPlanner`/`GraphExecutor`; единственная подмена —
модельная граница `locate`/`read.value` (Срез 5a; см. докстринг
`graph_atoms.py`), до Среза 5a это была одна граница `extract.scalar`."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from finengine.contracts import (
    CandidateLocator,
    EntityRelation,
    QuoteEvidence,
    ScalarFact,
    SourceLocator,
    StructuredFact,
)

GOLDEN_CLASSIFICATION = Path(__file__).resolve().parents[1] / "golden" / "classification" / "cases.json"


@pytest.fixture(scope="module")
def severstal_text() -> str:
    """Реальный excerpt МСФО-отчёта Северстали (`tests/golden/classification/
    cases.json`, `cls-023`) — тот же текст, которым уже проверяется `id.frame`
    в Срезе 1 (`tests/atoms/test_id_frame.py`), не выдуманный заново. Реально
    классифицируется `detect_profile()` в `ifrs_nf_ru` — детерминированно, без
    модели."""
    cases = json.loads(GOLDEN_CLASSIFICATION.read_text(encoding="utf-8"))
    case = next(c for c in cases if c["id"] == "cls-023")
    assert case["profile_id"] == "ifrs_nf_ru"
    return case["excerpt"]


@pytest.fixture(scope="module")
def sberbank_text() -> str:
    """Реальный excerpt МСФО-отчёта банка (`cls-027`) — классифицируется в
    `ifrs_bank_ru`. Нужен для сценария формы роли: раскладка `dgr` в
    `roles/layouts.json` сегодня заведена только для `company_type=bank`."""
    cases = json.loads(GOLDEN_CLASSIFICATION.read_text(encoding="utf-8"))
    case = next(c for c in cases if c["id"] == "cls-027")
    assert case["profile_id"] == "ifrs_bank_ru"
    return case["excerpt"]


def _make_fake_locate_and_read_value_executors(values: dict[str, float], *, unit: str = "RUB_MLN"):
    """Единственная модельная граница трёх сценариев — теперь ДВА узла,
    `locate`→`read.value` (Срез 5a), подменяется ПАРОЙ детерминированных
    дублей тем же batch-соглашением (список `NodeArgs` → `{concept:
    evidence}`), которое используют реальные `_locate_batch_real`/
    `_read_value_batch_real`. Каждый ЗАПРОШЕННЫЙ концепт обязан получить
    запись в ответе, даже пустую: «не нашлось» — успех с нулём доказательств,
    не техническая ошибка пакета (то же правило, что раньше держал
    `_extract_scalar_batch_real`).

    `locate`'s узлы несут `NodeArgs.concept` уже голым enum'ом (retrofit v9:
    `OutputSpec(output_type="CandidateLocator", concept=enum)` различает тип
    артефакта, не текстовый префикс имени концепта — см. `graph_atoms.py`).
    """
    def _fake_locate(items, ctx):  # noqa: ARG001
        out: dict[str, tuple] = {}
        for args in items:
            enum = args.concept or ""
            if enum in values:
                out[args.concept] = (CandidateLocator(
                    produced_by_atom="locate", produced_by_atom_version="1.0.0",
                    document_id="fake-doc", document_version=ctx.run_id, concept=enum,
                    locator=SourceLocator(page=1, text_fragment=f"fake evidence for {enum}"),
                    confidence=0.9, origin="model",
                ),)
            else:
                out[args.concept] = ()
        return out

    def _fake_read_value(items, ctx):  # noqa: ARG001
        out: dict[str, tuple] = {}
        for args in items:
            concept = args.concept
            if concept in values:
                out[concept] = (ScalarFact(
                    produced_by_atom="read.value", produced_by_atom_version="1.0.0",
                    source_ref=f"fake-read-value:{concept}", concept=concept,
                    period_ref=args.period_ref or "unknown", value=values[concept], unit=unit,
                ),)
            else:
                out[concept] = ()
        return out

    return _fake_locate, _fake_read_value


@pytest.fixture
def make_fake_locate_and_read_value_executors():
    """Фикстура-фабрика — та же причина, что у `atom`/`target` в `tests/
    planner/conftest.py`: `tests/integration/` не пакет, `import tests.
    integration.conftest` не сработает."""
    return _make_fake_locate_and_read_value_executors


def _make_fake_structure_executor(rows_by_table_kind: dict[str, list[dict]]):
    """Срез 5b: та же batch-подмена, что у `locate`/`read.value` — список
    `NodeArgs` (один на `table_kind`) → `{table_kind: (StructuredFact, ...)}`."""
    def _fake_structure(items, ctx):  # noqa: ARG001
        out: dict[str, tuple] = {}
        for args in items:
            table_kind = args.concept or ""
            rows = rows_by_table_kind.get(table_kind)
            if not rows:
                out[table_kind] = ()
                continue
            out[table_kind] = (StructuredFact(
                produced_by_atom="read.structure", produced_by_atom_version="1.0.0",
                source_ref=f"fake-structure:{table_kind}", document_version=ctx.run_id,
                table_kind=table_kind, rows=rows,
            ),)
        return out
    return _fake_structure


@pytest.fixture
def make_fake_structure_executor():
    return _make_fake_structure_executor


def _make_fake_quote_executor(quotes_by_topic: dict[str, tuple[str, int]]):
    """Срез 5b: batch-подмена `read.quote` — тема без записи получает пустой
    кортеж, как и «не нашлось» у `locate`."""
    def _fake_quote(items, ctx):  # noqa: ARG001
        out: dict[str, tuple] = {}
        for args in items:
            topic = args.concept or ""
            found = quotes_by_topic.get(topic)
            if found is None:
                out[topic] = ()
                continue
            quote_text, page = found
            out[topic] = (QuoteEvidence(
                produced_by_atom="read.quote", produced_by_atom_version="1.0.0",
                source_ref=f"fake-quote:{topic}", document_version=ctx.run_id,
                concept=topic, quote_text=quote_text, page=page, fragment_hash="fake-hash",
            ),)
        return out
    return _fake_quote


@pytest.fixture
def make_fake_quote_executor():
    return _make_fake_quote_executor


def _make_fake_entities_executor(relations: list[tuple[str, str, float | None]]):
    """Срез 5b: `read.entities` не батчируется по концепту — один логический
    вызов на документ, как `identify.classify`."""
    def _fake_entities(args, ctx):  # noqa: ARG001
        return [EntityRelation(
            produced_by_atom="read.entities", produced_by_atom_version="1.0.0",
            source_ref=f"fake-entities:{name}", document_version=ctx.run_id,
            subject_entity=name, relation=relation, object_entity="Parent",
            direction="object_owns_subject", ownership_percent=pct,
        ) for name, relation, pct in relations]
    return _fake_entities


@pytest.fixture
def make_fake_entities_executor():
    return _make_fake_entities_executor
