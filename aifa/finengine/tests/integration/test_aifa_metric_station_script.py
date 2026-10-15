"""`aifa-metric` (Срез 27) — единственный станционный вход, вызывающий
настоящий `intent.parse`. Проверяется через настоящий `prepare_production_
context`/`execute_production_goal`/`CaseRuntime`/`GraphPlanner`/
`GraphExecutor` — модельная граница подменяется в ДВУХ местах, ровно как
production делает вызовы: `intent.parse`'s собственный модельный вызов
(`finengine.skills.atoms.intent._default_complete`) и `locate`/`read.value`
(тот же приём, что `test_intent_to_graph.py`, через `build_registry`'s
executor-инъекцию). Planner/Executor/Case/manifest — настоящие.
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "station-skills" / "aifa-metric" / "scripts" / "metric_run.py"


def _metric_run_module():
    spec = importlib.util.spec_from_file_location("aifa_metric_run", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _fake_llm_response(payload: dict):
    async def _complete(system: str, prompt: str, *, model_version=None) -> str:
        return json.dumps(payload, ensure_ascii=False)
    return _complete


def _patched_build_registry(monkeypatch, locate_exec=None, read_value_exec=None, **defaults):
    from finengine.skills.atoms.graph_atoms import build_registry as real_build_registry

    def _wrapped(*, profile_id, registry_data=None, **kwargs):
        if locate_exec is not None:
            kwargs.setdefault("locate_executor", locate_exec)
        if read_value_exec is not None:
            kwargs.setdefault("read_value_executor", read_value_exec)
        for key, value in defaults.items():
            kwargs.setdefault(key, value)
        return real_build_registry(profile_id=profile_id, registry_data=registry_data, **kwargs)

    monkeypatch.setattr("finengine.skills.production_runtime.build_registry", _wrapped)


def _args(module, tmp_path: Path, text: str, query: str):
    text_path = tmp_path / "ocr-doc-1.txt"
    text_path.write_text(text, encoding="utf-8")
    out_json = tmp_path / "metric.json"
    return module.argparse.Namespace(
        text=str(text_path), document_id="doc-1", query=query,
        company_type="", cell="", out_json=str(out_json),
    ), out_json


@pytest.fixture(autouse=True)
def _owner_case_env(monkeypatch, tmp_path):
    from finengine import config
    from finengine.skills import station_guard

    # A station worker owns one room/cwd.  Linux's duplicate-run guard keeps
    # its heartbeat lock in that cwd for the lifetime of the process, so each
    # in-process integration scenario needs the same room isolation that the
    # real station gives separate requests.
    monkeypatch.chdir(tmp_path)
    # The real process exits after one run, which stops its daemon heartbeat.
    # These scenarios deliberately share one pytest process, so do not leave
    # production-style heartbeat threads alive between otherwise isolated rooms.
    monkeypatch.setattr(station_guard, "start_heartbeat", lambda _label: lambda: None)
    monkeypatch.setenv("FE_OWNER_ID", "owner-1")
    monkeypatch.setenv("FE_CASE_ID", "case-1")
    # `config.settings` is a process-wide singleton read once at import time —
    # setting the env var alone would not reach it this late; the attribute
    # itself is what `get_case_store()` actually reads.
    monkeypatch.setattr(config.settings, "state_database_url", f"sqlite:///{tmp_path}/case.db")


def test_extract_registered_metric_end_to_end_through_the_station_script(
    tmp_path, monkeypatch, severstal_text, make_fake_locate_and_read_value_executors,
):
    module = _metric_run_module()
    monkeypatch.setattr(
        "finengine.skills.atoms.intent._default_complete",
        _fake_llm_response({
            "intent_kind": "extract_registered_metric", "targets": [{"concept": "SALES_REVENUE"}],
            "entity_ref": "severstal", "period_ref": "p-2024-fy", "role": None,
            "unresolved": [], "ambiguities": [],
        }),
    )
    locate_exec, read_value_exec = make_fake_locate_and_read_value_executors({"SALES_REVENUE": 987_654.0})
    _patched_build_registry(monkeypatch, locate_exec, read_value_exec)

    args, out_json = _args(module, tmp_path, severstal_text, "какая выручка у Северстали за 2024 год")
    code = module._run(args)
    assert code == 0

    manifest = json.loads(out_json.read_text(encoding="utf-8"))
    assert "extracted_metric" in manifest
    metric = manifest["extracted_metric"]
    assert metric["output_type"] == "ExtractedMetric"
    assert metric["concept"] == "SALES_REVENUE"
    assert metric["scalar_fact"]["value"] == 987_654.0


def test_calculate_ad_hoc_metric_end_to_end_through_the_station_script(
    tmp_path, monkeypatch, severstal_text, make_fake_locate_and_read_value_executors,
):
    module = _metric_run_module()
    monkeypatch.setattr(
        "finengine.skills.atoms.intent._default_complete",
        _fake_llm_response({
            "intent_kind": "calculate_ad_hoc_metric", "targets": [],
            "ad_hoc_formula": {"expression": "SALES_REVENUE - NET_INCOME", "dependencies": ["SALES_REVENUE", "NET_INCOME"]},
            "entity_ref": "severstal", "period_ref": "p-2024-fy", "role": None,
            "unresolved": ["выручка минус чистая прибыль"], "ambiguities": [],
        }),
    )
    values = {"SALES_REVENUE": 1_000_000.0, "NET_INCOME": 120_000.0}
    locate_exec, read_value_exec = make_fake_locate_and_read_value_executors(values)
    _patched_build_registry(monkeypatch, locate_exec, read_value_exec)

    args, out_json = _args(
        module, tmp_path, severstal_text, "посчитай выручку минус чистая прибыль у Северстали за 2024 год",
    )
    code = module._run(args)
    assert code == 0

    manifest = json.loads(out_json.read_text(encoding="utf-8"))
    assert "calculated_metric" in manifest
    metric = manifest["calculated_metric"]
    assert metric["output_type"] == "CalculatedMetric"
    assert metric["definition_origin"] == "ad_hoc"
    assert metric["derived_fact"]["value"] == 1_000_000.0 - 120_000.0
    from finengine.store.cases import get_case_store
    case = get_case_store().load_case("owner-1", "case-1")
    assert case is not None
    assert len(case.ad_hoc_formulas) == 1
    persisted = next(iter(case.ad_hoc_formulas.values()))
    assert persisted.expression == "SALES_REVENUE - NET_INCOME"
    assert persisted.dependencies == ["SALES_REVENUE", "NET_INCOME"]


def test_extract_ad_hoc_metric_end_to_end_through_the_same_station_script(
    tmp_path, monkeypatch, severstal_text, make_fake_locate_and_read_value_executors,
):
    module = _metric_run_module()
    term = "площадь торговых залов"
    monkeypatch.setattr(
        "finengine.skills.atoms.intent._default_complete",
        _fake_llm_response({
            "intent_kind": "extract_ad_hoc_metric", "targets": [],
            "entity_ref": "severstal", "period_ref": "p-2024-fy", "role": None,
            "unresolved": [term], "ambiguities": [],
        }),
    )
    locate_exec, read_value_exec = make_fake_locate_and_read_value_executors({term: 321.0})
    _patched_build_registry(monkeypatch, locate_exec, read_value_exec)
    args, out_json = _args(module, tmp_path, severstal_text, "покажи площадь торговых залов")
    assert module._run(args) == 0
    manifest = json.loads(out_json.read_text(encoding="utf-8"))
    assert manifest["extracted_metric"]["definition_origin"] == "ad_hoc"
    assert manifest["extracted_metric"]["scalar_fact"]["value"] == 321.0


def test_calculate_registered_metric_end_to_end_through_the_same_station_script(
    tmp_path, monkeypatch, severstal_text, make_fake_locate_and_read_value_executors,
):
    module = _metric_run_module()
    monkeypatch.setattr(
        "finengine.skills.atoms.intent._default_complete",
        _fake_llm_response({
            "intent_kind": "calculate_registered_metric", "targets": [{"concept": "EBITDA"}],
            "entity_ref": "severstal", "period_ref": "p-2024-fy", "role": None,
            "unresolved": [], "ambiguities": [],
        }),
    )
    locate_exec, read_value_exec = make_fake_locate_and_read_value_executors({
        "OPERATING_PROFIT": 400.0, "DEPRECIATION_AMORTIZATION": 60.0,
    })
    _patched_build_registry(monkeypatch, locate_exec, read_value_exec)
    # `intent.parse` fail-closed отбрасывает период, которого пользователь не
    # называл, даже если fake model попыталась его добавить. Этот station-
    # тест не проходит period prepass query_stream-гейта, поэтому период
    # должен присутствовать в самом запросе.
    args, out_json = _args(module, tmp_path, severstal_text, "рассчитай EBITDA за 2024 год")
    assert module._run(args) == 0
    manifest = json.loads(out_json.read_text(encoding="utf-8"))
    assert manifest["calculated_metric"]["concept"] == "EBITDA"
    assert manifest["calculated_metric"]["derived_fact"]["value"] == 460.0


def test_fill_form_end_to_end_through_the_same_station_script(
    tmp_path, monkeypatch, sberbank_text, make_fake_locate_and_read_value_executors,
):
    from finengine.roles.scope import layouts

    module = _metric_run_module()
    monkeypatch.setattr(
        "finengine.skills.atoms.intent._default_complete",
        _fake_llm_response({
            "intent_kind": "form", "targets": [], "entity_ref": "sberbank",
            "period_ref": "p-2024-fy", "role": "dgr", "unresolved": [], "ambiguities": [],
        }),
    )
    rows = layouts()["roles"]["dgr"]["formats"]["bank"]["rows"]
    values = {row["enum"]: float(index + 1) for index, row in enumerate(rows[:5])}
    locate_exec, read_value_exec = make_fake_locate_and_read_value_executors(values)
    _patched_build_registry(monkeypatch, locate_exec, read_value_exec)
    args, out_json = _args(module, tmp_path, sberbank_text, "заполни форму ДГР")
    assert module._run(args) == 0
    manifest = json.loads(out_json.read_text(encoding="utf-8"))
    assert manifest["form_output"]["output_type"] == "FormOutput"
    assert manifest["form_output"]["role"] == "dgr"


def test_full_analysis_end_to_end_through_the_same_station_script(
    tmp_path, monkeypatch, severstal_text, make_fake_locate_and_read_value_executors,
    make_fake_structure_executor, make_fake_entities_executor,
):
    from tests.integration.test_intent_to_graph import (
        _fake_explanation_batch, _fake_external_executor, _fake_judgment_batch,
        _fake_narrative_executor,
    )

    module = _metric_run_module()
    monkeypatch.setattr(
        "finengine.skills.atoms.intent._default_complete",
        _fake_llm_response({
            "intent_kind": "full_analysis", "targets": [], "entity_ref": "severstal",
            "period_ref": "p-2024-fy", "role": "other", "unresolved": [], "ambiguities": [],
        }),
    )
    values = {"SALES_REVENUE": 1_000.0, "NET_INCOME": 120.0,
              "INTEREST_PAID": 9.0, "INTEREST_RECEIVED": 2.0}
    locate_exec, read_value_exec = make_fake_locate_and_read_value_executors(values)
    _patched_build_registry(
        monkeypatch, locate_exec, read_value_exec,
        structure_executor=make_fake_structure_executor({}),
        entities_executor=make_fake_entities_executor([]),
        judgment_executor=_fake_judgment_batch,
        explanation_executor=_fake_explanation_batch,
        external_executor=_fake_external_executor,
        narrative_executor=_fake_narrative_executor,
    )
    args, out_json = _args(module, tmp_path, severstal_text, "сделай полный финансовый анализ")
    assert module._run(args) == 0
    manifest = json.loads(out_json.read_text(encoding="utf-8"))
    assert manifest["report_output"]["output_type"] == "ReportOutput"


def test_monitor_changes_compares_the_periods_of_one_document_through_the_same_station_script(
    tmp_path, monkeypatch, severstal_text, make_fake_locate_and_read_value_executors,
    make_fake_structure_executor, make_fake_entities_executor,
):
    """Срез 32: тот же режим, что в чате, — без сохранённых снимков и предыдущих ходов."""
    from tests.integration.test_graph_analysis_narrative import (
        _fake_explanation_batch, _fake_external_executor, _fake_judgment_batch,
        _fake_narrative_executor,
    )
    from tests.integration.test_monitor_period_changes import VALUES, _two_period_read_value

    module = _metric_run_module()
    monkeypatch.setattr(
        "finengine.skills.atoms.intent._default_complete",
        _fake_llm_response({
            "intent_kind": "monitor_changes", "targets": [], "entity_ref": "issuer",
            "period_ref": None, "role": "other", "unresolved": [], "ambiguities": [],
        }),
    )
    locate_exec, _ = make_fake_locate_and_read_value_executors({concept: 1.0 for concept in VALUES})
    _patched_build_registry(
        monkeypatch, locate_exec, _two_period_read_value,
        structure_executor=make_fake_structure_executor({}),
        entities_executor=make_fake_entities_executor([]),
        judgment_executor=_fake_judgment_batch,
        explanation_executor=_fake_explanation_batch,
        external_executor=_fake_external_executor,
        narrative_executor=_fake_narrative_executor,
    )
    args, out_json = _args(module, tmp_path, severstal_text, "что изменилось у эмитента?")
    assert module._run(args) == 0
    manifest = json.loads(out_json.read_text(encoding="utf-8"))
    report = manifest["period_change_report"]
    assert report["output_type"] == "PeriodChangeReport"
    rows = {row["concept"]: row for row in report["rows"]}
    assert rows["SALES_REVENUE"]["relative_change_pct"] == 25.0
    assert rows["NET_INCOME"]["relative_change_pct"] == -25.0
    assert report["explanation_status"] == "ready"


def test_ambiguous_query_is_refused_not_guessed(tmp_path, monkeypatch, severstal_text):
    module = _metric_run_module()
    monkeypatch.setattr(
        "finengine.skills.atoms.intent._default_complete",
        _fake_llm_response({
            "intent_kind": "extract_registered_metric", "targets": [],
            "entity_ref": "severstal", "period_ref": "p-2024-fy", "role": None,
            "unresolved": [],
            "ambiguities": [{"term": "прибыль", "candidates": ["NET_INCOME", "EBITDA_MARGIN"], "reason": "не указан вид прибыли"}],
        }),
    )
    args, out_json = _args(module, tmp_path, severstal_text, "покажи прибыль Северстали")
    code = module._run(args)
    assert code == 4
    assert not out_json.exists()


def test_multiple_targets_are_refused_not_silently_dropped(tmp_path, monkeypatch, severstal_text):
    module = _metric_run_module()
    monkeypatch.setattr(
        "finengine.skills.atoms.intent._default_complete",
        _fake_llm_response({
            "intent_kind": "extract_registered_metric",
            "targets": [{"concept": "SALES_REVENUE"}, {"concept": "NET_INCOME"}],
            "entity_ref": "severstal", "period_ref": "p-2024-fy", "role": None,
            "unresolved": [], "ambiguities": [],
        }),
    )
    args, out_json = _args(module, tmp_path, severstal_text, "покажи выручку и прибыль Северстали")
    code = module._run(args)
    assert code == 4
    assert not out_json.exists()
