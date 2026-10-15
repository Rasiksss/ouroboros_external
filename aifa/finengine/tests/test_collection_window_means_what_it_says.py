"""Окно сбора значит то, что написано (18.08.2026).

ЖИВОЙ ПРОГОН НА ПРОДЕ ПО ПРОСЬБЕ ВЛАДЕЛЬЦА: «поставил сбор только за 8 часов —
проверь, всё ли корректно собрано». Собрано было некорректно, двумя разными
способами, и оба вскрылись только замером возраста записей в базе.

🔴 ПЕРВОЕ: НЕДЕЛЬНЫЕ ПУБЛИКАЦИИ УХОДИЛИ В ТЕЛО ВЫПУСКА.
В «Прочих событиях дня» при окне 8 часов стояли четыре публикации Минфина РФ
возрастом 147, 171, 195 и 315 часов (до 13 дней). Причина — не фильтр: фильтр
по окну в `HTMLConnector.fetch` был на месте и работал. Причина в том, что
`_minfin_ru` ставил КАЖДОЙ записи `datetime.now()` с комментарием «дату надёжно
не достать с листинга». Комментарий был неверен: дата лежит в разметке карточки
(`data-date="ДД.ММ.ГГ"`). Фильтр сравнивал `now >= now - 8ч` и пропускал всё.

🔴 ВТОРОЕ: БЛОК РЕГУЛЯТОРОВ ВРАЛ ПРО РОССТАТ.
Блок обещает «за сегодня или ближайший архив до 7 дн.» и печатал «Свежих
публикаций за 7 дней нет — мониторинг продолжается». Публикации были: недельный
ИПЦ от 12.08, оценка ВВП за II квартал. Коннектор их отдаёт — проверено
отдельным прогоном, 4 записи. Но архив блок берёт ИЗ БАЗЫ
(`recent_items_from_sources`), а в базу попадает только прошедшее окно сбора.
Росстат публикует раз в неделю и в восьмичасовое окно не попадает НИКОГДА.

Отсюда пара правок, которые держатся только вместе:
  * официальные источники опрашиваются минимум на неделю (`_OFFICIAL_FLOOR_H`),
    чтобы архив блока регуляторов был чем наполнить;
  * конвейер отсекает пул по окну пользователя ДО классификации, чтобы этот
    запас не уехал в Long list и «Прочие».
Одна без другой даёт либо пустой архив, либо ту самую протечку.

🔴 РЕВИЗИЯ 21.08.
  * `test_official_sources_are_polled_at_least_a_week` больше не читает
    `inspect.getsource(collect_all)` регуляркой. Грепалка охраняла НАПИСАНИЕ:
    поменять `s.type == "official"` на проверку по `s.id`, перепутать
    `max` с `min` или посчитать пол, но передать в `_fetch_one` окно
    пользователя — регулярка находит свои строки, тест зелёный, а Росстат
    снова не опрашивается. Теперь `collect_all` зовётся живьём с заглушкой
    вместо `_fetch_one`, и проверяется ОКНО, дошедшее до каждого источника.
  * Снят `test_the_two_halves_are_documented_as_a_pair`. Он искал слово
    «конвейер» в 800 знаках перед `_OFFICIAL_FLOOR_H`, то есть охранял
    КОММЕНТАРИЙ. Мутация «переписать комментарий, не тронув кода» роняла
    только его; мутация «снять пол окна» роняет тест выше и без него.

⚠️ ЧЕГО НЕ ЛОВИТ `test_the_pool_is_cut_back_to_the_user_window`. Он остался
текстовым: вторая половина связки живёт внутри `run_daily_news_pipeline`, а её
не запустить без заглушек на сбор, разметку, классификацию, LLM и базу — это
отдельный стенд, а не тест. Он читает ПОРЯДОК СТРОК в исходнике между метками
`Stage 2` и `Stage 3`, значит видит только написание: переименуй комментарий
`# === Stage 2` — покраснеет на здоровом коде; вынеси отсечение в хелпер,
вызванный после `annotate`, — останется зелёным при вернувшейся протечке.
"""
from __future__ import annotations

import asyncio
import inspect
from datetime import datetime, timedelta, timezone

import pytest

from finengine.skills.news import collect, config as _cfg, pipeline
from finengine.skills.news.connectors.html import _minfin_ru

_MSK = timezone(timedelta(hours=3))


def _card(day: str, ident: str, title: str) -> str:
    """Карточка в разметке Минфина — форма снята с живой страницы 18.08."""
    return (
        f'<div class="news_card_min"> '
        f'<a href="/ru/press-center/?id_4={ident}-slug" class="img_container"></a> '
        f'<div class="news_info"> <span class="news_date t_mn2" '
        f'data-date="{day}">{day}</span> </div> '
        f'<a href="/ru/press-center/?id_4={ident}-slug" class="news_title t_mb2">'
        f'{title}</a> </div>'
    )


