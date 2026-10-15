"""Slice 14 migration gates: removed alternatives must stay removed.

These checks prefer module boundaries, registered AtomDefs and runtime
contracts over prose grep. Small source assertions cover only artifacts whose
mere reappearance would recreate a second source of truth.
"""
from __future__ import annotations

import ast
from pathlib import Path

from finengine.contracts import EvidenceBundleV2, RunPassport
from finengine.skills.atoms.graph_atoms import build_registry
from finengine.skills.metric_schema import PROFILE_IDS, registry_for_profile


ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src" / "finengine"


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
    return names


def test_manual_spec_and_model_plan_runners_are_absent():
    removed = (
        ROOT / "station-skills" / "_runner" / "run_spec.py",
        ROOT / "station-skills" / "_runner" / "plan.py",
        ROOT / "station-skills" / "aifa-extract" / "skill.yaml",
        SRC / "skills" / "spec" / "runner.py",
        SRC / "skills" / "spec" / "steps.py",
    )
    assert not [str(path.relative_to(ROOT)) for path in removed if path.exists()]


def test_registered_atoms_have_no_inference_escape_hatches():
    atoms = build_registry().all()
    names = {atom.name for atom in atoms}
    assert "concept.infer" not in names
    assert "formula.infer" not in names
    assert all(not hasattr(atom, "optional_requires") for atom in atoms)
    assert all(atom.produces for atom in atoms)


def test_runtime_graph_uses_target_not_goal_dto():
    runtime_modules = (
        SRC / "skills" / "spec" / "graph_planner.py",
        SRC / "skills" / "spec" / "graph_executor.py",
        SRC / "skills" / "case_runtime.py",
    )
    for path in runtime_modules:
        assert "finengine.contracts.intent" not in _imports(path)


def test_case_state_does_not_import_the_ephemeral_grounding_bus():
    state_modules = [SRC / "skills" / "case_runtime.py", *sorted((SRC / "store").rglob("*.py"))]
    offenders = [path for path in state_modules if "finengine.skills.grounding_bus" in _imports(path)]
    assert not offenders


def test_removed_numeric_policy_lists_and_capital_prompt_do_not_return():
    python = "\n".join(
        path.read_text(encoding="utf-8")
        for path in sorted((SRC / "skills").rglob("*.py"))
    )
    for symbol in (
        "_BOUNDS",
        "_OUTFLOW_METRIC_CODES",
        "_COST_METRIC_CODES",
        "CAPITAL_EXTRACT_PROMPT",
        "_extract_scalar_batch_real_legacy_reference",
        "_legacy_external_compiled",
    ):
        assert symbol not in python


def test_bank_capital_concepts_are_in_each_effective_registry():
    required = {"CET1_RATIO_DISCLOSED", "TIER1_RATIO_DISCLOSED", "RISK_WEIGHTED_ASSETS"}
    for profile_id in (pid for pid in PROFILE_IDS if "_bank_" in pid):
        registry = registry_for_profile(profile_id)
        active = {row["enum"] for row in registry["extractable"]}
        active.update(row[0] for row in registry["calculated"])
        assert required <= active, f"{profile_id}: missing {sorted(required - active)}"


def test_one_contract_definition_for_passport_and_evidence_bundle():
    assert RunPassport.__module__ == "finengine.contracts.run"
    assert EvidenceBundleV2.__module__ == "finengine.contracts.evidence"
    definitions = "\n".join(
        path.read_text(encoding="utf-8") for path in sorted(SRC.rglob("*.py"))
    )
    assert definitions.count("class RunPassport(") == 1
    assert definitions.count("class EvidenceBundleV2(") == 1


def test_runtime_registry_has_no_complete_profile_adapter():
    metric_schema = (SRC / "skills" / "metric_schema.py").read_text(encoding="utf-8")
    assert "compiled_registry_from_effective_snapshot" not in metric_schema
    assert "_legacy_external_compiled" not in metric_schema


def test_grounding_bus_carries_no_financial_metrics_slot():
    """Срез 13.5.4: заземление ждущего анализа через шину — удалённая
    возможность, не временно выключенная. Публикующая половина (`_Slot`,
    `_slots`, `announce`/`publish`/`fail`/`take`/`metrics_of`/`cell_of`)
    возвращает шине роль второго источника финансового состояния — ровно то,
    что нерушимое решение №4 (`01-nerushimye-resheniya.md`) запрещает."""
    from finengine.skills import grounding_bus

    for name in ("_Slot", "_slots", "announce", "publish", "fail", "take",
                "metrics_of", "cell_of"):
        assert not hasattr(grounding_bus, name), (
            f"grounding_bus.{name} вернулся — bus снова публикует financial state")


def test_station_turn_has_no_second_analysis_result_path():
    """Срез 13.5.4: единственный слушатель typed-результата не пересказывает
    markdown обратно в структуру. `analysis_result.finalize_analysis`/
    `extract_result.finalize_extraction`/`publish_grounding` — старый рецепт
    пост-обработки старого движка; вернувшийся вызов из station_turn.py
    означал бы второй числовой путь параллельно каноническому графу."""
    turn_src = (SRC / "api" / "station_turn.py").read_text(encoding="utf-8")
    assert "finalize_analysis(" not in turn_src
    assert "finalize_extraction(" not in turn_src
    assert "publish_grounding" not in turn_src
    from finengine.api import station_turn
    from finengine.skills import extract_result

    for name in ("_result_for_screen", "_stream_canon", "_stream_analysis"):
        assert not hasattr(station_turn, name), (
            f"station_turn.{name} вернулся — недостижимое тело _finish_run/"
            f"_finish_analysis снова физически в файле"
        )
    assert not hasattr(extract_result, "publish_grounding")
