"""Признак `annualize` реестра работает В БОЕВОМ движке, а не только рядом с ним.

ЦЕНА (замер Северстали Q1 2025, 20.08.2026). Приведение к году делают три
производителя расчётных строк, и вызовов `annualized()` у них было 3 / 3 / 0:

    ratios.py                                     3
    bulk_extractor/streams_full_context.py        3
    two_stage_extractor.py                        0   ← БОЕВОЙ (extract_arch=two_stage)

В реестре у ROE, ROA, ROE_SHAREHOLDERS стоит `"annualize": true` (в банковских
ячейках таких по восемь). Значит на квартальном отчёте боевой движок отдавал
КВАРТАЛЬНУЮ доходность под именем годовой: ROE Северстали вышел 4,2 % там, где
приведённый к году — около 16,6 %.

ПОЧЕМУ ЭТО НЕ ЛОВИЛОСЬ. Число выглядит правдоподобно. «Рентабельность капитала
4 %» у металлурга — это не абсурд, на который смотрят второй раз, а плохой, но
возможный год; проверить его читателю нечем, потому что подпись расчёта тоже
молчала о множителе. Ровно тот класс, что и двойные enum'ы: правило заведено в
одном из трёх мест и МОЛЧА не действует в главном.

ЧТО ЗДЕСЬ ПРОВЕРЯЕТСЯ. Не наличие текста в файле, а ДОСТИЖИМОСТЬ: движок
крутится целиком, подделан ровно шлюз LLM, и ROE читается из готового события
`metric`. Мутация в любом звене цепочки (карта из реестра не собрана, не
доехала до цикла, не применена к значению) красит эти сторожа.

ПРОВЕРКА СТОРОЖЕЙ ОТКАТОМ (мутации прогнаны 21.08.2026, каждая покраснела):
  1. `calc_meta = {}` в `_prepare_two_stage_run` вместо чтения ячейки →
     падает `test_quarterly_roe_is_raised_to_the_year`;
  2. `do_annualize = False` в цикле выдачи →
     падает `test_quarterly_roe_is_raised_to_the_year`;
  3. `annualized(v, period_current, ...)` вместо `annualized(v, p, ...)` в
     сборке `period_values` → падает
     `test_each_column_is_raised_by_its_own_length`;
  4. снять `note` из подписи (`"formula": formula_shown`) →
     падает `test_the_signature_says_the_number_was_raised`;
  5. обернуть ЗАПАСНОЙ ВЫХОД вторым множителем — `bulk.annualized(
     bulk._value_for_period(...), period_current, do_annualize)` →
     падает `test_each_column_is_raised_by_its_own_length` (ROE 0,64);
  6. `note = "12/3" if do_annualize else ""` — множитель из признака, а не из
     длины периода → падает `test_an_annual_report_is_not_touched`;
  7. `"name_ru": str(name_ru).upper()` в строке боевого движка → падает
     `test_the_live_engine_agrees_with_the_calc_skill`;
  8. происхождение из прохода ДРУГОЙ колонки (`prov = source_trace.get(
     all_periods[-1], {}).get(enum)`) → падает
     `test_each_column_is_raised_by_its_own_length`: входы подписи оказываются
     за FY 2023, а множитель в ней — за Q1 2025.

Мутации 5–8 добавлены после проверки 21.08: сторожа 1–4 оставались ЗЕЛЁНЫМИ на
всех четырёх, то есть четыре отказа из восьми не стерёг никто.

⚠️ НЕ ПУТАТЬ С `ANNUAL(...)` В ФОРМУЛЕ — это ДРУГОЙ механизм, для потока в
ЗНАМЕНАТЕЛЕ, и он работал и до этой правки. Что вместе они не дают двойного
приведения, стережёт `tests/test_two_annualizations_are_refused.py`.
"""
from __future__ import annotations

import asyncio
import copy
import json
import uuid
from pathlib import Path

import pytest

from finengine.config import settings
from finengine.skills import extract_text as et
from finengine.skills import two_stage_extractor as ts
from finengine.skills.metric_schema import registry_for_profile

