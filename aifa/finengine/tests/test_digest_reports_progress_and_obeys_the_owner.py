"""Шесть минут работы не выглядят зависанием, и схема не врёт (24.08.2026).

ЖАЛОБА ВЛАДЕЛЬЦА: «делал новый прогон — ответ завис на 5 минут». Ничего не
зависало: прогон 24.08 08:46:13→08:51:51 отработал ровно 5 минут 38 секунд и
закрылся без ошибки. Зависанием это выглядело потому, что чат всё это время
молчал: календарь свои стадии показывает (`ctx.progress`), дайджест не
показывал.

🔴 И ВТОРОЕ, ХУДШЕЕ. Тот же прогон снова собрал 3902 новости вместо 671 — то
есть окно 48 часов вместо сохранённых владельцем восьми, хотя правка «читать
настройки владельца» была выкачена двумя днями раньше и работала.

Врала СХЕМА инструмента. В ней стояло `"since_hours": {"default": 48}`, модель
добросовестно передавала 48 при каждом вызове, а явно переданный аргумент по
нашему же правилу сильнее сохранённых настроек. Обработчик читал сценарий
верно — до него доезжало чужое число.

Это третий заход одной и той же болезни «умеет, но не доехало» на одном и том
же параметре: сначала обработчик не знал про сценарий, потом настройку
перебивал жёсткий литерал в вызове, теперь — умолчание в схеме. Поэтому
сторож смотрит на СХЕМУ, которую видит модель, а не только на код.

`top_n` из видимых модели полей снят целиком: это ручка стоимости и полноты
(`news_top_candidates`), решать её модели не за что, а её `maximum: 60` вдобавок
зажимал настроенные 100.
"""
from __future__ import annotations

import asyncio


from finengine.llm.tools import REGISTRY, ToolContext
from finengine.skills.news import pipeline

_TOOL = "daily_news_digest"


def _props() -> dict:
    return REGISTRY[_TOOL].parameters["properties"]


def test_the_schema_does_not_advertise_a_window_default():
    """Отрицательный контроль: вернул `"default": 48` — краснеет.

    Умолчание в схеме модель читает как «передавай это значение», и оно
    перебивает настройки владельца.
    """
    assert "default" not in _props()["since_hours"], (
        "схема снова объявляет умолчание окна — модель будет слать его каждый "
        "раз, и настройки страницы автоматизаций перестанут действовать")


def test_the_schema_tells_the_model_when_to_stay_silent():
    """Снять умолчание мало: модель должна ПОНИМАТЬ, что молчание — это выбор
    в пользу настроек владельца, а не пропуск обязательного поля."""
    d = _props()["since_hours"]["description"]
    assert "НЕ ПЕРЕДАВАЙ" in d and "настройки владельца" in d


def test_the_cost_knob_is_not_exposed_to_the_model():
    """`top_n` — ручка стоимости владельца. Её `maximum: 60` вдобавок зажимал
    настроенные 100 кандидатов."""
    assert "top_n" not in _props(), "потолок кандидатов снова виден модели"


def test_the_description_names_the_real_duration():
    """Обещание «~20-60с» на работе в 5-7 минут — причина, по которой ожидание
    читается как поломка, и повод для модели дёрнуть инструмент повторно."""
    d = REGISTRY[_TOOL].description
    assert "5-7 МИНУТ" in d, "описание снова обещает секунды вместо минут"
    assert "20-60с" not in d


def test_the_run_reports_its_stages(monkeypatch):
    """Отрицательный контроль: убрал `progress=_tick` из вызова — краснеет.

    Без стадий шесть минут работы неотличимы от зависания.
    """
    said: list[str] = []

    async def _fake(**kw):
        cb = kw.get("progress")
        assert cb is not None, "конвейер позван без канала прогресса"
        cb("этап")
        return {"run_id": "r", "markdown": "#", "items_raw": 0, "items_kept": 0,
                "cost_usd": 0.0, "error": None}

    monkeypatch.setattr(pipeline, "run_daily_news_pipeline", _fake)
    monkeypatch.setattr(pipeline, "get_cached_digest", lambda **kw: None)

    class _Q:
        def put_nowait(self, text):
            said.append(text)

    ctx = ToolContext(user_id=None)
    ctx.progress = _Q()
    asyncio.run(REGISTRY[_TOOL].handler({"fresh": True}, ctx))

    assert said == ["этап"], f"стадии не доехали до чата: {said}"


def test_progress_failure_never_breaks_the_run(monkeypatch):
    """Обратная связь — украшение, а не условие работы: если канал чата умер,
    выпуск обязан собраться."""
    calls: list[str] = []

    def _broken(text):
        calls.append(text)
        raise RuntimeError("канал закрыт")

    async def _run():
        return await pipeline.run_daily_news_pipeline(
            since_hours=8, progress=_broken)

    monkeypatch.setattr(pipeline._db, "init_schema", lambda: None)
    monkeypatch.setattr(pipeline._db, "start_run", lambda *a, **kw: None)
    monkeypatch.setattr(pipeline._db, "finish_run", lambda *a, **kw: None)

    async def _nothing(**kw):
        return []

    monkeypatch.setattr(pipeline._collect, "collect_all", _nothing)

    out = asyncio.run(_run())
    assert calls, "прогресс даже не попытались отправить"
    assert out["error"] is None, f"упавший канал уронил прогон: {out['error']}"
