"""Макро-блок не выдаёт индекс за проценты (17.08.2026).

РАЗБОР ВЛАДЕЛЬЦА ПО ЖИВОМУ ВЫПУСКУ. Он прочитал строку
    «Промышленная инфляция США (PPI): 284.1 — м/м -0.8%»
и спросил: «это действительно у них такая промышленная инфляция огромная?»
Вопрос ровно правильный, и ответ — нет: 284,1 это ИНДЕКС 1982=100. Единица не
показывалась вовсе, и число читалось как проценты. Для финансового продукта это
не косметика, а неверные данные на экране.

Тем же разбором вскрылись ещё три вещи:
  * «промпроизводство еврозоны 98,7» — тоже индекс без единицы;
  * «(данные за июл 2026)» — это ПЕРИОД наблюдения, а не дата выхода; владелец
    просил «понимать, это 15 августа или 16»;
  * источник был общей строкой на регион и вёл на главную поставщика.

🔴 И МОЯ СОБСТВЕННАЯ ОШИБКА, НАЙДЕННАЯ ТУТ ЖЕ. Днём я перевёл ВВП РФ на ряд
FRED `NGDPRNSAXDCRUQ` и оставил сравнение квартал к кварталу. Ряд БЕЗ сезонной
очистки: у ВВП первый квартал всегда ниже четвёртого, и в выпуск уехало
«кв/кв −20.6%». Это не спад экономики, это сезонность. Убрано.
"""
from __future__ import annotations

import pytest

from finengine.skills.news.macro_indicators import (
    _REGISTRY,
    _fmt_pub_date,
    render_macro_dashboard_md,
)

_INDEX_SERIES = ("us_cpi", "us_core_cpi", "us_ppi", "us_indpro")


@pytest.mark.parametrize("ind_id", _INDEX_SERIES)
def test_index_series_declare_their_base(ind_id):
    """Отрицательный контроль: вернул пустую единицу — краснеет.

    Индекс без базы неотличим от процента: 284,1 читается как инфляция.
    """
    spec = next(s for s in _REGISTRY if s.id == ind_id)
    assert spec.unit, f"{ind_id}: единица пуста — индекс прочитают как проценты"
    assert "индекс" in spec.unit and "=" in spec.unit, (
        f"{ind_id}: единица «{spec.unit}» не называет базу индекса")


def test_unseasonal_series_has_no_quarter_over_quarter():
    """МОЯ ОШИБКА, ЗАКРЫТАЯ СТОРОЖЕМ. На ряду без сезонной очистки сравнение
    соседних кварталов бессмысленно: первый квартал всегда ниже четвёртого."""
    gdp = next(s for s in _REGISTRY if s.id == "ru_gdp_q")
    assert "NSA" in gdp.series_id or gdp.series_id == "NGDPRNSAXDCRUQ"
    assert "qoq" not in gdp.compares, (
        "кв/кв вернулось на неочищенный ряд — в выпуск поедет сезонность, "
        "выданная за динамику")


def test_publication_date_shows_the_day():
    """«Опубликовано 13 авг», а не «авг 2026»: вся ценность блока «вышли на
    этой неделе» в том, какого числа."""
    assert _fmt_pub_date("2026-08-13") == "13 авг"
    assert _fmt_pub_date("") == "—"
    assert _fmt_pub_date("мусор") == "—"


def _sample() -> dict:
    return {"us_ppi": {
        "id": "us_ppi", "ru_name": "Промышленная инфляция США (PPI)",
        "region": "US", "unit": "индекс 1982=100", "latest_value": 284.1,
        "latest_date": "2026-07-01", "published": "2026-08-13",
        "source_url": "https://fred.stlouisfed.org/series/PPIACO",
        "first_seen": "2026-08-15",
        "compares": [{"label": "м/м", "pct": -0.8}, {"label": "г/г", "pct": 8.3}],
    }}


def test_rendered_row_carries_unit_date_and_link():
    """Всё, о чём спрашивал владелец, должно быть В СТРОКЕ показателя."""
    md = render_macro_dashboard_md(_sample(), mode="full")
    assert "индекс 1982=100" in md, "единица не доехала до выпуска"
    assert "13 авг" in md, "дата публикации не доехала"
    assert "fred.stlouisfed.org/series/PPIACO" in md, (
        "ссылка ведёт не на конкретный ряд")
    assert "|" in md and "---" in md, "блок не стал таблицей"


