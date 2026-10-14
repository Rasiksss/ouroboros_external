"""Список показателей от человека ДОЕЗЖАЕТ до движка (Ш13).

ЗАЧЕМ ИМЕННО ТАК. Сценарий владельца §5.2 — «извлеки выручку, чистую прибыль,
EBITDA и активы, посчитай рентабельность активов и Cost/Income» — до сих пор был
невыразим: у тула не было аргумента «список показателей», а роль — это шесть
фиксированных наборов, то есть новый набор = НОВЫЙ КОД под каждый запрос.

И главное: состав показателей считает СТАНЦИЯ, а не бэкенд
(`station_driver` передавал только `--role/--company-type/--basis-full`).
Поэтому правка одной схемы `ToolDef` не дала бы ровно ничего — просьба
человека остановилась бы на бэкенде, прогон пошёл бы полным набором, и
пользователь увидел бы 119 показателей вместо четырёх, НИЧЕГО об этом не узнав.
Это ровно та болезнь, что стоила дня 05.08: изменение обязано совпасть в двух
местах, обновляется одно, никто не жалуется.

Отсюда две проверяемые границы, по одной на звено:
  1. просьба сужает НАБОР той же общей функцией, что и роль (`build_engine_scope`);
  2. таблица показывает РОВНО запрошенное, а читает — вместе со слагаемыми.

🔴 ГРАНИЦЫ «просьба доезжает до КОМАНДНОЙ СТРОКИ станции» здесь больше нет, и
это не потеря сторожа, а переезд предмета: командную строку бэкенд не собирает
с коммита aa13cb1 (`station_driver` — 74 строки, ни `httpx`, ни сборки).
Флаги живут в станционном скрипте (`station-skills/aifa-extract/scripts/
extract.py`), и сторожат их ЗЕЛЁНЫЕ соседи:
`test_skill_commands_fit_the_sandbox.py::test_the_body_demands_the_metrics_
flag_when_the_user_names_metrics`, `test_skill_flags_are_documented.py::
test_every_script_flag_is_named_in_the_body` и — на отсечение чужой команды в
имени показателя — `test_ratios_door.py::test_shape_is_refused_before_the_
registry`.

ПРОВЕРКА СТОРОЖА ОТКАТОМ: убрать `requested` из `build_engine_scope` — оба
оставшихся раздела обязаны покраснеть.
"""
from __future__ import annotations


from finengine.roles.scope import build_engine_scope


# ── 2. Сужение считает ОДНА функция — та же, что у роли ──────────────────

def test_requested_narrows_the_set_and_pulls_in_formula_inputs():
    _plan, enums = build_engine_scope(
        "other", "non_financial", "ifrs_nf_ru",
        requested=["SALES_REVENUE", "NET_INCOME", "TOTAL_ASSETS", "ROA"])
    assert {"SALES_REVENUE", "NET_INCOME", "TOTAL_ASSETS", "ROA"} <= enums
    _plan_full, full = build_engine_scope("other", "non_financial", "ifrs_nf_ru")
    assert len(enums) < len(full) / 3, (
        f"сужения не произошло: {len(enums)} из {len(full)} — прогон будет полным")


def test_requested_ratio_brings_its_inputs():
    """Коэффициент без слагаемых не посчитается, и человек получит пустую строку."""
    _plan, enums = build_engine_scope(
        "other", "non_financial", "ifrs_nf_ru", requested=["EBITDA_MARGIN"])
    assert "EBITDA" in enums and "SALES_REVENUE" in enums, (
        f"входы формулы не подтянулись: {sorted(enums)}")


def test_unknown_names_do_not_empty_the_run():
    """Ни одного знакомого имени — это повод сказать, а не отдать пустую таблицу."""
    _plan, enums = build_engine_scope(
        "other", "non_financial", "ifrs_nf_ru", requested=["ФИОЛЕТОВЫЙ_СЛОН"])
    assert len(enums) > 50, "прогон схлопнулся в пустой набор вместо полного"


# ── 3. Таблица показывает РОВНО запрошенное ──────────────────────────────

class _Index:
    def get_document(self, doc_id):  # noqa: D102, ANN001
        return None


