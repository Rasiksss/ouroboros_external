"""Отметка хода называет инструменты поимённо (#235).

ПОЧЕМУ ЭТОТ ФАЙЛ СУЩЕСТВУЕТ. В гейтвей-петле тулы исполняет гейтвей, и раннер
их не видит. Отметка хода печатала `tools=0` буквальной константой — то есть
отвечала на вопрос «сколько вызовов ОСТАЛОСЬ» (на финальном ходе всегда ноль),
притворяясь ответом на «сколько их БЫЛО». По логу бэкенда прогон разобрать было
нечем: пришлось лезть в трассу ядра на VPS, чтобы узнать, что модель дважды
позвала `fe_analyze` и ни разу не открыла тело навыка.

Бэкенд знает это и сам: каждый fe_*-тул проходит через `/internal/tools/call`, а
ключ хода — свежий uuid на ход, поэтому накопление по ключу и есть накопление
по ходу.

ПРОВЕРКА СТОРОЖА ОТКАТОМ: убрать запись в `tools_call` — первый тест покраснеет;
вернуть константу `tools=0` в формат — второй.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from finengine.api import internal_tools as it
from finengine.llm.openclaw import _tools_mark
from finengine.skills import run_orchestrator as ro

GW = "agent:aifa-station:openresponses:fe-a1b2c3d4e5f60718"


@pytest.fixture
async def live_run():
    stop = asyncio.Event()

    def _runner():
        async def gen():
            yield {"type": "run_started"}
            await stop.wait()
        return gen()

    run_id = ro.start_run(_runner, kind="chat", meta={"user_id": 1, "project_id": "p"})
    yield run_id
    stop.set()


def _stub_tool(name: str):
    from finengine.llm.tools import ToolDef

    async def handler(args: dict, ctx) -> str:  # noqa: ANN001, ARG001
        return '{"ok": true}'

    return ToolDef(name=name, description="stub", parameters={"type": "object"},
                   handler=handler)


@pytest.mark.asyncio
async def test_mark_names_every_call_in_order_with_repeats(live_run, monkeypatch):
    """Повторы НЕ схлопываются: два `fe_analyze` подряд — это диагноз, не шум.

    Ровно так выглядел живой прогон 15.08, и именно повтор был уликой того, что
    модель идёт в монолит вместо навыка.
    """
    from finengine.llm import tools as _tools

    await asyncio.sleep(0)
    it.register_session(GW, user_id=1, project_id="p", run_id=live_run, session_id="")
    for name in ("fe_analyze", "fe_calc"):
        monkeypatch.setitem(_tools.REGISTRY, name, _stub_tool(name))
    monkeypatch.setattr(_tools, "_WHITELIST", {"fe_analyze", "fe_calc"}, raising=False)

    for name in ("fe_analyze", "fe_calc", "fe_analyze"):
        await it.tools_call(it.ToolCallBody(tool=name, args={}, gateway_session=GW),
                            authorization="")

    assert it.tools_called_of(GW) == ["fe_analyze", "fe_calc", "fe_analyze"]
    assert _tools_mark(GW) == "fe_analyze,fe_calc,fe_analyze"


def test_mark_is_never_a_constant():
    """Дефект был буквальным: `tools=0` в строке формата."""
    src = Path("src/finengine/llm/openclaw.py").read_text(encoding="utf-8")
    assert "tools=0" not in src, (
        "в отметке хода снова константа вместо факта — она врёт на каждом "
        "финальном ходе гейтвей-петли (#235)")


def test_mark_distinguishes_silence_from_the_other_path():
    """«Тулов не звали» и «путь не гейтвейный» — разные факты, не один.

    Слить их в общий прочерк значит потерять единственный признак, по которому
    в логе видно, какой петлёй шёл ход.
    """
    assert _tools_mark(None) == "инлайн"
    assert _tools_mark("") == "инлайн"
    assert _tools_mark("agent:aifa-station:openresponses:fe-никогда-не-был") == "нет"
