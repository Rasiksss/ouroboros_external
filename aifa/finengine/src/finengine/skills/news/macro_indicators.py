"""Macro-indicator dashboard for the Daily News digest.

Per ТЗ заказчика (`агент Daily.xlsx`): 11 indicators across РФ/США/
еврозона/Китай with M/M, Q/Q, Y/Y dynamics. Multi-source:

- **FRED** (St. Louis Fed) for US and what Eurostat-derived series FRED
  keeps fresh (US: full coverage; EU: HICP + GDP; CN: CPI).
- **Eurostat direct API** for EU unemployment / PPI / industrial
  production — FRED's OECD-sourced rows go stale around 2023, Eurostat
  itself updates monthly.
- **ЦБ РФ scrape** for RU key rate + annual CPI — there's no public RU
  data feed in FRED any more, and Росстат's EMISS lacks a stable JSON
  API. CBR's `/hd_base/infl/` table is small, public, and updates
  with each MPC meeting (key rate) + Росстат monthly print (CPI).

All three are free, only FRED needs a key. Each `IndicatorSpec` declares
its source via `provider`; `fetch_macro_dashboard` fans out per-source.
Cached on disk for 6h since most series release at most daily.
"""
from __future__ import annotations

import asyncio
import dataclasses
import json
import logging
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

import httpx

from finengine.config import settings

log = logging.getLogger(__name__)

_FRED_BASE = "https://api.stlouisfed.org/fred/series/observations"
_CACHE_PATH = Path(settings.data_dir) / "news" / "macro_cache.json"
# Per-provider freshness window: each row in the cache carries its own
# `fetched_at`; we only re-fetch when older than the provider's TTL.
# This is critical for Investing, where CF rate-limits after ~10 rapid
# calls — by spreading refreshes naturally we eventually populate all
# rows across multiple cycles instead of clobbering yesterday's good
# values with today's 429s.
_PROVIDER_TTL_S: dict[str, int] = {
    "fred":       6 * 3600,
    "eurostat":   6 * 3600,
    "cbr_scrape": 12 * 3600,
    "eia":        6 * 3600,
    "nbs":        12 * 3600,
    # 24h for Investing: month/quarter releases don't change intraday,
    # and the long TTL spreads the load across days. A single refresh
    # then hits Investing for at most the few rows whose monthly release
    # day has actually passed since the last successful fetch.
    "investing":  24 * 3600,
}
_CACHE_TTL_S = 6 * 3600  # legacy aggregate TTL (kept for telemetry)


# ─── Indicator registry ──────────────────────────────────────────────────
# Each indicator gets:
#   id         — short stable key for caching / display
#   ru_name    — Russian display name (matches ТЗ wording)
#   region     — RU/US/EU/CN/global (matches digest region buckets)
#   series_id  — FRED series identifier
#   unit       — display unit ("%", "тыс", "млн б.") или база индекса
#                («индекс 2021=100»). ПУСТОЙ БЫТЬ НЕ ДОЛЖЕН: 17.08 владелец
#                прочитал «PPI 284.1» как инфляцию в 284 процента — у ряда без
#                единицы число в таблице читается как проценты. Базу берём СО
#                СТРАНИЦЫ ПОСТАВЩИКА, а не по памяти: HICP оказался 2025=100
#                (Eurostat перебазировал), хотя «очевидным» выглядел 2015=100.
#   freq       — "M" (monthly), "Q" (quarterly), "W" (weekly), "D" (daily)
#   compares   — list of dynamics labels we want: "mom", "qoq", "yoy", "wow"
#   inv_sign   — if True, decrease is "good"; for crude inventory display
#                we don't want to label a drawdown as bullish/bearish, just
#                show change. Reserved for future formatting.

@dataclasses.dataclass(frozen=True)
class IndicatorSpec:
    id: str
    ru_name: str
    region: str
    series_id: str
    unit: str
    freq: str  # M | Q | W
    compares: tuple[str, ...]
    # Source plugin. "fred" = FRED v1 series/observations (needs API key);
    # "eurostat" = Eurostat REST JSON-stat (no key); "cbr_scrape" = ЦБ РФ
    # /hd_base/infl/ HTML table (no key). Adding a new source = adding
    # a fetch function below and a branch in `_dispatch_fetch`.
    provider: str = "fred"
    # For Eurostat: extra query params merged into the request URL.
    # For cbr_scrape: column index (0=key rate, 1=annual CPI).
    extra: dict | None = None


