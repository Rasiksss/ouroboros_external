"""Показатели не висят на поставщиках, до которых мы не достучимся (17.08.2026).

ЗАМЕРЫ ИЗ ПРОД-КОНТЕЙНЕРА:
  Investing.com — 403 И напрямую, И через прокси; тело ответа 3 байта. Это их
    защита от датацентровых адресов, а не наша сетевая фильтрация (та отдаёт
    HTML-страницу nginx, как было с Tavily). Прокси не помогает.
  NBS (статистика Китая) — 403 обоими путями. Сборщик `_fetch_nbs` в коде есть,
    и им не пользуется ни одна запись реестра. Теперь понятно почему.
  Росстат — сайт ОТДАЁТ 794 КБ, но цепочка сертификата не проверяется: издатель
    «Russian Trusted Sub CA» с ключом 77:3D:D9…, а в открытом наборе Минцифры
    лежат только D1:E1:71… и E1:D1:81…. Сервер свой промежуточный не присылает.
  FRED, Eurostat, ЦБ РФ, EIA — работают.

ВВП РФ переведён с Investing на FRED — единственная замена, которую FRED может
закрыть свежими данными (наблюдения до 2026-01). Остальные их международные
ряды заброшены: промпроизводство КНР до 2023-11, ИЦП КНР до 2022-12,
промпроизводство РФ до 2021-12, ISM PMI убран по лицензии.

Сторож фиксирует ДОСТИГНУТОЕ, чтобы правку не откатили обратно молча.
"""
from __future__ import annotations

from finengine.skills.news.macro_indicators import _REGISTRY

_REACHABLE = {"fred", "eurostat", "cbr_scrape", "eia"}


def _spec(ind_id: str):
    return next(s for s in _REGISTRY if s.id == ind_id)


def test_ru_gdp_moved_off_investing():
    """Отрицательный контроль: вернул provider='investing' — краснеет."""
    g = _spec("ru_gdp_q")
    assert g.provider == "fred", "ВВП РФ снова на недостижимом Investing"
    assert g.series_id == "NGDPRNSAXDCRUQ"


def test_ru_gdp_asks_for_year_over_year():
    """Ряд отдаёт УРОВЕНЬ, проценты считает наш код из самого ряда. Раньше был
    только квартал к кварталу — по ВВП это наименее говорящая величина."""
    assert "yoy" in _spec("ru_gdp_q").compares


def test_unit_matches_what_the_series_returns():
    """Ряд в млн рублей, а запись раньше объявляла «%». Разойдись они — на
    экране появилось бы «32 837 291 %»."""
    assert _spec("ru_gdp_q").unit == "млн ₽"


def test_nbs_stays_unused_until_it_is_reachable():
    """Сборщик NBS есть, но с этого стенда он 403. Появится запись на нём —
    показатель молча пропадёт из выпуска, а причина будет неочевидна."""
    on_nbs = [s.id for s in _REGISTRY if s.provider == "nbs"]
    assert not on_nbs, f"показатели повешены на недостижимый NBS: {on_nbs}"


def test_no_new_indicator_lands_on_an_unknown_provider():
    """Список поставщиков закрытый: у каждого свой сборщик и своя проверенная
    достижимость. Новый — только вместе с замером."""
    known = _REACHABLE | {"investing", "nbs"}
    unknown = {s.provider for s in _REGISTRY} - known
    assert not unknown, f"неизвестный поставщик: {unknown}"


def test_reachable_providers_cover_at_least_half_the_registry():
    """Считаем вслух: сколько показателей реально доезжает. Упадёт ниже
    половины — значит незаметно повесили ещё что-то на Investing."""
    ok = [s for s in _REGISTRY if s.provider in _REACHABLE]
    assert len(ok) * 2 >= len(_REGISTRY), (
        f"достижимых показателей {len(ok)} из {len(_REGISTRY)} — меньше половины")
