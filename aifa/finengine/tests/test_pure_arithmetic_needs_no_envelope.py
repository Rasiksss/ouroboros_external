"""Чистый счёт скриптом — без входного файла и без ячейки (решение владельца 31.08.2026).

ВОПРОС ВЛАДЕЛЬЦА, КОТОРЫМ ЭТО КУПЛЕНО: «если пользователь скажет сложи 2+2,
то откуда возьмётся файл metrics?» Ниоткуда — а флаг стоял `required=True`,
и модель была обязана сочинить файл с пустым списком ради обряда. Теперь
`--in` (новое имя `--metrics`) необязателен при `--script`.

Заодно стережётся, что чистый счёт не тащит реестровый перебор: без величин
полсотни формул ячейки дали бы полсотни строк «не посчитано» шумом.
"""
from __future__ import annotations

import importlib.util
import json
import pytest
from pathlib import Path


def _calc_run():
    spec = importlib.util.spec_from_file_location(
        "calc_run_pure", Path("station-skills/aifa-calc/scripts/calc_run.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_two_plus_two_runs_without_an_input_file(tmp_path: Path, monkeypatch):
    calc_run = _calc_run()
    monkeypatch.chdir(tmp_path)
    # Скрипт-путь исполняется только в комнате станции; её признак — переменная
    # моста. Тест ставит её так же, как ставит обёртка extract_run.sh.
    monkeypatch.setenv("FE_RPC_DIR", str(tmp_path / ".model-rpc"))
    скрипт = tmp_path / "model.py"
    скрипт.write_text(
        "RESULT = [{'enum': 'SUM_2_2', 'name_ru': 'Сумма', "
        "'value_current': 2 + 2, 'unit': 'ед.'}]\n", encoding="utf-8")

    код = calc_run._run("", "", "", "", False, "", str(скрипт))
    assert код == 0, "чистый счёт скриптом без --in отбит — обряд с пустым файлом вернулся"

    конверт = json.loads((tmp_path / "calc.json").read_text(encoding="utf-8"))
    строки = конверт["metrics"]
    assert [r["enum"] for r in строки if r.get("found")] == ["SUM_2_2"], (
        f"в конверте не одна строка скрипта: {[r.get('enum') for r in строки]} — "
        "либо счёт не дошёл, либо реестровый перебор налил шума")
    assert строки[0]["value_current"] == 4


def test_without_a_script_the_input_file_is_still_required():
    """Отрицательный контроль: без --script вход обязателен, как и был."""
    calc_run = _calc_run()
    assert calc_run._run("", "", "", "", False, "", "") == calc_run.EXIT_BAD_INPUT


def test_the_old_flag_name_still_parses(tmp_path: Path, monkeypatch):
    """`--metrics` живёт алиасом: его зовёт хвост extract_run.sh и старые ходы."""
    import sys

    calc_run = _calc_run()
    for флаг in ("--in", "--metrics"):
        monkeypatch.setattr(sys, "argv", ["calc_run.py", флаг, "нет.json"])
        try:
            calc_run.main()
        except SystemExit as e:
            assert e.code == calc_run.EXIT_BAD_INPUT, (
                f"{флаг} с несуществующим файлом дал {e.code}: парсер не принял флаг")


def test_own_formulas_survive_the_narrowed_tail(tmp_path: Path, monkeypatch):
    """Сужение хвоста заказом не выкидывает свои формулы (прогон da0b5ad4).

    Хвост извлечения зовёт расчёт с `--only <имена заказа>`; имён своих формул
    в заказе нет по устройству — их несёт файл `--extra-calculated`. Свои
    заказаны самим фактом подачи: с сужением обязаны посчитаться.
    """
    import json as _json

    calc_run = _calc_run()
    monkeypatch.chdir(tmp_path)
    конверт = {"run": {"cell": "ifrs_nf_ru", "main_period": "FY 2021",
                       "prev_period": "FY 2020"},
               "metrics": [
                   {"enum": "BANK_DEPOSITS", "value_current": 60177.0, "found": True},
                   {"enum": "CASH_EQUIVALENTS", "value_current": 45920.0, "found": True},
               ]}
    (tmp_path / "metrics.json").write_text(_json.dumps(конверт), encoding="utf-8")
    своя = [{"enum": "DEP_TO_CASH", "name_ru": "Депозиты к деньгам",
             "formula": "BANK_DEPOSITS / CASH_EQUIVALENTS",
             "deps": ["BANK_DEPOSITS", "CASH_EQUIVALENTS"], "unit_kind": "ratio"}]
    (tmp_path / "extra.json").write_text(_json.dumps(своя), encoding="utf-8")

    код = calc_run._run("metrics.json", "", "CASH_EQUIVALENTS", "", False,
                        "extra.json", "")
    assert код == 0
    строки = _json.loads((tmp_path / "metrics.json").read_text(encoding="utf-8"))["metrics"]
    dep = [r for r in строки if r.get("enum") == "DEP_TO_CASH"]
    assert dep and dep[0].get("value_current") == pytest.approx(1.3105, abs=0.001), (
        "своя формула выпала из суженного расчёта — заказ «отношение депозитов "
        f"к деньгам» исполнен пустотой: {[r.get('enum') for r in строки]}")
    лишние = [r for r in строки if r.get("is_calculated") and not r.get("found")
              and r.get("enum") != "DEP_TO_CASH"]
    assert len(лишние) < 40, (
        f"сужение не сработало: в конверте {len(лишние)} пустых расчётных строк")