_REGISTRY: list[IndicatorSpec] = [
    # ─── United States ───────────────────────────────────────────────
    IndicatorSpec("us_gdp", "ВВП США (real)", "US", "GDPC1",
                  "млрд $ (SAAR)", "Q", ("qoq", "yoy")),
    IndicatorSpec("us_cpi", "Инфляция США (CPI)", "US", "CPIAUCSL",
                  "индекс 1982-84=100", "M", ("mom", "yoy")),
    IndicatorSpec("us_core_cpi", "Базовая инфляция США (Core CPI)",
                  "US", "CPILFESL", "индекс 1982-84=100", "M", ("mom", "yoy")),
    IndicatorSpec("us_ppi", "Промышленная инфляция США (PPI)",
                  "US", "PPIACO", "индекс 1982=100", "M", ("mom", "yoy")),
    IndicatorSpec("us_indpro", "Промпроизводство США", "US", "INDPRO",
                  "индекс 2017=100", "M", ("mom", "yoy")),
    IndicatorSpec("us_retail", "Розничные продажи США", "US", "RSAFS",
                  "млн $", "M", ("mom", "yoy")),
    IndicatorSpec("us_nfp", "Занятость США (NFP)", "US", "PAYEMS",
                  "тыс", "M", ("mom",)),  # NFP is reported as Δ-month
    IndicatorSpec("us_unrate", "Безработица США", "US", "UNRATE",
                  "%", "M", ("mom",)),
    IndicatorSpec("us_sent", "Consumer Sentiment (Univ. of Michigan)",
                  "US", "UMCSENT", "индекс 1966:Q1=100", "M", ("mom",)),
    # EIA crude inventory — weekly print, the EIA storage report. Free
    # API key required (see settings.eia_api_key).
    IndicatorSpec("us_oil_stock",
                  "Запасы нефти в США (EIA, exc. SPR)", "US",
                  "WCESTUS1", "тыс. барр.", "W", ("wow", "yoy"),
                  provider="eia",
                  extra={"route": "petroleum/stoc/wstk"}),

    # ─── Eurozone — FRED for HICP + GDP (kept fresh by Eurostat sync);
    # direct Eurostat for unemployment / PPI / IP which FRED's OECD
    # mirror stopped updating around 2023.
    IndicatorSpec("ez_cpi", "Инфляция Еврозоны (HICP)", "EU",
                  "CP0000EZ19M086NEST", "индекс 2025=100", "M", ("mom", "yoy")),
    IndicatorSpec("ez_gdp", "ВВП Еврозоны (real)", "EU",
                  "CLVMNACSCAB1GQEA19", "млн € (SAAR)", "Q",
                  ("qoq", "yoy")),
    IndicatorSpec("ez_unrate", "Безработица Еврозоны", "EU",
                  "une_rt_m", "%", "M", ("mom",),
                  provider="eurostat",
                  extra={"geo": "EA21", "age": "TOTAL", "sex": "T",
                         "unit": "PC_ACT", "s_adj": "SA"}),
    IndicatorSpec("ez_ppi", "Промышленная инфляция Еврозоны (PPI)",
                  "EU", "sts_inppd_m", "индекс 2021=100", "M", ("mom", "yoy"),
                  provider="eurostat",
                  extra={"geo": "EA21", "nace_r2": "B-D",
                         "indic_bt": "PRC_PRR_DOM", "unit": "I21"}),
    IndicatorSpec("ez_indpro", "Промпроизводство Еврозоны", "EU",
                  "sts_inpr_m", "индекс 2021=100", "M", ("mom", "yoy"),
                  provider="eurostat",
                  extra={"geo": "EA21", "nace_r2": "B-D",
                         "s_adj": "SCA", "unit": "I21"}),

    # ─── Russia — ЦБ РФ scrape (key rate + annual CPI). No public RU
    # data feed in FRED any more; Росстат doesn't expose a clean JSON.
    IndicatorSpec("ru_keyrate", "Ключевая ставка ЦБ РФ", "RU",
                  "infl_keyrate", "%", "M", ("mom",),
                  provider="cbr_scrape",
                  extra={"col": 0}),  # 0 = key rate column on infl page
    IndicatorSpec("ru_cpi", "Годовая инфляция РФ (CPI)", "RU",
                  "infl_cpi", "%", "M", ("mom",),
                  provider="cbr_scrape",
                  extra={"col": 1}),  # 1 = annual inflation column

    # ─── China — FRED CPI as fallback (OECD MEI, lags by ~1 year), plus
    # NBS Open Data for fresh PPI / industrial production. NBS values
    # for these series are already "same period last year = 100" indices
    # i.e. YoY ratio; we present as-is without further computation.
    IndicatorSpec("cn_cpi", "Инфляция Китая (CPI, OECD)", "CN",
                  "CHNCPIALLMINMEI", "индекс 2015=100", "M", ("mom", "yoy")),

    # ─── Investing.com — single-point scrape of historic-prints tables,
    # one HTTP call per indicator. Covers PMI in all 4 regions per ТЗ
    # plus RU/CN macro that no other free machine source provides.
    # Each value is the latest "actual" print; we compute M/M, Q/Q,
    # Y/Y as deltas from the next-row values like every other source.

    # PMI Manufacturing — IDs verified against Investing's live event
    # catalogue 2026-06-05. ISM for US, HCOB for EU (S&P took over from
    # Markit, then HCOB bank rebrand), RatingDog (formerly Caixin) for
    # China, S&P Global for Russia.
    #
    # Единица PMI — не база индекса, а порог: 50 разделяет рост и спад. Пустой
    # она быть не может по той же причине, что у PPI: «54.0» без единицы в
    # таблице читается как проценты.
    IndicatorSpec("us_pmi_man", "PMI промышленности США (ISM)",
                  "US", "ism_manuf", "индекс (50 = порог роста)", "M", ("mom",),
                  provider="investing",
                  extra={"path": "/economic-calendar/ism-manufacturing-pmi-173"}),
    IndicatorSpec("ez_pmi_man", "PMI промышленности Еврозоны (HCOB)",
                  "EU", "ez_manuf", "индекс (50 = порог роста)", "M", ("mom",),
                  provider="investing",
                  extra={"path": "/economic-calendar/manufacturing-pmi-201"}),
    IndicatorSpec("cn_pmi_man", "PMI промышленности Китая (RatingDog/Caixin)",
                  "CN", "cn_caixin_manuf", "индекс (50 = порог роста)", "M", ("mom",),
                  provider="investing",
                  extra={"path": "/economic-calendar/chinese-caixin-manufacturing-pmi-753"}),
    IndicatorSpec("cn_pmi_man_nbs", "PMI промышленности Китая (NBS)",
                  "CN", "cn_nbs_manuf", "индекс (50 = порог роста)", "M", ("mom",),
                  provider="investing",
                  extra={"path": "/economic-calendar/chinese-manufacturing-pmi-594"}),
    IndicatorSpec("ru_pmi_man", "PMI промышленности РФ (S&P)",
                  "RU", "ru_manuf", "индекс (50 = порог роста)", "M", ("mom",),
                  provider="investing",
                  extra={"path": "/economic-calendar/russian-s-p-global-manufacturing-pmi-1630"}),

    # PMI Services
    IndicatorSpec("us_pmi_srv", "PMI услуг США (ISM)",
                  "US", "ism_srv", "индекс (50 = порог роста)", "M", ("mom",),
                  provider="investing",
                  extra={"path": "/economic-calendar/ism-non-manufacturing-pmi-176"}),
    IndicatorSpec("ez_pmi_srv", "PMI услуг Еврозоны (HCOB)",
                  "EU", "ez_srv", "индекс (50 = порог роста)", "M", ("mom",),
                  provider="investing",
                  extra={"path": "/economic-calendar/services-pmi-272"}),
    IndicatorSpec("cn_pmi_srv", "PMI услуг Китая (RatingDog/Caixin)",
                  "CN", "cn_caixin_srv", "индекс (50 = порог роста)", "M", ("mom",),
                  provider="investing",
                  extra={"path": "/economic-calendar/chinese-caixin-services-pmi-596"}),
    IndicatorSpec("ru_pmi_srv", "PMI услуг РФ (S&P)",
                  "RU", "ru_srv", "индекс (50 = порог роста)", "M", ("mom",),
                  provider="investing",
                  extra={"path": "/economic-calendar/russian-s-p-global-services-pmi-1629"}),

    # Russia macro
    # ВВП РФ переведён с Investing на FRED (17.08.2026). Investing отдаёт 403
    # и напрямую, и через прокси — это их защита от датацентровых адресов, а не
    # наша сетевая фильтрация. Из 19 показателей, висевших на Investing, FRED
    # закрывает ТОЛЬКО этот: остальные их международные ряды заброшены
    # (промпроизводство КНР обрывается на 2023-11, ИЦП КНР на 2022-12,
    # промпроизводство РФ на 2021-12, ISM PMI убран по лицензии).
    #
    # Ряд отдаёт УРОВЕНЬ в млн рублей, а не процент, как было. Это не потеря:
    # проценты считает наш же код из самого ряда (`_delta` по `compares`),
    # поэтому берём и год к году, и квартал к кварталу — раньше был только
    # второй. Свежесть проверена: наблюдения до 2026-01, обновлён 2026-07-13.
    IndicatorSpec("ru_gdp_q", "ВВП РФ",
                  "RU", "NGDPRNSAXDCRUQ", "млн ₽", "Q", ("yoy",),
                  provider="fred"),
    IndicatorSpec("ru_indpro", "Промпроизводство РФ (Y/Y)",
                  "RU", "ru_ip_yoy", "%", "M", ("mom",),
                  provider="investing",
                  extra={"path": "/economic-calendar/russian-industrial-production-553"}),
    IndicatorSpec("ru_unrate", "Безработица РФ",
                  "RU", "ru_unrate", "%", "M", ("mom",),
                  provider="investing",
                  extra={"path": "/economic-calendar/russian-unemployment-rate-556"}),
    IndicatorSpec("ru_ppi", "ИЦП РФ (Y/Y)", "RU",
                  "ru_ppi_yoy", "%", "M", ("mom",),
                  provider="investing",
                  extra={"path": "/economic-calendar/russian-ppi-972"}),
    IndicatorSpec("ru_retail", "Розничные продажи РФ (Y/Y)",
                  "RU", "ru_retail_yoy", "%", "M", ("mom",),
                  provider="investing",
                  extra={"path": "/economic-calendar/russian-retail-sales-555"}),

    # China macro
    IndicatorSpec("cn_gdp", "ВВП Китая (Y/Y)", "CN",
                  "cn_gdp_yoy", "%", "Q", ("qoq",),
                  provider="investing",
                  extra={"path": "/economic-calendar/chinese-gdp-461"}),
    IndicatorSpec("cn_ppi", "ИЦП Китая (Y/Y)", "CN",
                  "cn_ppi_yoy", "%", "M", ("mom",),
                  provider="investing",
                  extra={"path": "/economic-calendar/chinese-ppi-464"}),
    IndicatorSpec("cn_indpro", "Промпроизводство Китая (Y/Y)", "CN",
                  "cn_ip_yoy", "%", "M", ("mom",),
                  provider="investing",
                  extra={"path": "/economic-calendar/chinese-industrial-production-462"}),
    IndicatorSpec("cn_retail", "Розничные продажи Китая (Y/Y)", "CN",
                  "cn_retail_yoy", "%", "M", ("mom",),
                  provider="investing",
                  extra={"path": "/economic-calendar/chinese-retail-sales-465"}),
    IndicatorSpec("cn_unrate", "Безработица Китая", "CN",
                  "cn_unrate", "%", "M", ("mom",),
                  provider="investing",
                  extra={"path": "/economic-calendar/chinese-unemployment-rate-1793"}),
]

