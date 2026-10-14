"""Load + cache the sources.yaml + entity tables.

The YAML lives next to this module so the deploy ships it as data.
We never edit it from code — changes go through git review per AUA.
"""
from __future__ import annotations

import dataclasses
import logging
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)


@dataclasses.dataclass
class SourceConfig:
    id: str
    display_name: str
    type: str
    region: str
    connector: str
    url: str
    weight: float
    active: bool = True


@dataclasses.dataclass
class EntityConfig:
    canonical: str
    aliases: list[str]
    kind: str  # "watchlist_ru" | "watchlist_world" | "regulator" | "macro_topic"


@dataclasses.dataclass
class NewsConfig:
    sources: list[SourceConfig]
    entities: list[EntityConfig]

    def active_sources(self, connector: str | None = None) -> list[SourceConfig]:
        return [
            s for s in self.sources
            if s.active and (connector is None or s.connector == connector)
        ]

    def watchlist(self) -> list[EntityConfig]:
        return [e for e in self.entities if e.kind.startswith("watchlist_")]

    def regulators(self) -> list[EntityConfig]:
        return [e for e in self.entities if e.kind == "regulator"]

    def macro_topics(self) -> list[EntityConfig]:
        return [e for e in self.entities if e.kind == "macro_topic"]


_YAML_PATH = Path(__file__).parent / "sources.yaml"
_COMPANIES_YAML = Path(__file__).parent / "companies.yaml"
_cache: NewsConfig | None = None


def _company_sources() -> list[SourceConfig]:
    """Auto-generate one SourceConfig per company-disclosure feed.

    Reads companies.yaml. For each company with `edgar_cik`, builds an
    EDGAR Atom URL. For each with `edisclosure_id`, builds the future
    e-disclosure connector entry. Plus, for EVERY company, a Google
    News RSS proxy URL — this gives broad coverage for cos that don't
    file via EDGAR (Tencent, Xiaomi, ICBC) or aren't covered by our
    base wires (Russian second-tier).

    Returns sources list ready to merge into the main catalog.
    """
    if not _COMPANIES_YAML.exists():
        return []
    import yaml
    try:
        with _COMPANIES_YAML.open(encoding="utf-8") as f:
            doc = yaml.safe_load(f) or {}
    except (OSError, yaml.YAMLError) as e:
        log.warning("companies.yaml load failed: %s", e)
        return []

    out: list[SourceConfig] = []
    for c in doc.get("companies") or []:
        canonical = c.get("canonical", "")
        if not canonical:
            continue
        region = c.get("region", "global")
        weight = float(c.get("weight", 0.9))
        active_default = bool(c.get("active", True))

        # SEC EDGAR feed. Форма — из companies.yaml (`edgar_form`), по
        # умолчанию 8-K: американский эмитент объявляет существенный факт
        # именно ею.
        #
        # 🔴 ИНОСТРАННЫЕ ЭМИТЕНТЫ 8-K НЕ ПОДАЮТ (19.08.2026). У них форма 6-K,
        # и запрос `type=8-K` возвращает ВАЛИДНУЮ ПУСТУЮ ленту — HTTP 200,
        # правильный CIK, ноль записей, и так навсегда. Ревизия источников
        # показала ровно десять таких: Alibaba, JD.com, Baidu, Grab, Sea,
        # TSMC, ASML, PagSeguro, Spotify, Klarna. По журналу они числились
        # «молчащими» — то есть выглядели как компании без новостей.
        cik = c.get("edgar_cik")
        if cik:
            ticker = c.get("ticker", "")
            form = str(c.get("edgar_form") or "8-K")
            edgar_url = (
                "https://www.sec.gov/cgi-bin/browse-edgar"
                f"?action=getcompany&CIK={cik}&type={form}&output=atom"
            )
            out.append(SourceConfig(
                id=f"edgar_{ticker.lower()}" if ticker else f"edgar_{cik}",
                display_name=f"{canonical} (SEC {form})",
                type="official",
                region=region,
                connector="edgar",
                url=edgar_url,
                weight=weight,
                active=active_default,
            ))

        # e-disclosure.ru — RU material facts via Playwright. Respects
        # the per-company `active` override in companies.yaml so the
        # operator can disable a single name without yanking the rest.
        ed_id = c.get("edisclosure_id")
        if ed_id:
            ed_active = c.get("active", True)  # default ON now Playwright is wired
            out.append(SourceConfig(
                id=f"edisclosure_{ed_id}",
                display_name=f"{canonical} (e-disclosure)",
                type="official",
                region=region,
                connector="edisclosure",
                url=f"https://www.e-disclosure.ru/portal/company.aspx?id={ed_id}",
                weight=max(weight, 0.92),  # officials always top-bucket
                active=ed_active,
            ))

        # Google News RSS proxy — broad-net per-company coverage.
        # Picks the right Google News locale based on region: RU/CN/EU
        # default to ru-RU (Russian-language IR analyst audience),
        # US/global to en-US.
        from urllib.parse import quote
        if region in ("US", "global"):
            ceid, hl, gl = "US:en", "en-US", "US"
        else:
            ceid, hl, gl = "RU:ru", "ru-RU", "RU"
        # Quoted-canonical so we don't match every news mentioning the
        # word "Apple" as a fruit. Wire weight (0.65) — not official.
        query = f'"{canonical}"'
        # Окно запроса 7 дней, а НЕ сутки (17.08.2026).
        #
        # Здесь стояло `when:24h`, и это молча обрезало ручку «период сбора»:
        # пользователь ставит 48 часов или неделю, а по компаниям всё равно
        # приезжали только последние сутки. Замер: `gn_icbc` при 24h — ноль
        # записей, при 7d — три; `gn_raiffeisen` ноль против семи;
        # `gn_weride` ноль против четырёх. Из 112 RSS-источников десять
        # оказались пустыми ровно поэтому.
        #
        # Лишнего не приедет: дату отсекает НАШ фильтр по `since_hours`
        # (`connectors/rss.py`, отбрасывает всё старше окна). Запрос лишь не
        # должен быть УЖЕ нашего окна, иначе он и решает за пользователя.
        gn_url = (
            f"https://news.google.com/rss/search?"
            f"q={quote(query)}+when:7d&ceid={ceid}&hl={hl}&gl={gl}"
        )
        # Sanitize the slug: lowercase, ascii, strip space/punct
        import re as _re
        slug = _re.sub(r"[^a-z0-9]+", "_",
                       canonical.lower().encode("ascii", "ignore").decode()).strip("_")
        if not slug:
            slug = str(abs(hash(canonical)))[:8]
        out.append(SourceConfig(
            id=f"gn_{slug}",
            display_name=f"{canonical} (Google News)",
            type="wire",
            region=region,
            connector="rss",  # standard RSS connector handles it
            url=gn_url,
            weight=0.65,
            active=True,
        ))

    log.info("companies.yaml: generated %d virtual sources", len(out))
    return out


