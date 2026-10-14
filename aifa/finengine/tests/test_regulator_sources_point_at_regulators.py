"""Блок «Регуляторы» слушает самих регуляторов, а не что придётся (18.08.2026).

РАЗБОР ЖИВОГО ВЫПУСКА ПРОДА 17.08. В разделе «🏛️ Регуляторы» три строки были
неправдой разного сорта:

  * «Росстат — свежих публикаций за 7 дней нет». Источник `rosstat_gov` вёл на
    ленту ПРАВИТЕЛЬСТВА про Росстат (`government.ru/department/456`): замер
    18.08 — HTTP 200, 20 записей, свежайшая от 25.06 «Мишустин поздравил
    работников Росстата с профессиональным праздником», публикаций статистики
    ноль. Сам Росстат в это время печатал недельную инфляцию каждую среду.
    Шланг был не тот, а выпуск честно докладывал о пустоте.
  * «Минфин США — мониторинг продолжается» при `active: false`. Лента
    `treasury.gov/connect/blog` была выключена как «битый XML» — на деле адрес
    отдаёт HTML-лендинг, блога там нет. Мы не следили и говорили, что следим.
  * «НБК — мониторинг продолжается» при `active: false` и китайской странице
    «горячих тем» вместо ленты публикаций.

ДВЕ ЛОВУШКИ, В КОТОРЫЕ ЛЕГКО СЕСТЬ ПРИ ПОЧИНКЕ, — сторожа ниже держат обе:

  * лента `rosstat.gov.ru/announcements/rss` формально исправна, но датирована
    БУДУЩИМ (анонсы выхода). `rank.py:_freshness` на отрицательной дельте даёт
    свежесть 1.0 — ещё не вышедшая статистика вытеснила бы реальные новости;
  * `home.treasury.gov/rss.xml` и `ofac.treasury.gov/rss.xml` отдают 200,
    Content-Type `application/rss+xml`, `bozo=False` и записи с датами — то
    есть выглядят живыми. Внутри: FAQ по программе SSBCI (свежайшая 22.07) и
    статические страницы санкционных программ (свежайшая 01.07.2025). Такой
    источник делает ложь про «мониторинг продолжается» НЕОТЛИЧИМОЙ от правды.

Сторожа смотрят на КАТАЛОГ И КОД (адреса, флаги, запуск сборщика), а не на
текст комментариев рядом: на «сторож проверяет собственный комментарий» мы
стояли трижды.

⚠️ ЧЕГО ЭТИ СТОРОЖА НЕ ЛОВЯТ (дописано ревизией 21.08.2026). Ни один из них
не ходит в сеть, а значит НЕ ЗНАЕТ, что лента отдаёт сегодня. Адрес,
принадлежащий регулятору и внесённый в каталог правильно, может месяцами
возвращать 404, HTML-лендинг или прошлогодние статические страницы — и все
проверки ниже останутся зелёными. Ровно так и жили `home.treasury.gov/rss.xml`
и `ofac.treasury.gov/rss.xml`: 200, `bozo=False`, записи с датами, публикаций
ноль. Здесь охраняется ТОЛЬКО то, что адрес не подменён на чужой, флаг не
выключен молча и точечное доверие TLS доезжает до сборщика. Живость ленты
проверяется замером, а не тестом.
"""
from __future__ import annotations

import ast
import pathlib
import re
import ssl
from urllib.parse import urlsplit

import pytest

from finengine.net.ru_gov_tls import verify_for
from finengine.skills.news import config as _cfg

_ROOT = pathlib.Path(__file__).resolve().parents[1]
_YAML = _ROOT / "src" / "finengine" / "skills" / "news" / "sources.yaml"
_RSS_PY = _ROOT / "src" / "finengine" / "skills" / "news" / "connectors" / "rss.py"


def _source(sid: str):
    s = next((x for x in _cfg.load().sources if x.id == sid), None)
    assert s is not None, f"источник {sid} пропал из каталога"
    return s


# ─────────────────────────── Росстат ───────────────────────────


def test_rosstat_feed_belongs_to_rosstat_itself():
    """Отрицательный контроль: вернул `government.ru` — краснеет."""
    host = (urlsplit(_source("rosstat_gov").url).hostname or "").lower()
    assert host.endswith("rosstat.gov.ru"), (
        f"лента Росстата ведёт на {host} — это публикации не Росстата")


