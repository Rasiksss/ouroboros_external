from __future__ import annotations

import pytest

from finengine.skills.atoms.subject_history import diff_profiles
from tests.slice11_fixtures import identity, metric, profile


def test_diff_finds_new_changed_disappeared_and_ignores_run_ids():
    previous = profile(1, [
        metric(100, concept="A", period="2025", evidence_id="a-old"),
        metric(50, concept="B", period="2025", evidence_id="b-old"),
    ])
    current = profile(2, [
        metric(110, concept="A", period="2025", evidence_id="a-new"),
        metric(7, concept="C", period="2025", evidence_id="c-new"),
    ])
    diff = diff_profiles(previous, current)
    assert [x.identity.split("|")[0] for x in diff.changed_facts] == ["A"]
    assert diff.changed_facts[0].absolute_delta == 10
    assert [x.identity.split("|")[0] for x in diff.disappeared_facts] == ["B"]
    assert [x.identity.split("|")[0] for x in diff.new_facts] == ["C"]
    same = diff_profiles(
        profile(1, [metric(100, concept="A", period="2025", evidence_id="run-1")]),
        profile(2, [metric(100, concept="A", period="2025", evidence_id="run-2")]),
    )
    assert same.changed_facts == []


def test_basis_and_version_compatibility_are_explicit():
    previous = profile(1, [metric(100, period="2025", basis="IFRS")], registry="r1")
    current = profile(2, [metric(100, period="2025", basis="RAP")], registry="r2")
    diff = diff_profiles(previous, current)
    assert diff.comparability == "incompatible"
    assert diff.changed_facts[0].absolute_delta is None
    assert any("basis mismatch" in x for x in diff.comparability_issues)
    assert "registry_version changed" in diff.comparability_issues


def test_different_subjects_never_compare_even_with_same_name():
    previous = profile(1, [metric(1, period="2025")], entity=identity("5493001KJTIIGC8Y1R12", name="Twin"))
    current = profile(2, [metric(2, period="2025")], entity=identity("213800D1EI4B9WTWWD28", name="Twin"))
    with pytest.raises(ValueError, match="different SubjectKey"):
        diff_profiles(previous, current)
