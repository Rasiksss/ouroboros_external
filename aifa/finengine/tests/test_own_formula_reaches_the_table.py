"""Свой коэффициент человека считается движком и попадает в таблицу.

🔴 ЗАЧЕМ ЭТО ЕСТЬ (решение владельца 23.08.2026, по итогам прогонов Северстали).

`--extra-metrics` умеет только ИЗВЛЕКАЕМЫЕ показатели: описал, где искать в
отчёте, — движок нашёл. Коэффициента так не завести, а спрашивают именно их. Из
пяти, о которых владелец спросил по `severstal-Q1-2025.pdf`, ЧЕТЫРЕ в реестре
ячейки отсутствуют: ROCE, FCF на акцию, чистый долг с учётом аренды, запас по
ковенанту. Модель считала их в уме и писала текстом — в рабочую область они не
попадали вовсе, то есть результат нельзя было ни выгрузить, ни проверить, ни
пересчитать другим периодом.

ЧИСЛА ЗДЕСЬ — ИЗ РАЗМЕТКИ, А НЕ ИЗ ПРОГОНА, и это главное свойство этого файла.
`benchmarks/vendor/.../golden/ifrs_nf_ru/gt/severstal-Q1-2025.csv` — эталон того
же документа, на котором владелец гоняет живые тесты. Считая по нему, мы
проверяем СЧЁТ и ничего кроме: качество извлечения сюда не примешивается, и
красный тест здесь означает ровно «формула посчиталась не так».

⚠️ ЗНАК CAPEX — ЛОВУШКА, КУПЛЕННАЯ ЗАМЕРОМ ПРИ РАЗРАБОТКЕ. В таблице он стоит
отрицательным (`-43 591`), и написать «поток ПЛЮС капзатраты» — первое, что
приходит в голову; так и написала модель в живом ответе. Но у `CAPEX` в реестре
стоит `formula_default_abs_behavior: true`, движок берёт модуль, и `+ CAPEX`
даёт +58,0 вместо −46,1 — знак наизнанку и ни слова об ошибке. Реестровая
формула FCF says то же самое: `NET_OPERATING_CASH_FLOW – CAPEX`. Поэтому здесь
проверяются ОБА варианта: правильный даёт эталон, неправильный — заведомо другое
число.

ПРОВЕРКА СТОРОЖЕЙ ОТКАТОМ (мутации прогнаны 23.08.2026, каждая покраснела):
  · не подмешивать `extra` в реестр (вернуть `registry` как был) → падают все
    три расчётных теста: своих формул движок не видит;
  · пустить своё имя поверх реестрового (снять проверку `имя in known`) →
    падает `test_the_registry_wins_a_name_clash`;
  · принимать запись без проверки `CalcDef` → падает
    `test_a_formula_that_would_annualize_twice_is_refused`.
"""
from __future__ import annotations

import csv
from pathlib import Path

import pytest

from finengine.skills.ratios import compute_ratios, own_calc_defs

CELL = "ifrs_nf_ru"
GOLDEN = (Path(__file__).resolve().parents[1] / "benchmarks" / "vendor" / "tests"
          / "test_benchmark" / "data" / "golden" / CELL / "gt" / "severstal-Q1-2025.csv")


def _number(raw: str) -> float | None:
    """Эталон пишет числа как в отчёте: разряды запятой, минус скобками."""
    s = raw.strip().replace(",", "")
    negative = s.startswith("(") and s.endswith(")")
    s = s.strip("()")
    try:
        value = float(s)
    except ValueError:
        return None
    return -value if negative else value


@pytest.fixture(scope="module")
def эталон() -> dict[str, dict[str, float]]:
    if not GOLDEN.is_file():  # pragma: no cover — vendor не синкнут
        pytest.skip(f"разметки нет: {GOLDEN}")
    out: dict[str, dict[str, float]] = {}
    with GOLDEN.open(encoding="utf-8-sig") as f:
        for row in csv.DictReader(f, delimiter=";"):
            if row.get("segment_name") or row.get("unit") not in ("RUB million", "RUB"):
                continue
            value = _number(row["value"])
            if value is not None:
                out.setdefault(row["enum"], {})[row["period"]] = value
    return out