# Реестр по id. Отрисовке нужны частота (см. `_period_cell`) и единица — при
# этом единица СВЕЖАЯ, а не та, что лежит в записи кэша: кэш живёт 6–24 часа, а
# строки Investing после 403 не обновляются вовсе, и без подстановки девять PMI
# остались бы с «Ед.» = «—» навсегда — правка в реестре до экрана бы не доехала.
# В САМУ ЗАПИСЬ при этом ничего не добавляем: `_read_cache` выбрасывает записи
# старой формы, и лишнее поле стоило бы суток пустых колонок на проде — ровно
# это поймали 17.08 на живой выкатке, когда в запись добавили `source_url`.
_SPEC_BY_ID: dict[str, IndicatorSpec] = {s.id: s for s in _REGISTRY}


# ─── HTTP + math ─────────────────────────────────────────────────────────


async def _fetch_fred(
    client: httpx.AsyncClient, spec: IndicatorSpec, *, limit: int = 14,
) -> list[dict]:
    """Latest `limit` observations for a FRED series, desc order.

    14 covers 1 year of monthly + buffer; 14 weekly = ~3 months which
    is enough for week-over-week and 12-week trends. Quarterly series
    get 5 quarters which spans Y/Y comparison."""
    params = {
        "series_id": spec.series_id,
        "api_key": settings.fred_api_key,
        "file_type": "json",
        "sort_order": "desc",
        "limit": limit,
    }
    try:
        r = await client.get(_FRED_BASE, params=params, timeout=15.0)
        if r.status_code != 200:
            log.warning("FRED %s: HTTP %d %s",
                        spec.series_id, r.status_code, r.text[:200])
            return []
        data = r.json()
    except Exception as e:  # noqa: BLE001
        log.warning("FRED %s: fetch failed: %s", spec.series_id, e)
        return []
    obs = data.get("observations") or []
    return [o for o in obs if o.get("value") not in (".", "", None)]


_EUROSTAT_BASE = (
    "https://ec.europa.eu/eurostat/api/dissemination/statistics/1.0/data/"
)


async def _fetch_eurostat(
    client: httpx.AsyncClient, spec: IndicatorSpec, *, limit: int = 14,
) -> list[dict]:
    """Pull a JSON-stat dataset from Eurostat REST. Translates the
    multidimensional structure to a flat (date, value) list matching
    the FRED contract: desc, missing-value rows dropped."""
    params = {"format": "JSON", "lastTimePeriod": str(limit)}
    if spec.extra:
        params.update(spec.extra)
    try:
        r = await client.get(
            _EUROSTAT_BASE + spec.series_id, params=params, timeout=20.0,
        )
        if r.status_code != 200:
            log.warning("Eurostat %s: HTTP %d %s",
                        spec.series_id, r.status_code, r.text[:200])
            return []
        data = r.json()
    except Exception as e:  # noqa: BLE001
        log.warning("Eurostat %s: fetch failed: %s", spec.series_id, e)
        return []
    times = (data.get("dimension", {}).get("time", {})
             .get("category", {}).get("index") or {})
    values = data.get("value") or {}
    # Each non-time dim has exactly 1 selected member due to our filter,
    # so the flat index reduces to just the time index.
    out: list[dict] = []
    for date_label, idx in sorted(times.items(), key=lambda x: -x[1]):
        v = values.get(str(idx))
        if v is None:
            continue
        # Match FRED's first-of-period date (YYYY-MM-01) so downstream
        # formatting code doesn't need to special-case sources.
        d_iso = (date_label + "-01") if len(date_label) == 7 else date_label
        out.append({"date": d_iso, "value": str(v)})
    return out


_CBR_INFL_URL = "https://www.cbr.ru/hd_base/infl/"


async def _fetch_cbr_scrape(
    client: httpx.AsyncClient, spec: IndicatorSpec, *, limit: int = 14,
) -> list[dict]:
    """Scrape ЦБ РФ /hd_base/infl/ table. Layout per row:
       <td>MM.YYYY</td><td>key_rate</td><td>annual_CPI</td><td>target</td>

    `spec.extra["col"]` picks 0 (key rate) or 1 (annual CPI). Drops the
    most recent row if the value is blank (CBR publishes the month
    structure before the print sometimes)."""
    import re
    headers = {"User-Agent": "Mozilla/5.0 (compatible; FA-CLAW IR-agent)"}
    try:
        r = await client.get(_CBR_INFL_URL, headers=headers, timeout=20.0)
        if r.status_code != 200:
            log.warning("CBR scrape: HTTP %d", r.status_code)
            return []
    except Exception as e:  # noqa: BLE001
        log.warning("CBR scrape: fetch failed: %s", e)
        return []
    rows = re.findall(
        r"<td[^>]*>(\d{2}\.\d{4})</td>\s*"
        r"<td[^>]*>([0-9,.\s]+|—)</td>\s*"
        r"<td[^>]*>([0-9,.\s]+|—)</td>",
        r.text,
    )
    col = (spec.extra or {}).get("col", 0)
    out: list[dict] = []
    for date_str, key_rate, cpi in rows[:limit]:
        cell = (key_rate if col == 0 else cpi).strip()
        if cell in ("", "—", "-"):
            continue
        # CBR uses comma as decimal separator.
        try:
            val = float(cell.replace(",", ".").replace(" ", ""))
        except ValueError:
            continue
        # Convert "MM.YYYY" → "YYYY-MM-01".
        mm, yy = date_str.split(".")
        out.append({"date": f"{yy}-{mm}-01", "value": str(val)})
    return out


_EIA_BASE = "https://api.eia.gov/v2/"


