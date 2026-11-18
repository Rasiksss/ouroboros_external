"""The interfaces the loop graph depends on.

The ``new_core/loop`` orchestration layer names its external dependencies as
abstract base classes (D1). ``GraphContext`` is typed against these ABCs and
never against a concrete ``ouroboros`` class, which is what keeps this layer
import-free (see the import-boundary test). Concrete adapters live in
``ouroboros/agent/`` and subclass these interfaces.

LLM responses and tool calls are named types, not opaque values: ``LLMMessage`` and
``ToolCall`` live in ``new_core.llm.types``, which is where they moved *so that* this
layer could name them and stay import-free. Nodes read ``resp.content`` and hand
``resp.tool_calls`` straight back to ``ToolRunner.handle_tool_calls``, both against a
checked type.

What stays **opaque** (D2) is everything whose type is genuinely ouroboros': tool
schemas (``list[Any]``), the per-task tool context, ``usage`` and ``trace``. This layer
holds those without ever looking inside them.
"""

from __future__ import annotations

import abc
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from new_core.identity import OwnerScope
from new_core.llm.types import LLMFailure, LLMMessage, ToolCall


@dataclass
class LoopOverrides:
    """Compaction and prompt-surface requests tools queue during execution.

    Drained from the tool runner after tool execution (``ToolRunner.drain_overrides``)
    and applied at the top of the next round (``apply_overrides`` node). A ``None``
    field means "no change"; ``apply_overrides`` only touches active state for the
    fields that are set, so a drained-but-empty ``LoopOverrides`` is a no-op.

    ``enable_tools`` holds ``(name, schema)`` pairs: the name is a plain string the node
    needs for ``active_tool_names``, and the schema stays **opaque** (D2) — this layer
    cannot look inside it without importing ouroboros types. The requesting tool, not
    the node, resolves the name, so an unknown tool is reported in the same call that
    asked.
    """

    pending_compaction: int | None = None
    enable_tools: list[tuple[str, Any]] = field(default_factory=list)


class CancellationSignal(abc.ABC):
    """The cooperative stop for one run: set by whoever ends it, observed between rounds.

    Behind it is a process-local event today and shared state under a multi-instance
    supervisor, so the guard router and the memory lanes only ever ask.
    """

    @abc.abstractmethod
    def is_stopped(self) -> bool:
        """Whether a stop has been requested for this run."""

    @abc.abstractmethod
    def request_stop(self) -> None:
        """Ask this run to stop. Idempotent: a second request changes nothing."""


class LLMMiddleware(abc.ABC):
    """A hook pair the ``LLMProvider`` runs around every model call; must not raise."""

    @abc.abstractmethod
    def before_call(self, messages: list[dict[str, Any]]) -> None:
        """See the transcript the model is about to receive."""

    @abc.abstractmethod
    def after_call(self, response: LLMMessage | None) -> None:
        """See the reply the model returned, ``None`` when the call failed."""


class LLMProvider(abc.ABC):
    """The model-facing interface: model calls plus final-response post-processing."""

    middlewares: Sequence[LLMMiddleware]

    @abc.abstractmethod
    def call(
        self,
        *,
        messages: list[dict[str, Any]],
        model: str,
        tools: list[Any] | None,
        effort: str,
        round_idx: int,
        use_local: bool,
        accumulated_usage: dict[str, Any],
    ) -> LLMMessage | LLMFailure:
        """Return the current response or failure, accumulating usage separately."""

    @abc.abstractmethod
    def reset_circuits(self, model: str, use_local: bool) -> None:
        """Reset provider circuit breakers (active model plus any fallback)."""

    @abc.abstractmethod
    def repair_language(
        self,
        content: str | None,
        *,
        messages: list[dict[str, Any]],
        model: str,
        effort: str,
        use_local: bool,
        round_idx: int,
        accumulated_usage: dict[str, Any],
    ) -> str:
        """Repair the final answer's language if it violates the language policy.

        Returns the repaired text, or the original when no repair is needed or
        the repair attempt does not validate.
        """


