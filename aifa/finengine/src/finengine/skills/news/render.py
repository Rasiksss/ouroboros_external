"""Step 5: render classified+ranked items as the IR Daily Digest markdown.

Structure (per ТЗ заказчика):
    # 📈 IR Daily Digest — DD месяца YYYY, HH:MM MSK

    ## 🔥 Long list
    ### 🇷🇺 Россия
        Макроэкономика / Регуляторы / Корпоративные  ← 2×3 grid
    ### 🌎 Мировые рынки
        Макроэкономика / Регуляторы / Корпоративные

    ## 📰 Прочие события дня
        same 2×3, items that didn't make long list

    ## 🏦 Обзор ЦБ РФ по банковскому сектору
        only if a CBR review item is present today

The render is **pure code**, no LLM. It receives `DigestItem`s already
classified by classify_batch and ranked by code.
"""
from __future__ import annotations

import asyncio
import logging
import re
from collections import defaultdict
from datetime import datetime, timezone

from finengine.skills.news.config import EntityConfig, NewsConfig
from finengine.skills.news.models import (
    ClassifiedItem,
    DigestItem,
    NewsItem,
)

log = logging.getLogger(__name__)


_MONTH_RU = {
    1: "января", 2: "февраля", 3: "марта", 4: "апреля",
    5: "мая", 6: "июня", 7: "июля", 8: "августа",
    9: "сентября", 10: "октября", 11: "ноября", 12: "декабря",
}


_REGION_FLAGS = {
    "RU": "🇷🇺 Россия",
    "US": "🇺🇸 США",
    "EU": "🇪🇺 Европа",
    "CN": "🇨🇳 Китай",
    "global": "🌎 Мировые рынки",
}

_CATEGORY_HEADERS = {
    "macro": "**Макроэкономика**",
    "regulator": "**Регуляторы**",
    "corporate": "**Корпоративные**",
}


def _detect_watchlist(item: NewsItem, watchlist: list[EntityConfig]) -> str | None:
    """Return canonical company name if item matched any watchlist entity."""
    for ent in watchlist:
        if ent.canonical in item.matched_entities:
            return ent.canonical
    return None


def select_long_list(
    classified: list[ClassifiedItem],
    *,
    target_size: int = 15,
    seen_hashes: set[str] | None = None,
) -> tuple[list[ClassifiedItem], list[ClassifiedItem]]:
    """Pick top-N by importance for long list; the rest goes to 'Прочие'.
    Constraint: ensure ≥1 item per cell of the 2×3 grid where data exists.

    Returns (long_list, others). Both keep classify order (importance desc).
    """
    relevant = [c for c in classified if c.is_relevant and c.one_sentence_ru]
    # #94: межпрогонный дедуп — уже показанное в long list прошлых прогонов не
    # возвращается в топ (уходит в «Прочие»), место занимают свежие истории.
    if seen_hashes:
        repeats = [c for c in relevant if c.item.raw_hash in seen_hashes]
        relevant = [c for c in relevant if c.item.raw_hash not in seen_hashes]
    else:
        repeats = []
    relevant.sort(key=lambda c: c.importance, reverse=True)

    long_list: list[ClassifiedItem] = []
    seen_ids: set[int] = set()

    # 1) Top-K by importance get in unconditionally.
    for c in relevant[:target_size]:
        long_list.append(c)
        seen_ids.add(id(c))

    # 2) Coverage rule: if some 2×3 cell has none in long_list but does
    #    have a candidate in `relevant`, add the best one (cap +6 above
    #    target_size — never explode the list).
    # Добор пустых клеток сетки: 20 сверх пятнадцати (было 10) — решение
    # владельца 18.08.2026. Сетка — 5 регионов (RU/US/EU/CN/global) × 3
    # категории (macro/regulator/corporate), 15 клеток; при потолке 10 часть
    # из них оставалась пустой, даже когда кандидаты были.
    coverage_target = 20
    cells_in_long = {(c.region, c.category) for c in long_list}
    for cell in [(r, cat) for r in ("RU", "US", "EU", "CN", "global")
                 for cat in ("macro", "regulator", "corporate")]:
        if cell in cells_in_long:
            continue
        for c in relevant:
            if id(c) in seen_ids:
                continue
            if (c.region, c.category) == cell:
                long_list.append(c)
                seen_ids.add(id(c))
                cells_in_long.add(cell)
                break
        if len(long_list) >= target_size + coverage_target:
            break

    others = [c for c in relevant if id(c) not in seen_ids]
    return long_list, others + repeats


