"""IR Calendar agent — hybrid ±14-day corporate-events calendar across the
analytical-coverage universe (RU + foreign), from layered sources.

Window is symmetric around today:
  - upcoming [today .. today+14]   — scheduled events. US/EU IR pages announce
    earnings/investor-day dates ahead of time.
  - recent   [today-14 .. today)   — already-published events. Many RU IR URLs
    point at press-release/news feeds (verified: 8/13 RU links), where an event
    just appears with no advance notice — we look back so the agent doesn't miss it.

Sources are layered (a small priority matrix). Each source yields CalendarEvents
tagged with its id; events are merged by (company, date, type), `sources`
accumulates, and confidence = number of agreeing sources. Source precedence picks
the displayed link/excerpt.

  ir_site     — per-company IR page, headless-Chromium render + LLM extract (universal)
  finnhub     — foreign earnings dates via REST API (free tier; no RU coverage)
  edisclosure — RU material facts (e-disclosure.ru), LLM forward-date extraction
  smartlab    — RU aggregator HTML calendar (optional cross-check)

First-party sources (ir_site, edisclosure) outrank aggregators/APIs for the
displayed source_url. Every company is covered by ir_site as the graceful fallback;
structured sources (finnhub/edisclosure/smartlab) degrade to [] when unconfigured.
"""
from __future__ import annotations

import asyncio
import dataclasses
import json
import logging
import re
from collections import defaultdict
from contextvars import ContextVar
from datetime import date, timedelta
from pathlib import Path
from typing import AsyncIterator, Callable

import httpx

from finengine.config import settings
from finengine.llm import openrouter as llm

log = logging.getLogger(__name__)

_HTTP_TIMEOUT = 15.0  # httpx fallback + Finnhub/smartlab; short so dead sites give up fast
# IR-page fetch concurrency. Each page is rendered in headless Chromium via
# news.connectors.playwright_base.render_page, whose process-global semaphore
# caps live browsers at 3 — so effective render parallelism is min(this, 3).
# 4 lets a coroutine run LLM extraction / Finnhub while 3 render.
_MAX_CONCURRENT_FETCHES = 4
_MAX_PAGE_CHARS = 80_000  # cap LLM input per page
_MIN_USEFUL_CHARS = 400  # below this a rendered page is a shell — fall back to httpx
_WINDOW_BACK_DAYS = 14   # дефолт look back: catch already-published events (RU press feeds)
_WINDOW_FWD_DAYS = 14    # дефолт look forward: catch announced upcoming events (US/EU)
# #87: окно настраивается per-запуск (params сценария / чат-тул). Contextvar —
# изоляция по asyncio-таску (каждый прогон = свой таск), хелперы читают без
# прокидки через 7 сигнатур. Дефолты = исторические константы выше.
_back_var: ContextVar[int] = ContextVar("ir_cal_back", default=_WINDOW_BACK_DAYS)
_fwd_var: ContextVar[int] = ContextVar("ir_cal_fwd", default=_WINDOW_FWD_DAYS)


def _win_back() -> int:
    return _back_var.get()


def _win_fwd() -> int:
    return _fwd_var.get()


# #90: инструкция пользователя (сценарий «IR Календарь») — дополнительные
# указания LLM-экстрактору событий (какие компании/рынки/типы событий важны).
_instr_var: ContextVar[str] = ContextVar("ir_cal_instr", default="")

# Расход модели за прогон календаря (21.08.2026). До этого календарь звал
# `llm.complete`, который usage не возвращает, — стоимость прогона не
# записывалась НИГДЕ. На вопрос владельца «сколько стоит один календарь» я мог
# ответить только расчётом по токенам и тарифу, а не показанием журнала.
# Счётчик таск-скоупный, как и окно: параллельные прогоны не смешиваются.
_usage_var: ContextVar[dict] = ContextVar("ir_cal_usage")


def _usage() -> dict:
    try:
        return _usage_var.get()
    except LookupError:
        return {"in": 0, "out": 0, "cost_usd": 0.0, "calls": 0}

# Source precedence for choosing the displayed source_url / excerpt on a merged
# event (lower index = higher priority). First-party disclosure outranks the
# aggregators/APIs; ir_site (the company's own page) is the most authoritative link.
_SOURCE_PRECEDENCE = ["ir_site", "edisclosure", "finnhub", "fmp", "smartlab"]

# `region` values come from the xlsx as full words ("US", "China", "Russia", ...).
_RU_REGION = "Russia"


@dataclasses.dataclass
class CalendarCompany:
    region: str
    company: str
    sub_group: str
    ir_url: str