class ToolRunner(abc.ABC):
    """Owns the run's tool set: its prompt surface, its execution, its overrides (D3/D4).

    The per-task tool context is handed across on every call, exactly like ``messages``
    and ``trace``: the node holds it in run state, and no implementation retains it.
    It stays opaque here — this layer never looks inside it.

    ``bind_surface`` and ``drain_overrides`` are two halves of one thing: the first forms
    the starting surface, the second collects the requests to change it. Holding both here
    is what lets ``prepare_tools`` be a node in this layer rather than ouroboros glue — the
    surface decision itself (policy tables, the extension schema budget, settings) is deeply
    ouroboros and stays behind the interface.

    A round's tool work is likewise two calls rather than one: ``handle_tool_calls`` runs
    the calls, ``process_tool_results`` folds what they returned into the transcript. The
    node makes both, in that order, so that "what ran" and "what the model is told about
    it" are separate steps of the graph instead of one opaque effect.
    """

    @abc.abstractmethod
    def bind_surface(
        self,
        tool_ctx: Any,
        messages: list[dict[str, Any]],
    ) -> tuple[list[Any], set[str]]:
        """Form the run's starting prompt surface: ``(tool_schemas, active_tool_names)``.

        Called once, before the loop is entered. Free to attach whatever lets the tools read
        the surface later onto the opaque ``tool_ctx``, and to merge a system note into
        ``messages`` in place. The schemas stay **opaque** (D2).
        """

    @abc.abstractmethod
    def restore_surface(self, tool_ctx: Any, active_tool_names: set[str]) -> list[Any]:
        """Attach a resumed run's surface over ``active_tool_names``, the run's own set, and return its schemas."""

    @abc.abstractmethod
    def handle_tool_calls(
        self,
        *,
        tool_calls: list[ToolCall],
        active_tool_names: Iterable[str] | None,
        tool_ctx: Any,
        task_id: str,
        decisions: Mapping[ConfirmationKey, str],
    ) -> list[Any]:
        """Execute ``tool_calls`` and return results for calls started while the run is active.

        ``task_id`` names the task the calls belong to (logging, live tool records); it
        arrives on the call, off ``RunState``, exactly like ``tool_ctx`` — no
        implementation retains either.

        ``decisions`` holds the owner's verdicts for this round, keyed by the call each was
        given for: a gated call runs only on an approval, and one with no verdict was never
        asked about.

        The live logs a tool call produces (started / finished / late / timeout) are the
        implementation's own business: a run has one event sink, so an implementation that
        emits them is wired with it once rather than handed it every round.

        Touches neither the transcript nor the trace — that is
        ``process_tool_results``. Each result stays **opaque** (D2): the node carries the
        list from one call to the next without looking inside it.
        """

    @abc.abstractmethod
    def process_tool_results(
        self,
        *,
        results: list[Any],
        messages: list[dict[str, Any]],
        trace: dict[str, Any],
        tool_ctx: Any,
    ) -> int:
        """Append the results of ``handle_tool_calls`` to ``messages`` and ``trace``.

        Returns how many of them were errors.
        """

    @abc.abstractmethod
    def drain_overrides(self, tool_ctx: Any) -> LoopOverrides:
        """Read and reset the override channel tools wrote during execution."""

    @abc.abstractmethod
    def notify_user_input(self) -> None:
        """Signal the repeat guard that fresh owner input arrived."""