def test_rosstat_feed_is_releases_not_future_dated_announcements():
    """Лента анонсов исправна и потому опасна: её даты в будущем, а свежесть
    у будущей даты максимальная — анонс завтрашней инфляции встал бы в выпуск
    как уже вышедшая цифра."""
    url = _source("rosstat_gov").url
    assert "/announcements/" not in url, (
        "взята лента анонсов: даты в будущем получат свежесть 1.0")
    assert "/central-news/" in url, (
        "лента выпусков заменена — проверить, что в новой есть сами цифры "
        "(недельный ИПЦ, ВВП, ИЦП), а не пересказы про ведомство")


def test_catalog_address_is_inside_the_scoped_trust_list():
    """Адрес каталога и список доверия должны совпадать. Сертификат Росстата
    выдан центром Минцифры; адрес, выпавший из `_RU_GOV_HOSTS`, соберёт
    `CERTIFICATE_VERIFY_FAILED` и будет числиться недоступным."""
    assert verify_for(_source("rosstat_gov").url) is not True, (
        "адрес Росстата вне списка net/ru_gov_tls._RU_GOV_HOSTS — сбор упадёт "
        "по проверке сертификата")


@pytest.mark.parametrize("sid,scoped", [("rosstat_gov", True), ("cbr_press", False)])
def test_scoped_trust_actually_reaches_the_collector(monkeypatch, sid, scoped):
    """🔴 НЕДОСТАВКА, А НЕ ЛОГИКА. `net/ru_gov_tls.verify_for` лежал в
    репозитории с 17.08 и не имел НИ ОДНОГО боевого вызова: правку объявили
    сделанной, а сборщик продолжал ходить на штатном хранилище.

    🔴 ПЕРЕПИСАН 21.08.2026 С ДЕРЕВА РАЗБОРА НА ЗАПУСК. Прежняя редакция
    смотрела в AST `rss.py` и требовала, чтобы у КАЖДОГО `httpx.get`
    аргумент `verify` был вызовом с именем `verify_for`. Она охраняла
    написание: `verify=_trust(url)`, `**_tls_kwargs(url)` или второй сборщик
    в соседнем модуле проехали бы зелёными, а трафик пошёл бы на штатном
    хранилище — ровно та недоставка, ради которой сторож заведён.

    Теперь сборщик ЗАПУСКАЕТСЯ с подменённым `httpx.get`, и проверяется, что
    реально доехало до сети: госсайту РФ — свой контекст, всем прочим —
    штатная проверка.
    """
    import httpx

    from finengine.skills.news.connectors.rss import RSSConnector

    seen: dict = {}

    class _Resp:
        status_code = 200
        content = b"<rss version='2.0'><channel><title>t</title></channel></rss>"

    def _spy(url, **kwargs):
        seen["url"] = url
        seen.update(kwargs)
        return _Resp()

    monkeypatch.setattr(httpx, "get", _spy)
    RSSConnector(_source(sid)).fetch()

    assert seen, f"{sid}: сборщик не сходил в сеть — проверка не о том"
    assert "verify" in seen, (
        f"{sid}: сборщик ходит на штатном хранилище по умолчанию — "
        "государственные ленты РФ отвалятся по TLS")
    got = seen["verify"]
    if scoped:
        assert got is not True and isinstance(got, ssl.SSLContext), (
            f"{sid}: до сети доехало доверие {got!r}, а нужен контекст с "
            "сертификатом Минцифры — иначе CERTIFICATE_VERIFY_FAILED")
    else:
        assert got is True, (
            f"{sid}: не государственный адрес получил расширенное доверие "
            f"({got!r}) — туда уходят ключи и документы")


def test_the_collector_never_switches_verification_off():
    """Соблазн «просто verify=False» закрывает ту же ошибку ценой перехвата
    всего остального трафика — туда уходят ключи и документы."""
    tree = ast.parse(_RSS_PY.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.keyword) and node.arg == "verify":
            assert not (isinstance(node.value, ast.Constant)
                        and node.value.value is False), "проверка отключена целиком"


# ─────────────────────────── Минфин США ───────────────────────────


def test_treasury_is_switched_on():
    """Отрицательный контроль: вернул `active: false` — краснеет. Выключенный
    источник и был причиной строки «мониторинг продолжается»."""
    assert _source("ustreasury_blog").active, (
        "Минфин США снова выключен — выпуск обещает мониторинг, которого нет")