@dataclasses.dataclass
class CalendarEvent:
    date: str  # ISO YYYY-MM-DD
    event_type: str
    company: str
    region: str
    sub_group: str
    source_url: str
    raw_excerpt: str = ""
    when: str = "upcoming"  # "upcoming" (date >= today) | "recent" (date < today)
    sources: list[str] = dataclasses.field(default_factory=list)  # e.g. ["ir_site","finnhub"]

    def to_dict(self) -> dict:
        d = dataclasses.asdict(self)
        # #95: пользовательская группа события (3 шт по фидбеку потребителей):
        # earnings → отчётность, investor_day → день инвестора, остальное → прочее.
        d["group"] = EVENT_GROUP_BY_TYPE.get(self.event_type, "other")
        return d


# #95: маппинг движковых типов в 3 пользовательские группы (фильтр/ручка).
EVENT_GROUP_BY_TYPE = {"earnings": "reporting", "investor_day": "investor_day"}
EVENT_GROUP_LABELS = {"reporting": "Отчётность", "investor_day": "День инвестора",
                      "other": "Прочее"}


def _spec_path() -> Path:
    """Path to the bundled calendar xlsx (sibling of this module)."""
    p = Path(__file__).resolve().parent / "ir_data" / "calendar_list.xlsx"
    if not p.exists():
        raise FileNotFoundError(f"calendar_list.xlsx not found at {p}")
    return p


def load_companies(limit: int | None = None) -> list[CalendarCompany]:
    """Read the spec xlsx into a list of (region, company, sub_group, url)."""
    import openpyxl  # type: ignore[import-untyped]

    wb = openpyxl.load_workbook(_spec_path(), data_only=True)
    ws = wb.active
    out: list[CalendarCompany] = []
    header_seen = False
    for row in ws.iter_rows(values_only=True):
        if not row or not any(row):
            continue
        first = str(row[0] or "").strip()
        if not header_seen:
            if first == "Region":
                header_seen = True
            continue
        # Header may be followed by group-separator blank lines; skip them.
        if not first:
            continue
        company = str(row[1] or "").strip() if len(row) > 1 else ""
        sub_group = str(row[2] or "").strip() if len(row) > 2 else ""
        url = str(row[3] or "").strip() if len(row) > 3 else ""
        if not company or not url:
            continue
        out.append(CalendarCompany(region=first, company=company, sub_group=sub_group, ir_url=url))
        if limit and len(out) >= limit:
            break
    return out


_HTML_TAGS_RE = re.compile(r"<script[^>]*>.*?</script>|<style[^>]*>.*?</style>", re.S | re.I)
_HTML_TAG_RE = re.compile(r"<[^>]+>")
_WHITESPACE_RE = re.compile(r"[ \t]+")
_BLANK_LINES_RE = re.compile(r"\n{3,}")


def _html_to_text(html: str) -> str:
    """Bare-bones HTML stripper — good enough for the LLM to read most IR
    pages. We don't pull in BeautifulSoup just for this."""
    s = _HTML_TAGS_RE.sub(" ", html)
    s = _HTML_TAG_RE.sub(" ", s)
    s = _WHITESPACE_RE.sub(" ", s)
    s = _BLANK_LINES_RE.sub("\n\n", s)
    return s.strip()


def _window(today: date) -> tuple[str, str]:
    """ISO bounds of the ±window: (back, forward)."""
    return (
        (today - timedelta(days=_win_back())).isoformat(),
        (today + timedelta(days=_win_fwd())).isoformat(),
    )


def _norm(name: str) -> str:
    return name.strip().lower()


# ───────────────────────── fetch (IR page) ─────────────────────────


async def _fetch_ir_page(client: httpx.AsyncClient, url: str) -> tuple[str, str]:
    """Fetch IR URL → (plain-text body capped, method).

    `method` ∈ {"playwright", "httpx", "none"}. Render with headless Chromium
    FIRST — most IR pages are JS-SPAs or bot-protected (Akamai/q4cdn) and hand
    plain httpx an empty shell or a 403 (proven for Apple/Netflix/Meta/Nvidia).
    `render_page` is sync and binds Playwright greenlets to its own thread, so it
    MUST be driven via asyncio.to_thread. Falls back to httpx for static-HTML IR
    pages. ("", "none") when both fail.
    """
    from finengine.skills.news.connectors.playwright_base import render_page

    try:
        html = await asyncio.to_thread(
            render_page, url, timeout_s=30.0,
            wait_until="domcontentloaded", post_wait_ms=2000,
        )
    except Exception as e:  # noqa: BLE001 — browser/thread crash
        log.warning("IR render crashed %s: %s", url, e)
        html = None
    if html:
        text = _html_to_text(html)
        if len(text) >= _MIN_USEFUL_CHARS:
            return text[:_MAX_PAGE_CHARS], "playwright"

    try:
        resp = await client.get(url, follow_redirects=True, timeout=_HTTP_TIMEOUT)
        resp.raise_for_status()
        text = _html_to_text(resp.text)
        if text:
            return text[:_MAX_PAGE_CHARS], "httpx"
        return "", "none"
    except (httpx.HTTPError, httpx.TimeoutException) as e:
        log.warning("IR fetch failed %s: %s", url, e)
        return "", "none"


