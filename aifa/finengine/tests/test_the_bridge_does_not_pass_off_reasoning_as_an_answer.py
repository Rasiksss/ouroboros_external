"""Мост станции не подставляет размышления вместо ответа (19.08.2026).

ЦЕНА, КОТОРАЯ ЭТО КУПИЛА. Из инлайнового пути подстановку вычистили 11.08 после
прогона ФосАгро: токены размышлений считаются в общий потолок вывода, модель
израсходовала его на монолог и до ответа не дошла, а код молча отдал монолог как
отчёт. Разбор не нашёл ни одного раздела, карточка вышла пустой, в диалоге
осталось «Here's a thinking process:» и обрыв на полуслове.

На станции подмена прожила дольше — мост деплоится отдельно от кода, и правка
`openrouter.py` до него не доехала. Решение здесь ТО ЖЕ, а не второе: пустой
ответ — отказ; размышления в лог, наружу не отдаём.
"""
from __future__ import annotations

import asyncio
import importlib.util
import json
from pathlib import Path

import pytest

_BRIDGE = (
    Path(__file__).resolve().parents[1]
    / "deploy" / "station-bridge" / "bridge.py"
)

_THINKING = "Here's a thinking process: I need to make sure I"


def _bridge():
    spec = importlib.util.spec_from_file_location("station_bridge_daemon", _BRIDGE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _Answer:
    """Ответ инфпрокси, какой его видит демон."""

    def __init__(self, message: dict, finish_reason: str = "stop") -> None:
        self._payload = {
            "choices": [{"message": message, "finish_reason": finish_reason}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 20},
        }

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return self._payload


class _Upstream:
    def __init__(self, answer: _Answer) -> None:
        self._answer = answer

    async def post(self, *_args, **_kw) -> _Answer:
        return self._answer


def _serve(tmp_path: Path, message: dict, finish_reason: str = "stop") -> dict:
    """Один запрос через демон: записка → вызов → ответная записка."""
    bridge = _bridge()
    req = tmp_path / "rid1.req.json"
    req.write_text(json.dumps({"id": "rid1", "messages": [{"role": "user", "content": "?"}]}),
                   encoding="utf-8")
    asyncio.run(bridge._service(str(req), _Upstream(_Answer(message, finish_reason))))
    return json.loads((tmp_path / "rid1.resp.json").read_text(encoding="utf-8"))


def test_an_answer_travels_as_before(tmp_path):
    """Штатный случай не тронут: есть ответ — он и уезжает в комнату."""
    out = _serve(tmp_path, {"content": '{"metrics": {}}', "reasoning": _THINKING})
    assert out["text"] == '{"metrics": {}}'
    assert "error" not in out


def test_empty_answer_with_reasoning_is_a_refusal(tmp_path):
    """ГЛАВНОЕ. Пустой ответ при непустых размышлениях — отказ, а не результат."""
    out = _serve(tmp_path, {"content": "", "reasoning": _THINKING}, finish_reason="length")
    assert "text" not in out, "мост снова отдал что-то вместо отказа"
    assert out["error"], "отказ обязан быть назван"


def test_the_monologue_does_not_leak_into_the_room(tmp_path):
    """Размышления — в лог демона, но не в записку: текст, попавший в комнату,
    дальше неотличим от ответа модели."""
    out = _serve(tmp_path, {"content": "", "reasoning": _THINKING}, finish_reason="length")
    assert _THINKING not in json.dumps(out, ensure_ascii=False)


def test_the_refusal_names_what_happened(tmp_path):
    """Отказ без причины разбирать нечем: у инлайнового пути в тексте стоят
    `finish_reason` и объём размышлений — здесь то же самое."""
    out = _serve(tmp_path, {"content": "", "reasoning": _THINKING}, finish_reason="length")
    assert "length" in out["error"]
    assert str(len(_THINKING)) in out["error"]


@pytest.mark.parametrize("message", [
    {"content": "", "reasoning": ""},
    {"content": None, "reasoning": None},
    {},
])
def test_silence_without_reasoning_is_a_refusal_too(tmp_path, message):
    """Пустой ответ остаётся отказом и тогда, когда размышлений нет вовсе:
    раньше эта ветка отдавала пустую строку как успех."""
    out = _serve(tmp_path, message)
    assert "text" not in out
    assert out["error"]
