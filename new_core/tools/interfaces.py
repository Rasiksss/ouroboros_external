"""Tool-layer boundaries: the descriptor, the provider interface, the tool-facing surfaces.

Every tool source — repo modules, ``type: tool`` skills, ``type: extension`` skills, MCP
servers, remote A2A agents — is reached through an ``IToolProvider``, and every tool it
yields is described by one ``ToolDescriptor``. Consumers enumerate the ordered provider list;
they never read a provider's storage directly.

Two of these are what a *running* tool sees through its ``ToolContext``: ``ToolSurface`` (what
the tool surface is, and how to ask to widen it) and ``ToolEvents`` (how to report outward).
Both are deliberately narrow, and neither names a transport.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Awaitable, Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

from new_core.tools.schema import ToolSchema
from ouroboros.contracts.attachments import StoredFile

if TYPE_CHECKING:
    from new_core.identity import OwnerScope

    # Type-only: ``registry`` imports this module at runtime, so a real import here
    # would close a cycle. ``ToolContext`` moves out of ``registry`` in the follow-up
    # change that deletes ``ToolRegistry``.
    from new_core.tools.context import ToolContext
    from ouroboros.tools.skills import SkillReadResult

__all__ = [
    "IToolProvider",
    "IToolRuntime",
    "NullToolEvents",
    "ToolDescriptor",
    "ToolEvents",
    "ToolOutbound",
    "ToolResult",
    "ToolSurface",
]


@dataclass(frozen=True)
class ToolResult:
    text: str
    skill_page: SkillReadResult | None = None


class IToolRuntime(ABC):
    """Where a tool body runs — placement as a named object rather than the caller's thread.

    Asynchronous from the start: the callers above the catalog are still synchronous and
    reach this through one bridge, but they become coroutines in the change that follows,
    and an interface written synchronously now would be rewritten then.

    A runtime decides *only* placement. Gates, error text, truncation, and the timeout each
    already have an owner above it; a runtime that repeated any of them would mean two
    places deciding, and one answer per runtime.

    ``provider`` is passed alongside the call data rather than as a prepared closure: a
    closure cannot be shipped elsewhere, while ``name`` and ``args`` can.
    """

    @abstractmethod
    async def run(
        self,
        provider: IToolProvider,
        ctx: ToolContext,
        name: str,
        args: dict[str, Any],
    ) -> str | ToolResult:
        """Run ``provider.execute(ctx, name, args)`` wherever this runtime places it.

        ``IToolProvider`` is declared below in this module; the annotation resolves because
        ``from __future__ import annotations`` defers it.
        """


@dataclass(frozen=True)
class ToolDescriptor:
    """One immutable description of a tool, whatever its source.

    The model-facing part is the existing :class:`ToolSchema`, held rather than re-declared,
    so placing a tool in a model request needs no conversion. Policy fields are stated by the
    provider instead of being defaulted at the point of use.

    One field is not data: ``runtime`` holds a live, process-local object rather than a
    description of one, so a descriptor cannot be serialized as a whole. Every consumer today
    reads named fields instead, and the alternative — a string kind resolved through a
    registry — stays available if serializing the descriptor is ever needed.
    """

    schema: ToolSchema
    timeout_sec: int
    origin: str  # "builtin" | "skill:<name>" | "ext:<name>" | "mcp:<server>" | "a2a:<agent>"
    surface: str  # "core" | "discoverable" | "excluded"
    capability_class: str
    safety_policy: str
    skips_llm_safety: bool = True
    is_code_tool: bool = False
    env_prefix: str | None = None

    # --- runtime facts, stated by the source rather than defaulted at the point of use ---
    #
    # These four answer questions the *caller* asks once per tool call — how much of the
    # result may reach the model, which arguments must stay out of the logs, whether the
    # call may share a parallel batch, and what extra terms make it findable later. They
    # live here because the provider is the only thing that knows: an extension declares
    # them through ``register_tool``, an MCP server through its settings entry, a repo tool
    # through its ``ToolSpec``. Resolving them anywhere else means re-reading a live
    # registry mid-flight, which is what pinning one snapshot per call exists to avoid.

    #: Char cap for this tool's result; ``None`` = the runtime's per-tool map/default.
    result_limit: int | None = None
    #: Extra search terms merged into working-memory retrieval for this tool's calls.
    retrieval_aliases: tuple[str, ...] = ()
    #: Whether the caller may schedule this tool inside a parallel read-only batch.
    parallel_safe: bool = False
    #: Argument names whose values must be omitted from tool-call logs.
    log_redacted_args: frozenset[str] = frozenset()
    #: Retrieval-stable identity, when it differs from the model-facing name — an extension
    #: tool is ``"<skill>.<short>"``, so its invocations stay findable under the skill's own
    #: name even after the surface name changes. Empty means "the schema name is stable".
    stable_name: str = ""

    #: Per-descriptor placement override. When set it wins over the owning provider's
    #: ``default_runtime``; when ``None`` the provider's default is used.
    runtime: IToolRuntime | None = None

    @property
    def name(self) -> str:
        """Canonical name — the same string the model sees, stored once."""

        return self.schema.name


class IToolProvider(ABC):
    """One source of tools: catalog reads, source-specific execution, lifecycle."""

    #: Where this provider's tools run when their descriptor names no runtime of its own.
    #: Supplied by the interface so a provider written without mentioning placement still
    #: resolves. A thread placement, because ``execute`` below is a plain synchronous
    #: function in every provider there is: inline would make a gathered read-only batch run
    #: strictly one call after another, with no test failing and nothing but a profile to
    #: notice. A provider whose ``execute`` is genuinely non-blocking overrides this.
    default_runtime: IToolRuntime

    @abstractmethod
    def refresh(self) -> None:
        """(Re)build this provider's catalog. Takes no arguments; safe to call again."""

    @abstractmethod
    def descriptors(self, scope: OwnerScope) -> Sequence[ToolDescriptor]:
        """Every tool this provider offers to the given run scope."""

    @abstractmethod
    def get(self, name: str, scope: OwnerScope) -> ToolDescriptor | None:
        """Resolve a name — including this provider's own alias forms — to a descriptor.

        The *offerable* question: what may be put in front of the model. A provider is free
        to withhold a name it owns, and one does — a dead extension's tool must not reach the
        prompt surface. What to say when that happens is ``refuse_dispatch`` below.
        """

    @abstractmethod
    def refuse_dispatch(self, name: str, scope: OwnerScope) -> str | None:
        """The refusal for a name this provider owns but withholds — or ``None``.

        The other half of ``get``. Once a provider may withhold a name it owns, dispatch is
        left with a name nothing resolves, and the only thing it can say on its own is
        "unknown tool" — followed by the list of *builtin* tools, to a model that asked for
        an extension. That is the wrong answer to the wrong question.

        Returning the refusal is also the provider's chance to act on it: the extension
        provider unloads what it just refused, which is what stops the next task paying for
        the same dead extension again. A refusal doing cleanup is the point, not a surprise.

        Consulted only after resolution has already failed, so it runs before the safety
        supervisor rather than after it — a tool that will not run must not first cost a
        model call.

        Abstract rather than defaulted to ``None``: whether a source can withhold a name it
        owns is a property of that source, and a provider that inherited "nothing to say"
        without being asked would hide the question. ``return None`` is a perfectly good
        answer — it just has to be one the author gave.
        """

    @abstractmethod
    def execute(
        self, ctx: ToolContext, name: str, args: dict[str, Any]
    ) -> str | ToolResult | Awaitable[str | ToolResult]:
        """Run the source-specific invocation. Cross-cutting gates run before this.

        Synchronous in shape, and a plain result is what almost every provider returns. A
        provider whose body is *already* asynchronous may instead return that awaitable
        un-awaited, rather than bridging through a thread or a loop of its own; every
        runtime awaits an awaitable result, so the return shape does not depend on where the
        body was placed. Stated here rather than only inside the runtimes, because this is
        the signature a new provider is written against.
        """

    def close(self) -> None:  # noqa: B027 - optional by design; most providers hold nothing
        """Release whatever the provider holds. Default: nothing to release."""


