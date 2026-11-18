"""End-to-end Daily News pipeline orchestrator.

    collect → filter → rank → classify (LLM, once) → render → persist

Public entry: `run_daily_news_pipeline(profile=..., since_hours=...)`.
Backend HTTP endpoint and cron both call this; on-demand UI is just an
HTTP GET that triggers the same function.
"""
from __future__ import annotations

import logging
import time
import uuid
from datetime import datetime, timezone, timedelta
from typing import Any

from zoneinfo import ZoneInfo

from finengine.skills.news import classify as _classify
from finengine.skills.news import collect as _collect
from finengine.skills.news import config as _cfg
from finengine.skills.news import db as _db
from finengine.skills.news import filter as _filter
from finengine.skills.news import rank as _rank
from finengine.skills.news import render as _render

log = logging.getLogger(__name__)

_MSK = ZoneInfo("Europe/Moscow")


def settings_for_user(user_id: int | None) -> dict:
    """Параметры дайджеста, сохранённые владельцем на странице автоматизаций.

    🔴 ЗАЧЕМ ЭТО ЗАВЕДЕНО (22.08.2026). Запуск из ЧАТА и запуск КНОПКОЙ давали
    РАЗНЫЙ выпуск. Кнопка идёт через раннер сценария и читает его `params`;
    инструмент `daily_news_digest` о сценарии не знал вовсе и подставлял свои
    умолчания — окно 48 часов вместо сохранённых восьми. Живой прогон админа
    22.08 собрал 3911 новостей вместо обычных ~900 ровно поэтому.

    Пустой словарь = у владельца нет сценария дайджеста; зовущий подставит
    свои умолчания.
    """
    if user_id is None:
        return {}
    from finengine.skills.scenarios import store as _scn_store

    for scn in _scn_store.list_for_user(user_id):
        if scn.kind == "ir_daily":
            return dict(scn.params_dict or {})
    return {}