@pytest.fixture(scope="module")
def строки(эталон) -> list[dict]:
    """Таблица прогона в том виде, в каком её отдаёт движок."""
    return [{
        "enum": enum, "found": True,
        "period_values": [{"period": p, "value": v} for p, v in periods.items()],
        "value_current": periods.get("Q1 2025", periods.get("31.03.2025")),
        "value_prev": periods.get("Q1 2024", periods.get("31.12.2024")),
    } for enum, periods in эталон.items()]


def _посчитать(строки, defs: list[dict]) -> dict[str, float | None]:
    принятые, отказы = own_calc_defs(defs, set())
    assert not отказы, f"формулы не приняты: {отказы}"
    rows = compute_ratios(строки, CELL, only=[d["enum"] for d in принятые],
                          period_current="Q1 2025", period_prev="Q1 2024",
                          extra=принятые)
    return {str(r.get("enum")): r.get("value_current") for r in rows}


def test_roce_matches_the_golden_numbers(строки):
    """28 355 / (867 864 − 241 610) × 100 = 4,528 %."""
    got = _посчитать(строки, [{
        "enum": "ROCE_OWN", "name_ru": "ROCE",
        "formula": "OPERATING_PROFIT / (TOTAL_ASSETS - TOTAL_CURRENT_LIABILITIES) * 100",
        "deps": ["OPERATING_PROFIT", "TOTAL_ASSETS", "TOTAL_CURRENT_LIABILITIES"],
        "unit_kind": "percent"}])
    assert got["ROCE_OWN"] == pytest.approx(4.528, abs=0.001), (
        "свой коэффициент не сошёлся с разметкой — считает не движок или не по тем числам")


def test_working_capital_turnover_matches_the_golden_numbers(строки):
    """178 733 / (331 529 − 241 610) = 1,988."""
    got = _посчитать(строки, [{
        "enum": "WC_TURNOVER_OWN", "name_ru": "Оборачиваемость рабочего капитала",
        "formula": "SALES_REVENUE / (TOTAL_CURRENT_ASSETS - TOTAL_CURRENT_LIABILITIES)",
        "deps": ["SALES_REVENUE", "TOTAL_CURRENT_ASSETS", "TOTAL_CURRENT_LIABILITIES"],
        "unit_kind": "ratio"}])
    assert got["WC_TURNOVER_OWN"] == pytest.approx(1.988, abs=0.001)


def test_capex_sign_convention_is_minus_not_plus(строки):
    """🔴 Ловушка знака: в таблице CAPEX отрицателен, а формуле нужен МИНУС.

    У `CAPEX` в реестре стоит `formula_default_abs_behavior`, движок берёт
    модуль. Написавший «поток ПЛЮС капзатраты» получит знак наизнанку и ни
    слова об ошибке — ровно так и написала модель в живом ответе владельца.
    Реестровая формула FCF говорит то же самое: `NET_OPERATING_CASH_FLOW – CAPEX`.
    """
    правильно = _посчитать(строки, [{
        "enum": "FCF_PS_OWN", "name_ru": "FCF на акцию",
        "formula": "(NET_OPERATING_CASH_FLOW - CAPEX) / (NET_INCOME / EPS)",
        "deps": ["NET_OPERATING_CASH_FLOW", "CAPEX", "NET_INCOME", "EPS"]}])
    assert правильно["FCF_PS_OWN"] == pytest.approx(-46.1, abs=0.1), (
        "(4 982 − 43 591) / (21 072 / 25,16) = −46,1 ₽ на акцию")

    наизнанку = _посчитать(строки, [{
        "enum": "FCF_PS_WRONG", "name_ru": "FCF на акцию (со знаком наизнанку)",
        "formula": "(NET_OPERATING_CASH_FLOW + CAPEX) / (NET_INCOME / EPS)",
        "deps": ["NET_OPERATING_CASH_FLOW", "CAPEX", "NET_INCOME", "EPS"]}])
    assert наизнанку["FCF_PS_WRONG"] > 0, (
        "ловушка знака исчезла — значит поведение движка изменилось, и правило "
        "в теле навыка про «минус, а не плюс» надо переписать")


def test_the_registry_wins_a_name_clash(строки):
    """Своё имя поверх реестрового не проходит — иначе подмена невидима."""
    принятые, отказы = own_calc_defs(
        [{"enum": "ROE", "name_ru": "моя рентабельность", "formula": "NET_INCOME / TOTAL_ASSETS"}],
        {"ROE"})
    assert not принятые
    assert отказы and "уже есть в реестре" in отказы[0]