def _format_item(c: ClassifiedItem, *, watchlist_mark: str | None) -> str:
    """One line item with markdown link inline. If watchlist match —
    star prefix; if corporate, bold the company name."""
    star = " ⭐" if watchlist_mark else ""
    text = c.one_sentence_ru.strip().rstrip(".")
    src_label = c.item.source
    if c.category == "corporate" and watchlist_mark:
        return f"- **{watchlist_mark}**{star}: {text} — [{src_label}]({c.item.url})"
    return f"- {text}{star} — [{src_label}]({c.item.url})"


_EMPTY_PLACEHOLDER = "— Сегодня публикаций по этой теме не обнаружено."


def _render_section(
    title_block: str,
    classified: list[ClassifiedItem],
    *,
    watchlist: list[EntityConfig],
    show_empty: bool = True,
) -> str:
    """Render the Russia/World × Macro/Corporate grid.

    Regulators get their own dedicated block at the top of the digest
    (`_render_regulators_box`); rendering them here too would double-show
    every CBR/Fed/ECB entry. Macro stays in both top dashboard and Long
    list because the LLM team scans the grid as the canonical layout.

    When `show_empty=True` (default), missing cells are printed with a
    placeholder so the IR team sees coverage gaps explicitly instead of
    silently-omitted sections.
    """
    out: list[str] = [title_block]

    grouped: dict[str, dict[str, list[ClassifiedItem]]] = defaultdict(
        lambda: defaultdict(list)
    )
    for c in classified:
        if c.category == "regulator":
            continue  # rendered separately in the regulator block
        bucket_region = "RU" if c.region == "RU" else "world"
        grouped[bucket_region][c.category].append(c)

    for region_key, region_label in (("RU", "### 🇷🇺 Россия"),
                                     ("world", "### 🌎 Мировые рынки")):
        region_items = grouped.get(region_key, {})
        if not region_items and not show_empty:
            continue
        out.append("")
        out.append(region_label)
        for cat_key in ("macro", "corporate"):
            items = region_items.get(cat_key) or []
            if not items and not show_empty:
                continue
            out.append("")
            out.append(_CATEGORY_HEADERS[cat_key])
            if not items:
                out.append(_EMPTY_PLACEHOLDER)
                continue
            for c in items:
                # ⭐ ставится по решению МОДЕЛИ (категория), а не по поисковому
                # совпадению (18.08.2026). `matched_entities` заполняет сбор,
                # classify их только читает в промпт и НИКОГДА не пишет — то
                # есть правило промпта «рыночная сводка идёт без компании,
                # category='macro'» до экрана не доезжало: модель слушалась, а
                # звезда всё равно стояла. На живом прогоне 17.08 таких
                # macro-пунктов со звездой было семь, а легенда выпуска гласит
                # «⭐ — компания из watchlist», то есть утверждает привязку,
                # которой модель не делала.
                mark = (_detect_watchlist(c.item, watchlist)
                        if c.category == "corporate" else None)
                out.append(_format_item(c, watchlist_mark=mark))
    return "\n".join(out)


def _url_key(url: str) -> str:
    """Ключ сравнения ссылок. Хвостовой слэш — не другая публикация."""
    return (url or "").strip().rstrip("/")


# Canonical regulators per ТЗ заказчика (agent Daily.xlsx). The render
# guarantees a section for EACH of these — if today's classified pool
# has no fresh item from the regulator, we fall back to its latest item
# within the last 7 days. Empty after that → explicit "тишина 7 дн.".
_REGULATOR_SPEC: list[tuple[str, str, list[str], str]] = [
    # (display_name, region_bucket, source_ids, focus blurb)
    ("ЦБ РФ", "RU", ["cbr_press", "cbr_news", "cbr_events"],
     "ставка, макропрогноз, банковский сектор"),
    ("Минфин РФ", "RU", ["minfin_ru"], "бюджет, налоги, размещения"),
    ("Росстат", "RU", ["rosstat_gov"], "статистика"),
    ("ФРС", "US", ["fed"], "ставка, макропрогноз"),
    ("Минфин США", "US", ["ustreasury_blog"], "санкции, бюджет"),
    ("ЕЦБ", "EU", ["ecb"], "ставка, макропрогноз"),
    ("НБК", "CN", ["pboc"], "ставка, юань"),
]