def test_minfin_reads_the_date_that_is_on_the_page():
    """Отрицательный контроль: вернул `datetime.now()` всем записям — краснеет.

    Именно эта строка пускала в выпуск публикации 13-дневной давности.
    """
    today = datetime.now(_MSK)
    old = today - timedelta(days=6)
    body = (_card(today.strftime("%d.%m.%y"), "40535", "Свежая новость министерства")
            + _card(old.strftime("%d.%m.%y"), "40530", "Публикация шестидневной давности"))

    got = _minfin_ru(body, "https://minfin.gov.ru/ru/press-center/")

    assert len(got) == 2, "разбор карточек сломан"
    ages = {t: (datetime.now(timezone.utc) - d).total_seconds() / 3600
            for t, _, d in got if d}
    assert len(ages) == 2, "у записи нет даты — фильтр по окну снова ослепнет"
    assert ages["Свежая новость министерства"] < 1
    assert 130 < ages["Публикация шестидневной давности"] < 170, (
        "шестидневная публикация выдаётся за свежую")


def test_todays_item_stays_fresh_despite_day_granularity():
    """На листинге есть дата, но нет ЧАСА. Если сегодняшнюю новость датировать
    полуночью, она при окне 8 часов вылетит из собственного дня — поэтому
    сегодняшняя отдаётся как `now`. Граница проверяется явно."""
    today = datetime.now(_MSK).strftime("%d.%m.%y")
    got = _minfin_ru(_card(today, "1", "Сегодняшняя новость министерства"), "")

    age_h = (datetime.now(timezone.utc) - got[0][2]).total_seconds() / 3600
    assert age_h < 0.1, "сегодняшняя новость датирована прошлым — вылетит из окна"


def _source(sid: str, stype: str) -> _cfg.SourceConfig:
    return _cfg.SourceConfig(
        id=sid, display_name=sid, type=stype, region="RU",
        connector="rss", url=f"https://{sid}/feed", weight=0.5,
    )


@pytest.fixture()
def windows_seen(monkeypatch):
    """Окно, с которым сбор реально пошёл в каждый источник."""
    seen: dict[str, int] = {}

    async def _fake_fetch_one(source, since_hours, errors=None):
        seen[source.id] = since_hours
        return []

    cfg = _cfg.NewsConfig(
        sources=[_source("rosstat", "official"), _source("rbc", "tier1")],
        entities=[],
    )
    monkeypatch.setattr(collect._cfg, "load", lambda: cfg)
    monkeypatch.setattr(collect, "_fetch_one", _fake_fetch_one)
    monkeypatch.setattr(collect._db, "upsert_source", lambda **kw: None)
    return seen


def test_official_sources_are_polled_at_least_a_week(windows_seen):
    """Отрицательный контроль: убрал пол — краснеет.

    Без него блок регуляторов обещает архив за 7 дней, а показать может только
    то, что прошло окно пользователя. Проверяется ОКНО, дошедшее до источника,
    а не написание условия в исходнике.
    """
    asyncio.run(collect.collect_all(since_hours=8))

    assert windows_seen["rosstat"] == 168, (
        "официальный источник опрошен окном пользователя, а не полом в неделю "
        f"— блок регуляторов снова обещает архив, которого нет: {windows_seen}")
    assert windows_seen["rbc"] == 8, (
        "пол применён ко ВСЕМ источникам — окно пользователя перестало "
        f"работать: {windows_seen}")


def test_a_window_wider_than_the_floor_is_not_narrowed(windows_seen):
    """Пол — именно пол, а не подмена: окно шире недели остаётся своим."""
    asyncio.run(collect.collect_all(since_hours=336))

    assert windows_seen["rosstat"] == 336, (
        f"пол урезал широкое окно вместо того, чтобы поднять узкое: {windows_seen}")


def test_the_pool_is_cut_back_to_the_user_window():
    """Вторая половина связки. Запас, собранный ради архива регуляторов, не
    имеет права уехать в Long list и «Прочие» — ровно это и наблюдалось."""
    src = inspect.getsource(pipeline.run_daily_news_pipeline)
    i = src.index("Stage 2")
    j = src.index("Stage 3")
    stage2 = src[i:j]
    assert "since_hours" in stage2 and "published_at" in stage2, (
        "конвейер не отсекает пул по окну — недельный запас уедет в тело выпуска")
    assert stage2.index("published_at") < stage2.index("_filter.annotate"), (
        "отбор по дате идёт ПОСЛЕ разметки — лишнее успеет попасть в кандидаты")