def test_the_pointless_subtitle_is_gone():
    """«Где находится экономика прямо сейчас (последние публикации + динамика)»
    — владелец назвал этот комментарий лишним, и он ничего не добавлял к
    названию раздела."""
    md = render_macro_dashboard_md(_sample(), mode="full")
    assert "прямо сейчас" not in md
    assert "текущие значения" not in md, "старое название раздела вернулось"


def test_week_section_is_named_by_what_it_shows():
    """«Макро — обновлено на этой неделе» → «Макроиндикаторы — вышли на этой
    неделе»: раздел про ПУБЛИКАЦИИ, а не про наши обновления."""
    md = render_macro_dashboard_md(_sample(), mode="week_only")
    assert "Макроиндикаторы" in md


def test_percent_series_show_points_not_relative_change():
    """🔴 РЕГРЕССИЯ, КОТОРУЮ Я САМ И ВНЁС, ПЕРЕПИСЫВАЯ БЛОК.

    Новая ячейка динамики предпочитала относительное изменение всему прочему, и
    в выпуск уехало «ИЦП РФ 9,40 % — м/м +70,9%». Это процент от процента: ИЦП
    вырос с 5,5 % до 9,4 %, то есть на 3,9 ПУНКТА. Ставка ЦБ показывалась как
    «-1.8%» вместо «-0.25 пп».

    Правило: единица «%» → пункты; индексы, деньги, штуки → проценты.
    """
    from finengine.skills.news.macro_indicators import _delta_cell

    rate = {"unit": "%", "compares": [{"label": "м/м", "pct": -1.8, "abs": -0.25}]}
    ppi_ru = {"unit": "%", "compares": [{"label": "м/м", "pct": 70.9, "abs": 3.9}]}
    index = {"unit": "индекс 1982=100", "compares": [{"label": "м/м", "pct": -0.8, "abs": -2.3}]}

    assert _delta_cell(rate, "м/м") == "-0.25 пп"
    assert _delta_cell(ppi_ru, "м/м") == "+3.90 пп"
    assert _delta_cell(index, "м/м") == "-0.8%"


def test_cache_of_the_old_shape_is_dropped(tmp_path, monkeypatch):
    """Кэш живёт 6–24 часа. После правки состава полей он ещё сутки отдавал бы
    записи без единицы, даты и ссылки — блок выглядел бы сломанным, хотя код уже
    верный. Поймано на живой выкатке: таблица встала, колонки пустые.

    🔵 21.08.2026: сторож читал `inspect.getsource(_read_cache)` и искал в нём
    слово `source_url`. Мутация «`kept = dict(data)`, отбор — в комментарий»
    оставляла его зелёным. Теперь кэш кладётся на диск и читается.
    """
    import json as _json

    from finengine.skills.news import macro_indicators as mi

    cache = tmp_path / "macro_cache.json"
    cache.write_text(_json.dumps({"ts": 0, "data": {
        "старая_форма": {"id": "старая_форма", "latest_value": 1.0},
        "новая_форма": {"id": "новая_форма", "latest_value": 2.0,
                        "source_url": "https://fred.stlouisfed.org/series/PPIACO"},
    }}), encoding="utf-8")
    monkeypatch.setattr(mi, "_CACHE_PATH", cache)

    kept = (mi._read_cache() or {}).get("data") or {}
    assert set(kept) == {"новая_форма"}, (
        f"старые записи кэша больше не отсеиваются — отдано {sorted(kept)}; "
        "сутки после выкатки таблица будет с пустыми колонками")


# ─── Разбор выпуска 18.08.2026: три вещи, которые таблица всё ещё говорила
#     неправду. Вчера закрыли ЧЕТЫРЕ американских ряда — и на этом остановились.