_PROFILE = "ifrs_nf_ru"

# Три показателя, которых хватает на ROE: прибыль за период и капитал на две
# даты (формула — `NET_INCOME / AVG (TOTAL_EQUITY)`).
_NET_INCOME = "NET_INCOME"
_EQUITY = "TOTAL_EQUITY"

_QUARTERLY_DOC = (
    "===PAGE 1===\n"
    "Промежуточная сокращённая консолидированная финансовая отчётность\n"
    "за три месяца, закончившихся 31 марта 2025 года\n\n"
    "| Показатель | 3 мес. 2025 | 3 мес. 2024 |\n"
    "| --- | --- | --- |\n"
    "| Прибыль за период | 400 | 300 |\n"
    "| Итого капитал | 10 000 | 9 000 |\n"
)

_ANNUAL_DOC = (
    "===PAGE 1===\n"
    "Консолидированная финансовая отчётность\n"
    "за год, закончившийся 31 декабря 2024 года\n\n"
    "| Показатель | 2024 | 2023 |\n"
    "| --- | --- | --- |\n"
    "| Прибыль за период | 400 | 300 |\n"
    "| Итого капитал | 10 000 | 9 000 |\n"
)


def _registry_for(enums: list[str]) -> dict:
    """Реестр ячейки, суженный до нужных строк. Формулы и признаки — НАСТОЯЩИЕ.

    Сужение состава, а не подмена содержания: если бы `annualize` дописывался
    здесь, сторож проверял бы собственную выдумку, а не файл ячейки.
    """
    full = registry_for_profile(_PROFILE)
    wanted = {e.upper() for e in enums}
    return {
        **full,
        "extractable": [m for m in full["extractable"]
                        if str(m["enum"]).upper() in wanted],
        "calculated": [c for c in full["calculated"]
                       if str(c[0]).upper() in wanted],
    }


def _run(monkeypatch, *, values: dict[str, dict[str, float]], registry: dict,
         doc: str = _QUARTERLY_DOC) -> list[dict]:
    """Настоящий прогон боевого движка; подделан ровно шлюз LLM.

    `values` — что «нашла» вторая стадия: enum → {ярлык периода: число}.
    Возвращаются все события `metric`.
    """
    extract_enums = [str(m["enum"]).upper() for m in registry["extractable"]]
    all_enums = extract_enums + [str(c[0]).upper() for c in registry["calculated"]]
    targets: dict[str, list[str]] = {}
    real_build = ts._build_main_extraction_prompt

    def _spy_build(**kw):
        text = real_build(**kw)
        targets[text] = [str(m["enum"]).upper() for m in kw["target_metrics"]]
        return text

    async def _fake_llm(*, prompt, system=None, model=None, response_format=None,
                        max_output=None, omit_max_output=None, **kw):
        asked = targets.get(prompt)
        if asked is None:
            # Первая стадия: улики есть у всех, чтобы никого не отсеяло.
            return json.dumps({"metrics": {
                enum: {"tables": ["T001"],
                       "quotes": [{"page": 1, "text": "Итого капитал 10 000"}]}
                for enum in extract_enums
            }}), {}
        return json.dumps({
            enum: {"values": values.get(enum, {}), "unit_text": "млн руб.", "page": 1}
            for enum in asked
        }), {}

    monkeypatch.setattr(ts, "_build_main_extraction_prompt", _spy_build)
    monkeypatch.setattr(ts.llm, "complete_with_usage", _fake_llm)

    docs_dir = Path(settings.docs_dir)
    docs_dir.mkdir(parents=True, exist_ok=True)
    doc_id = f"annualize-{uuid.uuid4().hex[:8]}"
    (docs_dir / f"{doc_id}.txt").write_text(doc, encoding="utf-8")
    stub = et._StubIndex(doc_id, et._StubDoc(company_name="", filename=doc_id))

    rows: list[dict] = []

    async def _go() -> None:
        async for event in ts.extract_stream_finengine_dev_two_stage(
            doc_id,
            "non_financial",
            index=stub,
            metric_enums=set(all_enums),
            registry_override=registry,
            include_segments=False,
            include_consolidated_group=False,
        ):
            if event.get("type") == "metric":
                rows.append(event)

    asyncio.run(_go())
    return rows


