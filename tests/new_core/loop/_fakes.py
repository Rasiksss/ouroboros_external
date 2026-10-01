"""In-memory fake interface implementations for driving the loop graph in isolation.

These subclass the interface ABCs so the graph can be built and invoked without
any ouroboros machinery — the node/router tests assert control flow and state
mutation against these fakes.
"""

from __future__ import annotations

import pathlib
from dataclasses import dataclass, field
from typing import Any

from new_core.loop import (
    CancellationSignal,
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
from tests._shared import FakeCancellation


@dataclass
class FakeToolCall:
    """Opaque tool call: only ``.to_dict()`` is read across the interface boundary."""

    name: str = "noop"
    args: str = "{}"
    id: str = "tc"

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "function": {"name": self.name, "arguments": self.args}}


@dataclass
class FakeResp:
    """Opaque LLM response: nodes read only ``content`` and ``tool_calls``."""

    content: str | None = ""
    tool_calls: list[FakeToolCall] = field(default_factory=list)


class FakeLLM(LLMProvider):
    def __init__(self, responses: list[FakeResp | None], *, repair_return: str | None = None) -> None:
        self._responses = list(responses)
        self.calls: list[dict[str, Any]] = []
        self.reset_calls: list[tuple[str, bool]] = []
        self.repair_calls: list[str | None] = []
        self._repair_return = repair_return

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
        resp = self._responses.pop(0) if self._responses else None
        return resp

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
        skill_msg: str = "",
        surface: tuple[list[Any], set[str]] | None = None,
    ) -> None:
        self._overrides = overrides or LoopOverrides()
        self._skill_msg = skill_msg
        self._surface_schemas, self._surface_names = surface or ([], set())
        self.bind_calls: list[tuple[Any, Any]] = []
        self.run_calls: list[list[FakeToolCall]] = []
        self.processed: list[list[Any]] = []
        self.notify_calls = 0
        self.drained = 0
        self.run_contexts: list[Any] = []
        self.run_task_ids: list[str] = []
        self.process_contexts: list[Any] = []
        self.drain_contexts: list[Any] = []

    def bind_surface(self, tool_ctx, messages):
        self.bind_calls.append((tool_ctx, messages))
        return list(self._surface_schemas), set(self._surface_names)

    def handle_tool_calls(self, *, tool_calls, active_tool_names, tool_ctx, task_id):
        self.run_calls.append(list(tool_calls))
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

    def skill_finalization_message(self, trace):
        return self._skill_msg


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

    def compact(self, messages, *, keep_recent):
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


class FakeEvents(EventSink):
    def __init__(self, *, checkpoint: bool = False) -> None:
        self._checkpoint = checkpoint
        self.progress_calls: list[str] = []
        self.typing_calls = 0
        self.usage_calls: list[tuple[str, str]] = []
        self.checkpoint_calls = 0
        self.result_calls: list[dict[str, Any]] = []
        self.log_payloads: list[dict[str, Any]] = []

    def progress(self, text):
        self.progress_calls.append(text)

    def typing(self):
        self.typing_calls += 1

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
        "drive_root": None,
        "drive_logs": pathlib.Path("/tmp/loop-graph-tests"),
        "max_retries": 3,
        "light_model": "light",
        "default_model": "m1",
        "default_effort": "medium",
        "default_use_local": False,
    }
    defaults.update(overrides)
    return RunConfig(**defaults)


def make_ctx(
    *,
    llm: FakeLLM,
    tools: FakeTools | None = None,
    compactor: FakeCompactor | None = None,
    inbox: FakeInbox | None = None,
    events: FakeEvents | None = None,
    config: RunConfig | None = None,
    cancellation: CancellationSignal | None = None,
) -> GraphContext:
    return GraphContext(
        llm=llm,
        tools=tools or FakeTools(),
        compactor=compactor or FakeCompactor(),
        inbox=inbox or FakeInbox(),
        events=events or FakeEvents(),
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
