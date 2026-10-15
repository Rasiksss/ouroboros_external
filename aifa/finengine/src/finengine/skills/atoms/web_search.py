"""`web.search` — поиск во внешних источниках для цикла гибридного планировщика (часть 19 доводки).

Сетевой примитив с параметрами: искать умеет платформа, запрос задаёт планировщик, разбор находок и
соединение с данными отчёта пишет автор атомов кодом без сети.

**Запрос — концепт шага.** Индекс готовых результатов `Case` различает результаты одного типа по
концепту, а не по `extra`: с запросом в `extra` из двух поисков остался бы доступен только последний.
Режим — `extra["topic"]`: `general` (по умолчанию) или `news`; дату публикации поисковик отдаёт только
в `news` (`skills/web_search.py`).

**Результат — `DynamicArtifact`, не `ExternalFactRef`.** У того обязательны сущность, юрисдикция и дата,
у произвольного поиска их нет. Находки не числа отчётности (нерушимое решение №3): `origin=["external"]`.

**Согласие.** При выключенном тумблере «Поиск в интернете» узел падает с причиной
(`analysis.external.require_web_search`), а не возвращает пусто: планировщик не должен искать обход.

`search` внедряется параметром ради тестов без сети — тот же приём, что `collector` у `explain.external`."""
from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

from finengine.config import settings
from finengine.contracts import DynamicArtifact
from finengine.skills import web_search as _ws
from finengine.skills.analysis.external import require_web_search
from finengine.skills.spec.atom_registry import NodeArgs

ATOM_VERSION = "1.0.0"
WEB_SEARCH_TYPE = "dyn:web_search"
TOPICS = ("general", "news")
PRODUCER_PREFIX = "web.search@"
_STATE_TITLE_CHARS = 120
_STATE_DATE_CHARS = 40
_STATE_HIDDEN_NOTE_CHARS = 80
_STATE_SEPARATOR = " — "

Search = Callable[..., Awaitable[list]]


def web_search_step_problem(args: NodeArgs) -> str | None:
    if not (args.concept or "").strip():
        return "web.search: запрос — поле concept шага, оно пусто"
    topic = args.extra.get("topic")
    if topic is not None and topic not in TOPICS:
        return f"web.search: extra.topic={topic!r} — допустимы general и news"
    return None


def web_search_executor(args: NodeArgs, ctx, *, search: Search | None = None) -> list[DynamicArtifact]:  # noqa: ARG001
    require_web_search()
    if search is None and not _ws.is_enabled():
        raise RuntimeError("поиск в интернете не настроен на стенде: нет ключа поисковика")
    query = (args.concept or "").strip()
    topic = args.extra.get("topic") or TOPICS[0]
    found = asyncio.run((search or _ws.web_search)(query, max_results=settings.web_search_max_results, topic=topic))
    hits = [
        {"title": hit.title, "url": hit.url, "snippet": hit.content[:settings.web_search_snippet_chars],
         "published_date": hit.published_date}
        for hit in found if hit.url
    ]
    return [DynamicArtifact(
        output_type=WEB_SEARCH_TYPE, payload={"query": query, "topic": topic, "hits": hits},
        origin=["external"], producer=f"{PRODUCER_PREFIX}{ATOM_VERSION}",
    )]


def _one_line(text: str | None) -> str:
    """Текст из интернета — одной строкой: с переводом строки внутри заголовок или адрес находки открывал
    бы в состоянии хода свой раздел («## Итог прошлого хода») и говорил с планировщиком голосом системы."""
    return " ".join((text or "").split())


def _hit_line(number: int, hit: dict) -> str:
    date = _one_line(hit.get("published_date"))[:_STATE_DATE_CHARS]
    line = f"  {number}. {_one_line(hit['title'])[:_STATE_TITLE_CHARS]}{_STATE_SEPARATOR}{_one_line(hit['url'])}"
    return line + (_STATE_SEPARATOR + date if date else "")


def describe_hits(payload: dict, limit: int) -> str:
    """Находки для состояния хода: адрес каждой показанной — целиком (по нему планировщик закажет загрузку),
    фрагменты делят между собой то, что осталось от предела `limit`. Находка, строка которой в остаток
    предела не помещается (длинный адрес), не показывается — об этом говорит последняя строка."""
    hits = payload.get("hits") or []
    head = f"режим {payload.get('topic')}; находок: {len(hits)}"
    if not hits:
        return head + " — поиск ничего не нашёл"
    room = limit - len(head) - _STATE_HIDDEN_NOTE_CHARS
    shown: list[tuple[str, dict]] = []
    spent = 0
    for number, hit in enumerate(hits, 1):
        line = _hit_line(number, hit)
        size = len(line) + 1 + len(_STATE_SEPARATOR)
        if spent + size <= room:
            shown.append((line, hit))
            spent += size
    hidden = len(hits) - len(shown)
    share = (room - spent) // len(shown) if shown else 0
    rows = [
        line + (_STATE_SEPARATOR + _one_line(hit["snippet"])[:share] if share and hit.get("snippet") else "")
        for line, hit in shown
    ]
    note = [f"  …ещё находок: {hidden} — не поместились в предел состояния"] if hidden else []
    return "\n".join([head, *rows, *note])


__all__ = ["ATOM_VERSION", "PRODUCER_PREFIX", "TOPICS", "WEB_SEARCH_TYPE", "describe_hits", "web_search_executor",
           "web_search_step_problem"]
