"""Плагин `aifa-engine` переносит вызов в службу и её текст — модели (CONTRACTS.md §3.0, C3.0)."""

from __future__ import annotations

import asyncio
import inspect
import json

import httpx
import pytest
from aifa_engine_support import ROOT_TASK, RUN_ID, load_plugin, registered_tools, tool_context

from ouroboros.observability import aef_tracing

SERVICE = "http://engine:8000"


@pytest.fixture(autouse=True)
def service_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Адрес и токен службы — из окружения (R20); «кто я» отвечает ядро, его ответ подставлен."""
    monkeypatch.setenv("AIFA_ENGINE_URL", SERVICE)
    monkeypatch.setenv("AIFA_ENGINE_TOKEN", "secret-token")
    monkeypatch.setattr(aef_tracing, "current_agent_id", lambda: "aifa-agent-1")


def run(tool: str, handler, ctx=None, **args: object) -> str:
    call = load_plugin().EngineCall(tool, transport=httpx.MockTransport(handler))
    return asyncio.run(call(ctx or tool_context(), **args))


def reply(text: str, status: int = 200):
    return lambda request: httpx.Response(status, text=text, headers={"content-type": "text/plain; charset=utf-8"})


def test_the_call_goes_to_the_internal_api_with_the_context_and_arguments_of_the_contract() -> None:
    """§3.0: путь `/internal/ouro/v1/<tool>`, тело `{ctx{…, run_id}, args}`, `run_id` из `transport.source_message_id`."""
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, text="ГОТОВО")

    run("aifa_wait", handler, tool_context(root_task_id="parent-root", task_depth=1), job_id="j1", wait_sec=30)

    (request,) = seen
    assert (request.method, str(request.url)) == ("POST", f"{SERVICE}/internal/ouro/v1/aifa_wait")
    assert json.loads(request.content) == {
        "ctx": {
            "owner_id": "7",
            "agent_id": "main",
            "chat_id": "project-1",
            "task_id": ROOT_TASK,
            "root_task_id": "parent-root",
            "task_depth": 1,
            "run_id": RUN_ID,
        },
        "args": {"job_id": "j1", "wait_sec": 30},
    }


def test_a_root_call_has_an_empty_root_task_id_not_a_missing_one() -> None:
    """§3.0: у корня `ctx.root_task_id` пуст — служба узнаёт корень из `task_id` (ADR-0021 п. 3)."""
    seen: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        return httpx.Response(200, text="ГОТОВО")

    run("aifa_goal", handler)

    assert seen[0]["ctx"]["root_task_id"] == ""
    assert seen[0]["args"] == {}


def test_the_service_text_comes_back_to_the_model_unchanged() -> None:
    """§3.0: «плагин возвращает текст без изменений» — многострочный, с таблицей, любым кодом."""
    text = "ГОТОВО\n## Итог\nРасчёт выполнен.\n\nid | тип\nart-1 | ExtractedMetric  \n"

    assert run("aifa_goal", reply(text)) == text


@pytest.mark.parametrize("status", [401, 404, 500, 503])
def test_a_coded_refusal_of_the_service_is_passed_on_whatever_the_http_status(status: int) -> None:
    """§3.0: код — первая строка текста; статус HTTP плагин не истолковывает."""
    text = "ОТКАЗ чужой_ход\nЭтот вызов не относится к ходу, который сейчас идёт."

    assert run("aifa_goal", reply(text, status)) == text


@pytest.mark.parametrize(
    "failure",
    [httpx.ConnectError("refused"), httpx.ReadTimeout("slow"), httpx.RemoteProtocolError("closed")],
    ids=["no connection", "timeout", "closed mid-reply"],
)
def test_a_network_failure_is_a_service_fault_the_model_can_retry(failure: httpx.HTTPError) -> None:
    """§3.0: сетевой сбой → `СБОЙ_СЛУЖБЫ: <кратко>; повтори вызов`, без исключения в цикл ouroboros."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise failure

    result = run("aifa_goal", handler)

    assert result.startswith("СБОЙ_СЛУЖБЫ: ") and result.endswith("; повтори вызов")
    assert "\n" not in result


@pytest.mark.parametrize("missing", ["AIFA_ENGINE_URL", "AIFA_ENGINE_TOKEN"])
def test_a_missing_address_or_token_is_a_service_fault_not_a_crash(
    missing: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Адрес и токен — из окружения (R20); без них вызов не уходит, а модель получает понятный сбой."""
    monkeypatch.delenv(missing)

    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("запрос не должен уйти")

    result = run("aifa_goal", handler)

    assert result.startswith("СБОЙ_СЛУЖБЫ: ") and missing in result


def test_a_stop_during_a_long_wait_drops_the_request_and_says_so() -> None:
    """ADR-0047 п. 4: службе уходит один запрос на весь срок; плагин между опросами смотрит `stop_requested()`."""

    async def hang(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(30)
        return httpx.Response(200, text="ГОТОВО")

    call = load_plugin().EngineCall("aifa_wait", transport=httpx.MockTransport(hang), stop_poll_sec=0.01)
    ctx = tool_context(stop_after=3)

    result = asyncio.run(asyncio.wait_for(call(ctx, job_id="j1", wait_sec=240), timeout=5))

    assert result.startswith("СБОЙ_СЛУЖБЫ: ") and "остановлен" in result
    assert ctx.stop_requested.seen > 3, "«Стоп» опрашивался, пока шёл запрос"


def test_an_answer_that_arrives_before_the_stop_is_returned_as_it_is() -> None:
    """Ответ, пришедший раньше «Стопа», не теряется: плагин не подменяет его сбоем."""
    ctx = tool_context(stop_after=5)

    assert run("aifa_goal", reply("ГОТОВО"), ctx) == "ГОТОВО"


def test_the_handler_is_what_the_dispatcher_calls_with_a_leading_context() -> None:
    """Диспетчер ядра зовёт `handler(ctx, **args)` и ждёт корутину; форма вызова — часть регистрации."""
    for name, tool in registered_tools().items():
        parameters = list(inspect.signature(tool.handler).parameters)
        assert parameters[0] == "ctx", name
        assert inspect.iscoroutinefunction(tool.handler.__call__), name


@pytest.mark.parametrize(
    ("tool", "args"),
    [
        ("aifa_wait", {"task_id": "child-1", "wait_sec": 30}),
        (
            "aifa_report",
            {"label": "eph.margin", "status": "error", "note": "деление на ноль", "code": "def run(i): ..."},
        ),
        (
            "aifa_register_atom",
            {
                "name": "eph.margin",
                "purpose": "маржа",
                "requires": [{"input_type": "ExtractedMetric", "concept": "EBITDA"}],
                "output_type": "dyn:margin",
                "output_schema": {"type": "object"},
                "code": "def run(i): return {}",
            },
        ),
    ],
)
def test_the_tools_of_a_subtask_pass_their_arguments_to_the_service_as_they_are(tool: str, args: dict) -> None:
    """Шаг 16, §3.1: плагин остаётся тонким клиентом — ни разбора, ни правил; аргументы подзадачи уходят в `args` как есть."""
    seen: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        return httpx.Response(200, text="ГОТОВО")

    run(tool, handler, tool_context(root_task_id="parent-root", task_depth=1), **args)

    assert seen[0]["args"] == args
    assert seen[0]["ctx"]["task_depth"] == 1
