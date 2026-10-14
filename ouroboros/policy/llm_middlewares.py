"""The LLM middlewares every task runs around its model calls, in order.

Deliberately package code, not part of the swappable ``security_config/`` directory:
replacing a deployment's config must not be able to change or drop a safety
middleware. Settings only feed the entries. Each entry is a factory ``task_graph``
calls once per task, because a middleware keeps per-task state and tasks run on
worker threads.
"""

from collections.abc import Callable
from functools import partial

from new_core.loop.interfaces import LLMMiddleware
from ouroboros.observability import aef_tracing
from ouroboros.policy.guardrails import GuardrailsClient, GuardrailsMiddleware
from ouroboros.storage.settings.store import load_settings

_settings = load_settings()

llm_middlewares: list[Callable[[], LLMMiddleware]] = []

if _settings.GR_ENABLED:
    _guardrails = GuardrailsClient(
        url=_settings.GR_CHECK_ONLY_URL,
        # GR's own key stays an override, and the agent's one identifier answers for it when the
        # override is unset. Resolved through `current_agent_id` rather than by reading the keys
        # here: which variable names this agent is one decision, and it already has an owner.
        agent_id=_settings.GR_AGENT_ID or aef_tracing.current_agent_id(),
        model=_settings.GR_MODEL,
        timeout=_settings.GR_TIMEOUT_SEC,
        connect_timeout=_settings.GR_CONNECT_TIMEOUT_SEC,
        verify=_settings.GR_VERIFY_SSL,
    )
    llm_middlewares.append(partial(GuardrailsMiddleware, _guardrails))