# ───────────────────────── LLM extraction ─────────────────────────


def _build_extraction_prompt(company: CalendarCompany, page_text: str, today: date) -> str:
    """Ask the LLM for MATERIAL corporate events in the ±window — both already
    published (recent) and scheduled (upcoming) — as strict JSON."""
    back, fwd = _window(today)
    return (
        f"Сегодня: {today.isoformat()}.\n"
        f"Окно: с {back} по {fwd} включительно "
        f"(последние {_win_back()} дней + ближайшие {_win_fwd()} дней).\n\n"
        f"Компания: {company.company} (регион: {company.region}, сектор: {company.sub_group}).\n"
        f"Источник: {company.ir_url}\n\n"
        f"Содержимое страницы (текстовая выдержка):\n---\n{page_text}\n---\n\n"
        + (f"Дополнительные указания пользователя (обязательно учитывай):\n"
           f"{_instr_var.get()}\n\n" if _instr_var.get() else "")
        + "Извлеки МАТЕРИАЛЬНЫЕ корпоративные события в этом окне — двух видов:\n"
        "  • УЖЕ ОПУБЛИКОВАННЫЕ (вышел отчёт/пресс-релиз, объявлены дивиденды, "
        "M&A, прошедшее собрание) — это важно, чтобы не пропустить новость;\n"
        "  • ЗАПЛАНИРОВАННЫЕ на будущее (анонс даты отчёта, день инвестора, ОСА, "
        "дата отсечки/выплаты).\n"
        "НЕ включай маркетинговые/продуктовые новости без финансовой значимости.\n\n"
        "Типы (event_type): 'earnings' (квартальный/годовой отчёт), 'investor_day', "
        "'agm' (общее собрание акционеров), 'dividend' (отсечка/выплата), "
        "'press_release' (материальный пресс-релиз), 'other'.\n"
        "Поле when: 'recent' если дата <= сегодня (уже произошло), иначе 'upcoming'.\n\n"
        "Верни СТРОГО JSON-массив без markdown-fences, без комментариев. Формат:\n"
        '[{"date":"YYYY-MM-DD","event_type":"<тип>","when":"upcoming|recent",'
        '"title":"<кратко на русском>"}]\n\n'
        "Если событий нет — верни []."
    )


def _parse_events_json(
    raw: str, company: CalendarCompany, today: date, source_id: str
) -> list[CalendarEvent]:
    """LLM response → list[CalendarEvent], filtered to the ±window and tagged
    with `when` + `sources=[source_id]`. Tolerant of markdown fences."""
    s = raw.strip()
    if s.startswith("```"):
        s = s.split("```", 2)[1] if s.count("```") >= 2 else s.lstrip("`")
        if s.lstrip().startswith("json"):
            s = s.lstrip()[4:]
    i = s.find("[")
    j = s.rfind("]")
    if i < 0 or j < 0 or j <= i:
        return []
    try:
        items = json.loads(s[i : j + 1])
    except json.JSONDecodeError:
        log.warning("Could not parse calendar JSON for %s: %s", company.company, s[:200])
        return []

    back, fwd = _window(today)
    today_iso = today.isoformat()
    out: list[CalendarEvent] = []
    for it in items:
        if not isinstance(it, dict):
            continue
        d = str(it.get("date") or "").strip()[:10]
        if not d or d < back or d > fwd:  # drop out-of-window / undated
            continue
        et = str(it.get("event_type") or "other").strip().lower()
        title = str(it.get("title") or "").strip()
        when = str(it.get("when") or "").strip().lower()
        if when not in ("upcoming", "recent"):
            when = "recent" if d <= today_iso else "upcoming"
        out.append(
            CalendarEvent(
                date=d, event_type=et, company=company.company,
                region=company.region, sub_group=company.sub_group,
                source_url=company.ir_url, raw_excerpt=title,
                when=when, sources=[source_id],
            )
        )
    return out


