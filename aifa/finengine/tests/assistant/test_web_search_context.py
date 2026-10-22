"""Согласие на поиск проходит через HTTP инструмента, фоновое задание и поток исполнения."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field, replace

import pytest
from tool_harness import Plugin, coordination, ctx_of, open_run  # noqa: F401 — фикстура окружения

from finengine.assistant.loop_opening import NoDocument
from finengine.assistant.tool_api import ToolApi
from finengine.assistant.turn_record import TurnRecord, remember
from finengine.llm.openclaw import set_web_search, web_search_allowed
from finengine.skills.analysis.external import WebSearchOff
from finengine.skills.atoms.web_search import web_search_executor
from finengine.skills.spec.atom_registry import NodeArgs

pytestmark = pytest.mark.usefixtures("coordination")


class Gateway:
    def root_known(self, run_id: str, task_id: str) -> bool:
        return True


@dataclass
class SearchOpening:
    searches: list[str] = field(default_factory=list)

    async def search(self, query: str, **kwargs: object) -> list:
        self.searches.append(query)
        return []

    async def __call__(self, record: TurnRecord) -> NoDocument:
        try:
            await asyncio.to_thread(
                web_search_executor, NodeArgs(concept="ключевая ставка"), None, search=self.search,
            )
        except WebSearchOff:
            return NoDocument("Поиск выключен")
        return NoDocument("Поиск выполнен")


@pytest.mark.parametrize("enabled", [False, True])
async def test_the_tool_applies_the_turn_choice_to_background_search(enabled: bool) -> None:
    run = open_run()
    remember(replace(run.record, web_search=enabled))
    opening = SearchOpening()
    plugin = Plugin(ToolApi(Gateway(), opening))
    set_web_search(not enabled)
    try:
        response = await plugin.call("aifa_open", ctx_of(run.run_id))

        assert response.status_code == 200
        assert opening.searches == (["ключевая ставка"] if enabled else [])
        assert ("Поиск выполнен" if enabled else "Поиск выключен") in response.text
        assert web_search_allowed() is (not enabled)
    finally:
        set_web_search(True)
        await plugin.close()
        await run.end()


async def test_concurrent_turns_do_not_share_search_permission() -> None:
    allowed, denied = open_run(web_search=True), open_run(web_search=False)
    opening = SearchOpening()
    plugin = Plugin(ToolApi(Gateway(), opening))
    try:
        replies = await asyncio.gather(
            plugin.call("aifa_open", ctx_of(allowed.run_id)),
            plugin.call("aifa_open", ctx_of(denied.run_id)),
        )

        assert opening.searches == ["ключевая ставка"]
        assert "Поиск выполнен" in replies[0].text
        assert "Поиск выключен" in replies[1].text
    finally:
        await plugin.close()
        await allowed.end()
        await denied.end()