def _render_regulators_box(
    classified: list[ClassifiedItem], *, cfg: NewsConfig,
) -> tuple[str, set[str]]:
    """Always-on regulator section. Per ТЗ заказчика — each tracked
    regulator must appear every day, falling back to last-7d publications
    when nothing fresh hit the classified pool.

    Avoids DB import at module load (circular w/ db.connect→models).

    Возвращает пару (markdown, ссылки напечатанных пунктов). Ссылки нужны
    вызывающему: 17.08 публикация Минфина РФ встала И сюда (🗄️ из архива), И в
    «Прочие события дня» — читатель видел один URL двумя разными событиями.
    """
    from finengine.skills.news import db as _db  # noqa: PLC0415

    # Bucket today's classified regulator items by source_id for fast lookup.
    today_by_sid: dict[str, list[ClassifiedItem]] = defaultdict(list)
    for c in classified:
        if c.is_relevant and c.category == "regulator":
            today_by_sid[c.item.source_id].append(c)

    # Флаг active читается из КОНФИГА, а не сверяется со списком id в коде:
    # источник могут включить обратно тем же днём, и захардкоженный список
    # начал бы врать в другую сторону.
    active_sids = {s.id for s in cfg.sources if s.active}
    shown_urls: set[str] = set()

    out = ["## 🏛️ Регуляторы"]
    out.append("")
    out.append(
        "*Информационное поле вокруг ключевых регуляторов — за сегодня "
        "или ближайший архив до 7 дн.*"
    )

    region_labels = {"RU": "🇷🇺", "US": "🇺🇸", "EU": "🇪🇺", "CN": "🇨🇳"}

    for name, region, sids, focus in _REGULATOR_SPEC:
        # 1) anything classified today from this regulator's sources
        fresh: list[ClassifiedItem] = []
        for sid in sids:
            fresh.extend(today_by_sid.get(sid, []))
        # Importance desc, cap at 3 per regulator so a single one doesn't
        # dominate (ЦБ РФ tends to publish 6+ rate-table updates a day).
        fresh.sort(key=lambda c: -c.importance)
        fresh = fresh[:3]

        out.append("")
        out.append(f"### {region_labels.get(region, '')} {name}")
        out.append(f"*{focus}*")
        if fresh:
            for c in fresh:
                text = c.one_sentence_ru.strip().rstrip(".")
                src = c.item.source
                out.append(f"- 🏦 {text} — [{src}]({c.item.url})")
                shown_urls.add(_url_key(c.item.url))
            continue

        # 2) fall back to last 7d for this regulator
        backlog = _db.recent_items_from_sources(
            sids, max_age_days=7.0, limit=2,
        )
        if not backlog:
            # «Мониторинг продолжается» — ложь, когда ВСЕ источники регулятора
            # выключены (18.08.2026: у НБК `pboc` и Минфина США
            # `ustreasury_blog` стоит active:false, сбор их не опрашивает
            # вовсе). Читатель понимал «следим, но тихо», а правда была
            # «не следим».
            if any(sid in active_sids for sid in sids):
                out.append(
                    "- _Свежих публикаций за 7 дней нет — мониторинг продолжается._"
                )
            else:
                out.append(
                    "- _Источник временно отключён — публикации не отслеживаются._"
                )
            continue
        # English titles from ФРС / Минфин США / ЕЦБ / НБК get translated
        # to Russian via a small LLM batch (cached on disk so each title
        # is paid for only once). RU regulator items stay as-is.
        raw_titles = [(row.get("title") or "").strip().rstrip(".")[:200]
                      for row in backlog]
        translations = _translate_titles_to_ru(raw_titles)
        for row, raw_title in zip(backlog, raw_titles):
            age_days = int(
                (datetime.now(timezone.utc).timestamp()
                 - (row.get("published_at") or 0))
                / 86400
            )
            age_label = "сегодня" if age_days == 0 else f"{age_days} дн. назад"
            shown_title = translations.get(raw_title, raw_title)
            src = row.get("source_name") or name
            url = row.get("url") or "#"
            out.append(
                f"- 🗄️ ({age_label}) {shown_title} — [{src}]({url})"
            )
            if url != "#":
                shown_urls.add(_url_key(url))

    return "\n".join(out), shown_urls