async def _fetch_eia(
    client: httpx.AsyncClient, spec: IndicatorSpec, *, limit: int = 14,
) -> list[dict]:
    """Pull a weekly/monthly series from EIA Open Data v2. The route
    (path under `/v2/`) and series facet come from `spec.extra`.

    spec.extra = {"route": "petroleum/stoc/wstk", "facet_field": "series"}
    """
    extra = spec.extra or {}
    route = extra.get("route", "")
    facet_field = extra.get("facet_field", "series")
    url = _EIA_BASE + route.lstrip("/") + "/data/"
    params = [
        ("api_key", settings.eia_api_key),
        ("frequency", "weekly" if spec.freq == "W" else "monthly"),
        ("data[0]", "value"),
        (f"facets[{facet_field}][]", spec.series_id),
        ("sort[0][column]", "period"),
        ("sort[0][direction]", "desc"),
        ("length", str(limit)),
    ]
    # EIA refuses requests without a UA from many proxy IPs; explicit
    # browser-like UA fixes 403 from the NL outbound proxy.
    headers = {
        "User-Agent": "Mozilla/5.0 (compatible; FA-CLAW IR-agent)",
        "Accept": "application/json",
    }
    try:
        r = await client.get(url, params=params, headers=headers, timeout=20.0)
        if r.status_code != 200:
            log.warning("EIA %s: HTTP %d %s",
                        spec.series_id, r.status_code, r.text[:200])
            return []
        data = r.json()
    except Exception as e:  # noqa: BLE001
        log.warning("EIA %s: fetch failed: %s", spec.series_id, e)
        return []
    rows = (data.get("response") or {}).get("data") or []
    out: list[dict] = []
    for row in rows:
        v = row.get("value")
        p = row.get("period")
        if v in (None, "") or not p:
            continue
        # EIA periods come as ISO "YYYY-MM-DD" (weekly) or "YYYY-MM"
        # (monthly). Normalise to first-of-month for consistency with
        # FRED. Weekly stays as-is — the renderer treats them as dates.
        d_iso = p if len(p) > 7 else f"{p}-01"
        out.append({"date": d_iso, "value": str(v)})
    return out


_NBS_BASE = "https://data.stats.gov.cn/easyquery.htm"


async def _fetch_nbs(
    client: httpx.AsyncClient, spec: IndicatorSpec, *, limit: int = 14,
) -> list[dict]:
    """Pull monthly indicator from NBS China data portal.

    NBS doesn't expose an official documented API — `easyquery.htm` is
    the same endpoint their stats.gov.cn frontend uses. Spec carries
    `dbcode` (table family) and `zb` (indicator code) in `extra`. The
    response is a JSON envelope with a `returndata.datanodes` array
    where each entry has a code like "zb.A01010101_sj.202604" and a
    `data.data` float value. We parse the period out of the code.

    NBS uses Chinese seasons/periods and a `sj` (时间) wildcard means
    "all periods"; we sort desc client-side and return latest `limit`.
    """
    extra = spec.extra or {}
    dbcode = extra.get("dbcode", "hgyd")  # hgyd = national monthly
    zb = spec.series_id  # e.g. "A01010101" for CPI
    params = {
        "m": "QueryData",
        "dbcode": dbcode,
        "rowcode": "zb",
        "colcode": "sj",
        "wds": "[]",
        "dfwds": f'[{{"wdcode":"zb","valuecode":"{zb}"}}]',
        "k1": str(int(time.time() * 1000)),  # cache-buster the site uses
    }
    headers = {
        "User-Agent": "Mozilla/5.0 (compatible; FA-CLAW IR-agent)",
        "Accept": "application/json,text/javascript,*/*;q=0.1",
        "Referer": "https://data.stats.gov.cn/easyquery.htm?cn=A01",
    }
    try:
        r = await client.get(
            _NBS_BASE, params=params, headers=headers, timeout=20.0,
        )
        if r.status_code != 200:
            log.warning("NBS %s: HTTP %d", spec.series_id, r.status_code)
            return []
        data = r.json()
    except Exception as e:  # noqa: BLE001
        log.warning("NBS %s: fetch failed: %s", spec.series_id, e)
        return []
    nodes = (data.get("returndata") or {}).get("datanodes") or []
    out: list[dict] = []
    for node in nodes:
        code = node.get("code", "")
        # code shape: "zb.<zb_id>_sj.<period>", e.g. "zb.A01010101_sj.202604"
        period = code.rsplit(".", 1)[-1] if "." in code else ""
        if not period or len(period) < 4:
            continue
        # NBS period format: YYYYMM for monthly, YYYYQN for quarterly.
        # Skip Q-style for our monthly use; expect 6-digit YYYYMM.
        if len(period) == 6 and period.isdigit():
            iso = f"{period[:4]}-{period[4:]}-01"
        elif len(period) == 4 and period.isdigit():
            iso = f"{period}-01-01"
        else:
            continue
        val_raw = (node.get("data") or {}).get("data")
        if val_raw in (None, "", 0) and val_raw != 0.0:
            # NBS marks missing as empty string sometimes
            continue
        try:
            val = float(val_raw)
        except (ValueError, TypeError):
            continue
        out.append({"date": iso, "value": str(val)})
    # Already mostly desc by NBS, but enforce explicitly:
    out.sort(key=lambda x: x["date"], reverse=True)
    return out[:limit]


_INVESTING_BASE = "https://www.investing.com"


