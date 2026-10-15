"""Дайджест не врёт о себе самом (18.08.2026).

РАЗБОР ЖИВОГО ВЫПУСКА ПРОДА ОТ 17.08 23:01 MSK. Владелец попросил проверить,
«не врёт ли дайджест», — врал в трёх местах сразу:

  * «Свежих публикаций за 7 дней нет — мониторинг продолжается» стояло под НБК
    и Минфином США. У обоих источники в `sources.yaml` выключены
    (`pboc`, `ustreasury_blog` — `active: false`), сбор их не опрашивает вовсе.
    Читатель понимал «следим, но тихо», правда была «не следим».
  * Шапка заявляла «20 компаний (из 100 в покрытии) · 9 макропоказателей ·
    1 публикаций регуляторов». В теле стояли 11 компаний, 19 макропоказателей
    и 7 публикаций: счётчики считали по классифицированному пулу, а между
    пулом и телом лежат `select_long_list`, межпрогонный дедуп и режим
    макро-блока (при `both` показатель недели печатается дважды). Плюс
    «1 публикаций» — согласования не было вовсе.
  * Предварительная оценка Минфина РФ (`id_4=40530-...`) стояла в выпуске
    ДВАЖДЫ: архивным пунктом (🗄️) у регулятора и отдельным «прочим событием».

Сторож смотрит на СОБРАННЫЙ MARKDOWN и на конфиг источников, а не на текст
комментариев в коде: числа шапки сверяются с тем, что напечатано в теле.

Отрицательный контроль выполнен на всех трёх пунктах — каждая правка временно
откатывалась, соответствующий тест краснел:
  * вернул безусловное «мониторинг продолжается» →
    test_disabled_regulator_is_not_called_monitored;
  * вернул счёт по классифицированному пулу (`_count_coverage`) →
    test_header_counts_are_the_facts_of_the_body, test_macro_count_is_not_doubled_by_mode,
    test_archive_publications_are_counted_too;
  * вернул хардкод склонений («N компаний», «N публикаций») → те же три теста;
  * впустил в счёт компаний категорию «unknown», которую сетка не печатает →
    test_unclassified_item_is_counted_by_nobody;
  * убрал фильтр `pool` по ссылкам регуляторов →
    test_regulator_publication_is_not_repeated_in_the_body.

ВТОРОЙ РАЗБОР (18.08.2026): состязательные линзы прочитали правки выше и нашли,
что часть из них внесла свои дефекты. Починено здесь же, отрицательный контроль
выполнен на каждом:
  * фильтр `pool` выбрасывал публикации регуляторов не только из сетки, но и из
    ПЕРСИСТА: fe_news_lookup (`db.search_digest_items`) переставал их находить,
    а повторная отдача прогона печатала блок регуляторов иначе первичного
    выпуска. Вернул старую запись `long_list + others` →
    test_regulator_item_stays_findable_after_the_run,
    test_regulator_publication_is_not_repeated_in_the_body;
  * счёт компаний брал ВСЕ `matched_entities` пункта, а тело печатает одну
    компанию на пункт (на прогоне aaa9b9a3: 33 против 19). Вернул счёт по
    совпадениям → test_company_count_is_what_is_printed_not_what_was_matched;
  * ⭐ ставилась по поисковому совпадению, а не по категории от модели (на
    прогоне 17.08 — семь macro-пунктов со звездой). Вернул безусловную звезду →
    test_star_follows_the_model_not_the_search_match и тот же счёт компаний;
  * секция «Обзор ЦБ РФ» читала НЕотфильтрованный `classified` и печатала URL
    второй раз. Вернул `classified` →
    test_cbr_review_does_not_reprint_the_regulator_item.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from finengine import config as _config
from finengine.skills.news import db as _db
from finengine.skills.news import render
from finengine.skills.news.config import EntityConfig, NewsConfig, SourceConfig
from finengine.skills.news.models import ClassifiedItem, NewsItem

_NOW = datetime(2026, 8, 18, 9, 0, tzinfo=timezone.utc)


def _cfg(*, pboc_active: bool = False) -> NewsConfig:
    """Конфиг с теми же ролями источников, что и в бою: ЦБ РФ включён,
    НБК — по параметру (в `sources.yaml` он выключен)."""
    return NewsConfig(
        sources=[
            SourceConfig("cbr_press", "ЦБ РФ", "official", "RU", "rss",
                         "https://cbr.ru/rss", 1.0, True),
            SourceConfig("minfin_ru", "Минфин РФ", "official", "RU", "rss",
                         "https://minfin.gov.ru/rss", 0.95, True),
            SourceConfig("pboc", "НБК", "official", "CN", "html",
                         "http://www.pbc.gov.cn/", 0.9, pboc_active),
        ],
        entities=[
            EntityConfig("Сбербанк", ["Сбер"], "watchlist_ru"),
            EntityConfig("Газпром", [], "watchlist_ru"),
            EntityConfig("ВТБ", [], "watchlist_ru"),
        ],
    )


def _item(*, url: str, source_id: str = "rbc", source: str = "РБК",
          entities: list[str] | None = None) -> NewsItem:
    return NewsItem(
        source=source, source_id=source_id, source_type="wire", region="RU",
        source_weight=0.8, title="заголовок", snippet="", url=url,
        published_at=_NOW, raw_hash=url[-16:],
        matched_entities=list(entities or []),
    )


def _classified(item: NewsItem, *, category: str = "corporate",
                relevant: bool = True, sentence: str = "что-то произошло",
                importance: float = 0.8) -> ClassifiedItem:
    return ClassifiedItem(
        item=item, is_relevant=relevant, category=category, region="RU",
        importance=importance, one_sentence_ru=sentence,
    )


def _section(md: str, regulator: str) -> str:
    """Кусок markdown, относящийся к одному регулятору."""
    for chunk in md.split("\n### "):
        if chunk.startswith(("🇷🇺", "🇺🇸", "🇪🇺", "🇨🇳")) and regulator in chunk.splitlines()[0]:
            return chunk
    raise AssertionError(f"раздел регулятора «{regulator}» не найден в выпуске")


@pytest.fixture(autouse=True)
def _isolated(monkeypatch, tmp_path):
    """Ни архива, ни модели, ни боевого data_dir: тест меряет рендер."""
    monkeypatch.setattr(_db, "recent_items_from_sources",
                        lambda sids, **kw: [])
    monkeypatch.setattr(render, "_translate_titles_to_ru",
                        lambda titles: {t: t for t in titles})
    monkeypatch.setattr(_config.settings, "data_dir", tmp_path)


# ─── (а) отключённый источник не называется мониторингом ────────────────


def test_disabled_regulator_is_not_called_monitored():
    """НБК: все источники `active: false` → «мониторинг продолжается» — ложь."""
    md, _ = render._render_regulators_box([], cfg=_cfg(pboc_active=False))

    pboc = _section(md, "НБК")
    assert "не отслеживаются" in pboc, (
        "у регулятора с выключенными источниками нет честной пометки")
    assert "мониторинг продолжается" not in pboc, (
        "выпуск снова обещает мониторинг источника, который не опрашивается")

    cbr = _section(md, "ЦБ РФ")
    assert "мониторинг продолжается" in cbr, (
        "у ВКЛЮЧЁННОГО регулятора без новостей пометка о тишине пропала")


def test_the_notice_follows_the_config_not_a_hardcoded_list():
    """Другой агент может включить `pboc` обратно тем же днём. Пометка обязана
    сняться сама — иначе врать начнёт она."""
    md, _ = render._render_regulators_box([], cfg=_cfg(pboc_active=True))

    pboc = _section(md, "НБК")
    assert "мониторинг продолжается" in pboc, (
        "источник включили в конфиге, а выпуск всё ещё зовёт его отключённым")
    assert "не отслеживаются" not in pboc


# ─── (б) шапка сходится с телом ─────────────────────────────────────────


def _macro_payload() -> dict:
    return {
        "ru_keyrate": {
            "id": "ru_keyrate", "ru_name": "Ключевая ставка ЦБ РФ", "region": "RU",
            "unit": "%", "latest_value": 16.0, "latest_date": "2026-08-01",
            "published": "2026-08-14", "source_url": "https://cbr.ru/keyrate",
            "compares": [{"label": "м/м", "pct": -1.5, "abs": -0.25}],
        },
        "us_cpi": {
            "id": "us_cpi", "ru_name": "Инфляция США (CPI)", "region": "US",
            "unit": "индекс 1982-84=100", "latest_value": 320.1,
            "latest_date": "2026-07-01", "published": "2026-08-13",
            "source_url": "https://fred.stlouisfed.org/series/CPIAUCSL",
            "compares": [{"label": "м/м", "pct": 0.2}],
        },
    }


def test_header_counts_are_the_facts_of_the_body():
    """Три числа шапки против трёх фактов тела.

    Пул нарочно богаче тела: у «Газпрома» новость нерелевантна и не печатается,
    у «ВТБ» — регуляторная (её место в блоке регуляторов, а не в сетке).
    Именно на этой разнице шапка и разошлась с телом 17.08.
    """
    classified = [
        _classified(_item(url="https://rbc.ru/sber", entities=["Сбербанк"])),
        _classified(_item(url="https://rbc.ru/gazprom", entities=["Газпром"]),
                    relevant=False),
        _classified(_item(url="https://cbr.ru/press/1", source_id="cbr_press",
                          source="ЦБ РФ", entities=["ВТБ"]),
                    category="regulator", sentence="ЦБ сохранил ставку"),
    ]

    md, _ = render.render_digest(
        classified, cfg=_cfg(), now=_NOW,
        macro_payload=_macro_payload(), macro_mode="full",
    )
    head = md.split("\n---\n")[0]
    body = md.split("## 🔥 Long list")[1]

    assert "1 компания (из 3 в покрытии)" in head, (
        f"счёт компаний разошёлся с телом или не склоняется: {head!r}")
    assert "Сбербанк" in body and "Газпром" not in body, (
        "в теле не та компания, что посчитана в шапке")

    assert "2 макропоказателя" in head, f"счёт макро разошёлся с телом: {head!r}"
    assert "Ключевая ставка ЦБ РФ" in md and "Инфляция США (CPI)" in md

    assert "1 публикация регуляторов" in head, (
        f"счёт публикаций разошёлся с телом: {head!r}")
    assert md.count("- 🏦 ") + md.count("- 🗄️ ") == 1, (
        "в блоке регуляторов напечатано не то число пунктов, что в шапке")


def test_macro_count_is_not_doubled_by_mode():
    """Режим `both` печатает свежий показатель ДВАЖДЫ — в блоке недели и в
    полной таблице. 17.08 это и дало «9 макропоказателей» при 19 разных."""
    md, _ = render.render_digest(
        [], cfg=_cfg(), now=_NOW,
        macro_payload=_macro_payload(), macro_mode="both",
    )
    head = md.split("\n---\n")[0]

    assert md.count("**Ключевая ставка ЦБ РФ**") == 2, (
        "фикстура не воспроизводит боевой режим: показатель должен быть в двух "
        "таблицах")
    assert "2 макропоказателя" in head, (
        f"шапка посчитала строки таблиц, а не разные показатели: {head!r}")


def test_unclassified_item_is_counted_by_nobody():
    """Сетка печатает две категории — «macro» и «corporate» (`_render_section`).
    Новость с категорией «unknown» попадает в long list, но на экран не
    выходит: считать её компанию в шапке — тот же разрыв шапки и тела."""
    classified = [
        _classified(_item(url="https://rbc.ru/vtb", entities=["ВТБ"]),
                    category="unknown", sentence="что-то неразобранное"),
    ]

    md, _ = render.render_digest(classified, cfg=_cfg(), now=_NOW)
    head = md.split("\n---\n")[0]

    assert "что-то неразобранное" not in md, (
        "фикстура устарела: сетка научилась печатать категорию «unknown»")
    assert "0 компаний" in head, (
        f"в шапке компания, которой нет в теле: {head!r}")


def test_archive_publications_are_counted_too():
    """Семь публикаций тела против «1» в шапке 17.08 — это в основном архивные
    пункты (🗄️): классифицированный пул о них не знает, они приходят из БД уже
    внутри блока регуляторов. Считать надо напечатанное."""
    rows = [
        {"title": "Предварительная оценка исполнения бюджета",
         "url": "https://minfin.gov.ru/a", "published_at": _NOW.timestamp(),
         "source_name": "Минфин РФ"},
        {"title": "Итоги размещения ОФЗ", "url": "https://minfin.gov.ru/b",
         "published_at": _NOW.timestamp(), "source_name": "Минфин РФ"},
    ]
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(_db, "recent_items_from_sources",
                   lambda sids, **kw: rows if "minfin_ru" in sids else [])
        md, _ = render.render_digest([], cfg=_cfg(), now=_NOW)

    head = md.split("\n---\n")[0]
    assert md.count("- 🗄️ ") == 2, "фикстура не напечатала архивные пункты"
    assert "2 публикации регуляторов" in head, (
        f"архивные пункты тела не попали в счёт шапки: {head!r}")


def test_empty_digest_counts_zero_without_lying():
    """Пустой прогон — «0 компаний · 0 макропоказателей · 0 публикаций»."""
    md, _ = render.render_digest([], cfg=_cfg(), now=_NOW)
    head = md.split("\n---\n")[0]
    assert "0 компаний" in head and "0 макропоказателей" in head
    assert "0 публикаций регуляторов" in head


@pytest.mark.parametrize("n,expected", [
    (0, "публикаций"), (1, "публикация"), (2, "публикации"), (4, "публикации"),
    (5, "публикаций"), (11, "публикаций"), (14, "публикаций"),
    (21, "публикация"), (22, "публикации"), (25, "публикаций"),
    (111, "публикаций"),
])
def test_plural_agreement(n, expected):
    """В выпуске стояло «1 публикаций регуляторов»."""
    assert _plural(n) == expected


def _plural(n: int) -> str:
    return render._plural_ru(n, "публикация", "публикации", "публикаций")


# ─── (в) одна ссылка — одна секция ──────────────────────────────────────


def test_regulator_publication_is_not_repeated_in_the_body(monkeypatch):
    """URL Минфина РФ, показанный архивным пунктом у регулятора, не имеет права
    выйти вторым событием в «Прочих»."""
    url = "https://minfin.gov.ru/ru/press-center/?id_4=40530-predvaritelnaya_otsenka"
    monkeypatch.setattr(
        _db, "recent_items_from_sources",
        lambda sids, **kw: (
            [{"title": "Предварительная оценка исполнения бюджета",
              "url": url, "published_at": _NOW.timestamp(),
              "source_name": "Минфин РФ"}]
            if "minfin_ru" in sids else []
        ),
    )
    # Тот же URL приезжает в пул классифицированным как макро — ровно так он и
    # попал 17.08 в «Прочие события дня» рядом со своей же строкой у регулятора.
    classified = [
        _classified(_item(url=url, source_id="minfin_ru", source="Минфин РФ"),
                    category="macro", sentence="Минфин оценил исполнение бюджета"),
        _classified(_item(url="https://rbc.ru/sber", entities=["Сбербанк"])),
    ]

    md, digest_items = render.render_digest(classified, cfg=_cfg(), now=_NOW)

    assert md.count(url) == 1, (
        "одна публикация напечатана дважды — у регулятора и в сетке событий")
    assert "🗄️" in md.split("## 🔥 Long list")[0], (
        "публикация исчезла и из блока регуляторов — дедуп срезал не ту копию")
    assert url not in md.split("## 🔥 Long list")[1]
    # В markdown ссылка одна, а В ПЕРСИСТЕ запись есть — и не в long list.
    # Первая редакция этого сторожа требовала ОТСУТСТВИЯ записи, и код под неё
    # выбрасывал напечатанные публикации регуляторов из digest_items целиком:
    # `db.search_digest_items` (инструмент fe_news_lookup) переставал их
    # находить, а повторная отдача прогона из БД печатала блок регуляторов
    # иначе первичного выпуска.
    saved = [d for d in digest_items if d.classified.item.url == url]
    assert len(saved) == 1, (
        "напечатанная публикация регулятора потеряна для персиста — "
        "fe_news_lookup ответит «такой новости в выпуске не было»")
    assert saved[0].in_long_list is False, (
        "ненапечатанный в сетке пункт записан как показанный в long list — "
        "межпрогонный дедуп (db.recent_long_list_hashes) погасит его завтра")


def test_regulator_item_stays_findable_after_the_run():
    """Решение по ставке напечатано в блоке регуляторов свежим пунктом (🏦) —
    значит, оно обязано лежать в digest_items: по ним ищет fe_news_lookup и по
    ним же собирается повторная отдача прогона (`_render_from_db_rows`)."""
    rate = _classified(
        _item(url="https://cbr.ru/press/keyrate", source_id="cbr_press",
              source="ЦБ РФ"),
        category="regulator", sentence="ЦБ сохранил ключевую ставку 16%",
    )

    md, digest_items = render.render_digest([rate], cfg=_cfg(), now=_NOW)

    assert "- 🏦 ЦБ сохранил ключевую ставку 16%" in md, (
        "фикстура не напечатала свежий пункт регулятора")
    hashes = {d.classified.item.raw_hash for d in digest_items}
    assert rate.item.raw_hash in hashes, (
        "напечатанное решение по ставке не попало в персист")


# ─── (г) звезда и счёт компаний = решение модели, не совпадение поиска ──


def test_star_follows_the_model_not_the_search_match():
    """`matched_entities` заполняет СБОР; classify их только читает (записи в
    classify.py нет). Значит рыночная сводка, которую модель назвала macro,
    не имеет права нести ⭐ «компания из watchlist» — это утверждение о
    привязке, которой модель не делала. На прогоне 17.08 таких пунктов семь."""
    classified = [
        _classified(_item(url="https://rbc.ru/market", entities=["Сбербанк"]),
                    category="macro", sentence="индекс Мосбиржи вырос на 1%"),
        _classified(_item(url="https://rbc.ru/sber-buyback", entities=["Сбербанк"]),
                    category="corporate", sentence="Сбербанк объявил выкуп"),
    ]

    md, _ = render.render_digest(classified, cfg=_cfg(), now=_NOW)
    body = md.split("## 🔥 Long list")[1]

    macro_line = [ln for ln in body.splitlines() if "индекс Мосбиржи" in ln]
    corp_line = [ln for ln in body.splitlines() if "выкуп" in ln]
    assert macro_line and corp_line, f"фикстура не напечатала оба пункта: {body!r}"
    assert "⭐" not in macro_line[0], (
        f"звезда на рыночной сводке — привязка, которой модель не делала: "
        f"{macro_line[0]!r}")
    assert "⭐" in corp_line[0], (
        f"у корпоративной новости звезда пропала: {corp_line[0]!r}")


def test_company_count_is_what_is_printed_not_what_was_matched():
    """Тело печатает ОДНУ компанию на пункт, а `matched_entities` их несёт
    сколько нашёл поиск. Счёт по совпадениям давал на прогоне aaa9b9a3
    33 компании при 19 напечатанных."""
    classified = [
        # Поиск нашёл в одной новости три компании покрытия — напечатана будет
        # первая по порядку watchlist.
        _classified(_item(url="https://rbc.ru/three",
                          entities=["Сбербанк", "Газпром", "ВТБ"]),
                    sentence="банки обсудили сделку"),
        # А эта — macro: компания в ней от поиска, не от модели.
        _classified(_item(url="https://rbc.ru/macro", entities=["Газпром"]),
                    category="macro", sentence="нефть подорожала"),
    ]

    md, _ = render.render_digest(classified, cfg=_cfg(), now=_NOW)
    head = md.split("\n---\n")[0]
    body = md.split("## 🔥 Long list")[1]

    # Считаем звёзды ПУНКТОВ, а не всего хвоста: в конце выпуска стоит
    # легенда «⭐ — компания из watchlist».
    starred = [ln for ln in body.splitlines()
               if ln.startswith("- ") and "⭐" in ln]
    assert len(starred) == 1, (
        f"в теле не одна помеченная компания: {starred!r}")
    assert "1 компания (из 3 в покрытии)" in head, (
        f"шапка посчитала совпадения поиска, а не напечатанные компании: "
        f"{head!r}")


# ─── (д) обзор ЦБ не дублирует блок регуляторов ─────────────────────────


def test_cbr_review_does_not_reprint_the_regulator_item():
    """Ключи отбора обзора («банковск», «обзор») у ЦБ рядовые: пункт, уже
    напечатанный в «🏛️ Регуляторы», выходил тем же URL второй раз в
    «🏦 Обзор ЦБ РФ по банковскому сектору»."""
    url = "https://cbr.ru/press/review-bank-sector"
    classified = [
        _classified(
            _item(url=url, source_id="cbr_press", source="ЦБ РФ"),
            category="regulator",
            sentence="ЦБ опубликовал обзор банковского сектора за июль",
        ),
    ]

    md, _ = render.render_digest(classified, cfg=_cfg(), now=_NOW)

    assert "- 🏦 ЦБ опубликовал обзор" in md, (
        "фикстура не напечатала пункт в блоке регуляторов")
    assert md.count(url) == 1, (
        "один URL напечатан и у регулятора, и в обзоре банковского сектора")
