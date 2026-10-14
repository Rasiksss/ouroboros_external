"""Structures use the canonical evidence graph for banks as well as issuers."""
from __future__ import annotations

from finengine.contracts import GoalIR
from finengine.skills.atoms.graph_atoms import build_registry, default_policy
from finengine.skills.atoms.intent import root_from_goal
from finengine.skills.atoms.run_passport import build_run_passport, classification_from_cell
from finengine.skills.spec.graph_planner import GraphPlanner


def _analysis_plan(cell: str):
    goal = GoalIR(
        raw_query="полный анализ", intent_kind="full_analysis",
        entity_ref="issuer:test", period_ref="p-2025-fy",
    )
    classification = classification_from_cell(cell)
    passport = build_run_passport(
        "financial report", goal_ir_id=goal.goal_id, classification=classification,
    )
    registry = build_registry(profile_id=cell)
    return GraphPlanner(
        registry=registry, policy=default_policy(cell),
    ).plan(root_from_goal(goal)[0].root, run_passport=passport)


def test_bank_analysis_requests_segments_through_read_structure():
    names = {node.node_key.atom_name for node in _analysis_plan("ifrs_bank_ru").graph.nodes}
    assert "read.structure" in names
    assert "analyze.full_bundle" in names


def test_nonfinancial_analysis_uses_the_same_structure_route():
    names = {node.node_key.atom_name for node in _analysis_plan("ifrs_nf_ru").graph.nodes}
    assert "read.structure" in names
    assert "analyze.full_bundle" in names