async def _llm_extract(
    company: CalendarCompany, page_text: str, today: date, source_id: str
) -> list[CalendarEvent] | None:
    """Run one extraction LLM call over `page_text`. Returns parsed events, or
    None on LLM error (so the caller can distinguish 'no events' from 'failed')."""
    prompt = _build_extraction_prompt(company, page_text, today)
    try:
        resp, _u = await llm.complete_with_usage(
            prompt=prompt,
            system="Ты помощник IR-команды. Отвечай строго в требуемом формате JSON.",
            # 6000, а не 1500. Модель бэкенда рассуждающая, и токены
            # размышлений идут в тот же счёт, что и ответ. Живой замер 18.08:
            # «finish_reason=length; размышлений 12508 знаков» на странице
            # Магнита и 12621 у МТС Банка — обе компании попали в
            # `failed_companies`, то есть выглядели как «не удалось прочитать
            # сайт», хотя страница читалась, а кончался бюджет вывода.
            #
            # ⚠️ ЧЕСТНАЯ ОГОВОРКА: подъём закрывает ИМЕННО переполнение
            # размышлениями. Одиночные прогоны показали и разброс, не
            # связанный с бюджетом (та же страница то отдаёт 4 события, то
            # пустой ответ) — повторный замер на пяти прогонах не уложился в
            # отведённое время и остался несделанным. Неиспользованный бюджет
            # не оплачивается, вреда от подъёма нет, но разброс не объяснён.
            max_output=6000,
            temperature=0.0,
        )
    except Exception as e:  # noqa: BLE001 — network/API
        log.warning("LLM extraction failed for %s (%s): %s", company.company, source_id, e)
        return None
    # Цену считаем ТЕМ ЖЕ калькулятором, что и дайджест (`llm.pricing`):
    # прокси по этому пути `cost_usd` не возвращает вовсе (замер 21.08 — ноль
    # при 12 101 входящем токене), и без расчёта в журнал попал бы ноль. Один
    # способ на оба продукта — иначе их цены нельзя будет сравнивать.
    from finengine.llm.pricing import cost_usd as _cost
    acc = _usage()
    acc["in"] += int(_u.get("in", 0) or 0)
    acc["out"] += int(_u.get("out", 0) or 0)
    acc["cost_usd"] += float(_u.get("cost_usd") or 0.0) or _cost(
        {"model": _u.get("model") or settings.openrouter_model,
         "in": _u.get("in", 0), "out": _u.get("out", 0),
         "cached": _u.get("cached", 0)})
    acc["calls"] += 1
    _usage_var.set(acc)
    return _parse_events_json(resp, company, today, source_id)


# ───────────────────────── source: ir_site ─────────────────────────


async def _ir_site_company(
    client: httpx.AsyncClient, company: CalendarCompany, today: date, sem: asyncio.Semaphore
) -> tuple[list[CalendarEvent], str]:
    """Per-company `ir_site` source: render the IR page + LLM-extract events.
    Returns (events, status) where status ∈ {"playwright","httpx","none","llm_error"}
    for honest coverage diagnostics. Never raises."""
    async with sem:
        page, method = await _fetch_ir_page(client, company.ir_url)
        if not page:
            return [], "none"
        events = await _llm_extract(company, page, today, "ir_site")
        if events is None:
            return [], "llm_error"
        return events, method


# ───────────────────────── batch sources (stubs → Tasks 4-6) ─────────────────────────


# Foreign company name (as in calendar_list.xlsx) → US/ADR ticker. Only names
# that trade on US exchanges (incl. ADRs) — those appear in Finnhub's US earnings
# calendar in one call. Native-listed names (HK/EU/Tokyo/Korea: Tencent, Rakuten,
# DeliveryHero, …) are intentionally omitted (Finnhub free doesn't cover them) and
# stay covered by ir_site.
_FINNHUB_TICKERS: dict[str, str] = {
    "JP Morgan": "JPM", "Wells Fargo": "WFC", "Citigroup": "C",
    "Goldman Sachs": "GS", "Morgan Stanley": "MS", "BoFA": "BAC",
    "Amazon": "AMZN", "Instacart": "CART", "Shopify": "SHOP",
    "Alibaba": "BABA", "JD.Com": "JD", "MercadoLibre": "MELI",
    "Tesla": "TSLA", "Meta (Facebook)": "META", "IBM": "IBM",
    "Microsoft": "MSFT", "Google": "GOOGL", "Apple": "AAPL",
    "Nvidia": "NVDA", "Lyft": "LYFT", "Baidu": "BIDU",
    "Block (Square)": "XYZ", "Affirm": "AFRM", "PayPal": "PYPL",
    "PagSeguro": "PAGS", "Walmart": "WMT", "Netflix": "NFLX",
    "Walt Disney": "DIS", "Spotify": "SPOT", "Sea (+Shopee)": "SE",
    "DoorDash": "DASH", "Uber": "UBER", "Grab": "GRAB",
    "Broadcom": "AVGO", "TSMC": "TSM", "AMD": "AMD",
    "Pony AI": "PONY", "Weride": "WRD", "Kaspi": "KSPI",
}