class ToolSurface(ABC):
    """What a running tool may know about the tool surface, and how it may ask to change it.

    Deliberately no way to invoke another tool.
    """

    @abstractmethod
    def catalog(self) -> Sequence[ToolDescriptor]:
        """Every descriptor across the ordered provider list."""

    @abstractmethod
    def find(self, name: str) -> ToolDescriptor | None:
        """Resolve a name against the provider list in order."""

    @abstractmethod
    def active_names(self) -> frozenset[str]:
        """Names on the prompt surface, including requests made earlier this round."""

    @abstractmethod
    def request_enable(self, name: str, schema: Any) -> None:
        """Ask for a tool to join the prompt surface from the next round."""

    @abstractmethod
    def drain_pending_enables(self) -> Sequence[tuple[str, Any]]:
        """Take the requests buffered since the last drain, and clear them.

        The other half of ``request_enable``: the loop drains this between rounds and
        folds the result into the override channel. Abstract rather than duck-typed on the
        concrete class, because a surface that buffers requests and is never drained still
        answers "enabled" to the model while the tool never reaches the next request.
        """


class ToolEvents(ABC):
    """How a running tool reports outward. Names events, never a transport.

    One method per kind of event tools produce today; a fourth is added when a fourth kind
    appears, not before. Nothing here exposes the queue underneath — a tool that wants to
    report usage does not learn ``put_nowait`` from ``put``, does not assemble the
    supervisor's wire format, and carries no fallback of its own for the transport being
    absent. That fallback belongs to the implementation, which is the only thing that knows
    whether there is a supervisor to send to.

    Every method is best-effort: reporting that fails must not fail the tool call.
    """

    @abstractmethod
    def audit(
        self,
        stream: Literal["events", "tools", "supervisor", "memory"],
        event_type: str,
        payload: Mapping[str, Any],
        *,
        scope: OwnerScope | None,
    ) -> None:
        pass

    @abstractmethod
    def usage(
        self,
        model: str,
        usage: dict[str, Any],
        *,
        category: str = "task",
        provider: str | None = None,
        source: str = "tool",
    ) -> None:
        """Report tokens for a model call made outside the main loop.

        ``category`` and ``source`` classify the call for accounting ("task"/"safety",
        "vlm_query"/"safety_check"); ``provider`` overrides the identity inferred from the
        model name. Task identity is bound when the implementation is built, so it is not a
        parameter here.
        """

    @abstractmethod
    def lifecycle(self, event_type: str, payload: dict[str, Any]) -> None:
        """Emit a named lifecycle event, e.g. ``skill_exec_finished``.

        The caller names the type because the supervisor routes on that string and the set
        grows with skills rather than with this interface.
        """


