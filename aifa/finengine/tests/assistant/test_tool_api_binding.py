"""`tool_api`: защита, привязка вызова к ходу, роли (CONTRACTS.md §3.0, C3.0).

HTTP-клиент играет плагин; прогон — настоящий `run_orchestrator.start_run`; запись хода — в общем
кэше состояния. Инструмент в этих тестах — `aifa_wait` с неизвестным заданием: он доходит до
`ОТКАЗ задание_не_найдено` только если привязка вызов приняла.
"""
from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass, field

import pytest
from tool_harness import (  # noqa: F401 — `coordination`: фикстура окружения
    ROOT,
    TOKEN,
    Plugin,
    coordination,
    ctx_of,
    first_line,
    open_run,
)

from finengine.assistant.goal_result import GoalResult
from finengine.assistant.tool_api import ToolApi
from finengine.assistant.turn_record import TurnRecord, recall
from finengine.config import settings

pytestmark = pytest.mark.usefixtures("coordination")

WAIT = {"job_id": "feedbeef", "wait_sec": 10}


@dataclass
class RootSpy:
    """Роль `turn_gateway`: запоминает, какой корень ему назвали (`root_known`, шаг 08)."""

    named: list[tuple[str, str]] = field(default_factory=list)

    def root_known(self, run_id: str, task_id: str) -> bool:
        self.named.append((run_id, task_id))
        return True


class NoGoal:
    async def __call__(self, record: TurnRecord, emit: object) -> GoalResult:  # pragma: no cover
        raise AssertionError("привязка должна была отказать раньше")


@pytest.fixture
async def plugin() -> AsyncIterator[Plugin]:
    client = Plugin(ToolApi(RootSpy(), NoGoal()))
    yield client
    await client.close()


async def test_a_call_without_the_token_is_unauthorized(plugin: Plugin) -> None:
    """C3.0 / ADR-0028: без токена — 401."""
    run = open_run()

    response = await plugin.call("aifa_wait", ctx_of(run.run_id), WAIT, token=None)

    assert response.status_code == 401
    await run.end()


async def test_a_call_with_a_wrong_token_is_unauthorized(plugin: Plugin) -> None:
    run = open_run()

    wrong = await plugin.call("aifa_wait", ctx_of(run.run_id), WAIT, token=TOKEN + "x")
    other = await plugin.call("aifa_wait", ctx_of(run.run_id), WAIT, token="")

    assert (wrong.status_code, other.status_code) == (401, 401)
    await run.end()