_FINNHUB_URL = "https://finnhub.io/api/v1/calendar/earnings"


async def _source_finnhub(companies: list[CalendarCompany], today: date) -> list[CalendarEvent]:
    """Finnhub earnings calendar — upcoming/recent earnings dates for FOREIGN
    companies. Queries PER TICKER: the no-symbol "all" endpoint is unreliable on
    the free tier (returns a capped slice that misses our mega-caps), whereas the
    per-symbol query is exact. ~39 calls, concurrent under a small semaphore (free
    tier = 60 req/min). Graceful: no key / no mapped tickers / per-ticker error →
    skipped. No RU/MOEX coverage by design. httpx honours HTTPS_PROXY."""
    key = settings.finnhub_api_key
    if not key:
        return []
    by_ticker: dict[str, CalendarCompany] = {}
    for c in companies:
        t = _FINNHUB_TICKERS.get(c.company.strip())
        if t:
            by_ticker[t] = c
    if not by_ticker:
        return []

    back, fwd = _window(today)
    today_iso = today.isoformat()
    sem = asyncio.Semaphore(8)  # stay well under Finnhub free 60 req/min

    async def _one(client: httpx.AsyncClient, ticker: str, c: CalendarCompany) -> list[CalendarEvent]:
        async with sem:
            try:
                resp = await client.get(
                    _FINNHUB_URL,
                    params={"from": back, "to": fwd, "symbol": ticker, "token": key},
                )
                resp.raise_for_status()
                rows = (resp.json() or {}).get("earningsCalendar") or []
            except Exception as e:  # noqa: BLE001 — per-ticker; never kill the batch
                log.warning("finnhub %s failed: %s", ticker, e)
                return []
        evs: list[CalendarEvent] = []
        for row in rows:
            d = str(row.get("date") or "").strip()[:10]
            if not d or d < back or d > fwd:
                continue
            q, y = row.get("quarter"), row.get("year")
            # Имя поставщика в подписи события не нужно: он и так едет в
            # `sources`, а на экране рядом стоит колонка «Источник» со ссылкой.
            # Владелец 17.08 на строку «Отчётность (Q2 2026) — Finnhub» спросил
            # «что это за отчётность и чьей компании» — компания стоит в своей
            # колонке, а вот «— Finnhub» занимал место осмысленной подписи.
            # Своего заголовка Finnhub не отдаёт вовсе: в бесплатном тарифе это
            # тикер, дата и квартал, поэтому подпись собирается кодом.
            excerpt = (f"Отчётность за Q{q} {y} г." if q and y
                       else "Отчётность (дата подтверждена)")
            evs.append(CalendarEvent(
                date=d, event_type="earnings", company=c.company, region=c.region,
                sub_group=c.sub_group, source_url=c.ir_url, raw_excerpt=excerpt,
                when=("recent" if d <= today_iso else "upcoming"), sources=["finnhub"],
            ))
        return evs

    async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
        batches = await asyncio.gather(*[_one(client, t, c) for t, c in by_ticker.items()])
    out: list[CalendarEvent] = []
    for b in batches:
        out.extend(b)
    return out


_BROWSER_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
_SMARTLAB_URL = "https://smart-lab.ru/calendar/stocks/"

# RU calendar company name → MOEX ticker (smart-lab keys rows by ticker, e.g.
# "MGNT: закрытие реестра по дивидендам"). Unmapped names just don't match.
_SMARTLAB_TICKERS: dict[str, str] = {
    "ВТБ": "VTBR", "Т-Технологии": "T", "Совкомбанк": "SVCB", "МТС Банк": "MBNK",
    "МТС": "MTSS", "Ozon": "OZON", "Yandex": "YDEX", "VK": "VKCO", "Positive": "POSI",
    "X5 Retail (ops)": "X5", "X5 Retail (fin)": "X5", "Lenta (ops)": "LENT",
    "Lenta (fin)": "LENT", "Magnit": "MGNT",
}

