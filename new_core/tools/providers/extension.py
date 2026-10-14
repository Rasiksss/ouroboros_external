"""``type: extension`` skills: tools registered from a plugin's ``register(api)``.

An extension tool's schema is knowable only after ``register(api)`` has run, so this
provider reads the extension loader's catalog rather than a manifest.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
from collections.abc import Awaitable, Callable, Sequence
from typing import TYPE_CHECKING, Any

from new_core.skills.interfaces import IExtensionRuntime
from new_core.skills.models import LoadedSkill, SkillId
from new_core.tools.interfaces import IToolProvider, ToolDescriptor
from new_core.tools.runtimes import THREAD_RUNTIME
from new_core.tools.schema import ToolSchema
from ouroboros.tools.catalog import spec_for_tool

if TYPE_CHECKING:
    from new_core.identity import OwnerScope
    from new_core.skills.surface import SkillSurface
    from new_core.tools.context import ToolContext

log = logging.getLogger(__name__)


def _record_skill_id(record: dict[str, Any]) -> SkillId | None:
    try:
        return SkillId.parse(str(record.get("skill_id") or ""))
    except ValueError:
        return None


DEFAULT_EXTENSION_TOOL_TIMEOUT_SEC = 300


class EmptyExtensionRuntime(IExtensionRuntime):
    """A runtime profile that composes no extension execution."""

    def registered_tools(self) -> Sequence[dict[str, Any]]:
        return ()

    def get_tool(self, name: str) -> dict[str, Any] | None:
        del name
        return None

    def parse_surface_name(self, name: str) -> tuple[str, str] | None:
        del name
        return None

    def is_live(self, skill_id: SkillId, skill_surface: SkillSurface) -> bool:
        del skill_id, skill_surface
        return False

    def runtime_state(self, skill: LoadedSkill) -> dict[str, Any]:
        del skill
        return {}

    def unload(self, skill_name: str) -> None:
        del skill_name


def extension_timeout_sec(ext_tool: dict[str, Any]) -> int:
    raw = ext_tool.get("timeout_sec")
    try:
        timeout = int(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return DEFAULT_EXTENSION_TOOL_TIMEOUT_SEC
    if timeout <= 0:
        return DEFAULT_EXTENSION_TOOL_TIMEOUT_SEC
    return timeout


def _positive_int(value: Any) -> int | None:
    """A declared ``result_limit``, or ``None`` for anything that is not one.

    Registration already rejects a non-positive or oversized cap; this is the read side,
    which must not raise on a record written by an older loader.
    """
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        return None
    return value


def _stable_name(runtime: IExtensionRuntime, record: dict[str, Any], surface_name: str) -> str:
    """``"<skill>.<short>"`` — the identity an extension tool keeps across surface renames.

    Working-memory retrieval keys on it, so a tool found today under ``ext_foo_search``
    stays findable after the surface name changes. Falls back to the surface name when the
    loader cannot parse it, which is the same answer the surface name alone would give.
    """
    skill_name = str(record.get("skill") or "")
    if not skill_name:
        return surface_name
    short = surface_name
    try:
        parsed = runtime.parse_surface_name(surface_name)
        if parsed is not None:
            short = parsed[1]
    except Exception:
        short = surface_name
    return f"{skill_name}.{short}"


def not_live_refusal(skill_name: str) -> str:
    """What a caller is told about an extension that may not dispatch right now.

    One string for both paths that produce it — the resolution-time refusal and the
    dispatch-time race — because the model should not be able to tell them apart.
    """
    return f"⚠️ EXTENSION_NOT_LIVE: extension {skill_name!r} is not allowed to dispatch right now."


def _accepts_ctx(handler: Callable[..., Any], args: dict[str, Any]) -> bool:
    """True when ``handler(ctx, **args)`` is the call shape to use, else ``handler(**args)``.

    Extension handlers may or may not take a leading context argument, and the shape alone
    does not say which: ``def run(query="")`` and ``def run(ctx, query="")`` both start with
    one positional parameter. The registry settled it by calling with ``ctx`` and retrying
    on ``TypeError`` — correct, but it also swallowed a genuine ``TypeError`` raised *inside*
    the handler and silently re-ran it, so a handler that failed halfway ran twice.

    ``Signature.bind`` asks the same question without invoking anything, which keeps the
    registry's answer and drops the double-call.
    """
    try:
        sig = inspect.signature(handler)
    except (TypeError, ValueError):
        # Unintrospectable callable (some C builtins): assume the documented shape.
        return True
    try:
        sig.bind(None, **args)
        return True
    except TypeError:
        pass
    try:
        sig.bind(**args)
        return False
    except TypeError:
        # Neither binds — the call fails either way; take the documented shape so the
        # resulting error names the argument the handler actually declares.
        return True


class ExtensionToolProvider(IToolProvider):
    """Live tools registered by extension skills."""

    default_runtime = THREAD_RUNTIME

    def __init__(
        self,
        *,
        skill_surface: SkillSurface,
        extension_runtime: IExtensionRuntime,
    ) -> None:
        self._skill_surface = skill_surface
        self._extension_runtime = extension_runtime

    def refresh(self) -> None:
        # The catalog is read live from the loader on every call; nothing to precompute.
        # Liveness is per-descriptor and can change between calls without a refresh.
        return

    # --- catalog -------------------------------------------------------------------

    def _descriptor_from_record(self, record: dict[str, Any]) -> ToolDescriptor:
        name = str(record.get("name") or "")
        spec = spec_for_tool(name)
        return ToolDescriptor(
            schema=ToolSchema(
                name=name,
                description=str(record.get("description") or ""),
                parameters=record.get("schema") or {"type": "object", "properties": {}},
            ),
            timeout_sec=extension_timeout_sec(record),
            origin=f"ext:{record.get('skill') or ''}",
            surface=spec.surface,
            # Declared policy wins; unstated falls back to the core spec default,
            # since the name-keyed table cannot key a namespaced extension name.
            capability_class=str(record.get("capability_class") or spec.capability_class),
            safety_policy=str(record.get("safety_policy") or spec.safety_policy),
            # distribution_owned no longer buys a safety bypass (#84).
            skips_llm_safety=False,
            result_limit=_positive_int(record.get("result_limit")),
            retrieval_aliases=tuple(str(alias) for alias in record.get("retrieval_aliases") or ()),
            parallel_safe=bool(record.get("parallel_safe")),
            log_redacted_args=frozenset(str(key) for key in record.get("log_redacted_args") or ()),
            stable_name=_stable_name(self._extension_runtime, record, name),
        )

    def _record_is_live(self, record: dict[str, Any], memo: dict[str, bool]) -> bool:
        """Liveness for one record, memoised per skill for the duration of a sweep.

        One extension may register many tools, so compute its registry/snapshot verdict once.
        """
        skill_id = _record_skill_id(record)
        if skill_id is None:
            return False
        key = str(skill_id)
        cached = memo.get(key)
        if cached is None:
            cached = bool(
                self._extension_runtime.is_live(
                    skill_id,
                    self._skill_surface,
                )
            )
            memo[key] = cached
        return cached

    def _record_is_available(self, record: dict[str, Any], scope: OwnerScope) -> bool:
        skill_id = _record_skill_id(record)
        if skill_id is None:
            return False
        skill = self._skill_surface.get(skill_id, agent_name=scope.agent_id)
        return bool(skill and skill.selected and skill.eligible)

    def descriptors(self, scope: OwnerScope) -> Sequence[ToolDescriptor]:
        try:
            records = self._extension_runtime.registered_tools()
        except Exception:
            return []

        memo: dict[str, bool] = {}
        result: list[ToolDescriptor] = []
        for record in records:
            name = str(record.get("name") or "")
            if self._extension_runtime.parse_surface_name(name) is None:
                continue
            if not self._record_is_available(record, scope):
                continue
            if not self._record_is_live(record, memo):
                continue
            result.append(self._descriptor_from_record(record))
        return result

    def get(self, name: str, scope: OwnerScope) -> ToolDescriptor | None:
        """Resolve a name, applying the same liveness filter ``descriptors`` applies.

        Without it the two reads disagree: ``list_available_tools`` would omit a dead
        extension's tool while ``enable_tools`` happily put it on the prompt surface, where
        every call returns ``EXTENSION_NOT_LIVE`` for the rest of the task.
        """
        try:
            record = self._extension_runtime.get_tool(str(name or "").strip())
        except Exception:
            return None
        if not record:
            return None
        if not self._record_is_available(record, scope):
            return None
        if not self._record_is_live(record, {}):
            return None
        return self._descriptor_from_record(record)

    def refuse_dispatch(self, name: str, scope: OwnerScope) -> str | None:
        """Explain the name ``get`` withheld, and unload the extension behind it.

        Without this, dispatch resolves nothing and reports an unknown tool with the list of
        builtin tools attached — for a name that is perfectly well known and merely not
        live. This is also where the unload the registry used to do on a failed dispatch
        happens; ``execute`` no longer gets the chance, because dispatch stops before it.

        ``None`` for a name this provider does not own, and for one it owns *and* would
        offer: in the second case ``get`` returned it and dispatch never asks.
        """
        try:
            record = self._extension_runtime.get_tool(str(name or "").strip())
        except Exception:
            return None
        if not record:
            return None

        skill_name = str(record.get("skill") or "")
        if not self._record_is_available(record, scope):
            return f"⚠️ SKILL_NOT_ELIGIBLE: extension {skill_name!r} is unavailable for this agent."
        if not skill_name:
            return None
        if self._record_is_live(record, {}):
            return None

        self._unload(skill_name)
        return not_live_refusal(skill_name)

    def _unload(self, skill_name: str) -> None:
        try:
            self._extension_runtime.unload(skill_name)
        except Exception:
            log.debug("Failed to unload extension %s after a liveness miss", skill_name, exc_info=True)

    # --- execution -----------------------------------------------------------------

    def execute(self, ctx: ToolContext, name: str, args: dict[str, Any]) -> str | Awaitable[str]:
        record = self._extension_runtime.get_tool(str(name or "").strip())
        if not record:
            return f"⚠️ Unknown tool: {name}"

        skill_name = str(record.get("skill") or "")
        skill_id = _record_skill_id(record)
        if skill_id is None:
            return f"⚠️ EXTENSION_NOT_LIVE: extension {skill_name!r} has no canonical skill id."
        if skill_name and not self._is_live(skill_id, ctx):
            # The race: live when dispatch resolved the name, dead by the time the call got
            # here. `refuse_dispatch` covers the ordinary case, before the safety gate.
            self._unload(skill_name)
            return not_live_refusal(skill_name)

        handler = record["handler"]
        call_args = args or {}
        try:
            result = handler(ctx, **call_args) if _accepts_ctx(handler, call_args) else handler(**call_args)
        except Exception as exc:
            return f"⚠️ extension tool {name!r} failed: {type(exc).__name__}: {exc}"

        if inspect.iscoroutine(result):
            # Returned un-awaited, with this tool's own deadline and this provider's own
            # error text already wrapped around it. Awaiting it on the round's loop is what
            # applies the bound, so the provider keeps a timeout tighter than the call site's
            # ``_get_tool_timeout`` without needing a thread or a loop of its own, and
            # without knowing where its body was placed — every runtime awaits an awaitable
            # result.
            return self._bounded(name, result, extension_timeout_sec(record))
        return result if isinstance(result, str) else str(result)

    @staticmethod
    async def _bounded(name: str, coro: Any, timeout: int) -> str:
        """Await one asynchronous handler under its own deadline, reporting as this provider.

        Awaiting is the runtime's business; *reporting* is this provider's, exactly as it is
        for the synchronous handler two lines above. Letting the exception escape instead
        leaves it to ``_execute_single_tool``, which spells every failure ``⚠️ TOOL_ERROR
        (<tool>): <type>: <msg>`` — and on Python 3.10 ``asyncio.TimeoutError`` carries no
        message, so a handler that ran out of time reported no reason at all.

        ``asyncio.TimeoutError`` is caught before ``Exception`` because on 3.10 it is
        ``concurrent.futures.TimeoutError``, a plain ``Exception`` subclass. ``CancelledError``
        is a ``BaseException`` and passes straight through, which is what abandonment needs.
        """
        try:
            result = await asyncio.wait_for(coro, timeout=timeout)
        except asyncio.TimeoutError:
            return f"⚠️ extension tool {name!r} async handler failed: TimeoutError: handler exceeded timeout"
        except Exception as exc:
            return f"⚠️ extension tool {name!r} async handler failed: {type(exc).__name__}: {exc}"
        return result if isinstance(result, str) else str(result)

    def _is_live(self, skill_id: SkillId, ctx: ToolContext) -> bool:
        # Dispatch checks liveness against the calling task's roots, which is what the
        # registry did; they match this provider's roots in the single-process runtime.
        return bool(
            self._extension_runtime.is_live(
                skill_id,
                self._skill_surface,
            )
        )
