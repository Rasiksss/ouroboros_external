"""In-memory fake interface implementations for driving the loop graph in isolation.

These subclass the interface ABCs so the graph can be built and invoked without
any ouroboros machinery — the node/router tests assert control flow and state
mutation against these fakes.
"""

from __future__ import annotations

import pathlib
from typing import Any

from new_core.identity import OwnerScope
from new_core.llm.types import LLMFailure, LLMMessage
from new_core.loop import (
    CancellationSignal,
    ConfirmationBroker,
    ConfirmationKey,
    ConfirmationRequest,
    ConfirmationRound,
    ContextAssembler,
    EventSink,
    GraphContext,
    LLMProvider,
    LoopOverrides,
    OwnerInbox,
    RunConfig,
    ToolRunner,
    TranscriptCompactor,
    make_run_state,
)
from tests.helpers.shared import FakeCancellation


class FakeLLM(LLMProvider):
    middlewares = ()

    def __init__(
        self,
        responses: list[LLMMessage | LLMFailure],
        *,
        repair_return: str | None = None,
        unavailable_models: set[str] | None = None,
    ) -> None:
        self._responses = list(responses)
        self.calls: list[dict[str, Any]] = []
        self.reset_calls: list[tuple[str, bool]] = []
        self.repair_calls: list[str | None] = []
        self._repair_return = repair_return
        self._unavailable_models = set(unavailable_models or ())

    def call(self, *, messages, model, tools, effort, round_idx, use_local, accumulated_usage):
        self.calls.append(
            {
                "model": model,
                # ``tools`` is the live RunState list; keep a per-round copy too, so a
                # later round's append is not visible in an earlier round's record.
                "tools": tools,
                "tools_snapshot": None if tools is None else list(tools),
                "effort": effort,
                "use_local": use_local,
                "round": round_idx,
            }
        )
        # Mirrors call_llm_with_retry: a disabled model version flags the run and
        # yields no response, without consuming a scripted one.
        if model in self._unavailable_models:
            return LLMFailure("model unavailable", model_unavailable=True)
        return self._responses.pop(0) if self._responses else LLMFailure("empty response")

    def reset_circuits(self, model, use_local):
        self.reset_calls.append((model, use_local))

    def repair_language(self, content, *, messages, model, effort, use_local, round_idx, accumulated_usage):
        self.repair_calls.append(content)
        return self._repair_return if self._repair_return is not None else (content or "")


class FakeTools(ToolRunner):
    def __init__(
        self,
        *,
        overrides: LoopOverrides | None = None,
        surface: tuple[list[Any], set[str]] | None = None,
    ) -> None:
        self._overrides = overrides or LoopOverrides()
        self._surface_schemas, self._surface_names = surface or ([], set())
        self.bind_calls: list[tuple[Any, Any]] = []
        self.run_calls: list[list[Any]] = []
        self.run_decisions: list[dict[Any, str]] = []
        self.processed: list[list[Any]] = []
        self.notify_calls = 0
        self.drained = 0
        self.run_contexts: list[Any] = []
        self.run_task_ids: list[str] = []
        self.process_contexts: list[Any] = []
        self.drain_contexts: list[Any] = []
        self.restored_names: set[str] | None = None

    def bind_surface(self, tool_ctx, messages):
        self.bind_calls.append((tool_ctx, messages))
        return list(self._surface_schemas), set(self._surface_names)

    def restore_surface(self, tool_ctx, active_tool_names):
        self.restored_names = active_tool_names
        return [f"schema:{name}" for name in sorted(active_tool_names)]

    def handle_tool_calls(self, *, tool_calls, active_tool_names, tool_ctx, task_id, decisions):
        self.run_calls.append(list(tool_calls))
        self.run_decisions.append(dict(decisions))
        self.run_contexts.append(tool_ctx)
        self.run_task_ids.append(task_id)
        # Opaque to the node: one result per call, shaped however the adapter likes.
        return [{"tool_call_id": tc.id} for tc in tool_calls]

    def process_tool_results(self, *, results, messages, trace, tool_ctx):
        self.processed.append(list(results))
        self.process_contexts.append(tool_ctx)
        for result in results:
            messages.append({"role": "tool", "tool_call_id": result["tool_call_id"], "content": "ok"})
        trace["tool_calls"].append({"tool": "noop"})
        return 0

    def drain_overrides(self, tool_ctx):
        self.drained += 1
        self.drain_contexts.append(tool_ctx)
        ov, self._overrides = self._overrides, LoopOverrides()
        return ov

    def notify_user_input(self):
        self.notify_calls += 1


class FakeConfirmations(ConfirmationBroker):
    """Nothing to ask about by default, so a round with tool calls runs as it always did."""

    def __init__(
        self,
        *,
        decided: dict[ConfirmationKey, str] | None = None,
        request: ConfirmationRequest | None = None,
        snapshot: dict[str, Any] | None = None,
        abandon_result: bool = True,
        restore_error: Exception | None = None,
    ) -> None:
        self._decided = dict(decided or {})
        self._request = request
        self._snapshot = snapshot
        self._abandon_result = abandon_result
        self._restore_error = restore_error
        self.rounds: list[dict[str, Any]] = []
        self.abandoned: list[ConfirmationRequest] = []

    def evaluate(self, tool_calls, *, snapshot, tool_ctx, task_id, round_idx, active_tool_names):
        self.rounds.append({"snapshot": snapshot, "round_idx": round_idx})
        return ConfirmationRound(decisions=dict(self._decided), request=self._request)

    def abandon(self, request):
        self.abandoned.append(request)
        return self._abandon_result

    def restore(self, *, task_id, tool_ctx):
        if self._restore_error is not None:
            raise self._restore_error
        return self._snapshot