class ContextAssembler(abc.ABC):
    """Builds the run's opening transcript and its per-task tool context (D1).

    Assembly is nobody else's concern: it is not an LLM call, not tool work, not
    transcript maintenance, not the inbox, and not an event — hence its own interface
    rather than a member on one of theirs. ``ToolRunner`` in particular is the wrong
    owner: it owns the run's *surface*, and ``prepare_tools`` deliberately writes
    nothing onto ``tool_ctx``.

    The whole assembly policy — the tool context, the prompt soft cap, memory recall,
    the prompt build — lives behind these members on the ouroboros side. A resumed run asks
    only for the tool context: its transcript comes from the snapshot.
    """

    @abc.abstractmethod
    def tool_context(
        self,
        *,
        task: Any,
        task_workspace: Any,
        scope: OwnerScope,
        messages: list[dict[str, Any]],
    ) -> Any:
        """Build this run's tool context, bound to ``messages``, the run's transcript list.

        ``task`` and ``task_workspace`` are the opaque task-level inputs held on
        ``RunState`` (D2); ``scope`` is the run's identity, passed through so the built
        context carries that exact object rather than deriving one from the task dict.
        The returned context stays **opaque** — this layer holds it without looking inside.
        """

    @abc.abstractmethod
    def opening_transcript(self, *, task: Any, tool_ctx: Any, scope: OwnerScope) -> list[dict[str, Any]]:
        """Assemble a fresh run's opening transcript."""


class TranscriptCompactor(abc.ABC):
    """Transcript maintenance: size estimation, compaction, ledger, cache seal."""

    @abc.abstractmethod
    def estimate_tokens(self, messages: list[dict[str, Any]]) -> int:
        """Estimate the transcript size in tokens."""

    @abc.abstractmethod
    def estimate_chars(self, messages: list[dict[str, Any]]) -> int:
        """Estimate the mutable transcript size in characters."""

    @abc.abstractmethod
    def microcompact(
        self,
        messages: list[dict[str, Any]],
        *,
        keep_recent_rounds: int,
        token_threshold: int,
    ) -> None:
        """Replace old tool results with receipts in place once over threshold."""

    @abc.abstractmethod
    def compact(
        self,
        messages: list[dict[str, Any]],
        *,
        keep_recent: int,
        target_tokens: int | None = None,
        target_chars: int | None = None,
    ) -> dict[str, Any] | None:
        """Compact history, then whole instruction sections toward the supplied targets."""

    @abc.abstractmethod
    def upsert_ledger_note(self, messages: list[dict[str, Any]]) -> None:
        """Insert or refresh the tool-outcome ledger note in place."""

    @abc.abstractmethod
    def seal(self, messages: list[dict[str, Any]]) -> None:
        """Mark one stable tool-result boundary with a provider cache marker in place."""


class OwnerInbox(abc.ABC):
    """Injects owner messages that arrived during task execution."""

    @abc.abstractmethod
    def drain(self, messages: list[dict[str, Any]], seen: set[str]) -> bool:
        """Inject queued/drive owner messages in place; return whether any were injected."""


@dataclass(frozen=True)
class ConfirmationRequest:
    """A gated call in fields, published beside the question; the arguments are the shown preview."""

    confirmation_id: str
    task_id: str
    tool: str
    arguments_preview: str
    arguments_truncated: bool
    question: str


#: Position of the call in its round, tool name, argument hash.
ConfirmationKey = tuple[int, str, str]


@dataclass(frozen=True)
class ConfirmationRound:
    """One round's standing with the owner: what is decided, and what is being asked."""

    decisions: Mapping[ConfirmationKey, str]
    request: ConfirmationRequest | None


class ConfirmationBroker(abc.ABC):
    """Where a call that must not run unasked meets the owner's decision."""

    @abc.abstractmethod
    def evaluate(
        self,
        tool_calls: list[ToolCall],
        *,
        snapshot: dict[str, Any],
        tool_ctx: Any,
        task_id: str,
        round_idx: int,
        active_tool_names: Iterable[str] | None,
    ) -> ConfirmationRound:
        """What this round may run, and what the owner is asked for the rest.

        A request means the run is parked and its snapshot stored; its absence means the round goes on.
        """

    @abc.abstractmethod
    def abandon(self, request: ConfirmationRequest) -> bool:
        """Close a question that never went out and unpark the run; ``False`` unless both writes applied."""

    @abc.abstractmethod
    def restore(self, *, task_id: str, tool_ctx: Any) -> dict[str, Any] | None:
        """The snapshot this task was parked with, or ``None`` when it was not parked.

        Handed over once, and state that cannot be read back raises rather than reading as never parked.
        """