def _row(rows: list[dict], enum: str) -> dict:
    match = [r for r in rows if str(r.get("enum")).upper() == enum]
    assert match, f"движок не отдал строку {enum} — мерить нечего"
    return match[0]


# ── Главное: квартал поднимается вчетверо ───────────────────────────────────


def test_quarterly_roe_is_raised_to_the_year(monkeypatch):
    """ROE квартального отчёта = ЧЕТЫРЁХКРАТНЫЙ квартальный. Тот самый Q1 2025.

    400 / 10 000 = 4 % за квартал. Читателю обещана годовая рентабельность,
    значит напечатать движок обязан 16 % — то, что он печатал раньше, было
    занижено вчетверо и выглядело правдоподобно.
    """
    rows = _run(
        monkeypatch,
        values={_NET_INCOME: {"Q1 2025": 400.0, "Q1 2024": 300.0},
                _EQUITY: {"Q1 2025": 10_000.0, "Q1 2024": 10_000.0}},
        registry=_registry_for([_NET_INCOME, _EQUITY, "ROE"]),
    )
    roe = _row(rows, "ROE")["value_current"]
    assert roe == pytest.approx(16.0), (
        f"боевой движок отдал ROE {roe} вместо приведённого к году 16 % — "
        "признак `annualize` реестра до него не доехал, и квартальная "
        "доходность напечатана под именем годовой")


def test_an_annual_report_is_not_touched(monkeypatch):
    """Годовой отчёт множителем не двигается: 12/12 — это единица.

    Сторож стоит рядом с предыдущим не для симметрии: правка, поднявшая
    квартал, но забывшая про длину периода, завысила бы КАЖДЫЙ годовой ROE
    вчетверо — и это заметили бы уже на следующем прогоне.
    """
    rows = _run(
        monkeypatch,
        doc=_ANNUAL_DOC,
        values={_NET_INCOME: {"FY 2024": 400.0, "FY 2023": 300.0},
                _EQUITY: {"FY 2024": 10_000.0, "FY 2023": 10_000.0}},
        registry=_registry_for([_NET_INCOME, _EQUITY, "ROE"]),
    )
    row = _row(rows, "ROE")
    roe = row["value_current"]
    assert roe == pytest.approx(4.0), (
        f"годовой ROE поднят до {roe} вместо 4 % — множитель применяется без "
        "оглядки на длину периода")
    # ПОДПИСЬ ТОЖЕ НЕ ДВИГАЕТСЯ, и это отдельная проверка, а не украшение.
    # Правка `note = "12/3" if do_annualize else ""` (то есть множитель из
    # признака, а не из длины периода) оставляет число 4 % нетронутым и
    # печатает над ним «* 12/3»: читатель, воспроизводящий строку по подписи,
    # получает 16 % против стоящих 4 % и считает ошибкой РАСЧЁТ.
    assert "12/" not in row["formula"], (
        f"на годовом отчёте в подписи появился множитель: «{row['formula']}» — "
        "число не поднято, а подпись обещает, что поднято")