@pytest.mark.parametrize("dead", [
    "treasury.gov/connect/blog",        # мягкий 404, блога больше нет
    "home.treasury.gov/rss.xml",        # 200 + bozo=False, но внутри FAQ SSBCI
    "ofac.treasury.gov/rss.xml",        # 200 + bozo=False, свежайшая 01.07.2025
    "government.ru/department/456",     # лента ПРО Росстат, не Росстата
])
def test_feeds_that_look_alive_but_are_empty_do_not_come_back(dead):
    """Три из четырёх адресов отдают HTTP 200 и разбираются без ошибок —
    именно поэтому их легко вернуть копипастой и не заметить."""
    urls = " ".join(s.url or "" for s in _cfg.load().sources)
    assert dead not in urls, f"вернулся адрес без публикаций: {dead}"


# ─────────────────── Каталог в целом: выключенные и HTML ───────────────────


def _sources_section() -> list[str]:
    lines = _YAML.read_text(encoding="utf-8").splitlines()
    end = next((i for i, ln in enumerate(lines) if ln.startswith("entities:")),
               len(lines))
    return lines[:end]


def _blocks() -> list[tuple[str, list[str], list[str]]]:
    """(id, строки блока, непрерывный хвост комментариев над блоком)."""
    lines = _sources_section()
    starts = [i for i, ln in enumerate(lines) if ln.lstrip().startswith("- id:")]
    out: list[tuple[str, list[str], list[str]]] = []
    for n, start in enumerate(starts):
        stop = starts[n + 1] if n + 1 < len(starts) else len(lines)
        sid = lines[start].split("- id:", 1)[1].strip()
        above: list[str] = []
        j = start - 1
        while j >= 0 and lines[j].lstrip().startswith("#"):
            above.append(lines[j])
            j -= 1
        out.append((sid, lines[start:stop], above))
    return out


def _reason_text(block: list[str], above: list[str]) -> str:
    """Текст комментариев блока. Баннеры разделов («# ===== US — Wires =====»)
    отброшены: они стоят над каждым первым источником секции и объяснением
    отключения не являются."""
    out = []
    for line in block + above:
        if "#" not in line:
            continue
        text = line.split("#", 1)[1].strip()
        if text.startswith("=") or not text:
            continue
        out.append(text)
    return " ".join(out)


def test_every_disabled_source_says_why_and_when():
    """Отрицательный контроль: убрал причину у одного `active: false` —
    краснеет.

    Эта строка — ИСТОЧНИК ПРАВДЫ для фразы выпуска «источник временно
    отключён»: без неё читателю (и следующему разработчику) неизвестно, что
    именно проверяли и почему выключили. Так и простоял Минфин США —
    с диагнозом «битый XML», который оказался неверным.
    """
    silent = [
        sid for sid, block, above in _blocks()
        if any(re.match(r"\s*active:\s*false", ln) for ln in block)
        and len(_reason_text(block, above)) < 20
    ]
    assert not silent, f"выключены без объяснения причины: {silent}"


def test_html_sources_either_have_an_extractor_or_stay_off():
    """Отрицательный контроль: поставил `pboc` в active: true — краснеет.

    `connectors/html.py` берёт разборщик из `_EXTRACTORS` по id источника и
    для незарегистрированного возвращает [] с warning, не сходив в сеть.
    Включённый html-источник без разборщика — «живой и пустой»: ровно тот
    класс отказа, которым `reuters_business` жил с момента заведения.
    """
    from finengine.skills.news.connectors.html import _EXTRACTORS

    orphans = [
        s.id for s in _cfg.load().sources
        if s.connector == "html" and s.active and s.id not in _EXTRACTORS
    ]
    assert not orphans, (
        f"html-источники включены без разборщика: {orphans} — они будут "
        "числиться исправными и молча отдавать ноль")


def test_regulator_block_points_at_ids_that_exist():
    """Блок «Регуляторы» прибит к id источников списком в render.py. Смена id
    в каталоге (соблазн: `ustreasury_blog` → `ustreasury_press`) оставила бы
    там висячую ссылку, и раздел регулятора замолчал бы навсегда."""
    from finengine.skills.news.render import _REGULATOR_SPEC

    known = {s.id for s in _cfg.load().sources}
    for name, _region, sids, _focus in _REGULATOR_SPEC:
        missing = [sid for sid in sids if sid not in known]
        assert not missing, f"{name}: в каталоге нет источников {missing}"