_SL_ROW_RE = re.compile(r"<tr.*?</tr>", re.S)
_SL_CELL_RE = re.compile(r"<td[^>]*>(.*?)</td>", re.S)
_SL_TABLE_RE = re.compile(r"<table.*?</table>", re.S)
_SL_DESC_RE = re.compile(r"([A-Z0-9]{2,6})\s*:\s*(.+)")


async def _source_smartlab(companies: list[CalendarCompany], today: date) -> list[CalendarEvent]:
    """smart-lab.ru RU corporate calendar — the main FREE source of RU FORWARD
    dates (earnings/dividend/AGM), which press-release IR pages and e-disclosure
    (recent) don't give. HTML scrape (plain httpx, no Cloudflare); rows are
    `Дата | Страна | TICKER: описание`. We match by MOEX ticker. Aggregator =
    lowest precedence (cross-check); graceful → [] on any failure."""
    by_ticker: dict[str, CalendarCompany] = {}
    for c in companies:
        t = _SMARTLAB_TICKERS.get(c.company.strip())
        if t:
            by_ticker[t] = c
    if not by_ticker:
        return []
    try:
        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT, headers={"User-Agent": _BROWSER_UA}) as client:
            resp = await client.get(_SMARTLAB_URL, follow_redirects=True)
            resp.raise_for_status()
            html = resp.text
    except Exception as e:  # noqa: BLE001 — network; must not kill the build
        log.warning("smartlab fetch failed: %s", e)
        return []

    tables = _SL_TABLE_RE.findall(html)
    if len(tables) < 2:  # [0] = filter form, [1] = events
        return []
    back, fwd = _window(today)
    today_iso = today.isoformat()
    out: list[CalendarEvent] = []
    for row in _SL_ROW_RE.findall(tables[1]):
        cells = [
            re.sub(r"\s+", " ", _HTML_TAG_RE.sub(" ", c)).strip()
            for c in _SL_CELL_RE.findall(row)
        ]
        if len(cells) < 3:
            continue
        dm = re.match(r"(\d{2})\.(\d{2})\.(\d{4})", cells[0])
        if not dm:
            continue
        d = f"{dm.group(3)}-{dm.group(2)}-{dm.group(1)}"
        if d < back or d > fwd:
            continue
        tm = _SL_DESC_RE.match(cells[2])
        if not tm:
            continue
        c = by_ticker.get(tm.group(1))
        if not c:
            continue
        text = tm.group(2).strip()
        out.append(CalendarEvent(
            date=d, event_type=_classify_edisclosure(text), company=c.company,
            region=c.region, sub_group=c.sub_group, source_url=c.ir_url,
            raw_excerpt=text[:200], when=("recent" if d <= today_iso else "upcoming"),
            sources=["smartlab"],
        ))
    return out


# RU calendar company name (as in calendar_list.xlsx) → e-disclosure.ru portal id
# (from news/companies.yaml). e-disclosure is the official, ЦБ-accredited
# first-party disclosure — most authoritative free RU source. Т-Технологии has
# no id here → falls back to ir_site.
_EDISCLOSURE_IDS: dict[str, int] = {
    "ВТБ": 1210, "Совкомбанк": 30052, "МТС Банк": 1285, "МТС": 236,
    "Ozon": 38334, "Yandex": 34514, "VK": 38965, "Positive": 38196,
    "X5 Retail (ops)": 39008, "X5 Retail (fin)": 39008,
    "Lenta (ops)": 38380, "Lenta (fin)": 38380, "Magnit": 17783,
}


def _classify_edisclosure(title: str) -> str:
    """Map an e-disclosure fact category (Russian) → our event_type."""
    t = title.lower()
    if "дивиденд" in t:
        return "dividend"
    if "собрани" in t or "акционер" in t or "оса" in t or "госа" in t:
        return "agm"
    if "отчётност" in t or "отчетност" in t or "мсфо" in t or "рсбу" in t or "результат" in t:
        return "earnings"
    return "press_release"  # material fact ≈ press release