async def test_an_unset_token_closes_the_api_instead_of_opening_it(
    plugin: Plugin, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ADR-0028: API меняет `Case`; пустой секрет — 503 на любой вызов, в том числе «без токена»."""
    monkeypatch.setattr(settings, "aifa_engine_token", "")
    run = open_run()

    bare = await plugin.call("aifa_wait", ctx_of(run.run_id), WAIT, token=None)
    empty = await plugin.call("aifa_wait", ctx_of(run.run_id), WAIT, token="")

    assert (bare.status_code, empty.status_code) == (503, 503)
    await run.end()


async def test_an_unauthorized_call_still_answers_in_the_grammar_of_the_contract(
    plugin: Plugin, monkeypatch: pytest.MonkeyPatch
) -> None:
    """§3.0 (P65 З2): плагин отдаёт текст ответа как есть, поэтому 401 и 503 — тоже `СБОЙ_СЛУЖБЫ`, а не JSON FastAPI."""
    run = open_run()
    wrong = await plugin.call("aifa_wait", ctx_of(run.run_id), WAIT, token="bad")
    monkeypatch.setattr(settings, "aifa_engine_token", "")
    closed = await plugin.call("aifa_wait", ctx_of(run.run_id), WAIT)

    for response in (wrong, closed):
        assert first_line(response).startswith("СБОЙ_СЛУЖБЫ: "), response.text
        assert response.headers["content-type"].startswith("text/plain")
    await run.end()


async def test_a_call_of_the_running_turn_passes_the_binding(plugin: Plugin) -> None:
    run = open_run()

    response = await plugin.call("aifa_wait", ctx_of(run.run_id), WAIT)

    assert response.status_code == 200
    assert first_line(response) == "ОТКАЗ задание_не_найдено"
    await run.end()


async def test_an_unknown_run_is_a_foreign_turn(plugin: Plugin) -> None:
    """C3.0: чужой `run_id` — `ОТКАЗ чужой_ход`."""
    response = await plugin.call("aifa_wait", ctx_of("no-such-run"), WAIT)

    assert (response.status_code, first_line(response)) == (200, "ОТКАЗ чужой_ход")


async def test_a_finished_turn_accepts_no_calls(plugin: Plugin) -> None:
    run = open_run()
    await run.end()

    response = await plugin.call("aifa_wait", ctx_of(run.run_id), WAIT)

    assert first_line(response) == "ОТКАЗ чужой_ход"


@pytest.mark.parametrize(
    "who",
    [{"owner_id": "8"}, {"chat_id": "project-2"}],
    ids=["another owner", "another chat"],
)
async def test_the_owner_and_the_chat_must_be_those_of_the_turn(
    plugin: Plugin, who: dict[str, str]
) -> None:
    run = open_run()
    ctx = {**ctx_of(run.run_id), **who}

    response = await plugin.call("aifa_wait", ctx, WAIT)

    assert first_line(response) == "ОТКАЗ чужой_ход"
    await run.end()


async def test_a_task_of_another_root_is_a_foreign_turn(plugin: Plugin) -> None:
    """C3.0: «задача не этого хода»: корень назван первым вызовом, чужой корень и подзадача чужого корня — отказ."""
    run = open_run()
    first = await plugin.call("aifa_wait", ctx_of(run.run_id, task_id=ROOT), WAIT)
    other_root = await plugin.call("aifa_wait", ctx_of(run.run_id, task_id="other-root"), WAIT)
    foreign_subtask = await plugin.call(
        "aifa_wait",
        ctx_of(run.run_id, task_id="sub-1", root_task_id="other-root", task_depth=1),
        WAIT,
    )

    assert first_line(first) == "ОТКАЗ задание_не_найдено"
    assert first_line(other_root) == "ОТКАЗ чужой_ход"
    assert first_line(foreign_subtask) == "ОТКАЗ чужой_ход"
    await run.end()


async def test_a_subtask_of_the_root_of_the_turn_passes_the_binding(plugin: Plugin) -> None:
    run = open_run()
    await plugin.call("aifa_wait", ctx_of(run.run_id, task_id=ROOT), WAIT)

    response = await plugin.call(
        "aifa_wait", ctx_of(run.run_id, task_id="sub-1", root_task_id=ROOT, task_depth=1), WAIT
    )

    assert first_line(response) == "ОТКАЗ задание_не_найдено"
    await run.end()


async def test_aifa_goal_from_a_subtask_is_not_its_role(plugin: Plugin) -> None:
    """C3.0 / ADR-0043 п. 2: `aifa_goal` — только корень (`task_depth == 0`)."""
    run = open_run()
    await plugin.call("aifa_wait", ctx_of(run.run_id, task_id=ROOT), WAIT)

    response = await plugin.call(
        "aifa_goal", ctx_of(run.run_id, task_id="sub-1", root_task_id=ROOT, task_depth=1)
    )

    assert first_line(response) == "ОТКАЗ не_та_роль"
    await run.end()


async def test_a_foreign_turn_is_refused_before_the_role_is_looked_at(plugin: Plugin) -> None:
    """Привязка раньше ролей: чужому вызову не сообщают, какие роли у хода бывают."""
    response = await plugin.call(
        "aifa_goal", ctx_of("no-such-run", task_id="sub-1", root_task_id=ROOT, task_depth=1)
    )

    assert first_line(response) == "ОТКАЗ чужой_ход"


async def test_the_first_call_names_the_root_of_the_turn_to_the_gateway() -> None:
    """Шаг 08 → 09: `root_known(run_id, task_id)` вызывается один раз, на первом вызове корня."""
    spy = RootSpy()
    client = Plugin(ToolApi(spy, NoGoal()))
    run = open_run()

    await client.call("aifa_wait", ctx_of(run.run_id, task_id=ROOT), WAIT)
    await client.call("aifa_wait", ctx_of(run.run_id, task_id=ROOT), WAIT)

    assert spy.named == [(run.run_id, ROOT)]
    saved = recall(run.run_id)
    assert saved is not None and saved.root_task_id == ROOT
    await run.end()
    await client.close()


async def test_a_call_that_the_binding_refused_does_not_name_a_root() -> None:
    spy = RootSpy()
    client = Plugin(ToolApi(spy, NoGoal()))
    run = open_run()

    await client.call("aifa_wait", {**ctx_of(run.run_id), "owner_id": "8"}, WAIT)

    assert spy.named == []
    await run.end()
    await client.close()


async def test_a_body_that_is_not_a_call_is_a_bad_argument_not_a_crash(plugin: Plugin) -> None:
    run = open_run()

    response = await plugin._client.post(
        "/internal/ouro/v1/aifa_wait",
        content=b'{"ctx": {"run_id": 5}}',
        headers={"Authorization": f"Bearer {TOKEN}"},
    )

    assert (response.status_code, first_line(response)) == (200, "ОТКАЗ неверные_аргументы")
    await run.end()


async def test_an_unknown_tool_is_not_found(plugin: Plugin) -> None:
    run = open_run()

    response = await plugin.call("aifa_nothing", ctx_of(run.run_id))

    assert response.status_code == 404
    await run.end()