def test_a_formula_that_would_annualize_twice_is_refused():
    """Проверка ТА ЖЕ, что у поставляемого реестра, а не своя.

    `annualize` вместе с `ANNUAL(...)` даёт на квартале ×16 (замер 21.08). Своя
    проверка здесь была бы вторым набором правил, и обойти запрет через свою
    формулу стало бы возможно.
    """
    принятые, отказы = own_calc_defs([{
        "enum": "MY_RATIO", "name_ru": "моё",
        "formula": "ANNUAL(EBITDA) / TOTAL_ASSETS", "annualize": True}], set())
    assert not принятые
    assert отказы and "annualize" in отказы[0]
    assert "pydantic.dev" not in отказы[0], (
        "в отказе ссылка на документацию вместо причины — человек чинил бы не то")


def test_a_row_without_a_formula_or_a_name_is_refused():
    принятые, отказы = own_calc_defs(
        [{"enum": "NO_FORMULA"}, {"name_ru": "без имени", "formula": "A / B"}], set())
    assert not принятые
    assert len(отказы) == 2


# ─── обвязка: своя формула доезжает до ЭКРАНА, а не только считается ────────
#
# 🔴 ДВА ДЕФЕКТА, НАЙДЕННЫХ ПРОГОНОМ ОБВЯЗКИ ДО ВЫКАТКИ (23.08.2026). Оба
# выключали новую способность ровно в том случае, ради которого она заведена, и
# оба невидимы в тестах `compute_ratios` — там нет ни `--only`, ни среза показа.
#
#   1. `--only ROCE_OWN` вместе со СВОЕЙ ЖЕ формулой давал «не опознано ни
#      одного показателя» и отказ: мост `resolve_requested` сверяется с
#      РЕЕСТРОМ, а своих формул в нём нет по устройству;
#   2. посчитанная строка выходила `in_role: False` — срез показа (`scope.show`)
#      считает ИЗВЛЕЧЕНИЕ, до того как свои формулы появились. На фронте её
#      отфильтровывал `table_output_enums`: посчитано верно, а в рабочей области
#      пусто. Тот же класс, что чинили днём в `station_turn`.
#
# Здесь проверяется НАСТОЯЩИЙ скрипт навыка, запущенный подпроцессом, — не его
# внутренности: оба дефекта жили именно в обвязке.

import json
import subprocess
import sys as _sys

_CALC = (Path(__file__).resolve().parents[1] / "station-skills" / "aifa-calc"
         / "scripts" / "calc_run.py")


def _envelope(tmp_path: Path, строки: list[dict]) -> Path:
    """Конверт прогона с УЗКИМ срезом показа — как его отдаёт извлечение."""
    path = tmp_path / "metrics.json"
    path.write_text(json.dumps({
        "run": {"cell": CELL, "company_type": "non_financial", "company_name": "Северсталь",
                "main_period": "Q1 2025", "prev_period": "Q1 2024", "role": "other",
                "scope": {"read": [r["enum"] for r in строки],
                          "show": ["SALES_REVENUE", "OPERATING_PROFIT"]}},
        "metrics": [{**r, "name_ru": r["enum"], "unit": "млн. руб."} for r in строки],
    }, ensure_ascii=False), encoding="utf-8")
    return path


def _run_calc(tmp_path: Path, строки: list[dict], defs: list[dict], only: str):
    own = tmp_path / "own.json"
    own.write_text(json.dumps(defs, ensure_ascii=False), encoding="utf-8")
    env_path = _envelope(tmp_path, строки)
    proc = subprocess.run(
        [_sys.executable, str(_CALC), "--metrics", str(env_path),
         "--extra-calculated", str(own), "--only", only],
        capture_output=True, text=True, timeout=180,
        env={"PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src"),
             "PATH": "/usr/bin:/bin"})
    return proc, json.loads(env_path.read_text(encoding="utf-8"))


_OWN = [{"enum": "ROCE_OWN", "name_ru": "ROCE", "unit_kind": "percent",
         "formula": "OPERATING_PROFIT / (TOTAL_ASSETS - TOTAL_CURRENT_LIABILITIES) * 100",
         "deps": ["OPERATING_PROFIT", "TOTAL_ASSETS", "TOTAL_CURRENT_LIABILITIES"]}]