def test_each_column_is_raised_by_its_own_length(monkeypatch):
    """В таблице стоят колонки РАЗНОЙ длины, и каждая поднимается по своей.

    Приведение всех колонок по ярлыку основного периода — самая тихая из
    возможных ошибок здесь: текущая колонка сходится, а годовая рядом с ней
    завышена вчетверо, и подпись, стоящая одна на строку, этого не показывает.
    """
    rows = _run(
        monkeypatch,
        values={
            _NET_INCOME: {"Q1 2025": 400.0, "FY 2024": 400.0, "FY 2023": 400.0},
            _EQUITY: {"Q1 2025": 10_000.0, "FY 2024": 10_000.0, "FY 2023": 10_000.0},
        },
        registry=_registry_for([_NET_INCOME, _EQUITY, "ROE"]),
    )
    row = _row(rows, "ROE")
    by_period = {pv["period"]: pv["value"] for pv in row["period_values"]}
    assert "Q1 2025" in by_period and "FY 2024" in by_period, (
        f"в строке ROE не обе колонки: {sorted(by_period)}")
    assert by_period["Q1 2025"] == pytest.approx(16.0), (
        f"квартальная колонка {by_period['Q1 2025']} вместо 16 %")
    assert by_period["FY 2024"] == pytest.approx(4.0), (
        f"годовая колонка поднята до {by_period['FY 2024']} вместо 4 % — "
        "колонки приведены по длине ОСНОВНОГО периода, а не каждая по своей")
    # 🔴 ЗАПАСНОЙ ВЫХОД НЕ ПОДНИМАЕТ ВТОРОЙ РАЗ. Значение строки идёт двумя
    # путями: парный проход (`calc_current`) и, когда тот молчит, отбор из уже
    # поднятых `period_values`. ИМЕННО ЗДЕСЬ работает второй: на смешанных
    # колонках парный проход числа не даёт, и `value_current` приезжает из
    # `_value_for_period`. Множитель к нему уже применён — приписать его ещё
    # раз значит выдать ROE 64 % на квартале, и ни одна из проверок выше этого
    # не увидит: колонки-то подняты правильно.
    assert row["value_current"] == pytest.approx(16.0), (
        f"value_current = {row['value_current']} вместо 16 % — приведение к "
        "году наложено дважды: сначала на колонку, потом на выбранное из неё "
        "значение")
    # ПОДПИСЬ ОДНА НА СТРОКУ, А МНОЖИТЕЛЬ — НА КОЛОНКУ, и так остаётся: форма
    # унаследована от всех пяти производителей расчётных строк. Терпима она
    # ровно потому, что подпись целиком про ОДНУ колонку — ту же, что названа у
    # входов (Б5, 20.08): читатель, сверяющий FY 2024 по множителю «12/3»,
    # сверяет её не с той колонкой и ВИДИТ это по периоду входов. Разъедется —
    # цена станет молчаливой, поэтому связка проверяется парой.
    # Красит пару мутация «происхождение из прохода другой колонки»
    # (`prov = source_trace.get(all_periods[-1], ...)`). Мутация «множитель от
    # `period_prev`» её НЕ красит и красить не может: предыдущий период движок
    # берёт той же длины (Q1 2025 → Q1 2024, замерено 21.08).
    assert {i["period"] for i in row["inputs"]} == {"Q1 2025"}, (
        f"входы подписи сняты за {sorted({i['period'] for i in row['inputs']})}, "
        "а множитель в формуле — за Q1 2025")
    assert "12/3" in row["formula"], (
        f"множитель подписи «{row['formula']}» не от той колонки, за которую "
        "сняты входы")


# ── Подпись: приведение обязано быть видно ──────────────────────────────────


def test_the_signature_says_the_number_was_raised(monkeypatch):
    """Множитель стоит в подписи ДРОБЬЮ — иначе входы не дают напечатанного.

    Аналитик, которому показали `NET_INCOME / AVG(TOTAL_EQUITY)` и входы
    400 и 10 000, поделит вход на вход и получит 4 % при напечатанных 16 %.
    Расхождение вчетверо выглядит ошибкой расчёта, и доверие теряет ВСЯ
    таблица, а не одна строка.
    """
    rows = _run(
        monkeypatch,
        values={_NET_INCOME: {"Q1 2025": 400.0, "Q1 2024": 300.0},
                _EQUITY: {"Q1 2025": 10_000.0, "Q1 2024": 10_000.0}},
        registry=_registry_for([_NET_INCOME, _EQUITY, "ROE"]),
    )
    formula = _row(rows, "ROE")["formula"]
    assert "12/3" in formula, (
        f"подпись «{formula}» молчит о приведении к году — по ней напечатанное "
        "число не воспроизводится")