# ─── Title translation helper ───────────────────────────────────────────


# Расход на перевод заголовков за прогон. Модульный счётчик, а не поле:
# `render_digest` синхронная и вызывается из конвейера без общего состояния,
# протаскивать usage через шесть сигнатур ради одного числа — хуже.
_TRANSLATION_USAGE: dict = {"in": 0, "out": 0, "cost_usd": 0.0}


def take_translation_usage() -> dict:
    """Забрать накопленный расход перевода и обнулить счётчик.

    Забираем ИМЕННО с обнулением: следующий прогон в том же процессе не должен
    унаследовать чужие числа. Зовётся один раз, из конвейера, сразу после
    сборки выпуска.
    """
    out = dict(_TRANSLATION_USAGE)
    _TRANSLATION_USAGE.update({"in": 0, "out": 0, "cost_usd": 0.0})
    return out


def _translate_batch_in_own_loop(titles: list[str]) -> dict[str, str]:
    """Перевод в отдельном потоке со СВОИМ циклом событий (17.08.2026).

    Здесь стоял `asyncio.run(...)`, и он падал ВСЕГДА, когда дайджест идёт из
    сценария: `render_digest` синхронная, но зовут её из асинхронного конвейера
    (`pipeline.py:141`), а `asyncio.run` внутри работающего цикла — ошибка
    «cannot be called from a running event loop». Отказ гасился общим
    `except`, поэтому выглядел как «перевода нет», а не как поломка: замер
    17.08 показал два таких отказа за прогон и английские заголовки ФРС и ЕЦБ
    в выпуске.

    Отдельный поток выбран вместо перевода всей цепочки рендера в async:
    цепочка `pipeline → render_digest → _translate_titles_to_ru` синхронная на
    всём протяжении, и её переписывание — работа, несоразмерная одной строке.
    Корутина к циклу не привязана до первого `await`, поэтому создать её здесь
    и выполнить в чужом цикле безопасно.
    """
    from concurrent.futures import ThreadPoolExecutor  # noqa: PLC0415 — только здесь

    with ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(asyncio.run, _llm_translate_batch(titles)).result()