def test_no_indicator_ships_without_a_unit():
    """18.08.2026, живой выпуск. Единицу вчера получили только us_cpi,
    us_core_cpi, us_ppi, us_indpro. В таблице осталось ПЯТЬ рядов с «Ед.» = «—»
    (UMCSENT 55.1, HICP еврозоны, ИЦП и промпроизводство еврозоны, CPI Китая)
    плюс девять PMI — то есть та же болезнь, от которой лечили PPI: число без
    единицы читается как проценты.

    Сторож смотрит на РЕЕСТР целиком, а не на список из четырёх имён, чтобы
    новый показатель нельзя было завести без единицы.
    """
    empty = [s.id for s in _REGISTRY if not s.unit.strip()]
    assert not empty, f"единица пуста у {empty} — числа прочитают как проценты"


def test_index_bases_are_the_ones_the_providers_publish():
    """Базы взяты СО СТРАНИЦ ПОСТАВЩИКОВ 18.08.2026, а не по памяти.

    Проверка была не формальной: по HICP еврозоны «очевидная» база 2015=100
    оказалась неверной — FRED на странице CP0000EZ19M086NEST пишет
    «Index 2025=100» (Eurostat перебазировал ряд). Пины ниже держат именно
    проверенные значения, чтобы правдоподобная догадка не заменила их молча.
    """
    verified = {
        "us_sent":   "1966",  # FRED UMCSENT: Index 1966:Q1=100
        "ez_cpi":    "2025",  # FRED CP0000EZ19M086NEST: Index 2025=100
        "ez_ppi":    "2021",  # Eurostat sts_inppd_m, unit I21: Index, 2021=100
        "ez_indpro": "2021",  # Eurostat sts_inpr_m,  unit I21: Index, 2021=100
        "cn_cpi":    "2015",  # FRED CHNCPIALLMINMEI: Index 2015=100
    }
    for ind_id, base in verified.items():
        spec = next(s for s in _REGISTRY if s.id == ind_id)
        assert spec.unit.startswith("индекс"), f"{ind_id}: {spec.unit!r} — не индекс"
        assert f"{base}=100" in spec.unit.replace(":Q1", ""), (
            f"{ind_id}: база «{spec.unit}» разошлась с проверенной {base}=100")


def test_pmi_units_name_the_growth_threshold():
    """У PMI базы нет — есть порог: 50 разделяет рост и спад. «54.0» без этого
    в колонке «Ед.» = «—» читается как проценты, ровно как читался PPI."""
    pmi = [s for s in _REGISTRY if "PMI" in s.ru_name]
    assert len(pmi) == 9, "состав PMI-рядов изменился — проверить единицы заново"
    for s in pmi:
        assert "50" in s.unit, f"{s.id}: единица «{s.unit}» не называет порог 50"


def _oil_sample() -> dict:
    """Запасы нефти EIA: частота W, сравнения — н/н и г/г."""
    return {"us_oil_stock": {
        "id": "us_oil_stock", "ru_name": "Запасы нефти в США (EIA, exc. SPR)",
        "region": "US", "unit": "тыс. барр.", "latest_value": 421000.0,
        "latest_date": "2026-08-07", "published": "",
        "source_url": "https://www.eia.gov/petroleum/supply/weekly/",
        "first_seen": "2026-08-13",
        "compares": [{"label": "н/н", "pct": 4.3, "abs": 17000.0},
                     {"label": "г/г", "pct": -2.1, "abs": -9000.0}],
    }}


def test_weekly_change_is_not_passed_off_as_monthly():
    """18.08.2026, живой выпуск: у запасов нефти EIA «+4.3%» стояло в колонке
    «м/м». Своей колонки у недельного изменения нет, и отрисовка молча роняла
    его в месячную — недельный скачок читался как месячный."""
    md = render_macro_dashboard_md(_oil_sample(), mode="full")
    assert "+4.3% н/н" in md, "недельное изменение отдано без своей подписи"
    assert "| +4.3% |" not in md, "голое «+4.3%» осталось в колонке «м/м»"


