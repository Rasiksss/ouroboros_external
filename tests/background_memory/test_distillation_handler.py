"""The distillation tool loop: what the handler does with an eligible event."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from background_memory.env import MemoryEnv
from background_memory.handlers.distillation import DistillationHandler
from background_memory.interfaces import MemoryDistillationEvent, MemoryTaskRef
from config_registry.user_settings.reader import UserSettings
from ouroboros.storage.core_memory.memory import MemoryFactory, get_memory
from tests.helpers.audit_events import sql_audit_event_repository
from tests.helpers.background_memory_db import llm_double, memory_database, mock_llm
from tests.helpers.execution_memory import sql_execution_memory_repository
from tests.helpers.shared import TEST_SCOPE, make_tool_ctx
from tests.helpers.user_settings import stub_user_settings


@pytest.fixture(autouse=True)
def _reset_long_term_memory_cache() -> None:
    """Drop the cached service so this test binds to its own runtime DB."""
    get_memory.cache_clear()


@pytest.fixture
def owner() -> UserSettings:
    """What the owner chose, read once per delivery: two models, because the fallback path needs them to differ."""
    return stub_user_settings(main_model="primary-model", fallback_model="fallback-model")


async def _raising_claim(_event) -> bool:
    raise RuntimeError("interrupted")


def _returning(answer):
    """A coroutine stand-in for ``distil``, which the handler now awaits."""

    async def _distil(_event):
        return answer

    return _distil


async def _raising_distil(_event):
    raise RuntimeError("interrupted")


def _recording(sink: list):
    async def _distil(event):
        sink.append(event)
        return None

    return _distil


def _event(
    *,
    task_text: str = "task",
    response_text: str = "A response long enough to be worth distilling. " * 4,
    trace_summary: str = "",
    source_task_id: str = "",
    job_id: str = "",
    recorded_at: float = 0.0,
) -> MemoryDistillationEvent:
    """An eligible event. The prompt renders ``trace_summary`` from ``llm_trace``."""
    return MemoryDistillationEvent(
        scope=TEST_SCOPE,
        task=MemoryTaskRef(
            job_id=job_id,
            task_id=source_task_id,
            task_type="task",
            text=task_text,
            recorded_at=recorded_at,
        ),
        response_text=response_text,
        llm_trace={"reasoning_notes": [trace_summary]} if trace_summary else {},
    )


@pytest.fixture
def db():
    yield from memory_database()


def _handler(db, tmp_path, llm, owner: UserSettings | None = None) -> DistillationHandler:
    return DistillationHandler(
        MemoryEnv(repo_dir=tmp_path, drive_root=tmp_path),
        llm,
        tasks=db.tasks,
        audit=db.audit,
        memory_headers=db.memory_headers,
        user_settings=owner or stub_user_settings(main_model="primary-model", fallback_model=""),
    )


def _settings(*, read_timeout_sec: float = 30, max_tokens: int = 32_768) -> SimpleNamespace:
    """Only the app settings a run reads."""
    return SimpleNamespace(
        OUROBOROS_MEMORY_DISTILL=True,
        OUROBOROS_BACKGROUND_LLM_HTTP_READ_TIMEOUT_SEC=read_timeout_sec,
        OUROBOROS_MEMORY_DISTILL_MAX_TOKENS=max_tokens,
    )


def _memory_events(tmp_path) -> list[dict]:
    from ouroboros.observability.event_record import list_runtime_events

    return list_runtime_events(
        log_stream="memory",
        event_type="memory_distillation",
        audit_event_repository=sql_audit_event_repository(),
    )


# --- tool loop --------------------------------------------------------------


def test_distillation_nothing_to_save_completes_with_the_round_accounted(db, tmp_path):
    llm = mock_llm()
    llm.chat.return_value = (
        {"content": "NOTHING_TO_SAVE", "tool_calls": [], "finish_reason": "stop"},
        {
            "prompt_tokens": 10,
            "completion_tokens": 2,
            "provider": "gigachat-b2bank",
            "resolved_model": "GigaChat-Pro",
        },
    )

    result = db.run(_handler(db, tmp_path, llm).distil(_event(source_task_id="dt1")))

    assert result.status == "completed"
    # The totals live on the result and its observability event; no usage event
    # is emitted anywhere — distillation has no task row to attribute spend to.
    assert result.prompt_tokens == 10
    assert result.completion_tokens == 2
    assert result.rounds == 1
    assert result.saved_count == 0
    assert result.saved_names == []


def test_distillation_saves_project_memory_via_tool_call(db, tmp_path):
    llm = mock_llm()
    first_msg = {
        "content": "",
        "finish_reason": "tool_calls",
        "tool_calls": [
            {
                "id": "call_1",
                "function": {
                    "name": "memory_save",
                    "arguments": (
                        '{"name": "deploy_incident", "memory_type": "project", '
                        '"description": "Deploy pipeline incident", "content": "Pipeline failed due to X."}'
                    ),
                },
            }
        ],
    }
    second_msg = {"content": "NOTHING_TO_SAVE", "tool_calls": [], "finish_reason": "stop"}
    llm.chat.side_effect = [
        (first_msg, {"prompt_tokens": 5, "completion_tokens": 5}),
        (second_msg, {"prompt_tokens": 5, "completion_tokens": 5}),
    ]

    result = db.run(
        _handler(db, tmp_path, llm).distil(
            _event(trace_summary="data_read status=ok confirmed source", source_task_id="dt2")
        )
    )

    assert result.status == "completed"
    assert result.rounds == 2
    assert result.saved_count == 1
    assert result.saved_names == ["deploy_incident"]

    memory = MemoryFactory().get_memory()
    content, _, _ = memory.read(TEST_SCOPE, "deploy_incident")
    assert "Pipeline failed due to X." in content
    assert "source: distilled" in content


@pytest.mark.parametrize("invalid_arguments", ["{", "[]", "null", "42", '"text"'])
def test_distillation_reports_invalid_tool_arguments_with_valid_history(db, tmp_path, invalid_arguments):
    llm = mock_llm()
    llm.chat.side_effect = [
        (
            {
                "content": "",
                "finish_reason": "tool_calls",
                "tool_calls": [
                    {
                        "id": "call_bad",
                        "function": {"name": "memory_save", "arguments": invalid_arguments},
                    }
                ],
            },
            {},
        ),
        ({"content": "NOTHING_TO_SAVE", "tool_calls": [], "finish_reason": "stop"}, {}),
    ]

    result = db.run(_handler(db, tmp_path, llm).distil(_event(source_task_id="dt-invalid-args")))

    assert result.status == "completed"
    assert result.rounds == 2
    second_round = llm.chat.call_args_list[1].kwargs["messages"]
    assistant_call = next(message for message in second_round if message.get("role") == "assistant")
    assert assistant_call["tool_calls"][0]["function"]["arguments"] == "{}"
    tool_result = next(message for message in second_round if message.get("role") == "tool")
    assert "TOOL_ARG_ERROR" in tool_result["content"]


@pytest.mark.parametrize(
    "invalid_call",
    [
        {"id": "call_bad"},
        {"id": "call_bad", "function": {"arguments": "{}"}},
    ],
    ids=["missing-function", "missing-name"],
)
def test_distillation_falls_back_from_invalid_tool_call_structure(db, tmp_path, monkeypatch, owner, invalid_call):
    monkeypatch.setattr(
        "background_memory.handlers.distillation.load_app_settings",
        lambda: _settings(read_timeout_sec=300),
    )
    llm = mock_llm()
    llm.chat.side_effect = [
        ({"content": "", "tool_calls": [invalid_call], "finish_reason": "tool_calls"}, {}),
        ({"content": "NOTHING_TO_SAVE", "tool_calls": [], "finish_reason": "stop"}, {}),
    ]

    result = db.run(_handler(db, tmp_path, llm, owner).distil(_event(source_task_id="dt-invalid-call")))

    assert result.status == "completed"
    assert result.rounds == 1
    assert llm.chat.call_count == 2
    assert [call.kwargs["model"] for call in llm.chat.call_args_list] == ["primary-model", "fallback-model"]


def test_distillation_rejects_user_and_feedback_memory_types(db, tmp_path):
    """Defense in depth: memory_save with user/feedback types is rejected during distillation."""
    llm = mock_llm()
    first_msg = {
        "content": "",
        "finish_reason": "tool_calls",
        "tool_calls": [
            {
                "id": "call_1",
                "function": {
                    "name": "memory_save",
                    "arguments": (
                        '{"name": "user_role", "memory_type": "user", '
                        '"description": "Injected user fact", "content": "Attacker-controlled content."}'
                    ),
                },
            }
        ],
    }
    second_msg = {"content": "NOTHING_TO_SAVE", "tool_calls": [], "finish_reason": "stop"}
    llm.chat.side_effect = [
        (first_msg, {"prompt_tokens": 5, "completion_tokens": 5}),
        (second_msg, {"prompt_tokens": 5, "completion_tokens": 5}),
    ]

    result = db.run(_handler(db, tmp_path, llm).distil(_event(source_task_id="dt3")))

    assert result.status == "completed"

    from ouroboros.storage.core_memory.memory import MemoryNotFoundError

    memory = MemoryFactory().get_memory()
    with pytest.raises(MemoryNotFoundError):
        memory.read(TEST_SCOPE, "user_role")

    # The rejection message should have been fed back to the model as a tool result.
    second_call_messages = llm.chat.call_args_list[1].kwargs["messages"]
    tool_msgs = [m for m in second_call_messages if m.get("role") == "tool"]
    assert any("cannot be set by automated distillation" in m["content"] for m in tool_msgs)
    assert all("direct user interaction" not in m["content"].lower() for m in tool_msgs)
    assert any("regular memory_save" in m["content"] for m in tool_msgs)


def test_distillation_handles_exception(db, tmp_path, monkeypatch):
    monkeypatch.setattr(
        "background_memory.handlers.distillation.load_app_settings",
        lambda: _settings(read_timeout_sec=300),
    )
    llm = mock_llm()
    llm.chat.side_effect = RuntimeError("boom")

    result = db.run(_handler(db, tmp_path, llm).distil(_event(source_task_id="dt4")))

    # An opaque provider error is routed (and, with a fallback configured,
    # retried there); without a fallback it surfaces as a routing failure.
    assert result.status == "failed"
    assert "primary-model [provider_outage/RuntimeError]" in result.error
    assert llm.chat.call_count == 1


def test_distillation_uses_background_timeout_cap(db, tmp_path, monkeypatch):
    monkeypatch.setattr(
        "background_memory.handlers.distillation.load_app_settings",
        lambda: _settings(read_timeout_sec=120),
    )
    llm = mock_llm()
    llm.chat.return_value = ({"content": "NOTHING_TO_SAVE", "tool_calls": [], "finish_reason": "stop"}, {})

    result = db.run(_handler(db, tmp_path, llm).distil(_event(source_task_id="dt-timeout")))

    assert result.status == "completed"
    assert llm.chat.call_args.kwargs["timeout_sec"] == 30
    assert llm.chat.call_args.kwargs["read_timeout_sec"] == 120
    assert llm.chat.call_args.kwargs["deadline"] is not None


def test_distillation_call_asks_for_low_effort_within_the_configured_budget(db, tmp_path, monkeypatch):
    monkeypatch.setattr(
        "background_memory.handlers.distillation.load_app_settings",
        lambda: _settings(max_tokens=2048),
    )
    llm = mock_llm()
    llm.chat.return_value = ({"content": "NOTHING_TO_SAVE", "tool_calls": [], "finish_reason": "stop"}, {})

    result = db.run(_handler(db, tmp_path, llm).distil(_event(source_task_id="dt-budget")))

    # Reasoning tokens are billed against max_tokens, so the budget is a setting and the
    # effort is not the client's default: a reasoning model at "medium" spends the whole
    # budget before it emits a tool call, and the truncated round is rejected outright.
    assert result.status == "completed"
    assert llm.chat.call_args.kwargs["max_tokens"] == 2048
    assert llm.chat.call_args.kwargs["reasoning_effort"] == "low"


def test_distillation_falls_back_after_rejected_response_and_stays_on_fallback(db, tmp_path, monkeypatch, owner):
    monkeypatch.setattr(
        "background_memory.handlers.distillation.load_app_settings",
        lambda: _settings(read_timeout_sec=30),
    )
    models: list[str] = []

    def chat(**kwargs):
        models.append(kwargs["model"])
        if models == ["primary-model"]:
            return {"content": "partial", "tool_calls": [], "finish_reason": "length"}, {"prompt_tokens": 2}
        if models == ["primary-model", "fallback-model"]:
            return {
                "content": "",
                "tool_calls": [
                    {
                        "id": "save-1",
                        "function": {
                            "name": "memory_save",
                            "arguments": (
                                '{"name":"deploy_incident","memory_type":"project",'
                                '"description":"Deploy incident","content":"Rollback at 05:00 UTC"}'
                            ),
                        },
                    }
                ],
                "finish_reason": None,
            }, {"prompt_tokens": 3, "completion_tokens": 4}
        if models == ["primary-model", "fallback-model", "fallback-model"]:
            return {"content": "Saved the fact.", "tool_calls": [], "finish_reason": "stop"}, {
                "prompt_tokens": 5,
                "completion_tokens": 1,
            }
        raise AssertionError(f"unexpected model sequence: {models}")

    result = db.run(_handler(db, tmp_path, llm_double(chat), owner).distil(_event()))

    assert result.status == "completed"
    assert result.rounds == 2
    assert result.saved_count == 1
    assert result.saved_names == ["deploy_incident"]
    # Sticky routing: after a successful fallback the next round starts there,
    # and the rejected primary's usage (2 tokens) is still counted: 2 + 3 + 5.
    assert models == ["primary-model", "fallback-model", "fallback-model"]
    assert (result.prompt_tokens, result.completion_tokens) == (10, 5)


def test_distillation_failure_preserves_completed_rounds_usage_and_saves(db, tmp_path, monkeypatch):
    monkeypatch.setattr(
        "background_memory.handlers.distillation.load_app_settings",
        lambda: _settings(read_timeout_sec=30),
    )
    llm = mock_llm()
    llm.chat.side_effect = [
        (
            {
                "content": "",
                "tool_calls": [
                    {
                        "id": "save-1",
                        "function": {
                            "name": "memory_save",
                            "arguments": (
                                '{"name":"deploy_incident","memory_type":"project",'
                                '"description":"Deploy incident","content":"Rollback at 05:00 UTC"}'
                            ),
                        },
                    }
                ],
                "finish_reason": "tool_calls",
            },
            {"prompt_tokens": 3, "completion_tokens": 4},
        ),
        RuntimeError("late round failed"),
    ]

    result = db.run(_handler(db, tmp_path, llm).distil(_event()))

    assert result.status == "failed"
    assert "primary-model [provider_outage/RuntimeError]" in result.error
    assert result.rounds == 2
    assert (result.prompt_tokens, result.completion_tokens) == (3, 4)
    assert result.saved_count == 1
    assert result.saved_names == ["deploy_incident"]


@pytest.mark.parametrize(
    ("expiry", "expected_error"), [("waiting_for_slot", "local_timeout"), ("during_preparation", "deadline expired")]
)
def test_distillation_deadline_before_any_response_fails(db, tmp_path, monkeypatch, expiry, expected_error):
    """Regression: a deadline expiring before the first response analyzed nothing,
    so the run must report a failure instead of a clean completion."""
    from ouroboros.llm.client import LocalLLMDeadlineError

    monkeypatch.setattr(
        "background_memory.handlers.distillation.load_app_settings",
        lambda: _settings(read_timeout_sec=30),
    )
    llm = mock_llm()
    if expiry == "during_preparation":
        monkeypatch.setattr("background_memory.handlers.distillation._DISTILL_TIMEOUT_SEC", -1.0)
        llm.chat.side_effect = AssertionError("no LLM call is expected after the deadline")
    else:
        llm.chat.side_effect = LocalLLMDeadlineError("LLM deadline expired while waiting for a concurrency slot")

    result = db.run(_handler(db, tmp_path, llm).distil(_event()))

    assert result.status == "failed"
    assert expected_error in result.error
    assert result.saved_count == 0


# --- prompt and manifest ----------------------------------------------------


def test_the_distillation_prompt_and_the_tool_schema_permit_the_same_kinds(db, tmp_path):
    """The prompt and the schema are two views of one policy and must not drift.

    They did: folding knowledge into the memory table made it a third permitted
    kind, while the hand-written prompt still said "ONLY these two" — the model
    was handed a schema option its instructions forbade.
    """
    from background_memory.handlers.distillation import _distill_system_prompt
    from ouroboros.storage.core_memory.memory.domain.models import (
        DISTILLATION_FORBIDDEN_KINDS,
        KINDS,
        allowed_kinds,
    )
    from ouroboros.tools.memory_tools import get_tools

    ctx = make_tool_ctx(
        repo_dir=str(tmp_path),
        drive_root=str(tmp_path),
        pending_events=[],
        current_task_type="distillation",
    )
    prompt = _distill_system_prompt(ctx)
    schema_kinds = next(
        entry.schema.parameters["properties"]["memory_type"]["enum"]
        for entry in get_tools(ctx)
        if entry.name == "memory_save"
    )

    # Both halves render as "- kind: ...", so a kind is placed by which block it
    # falls in, not by whether the prompt mentions it anywhere.
    permitted_block, _, forbidden_block = prompt.partition("Automated post-task extraction cannot write")

    rendered_forbidden = DISTILLATION_FORBIDDEN_KINDS.intersection(KINDS)

    assert set(schema_kinds) == set(allowed_kinds(ctx))
    for kind in schema_kinds:
        assert f"- {kind}:" in permitted_block, f"{kind} is offered by the schema but not described in the prompt"
        assert f"- {kind}:" not in forbidden_block, f"{kind} is offered by the schema but listed as forbidden"
    for kind in rendered_forbidden:
        assert f"- {kind}:" not in permitted_block
        assert f"- {kind}:" in forbidden_block, f"{kind} is withheld but the prompt never names it as forbidden"
    for kind in DISTILLATION_FORBIDDEN_KINDS - rendered_forbidden:
        assert f"- {kind}:" not in forbidden_block


def test_the_distillation_prompt_covers_every_kind_distillation_may_write(tmp_path):
    """Every distillable kind has prompt guidance."""
    from background_memory.handlers.distillation import _DISTILL_KIND_GUIDANCE
    from ouroboros.storage.core_memory.memory.domain.models import DISTILLATION_FORBIDDEN_KINDS, KINDS

    assert set(KINDS) - DISTILLATION_FORBIDDEN_KINDS <= set(_DISTILL_KIND_GUIDANCE)


def test_memory_wording_avoids_code_domain_terms(db):
    from background_memory.handlers.distillation import _distill_system_prompt
    from ouroboros.storage.core_memory.memory.domain.render import memory_freshness_note
    from ouroboros.tools.memory_tools import get_tools

    memory_save = next(tool for tool in get_tools() if tool.name == "memory_save")
    combined = "\n".join(
        [
            # Rendered for the real caller: the wording rule covers every line the
            # distillation model actually sees, including the per-kind guidance.
            _distill_system_prompt(SimpleNamespace(current_task_type="distillation")),
            memory_save.schema.description,
            memory_freshness_note(0.0),
        ]
    ).lower()

    forbidden = [
        "code",
        "file",
        "files",
        "git",
        "implementation",
        "architecture",
        "file-line",
        "file:line",
        "project state",
        "task results",
        "direct user interaction",
        "direct conversation",
    ]
    for term in forbidden:
        assert term not in combined


def test_distillation_prompt_truncates_the_manifest_with_an_omission_note(db, tmp_path):
    memory = MemoryFactory().get_memory()
    for i in range(60):
        memory.save(
            TEST_SCOPE,
            f"topic_{i:02d}",
            "project",
            f"Long description {i} " + ("x" * 150),
            f"content {i}",
        )

    llm = mock_llm()
    llm.chat.return_value = ({"content": "NOTHING_TO_SAVE", "tool_calls": [], "finish_reason": "stop"}, {})

    result = db.run(_handler(db, tmp_path, llm).distil(_event(source_task_id="dt-index-trunc")))

    assert result.status == "completed"
    user_prompt = llm.chat.call_args.kwargs["messages"][1]["content"]
    assert "Existing memories:" in user_prompt
    assert "OMISSION NOTE" in user_prompt


def test_distillation_sees_every_kind_of_existing_memory_not_just_headings(db, tmp_path):
    """Regression: the prompt sections' headings ate the budget before any entry.

    The prompt tells the model to check this list for duplicates; with ~750
    characters of heading and banner inside a 1 000-character cap it saw about
    seven entries and never a knowledge one, then re-created what it could not
    see under a new name.
    """
    memory = MemoryFactory().get_memory()
    for i in range(12):
        memory.save(TEST_SCOPE, f"project_{i:02d}", "project", f"project desc {i}", f"content {i}")
    memory.save(TEST_SCOPE, "git-recipes", "knowledge", "Rebase before push", "knowledge body")
    memory.save(TEST_SCOPE, "patterns", "knowledge", "The pattern register", "pattern body")

    llm = mock_llm()
    llm.chat.return_value = ({"content": "NOTHING_TO_SAVE", "tool_calls": [], "finish_reason": "stop"}, {})

    db.run(_handler(db, tmp_path, llm).distil(_event(source_task_id="dt-index-full")))

    user_prompt = llm.chat.call_args.kwargs["messages"][1]["content"]
    manifest = user_prompt.split("Existing memories:", 1)[1]
    assert "OMISSION NOTE" not in manifest
    assert "[knowledge] git-recipes" in manifest
    assert "[knowledge] patterns" in manifest
    assert all(f"project_{i:02d}" in manifest for i in range(12))
    # None of the prompt's section furniture: no heading, no untrusted banner.
    assert "## Memory (" not in manifest
    assert "MEMORY_INDEX_DATA" not in manifest


# --- the timestamp travels in the request -----------------------------------


def test_saved_memories_carry_the_stamp_from_the_event(db, tmp_path):
    from datetime import datetime, timezone

    recorded_at = datetime.fromisoformat("2026-06-24T05:04:00+00:00").timestamp()
    llm = mock_llm()
    llm.chat.return_value = (
        {
            "content": "",
            "finish_reason": "tool_calls",
            "tool_calls": [
                {
                    "function": {
                        "name": "memory_save",
                        "arguments": (
                            '{"name":"deploy_incident","memory_type":"project",'
                            '"description":"Deploy incident","content":"Rollback at 05:00 UTC"}'
                        ),
                    }
                }
            ],
        },
        {},
    )

    result = db.run(
        _handler(db, tmp_path, llm).distil(
            _event(response_text="A" * 120, source_task_id="source-task-2", recorded_at=recorded_at)
        )
    )
    assert result.status == "completed"

    store = MemoryFactory().get_memory()
    topics = store.list_headers(TEST_SCOPE)
    assert len(topics) == 1
    recorded = datetime.fromtimestamp(topics[0].first_recorded_at, tz=timezone.utc)
    assert recorded.isoformat().startswith("2026-06-24T05:04")


def test_the_handler_never_reads_the_task_store_for_the_stamp(db, tmp_path, monkeypatch):
    """It runs before the source task is finished, so the db would mislead it."""
    from ouroboros.storage.runtime_models import TaskRecord
    from ouroboros.storage.runtime_persistence import runtime_persistence

    runtime_persistence.initialize()
    db.store.tasks_repo.upsert_task(
        TaskRecord(
            task_id="source-task-3",
            owner_id="global",
            agent_id="main",
            task_type="task",
            status="completed",
            created_at="2020-01-01T00:00:00+00:00",
            finished_at="2020-01-01T00:01:00+00:00",
        )
    )

    async def _forbidden(*args, **kwargs):
        raise AssertionError("the handler must not read the task table")

    monkeypatch.setattr(type(db.tasks), "is_detached", _forbidden)

    llm = mock_llm()
    llm.chat.return_value = ({"content": "NOTHING_TO_SAVE", "tool_calls": [], "finish_reason": "stop"}, {})

    result = db.run(
        _handler(db, tmp_path, llm).distil(_event(source_task_id="source-task-3", recorded_at=1_781_000_000.0))
    )

    assert result.status == "completed"


# --- the outcome is observable ----------------------------------------------


def test_a_completed_run_records_an_observability_event(db, tmp_path):
    llm = mock_llm()
    llm.chat.side_effect = [
        (
            {
                "content": "",
                "finish_reason": "tool_calls",
                "tool_calls": [
                    {
                        "id": "call_1",
                        "function": {
                            "name": "memory_save",
                            "arguments": (
                                '{"name":"deploy_incident","memory_type":"project",'
                                '"description":"Deploy incident","content":"Rollback at 05:00 UTC"}'
                            ),
                        },
                    }
                ],
            },
            {"prompt_tokens": 5, "completion_tokens": 5},
        ),
        ({"content": "NOTHING_TO_SAVE", "tool_calls": [], "finish_reason": "stop"}, {}),
    ]

    db.run(_handler(db, tmp_path, llm).distil(_event(source_task_id="src-obs")))

    events = _memory_events(tmp_path)
    assert len(events) == 1
    assert events[0]["status"] == "completed"
    assert events[0]["saved_names"] == ["deploy_incident"]
    assert events[0]["rounds"] == 2
    assert events[0]["task_id"] == "src-obs"
    # All four axes: a distillation run really is one task's run.
    assert (events[0]["owner_id"], events[0]["agent_id"]) == (TEST_SCOPE.owner_id, TEST_SCOPE.agent_id)
    assert (events[0]["surface_id"], events[0]["chat_id"]) == ("browser", TEST_SCOPE.chat_id)


def test_a_failed_run_still_records_an_observability_event(db, tmp_path, monkeypatch):
    monkeypatch.setattr(
        "background_memory.handlers.distillation.load_app_settings",
        lambda: _settings(read_timeout_sec=30),
    )
    llm = mock_llm()
    llm.chat.side_effect = RuntimeError("boom")

    db.run(_handler(db, tmp_path, llm).distil(_event(source_task_id="src-failed")))

    events = _memory_events(tmp_path)
    assert len(events) == 1
    assert events[0]["status"] == "failed"
    assert events[0]["saved_names"] == []
    assert events[0]["task_id"] == "src-failed"
    assert "RuntimeError" in events[0]["error"]


def test_the_handler_logs_its_own_result_and_asks_for_no_follow_up(db, tmp_path, caplog):
    """The loop names no result type: the run that produced it emits its own line."""
    caplog.set_level("INFO")
    llm = mock_llm()
    llm.chat.return_value = ({"content": "NOTHING_TO_SAVE", "tool_calls": [], "finish_reason": "stop"}, {})

    assert db.run(_handler(db, tmp_path, llm).handle(_event(source_task_id="src-log"))) is None

    assert "Memory distillation completed source_task_id=src-log" in caplog.text


@pytest.mark.parametrize(
    "outcome", ["success", "crash", "claim-failure", "failed", "cancelled", "rejected_duplicate", "superseded"]
)
def test_only_logical_success_claims_distillation_and_a_claim_is_never_reused(db, tmp_path, monkeypatch, outcome):
    from ouroboros.storage.runtime_models import TaskRequest

    handler = _handler(db, tmp_path, mock_llm())
    status = outcome if outcome in {"failed", "cancelled", "rejected_duplicate"} else "completed"
    db.store.tasks_repo.create_task(
        TaskRequest(id="attempt-2", job_id="logical-job", detached=True, scope=TEST_SCOPE.to_dict()), status=status
    )
    event = _event(source_task_id="stale" if outcome == "superseded" else "attempt-2", job_id="logical-job")
    distilled: list = []
    handler.distil = _recording(distilled)
    if outcome in {"claim-failure", "crash"}:
        with monkeypatch.context() as patch:
            if outcome == "claim-failure":
                patch.setattr(db.audit, "claim", _raising_claim)
            else:
                patch.setattr(handler, "distil", _raising_distil)
            with pytest.raises(RuntimeError, match="interrupted"):
                db.run(handler.handle(event))
    db.run(handler.handle(event))
    db.run(handler.handle(event))
    assert len(distilled) == (1 if outcome in {"success", "claim-failure"} else 0)


def test_concurrent_logical_job_delivery_claims_distillation_once(db, tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    from ouroboros.storage.runtime_models import TaskRequest

    handlers = [_handler(db, tmp_path, mock_llm()) for _ in range(2)]
    db.store.tasks_repo.create_task(
        TaskRequest(id="attempt-2", job_id="logical-job", detached=True, scope=TEST_SCOPE.to_dict()), status="completed"
    )
    event, barrier, calls = _event(source_task_id="attempt-2", job_id="logical-job"), Barrier(2), []
    for handler in handlers:

        async def _record(value, sink=calls):
            sink.append(value)

        handler.distil = _record

    def handle(handler):
        barrier.wait(timeout=5)
        db.run(handler.handle(event))

    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(handle, handlers))
    assert len(calls) == 1


@pytest.mark.parametrize("enabled", [False, True])
@pytest.mark.parametrize("parent", ["", "parent"])
@pytest.mark.parametrize("legacy_event", [False, True])
def test_foreground_root_and_child_results_reach_distillation(db, tmp_path, monkeypatch, enabled, parent, legacy_event):
    from background_memory.infra.events.codec import decode_memory_event, encode_memory_event
    from ouroboros.agent.loop_adapters import EventSinkAdapter
    from ouroboros.storage.runtime_models import TaskRequest

    monkeypatch.setenv("OUROBOROS_DURABLE_JOBS_ENABLED", str(enabled).lower())
    handler = _handler(db, tmp_path, mock_llm())
    publisher = MagicMock()
    if parent:
        db.store.tasks_repo.create_task(TaskRequest(id=parent, scope=TEST_SCOPE.to_dict()), status="running")
    row = db.store.tasks_repo.create_task(
        TaskRequest(id="ordinary", text="Analyze work", parent_task_id=parent, scope=TEST_SCOPE.to_dict()),
        status="running",
    )
    EventSinkAdapter(
        emit_progress=lambda _: None,
        events=None,
        task_id=row.task_id,
        drive_logs=tmp_path,
        memory_events=publisher,
        scope=TEST_SCOPE,
        env=handler._env,
        llm=mock_llm(),
        pending_events=[],
        task=row.to_agent_input(),
        execution_memory_repository=sql_execution_memory_repository(),
        audit_event_repository=sql_audit_event_repository(),
    ).emit_result(text="Useful completed analysis. " * 20, usage={}, trace={}, tool_ctx=None)
    assert db.store.tasks_repo.get_task(row.task_id).status == "completed"
    publisher.publish.assert_called_once()
    payload = encode_memory_event(publisher.publish.call_args.args[0])
    if legacy_event:
        payload["task"]["job_id"] = row.job_id
    event = decode_memory_event(payload)
    distilled: list = []
    handler.distil = _recording(distilled)
    db.run(handler.handle(event))
    assert distilled == [event]


def test_a_failed_result_is_logged_by_the_handler(db, tmp_path, monkeypatch, caplog):
    caplog.set_level("INFO")
    monkeypatch.setattr(
        "background_memory.handlers.distillation.load_app_settings",
        lambda: _settings(read_timeout_sec=30),
    )
    llm = mock_llm()
    llm.chat.side_effect = RuntimeError("boom")

    assert db.run(_handler(db, tmp_path, llm).handle(_event(source_task_id="src-log-failed"))) is None

    assert "Memory distillation failed source_task_id=src-log-failed" in caplog.text


def test_a_skipped_event_produces_no_result_line(db, tmp_path, caplog):
    """The skip reason is the run's own log; no outcome line follows it."""
    caplog.set_level("INFO")

    assert db.run(_handler(db, tmp_path, mock_llm()).handle(_event(response_text="short"))) is None

    assert "Memory distillation completed" not in caplog.text
    assert "Memory distillation failed source_task_id" not in caplog.text


def test_a_run_emits_no_event_of_its_own_beyond_the_observability_record(db, tmp_path):
    """Distillation has no task row and no usage event: the memory stream is the trace."""
    llm = mock_llm()
    llm.chat.return_value = (
        {"content": "NOTHING_TO_SAVE", "tool_calls": [], "finish_reason": "stop"},
        {"prompt_tokens": 1, "completion_tokens": 1},
    )

    db.run(_handler(db, tmp_path, llm).distil(_event(source_task_id="src-no-task-done")))

    from ouroboros.observability.event_record import list_runtime_events

    assert (
        list_runtime_events(
            log_stream="events", event_type="task_done", audit_event_repository=sql_audit_event_repository()
        )
        == []
    )
    events = _memory_events(tmp_path)
    assert len(events) == 1
    assert events[0]["prompt_tokens"] == 1