def test_only_understands_an_own_formula_name(строки, tmp_path):
    """`--only` со своим именем не должен отбиваться как «не опознано»."""
    proc, _ = _run_calc(tmp_path, строки, _OWN, "ROCE_OWN")
    assert proc.returncode == 0, (
        f"расчёт отказал на своём же имени:\n{proc.stderr[-600:]}")
    assert "не опознано ни одного" not in proc.stderr


def test_an_own_formula_lands_in_the_shown_slice(строки, tmp_path):
    """Срез показа обязан пополниться своим именем — иначе строки нет на экране.

    Конверт приходит с УЗКИМ срезом (`SALES_REVENUE`, `OPERATING_PROFIT`) —
    ровно так его отдаёт извлечение, которое про свои формулы не знает.
    """
    proc, envelope = _run_calc(tmp_path, строки, _OWN, "ROCE_OWN")
    assert proc.returncode == 0, proc.stderr[-600:]

    показ = envelope["run"]["scope"]["show"]
    assert "ROCE_OWN" in показ, (
        "своё имя не попало в срез показа — на фронте строку отфильтрует "
        "table_output_enums, и человек увидит пустую таблицу при верном счёте")

    строка = next(m for m in envelope["metrics"] if m.get("enum") == "ROCE_OWN")
    assert строка.get("in_role") is True, "строка помечена как показу не подлежащая"
    assert строка["value_current"] == pytest.approx(4.528, abs=0.001)
    assert "ROCE_OWN = 4.52" in proc.stdout, "строки нет и в выдаче человеку"


# ─── формула зовёт то, чего у движка нет — отказ СРАЗУ, а не пустая клетка ───
#
# 🔴 ЖИВОЙ ПРОГОН ВЛАДЕЛЬЦА 24.08.2026, НОНС deb9deb9. Модель впервые прошла
# полный путь и написала пять своих формул. Две сошлись с разметкой точно
# (ROCE 18,111 при эталоне 18,11; оборачиваемость РК 7,951 при 7,95), а чистый
# долг она написала так:
#
#     SHORT_TERM_BORROWINGS + LONG_TERM_DEBT
#       + COALESCE(SHORT_TERM_LEASE_LIABILITIES, 0)
#       + COALESCE(LONG_TERM_LEASE_LIABILITIES, 0) - CASH_EQUIVALENTS
#
# `COALESCE` — синтаксис из SQL, у движка его нет. Отказ он выдаёт
# (`formula_engine._eval_formula_node`), но ГЛУБОКО: наружу выходит строка без
# значения, неотличимая от честного «входов не нашлось». Человек видит пустую
# клетку и думает, что аренды в отчёте нет, — а нерабочей была формула.
#
# ⚠️ ПРОВЕРКА НА ПРИЁМЕ, А НЕ ПРИ СЧЁТЕ. Разница — пять минут прогона и
# понятность отказа: на приёме мы ещё можем назвать причину и перечислить
# разрешённое, и модель поправит формулу до запуска.
#
# ПРОВЕРКА СТОРОЖЕЙ ОТКАТОМ (мутации прогнаны 24.08.2026, каждая покраснела):
#   · не звать `_refuse_unknown_calls` → падает `test_an_unknown_function_is_refused`;
#   · захардкодить свой список функций вместо `_FORMULA_FUNCS` → падает
#     `test_the_allowed_list_comes_from_the_engine`.


def test_an_unknown_function_is_refused_with_its_name():
    принятые, отказы = own_calc_defs([{
        "enum": "NET_DEBT_OWN", "name_ru": "Чистый долг",
        "formula": "SHORT_TERM_BORROWINGS + COALESCE(LONG_TERM_LEASE_LIABILITIES, 0)",
        "deps": ["SHORT_TERM_BORROWINGS", "LONG_TERM_LEASE_LIABILITIES"]}], set())
    assert not принятые, "формула с несуществующей функцией принята — счёт даст пустую клетку"
    assert "COALESCE" in отказы[0], "отказ не называет виновную функцию"
    assert "ABS" in отказы[0], "отказ не перечисляет разрешённое — модель не поправит"


def test_the_allowed_list_comes_from_the_engine():
    """Список берём у движка, а не копией: копия разошлась бы и отказ соврал."""
    from finengine.skills.bulk_extractor.formula_engine import _FORMULA_FUNCS

    _, отказы = own_calc_defs([{
        "enum": "X_OWN", "name_ru": "x", "formula": "FOO(A)", "deps": ["A"]}], set())
    for имя in _FORMULA_FUNCS:
        assert имя in отказы[0], (
            f"движок разрешает {имя}, а отказ о нём не говорит — списки разошлись")


