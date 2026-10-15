"""Встраивание `EvidenceBundleV2` в `calc_run.py` (C4/C10 из 03-metodologiya-
cikla-sreza.md).

ЗАКРЫТО (Срез 13.5): три теста этого файла, проверявшие мост `extract.py`
(старый двухстадийный станционный скрипт) → `metrics.json`-конверт →
`evidence_bundle`, удалены. Канонический `extract.py` (тонкая прослойка над
`production_runtime.py`) больше не строит этот конверт вовсе:
`EvidenceBundleV2` собирается внутри графа, а не эстафетой между станционными
скриптами. Мост, который они проверяли, физически не существует — не
регрессия, архитектурное решение Среза 13.5. Что реально проверять на
каноническом пути — тема `docs/build-plan/23f-1-srez-13-5-4-station-listener-
design.md`, группа E.

`calc_run.py` (`aifa-calc`) остаётся отдельным, ещё не мигрированным legacy
потребителем (см. Nedodelano.md, Срез 13.5, п.2 — трекается в 13.5.2, не
здесь), и два теста ниже проверяют его собственное, всё ещё живое поведение:
`_run()` не вызывает модель, его можно прогнать целиком.
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _calc_run():
    spec = importlib.util.spec_from_file_location(
        "calc_run_evidence_bundle", ROOT / "station-skills" / "aifa-calc" / "scripts" / "calc_run.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ── calc_run.py: прогон целиком, реальный файл на диске ───────────────────

def test_calc_writes_one_evidence_bundle_without_changing_the_legacy_numbers(
    tmp_path: Path, monkeypatch,
):
    """Тот самый сценарий `test_own_formulas_survive_the_narrowed_tail`
    (репозиторий уже доверяет этим числам), плюс новая проверка: после расчёта
    в ТОМ ЖЕ файле лежит `evidence_bundle`, а старые ключи/числа не сдвинулись —
    ровно то, что требует DoD Среза 2."""
    calc_run = _calc_run()
    monkeypatch.chdir(tmp_path)
    envelope = {
        "run": {"cell": "ifrs_nf_ru", "main_period": "FY 2021", "prev_period": "FY 2020",
               "run_passport": {"run_id": "run-evidence-42"}},
        "metrics": [
            {"enum": "BANK_DEPOSITS", "value_current": 60177.0, "value_prev": 50000.0,
             "found": True, "is_calculated": False, "unit": "млн руб.",
             "period_values": [{"period": "FY 2021", "value": 60177.0, "unit": "млн руб."},
                               {"period": "FY 2020", "value": 50000.0, "unit": "млн руб."}]},
            {"enum": "CASH_EQUIVALENTS", "value_current": 45920.0, "value_prev": 40000.0,
             "found": True, "is_calculated": False, "unit": "млн руб.",
             "period_values": [{"period": "FY 2021", "value": 45920.0, "unit": "млн руб."},
                               {"period": "FY 2020", "value": 40000.0, "unit": "млн руб."}]},
        ],
    }
    (tmp_path / "metrics.json").write_text(json.dumps(envelope), encoding="utf-8")
    own = [{"enum": "DEP_TO_CASH", "name_ru": "Депозиты к деньгам",
            "formula": "BANK_DEPOSITS / CASH_EQUIVALENTS",
            "deps": ["BANK_DEPOSITS", "CASH_EQUIVALENTS"], "unit_kind": "ratio"}]
    (tmp_path / "extra.json").write_text(json.dumps(own), encoding="utf-8")

    код = calc_run._run("metrics.json", "", "CASH_EQUIVALENTS", "", False, "extra.json", "")
    assert код == 0

    written = json.loads((tmp_path / "metrics.json").read_text(encoding="utf-8"))

    # СТАРЫЙ РЕЗУЛЬТАТ НЕ ТРОНУТ — те же числа, что держит существующий сторож.
    by_enum = {r["enum"]: r for r in written["metrics"]}
    assert by_enum["DEP_TO_CASH"]["value_current"] == pytest.approx(1.3105, abs=0.001)
    assert by_enum["BANK_DEPOSITS"]["value_current"] == 60177.0
    assert by_enum["CASH_EQUIVALENTS"]["value_current"] == 45920.0

    # ОДНА ДОКАЗАТЕЛЬНАЯ БАЗА НА ПРОГОН — извлечённое и посчитанное в одном бандле.
    bundle = written["evidence_bundle"]
    assert bundle["run_id"] == "run-evidence-42"
    scalar_concepts = {f["concept"] for f in bundle["scalar_facts"]}
    derived_concepts = {f["concept"] for f in bundle["derived_facts"]}
    assert {"BANK_DEPOSITS", "CASH_EQUIVALENTS"} <= scalar_concepts
    assert "DEP_TO_CASH" in derived_concepts
    dep_fact = next(f for f in bundle["derived_facts"] if f["concept"] == "DEP_TO_CASH")
    assert dep_fact["formula_id"] == "DEP_TO_CASH"
    assert set(dep_fact["inputs"]) == {"BANK_DEPOSITS", "CASH_EQUIVALENTS"}


def test_calc_without_a_run_passport_skips_the_bundle_quietly(tmp_path: Path, monkeypatch):
    """Чистый счёт скриптом (`--script`, без входного файла прогона) — паспорта
    нет, и бандл не собирается: это не отказ расчёта, а честное «не с чем
    собрать», см. докстринг в `calc_run.py`."""
    calc_run = _calc_run()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("FE_RPC_DIR", str(tmp_path / ".model-rpc"))
    script = tmp_path / "model.py"
    script.write_text(
        "RESULT = [{'enum': 'SUM_2_2', 'name_ru': 'Сумма', "
        "'value_current': 2 + 2, 'unit': 'ед.'}]\n", encoding="utf-8")

    код = calc_run._run("", "", "", "", False, "", str(script))
    assert код == 0
    written = json.loads((tmp_path / "calc.json").read_text(encoding="utf-8"))
    assert "evidence_bundle" not in written