async def _fetch_investing(
    client: httpx.AsyncClient, spec: IndicatorSpec, *, limit: int = 14,
) -> list[dict]:
    """Scrape the historic-prints table from an Investing.com event page.

    Each event page (e.g. /economic-calendar/ism-manufacturing-pmi-173)
    ships a server-rendered `<tbody>` with rows like
       <td>Jun 01, 2026 (May)</td>
       <td>14:00</td>
       <td class="greenFont">54.0</td>     ← actual
       <td>53.3</td>                       ← forecast
       <td><span>52.7</span></td>          ← previous

    We extract date + actual into the same shape the FRED/Eurostat
    fetchers return. Investing's "(May)" suffix on the date is the
    reference period; we treat it as the value's `date`.

    Cloudflare hasn't been throwing JS challenges on these pages so a
    plain GET with a browser UA gets through.
    """
    import re
    path = (spec.extra or {}).get("path", "")
    if not path:
        return []
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                      "AppleWebKit/537.36 (KHTML, like Gecko) "
                      "Chrome/121.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.5",
    }
    # Circuit breaker: once CF has 429'd us a few times in a row,
    # skip the rest of this cycle's Investing fetches — the IP is
    # blacklisted for hours, no point burning 30s of backoff per row.
    if _investing_trip_state["tripped"]:
        log.info("Investing %s: skipped (CF block tripped earlier)", spec.id)
        return []
    try:
        r = await client.get(
            _INVESTING_BASE + path, headers=headers,
            timeout=20.0, follow_redirects=True,
        )
        # 403 наравне с 429 (17.08.2026). Предохранитель ловил только 429, а
        # Cloudflare отбивает нас именно 403: замер на проде дал 57 таких
        # отказов за трое суток и НИ ОДНОГО 429. Предохранитель не срабатывал
        # ни разу, и каждый прогон впустую ходил за всеми 23 показателями,
        # которые берутся отсюда — по 20 секунд таймаута на каждый в худшем
        # случае. Отказ тот же по смыслу: нас не пускают до конца цикла.
        if r.status_code in (403, 429):
            _investing_trip_state["streak"] += 1
            log.info("Investing %s: HTTP %d (streak %d)", spec.id, r.status_code,
                     _investing_trip_state["streak"])
            if _investing_trip_state["streak"] >= _INVESTING_TRIP_COUNT:
                _investing_trip_state["tripped"] = True
                log.warning(
                    "Investing: %d отказов подряд (403/429) — размыкаю до конца "
                    "цикла, остальные показатели этого источника не приедут.",
                    _investing_trip_state["streak"],
                )
            return []
        # Successful fetch — reset the streak.
        _investing_trip_state["streak"] = 0
        if r.status_code != 200:
            log.warning("Investing %s: HTTP %d", spec.id, r.status_code)
            return []
    except Exception as e:  # noqa: BLE001
        log.warning("Investing %s: fetch failed: %s", spec.id, e)
        return []

    m = re.search(r"<tbody[^>]*>(.*?)</tbody>", r.text, re.DOTALL)
    if not m:
        log.warning("Investing %s: no history table", spec.id)
        return []
    rows = re.findall(r"<tr[^>]*>(.*?)</tr>", m.group(0), re.DOTALL)
    out: list[dict] = []
    for row in rows[:limit]:
        cells = re.findall(r"<td[^>]*>(.*?)</td>", row, re.DOTALL)
        if len(cells) < 3:
            continue
        clean = [re.sub(r"<[^>]+>", "", c).strip() for c in cells]
        date_str = clean[0]
        actual = clean[2]
        # Parse "Jun 01, 2026 (May)" or "Jun 01, 2026"
        date_m = re.match(r"(\w{3}) (\d{1,2}), (\d{4})", date_str)
        if not date_m:
            continue
        try:
            iso = datetime.strptime(
                date_m.group(0), "%b %d, %Y",
            ).strftime("%Y-%m-%d")
        except ValueError:
            continue
        # Sanitize value: strip %, K/M/B suffixes, commas, parens
        v = re.sub(r"[%,KMB()]", "", actual).strip()
        if not v or v == "&nbsp;":
            continue
        try:
            float(v)
        except ValueError:
            continue
        out.append({"date": iso, "value": v})
    return out


_INVESTING_SEM = asyncio.Semaphore(1)
_INVESTING_DELAY = 4.0  # seconds between calls — CF flags >10 req/min
# Circuit breaker — after N consecutive 429s in a single dashboard
# build, give up on Investing for this cycle. Saves 5+ minutes of
# pointless 30s-backoff retries when CF has fully blocked our IP.
_INVESTING_TRIP_COUNT = 3
_investing_trip_state: dict = {"streak": 0, "tripped": False}


# ─── Дата публикации и ссылка на источник ────────────────────────────────
# Владелец 17.08: «данные за июль 2026 — хочется понимать, от какой даты
# конкретно, 15 августа или 16». Справедливо: период наблюдения и дата
# ВЫХОДА данных — разные вещи, и в блоке «обновлено на этой неделе» важна
# именно вторая. FRED отдаёт её в метаданных ряда (`last_updated`), мы просто
# не спрашивали.
_PUBLISHED_CACHE: dict[str, str] = {}


async def _fred_published(client: httpx.AsyncClient, series_id: str) -> str:
    """Когда ряд обновлялся последний раз (YYYY-MM-DD) или пустая строка."""
    if series_id in _PUBLISHED_CACHE:
        return _PUBLISHED_CACHE[series_id]
    try:
        r = await client.get(
            "https://api.stlouisfed.org/fred/series",
            params={"series_id": series_id, "api_key": settings.fred_api_key,
                    "file_type": "json"},
            timeout=15.0,
        )
        d = (r.json().get("seriess") or [{}])[0]
        out = str(d.get("last_updated") or "")[:10]
    except Exception as e:  # noqa: BLE001 — дата приятна, но не критична
        log.info("FRED %s: дату публикации взять не удалось: %s", series_id, e)
        out = ""
    _PUBLISHED_CACHE[series_id] = out
    return out


# Куда ведёт ссылка у показателя. Ведём НА КОНКРЕТНЫЙ РЯД, а не на главную
# поставщика: владелец просил «нажать и получить источник», а корень сайта
# источником одного числа не является.
_SOURCE_LINK = {
    "fred":       lambda s: f"https://fred.stlouisfed.org/series/{s.series_id}",
    "eurostat":   lambda s: f"https://ec.europa.eu/eurostat/databrowser/view/{s.series_id}/default/table",
    "cbr_scrape": lambda s: "https://www.cbr.ru/hd_base/infl/",
    "eia":        lambda s: "https://www.eia.gov/petroleum/supply/weekly/",
    "investing":  lambda s: "https://www.investing.com" + ((s.extra or {}).get("path") or "/economic-calendar/"),
    "nbs":        lambda s: "https://data.stats.gov.cn/easyquery.htm",
}


def source_link(spec: "IndicatorSpec") -> str:
    fn = _SOURCE_LINK.get(spec.provider)
    return fn(spec) if fn else ""


async def _dispatch_fetch(
    client: httpx.AsyncClient, spec: IndicatorSpec,
) -> list[dict]:
    """Route a spec to the right per-source fetcher."""
    if spec.provider == "fred":
        return await _fetch_fred(client, spec)
    if spec.provider == "eurostat":
        return await _fetch_eurostat(client, spec)
    if spec.provider == "cbr_scrape":
        return await _fetch_cbr_scrape(client, spec)
    if spec.provider == "eia":
        return await _fetch_eia(client, spec)
    if spec.provider == "nbs":
        return await _fetch_nbs(client, spec)
    if spec.provider == "investing":
        # Cloudflare rate-limits aggressive scrapes; serialise calls
        # and inject a short delay between them. 20 indicators × 1.5s
        # ≈ 30s extra latency but stays under the 429 threshold.
        async with _INVESTING_SEM:
            result = await _fetch_investing(client, spec)
            await asyncio.sleep(_INVESTING_DELAY)
            return result
    log.warning("Unknown macro provider %r for %s", spec.provider, spec.id)
    return []


def _delta(curr: float, prev: float) -> tuple[float, float]:
    """Return (absolute, percentage) change from prev → curr.
    For 0 baseline returns (curr-prev, 0.0) instead of NaN."""
    abs_d = curr - prev
    pct_d = (abs_d / prev * 100.0) if prev else 0.0
    return abs_d, pct_d