class ToolOutbound(ABC):
    """Persist history and publish stored file references for a run."""

    @abstractmethod
    def send_file(
        self,
        media: StoredFile,
        *,
        scope: OwnerScope,
        filename: str,
        mime: str = "application/octet-stream",
        caption: str = "",
        source: str = "",
        transport: dict[str, Any] | None = None,
        task_id: str = "",
    ) -> tuple[bool, str]:
        """Report ``(accepted, detail)``; ``DELIVERY_UNKNOWN`` must not trigger a retry."""

    @abstractmethod
    def send_image(
        self,
        media: StoredFile,
        *,
        scope: OwnerScope,
        filename: str,
        mime: str = "image/png",
        caption: str = "",
        source: str = "",
        transport: dict[str, Any] | None = None,
    ) -> tuple[bool, str]:
        """Publish one image; report ``(accepted, detail)``, as ``send_file`` does."""


class NullToolEvents(ToolEvents):
    """Emits nothing. The default for a ``ToolContext`` built outside the task graph.

    Imports nothing from ``ouroboros``, so it is usable as a dataclass default factory on
    ``ToolContext`` without a lazy import. Inside a task this is never the installed
    implementation — the agent builds a real one while assembling the context.
    """

    def audit(
        self,
        stream: Literal["events", "tools", "supervisor", "memory"],
        event_type: str,
        payload: Mapping[str, Any],
        *,
        scope: OwnerScope | None,
    ) -> None:
        return None

    def usage(
        self,
        model: str,
        usage: dict[str, Any],
        *,
        category: str = "task",
        provider: str | None = None,
        source: str = "tool",
    ) -> None:
        return None

    def lifecycle(self, event_type: str, payload: dict[str, Any]) -> None:
        return None