async def run_daily_news_pipeline(
    *,
    profile: str = "ir_market_news",
    since_hours: int = 48,
    top_n_candidates: int | None = None,
    model: str | None = None,
    user_id: int | None = None,
    focus: str = "",
    source_groups: dict | None = None,
    macro_mode: str = "both",
    progress: Any = None,
) -> dict[str, Any]:
    """Run the full pipeline once and return:
        {
          run_id, markdown, cost_usd, items_raw, items_kept,
          tokens_in, tokens_out, duration_s
        }
    Persists everything in SQLite. Safe to call from cron OR live request.

    Per-user mode: when `user_id` is set, the source pool is the user's
    overlay (defaults − their disabled ∪ their added). The background
    daily scheduler still calls with `user_id=None` to fetch the global
    catalogue — that's the "communal" pass that warms the news.db
    raw_items cache for everyone.
    """
    run_id = str(uuid.uuid4())
    started = time.perf_counter()
    _db.init_schema()
    _db.start_run(run_id, profile, user_id=user_id)

    cfg = _cfg.load()
    error: str | None = None
    items_raw = items_kept = llm_in = llm_out = 0
    cost = 0.0
    markdown = ""
    source_stats: dict = {}
    # Точки мониторинга прогона (17.08.2026): время по стадиям, модель,
    # здоровье источников. Кладутся в `digest_runs.diag` — чтобы «почему
    # прогон идёт 6 минут» и «почему новостей мало» разбирались по журналу,
    # а не пересказом из памяти.
    marks: dict = {}
    _t = time.perf_counter

    # Прогон идёт 5-7 минут, и всё это время чат молчал: владелец 24.08
    # написал «ответ завис на 5 минут». Календарь стадии показывает, дайджест
    # не показывал — отсюда и ощущение зависания на нормальной работе.
    def _say(text: str) -> None:
        if progress is not None:
            try:
                progress(text)
            except Exception:  # noqa: BLE001 — обратная связь не ломает прогон
                pass

    try:
        # === Stage 1: collect ===
        _s = _t()
        _say("Собираю новости из источников (это дольше всего, ~2-3 мин)…")
        items = await _collect.collect_all(
            since_hours=since_hours, user_id=user_id,
            source_groups=source_groups, stats_out=source_stats,
        )
        items_raw = len(items)
        marks["collect_s"] = round(_t() - _s, 1)
        log.info("pipeline: collected %d raw items", items_raw)

        # === Stage 2: annotate + filter ===
        # Окно пользователя решает, что попадёт В ТЕЛО выпуска. Официальные
        # источники собираются шире (пол в неделю, см. collect._OFFICIAL_FLOOR_H)
        # — иначе блок регуляторов не может показать обещанный архив за 7 дней.
        # Но в Long list и «Прочие» ИЗ ЭТОГО ЗАПАСА ничего уходить не должно:
        # 18.08 на окне 8 часов туда уехали четыре публикации Минфина возрастом
        # 6–13 дней. Блока регуляторов отбор не касается — он читает базу сам.
        _cut = datetime.now(timezone.utc) - timedelta(hours=since_hours)
        _wide = len(items)
        items = [it for it in items if not it.published_at or it.published_at >= _cut]
        if _wide != len(items):
            log.info("pipeline: окно %dh отсекло %d записей из архива официальных",
                     since_hours, _wide - len(items))
        _filter.annotate(items, cfg)
        # Back-fill matched_json in DB so we can grep for filter coverage
        # via SQL post-hoc. annotate() above mutated items in-memory only;
        # the raw_items rows were upserted in collect_all with empty
        # matched_json placeholder.
        for it in items:
            try:
                _db.set_matched(it.raw_hash, it.matched_entities, it.matched_topics)
            except Exception:  # noqa: BLE001
                pass
        items = _filter.filter_relevant(items, cfg)
        # Title-similarity dedup AFTER rule filter — wires often repeat
        # the same Reuters/AP story with rephrased titles, but URL hash
        # is different so canonical_hash dedup doesn't catch them.
        items = _filter.dedup_near_duplicates(items)
        items_kept = len(items)
        log.info("pipeline: %d items after rule filter", items_kept)

        if not items:
            log.warning("pipeline: no candidates after filter — empty digest")
            # Окно называется НАСТОЯЩЕЕ. Здесь стояло жёсткое «48ч» — при
            # ручке в 8 часов сообщение врало бы ровно в том месте, где
            # читатель ищет причину пустого выпуска.
            markdown = (
                "# 📈 IR Daily Digest\n\n"
                f"Сегодня в окне {since_hours}ч не найдено релевантных событий "
                "из подключённых источников. Проверьте подключение RSS-фидов."
            )
            return _finish(run_id, markdown, started, items_raw, items_kept, 0, 0, 0.0, None,
                           source_stats=source_stats)

        # === Stage 3: rank in code (no LLM) ===
        # None → настройка (см. config.news_top_candidates): потолок меняется
        # переменной окружения, без выкатки.
        from finengine.config import settings as _settings
        n_cand = top_n_candidates or _settings.news_top_candidates
        candidates = _rank.top_n(items, n=n_cand)
        _say(f"Отобрано {items_kept}, разбираю {len(candidates)} важнейших моделью…")
        log.info("pipeline: %d top candidates → LLM", len(candidates))

        # === Stage 4: single LLM batch classify ===
        _s = _t()
        classified, usage = await _classify.classify_batch(candidates, model=model, focus=focus)
        marks["classify_s"] = round(_t() - _s, 1)
        # 🔴 ОТКАЗ КЛАССИФИКАЦИИ — ЭТО ОТКАЗ ПРОГОНА (17.08.2026).
        # `classify_batch` гасит исключение и возвращает пустой список с ключом
        # `error` в usage. Конвейер этот ключ не смотрел, и выпуск сохранялся
        # как УСПЕШНЫЙ: заголовок «0 компаний · 0 макропоказателей · 0
        # публикаций регуляторов» при 942 собранных новостях, а в журнале
        # «ошибка: нет». Пустой выпуск, выданный за нормальный, — худшее из
        # возможного: его никто не перепроверит.
        if usage.get("error"):
            raise RuntimeError(f"классификация не удалась: {usage['error']}")
        marks["model"] = usage.get("model") or model or ""
        marks["candidates"] = len(candidates)
        llm_in = int(usage.get("in", 0))
        llm_out = int(usage.get("out", 0))
        cost = float(usage.get("cost_usd", 0.0))

        # === Stage 5: render ===
        # Render derives coverage counts (companies, macro, regs) from
        # the actual classified pool — what surfaced today, not the
        # static watchlist size. Macro indicator dashboard pulled in
        # parallel here; FRED has its own 6h cache so a missed call
        # just reuses yesterday's prints (acceptable — these series
        # release monthly/weekly).
        from finengine.skills.news.macro_indicators import (
            fetch_macro_dashboard,
        )
        now_msk = datetime.now(_MSK)
        try:
            macro_payload = await fetch_macro_dashboard()
        except Exception as e:  # noqa: BLE001
            log.warning("macro: fetch_macro_dashboard failed: %s", e)
            macro_payload = {}
        # #94: межпрогонный дедуп — не показывать в long list то, что уже было
        # в long list прогонов за последнюю неделю (окно сбора 48ч + ежедневный
        # запуск легально протаскивали вчерашние истории; прод: 3/16 повторов).
        try:
            seen_hashes = _db.recent_long_list_hashes(days=7, user_id=user_id)
        except Exception:  # noqa: BLE001 — дедуп best-effort, дайджест важнее
            log.warning("recent_long_list_hashes failed — прогон без межпрогонного дедупа",
                        exc_info=True)
            seen_hashes = set()
        markdown, digest_items = _render.render_digest(
            classified, cfg=cfg, now=now_msk,
            macro_payload=macro_payload,
            seen_hashes=seen_hashes,
            macro_mode=macro_mode,
        )
        shown = {d.classified.item.raw_hash for d in digest_items if d.in_long_list}
        log.info("long list: повторов прошлых прогонов скрыто %d",
                 len([1 for c in classified
                      if c.is_relevant and c.item.raw_hash in seen_hashes
                      and c.item.raw_hash not in shown]))

        # Расход на перевод заголовков (17.08.2026). До этой правки usage
        # перевода выбрасывался, и стоимость прогона в журнале была занижена
        # ровно на него. Забираем с обнулением: следующий прогон в том же
        # процессе не должен унаследовать чужие числа.
        _tr = _render.take_translation_usage()
        if _tr.get("in") or _tr.get("out"):
            llm_in += int(_tr["in"])
            llm_out += int(_tr["out"])
            cost += float(_tr.get("cost_usd") or 0.0)
            marks["translate_in"] = int(_tr["in"])
            marks["translate_out"] = int(_tr["out"])

        # === Stage 6: persist digest items ===
        _db.insert_digest_items(run_id, digest_items)

    except Exception as e:  # noqa: BLE001
        log.exception("pipeline: failed")
        error = f"{type(e).__name__}: {e}"
        if not markdown:
            markdown = f"⚠️ Ошибка сборки дайджеста: {error}"

    return _finish(run_id, markdown, started, items_raw, items_kept,
                   llm_in, llm_out, cost, error, source_stats=source_stats,
                   marks=marks)