def test_stale_series_is_marked_in_the_period_cell():
    """18.08.2026, живой выпуск: «Инфляция Китая (CPI, OECD)» — период апр 2025,
    шестнадцать месяцев давности (зеркало OECD в FRED замерло). В таблице она
    стояла наравне со свежими, и прошлогоднее число читалось как текущее.

    Пороги — по частоте ряда. Частота берётся ИЗ РЕЕСТРА по id: в записи ниже
    поля `freq` нет, и появиться оно не должно — состав записи кэша менять
    нельзя, иначе прод сутки отдаёт форму, которую `_read_cache` выбросит.
    """
    from datetime import datetime

    from finengine.skills.news.macro_indicators import _period_cell

    now = datetime(2026, 8, 18)
    assert _period_cell({"id": "cn_cpi", "latest_date": "2025-04-01"}, now) == (
        "апр 2025 · ⚠️ ряд отстаёт")
    # Свежие ряды пометку не получают. Даты ниже — ЖИВЫЕ на 18.08.2026
    # (CPIAUCSL: июл 2026, GDPC1: II кв., EIA: неделя к 7 августа), то есть
    # порог сверен с обычной задержкой публикации, а не выбран на глаз.
    assert _period_cell({"id": "us_cpi", "latest_date": "2026-07-01"}, now) == "июл 2026"
    assert _period_cell({"id": "us_gdp", "latest_date": "2026-04-01"}, now) == "апр 2026"
    assert _period_cell({"id": "us_oil_stock", "latest_date": "2026-08-07"}, now) == (
        "авг 2026")
    # А недельный ряд, отставший на полтора месяца, — получает.
    assert "отстаёт" in _period_cell(
        {"id": "us_oil_stock", "latest_date": "2026-07-01"}, now)


def test_unit_reaches_a_row_that_was_cached_without_one():
    """НЕДОСТАВКА, а не логика: единица лежит В ЗАПИСИ КЭША, и записи, сложенные
    до этой правки, лежат с пустой. Кэш живёт 6–24 часа, а строки Investing
    после 403 не обновляются вовсе — девять PMI остались бы с «Ед.» = «—»
    навсегда, и правка в реестре до экрана бы не доехала."""
    cached_before_the_fix = {"us_pmi_man": {
        "id": "us_pmi_man", "ru_name": "PMI промышленности США (ISM)",
        "region": "US", "unit": "", "latest_value": 54.0,
        "latest_date": "2026-08-01", "published": "",
        "source_url": "https://www.investing.com/economic-calendar/ism-manufacturing-pmi-173",
        "first_seen": "2026-08-15",
        "compares": [{"label": "м/м", "pct": 2.5, "abs": 1.3}],
    }}
    md = render_macro_dashboard_md(cached_before_the_fix, mode="full")
    # Проверяем ИМЕННО колонку «Ед.» (третью), а не наличие текста где-нибудь.
    assert "| 54.00 | индекс (50 = порог роста) |" in md, (
        "единица из реестра не подставилась в колонку «Ед.»")


def test_stale_mark_reaches_the_digest():
    """Сторож на помётку в ячейке бесполезен, если ячейка не доехала до выпуска:
    на «поправил, но не подключил» мы стояли трижды."""
    stale = {"cn_cpi": {
        "id": "cn_cpi", "ru_name": "Инфляция Китая (CPI, OECD)", "region": "CN",
        "unit": "индекс 2015=100", "latest_value": 104.3,
        "latest_date": "2025-04-01", "published": "2026-08-01",
        "source_url": "https://fred.stlouisfed.org/series/CHNCPIALLMINMEI",
        "first_seen": "2026-08-15",
        "compares": [{"label": "м/м", "pct": 0.1, "abs": 0.1}],
    }}
    md = render_macro_dashboard_md(stale, mode="full")
    assert "ряд отстаёт" in md, "устаревший ряд подан как текущий"


# ─── Разбор состязательных линз 18.08.2026: правка против вранья сама начала
#     врать в двух местах. Обе находки воспроизведены прогоном.


