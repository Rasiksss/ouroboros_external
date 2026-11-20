import asyncio
import json
from types import SimpleNamespace

import pytest

from test_paths_layout import finengine_importable  # noqa: F401

pytestmark = pytest.mark.usefixtures("finengine_importable")


@pytest.fixture
def audit(tmp_path, monkeypatch, finengine_importable):  # noqa: F811
    monkeypatch.setenv("FINENGINE_LLM_AUDIT_DIR", str(tmp_path))
    from finengine.llm import audit
    return audit


def rows(tmp_path):
    return [json.loads(p.read_text(encoding="utf-8")) for p in tmp_path.rglob("*.json")]


def test_real_retry_saves_full_input_and_separate_attempts(audit, tmp_path, monkeypatch):
    from finengine.llm import openrouter as llm
    count = [0]

    async def create(**kwargs):
        count[0] += 1
        if count[0] == 1:
            raise RuntimeError("secret exception")
        return SimpleNamespace(choices=[SimpleNamespace(
            message=SimpleNamespace(content="answer"), finish_reason="stop")], usage=None)

    async def sleep(delay):
        pass

    monkeypatch.setattr(llm, "_client", lambda: SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=create))))
    monkeypatch.setattr(llm.asyncio, "sleep", sleep)
    prompt = "full confidential input " * 10000
    with audit.atom_context(SimpleNamespace(run_id="run", inputs=SimpleNamespace(
        extra={"owner_id": "owner", "project_id": "case", "trace_id": "trace"})),
        "read.value", [SimpleNamespace(concept="TOTAL_ASSETS", entity_ref="doc")]):
        assert asyncio.run(llm.complete_with_usage(prompt, model="glm-test", system="system", retries=2))[0] == "answer"
    records = rows(tmp_path)
    logical, = [r for r in records if r["kind"] == "llm_call"]
    attempts = [r for r in records if r["kind"] == "llm_attempt"]
    assert len(attempts) == 2 and {r["status"] for r in attempts} == {"ok", "error"}
    assert logical["input"]["prompt"] == prompt and logical["run_id"] == "run"
    assert logical["atom_name"] == "read.value" and logical["concepts"] == ["TOTAL_ASSETS"]
    assert all(r["parent_call_id"] == logical["record_id"] for r in attempts)
    assert all(r["input"]["messages"][1]["content"] == prompt for r in attempts)
    assert "secret exception" not in json.dumps(records)


def test_stream_records_until_consumed_and_does_not_buffer(audit, tmp_path, monkeypatch):
    from finengine.llm import openrouter as llm
    chunks = []

    class Stream:
        def __aiter__(self):
            return self

        async def __anext__(self):
            if len(chunks) == 2:
                raise StopAsyncIteration
            chunks.append("part")
            return SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content="part"))])

        async def close(self):
            pass

    async def create(**kwargs):
        return Stream()

    monkeypatch.setattr(llm, "_client", lambda: SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=create))))

    async def consume():
        iterator = llm.stream("prompt", system_prompt="system", model="glm-test")
        assert await anext(iterator) == "part" and len(chunks) == 1
        assert all(r["status"] == "running" for r in rows(tmp_path))
        assert [p async for p in iterator] == ["part"]

    asyncio.run(consume())
    assert len(rows(tmp_path)) == 2 and all(r["status"] == "ok" for r in rows(tmp_path))


def test_stream_early_close_is_recorded(audit, tmp_path):
    closed = []

    @audit.audit_llm()
    async def stream(prompt):
        try:
            yield "first"
            yield "second"
        finally:
            closed.append(True)

    async def consume():
        iterator = stream("prompt")
        assert await anext(iterator) == "first"
        await iterator.aclose()

    asyncio.run(consume())
    assert closed and rows(tmp_path)[0]["status"] == "cancelled"


def test_native_sync_attempt_records_usage(audit, tmp_path):
    @audit.audit_llm(kind="llm_attempt")
    def call(prompt, system=None, model=None):
        return "answer", {"in": 3, "out": 1}
    assert call("prompt", system="system", model="GigaChat")[0] == "answer"
    record, = rows(tmp_path)
    assert record["input"]["system"] == "system" and record["usage"]["in"] == 3


def test_disabled_and_storage_failure_preserve_behavior(audit, tmp_path, monkeypatch):
    @audit.audit_llm()
    async def call(prompt):
        return "answer", {}
    monkeypatch.delenv("FINENGINE_LLM_AUDIT_DIR")
    assert asyncio.run(call("prompt"))[0] == "answer" and not rows(tmp_path)
    blocked = tmp_path / "file"
    blocked.write_text("occupied")
    monkeypatch.setenv("FINENGINE_LLM_AUDIT_DIR", str(blocked))
    assert asyncio.run(call("prompt"))[0] == "answer"


def test_cancellation_and_headers(audit, tmp_path):
    @audit.audit_llm(kind="llm_attempt")
    async def request(**kwargs):
        raise asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(request(model="model", messages=[{"content": "prompt"}],
                            extra_headers={"Authorization": "secret-key", "X-Trace-Id": "trace"}))
    record, = rows(tmp_path)
    assert record["status"] == "cancelled" and "secret-key" not in json.dumps(record)


def test_parallel_atom_contexts_are_isolated(audit, tmp_path):
    from concurrent.futures import ThreadPoolExecutor

    @audit.audit_llm()
    async def call(prompt):
        return "answer", {}

    def run(index):
        ctx = SimpleNamespace(run_id=f"run/{index}", inputs=SimpleNamespace(extra={}))
        with audit.atom_context(ctx, "read.value", [SimpleNamespace(concept=f"M{index}", entity_ref="doc")]):
            asyncio.run(call(str(index)))

    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(run, range(8)))
    records = rows(tmp_path)
    assert len(records) == 8
    for record in records:
        index = record["input"]["prompt"]
        assert record["run_id"] == f"run/{index}" and record["concepts"] == [f"M{index}"]