def _translate_titles_to_ru(titles: list[str]) -> dict[str, str]:
    """Translate any English titles in `titles` to Russian. Russian
    titles pass through unchanged. Results are cached on disk so each
    unique English headline is paid for only once across all future
    runs.

    Implementation: one batch LLM call per render with whichever
    titles are not already cached. Empty input → empty result.
    """
    if not titles:
        return {}
    from pathlib import Path
    from finengine.config import settings
    cache_path = Path(settings.data_dir) / "news" / "title_translations.json"
    try:
        cache: dict[str, str] = (
            __import__("json").loads(cache_path.read_text())
            if cache_path.exists() else {}
        )
    except (OSError, ValueError):
        cache = {}

    out: dict[str, str] = {}
    to_translate: list[str] = []
    for t in titles:
        if not t:
            continue
        if _looks_russian(t):
            out[t] = t
            continue
        cached = cache.get(t)
        if cached:
            out[t] = cached
            continue
        to_translate.append(t)

    if to_translate:
        try:
            fresh = _translate_batch_in_own_loop(to_translate)
        except Exception as e:  # noqa: BLE001 — render must never crash
            log.warning("title-translation: LLM call failed: %s", e)
            fresh = {}
        out.update(fresh)
        cache.update(fresh)
        try:
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            cache_path.write_text(
                __import__("json").dumps(cache, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except OSError as e:
            log.warning("title-translation: cache write failed: %s", e)
    # Anything still missing falls back to the original English.
    for t in titles:
        out.setdefault(t, t)
    return out


def _looks_russian(s: str) -> bool:
    """True if ≥30% of letters in `s` are Cyrillic — enough to skip
    translation. Mixed RU/Latin titles (e.g. "ЦБ РФ опубликовал MIACR")
    pass as Russian since the bulk of the text is already in target
    language."""
    letters = [c for c in s if c.isalpha()]
    if not letters:
        return True  # nothing to translate
    cyr = sum(1 for c in letters if "Ѐ" <= c <= "ӿ")
    return cyr / len(letters) >= 0.3


async def _llm_translate_batch(titles: list[str]) -> dict[str, str]:
    """One LLM call. Asks for JSON dict {title: ru_translation}."""
    import json as _json
    from finengine.llm import openrouter as _llm

    system = (
        "Ты — переводчик заголовков финансовых пресс-релизов с английского "
        "на русский. Стиль: деловой, краткий, точный (как РБК или "
        "Интерфакс). Возвращай ТОЛЬКО JSON-объект где каждый ключ — "
        "оригинальный заголовок, значение — русский перевод. Никакого "
        "сопроводительного текста, никаких markdown-обёрток."
    )
    user = (
        "Переведи на русский язык каждый из следующих заголовков "
        "(сохраняй имена собственные и аббревиатуры как есть):\n\n"
        + _json.dumps(titles, ensure_ascii=False, indent=2)
    )
    from finengine.config import settings
    try:
        text, _usage = await _llm.complete_with_usage(
            prompt=user, system=system,
            model=settings.news_classify_model,
            temperature=0.0,
            response_format={"type": "json_object"},
            # 8000, а не 2000. Модель рассуждающая, и токены размышлений идут
            # в тот же счёт: замер на живом проде 18.08 на пяти настоящих
            # заголовках регуляторов дал при бюджете 2000 отказ ModelSilent
            # (finish_reason=length, 6861 знак размышлений) — перевод вернул бы
            # пустоту, и заголовки ФРС/ЕЦБ остались бы в выпуске английскими.
            # На 4000 прошло впритык, на 8000 с запасом. Ровно этой же ошибкой
            # 17.08 был куплен бюджет классификации.
            max_output=8000,
        )
    except Exception as e:  # noqa: BLE001
        log.warning("translate: LLM call failed: %s", e)
        return {}
    # Расход перевода КОПИТСЯ, а не выбрасывается (17.08.2026). Раньше здесь
    # стояло `text, _ =` — usage уходил в никуда, и стоимость прогона в журнале
    # была занижена на весь перевод заголовков. Модель тут дешёвая
    # (deepseek-chat-v3), но «не считаем, потому что мало» — это как раз то,
    # из-за чего потом не сходятся цифры.
    _TRANSLATION_USAGE["in"] += int(_usage.get("in", 0))
    _TRANSLATION_USAGE["out"] += int(_usage.get("out", 0))
    _TRANSLATION_USAGE["cost_usd"] += float(_usage.get("cost_usd", 0.0) or 0.0)
    try:
        s = text.strip()
        if s.startswith("```"):
            s = s.split("\n", 1)[1] if "\n" in s else s[3:]
            if s.endswith("```"):
                s = s[:-3]
        parsed = _json.loads(s)
    except (ValueError, IndexError) as e:
        log.warning("translate: bad JSON: %s — raw=%s", e, text[:300])
        return {}
    if not isinstance(parsed, dict):
        return {}
    # Keep only valid str→str pairs.
    return {k: v for k, v in parsed.items()
            if isinstance(k, str) and isinstance(v, str)}


# Три счётчика шапки считают ПО НАПЕЧАТАННОМУ ТЕЛУ (18.08.2026).
#
# Здесь стоял один `_count_coverage`, который считал по классифицированному
# пулу — то есть по тому, что пришло от модели, а не по тому, что попало в
# выпуск. Живой выпуск 17.08 заявлял «20 компаний · 9 макропоказателей ·
# 1 публикаций регуляторов», тогда как в теле стояли 11 компаний,
# 19 макропоказателей и 7 публикаций: между пулом и телом лежат
# `select_long_list`, межпрогонный дедуп по `seen_hashes` и режим макро-блока.
# Шапка — первое, что читает человек, и она врала трижды в одной строке.


def _companies_in_body(shown: list[ClassifiedItem],
                       watchlist: list[EntityConfig]) -> set[str]:
    """Компании покрытия, чьи новости реально напечатаны в Long list/Прочих.

    Считаем РОВНО печатаемое — тем же выражением, что и звезда в
    `_render_section`: одна компания на пункт (`_detect_watchlist` берёт первую
    совпавшую) и только у корпоративных (у macro привязку делает не модель, а
    поиск). Прошлая редакция брала ВСЕ `matched_entities` пункта: на реальном
    сохранённом прогоне aaa9b9a3 (48 пунктов) она давала 33 компании при 19
    напечатанных — то есть шапка по-прежнему не сходилась с телом.
    """
    return {m for c in shown if c.category == "corporate"
            and (m := _detect_watchlist(c.item, watchlist))}


def _macro_indicators_in_body(dashboard_md: str) -> int:
    """Сколько РАЗНЫХ показателей напечатано в макро-дашборде.

    Считаем по готовому markdown, а не по payload: состав таблиц решает режим
    (`both` печатает показатель недели дважды — и в блоке недели, и в полной
    таблице, `week_only` — только свежие). Повтор логики режима здесь означал
    бы второй источник истины, который однажды разойдётся с первым.
    Жирным в таблице выделено только имя показателя.
    """
    return len(set(re.findall(r"\*\*(.+?)\*\*", dashboard_md)))


def _publications_in_body(regulators_md: str) -> int:
    """Число пунктов в блоке регуляторов — свежих (🏦) и архивных (🗄️)."""
    return sum(1 for line in regulators_md.splitlines()
               if line.startswith("- 🏦 ") or line.startswith("- 🗄️ "))


def _plural_ru(n: int, one: str, few: str, many: str) -> str:
    """«1 компания / 2 компании / 5 компаний».

    В выпуске 17.08 стояло «1 публикаций регуляторов» — согласование не
    делалось вовсе.
    """
    if n % 100 in (11, 12, 13, 14):
        return many
    tail = n % 10
    if tail == 1:
        return one
    if tail in (2, 3, 4):
        return few
    return many


def render_digest(
    classified: list[ClassifiedItem],
    *,
    cfg: NewsConfig,
    now: datetime,
    macro_payload: dict | None = None,
    seen_hashes: set[str] | None = None,
    macro_mode: str = "both",
) -> tuple[str, list[DigestItem]]:
    """Build the full markdown and return alongside DigestItem records
    suitable for persisting in SQLite.

    `now` is the timestamp shown in the header, normally Europe/Moscow.
    Coverage counts are derived from the rendered body (what the reader
    actually sees), not from config-level totals — IR team already knows
    they're tracking 100 companies, what they want to see is "how many
    of them moved today".
    """
    watchlist = cfg.watchlist()

    # Порядок сборки: СЕКЦИИ, потом шапка (18.08.2026). Шапка обязана называть
    # числа напечатанного тела, а тело до конца не известно, пока не собраны
    # блок регуляторов (он же даёт дедуп), long list и макро-дашборд.

    # 1) Regulator monitor — one section per ТЗ-mandated regulator
    # (ЦБ РФ / Минфин РФ / Росстат / ФРС / Минфин США / ЕЦБ / НБК)
    # showing today's items or a 7-day fallback. Macro news that came
    # from regulators (rate statements, MPC minutes, etc.) lives here,
    # not in the Long list — that's the single home for "what regulators
    # said".
    regulators_md, regulator_urls = _render_regulators_box(classified, cfg=cfg)

    # Публикация, уже показанная у регулятора, не идёт по второму кругу в
    # Long list / Прочие: 17.08 предварительная оценка Минфина РФ стояла в
    # выпуске дважды — архивным пунктом (🗄️) и отдельным «прочим событием».
    #
    # Фильтр гасит ПЕЧАТЬ в сетке, но не запись: `shown_by_regulators` ниже
    # уезжает в digest_items с in_long_list=False (18.08.2026). Пока эти пункты
    # выбрасывались совсем, терялись два потребителя персиста: fe_news_lookup
    # (`db.search_digest_items`) не находил НИ ОДНОЙ публикации регулятора и
    # агент отвечал «такой новости в выпуске не было» про напечатанное решение
    # по ставке, а повторная отдача прогона из БД печатала блок регуляторов
    # иначе первичного выпуска — архивным fallback (limit=2) вместо свежих
    # пунктов (cap=3).
    shown_by_regulators = [c for c in classified
                           if _url_key(c.item.url) in regulator_urls]
    pool = [c for c in classified
            if _url_key(c.item.url) not in regulator_urls]
    long_list, others = select_long_list(pool, seen_hashes=seen_hashes)

    # 2) Macro indicators — static snapshot of the world (numbers,
    # not news): CPI/PPI/GDP/NFP/unemployment/PMI/oil-stocks etc. with
    # M/M, Q/Q, Y/Y deltas. Updates lazily as data sources publish.
    dashboard_md = ""
    if macro_payload:
        from finengine.skills.news.macro_indicators import (
            render_macro_dashboard_md,
        )
        dashboard_md = render_macro_dashboard_md(macro_payload, mode=macro_mode)

    # Сетка Long list/Прочих печатает ровно две категории (`_render_section`):
    # регуляторные пункты живут в своём блоке, а `unknown` не печатается
    # нигде. В счёт компаний шапки идёт только то, что видно на экране.
    shown_items = [c for c in long_list + others
                   if c.category in ("macro", "corporate")]
    companies_today = len(_companies_in_body(shown_items, watchlist))
    macro_today = _macro_indicators_in_body(dashboard_md)
    reg_today = _publications_in_body(regulators_md)
    companies_total = len(watchlist)

    header = (
        f"# 📈 IR Daily Digest — {now.day} {_MONTH_RU[now.month]} {now.year}, "
        f"{now.strftime('%H:%M')} MSK\n\n"
        f"Сегодня в дайджесте: {companies_today} "
        f"{_plural_ru(companies_today, 'компания', 'компании', 'компаний')} "
        f"(из {companies_total} в покрытии) · "
        f"{macro_today} "
        f"{_plural_ru(macro_today, 'макропоказатель', 'макропоказателя', 'макропоказателей')} · "
        f"{reg_today} "
        f"{_plural_ru(reg_today, 'публикация', 'публикации', 'публикаций')} регуляторов"
    )

    parts: list[str] = [header, "\n---\n"]

    if dashboard_md:
        parts.append(dashboard_md)
        parts.append("\n---\n")

    parts.append(regulators_md)
    parts.append("\n---\n")

    # Long list: show placeholders for empty 2×3 cells so IR-team sees
    # coverage gaps. Прочие: hide empties — those are bulk follow-ups.
    if long_list or True:  # always render long list header for consistency
        parts.append(_render_section(
            "## 🔥 Long list", long_list, watchlist=watchlist, show_empty=True,
        ))
        parts.append("\n---\n")

    if others:
        parts.append(_render_section(
            "## 📰 Прочие события дня", others, watchlist=watchlist, show_empty=False,
        ))
        parts.append("\n---\n")

    # CBR banking review carve-out (special section per ТЗ заказчика).
    # Из `pool`, а не из `classified`: ключи отбора («банковск», «обзор») у ЦБ
    # рядовые, и пункт, уже напечатанный в блоке регуляторов, выходил тем же
    # URL второй раз здесь — тот же дефект «один URL в двух секциях», ради
    # которого заведён фильтр `pool`, только с другой стороны.
    cbr_review = [
        c for c in pool
        if c.item.source_id in ("cbr_review", "cbr_press")
        and any(t in c.one_sentence_ru.lower() for t in
                ("банковск", "обзор", "прибыль сектора"))
    ]
    if cbr_review:
        parts.append("## 🏦 Обзор ЦБ РФ по банковскому сектору\n")
        for c in cbr_review[:2]:
            parts.append(
                f"> {c.one_sentence_ru}\n> — [{c.item.source}]({c.item.url})\n"
            )

    parts.append("\n⭐ — компания из watchlist.")

    md = "\n".join(parts)

    # Persistable DigestItems
    digest_items: list[DigestItem] = []
    long_ids = {id(c) for c in long_list}
    for c in long_list + others + shown_by_regulators:
        digest_items.append(DigestItem(
            classified=c,
            final_score=c.importance,  # placeholder — rank score lost by now
            in_long_list=(id(c) in long_ids),
            watchlist_match=_detect_watchlist(c.item, watchlist),
        ))

    return md, digest_items