def test_stale_mark_survives_a_full_publication_cycle():
    """🔴 РЕГРЕССИЯ ПРЕДЫДУЩЕЙ ПРАВКИ. Пороги пометки «ряд отстаёт» были взяты
    от СНИМКА живых дат (M=92, Q=183) — короче интервала между публикациями, и
    здоровые ряды начали бы помечаться отставшими.

    Точка датируется НАЧАЛОМ периода, а выходит после его конца, поэтому лаг
    свежайшей точки растёт весь межвыпускной промежуток:
      * III кв. (2026-07-01) BEA публикует advance-оценкой в конце октября; со
        2 октября и до неё свежайшая точка ВВП США и еврозоны — 2026-04-01, её
        лаг уже 187 дней при пороге 183;
      * промпроизводство еврозоны за июнь выходит в середине августа, к кануну
        следующего выпуска лаг июньской точки — ~105 дней при пороге 92.

    Даты ниже — те самые, на которых линза воспроизвела ложную пометку.
    """
    from datetime import datetime

    from finengine.skills.news.macro_indicators import _period_cell

    assert _period_cell(
        {"id": "us_gdp", "latest_date": "2026-04-01"}, datetime(2026, 10, 5),
    ) == "апр 2026", "квартальный ряд помечен до выхода следующей точки"
    assert _period_cell(
        {"id": "ez_gdp", "latest_date": "2026-04-01"}, datetime(2026, 10, 25),
    ) == "апр 2026", "квартальный ряд помечен до выхода следующей точки"
    assert _period_cell(
        {"id": "ez_indpro", "latest_date": "2026-06-01"}, datetime(2026, 9, 3),
    ) == "июн 2026", "месячный ряд помечен на обычной задержке публикации"
    assert _period_cell(
        {"id": "ez_ppi", "latest_date": "2026-06-01"}, datetime(2026, 9, 12),
    ) == "июн 2026", "месячный ряд помечен на обычной задержке публикации"

    # И ровно тот случай, ради которого пометка делалась, — по-прежнему ловится.
    assert _period_cell(
        {"id": "cn_cpi", "latest_date": "2025-04-01"}, datetime(2026, 8, 18),
    ) == "апр 2025 · ⚠️ ряд отстаёт", "замершее зеркало OECD снова подано как свежее"
    assert "отстаёт" in _period_cell(
        {"id": "us_oil_stock", "latest_date": "2026-07-01"}, datetime(2026, 8, 18),
    ), "недельный ряд полуторамесячной давности подан как свежий"


def _gdp_sample() -> dict:
    """ВВП США: частота Q, сравнения — кв/кв и г/г. Числа живые (кэш 17.08)."""
    return {"us_gdp": {
        "id": "us_gdp", "ru_name": "ВВП США (real)", "region": "US",
        "unit": "млрд $ (SAAR)", "latest_value": 24152.656,
        "latest_date": "2026-01-01", "published": "2026-07-30",
        "source_url": "https://fred.stlouisfed.org/series/GDPC1",
        "first_seen": "2026-08-15",
        "compares": [{"label": "кв/кв", "pct": 0.4028, "abs": 96.907},
                     {"label": "г/г", "pct": 2.5668, "abs": 604.446}],
    }}


def test_quarterly_change_reaches_the_screen():
    """ТА ЖЕ БОЛЕЗНЬ, ЧТО У EIA: динамика посчитана и не доехала. Ячейку «м/м»
    научили подхватывать недельное «н/н» и забыли квартальное — у рядов частоты
    Q вычисленное «кв/кв» отбрасывалось, и колонка динамики выходила пустой."""
    md = render_macro_dashboard_md(_gdp_sample(), mode="full")
    assert "+0.4% кв/кв" in md, "квартальное изменение не доехало до выпуска"
    assert "| +0.4% |" not in md, "голое «+0.4%» осталось в колонке «м/м»"
    assert "| — | +2.6% |" not in md, "колонка динамики осталась пустой"


def test_row_with_quarter_over_quarter_only_is_not_left_without_dynamics():
    """ВВП Китая приходит с ЕДИНСТВЕННЫМ сравнением — кв/кв. При отброшенном
    «кв/кв» его строка выходила вообще без динамики: обе колонки «—»."""
    cn_gdp = {"cn_gdp": {
        "id": "cn_gdp", "ru_name": "ВВП Китая (Y/Y)", "region": "CN",
        "unit": "%", "latest_value": 5.2, "latest_date": "2026-04-01",
        "published": "",
        "source_url": "https://www.investing.com/economic-calendar/chinese-gdp-461",
        "first_seen": "2026-08-15",
        "compares": [{"label": "кв/кв", "pct": 4.0, "abs": 0.2}],
    }}
    md = render_macro_dashboard_md(cn_gdp, mode="full")
    row = next(ln for ln in md.splitlines() if "ВВП Китая" in ln)
    assert "+0.20 пп кв/кв" in row, f"строка вышла без динамики: {row}"