def test_the_functions_the_engine_does_know_pass():
    """Запрет узкий: ABS/MIN/MAX — законные, и их принимать обязаны."""
    принятые, отказы = own_calc_defs([{
        "enum": "OK_OWN", "name_ru": "ok",
        "formula": "ABS(SALES_REVENUE) / MAX(TOTAL_ASSETS, 1)",
        "deps": ["SALES_REVENUE", "TOTAL_ASSETS"]}], set())
    assert принятые and not отказы, f"законная формула отвергнута: {отказы}"


def test_a_broken_formula_is_refused_not_crashed():
    """Синтаксическая ошибка — отказ строкой, а не падение всего расчёта."""
    принятые, отказы = own_calc_defs([{
        "enum": "BAD_OWN", "name_ru": "bad", "formula": "A ++ / B", "deps": ["A", "B"]}], set())
    assert not принятые and отказы


# ─── отказ несёт ДИАГНОЗ, а не список имён ──────────────────────────────────
#
# 🔴 ВОПРОС ВЛАДЕЛЬЦА 24.08.2026: «как модель поймёт, что нам не хватает чего-то
# для расчёта? Как поймёт, что нужно дозапросить показатель в интернете?»
#
# Раньше расчёт говорил «нет входов: A, B» — и три РАЗНЫХ случая выглядели
# одинаково, хотя требуют противоположных действий. Модель различала их
# догадкой, и на живых прогонах ошибалась: гоняла полное переизвлечение там,
# где строки в документе нет вовсе.
#
# Код различает их ТОЧНО, потому что знает то, чего модель не помнит:
#   · `scope.read` прогона — что заказывали у движка;
#   · `found` — что он нашёл;
#   · `source: "market"` в СВОЕЙ записи — что показатель не из отчёта по природе.
# Третий признак ставит МОДЕЛЬ, когда придумывает показатель: откуда он берётся,
# знает она, а не мы. Это ровно та граница «код возит, модель решает».
#
# ПРОВЕРКА ОТКАТОМ (24.08.2026): свести три ветки к одной строке «нет входов» →
# падают все три теста ниже.

def _diagnose(missing, ordered, market):
    """Тот же `_diagnose`, что в скрипте навыка, загруженный по пути.

    Скрипт лежит вне пакета (`station-skills/`), импортировать его обычным
    `import` нельзя — грузим по файлу, чтобы проверять НАСТОЯЩИЙ код, а не его
    пересказ.
    """
    import importlib.util as _u

    путь = (Path(__file__).resolve().parents[1] / "station-skills" / "aifa-calc"
            / "scripts" / "calc_run.py")
    spec = _u.spec_from_file_location("_calc_run_probe", путь)
    модуль = _u.module_from_spec(spec)
    spec.loader.exec_module(модуль)
    return модуль._diagnose(missing, ordered, market)


def test_a_missing_input_that_was_never_ordered_says_so():
    строки = _diagnose(["SHARES_OUTSTANDING"], ordered=set(), market=set())
    assert "не заказывали" in строки[0] and "нужно извлечение" in строки[0]


def test_an_input_the_engine_looked_for_and_did_not_find_says_so():
    """Переизвлечение бесполезно — строки в документе нет."""
    строки = _diagnose(["ASSET_SALES_PROCEEDS"],
                       ordered={"ASSET_SALES_PROCEEDS"}, market=set())
    assert "в отчёте НЕТ" in строки[0]
    assert "переизвлечение не поможет" in строки[0].lower(), (
        "не сказано главного: гонять извлечение заново незачем, и это самая "
        "дорогая из ошибок выбора — пять минут против секунд")


def test_a_market_input_asks_for_an_external_source():
    """Цена акции, курс, ставка — их в отчёте нет по природе."""
    строки = _diagnose(["SHARE_PRICE"], ordered=set(), market={"SHARE_PRICE"})
    assert "рыночная" in строки[0] and "внешний источник" in строки[0]


def test_the_market_flag_beats_the_order_list():
    """Рыночный признак сильнее: даже заказанный, он не из отчёта."""
    строки = _diagnose(["SHARE_PRICE"], ordered={"SHARE_PRICE"}, market={"SHARE_PRICE"})
    assert "рыночная" in строки[0]


