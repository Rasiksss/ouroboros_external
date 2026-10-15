from __future__ import annotations

import pytest

from finengine.contracts import ApproachPolicy
from finengine.skills.atoms.subject_history import contains_recommendation_language, evaluate_approach
from tests.slice11_fixtures import rule


POLICY = ApproachPolicy(
    policy_id="ten-percent", version="1", distance_kind="relative_to_threshold",
    approach_boundary=0.10,
)


def test_boundary_direction_and_crossing_are_deterministic():
    prior = rule(2.0, 3.0, passed=True, headroom=1.0, rule_id="old", evidence_id="e1")
    at_boundary = rule(2.7, 3.0, passed=True, headroom=0.3, rule_id="now", evidence_id="e2")
    signal = evaluate_approach(at_boundary, prior, POLICY)
    assert signal.status == "approaching" and signal.approaching
    assert signal.direction == "toward" and signal.distance == pytest.approx(0.1)
    crossed = evaluate_approach(
        rule(3.2, 3.0, passed=False, headroom=-0.2, rule_id="cross", evidence_id="e3"),
        at_boundary, POLICY,
    )
    assert crossed.status == "crossed" and crossed.crossed


def test_threshold_change_blocks_direction_and_narrative_has_no_recommendation():
    previous = rule(2.0, 3.0, passed=True, headroom=1.0, rule_id="old", evidence_id="e1")
    changed = rule(2.1, 2.5, passed=True, headroom=0.4, rule_id="new", evidence_id="e2")
    signal = evaluate_approach(changed, previous, POLICY)
    assert signal.status == "insufficient" and signal.direction == "unknown"
    assert not contains_recommendation_language(signal.narrative)
