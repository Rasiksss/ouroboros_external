"""Справочник ГОТОВ заменить реестр в извлечении — и что при этом изменится (#250).

ЗАЧЕМ ЭТОТ ФАЙЛ. Справочник построен (10 ячеек, 711 показателей), но боевое
извлечение по-прежнему берёт строки из `registry_for_profile`
(`station-skills/aifa-extract/scripts/extract.py:86`). Пока источник не
переключён, справочник — параллельная копия, и вопрос «а можно ли переключать»
должен отвечаться прогоном, а не памятью.

ЧТО ЗДЕСЬ ДОКАЗЫВАЕТСЯ, тремя утверждениями:

  1. СОСТАВ СОВПАДАЕТ — набор показателей из справочника равен реестровому.
  2. НЕТ ПРОТИВОРЕЧИЙ — там, где реестр говорит, справочник говорит ТО ЖЕ.
     Это главное: переключение не может поменять смысл ни одного поля.
  3. ОБОГАЩЕНИЕ ПОСЧИТАНО ПОИМЁННО — где справочник богаче, там и на сколько.
     Это и есть цена переключения: промпт станет другим, и вот в скольких
     местах. Число зафиксировано, чтобы следующая правка была видна.

⚠️ ПОЧЕМУ ПЕРЕКЛЮЧЕНИЕ НЕ СДЕЛАНО ВМЕСТЕ С МОСТОМ. Замер 15.08.2026: из 4266
полей 4067 совпадают, 199 у справочника богаче, противоречий ноль. Значит
переключение ничего не теряет — но меняет промпт в 199 местах, а это правка
КАЧЕСТВА. Её место за бенчмарком, как у всех прошлых переключений проекта.

ПРОВЕРКА СТОРОЖА ОТКАТОМ: испортить любое значение в файле ячейки так, чтобы
оно противоречило реестру (не дополняло, а спорило) — `test_catalogue_never_
contradicts_the_registry` обязан назвать показатель и поле.
"""
from __future__ import annotations

import pytest

from finengine.skills.extraction import catalogue as cat
from finengine.skills.metric_schema import PROFILE_IDS, registry_for_profile

# Поля, которые ЧИТАЕТ ПРОМПТ (`bulk_extractor.py:4216-4223`). Расхождение здесь
# меняет то, что видит модель; расхождение в остальных — нет.
PROMPT_FIELDS = ("name_ru", "name_en", "aliases_ru", "aliases_en", "description", "hint")

# Замер 23.09.2026 (было 199 на 15.08.2026). Разница — следствие Среза 13
# (docs/build-plan/23-srez-13-kompoziciya-reestra.md): реестр собирается
# слоями поверх старых 10 монолитных профилей, часть прежнего расхождения
# закрылась при слиянии слоёв. Растёт — значит справочник обогатили; падает —
# значит обогащение потеряли. И то и другое обязано быть замеченным.
ENRICHED_FIELDS = 90


def _pair(cell: str):
    rows = {m["enum"]: m for m in registry_for_profile(cell)["extractable"]}
    return rows, {m.enum: m for m in cat.load(cell).select()}


def _as_list(v) -> list | None:
    return list(v) if isinstance(v, (list, tuple)) else None


@pytest.mark.parametrize("cell", PROFILE_IDS)
def test_the_set_of_metrics_is_the_same(cell: str):
    """Состав совпадает: переключение не добавит и не уберёт ни одного показателя."""
    rows, metrics = _pair(cell)
    assert set(rows) == set(metrics), (
        f"{cell}: состав разошёлся — только в реестре {sorted(set(rows) - set(metrics))}, "
        f"только в справочнике {sorted(set(metrics) - set(rows))}")


@pytest.mark.parametrize("cell", PROFILE_IDS)
def test_the_catalogue_never_contradicts_the_registry(cell: str):
    """ГЛАВНОЕ: где реестр говорит, справочник говорит то же.

    Дополнять можно (описание получило абзац, синонимов стало больше), спорить
    нельзя. Переключение источника не имеет права поменять смысл поля.
    """
    rows, metrics = _pair(cell)
    conflicts: list[str] = []
    for enum, row in rows.items():
        m = metrics[enum]
        for f in PROMPT_FIELDS:
            src, got = row.get(f), getattr(m, f)
            if not src:
                continue  # реестр молчит — справочник вправе сказать
            src_list, got_list = _as_list(src), _as_list(got)
            if src_list is not None:
                if not set(src_list) <= set(got_list or []):
                    conflicts.append(f"{enum}.{f}: потеряны {sorted(set(src_list) - set(got_list or []))}")
            elif f == "description":
                # Описание справочника = описание реестра + свои абзацы.
                if got.split("\n\n")[0] != src:
                    conflicts.append(f"{enum}.{f}: первый абзац не равен реестровому")
            elif got != src:
                conflicts.append(f"{enum}.{f}: {got!r} вместо {src!r}")
    assert not conflicts, (
        f"{cell}: справочник СПОРИТ с реестром в {len(conflicts)} местах — "
        f"{conflicts[:8]}. Переключать источник нельзя, пока это так.")


def test_the_price_of_the_switch_is_counted():
    """Насколько именно изменится промпт после переключения — числом.

    Не «примерно ничего»: 199 полей. Если число уехало, переключение стало
    другой правкой, и оценивать её надо заново.
    """
    enriched = 0
    for cell in PROFILE_IDS:
        rows, metrics = _pair(cell)
        for enum, row in rows.items():
            m = metrics[enum]
            for f in PROMPT_FIELDS:
                src, got = row.get(f), getattr(m, f)
                src_list, got_list = _as_list(src), _as_list(got)
                if src_list is not None:
                    enriched += set(got_list or []) > set(src_list)
                elif f == "description":
                    # Обогащение — это «реестр молчал» ИЛИ «первый абзац тот же,
                    # дальше добавлено». Спор сюда попасть не может: его ловит
                    # тест выше и роняет прогон раньше этого счёта.
                    enriched += bool(got) and got != (src or "") and (
                        not src or got.split("\n\n")[0] == src)
                else:
                    enriched += bool(got) and not src
    assert enriched == ENRICHED_FIELDS, (
        f"обогащённых полей {enriched}, а зафиксировано {ENRICHED_FIELDS} — "
        "справочник изменился; пересчитайте цену переключения и обновите число")


@pytest.mark.parametrize("cell", PROFILE_IDS)
def test_the_bridge_gives_the_engine_the_shape_it_expects(cell: str):
    """Мост отдаёт строки в форме движка, а не дата-класс.

    Движок читает `list[dict]` по ключам; отдай ему `Metric` — молча получит
    пустые подсказки у ВСЕХ показателей, потому что `.get` на дата-классе нет.
    """
    rows = cat.registry_rows(cell)
    assert len(rows) == len(registry_for_profile(cell)["extractable"])
    for r in rows:
        assert isinstance(r, dict), "строка не словарь — движок прочитает её как пустую"
        assert r["enum"] and r["name_ru"]
        assert isinstance(r["aliases_ru"], list) and isinstance(r["aliases_en"], list)
        assert r["period_type"] in ("point", "duration")


def test_the_bridge_keeps_the_asked_order():
    """Порядок запрошенных имён — как попросили: его задаёт отбор по смыслу."""
    asked = ["NET_INCOME", "SALES_REVENUE", "EBITDA"]
    assert [r["enum"] for r in cat.registry_rows("ifrs_nf_ru", asked)] == asked
