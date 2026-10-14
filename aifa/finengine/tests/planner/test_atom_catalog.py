"""Срез 31, П8: каталог атомов для динамического планировщика.

Агент-планировщик видит краткое описание ВСЕХ атомов. Каталог обязан совпадать с
реестром: атом без описания агенту невидим, описание без атома — ложь. Тест-страж
ловит дрейф в обе стороны (новый атом в `build_registry` без строки в каталоге
краснит сборку).
"""
from __future__ import annotations

from finengine.skills.atoms.graph_atoms import build_registry
from finengine.skills.atoms.locate import ATOM_VERSION as LOCATE_ATOM_VERSION
from finengine.skills.spec.atom_catalog import (
    ATOM_DESCRIPTIONS,
    NOT_PLANNABLE,
    catalog_text,
    plannable_atoms,
)


def test_every_registered_atom_has_a_description_and_no_description_is_orphaned():
    registered = {atom.name for atom in build_registry().all()}
    assert set(ATOM_DESCRIPTIONS) == registered, (
        f"нет описания: {sorted(registered - set(ATOM_DESCRIPTIONS))}; "
        f"лишнее описание: {sorted(set(ATOM_DESCRIPTIONS) - registered)}"
    )


def test_descriptions_are_short_enough_to_fit_all_atoms_into_one_prompt():
    assert all(20 <= len(text) <= 330 for text in ATOM_DESCRIPTIONS.values())
    assert len(catalog_text(build_registry())) < 22_000


def test_gates_and_infrastructure_are_not_offered_as_plannable_steps():
    names = {atom.name for atom in plannable_atoms(build_registry())}
    for name in ("norm.unit", "norm.time", "verify.bounds", "verify.identity", "verify.cross",
                 "verify.citation", "verify.grounding", "verify.arithmetic",
                 "ingest.document", "identify.classify", "read.override", "watch.subscribe"):
        assert name in NOT_PLANNABLE and name not in names
    for name in ("locate", "read.value", "read.quote", "read.structure", "calc.formula", "out.narrative"):
        assert name in names


def test_the_catalog_states_what_each_atom_needs_and_produces():
    text = catalog_text(build_registry())
    assert "read.quote" in text and "QuoteEvidence" in text
    assert "locate" in text and "CandidateLocator" in text
    # Гейты и инфраструктура достраиваются планировщиком сами — агенту это сказано.
    assert "достраиваются автоматически" in text


def test_a_type_with_many_concepts_is_summarized_not_enumerated():
    text = catalog_text(build_registry())
    read_value_line = next(line for line in text.splitlines() if line.startswith("- read.value"))
    assert "ExtractedMetric[" in read_value_line and read_value_line.count(",") < 12, (
        "78 показателей реестра не должны раздувать каталог"
    )


def test_locate_catalog_version_tracks_its_evidence_semantics():
    locate = next(atom for atom in build_registry().all() if atom.name == "locate")
    assert locate.version == LOCATE_ATOM_VERSION
    assert locate.version == "1.0.2", "старые CandidateLocator нельзя брать из кэша"