def _build_diag(duration_s: float, source_stats: dict | None,
                marks: dict | None) -> dict[str, Any]:
    """Точки мониторинга одного прогона — для разбора ПОСЛЕ, а не по памяти.

    Что кладём и зачем (всё найдено разбором 17.08):
      * `sources_*` — из 180 активных источников новости дали 98, молчали 82,
        а счётчик отказов при этом показывал ноль. Теперь эти три числа
        сохраняются на каждый прогон: видно, когда лента умирает, и когда;
      * `model` — модель классификации стала настройкой, и надо знать, каким
        именно прогон считали, когда сравниваем стоимость;
      * `collect_s` / `classify_s` — на прогоне 17.08 из 353 секунд почти все
        ушли в сбор, но доказать это можно было только по секундомеру рядом.
    """
    st = source_stats or {}
    per = st.get("per_source") or {}
    failed = st.get("failed") or {}
    out: dict[str, Any] = {
        "duration_s": duration_s,
        "sources_total": st.get("sources_total", 0),
        "sources_with_news": sum(1 for v in per.values() if v),
        "sources_failed": len(failed),
        "sources_silent": sum(1 for n, v in per.items() if not v and n not in failed),
    }
    if failed:
        # Имена — чтобы мёртвую ленту было видно по имени, а не по счётчику.
        out["failed_names"] = sorted(failed)[:20]
    out.update(marks or {})
    return out


