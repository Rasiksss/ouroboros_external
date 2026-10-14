"""Исправный источник не выглядит сломанным из-за нашего заголовка (18.08.2026).

В прогоне на проде два источника числились павшими: `failed_names:
['MarketWatch', 'TechCrunch']`. Отдельная проверка их лент показывала 20 и 61
свежую запись — то есть ленты живы, а падение наше.

ПРИЧИНА. RSS-коннектор просил `Accept-Encoding: gzip, deflate, br`, но пакета
`brotli` в образе нет (проверено: ни `brotli`, ни `brotlicffi`). Сервер честно
отдавал `Content-Encoding: br`, httpx без декодера клал в `r.content` сжатые
байты, и feedparser сообщал «bozo=1, not well-formed (invalid token)».

    как в коннекторе (br) → enc=br    b'\\x15+C\\x00\\xe4>keo…'   ❌
    без br                → enc=gzip  b'<?xml version="1.0"…'      ✅

Опасен именно тихий вид отказа: источник попадает в «павшие», и по журналу
кажется, что сломалась ЛЕНТА. Так можно неделями чинить не то — я на этом уже
обжигался, объявив 83 источника мёртвыми по неверному замеру.

Лечится снятием `br`, а не новой зависимостью: gzip и deflate httpx
разворачивает сам, и их поддерживают все наши ленты. Замер по 70 активным
rss-источникам: снятие `br` возвращает ровно два — TechCrunch и MarketWatch.

Здесь же закрыт жёсткий «48ч» в сообщении о пустом выпуске: при ручке в 8 часов
оно врало ровно там, где читатель ищет причину пустоты.

РЕВИЗИЯ 21.08.2026. Оба сторожа искали строки в тексте модулей и оба зеленели
на мутациях, возвращающих ровно эти два дефекта (второй заголовок
`accept-encoding: …, br` рядом с первым; литерал «48ч» вместо подстановки).
Теперь коннектор и конвейер ЗАПУСКАЮТСЯ. Третий сторож — «объяснение про
brotli лежит рядом с заголовком» — снят: он охранял комментарий, то есть
краснел от правки текста и молчал от правки поведения.
"""
from __future__ import annotations

import asyncio

from finengine.skills.news import pipeline
from finengine.skills.news.config import SourceConfig
from finengine.skills.news.connectors.rss import RSSConnector


def _sent_headers(monkeypatch) -> dict:
    """Заголовки, которые `fetch` ДЕЙСТВИТЕЛЬНО отдаёт httpx."""
    import httpx

    seen: dict = {}

    class _Resp:
        status_code = 500  # дальше разбора заголовков идти незачем

    def _get(url, **kw):
        seen.update(kw.get("headers") or {})
        return _Resp()

    monkeypatch.setattr(httpx, "get", _get)
    source = SourceConfig(id="s", display_name="S", type="wire", region="world",
                          connector="rss", url="https://example.org/feed", weight=1.0)
    RSSConnector(source).fetch()
    assert seen, "коннектор не дошёл до запроса — сторож ослеп"
    return seen


def test_we_do_not_ask_for_an_encoding_we_cannot_decode(monkeypatch):
    """Отрицательный контроль: вернул `br` в заголовок — краснеет.

    Смотрим на ОТПРАВЛЕННЫЙ запрос, а не на текст модуля: второй заголовок
    `accept-encoding` рядом с первым текстовому сторожу невидим, а сервер
    после него снова присылает brotli (мутация проверена 21.08).
    """
    asked = {k: v for k, v in _sent_headers(monkeypatch).items()
             if k.lower() == "accept-encoding"}
    assert asked, "сжатие не запрошено вовсе — это лишний трафик, а не исправление"
    for key, value in asked.items():
        parts = {p.strip().split(";")[0] for p in str(value).split(",")}
        assert "br" not in parts, (
            f"{key}: коннектор снова просит brotli — тела ответов приедут "
            f"сжатыми, и живые ленты попадут в «павшие»")
        assert {"gzip", "deflate"} <= parts, f"{key}: сжатие снято целиком"


def test_empty_digest_names_the_real_window(monkeypatch):
    """Отрицательный контроль: вернул «окне 48ч» литералом — краснеет.

    Сообщение о пустом выпуске — единственное, что читатель видит, когда
    новостей нет. Названное в нём окно обязано быть настоящим, поэтому
    прогоняем конвейер с окном 8ч и читаем ЕГО выпуск.
    """
    async def _nothing_collected(**kw):
        return []

    monkeypatch.setattr(pipeline._collect, "collect_all", _nothing_collected)
    for stub in ("init_schema", "start_run", "finish_run"):
        monkeypatch.setattr(pipeline._db, stub, lambda *a, **kw: None)

    out = asyncio.run(pipeline.run_daily_news_pipeline(since_hours=8))
    assert out["error"] is None, out["error"]
    assert "окне 8ч" in out["markdown"], (
        f"окно в сообщении не настоящее: {out['markdown']!r}")