# ─── границы правдоподобия ставит АВТОР формулы (Р7) ────────────────────────
#
# 🔴 ВОПРОС ВЛАДЕЛЬЦА 24.08.2026: «написала она формулу, значения подставила, а
# получилось 500 миллиардов — выглядит подозрительно. Чтобы она могла себя
# перепроверить в явных сильных отклонениях».
#
# У РЕЕСТРОВЫХ показателей диапазоны есть в effective registry, и выход за
# них помечается полем `sanity` — тем же, что читает разбор и карточка. У СВОЕЙ
# формулы взяться им неоткуда: реестр не знает чужого показателя и его границ
# не бывает и быть не может.
#
# Решение в духе остального: границы знает ТОТ, КТО ПРИДУМАЛ показатель. Модель,
# сочиняя коэффициент, представляет его нормальный размах — пусть и объявит.
# Пусто — проверки нет, и это законно: не у всякой величины есть осмысленный
# диапазон.
#
# ⚠️ ПОЛЕ СТАВИТСЯ, ТОЛЬКО ЕСЛИ ЕСТЬ ЧТО СКАЗАТЬ. Пустое `sanity` в каждой
# строке ломает паритет с боевым путём движка — состав полей у двух путей обязан
# совпадать, иначе форматы разойдутся молча. Поймано сторожем
# `test_the_live_engine_agrees_with_the_calc_skill` при разработке.
#
# ПРОВЕРКА ОТКАТОМ (24.08.2026): не ставить `sanity` вовсе → падает первый тест;
# ставить его всегда → краснеет сторож паритета в соседнем файле.


def _посчитать_со_знаком(строки, defs):
    принятые, отказы = own_calc_defs(defs, set())
    assert not отказы, отказы
    return {str(r.get("enum")): r for r in compute_ratios(
        строки, CELL, only=[d["enum"] for d in принятые],
        period_current="Q1 2025", period_prev="Q1 2024", extra=принятые)}


def test_a_value_outside_the_declared_range_is_flagged(строки):
    """Ошибка масштаба — самая частая и самая незаметная глазом."""
    got = _посчитать_со_знаком(строки, [{
        "enum": "CRAZY_OWN", "name_ru": "с ошибкой масштаба", "unit_kind": "percent",
        "formula": "SALES_REVENUE * 1000000 / TOTAL_ASSETS",
        "deps": ["SALES_REVENUE", "TOTAL_ASSETS"],
        "min_value": 0, "max_value": 100}])
    note = got["CRAZY_OWN"].get("sanity") or ""
    assert note, "значение в тысячи раз выше объявленной границы прошло молча"
    assert "выше" in note and "100" in note, f"отказ не называет границу: {note!r}"


def test_a_value_inside_the_range_says_nothing(строки):
    """Молчание — норма. Пометка на каждой строке обесценила бы пометки."""
    got = _посчитать_со_знаком(строки, [{
        "enum": "SANE_OWN", "name_ru": "нормальный", "unit_kind": "percent",
        "formula": "OPERATING_PROFIT / SALES_REVENUE * 100",
        "deps": ["OPERATING_PROFIT", "SALES_REVENUE"],
        "min_value": 0, "max_value": 100}])
    assert "sanity" not in got["SANE_OWN"], (
        "поле появилось без повода — состав полей разойдётся с боевым путём")


def test_no_bounds_means_no_check(строки):
    """Не у всякой величины есть осмысленный размах — молчим, а не выдумываем."""
    got = _посчитать_со_знаком(строки, [{
        "enum": "FREE_OWN", "name_ru": "без границ",
        "formula": "SALES_REVENUE * 1000000 / TOTAL_ASSETS",
        "deps": ["SALES_REVENUE", "TOTAL_ASSETS"]}])
    assert "sanity" not in got["FREE_OWN"]


def test_an_upside_down_range_is_refused_at_acceptance():
    """`min > max` не поймает ничего — отказ на приёме, а не тихая бесполезность."""
    принятые, отказы = own_calc_defs([{
        "enum": "BAD_RANGE_OWN", "name_ru": "x", "formula": "A / B",
        "min_value": 100, "max_value": 0}], set())
    assert not принятые
    assert отказы and "min_value" in отказы[0]