def _flatten_entities(raw: dict[str, Any]) -> list[EntityConfig]:
    out: list[EntityConfig] = []
    for kind, items in (raw or {}).items():
        for it in (items or []):
            out.append(EntityConfig(
                canonical=it.get("canonical", ""),
                aliases=list(it.get("aliases", [])),
                kind=("macro_topic" if kind == "macro_topics"
                      else "regulator" if kind == "regulators"
                      else kind),
            ))
    return out


def load() -> NewsConfig:
    """Read sources.yaml once and cache. Call `reload()` to force re-parse."""
    global _cache
    if _cache is not None:
        return _cache
    import yaml
    with _YAML_PATH.open(encoding="utf-8") as f:
        doc = yaml.safe_load(f) or {}
    raw_sources = doc.get("sources", [])
    sources = [
        SourceConfig(
            id=s["id"],
            display_name=s["display_name"],
            type=s["type"],
            region=s["region"],
            connector=s["connector"],
            url=s["url"],
            weight=float(s.get("weight", 0.5)),
            active=bool(s.get("active", True)),
        )
        for s in raw_sources
    ]
    entities = _flatten_entities(doc.get("entities", {}))

    # Merge in auto-generated per-company sources (SEC EDGAR + future
    # e-disclosure). They follow the same SourceConfig shape so the
    # rest of the pipeline doesn't care about origin.
    sources = sources + _company_sources()

    _cache = NewsConfig(sources=sources, entities=entities)
    log.info(
        "news.config: loaded %d sources (%d active) + %d entities",
        len(sources), sum(1 for s in sources if s.active), len(entities),
    )
    return _cache


def reload() -> NewsConfig:
    global _cache
    _cache = None
    return load()