def _compute_dynamics(
    spec: IndicatorSpec, observations: list[dict],
) -> dict:
    """Walk the (already DESC-sorted) observations and compute the
    deltas we declared in `spec.compares`. Returns a dict ready for
    rendering or empty if data is unreadable.

    Observation lookup offsets by frequency:
       monthly:    mom = 1, yoy = 12
       quarterly:  qoq = 1, yoy = 4
       weekly:     wow = 1, yoy = 52
    """
    if not observations:
        return {}
    try:
        rows = [
            (o["date"], float(o["value"]))
            for o in observations
            if o.get("value") not in (".", "", None)
        ]
    except (ValueError, TypeError):
        return {}
    if not rows:
        return {}

    latest_date, latest_val = rows[0]
    out: dict = {
        "latest_date": latest_date,
        "latest_value": latest_val,
        "compares": [],
    }

    offsets = {
        "mom": (1, "м/м"),
        "qoq": (1, "кв/кв"),
        "yoy": (12 if spec.freq == "M" else (4 if spec.freq == "Q" else 52), "г/г"),
        "wow": (1, "н/н"),
    }
    for kind in spec.compares:
        if kind not in offsets:
            continue
        off, label = offsets[kind]
        if off >= len(rows):
            continue
        _prev_date, prev_val = rows[off]
        abs_d, pct_d = _delta(latest_val, prev_val)
        out["compares"].append({
            "label": label,
            "abs": abs_d,
            "pct": pct_d,
            "prev_value": prev_val,
        })
    return out


# ─── Top-level fetch ────────────────────────────────────────────────────


