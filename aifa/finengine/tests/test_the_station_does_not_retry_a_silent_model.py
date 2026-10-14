"""Молчание модели в станции не повторяется трижды (20.08.2026).

ЧТО БЫЛО СДЕЛАНО НАПОЛОВИНУ. 19.08 демон моста перестал выдавать размышления за
ответ: пустой `content` — отказ, а не результат. Инлайновый путь вычистили тем же
решением 11.08, но там решение состоит ИЗ ДВУХ ЧАСТЕЙ, а перенесли одну.
Вторая — отдельный тип `openrouter.ModelSilent` и `except ModelSilent: raise` в
петле ретраев, с прямо названной причиной: «повтор с тем же бюджетом даст тот же
исход — не тратим 360 с».

ЦЕНА НЕДОСТАЮЩЕЙ ПОЛОВИНЫ. Клиент комнаты (`station_bridge.complete_with_usage`)
ретраит ЛЮБОЙ `error` из ответной записки, по умолчанию три раза с бэкоффом.
Молчание модели детерминировано: бюджет тот же, промпт тот же. Значит на каждый
такой вызов уходило до трёх окон ожидания по 400 с — до двадцати минут прогона,
после которых извлечение всё равно падает. На стадии сжатия двухстадийной схемы,
где потолок вывода снят и молчание как раз и случается, это цена целого прогона.

КАК ЧИНИТСЯ, ЧТОБЫ ЭТО НЕ БЫЛО «ВТОРЫМ СПОСОБОМ». Признак ставит демон
(`silent: true` в записке) — причину знает только его сторона; клиент по
признаку поднимает ТОТ ЖЕ `ModelSilent`, что и инлайновый путь, и та же строка
`except ModelSilent: raise` выводит его из петли.

РАЗЪЕЗД ВЕРСИЙ. Мост деплоится отдельно от полки. Старый демон признака не
ставит — но он и молчания не докладывает (подставляет размышления), так что
поведение старой пары не меняется вовсе; проверка на это ниже.
"""
from __future__ import annotations

import asyncio
import importlib.util
import json
from pathlib import Path

import pytest

from finengine.llm import station_bridge
from finengine.llm.openrouter import ModelSilent

_BRIDGE = Path(__file__).resolve().parents[1] / "deploy" / "station-bridge" / "bridge.py"

_THINKING = "Here's a thinking process: I need to make sure I"


def _bridge():
    spec = importlib.util.spec_from_file_location("station_bridge_daemon", _BRIDGE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ── Сторона демона: молчание помечено, обычный отказ — нет


class _Answer:
    def __init__(self, message: dict) -> None:
        self._payload = {"choices": [{"message": message, "finish_reason": "length"}],
                         "usage": {"prompt_tokens": 10, "completion_tokens": 20}}

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return self._payload


class _Upstream:
    def __init__(self, answer) -> None:
        self._answer = answer

    async def post(self, *_args, **_kw):
        if isinstance(self._answer, Exception):
            raise self._answer
        return self._answer


def _serve(tmp_path: Path, upstream) -> dict:
    bridge = _bridge()
    req = tmp_path / "rid1.req.json"
    req.write_text(json.dumps({"id": "rid1", "messages": [{"role": "user", "content": "?"}]}),
                   encoding="utf-8")
    asyncio.run(bridge._service(str(req), upstream))
    return json.loads((tmp_path / "rid1.resp.json").read_text(encoding="utf-8"))


def test_the_daemon_marks_the_silence(tmp_path):
    """Записка обязана НАЗЫВАТЬ причину машинно, а не только словами: по словам
    комната отличить молчание от таймаута моста не может."""
    out = _serve(tmp_path, _Upstream(_Answer({"content": "", "reasoning": _THINKING})))
    assert out.get("silent") is True, "молчание не помечено — комната будет ретраить"
    assert "не выдала ответ" in out["error"]


def test_an_ordinary_failure_keeps_its_right_to_a_retry(tmp_path):
    """Обрыв связи с инфпрокси повторить СТОИТ. Пометить всё подряд значило бы
    променять один дефект на обратный: сеть моргнула — прогон умер."""
    out = _serve(tmp_path, _Upstream(TimeoutError("read timeout")))
    assert "silent" not in out
    assert out["error"]


# ── Сторона комнаты: помеченный отказ не повторяется


async def _answer_every_note(rpc: Path, answer: dict, served: list[str]) -> None:
    """Демон-подстава: на каждую записку кладёт заранее заданный ответ."""
    while True:
        for note in sorted(rpc.glob("*.req.json")):
            rid = json.loads(note.read_text(encoding="utf-8"))["id"]
            served.append(rid)
            note.unlink()
            (rpc / f"{rid}.resp.json").write_text(
                json.dumps({"id": rid, **answer}), encoding="utf-8")
        await asyncio.sleep(0.01)


async def _ask(rpc: Path, answer: dict, served: list[str], retries: int) -> str:
    daemon = asyncio.ensure_future(_answer_every_note(rpc, answer, served))
    try:
        text, _ = await station_bridge.complete_with_usage(
            prompt="дай выжимку", retries=retries)
        return text
    finally:
        daemon.cancel()


def _rpc(tmp_path: Path, monkeypatch) -> Path:
    rpc = tmp_path / ".model-rpc"
    monkeypatch.setenv("FE_RPC_DIR", str(rpc))
    monkeypatch.setenv("FE_RPC_POLL_SEC", "0.01")
    return rpc


def test_the_room_asks_once_and_gives_up(tmp_path, monkeypatch):
    """ГЛАВНОЕ. Одна записка на весь вызов — второго окна ожидания нет."""
    rpc = _rpc(tmp_path, monkeypatch)
    served: list[str] = []
    with pytest.raises(ModelSilent) as err:
        asyncio.run(_ask(rpc, {"error": "модель не выдала ответ", "silent": True},
                         served, retries=3))
    assert len(served) == 1, f"молчание повторено {len(served)} раз вместо одного"
    assert "не выдала ответ" in str(err.value)


def test_the_refusal_is_the_same_type_as_inline(tmp_path, monkeypatch):
    """Тип тот же, что у сетевого пути: вызывающий код, различающий молчание,
    не обязан знать, каким транспортом ходила модель."""
    rpc = _rpc(tmp_path, monkeypatch)
    with pytest.raises(ModelSilent):
        asyncio.run(_ask(rpc, {"error": "тишина", "silent": True}, [], retries=3))


def test_an_unmarked_error_is_still_retried(tmp_path, monkeypatch):
    """Обратная сторона — и она же случай СТАРОГО демона, который признака не
    ставит: поведение прежнее, ретраи на месте."""
    rpc = _rpc(tmp_path, monkeypatch)
    served: list[str] = []
    with pytest.raises(RuntimeError) as err:
        asyncio.run(_ask(rpc, {"error": "502 bad gateway"}, served, retries=2))
    assert not isinstance(err.value, ModelSilent)
    assert len(served) == 2, "обычный отказ перестал повторяться"


def test_a_good_answer_is_untouched(tmp_path, monkeypatch):
    """Штатный путь не задет: ответ есть — он и возвращается с первой попытки."""
    rpc = _rpc(tmp_path, monkeypatch)
    served: list[str] = []
    text = asyncio.run(_ask(rpc, {"text": "выжимка", "usage": {"in": 1, "out": 2}},
                            served, retries=3))
    assert text == "выжимка"
    assert len(served) == 1