class FakeCompactor(TranscriptCompactor):
    def __init__(self, *, tokens: int = 0, chars: int = 0, compact_usage: dict[str, Any] | None = None) -> None:
        self._tokens = tokens
        self._chars = chars
        self._compact_usage = compact_usage
        self.compact_calls: list[int] = []
        self.microcompact_calls: list[tuple[int, int]] = []
        self.upsert_calls = 0
        self.seal_calls = 0

    def estimate_tokens(self, messages):
        return self._tokens

    def estimate_chars(self, messages):
        return self._chars

    def microcompact(self, messages, *, keep_recent_rounds, token_threshold):
        self.microcompact_calls.append((keep_recent_rounds, token_threshold))

    def compact(self, messages, *, keep_recent, target_tokens=None, target_chars=None):
        self.compact_calls.append(keep_recent)
        return self._compact_usage

    def upsert_ledger_note(self, messages):
        self.upsert_calls += 1

    def seal(self, messages):
        self.seal_calls += 1


class FakeInbox(OwnerInbox):
    def __init__(self, *, injected: bool = False) -> None:
        self._injected = injected
        self.calls = 0

    def drain(self, messages, seen):
        self.calls += 1
        return self._injected


class FakeContextAssembler(ContextAssembler):
    """Returns a canned tool context and transcript, recording what it was handed."""

    def __init__(
        self,
        *,
        messages: list[dict[str, Any]] | None = None,
        tool_ctx: Any = None,
    ) -> None:
        self._messages = messages if messages is not None else [{"role": "user", "content": "assembled"}]
        self._tool_ctx = tool_ctx if tool_ctx is not None else object()
        self.calls: list[dict[str, Any]] = []

    def tool_context(self, *, task, task_workspace, scope, messages):
        self.calls.append({"task": task, "task_workspace": task_workspace, "scope": scope, "messages": messages})
        return self._tool_ctx

    def opening_transcript(self, *, task, tool_ctx, scope):
        return list(self._messages)


class FakeEvents(EventSink):
    def __init__(self, *, checkpoint: bool = False, question_delivered: bool = True) -> None:
        self._checkpoint = checkpoint
        self._question_delivered = question_delivered
        self.progress_calls: list[str] = []
        self.typing_calls = 0
        self.usage_calls: list[tuple[str, str]] = []
        self.checkpoint_calls = 0
        self.result_calls: list[dict[str, Any]] = []
        self.log_payloads: list[dict[str, Any]] = []
        self.task_started_calls = 0
        self.owner_questions: list[str] = []
        #: Every sink call in order, so a test can pin the emitted-event sequence.
        self.order: list[str] = []

    def progress(self, text):
        self.progress_calls.append(text)

    def typing(self):
        self.typing_calls += 1
        self.order.append("typing")

    def ask_owner(self, text, *, request):
        self.owner_questions.append(text)
        self.order.append("ask_owner")
        return self._question_delivered

    def task_started(self):
        self.task_started_calls += 1
        self.order.append("task_started")

    def emit_tool_live_log(self, payload):
        self.log_payloads.append(payload)

    def usage(self, model, usage, kind):
        self.usage_calls.append((model, kind))

    def maybe_checkpoint(self, messages, *, round_idx, max_rounds):
        self.checkpoint_calls += 1
        return self._checkpoint

    def emit_result(self, *, text, usage, trace, tool_ctx):
        # The standalone loop terminal is END, so this is never reached; record
        # calls anyway so a task-graph test driving fakes can assert publication.
        self.result_calls.append({"text": text, "usage": usage, "trace": trace, "tool_ctx": tool_ctx})


def make_config(**overrides: Any) -> RunConfig:
    defaults: dict[str, Any] = {
        "max_rounds": 200,
        "model_context_tokens": 200_000,
        "emergency_threshold": 150_000,
        "emergency_use_chars": False,
        "emergency_char_threshold": 1_200_000,
        "fallback_model": "",
        "fallback_use_local": False,
        "task_id": "t",
        "task_type": "task",
        "scope": OwnerScope(owner_id="global", agent_id="main", surface_id="browser", chat_id="1"),
        "drive_root": None,
        "drive_logs": pathlib.Path("/tmp/loop-graph-tests"),
        "max_retries": 3,
        "light_model": "light",
        "default_model": "m1",
        "default_effort": "medium",
        "default_use_local": False,
        "reasoning_progress": True,
    }
    defaults.update(overrides)
    return RunConfig(**defaults)


def make_ctx(
    *,
    llm: FakeLLM,
    tools: FakeTools | None = None,
    compactor: FakeCompactor | None = None,
    assembler: FakeContextAssembler | None = None,
    inbox: FakeInbox | None = None,
    events: FakeEvents | None = None,
    confirmations: FakeConfirmations | None = None,
    config: RunConfig | None = None,
    cancellation: CancellationSignal | None = None,
) -> GraphContext:
    return GraphContext(
        llm=llm,
        tools=tools or FakeTools(),
        compactor=compactor or FakeCompactor(),
        assembler=assembler or FakeContextAssembler(),
        inbox=inbox or FakeInbox(),
        events=events or FakeEvents(),
        confirmations=confirmations or FakeConfirmations(),
        config=config or make_config(),
        cancellation=cancellation or FakeCancellation(),
    )


def make_state(ctx: GraphContext, *, messages: list[dict[str, Any]] | None = None):
    return make_run_state(
        ctx.config,
        messages=messages if messages is not None else [{"role": "user", "content": "hi"}],
        tool_schemas=[],
        active_tool_names=set(),
    )
