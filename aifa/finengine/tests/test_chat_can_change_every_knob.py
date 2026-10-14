"""Из чата можно менять ВСЕ ручки автоматизаций, а не половину (18.08.2026).

ВОПРОС ВЛАДЕЛЬЦА: «можно ли через модель менять настройки в живом общении в
чате? если нет, давай сделаем». Оказалось — можно, инструмент
`fe_scenario_settings` есть и обработчик принимает любую ручку, которую знает
схема сценария. Но в ОПИСАНИИ для модели были названы не все: не хватало
`event_groups` (какие типы событий показывать в календаре) и `macro_mode`
(какой блок экономики попадает в выпуск).

Модель выбирает инструмент и его аргументы по описанию. Ручка, о которой в
описании не сказано, для неё не существует — обработчик мог принять её хоть
всегда. Это тот же разрыв «умеет, но не доехало», что стоил нам #252 и #253,
только доезжать должно было ЗНАНИЕ, а не код.

Сторож связывает три места, которые обязаны совпадать: схема сценария,
описание инструмента и подсказка, которую инструмент отдаёт при `action=list`.

⚠️ ЧЕГО ЭТИ ТРИ ТЕСТА НЕ ЛОВЯТ. Первые три текстовые по природе предмета:
инвариант — совпадение ИМЕНИ ручки в схеме и в описании, которое уходит модели.
Подействует ли описание — то есть поставит ли модель ручку, услышав просьбу
словами, — тест сказать не может: это решает модель. И то, что ручка после
записи что-то меняет в прогоне, здесь тоже не проверяется.

🔴 РЕВИЗИЯ 21.08: `test_handler_validates_through_the_scenario_schema` больше
не ищет `validate_param` в `inspect.getsource`. Грепалка охраняла УПОМИНАНИЕ
имени: вызвать схему и выбросить её вердикт (`norm, _ = validate_param(...)`)
или проверять ею не ту ручку — «validate_param» на месте, тест зелёный, а из
чата выставляется то, что страница автоматизаций отвергает. Теперь обработчик
зовётся живьём на негодном значении и на годном.
"""
from __future__ import annotations

import json

import pytest

from finengine.llm.tools import REGISTRY, ToolContext
from finengine.skills.scenarios import schema, store

# Файловых запасных путей у новостей, сценариев и реестра документов больше нет
# (снесены 26.08.2026) — файлу нужна настоящая база.
pytestmark = pytest.mark.usefixtures("бд_хранилищ")


_TOOL = "fe_scenario_settings"


def _description() -> str:
    t = REGISTRY[_TOOL]
    return t.description + " " + str(t.parameters)


@pytest.mark.parametrize("knob", sorted(
    set(schema.PARAM_SCHEMAS.get("ir_calendar", {}))
    | set(schema.PARAM_SCHEMAS.get("ir_daily", {}))
))
def test_every_knob_the_schema_accepts_is_named_to_the_model(knob):
    """Отрицательный контроль: убрал `event_groups` из описания — краснеет.

    Схема — источник истины о том, что можно настроить. Если ручка есть в ней,
    но не названа модели, пользователь не сможет попросить её словами.
    """
    assert knob in _description(), (
        f"ручка «{knob}» есть в схеме, но не названа в описании инструмента — "
        "модель о ней не узнает")


def test_tool_offers_both_actions():
    props = REGISTRY[_TOOL].parameters["properties"]
    assert set(props["action"]["enum"]) == {"list", "update"}
    assert "params" in props, "менять параметры нечем"


def test_description_tells_when_to_call_it():
    """Модель зовёт инструмент по поводу, а не по имени. Повод должен быть
    описан словами пользователя — «смотри 3 недели назад», а не «patch params»."""
    d = REGISTRY[_TOOL].description
    assert "период" in d or "окно" in d
    assert "action=list" in d, "не сказано, что сначала надо взять scenario_id"


@pytest.fixture()
def scenario(tmp_path, monkeypatch):
    """Свой сценарий календаря в отдельной базе — тул owner-scoped."""
    monkeypatch.setattr(store, "db_path", lambda: tmp_path / "scenarios.db")
    store.init_schema()
    return store.create(user_id=7, name="IR Календарь", kind="ir_calendar")


async def _call(scn, params: dict) -> dict:
    out = await REGISTRY[_TOOL].handler(
        {"action": "update", "scenario_id": scn.id, "params": params},
        ToolContext(user_id=7),
    )
    return json.loads(out)


@pytest.mark.asyncio
async def test_handler_validates_through_the_scenario_schema(scenario):
    """Обработчик обязан проверять ручки ТОЙ ЖЕ схемой, что и страница
    автоматизаций: иначе из чата можно будет выставить то, что страница
    отвергает, и настройки разойдутся.

    Отрицательный контроль: обойти `validate_param` и класть значение как
    пришло — краснеют оба ассерта ниже.
    """
    # `back_days` — int 1..120 по схеме. 999 страница отвергает.
    bad = await _call(scenario, {"back_days": 999})
    assert bad.get("params", {}).get("back_days") != 999, (
        "чат записал окно вне схемы — страница автоматизаций такое отвергает, "
        f"настройки разошлись: {bad}")
    assert store.get(scenario.id).params_dict.get("back_days") != 999, (
        "негодное значение доехало до базы")

    good = await _call(scenario, {"back_days": 30})
    assert good.get("params", {}).get("back_days") == 30, (
        f"годное значение не записалось: {good}")
