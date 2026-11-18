"""Step 1 of the pipeline: parallel-fetch all active connectors,
persist raw items in SQLite (dedup-by-hash), return the surviving set.

Connectors run in parallel via `asyncio.to_thread` since feedparser is
sync. RSS is I/O bound — concurrency >> CPU.
"""
from __future__ import annotations

import asyncio
import logging

from finengine.skills.news import config as _cfg
from finengine.skills.news import db as _db
from finengine.skills.news.connectors import registry
from finengine.skills.news.models import NewsItem

log = logging.getLogger(__name__)


class _ConnectorFailureLog(logging.Handler):
    """Собирает отказы коннекторов за один прогон сбора (17.08.2026).

    ЗАЧЕМ. Счётчик `failed` показывал НОЛЬ при живых отказах: `_fetch_one`
    записывал отказ только если коннектор БРОСИЛ исключение, а по контракту
    (`connectors/base.py`) он не бросает никогда — гасит ошибку, пишет
    предупреждение и возвращает пустой список. Замер на проде 17.08: 19 отказов
    Investing (403), `bea_news` (сеть недоступна), три EDGAR (таймаут),
    `pr_newswire` (404), `techcrunch` (битый XML) — и `failed: {}`.
    Владелец диагностирует этой строкой «почему новостей нет», а она молчала.

    ПОЧЕМУ ПЕРЕХВАТ, А НЕ ПРАВКА КОННЕКТОРОВ. Отказ у них не один на файл:
    только в RSS их пять (нет feedparser, код ответа, сеть, разбор, bozo), и
    столько же в остальных пяти коннекторах. Перехват ловит ВСЕ пути разом,
    включая те, что появятся позже, и не ломает контракт «наружу не бросаем».

    Привязка к источнику — по вхождению его id в текст записи: коннекторы
    печатают `self.source.id` в каждом предупреждении. Запись, в которой не
    нашёлся ни один id, отбрасывается: лучше промолчать, чем приписать отказ
    не тому источнику.
    """

    def __init__(self, ids_to_names: dict[str, str]) -> None:
        super().__init__(level=logging.WARNING)
        self._ids = ids_to_names
        self.by_source: dict[str, str] = {}

    def emit(self, record: logging.LogRecord) -> None:
        text = record.getMessage()
        for sid, display in self._ids.items():
            if sid in text and display not in self.by_source:
                self.by_source[display] = text[:120]
                return


def _build_connector(source: _cfg.SourceConfig):
    cls = registry().get(source.connector)
    if cls is None:
        log.warning("collect: no connector for %r (source %s)",
                    source.connector, source.id)
        return None
    return cls(source)


async def _fetch_one(
    source: _cfg.SourceConfig, since_hours: int,
    errors: dict[str, str] | None = None,
) -> list[NewsItem]:
    conn = _build_connector(source)
    if conn is None:
        return []
    # connectors are sync (feedparser is sync); offload to thread pool.
    try:
        return await asyncio.to_thread(conn.fetch, since_hours=since_hours)
    except Exception as e:  # noqa: BLE001
        log.exception("collect: connector %s crashed: %s", source.id, e)
        if errors is not None:
            errors[source.display_name] = f"{type(e).__name__}: {e}"[:120]
        return []


# #97: пользовательские группы источников (ручка сценария) → внутренний type.
SOURCE_GROUPS = {
    "official": "Официальные",   # регуляторы, EDGAR, e-disclosure, IR компаний
    "media": "СМИ",              # wire/tier1 новостные сайты
    "telegram": "Telegram",
    "aggregator": "Агрегаторы",  # google news и т.п.
}
_TYPE_TO_GROUP = {"official": "official", "wire": "media", "tier1": "media",
                  "wire_tg": "telegram", "aggregator": "aggregator"}

# 🔴 ПОЛ ОКНА ДЛЯ ОФИЦИАЛЬНЫХ ИСТОЧНИКОВ (18.08.2026).
# Блок «Регуляторы» обещает читателю «за сегодня или ближайший архив до 7 дн.»,
# но архив он берёт ИЗ БАЗЫ (`recent_items_from_sources`), а в базу попадает
# только то, что прошло окно сбора. На живом выпуске с окном 8 часов Росстат
# получил «Свежих публикаций за 7 дней нет» при четырёх реальных публикациях
# (недельный ИПЦ 12.08, оценка ВВП за II квартал): он публикует раз в неделю,
# и в восьмичасовое окно не попадает НИКОГДА.
#
# Парная правка живёт в конвейере (`pipeline`, стадия 2): он отсекает пул по
# окну пользователя ДО классификации, иначе этот недельный запас уедет в Long
# list и «Прочие» — ровно так 18.08 туда попали четыре публикации Минфина
# возрастом 6–13 дней. Снимать одну правку без второй нельзя.
_OFFICIAL_FLOOR_H = 168