class EventSink(abc.ABC):
    """Surfaces progress, typing, checkpoint, usage, and the final task result to the outside.

    ``emit_result`` generalizes this interface from a per-round event surface into
    a result-emitting effect any node can reach (DA5): a terminal node — normal
    finish, round limit, or graceful error — publishes the task's final result by
    calling it, rather than relying on a dedicated downstream emit stage. The
    standalone ``run_llm_loop`` keeps terminal ``END`` and never calls it (its
    adapter's ``emit_result`` is unused), so its return contract is unchanged.
    """

    self_failed: bool

    @abc.abstractmethod
    def progress(self, text: str) -> None:
        """Emit a progress line."""

    @abc.abstractmethod
    def typing(self) -> None:
        """Signal that a model request is about to start, so chat transports can show a typing indicator."""

    @abc.abstractmethod
    def ask_owner(self, text: str, *, request: ConfirmationRequest) -> bool:
        """Put a question to the owner mid-run and report whether it went out.

        Not ``progress``: that may be suppressed and is not kept in history, and a run waits on this.
        """

    @abc.abstractmethod
    def emit_tool_live_log(self, payload: dict[str, Any]) -> None:
        """Emit one live log line onto the event channel.

        Free-form and best-effort: the payload names its own ``type`` and carries whatever
        that type needs, and a failure to publish must not fail whatever produced it. Tool
        execution reaches this the same way a node does — the sink is handed to it as an
        argument, so nothing has to rediscover the event channel from a tool context.
        """

    @abc.abstractmethod
    def usage(self, model: str, usage: dict[str, Any], kind: str) -> None:
        """Emit a usage event for a non-primary LLM call (e.g. compaction)."""

    @abc.abstractmethod
    def maybe_checkpoint(
        self,
        messages: list[dict[str, Any]],
        *,
        round_idx: int,
        max_rounds: int,
    ) -> bool:
        """Maybe inject a periodic self-check turn and emit its checkpoint event.

        The interval policy lives inside the mechanism; returns whether a
        checkpoint was injected this round (so routine compaction can be skipped).
        """

    @abc.abstractmethod
    def task_started(self) -> None:
        """Open the task's record: the durable task-received event and the running status.

        The mirror image of ``emit_result`` at the other end of the run — opening the
        record is the same kind of effect as closing it, which is why it sits on this
        interface rather than on a seventh one. Everything it needs (the task, the
        drive, the scope, the lineage) is run configuration the implementation is built
        with, so the node hands it nothing.

        Like ``emit_result``, only the task graph reaches it: the standalone
        ``run_llm_loop`` has no task record and never registers the node that calls it.
        """

    @abc.abstractmethod
    def emit_result(
        self,
        *,
        text: str | None,
        usage: dict[str, Any],
        trace: dict[str, Any],
        tool_ctx: Any,
    ) -> None:
        """Publish the task's final result.

        Callable from any node (DA5): a terminal node hands its final ``text``
        (the model's answer, the round-limit wrap-up, or a graceful error
        message) together with the accumulated ``usage``, the ``trace``, and the
        opaque ``tool_ctx``. The implementation applies the empty-text fallback
        and performs end-of-task result publication. ``text`` may be ``None`` or
        empty; the fallback lives in the implementation.
        """


__all__ = [
    "CancellationSignal",
    "ConfirmationBroker",
    "ConfirmationKey",
    "ConfirmationRequest",
    "ConfirmationRound",
    "ContextAssembler",
    "EventSink",
    "LLMMiddleware",
    "LLMProvider",
    "LoopOverrides",
    "OwnerInbox",
    "ToolRunner",
    "TranscriptCompactor",
]
