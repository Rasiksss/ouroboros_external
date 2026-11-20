import asyncio
import json
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest


@pytest.fixture
def audit(tmp_path, monkeypatch):
    monkeypatch.setenv("OUROBOROS_LLM_AUDIT_DIR", str(tmp_path))
    from ouroboros.observability import llm_audit
    return llm_audit


def records(tmp_path, kind):
    return [json.loads(p.read_text(encoding="utf-8")) for p in tmp_path.rglob("*.json")
            if json.loads(p.read_text(encoding="utf-8"))["kind"] == kind]


def test_full_input_and_task_summary(audit, tmp_path, caplog):
    from new_core.tools.schema import ToolSchema
    messages = [{"role": "system", "content": "secret full prompt " * 10000}]
    answer = ({"content": "answer"}, {"prompt_tokens": 12, "completion_tokens": 3})

    @audit.audit_llm
    def chat(messages, model, tools=None, max_tokens=100):
        audit.mark_provider_dispatch({"provider": "test"})
        assert records(tmp_path, "llm_call")[0]["status"] == "running"
        return answer

    @audit.audit_task
    def run(task):
        return chat(messages, "model", [ToolSchema("lookup", "description", {"type": "object"})])

    assert run({"id": "root", "scope": {"owner_id": "owner", "agent_id": "agent"}}) == answer
    call, = records(tmp_path, "llm_call")
    summary, = records(tmp_path, "task")
    assert call["input"]["messages"] == messages
    assert call["input"]["tools"][0]["function"]["name"] == "lookup"
    assert call["input"]["max_tokens"] == 100
    assert call["task_id"] == "root" and call["task_role"] == "orchestrator"
    assert call["status"] == "ok" and call["usage"] == answer[1]
    assert call["duration_ms"] >= call["provider_duration_ms"] >= 0
    assert call["pre_dispatch_duration_ms"] >= 0
    assert summary["llm_calls"] == 1 and summary["llm_duration_ms"] == call["duration_ms"]
    assert summary["status"] == "ok" and "secret" not in caplog.text


@pytest.mark.parametrize("asynchronous", [False, True])
def test_errors_propagate_and_leave_record(audit, tmp_path, asynchronous):
    failure = ValueError("secret failure text")

    @audit.audit_llm
    def sync(messages, model):
        raise failure

    @audit.audit_llm
    async def async_call(messages, model):
        raise failure

    with pytest.raises(ValueError) as caught:
        if asynchronous:
            asyncio.run(async_call([], "model"))
        else:
            sync([], "model")
    assert caught.value is failure
    call, = records(tmp_path, "llm_call")
    assert call["status"] == "error" and call["error_type"] == "ValueError"
    assert call["provider_duration_ms"] is None
    assert "secret failure" not in json.dumps(call)


def test_async_cancellation_is_recorded(audit, tmp_path):
    @audit.audit_llm
    async def call(messages, model):
        raise asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(call([], "model"))
    assert records(tmp_path, "llm_call")[0]["status"] == "cancelled"


def test_disabled_does_not_serialize_or_write(audit, tmp_path, monkeypatch):
    monkeypatch.delenv("OUROBOROS_LLM_AUDIT_DIR")

    @audit.audit_llm
    def call(messages, model):
        return "ok"
    assert call([object()], "model") == "ok"
    assert not list(tmp_path.iterdir())


def test_unwritable_destination_does_not_break_model(audit, tmp_path, monkeypatch, caplog):
    blocked = tmp_path / "file"
    blocked.write_text("not a directory")
    monkeypatch.setenv("OUROBOROS_LLM_AUDIT_DIR", str(blocked))

    @audit.audit_llm
    def call(messages, model):
        return ({}, {})
    assert call([], "model") == ({}, {})
    assert "event=llm_audit_write_failed" in caplog.text


def test_parallel_tasks_have_distinct_files_and_context(audit, tmp_path):
    @audit.audit_llm
    def call(messages, model):
        return ({}, {})

    @audit.audit_task
    def run(task):
        call([{"content": task["id"]}], "model")

    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(run, [{"id": f"task/{i}", "parent_task_id": "root",
                             "description": "Навык: distribution:aifa-plan-author. План"} for i in range(8)]))
    calls = records(tmp_path, "llm_call")
    assert len(calls) == 8 and len(records(tmp_path, "task")) == 8
    for call in calls:
        assert call["task_id"] == call["input"]["messages"][0]["content"]
        assert call["task_role"] == "planner"


@pytest.mark.parametrize("asynchronous", [False, True])
def test_llm_client_facade_is_instrumented(audit, tmp_path, monkeypatch, asynchronous):
    from contextlib import asynccontextmanager, nullcontext

    from ouroboros.llm import client
    monkeypatch.setattr(client, "_llm_concurrency_slot", lambda **kw: nullcontext())

    @asynccontextmanager
    async def slot(**kw):
        yield

    async def chat(*a, **kw):
        return ({"content": "ok"}, {})

    monkeypatch.setattr(client, "_async_llm_concurrency_slot", slot)
    adapter = SimpleNamespace(chat=lambda *a, **kw: ({"content": "ok"}, {}), async_chat=chat)
    llm = client.LLMClient(registry=SimpleNamespace(resolve=lambda model: (adapter, {"provider": "test"})))

    @audit.audit_task
    def run(task):
        return (asyncio.run(llm.async_chat([{"content": "input"}], "model")) if asynchronous
                else llm.chat([{"content": "input"}], "model"))

    assert run({"id": "task"})[0]["content"] == "ok"
    call, = records(tmp_path, "llm_call")
    assert call["provider"] == "test" and call["provider_duration_ms"] is not None
    summary, = records(tmp_path, "task")
    assert summary["llm_calls"] == 1


def test_provider_time_excludes_pre_dispatch_wait(audit, tmp_path, monkeypatch):
    clock = [0.0]
    monkeypatch.setattr(audit, "time", SimpleNamespace(monotonic=lambda: clock[0]))

    @audit.audit_llm
    def call(messages, model):
        clock[0] = 5.0
        audit.mark_provider_dispatch({"provider": "test", "resolved_model": "resolved"})
        clock[0] = 12.0
        return ({}, {})

    call([], "alias")
    record, = records(tmp_path, "llm_call")
    assert record["duration_ms"] == 12000
    assert record["pre_dispatch_duration_ms"] == 5000
    assert record["provider_duration_ms"] == 7000
    assert record["input"]["model"] == "alias" and record["resolved_model"] == "resolved"


def test_atom_author_has_separate_attempt_summary(audit, tmp_path):
    @audit.audit_task
    def run(task):
        return [SimpleNamespace(task_id=task["id"], status="failed")]

    task = {"id": "atom", "parent_task_id": "root",
            "description": "Навык: distribution:aifa-atom-author. Напиши код"}
    run(task)
    run(task)
    summaries = records(tmp_path, "task")
    assert len(summaries) == 2
    assert all(item["task_role"] == "atom_author" and item["task_status"] == "failed" for item in summaries)