def _finish(
    run_id: str, markdown: str, started: float,
    items_raw: int, items_kept: int, llm_in: int, llm_out: int,
    cost: float, error: str | None, *,
    source_stats: dict | None = None,
    marks: dict | None = None,
) -> dict[str, Any]:
    duration_s = round(time.perf_counter() - started, 2)
    diag = _build_diag(duration_s, source_stats, marks)
    _db.finish_run(
        run_id, items_raw=items_raw, items_kept=items_kept,
        llm_in=llm_in, llm_out=llm_out, cost_usd=cost, error=error,
        diag=diag,
    )
    log.info("pipeline: мониторинг run=%s %s", run_id[:8], diag)
    log.info(
        "pipeline: done run=%s raw=%d kept=%d llm_in=%d llm_out=%d "
        "cost=$%.4f time=%ss error=%s",
        run_id[:8], items_raw, items_kept, llm_in, llm_out,
        cost, duration_s, error,
    )
    return {
        "run_id": run_id,
        "markdown": markdown,
        "items_raw": items_raw,
        "items_kept": items_kept,
        "tokens_in": llm_in,
        "tokens_out": llm_out,
        "source_stats": source_stats or {},
        "cost_usd": cost,
        "duration_s": duration_s,
        "error": error,
    }


def get_cached_digest(profile: str = "ir_market_news",
                     max_age_hours: float = 12.0,
                     user_id: int | None = None) -> dict[str, Any] | None:
    """Return the most recent successful run's markdown if it's fresh.
    Used by the UI: 'собери дайджест' → if cache fresh, just return it.

    Выпуск отдаётся ТОГО ЖЕ владельца: чужой собран по чужому окну сбора,
    чужим группам источников и чужому фокусу (см. db.latest_digest_run).
    """
    _db.init_schema()
    last = _db.latest_digest_run(profile, user_id=user_id)
    if last is None:
        return None
    finished = last.get("finished_at") or 0
    age_h = (datetime.now(timezone.utc).timestamp() - finished) / 3600
    if age_h > max_age_hours:
        return None
    # Re-render from persisted digest_items so we don't store the markdown
    # blob in two places (db has the structured data, render is cheap).
    items = _db.digest_items_for_run(last["run_id"])
    if not items:
        return None
    md = _render_from_db_rows(items)
    return {
        "run_id": last["run_id"],
        "markdown": md,
        "items_raw": last.get("items_raw") or 0,
        "items_kept": last.get("items_kept") or 0,
        "cost_usd": last.get("cost_usd") or 0,
        "age_hours": round(age_h, 2),
    }


def _render_from_db_rows(rows: list[dict]) -> str:
    """Reconstruct ClassifiedItem from DB rows + re-render.
    Lossy for `published_at` precision but good enough for cache."""
    import json as _json
    from finengine.skills.news.models import ClassifiedItem, NewsItem
    cfg = _cfg.load()
    now_msk = datetime.now(_MSK)
    classified: list[ClassifiedItem] = []
    for r in rows:
        # Restore matched_entities/matched_topics from saved JSON so
        # _detect_watchlist works on cache-rendered output (otherwise
        # ⭐ marks vanish on every UI fetch of the cached digest).
        try:
            mj = _json.loads(r.get("matched_json") or "{}")
            mentities = list(mj.get("entities", []))
            mtopics = list(mj.get("topics", []))
        except (ValueError, TypeError):
            mentities = mtopics = []
        item = NewsItem(
            source=r["source_name"], source_id=r.get("source_id", ""),
            source_type="wire",
            region=r.get("region", "global"),
            source_weight=0.5,
            title=r["title"], snippet=r.get("snippet") or "",
            url=r["url"],
            published_at=datetime.fromtimestamp(
                r["published_at"], tz=timezone.utc),
            raw_hash=r["raw_hash"],
            matched_entities=mentities,
            matched_topics=mtopics,
        )
        classified.append(ClassifiedItem(
            item=item,
            is_relevant=bool(r["is_relevant"]),
            category=r.get("category") or "unknown",
            region=r.get("region") or "global",
            importance=float(r.get("importance") or 0),
            one_sentence_ru=r.get("one_sentence") or item.title,
            why_important=r.get("why_important") or "",
        ))
    # Cached re-render: reuse on-disk FRED cache too. If FRED hadn't
    # been fetched yet (e.g. first run after `force_refresh=true` and
    # cache invalidated), pulling fresh would mean an async call from
    # a sync code path — not worth it. Synchronous cache read only.
    from finengine.skills.news.macro_indicators import _read_cache
    cached_macro = _read_cache() or {}
    macro_payload = cached_macro.get("data") or {}
    md, _ = _render.render_digest(
        classified, cfg=cfg, now=now_msk, macro_payload=macro_payload,
    )
    return md