async def collect_all(
    since_hours: int = 48,
    *,
    user_id: int | None = None,
    source_groups: dict | None = None,
    stats_out: dict | None = None,
) -> list[NewsItem]:
    """Pull from every active source in parallel. Persist raw items in
    SQLite (insert-or-ignore by raw_hash → dedup is automatic across
    same-day runs as well). Returns the consolidated list across all
    sources, post-dedup.

    When `user_id` is given, the active-source list is the user's
    overlay view (defaults − their `disabled` ∪ their `added`). For
    `user_id=None` — e.g. the background daily-trigger scheduler — we
    fall back to the global default catalogue verbatim.
    """
    cfg = _cfg.load()

    # Mirror sources into SQLite — keeps the join in `fetch_unprocessed_since`
    # working even when sources.yaml changes mid-day.
    for s in cfg.sources:
        _db.upsert_source(
            source_id=s.id, display_name=s.display_name, source_type=s.type,
            region=s.region, url=s.url, weight=s.weight, active=s.active,
        )

    # Apply per-user overlay if a user_id was passed in.
    if user_id:
        from finengine.skills.news import sources_user as _su
        resolved = _su.resolve_sources(user_id, cfg.sources)
        active = [s for s in resolved if getattr(s, "active", True)]
        # Mirror user-added sources into SQLite too so they show up in
        # join queries downstream.
        for s in active:
            _db.upsert_source(
                source_id=s.id, display_name=s.display_name,
                source_type=s.type, region=s.region, url=s.url,
                weight=s.weight, active=s.active,
            )
        log.info("collect: user=%s overlay → %d active sources",
                 user_id, len(active))
    else:
        active = cfg.active_sources()
    # #97: ручка групп источников (сценарий): выключенная группа снимает все её
    # источники. Отсутствующий ключ = группа включена (обратная совместимость).
    if source_groups:
        before = len(active)
        active = [
            s for s in active
            if source_groups.get(_TYPE_TO_GROUP.get(s.type, "media"), True)
        ]
        off = [SOURCE_GROUPS[g] for g, on in source_groups.items()
               if not on and g in SOURCE_GROUPS]
        if off:
            log.info("collect: группы выключены (%s) — источников %d → %d",
                     ", ".join(off), before, len(active))
    if not active:
        log.warning("collect: no active sources after overlay")
        return []

    log.info("collect: fetching %d active sources (window %dh, официальные %dh)",
             len(active), since_hours, max(since_hours, _OFFICIAL_FLOOR_H))
    _errors: dict[str, str] = {}
    # Ставим перехват на дерево логгеров коннекторов на время сбора: их
    # предупреждения — единственный след отказа, наружу они не бросают.
    _sink = _ConnectorFailureLog({s.id: s.display_name for s in active})
    _conn_log = logging.getLogger("finengine.skills.news.connectors")
    _conn_log.addHandler(_sink)
    try:
        fetched_lists = await asyncio.gather(
            *[_fetch_one(s, max(since_hours, _OFFICIAL_FLOOR_H)
                         if s.type == "official" else since_hours,
                         errors=_errors) for s in active],
            return_exceptions=False,
        )
    finally:
        _conn_log.removeHandler(_sink)
    # Упавшие с исключением важнее: их сообщение точнее перехваченного.
    _errors = {**_sink.by_source, **_errors}
    # #97: пер-источник статистика — диагноз «новостей нет, хотя есть» (какие
    # источники дали 0/упали) + доверие пользователей к сбору.
    if stats_out is not None:
        stats_out["per_source"] = {
            s.display_name: len(items) for s, items in zip(active, fetched_lists)
        }
        stats_out["failed"] = _errors
        stats_out["sources_total"] = len(active)

    # In-memory dedup before DB write — same story syndicated by multiple
    # wires (Reuters → Интерфакс → РБК) often has identical title.
    by_hash: dict[str, NewsItem] = {}
    inserted = 0
    duplicates = 0
    for source, items in zip(active, fetched_lists):
        for it in items:
            if it.raw_hash in by_hash:
                # Keep the version with higher source weight.
                if by_hash[it.raw_hash].source_weight < it.source_weight:
                    by_hash[it.raw_hash] = it
                duplicates += 1
                continue
            by_hash[it.raw_hash] = it
            if _db.upsert_raw_item(it):
                inserted += 1

    log.info("collect: %d unique items, %d duplicates skipped, %d new in DB",
             len(by_hash), duplicates, inserted)
    return list(by_hash.values())