def test_a_row_without_the_flag_keeps_a_clean_signature(monkeypatch):
    """Показателю БЕЗ признака множитель в подпись не приписывается.

    Отрицательный контроль к предыдущему: правка, дописывающая «12/3» всем
    подряд, прошла бы его и соврала бы в 48 строках реестра из 51.
    """
    rows = _run(
        monkeypatch,
        values={"TOTAL_ASSETS": {"Q1 2025": 30_000.0}, _EQUITY: {"Q1 2025": 10_000.0}},
        registry=_registry_for(["TOTAL_ASSETS", _EQUITY, "ASSETS_TO_EQUITY_RATIO"]),
    )
    formula = _row(rows, "ASSETS_TO_EQUITY_RATIO")["formula"]
    assert "12/" not in formula, (
        f"в подписи показателя без признака `annualize` появился множитель: «{formula}»")


# ── Боевой движок считает то же, что и два других производителя ─────────────


def test_the_live_engine_agrees_with_the_calc_skill(monkeypatch):
    """Та же СТРОКА ЦЕЛИКОМ, что отдаёт навык расчёта `aifa-calc`.

    Разные числа под одним именем из разных мест — это ровно #163, и правка
    «в одном из трёх» уже стоила нам этого дефекта дважды.

    СВЕРЯЕТСЯ ВСЯ СТРОКА, А НЕ ОДНО ПОЛЕ, и это куплено проверкой 21.08: тест с
    названием «движки согласны» сверял только `value_current` и был зелёным на
    строке, где движки расходились по `unit` (по десяти ячейкам таких строк 152).
    Сторож, проверяющий одно поле из восьми, обещает согласие и не проверяет его.
    """
    from finengine.skills.ratios import compute_ratios

    registry = _registry_for([_NET_INCOME, _EQUITY, "ROE"])
    rows = _run(
        monkeypatch,
        values={_NET_INCOME: {"Q1 2025": 400.0, "Q1 2024": 300.0},
                _EQUITY: {"Q1 2025": 10_000.0, "Q1 2024": 10_000.0}},
        registry=registry,
    )
    extracted = [copy.deepcopy(r) for r in rows if not r.get("is_calculated")]
    by_skill = compute_ratios(extracted, _PROFILE, only=["ROE"], period_current="Q1 2025")
    assert by_skill, "навык расчёта не отдал ROE — сверять не с чем"
    live, skill = _row(rows, "ROE"), by_skill[0]

    assert set(live) == set(skill), (
        "у строк разный СОСТАВ полей: только у боевого "
        f"{sorted(set(live) - set(skill))}, только у навыка "
        f"{sorted(set(skill) - set(live))}")
    # Исключений НЕТ. До слияния здесь стояло `unit`: боевой движок подписывал
    # единицу догадкой по формуле, навык расчёта звал `unit_for_calculated`, и
    # на ROE выходило «млн. руб.» против пустой строки. Правка приехала своим
    # коммитом, расхождений ноль, исключение снято.
    #
    # ⚠️ ЧЕГО ЭТОТ СТОРОЖ НЕ ЛОВИТ, И ЭТО ПРОВЕРЕНО МУТАЦИЕЙ: возврат догадки
    # `"%" if "* 100" in formula` оставляет его ЗЕЛЁНЫМ. Сверяется одна строка —
    # ROE, а её формула несёт сотню, и оба механизма отвечают «%» одинаково.
    # Класс «разы» и «дни» (85 записей из 361) сторожит
    # `tests/test_the_live_engine_labels_units_like_the_registry.py`.
    diverging = {field for field in live if live[field] != skill[field]}
    assert not diverging, (
        "боевой движок и навык расчёта разошлись по полям "
        f"{sorted(diverging)}: "
        + "; ".join(f"{f}: боевой {live[f]!r} против навыка {skill[f]!r}"
                    for f in sorted(diverging)))
