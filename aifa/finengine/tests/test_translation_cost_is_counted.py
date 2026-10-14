"""Перевод заголовков попадает в стоимость прогона (17.08.2026).

ЧТО БЫЛО. `_llm_translate_batch` брал ответ как `text, _ = ...` — usage уходил
в никуда. В журнале прогона стоимость была занижена ровно на перевод: сам
дайджест считал только классификацию.

Сумма невелика — перевод идёт на дешёвой `deepseek-chat-v3`, — но «не считаем,
потому что мало» это как раз то, из-за чего потом не сходятся цифры. Сегодня я
на этом уже обжёгся: сравнил сумму по артефактам с пожизненным расходом ключа и
заявил владельцу, что журнал врёт в сто раз. Знаменатели были разные, вывод
неправомерен — но осадок в том, что учёт и правда неполон.
"""
from __future__ import annotations

import asyncio

from finengine.llm import openrouter as _or
from finengine.skills.news import render


def _reset():
    render.take_translation_usage()


def test_usage_accumulates_across_calls(monkeypatch, tmp_path):
    """Отрицательный контроль: вернул `text, _ =` — счётчик остаётся нулевым."""
    _reset()

    async def _fake(prompt="", system=None, **kw):  # noqa: ANN001, ANN003
        return '{"a": "б"}', {"in": 100, "out": 20, "cost_usd": 0.001}

    # `_llm` импортируется ВНУТРИ функции — подменяем сам модуль.
    monkeypatch.setattr(_or, "complete_with_usage", _fake)
    from finengine import config as _config
    monkeypatch.setattr(_config.settings, "data_dir", str(tmp_path))

    async def _run():
        await render._llm_translate_batch(["Fed holds rates"])
        await render._llm_translate_batch(["ECB keeps policy"])

    asyncio.run(_run())
    got = render.take_translation_usage()
    assert got["in"] == 200 and got["out"] == 40
    assert abs(got["cost_usd"] - 0.002) < 1e-9


def test_taking_the_usage_resets_it():
    """Следующий прогон в том же процессе не должен унаследовать чужие числа —
    иначе стоимость поедет вверх от прогона к прогону."""
    _reset()
    assert render.take_translation_usage() == {"in": 0, "out": 0, "cost_usd": 0.0}


def test_failed_translation_adds_nothing(monkeypatch, tmp_path):
    """Отказ модели не должен приписывать прогону расход."""
    _reset()

    async def _boom(**kw):  # noqa: ANN003
        raise RuntimeError("модель недоступна")

    monkeypatch.setattr(_or, "complete_with_usage", _boom)
    from finengine import config as _config
    monkeypatch.setattr(_config.settings, "data_dir", str(tmp_path))

    asyncio.run(render._llm_translate_batch(["Fed holds rates"]))
    assert render.take_translation_usage()["in"] == 0


def test_pipeline_actually_adds_it_to_the_run_total():
    """ГЛАВНОЕ: счётчик бесполезен, если конвейер его не забирает. Ровно на
    этом разрыве «посчитали, но не доехало» мы горели весь день."""
    import inspect

    from finengine.skills.news import pipeline

    src = inspect.getsource(pipeline)
    assert "take_translation_usage" in src, "конвейер не забирает расход перевода"
    assert "llm_in += " in src and "cost += " in src, (
        "забрал, но не прибавил к итогу прогона")


# ─── модель перевода и её бюджет (18.08.2026) ───────────────────────────


def test_translation_uses_the_same_model_knob_as_classification():
    """Владелец: «на перевод заголовков поставь v4-flash тоже». Обе стадии
    дайджеста ходят к модели, и обе теперь берут её из одной настройки —
    иначе «поменяли модель» означало бы поменять в одном месте из двух."""
    import inspect

    from finengine.config import settings

    src = inspect.getsource(render._llm_translate_batch)
    assert "settings.news_classify_model" in src, (
        "перевод снова прибит к литералу — смена модели его не заденет")
    assert settings.news_classify_model, "настройка модели дайджеста пуста"


def test_translation_budget_survives_a_reasoning_model():
    """🔴 ЗАМЕР НА ЖИВОМ ПРОДЕ, а не рассуждение.

    v4-flash — рассуждающая, и токены размышлений идут в тот же счёт, что и
    ответ. Прогон на пяти настоящих заголовках регуляторов (ФРС, ЕЦБ, Минфин
    США) через прод-бэкенд 18.08:

        бюджет 2000 → ModelSilent, finish_reason=length, 6861 знак размышлений
        бюджет 4000 → все 5 переведены
        бюджет 8000 → все 5 переведены

    При отказе `_llm_translate_batch` возвращает {} молча, и заголовки
    регуляторов остались бы в выпуске английскими — то есть дефект выглядел бы
    как «перевод не настроен», а не как отказ модели. Ровно этой же ошибкой
    17.08 был куплен бюджет классификации (9000 → 24000).
    """
    import inspect

    src = inspect.getsource(render._llm_translate_batch)
    assert "max_output=8000" in src, (
        "бюджет перевода вернулся к тесному — рассуждающая модель замолчит")
    assert "размышлен" in src, (
        "исчезло объяснение, ПОЧЕМУ бюджет такой — вернут 2000 при следующей "
        "чистке кода")