async def _edisclosure_company(company: CalendarCompany, today: date) -> list[CalendarEvent]:
    """RU `edisclosure` source: pull recent material facts (e-disclosure.ru) in the
    back-window and emit them as `recent` events — the authoritative way to catch
    just-published RU disclosures the agent would otherwise miss. Reuses the news
    EDisclosureConnector (Playwright render + row parse). Returns [] if no id /
    render fails. (Forward dates buried in fact TEXT are a future refinement —
    here we use the disclosure category + publication date.)"""
    eid = _EDISCLOSURE_IDS.get(company.company.strip())
    if eid is None:
        return []
    from finengine.skills.news.config import SourceConfig
    from finengine.skills.news.connectors.edisclosure import EDisclosureConnector

    src = SourceConfig(
        id=f"edisclosure_{eid}", display_name=f"{company.company} (e-disclosure)",
        type="official", region="RU", connector="edisclosure",
        url=f"https://e-disclosure.ru/portal/company.aspx?id={eid}", weight=1.0,
    )
    conn = EDisclosureConnector(src)
    try:
        items = await asyncio.to_thread(conn.fetch, since_hours=_win_back() * 24, limit=30)
    except Exception as e:  # noqa: BLE001 — render/parse; must not kill the build
        log.warning("edisclosure %s failed: %s", company.company, e)
        return []

    back, fwd = _window(today)
    today_iso = today.isoformat()
    out: list[CalendarEvent] = []
    for it in items:
        d = it.published_at.date().isoformat()
        if d < back or d > fwd:
            continue
        out.append(CalendarEvent(
            date=d, event_type=_classify_edisclosure(it.title), company=company.company,
            region=company.region, sub_group=company.sub_group, source_url=it.url,
            raw_excerpt=it.title[:200],
            when=("recent" if d <= today_iso else "upcoming"), sources=["edisclosure"],
        ))
    return out


@dataclasses.dataclass
class _Source:
    id: str
    applies: Callable[[CalendarCompany], bool]
    fetch: Callable[..., object]  # async (companies, today) -> list[CalendarEvent]


# Batch sources run ONCE per calendar build and index events by company. The
# `applies` predicate is the region half of the priority matrix (finnhub → foreign,
# smartlab → RU). Per-company sources (ir_site, edisclosure) run in the company loop.
_BATCH_SOURCES: list[_Source] = [
    _Source("finnhub", lambda c: c.region != _RU_REGION, _source_finnhub),
    _Source("smartlab", lambda c: c.region == _RU_REGION, _source_smartlab),
]


# ───────────────────────── merge + confidence ─────────────────────────


def _precedence(ev: CalendarEvent) -> int:
    """Best (lowest) precedence index among the event's sources."""
    idxs = [_SOURCE_PRECEDENCE.index(s) for s in ev.sources if s in _SOURCE_PRECEDENCE]
    return min(idxs) if idxs else len(_SOURCE_PRECEDENCE)


def _merge_events(events: list[CalendarEvent]) -> list[CalendarEvent]:
    """Dedup by (company, date, event_type). The highest-precedence event becomes
    primary (keeps its source_url / excerpt / when); `sources` is the union across
    all agreeing events → confidence = len(sources)."""
    groups: dict[tuple, list[CalendarEvent]] = defaultdict(list)
    for e in events:
        groups[(_norm(e.company), e.date, e.event_type)].append(e)
    merged: list[CalendarEvent] = []
    for evs in groups.values():
        evs.sort(key=_precedence)  # best precedence first
        primary = evs[0]
        union: list[str] = []
        for e in evs:
            for s in e.sources:
                if s not in union:
                    union.append(s)
        primary.sources = union
        merged.append(primary)
    merged.sort(key=lambda e: (e.date, e.region, e.company))
    return merged


# ───────────────────────── orchestration ─────────────────────────