def _read_cache() -> Optional[dict]:
    """Load the on-disk cache as-is. Returns the full envelope
    {ts, data: {indicator_id: {...}}}; per-row freshness is checked
    by the caller against `_PROVIDER_TTL_S`."""
    try:
        if not _CACHE_PATH.exists():
            return None
        env = json.loads(_CACHE_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    # Записи СТАРОЙ ФОРМЫ выбрасываем (17.08.2026). Кэш живёт 6–24 часа, и
    # после правки состава полей он ещё сутки отдавал бы записи без единицы,
    # даты публикации и ссылки — то есть новый блок выглядел бы сломанным, хотя
    # код уже верный. Поймано на живой выкатке: таблица встала, а колонки
    # «Ед.», «Опубл.» и «Источник» оказались пустыми.
    data = env.get("data") or {}
    kept = {k: v for k, v in data.items() if isinstance(v, dict) and "source_url" in v}
    if len(kept) != len(data):
        log.info("macro: кэш частично устарел по составу полей — отброшено %d записей",
                 len(data) - len(kept))
        env["data"] = kept
    return env


def _write_cache(payload: dict) -> None:
    """Merge the freshly-fetched payload into whatever the on-disk
    cache already has, keeping previous successful rows when the new
    refresh failed to renew them. This is what lets us recover from
    Investing 429 storms — yesterday's good prints stick around even
    when Cloudflare denied today's fetch."""
    try:
        _CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        existing = _read_cache() or {}
        merged = dict(existing.get("data") or {})
        now = time.time()
        for ind_id, row in payload.items():
            row_with_ts = dict(row)
            row_with_ts.setdefault("fetched_at", now)
            merged[ind_id] = row_with_ts
        _CACHE_PATH.write_text(
            json.dumps({"ts": now, "data": merged},
                       ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except OSError as e:
        log.warning("macro cache: write failed: %s", e)


def _stale_specs(specs: list[IndicatorSpec], cached: dict) -> list[IndicatorSpec]:
    """Return the subset of `specs` whose cached row is missing or
    older than its provider's TTL. Anything still fresh is left alone."""
    out: list[IndicatorSpec] = []
    now = time.time()
    cache_data = cached.get("data") or {}
    for s in specs:
        row = cache_data.get(s.id)
        ttl = _PROVIDER_TTL_S.get(s.provider, _CACHE_TTL_S)
        if not row or (now - float(row.get("fetched_at") or 0)) > ttl:
            out.append(s)
    return out


async def fetch_macro_dashboard(*, force_refresh: bool = False) -> dict:
    """Pull all indicators in parallel and return a dict keyed by
    `IndicatorSpec.id` → metric record. Cached on disk for 6h.

    Result shape per indicator:
       {
         "id": "us_cpi", "ru_name": "Инфляция США (CPI)",
         "region": "US", "unit": "",
         "latest_date": "2026-04-01", "latest_value": 332.407,
         "compares": [
            {"label": "м/м", "abs": ..., "pct": ..., "prev_value": ...},
            {"label": "г/г", ...},
         ],
       }
    """
    cached = _read_cache() or {"data": {}}
    # Reset the Investing circuit-breaker at the start of every cycle.
    _investing_trip_state["streak"] = 0
    _investing_trip_state["tripped"] = False

    # Skip provider specs whose API key is missing, or whose endpoints
    # are not reachable from the current egress IP. Eurostat/CBR are
    # free + reachable. EIA blocks RU IPs at the WAF layer; NBS blocks
    # foreign IPs. Both need a non-RU outbound proxy — enable via the
    # FINENGINE_MACRO_FOREIGN_PROXY env when one is configured.
    skip_providers: set[str] = set()
    if not settings.fred_api_key:
        log.info("FRED: no API key, skipping")
        skip_providers.add("fred")
    if not settings.eia_api_key:
        log.info("EIA: no API key, skipping crude-stocks row")
        skip_providers.add("eia")
    # Geo-blocked sources: skip until a non-RU egress proxy is wired.
    if not settings.macro_foreign_proxy:
        log.info("macro: foreign_proxy unset, skipping EIA (US-WAF)")
        skip_providers.add("eia")
    all_targets = [s for s in _REGISTRY if s.provider not in skip_providers]

    # Per-provider TTL: only re-fetch rows whose cache is stale (or
    # missing) — `force_refresh=True` overrides and refetches everything.
    if force_refresh:
        targets = all_targets
    else:
        targets = _stale_specs(all_targets, cached)
        log.info(
            "macro: %d/%d indicators stale → refetch", len(targets),
            len(all_targets),
        )
        if not targets:
            return cached.get("data") or {}

    # Two clients: a default one for sources reachable from the prod
    # VPS (FRED/Eurostat/CBR), and a separately-routed one for sources
    # that benefit from a non-RU egress IP — EIA (US WAF geo-blocks),
    # NBS (CN URL ACL), and Investing.com (its Cloudflare WAF flags the
    # prod IP after ~20 rapid scrapes; PL proxy gives us a fresh-IP slot
    # per cycle).
    _FOREIGN = {"eia", "nbs", "investing"}
    foreign_specs = {s.provider for s in targets} & _FOREIGN
    use_foreign = bool(settings.macro_foreign_proxy) and foreign_specs
    async with httpx.AsyncClient() as client:
        if use_foreign:
            async with httpx.AsyncClient(
                proxy=settings.macro_foreign_proxy,
            ) as fclient:
                tasks = [
                    _dispatch_fetch(
                        fclient if s.provider in _FOREIGN else client,
                        s,
                    )
                    for s in targets
                ]
                results = await asyncio.gather(*tasks, return_exceptions=True)
        else:
            tasks = [_dispatch_fetch(client, s) for s in targets]
            results = await asyncio.gather(*tasks, return_exceptions=True)
        # Даты публикации спрашиваем ЗДЕСЬ, пока клиент жив: блок `async with`
        # закрывается раньше сборки payload, и вызов оттуда упал бы на закрытом
        # соединении. Ответы кэшируются в процессе, так что повторный прогон
        # наружу не ходит.
        published = {
            s.series_id: await _fred_published(client, s.series_id)
            for s in targets if s.provider == "fred"
        }

    payload: dict = {}
    for spec, obs in zip(targets, results):
        if isinstance(obs, BaseException):
            log.warning("macro %s (%s): task exception: %s",
                        spec.id, spec.provider, obs)
            continue
        dyn = _compute_dynamics(spec, obs)
        if not dyn:
            continue
        payload[spec.id] = {
            "id": spec.id,
            "ru_name": spec.ru_name,
            "region": spec.region,
            "unit": spec.unit,
            # Дата ВЫХОДА данных, а не периода наблюдения — см. `_fred_published`.
            # Только для FRED: у остальных поставщиков её взять неоткуда, и
            # пустая строка честнее выдуманной.
            "published": published.get(spec.series_id, ""),
            "source_url": source_link(spec),
            "fetched_at": time.time(),
            **dyn,
        }
    if payload:
        _write_cache(payload)
    # Return merged view: fresh fetches overlaid on cached previous rows.
    return {**(cached.get("data") or {}), **payload}


# ─── Render ─────────────────────────────────────────────────────────────


_REGION_LABELS = {
    "RU": ("🇷🇺", "Россия"),
    "US": ("🇺🇸", "США"),
    "EU": ("🇪🇺", "Еврозона"),
    "CN": ("🇨🇳", "Китай"),
}


def _fmt_value(v: float, unit: str) -> str:
    """Latest-value formatter. Cyrillic non-breaking space between
    value and unit so it doesn't wrap. Indexes (no unit) get one
    decimal; values with units get whichever density is readable."""
    abs_v = abs(v)
    if not unit:
        if abs_v >= 100:
            return f"{v:,.1f}".replace(",", " ")
        return f"{v:,.2f}".replace(",", " ")
    if abs_v >= 1000:
        return f"{v:,.0f} {unit}".replace(",", " ")
    if abs_v >= 10:
        return f"{v:,.1f} {unit}".replace(",", " ")
    return f"{v:.2f} {unit}"


# Human-readable label + landing URL for each data provider. The
# regional footer rolls these up so the IR analyst can verify a number
# at its origin without having to remember which agency owns what.
_PROVIDER_LABELS: dict[str, tuple[str, str]] = {
    "fred":       ("FRED (St. Louis Fed)", "https://fred.stlouisfed.org/"),
    "eurostat":   ("Eurostat",              "https://ec.europa.eu/eurostat"),
    "cbr_scrape": ("ЦБ РФ",                 "https://www.cbr.ru/hd_base/infl/"),
    "eia":        ("EIA",                   "https://www.eia.gov/petroleum/weekly/"),
    "investing":  ("Investing.com",         "https://www.investing.com/economic-calendar/"),
    "nbs":        ("NBS China",             "http://data.stats.gov.cn/"),
}


def _mark_freshness(payload: dict) -> None:
    """#96: проставить каждому индикатору first_seen — когда МЫ впервые увидели
    его текущее значение (честный прокси даты публикации: у FRED/ЦБ отдаётся
    дата наблюдения, не релиза). Стейт — маленький JSON-сайдкар в data_dir."""
    import json as _json
    import time as _time
    from finengine.config import settings as _settings
    path = _settings.data_dir / "macro_first_seen.json"
    try:
        seen = _json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    except (ValueError, OSError):
        seen = {}
    now_iso = _time.strftime("%Y-%m-%d")
    changed = False
    for key, ind in payload.items():
        latest = str(ind.get("latest_date") or "")
        rec = seen.get(key)
        if not isinstance(rec, dict) or rec.get("latest_date") != latest:
            seen[key] = rec = {"latest_date": latest, "first_seen": now_iso}
            changed = True
        ind["first_seen"] = rec.get("first_seen", now_iso)
    if changed:
        try:
            tmp = path.with_suffix(".tmp")
            tmp.write_text(_json.dumps(seen, ensure_ascii=False), encoding="utf-8")
            tmp.replace(path)
        except OSError:
            pass


def _fresh_this_week(payload: dict, now: "datetime | None" = None) -> list[dict]:
    """Индикаторы, чьё значение впервые появилось у нас за последние 7 дней."""
    from datetime import datetime as _dt, timedelta as _td
    cutoff = ((now or _dt.now()) - _td(days=7)).strftime("%Y-%m-%d")
    return [ind for ind in payload.values()
            if str(ind.get("first_seen") or "") >= cutoff]


def _delta_cell(ind: dict, kind: str) -> str:
    """Ячейка динамики.

    🔴 У ПОКАЗАТЕЛЕЙ В ПРОЦЕНТАХ ДИНАМИКА МЕРЯЕТСЯ ПУНКТАМИ, А НЕ ПРОЦЕНТАМИ.
    Первая редакция этой ячейки предпочитала относительное изменение всему
    остальному, и в выпуск уехало «ИЦП РФ 9,40 % — м/м +70,9%». Это процент от
    процента: ИЦП вырос с 5,5 % до 9,4 %, то есть на 3,9 ПУНКТА. Ставка ЦБ
    показывалась как «-1.8%» вместо «-0.25 пп».

    Правило: единица «%» → берём абсолютную разницу и называем её пунктами;
    всё остальное (индексы, деньги, штуки) → относительное изменение.
    """
    in_points = (ind.get("unit") or "").strip() == "%"
    for c in ind.get("compares") or []:
        if c.get("label", "").startswith(kind):
            pct, abs_d = c.get("pct"), c.get("abs")
            if in_points and isinstance(abs_d, (int, float)):
                return f"{abs_d:+.2f} пп"
            if isinstance(pct, (int, float)):
                return f"{pct:+.1f}%"
            if isinstance(abs_d, (int, float)):
                return f"{abs_d:+.2f} пп"
    return "—"


def render_macro_dashboard_md(payload: dict, mode: str = "both") -> str:
    """Блок макроиндикаторов — ТАБЛИЦЕЙ (переписан 17.08.2026 по разбору владельца).

    ЧТО БЫЛО НЕ ТАК В СПИСКЕ И ПОЧЕМУ ТАБЛИЦА ЛУЧШЕ:
      * «Промышленная инфляция США (PPI): 284.1» читалось как инфляция в 284
        процента. На деле это ИНДЕКС 1982=100 — единица теперь в своей колонке
        и врать не может;
      * динамика шла хвостом одной строкой («м/м +0.1% · г/г +3.5%») и глазами
        не сравнивалась между показателями. В таблице колонки м/м и г/г стоят
        друг под другом;
      * не было видно, КОГДА данные вышли: «(данные за июл 2026)» — это период
        наблюдения, а владелец спрашивал про дату публикации;
      * источник был общей строкой на регион, ведущей на главную поставщика.
        Теперь ссылка у КАЖДОГО показателя и ведёт на конкретный ряд.

    Подзаголовок «Где находится экономика прямо сейчас (последние публикации +
    динамика)» убран: он ничего не добавлял к названию.
    """
    if not payload:
        return ""

    _mark_freshness(payload)
    sections: list[str] = []

    if mode in ("both", "week_only"):
        fresh = _fresh_this_week(payload)
        if fresh:
            sections.append(_render_macro_table(
                "## 🆕 Макроиндикаторы — вышли на этой неделе", fresh, with_region=True))
        elif mode == "week_only":
            sections.append("## 🆕 Макроиндикаторы — вышли на этой неделе\n\n"
                            "_На этой неделе новых публикаций не было._")
        if mode == "week_only":
            return "\n\n".join(sections)

    by_region: dict[str, list[dict]] = {}
    for ind in payload.values():
        by_region.setdefault(ind["region"], []).append(ind)

    out: list[str] = ["## 📊 Макроиндикаторы"]
    for region in ("RU", "US", "EU", "CN"):
        items = by_region.get(region)
        if not items:
            continue
        flag, name = _REGION_LABELS.get(region, ("", region))
        out.append("")
        out.append(f"### {flag} {name}")
        out.append(_render_macro_table(None, items, with_region=False))
    sections.append("\n".join(out))
    return "\n\n".join(sections)


def _fmt_pub_date(iso: str) -> str:
    """Дата ПУБЛИКАЦИИ — с числом, а не «авг 2026».

    Владелец 17.08: «хочется понимать, это 15 августа, 16, типа когда именно».
    Для периода наблюдения месяц уместен (данные месячные), для даты выхода —
    нет: вся ценность блока «вышли на этой неделе» в том, какого числа.
    """
    if not iso or len(iso) < 10:
        return "—"
    try:
        d = datetime.strptime(iso[:10], "%Y-%m-%d")
    except ValueError:
        return "—"
    months = {
        1: "янв", 2: "фев", 3: "мар", 4: "апр", 5: "май", 6: "июн",
        7: "июл", 8: "авг", 9: "сен", 10: "окт", 11: "ноя", 12: "дек",
    }
    return f"{d.day} {months[d.month]}"


# Сколько ряд имеет право отставать от сегодняшнего дня — по своей частоте, в
# днях. WHY: 17.08 в выпуске стояла «Инфляция Китая (CPI, OECD)» с периодом
# «апр 2025» наравне со свежими показателями — зеркало OECD в FRED замерло 16
# месяцев назад, и читатель принимал прошлогоднее число за текущее.
#
# 🔴 ПОРОГ МЕРЯЕТСЯ ЦИКЛОМ ПУБЛИКАЦИИ, А НЕ СНИМКОМ ДАТ. Первая редакция взяла
# запас от ЖИВЫХ наблюдений 18.08 (M=92, Q=183) — и правка против вранья сама
# начала бы врать: точка датируется НАЧАЛОМ периода, а выходит после его конца,
# так что лаг свежайшей точки РАСТЁТ весь межвыпускной промежуток и обнуляется
# только выходом следующей.
#   * Q=275. Точка за III кв. — 2026-07-01, advance-оценка BEA выходит в конце
#     октября; до неё свежайшая 2026-04-01, и уже 2 октября её лаг перевалил бы
#     за 183 дня. Худший НОРМАЛЬНЫЙ лаг = 92 (квартал) + ~121 (до advance) ≈ 211.
#   * M=125. Промпроизводство еврозоны за июнь выходит в середине августа; к
#     кануну следующего выпуска лаг июньской точки дорастает до ~105 дней при
#     пороге 92 — здоровые ez_unrate/ez_ppi/ez_indpro пометились бы все трое.
#   * W=21 оставлен: отчёт EIA выходит через 5–6 дней после отчётной недели,
#     худший нормальный лаг ~13.
# Честнее календарного лага был бы ИНТЕРВАЛ САМОГО РЯДА, но в записи его нет:
# `_compute_dynamics` кладёт `latest_date` и `prev_value` без даты предыдущей
# точки, а состав записи кэша менять нельзя — прод сутки отдавал бы форму,
# которую `_read_cache` выбросит (поймано на живой выкатке 17.08).
_STALE_AFTER_DAYS: dict[str, int] = {"M": 125, "Q": 275, "W": 21}


def _period_cell(ind: dict, now: "datetime | None" = None) -> str:
    """Период наблюдения; у отставшего ряда — с пометкой (см. `_STALE_AFTER_DAYS`)."""
    iso = str(ind.get("latest_date", ""))
    label = _fmt_date(iso)
    spec = _SPEC_BY_ID.get(str(ind.get("id", "")))
    limit = _STALE_AFTER_DAYS.get(spec.freq) if spec else None
    # `_fmt_date` возвращает вход как есть, когда дата не разобралась, — считать
    # отставание тогда не по чему.
    if not limit or label == iso:
        return label
    lag_days = ((now or datetime.now()) - datetime.fromisoformat(iso)).days
    return f"{label} · ⚠️ ряд отстаёт" if lag_days > limit else label


def _render_macro_table(title: str | None, items: list[dict], *,
                        with_region: bool) -> str:
    """Одна таблица показателей.

    Колонки выбраны по вопросам, которые владелец задал вслух, глядя на старый
    список: «в чём измеряется», «какое было раньше», «от какой даты», «где
    посмотреть». Каждая колонка — ответ на один из них.
    """
    flag = {"RU": "🇷🇺", "US": "🇺🇸", "EU": "🇪🇺", "CN": "🇨🇳"}
    lines: list[str] = []
    if title:
        lines += [title, ""]
    head = ("| Показатель | Значение | Ед. | м/м | г/г | Период | Опубл. | Источник |"
            if not with_region else
            "| | Показатель | Значение | Ед. | м/м | г/г | Период | Опубл. | Источник |")
    lines.append(head)
    lines.append("|" + "---|" * (8 if not with_region else 9))
    for ind in sorted(items, key=lambda x: (x.get("region", ""), x.get("ru_name", ""))):
        try:
            val = _fmt_value(float(ind.get("latest_value")), "")
        except (TypeError, ValueError):
            val = str(ind.get("latest_value", ""))
        spec = _SPEC_BY_ID.get(str(ind.get("id", "")))
        unit = ind.get("unit") or (spec.unit if spec else "") or "—"
        period = _period_cell(ind)
        pub = _fmt_pub_date(str(ind.get("published") or ""))
        url = ind.get("source_url") or ""
        src = f"[↗]({url})" if url else "—"
        # Ни у недельного, ни у квартального изменения своей колонки нет — оба
        # садятся в «м/м». 17.08 в выпуск так молча уехали запасы нефти EIA
        # (частота W): «+4.3%» под шапкой «м/м» читается как месячный прирост,
        # поэтому подписываем прямо в ячейке. Квартальные при этом не доезжали
        # вовсе: у ВВП США и еврозоны кв/кв считался и отбрасывался, а у ВВП
        # Китая кв/кв — ЕДИНСТВЕННОЕ сравнение, и строка выходила без динамики.
        mom = _delta_cell(ind, "м/м")
        if mom == "—":
            for label in ("н/н", "кв/кв"):
                alt = _delta_cell(ind, label)
                if alt != "—":
                    mom = f"{alt} {label}"
                    break
        cells = [f"**{ind.get('ru_name', '')}**", val, unit,
                 mom, _delta_cell(ind, "г/г"), period, pub, src]
        if with_region:
            cells.insert(0, flag.get(ind.get("region", ""), ""))
        lines.append("| " + " | ".join(cells) + " |")
    return "\n".join(lines)


def _fmt_date(iso: str) -> str:
    """`2026-04-01` → `апр 2026`. Keeps it short under the indicator
    name so the value visually anchors the row."""
    try:
        dt = datetime.fromisoformat(iso)
    except ValueError:
        return iso
    months = {
        1: "янв", 2: "фев", 3: "мар", 4: "апр", 5: "май", 6: "июн",
        7: "июл", 8: "авг", 9: "сен", 10: "окт", 11: "ноя", 12: "дек",
    }
    return f"{months[dt.month]} {dt.year}"