async def build_calendar_stream(
    limit: int | None = None, names: list[str] | None = None,
    back_days: int | None = None, fwd_days: int | None = None,
    instructions: str = "",
) -> AsyncIterator[dict]:
    """Stream merged events as companies complete. Yields dicts:
      - {"type": "started", "companies": int, "window_back": int, "window_fwd": int}
      - {"type": "source_done", "source": str, "events": int}      (batch sources)
      - {"type": "company_done", "company": str, "done": int, "total": int,
         "found": int}                                    (ход обхода, всегда)
      - {"type": "events", "company": str, "events": [CalendarEvent.to_dict(), ...]}
      - {"type": "done", "total_events", "total_companies", "by_source": {...},
         "multi_source": int, "fetch_failed": int, "failed_companies": [str,...]}

    Flow: (A) run batch sources once, index by company; (B) per-company run ir_site
    (+ edisclosure for RU), merge with the batch events for that company, emit.
    Coverage stays honest via `fetch_failed`/`failed_companies` (ir_site).
    """
    today = date.today()
    # #87: окно per-запуск (клэмп 1..120) — таск-скоуп, см. _back_var выше.
    # Ставим ВСЕГДА (дефолты явно): без этого второй прогон в том же таске
    # наследовал окно первого (поймано тестом на dev).
    _back_var.set(max(1, min(120, int(back_days))) if back_days else _WINDOW_BACK_DAYS)
    _fwd_var.set(max(1, min(120, int(fwd_days))) if fwd_days else _WINDOW_FWD_DAYS)
    _instr_var.set((instructions or "").strip()[:2000])  # тоже всегда (см. выше)
    _usage_var.set({"in": 0, "out": 0, "cost_usd": 0.0, "calls": 0})
    companies = load_companies(limit=None if names else limit)
    if names:  # targeted scan (chat tool: "когда отчитывается X / события по X")
        wanted = [n.strip().lower() for n in names if n and n.strip()]
        companies = [
            c for c in companies
            if any(w in c.company.lower() or c.company.lower() in w for w in wanted)
        ]
    yield {
        "type": "started", "companies": len(companies),
        "window_back": _win_back(), "window_fwd": _win_fwd(),
    }

    # ── Phase A: batch sources (finnhub, smartlab) — one fetch each, index by company.
    batch_by_company: dict[str, list[CalendarEvent]] = defaultdict(list)
    by_source: dict[str, int] = defaultdict(int)
    for src in _BATCH_SOURCES:
        applicable = [c for c in companies if src.applies(c)]
        if not applicable:
            continue
        try:
            evs = await src.fetch(applicable, today)
        except Exception as e:  # noqa: BLE001 — a source must never kill the build
            log.warning("batch source %s failed: %s", src.id, e)
            evs = []
        for e in evs:
            batch_by_company[_norm(e.company)].append(e)
        by_source[src.id] += len(evs)
        yield {"type": "source_done", "source": src.id, "events": len(evs)}

    # ── Phase B: per-company (ir_site always; edisclosure for RU), merge, stream.
    sem = asyncio.Semaphore(_MAX_CONCURRENT_FETCHES)
    total = 0
    multi_source = 0
    counts = {"playwright": 0, "httpx": 0, "none": 0, "llm_error": 0}
    failed_names: list[str] = []
    done_n = 0

    async def _per_company(client, c: CalendarCompany):
        evs, status = await _ir_site_company(client, c, today, sem)
        if c.region == _RU_REGION:
            try:
                evs = evs + await _edisclosure_company(c, today)
            except Exception as e:  # noqa: BLE001
                log.warning("edisclosure %s failed: %s", c.company, e)
        return c, evs, status

    async with httpx.AsyncClient(headers={"User-Agent": "FA-CLAW/IRCalendar 0.2"}) as client:
        tasks = [asyncio.create_task(_per_company(client, c)) for c in companies]
        for c, fut in zip(companies, tasks):
            try:
                _c, per_evs, status = await fut
            except Exception as e:  # noqa: BLE001
                log.warning("processing %s failed: %s", c.company, e)
                per_evs, status = [], "none"
            counts[status] = counts.get(status, 0) + 1
            for e in per_evs:
                for s in e.sources:
                    by_source[s] += 1
            merged = _merge_events(per_evs + batch_by_company.get(_norm(c.company), []))
            # 🔴 ОТМЕТКА О КАЖДОЙ КОМПАНИИ, А НЕ ТОЛЬКО О НАЙДЕННОЙ (25.08.2026).
            # Событие `events` шлётся ниже лишь при `if merged`, и на узком окне
            # («собери календарь за 1 день») событий почти ни у кого нет: чат
            # получал «Сканирую 89 компаний…» и замолкал на двадцать минут.
            # Владелец написал «поставил, не увидел ответа» — работа шла, просто
            # её не было видно. Ход обхода — отдельное событие, независимое от
            # улова.
            done_n += 1
            yield {"type": "company_done", "company": c.company,
                   "done": done_n, "total": len(companies),
                   "found": len(merged)}
            if merged:
                total += len(merged)
                multi_source += sum(1 for e in merged if len(e.sources) >= 2)
                yield {
                    "type": "events",
                    "company": c.company,
                    "events": [e.to_dict() for e in merged],
                }
            elif status in ("none", "llm_error"):
                # No event from ANY source AND the IR-page fetch errored → a real
                # coverage gap (≠ "fetched OK but nothing scheduled in the window").
                # A company whose ir_site failed but a batch source (e.g. Finnhub)
                # still covered ends up in `merged` above, so it's NOT counted here.
                failed_names.append(c.company)

    yield {
        "type": "done",
        "total_events": total,
        "total_companies": len(companies),
        "by_source": dict(by_source),
        "multi_source": multi_source,
        "fetch_failed": len(failed_names),  # no data from ANY source + fetch errored
        "failed_companies": failed_names[:20],
        # Расход прогона: без него «сколько стоит календарь» отвечается
        # расчётом по тарифу, а не журналом.
        "usage": _usage(),
    }
