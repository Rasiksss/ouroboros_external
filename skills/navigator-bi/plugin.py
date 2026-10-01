"""PluginAPI registration for navigator_bi_search."""

from __future__ import annotations

import json
from typing import Any

from . import api as navigator_api, handlers
from .api import NavigatorApiError
from .filters import PlanValidationError
from .schema import NAVIGATOR_BI_SCHEMA

_NAVIGATOR_BI_TIMEOUT_SEC = 120

# Char cap the core enforces on this tool's result (the value that was effective
# in the core catalog before the extension migration). Declared via register_tool
# so the core honors it instead of applying the smaller runtime default.
_RESULT_LIMIT = 30_000

_NAVIGATOR_BI_DESCRIPTION = str(NAVIGATOR_BI_SCHEMA["description"])
_NAVIGATOR_BI_PARAMETERS = dict(NAVIGATOR_BI_SCHEMA["parameters"])

# Exactly the SKILL.md env_from_settings keys; get_settings is the sanctioned,
# manifest-gated path to settings, so the payload never reads the settings store.
_SETTING_KEYS = (
    "NAVIGATOR_BI_BASE_URL",
    "NAVIGATOR_BI_MODEL_IDS",
    "NAVIGATOR_BI_SBER_PDI",
    "NAVIGATOR_BI_EMAIL",
    "NAVIGATOR_BI_TIMEOUT_SEC",
    "NAVIGATOR_BI_TOTAL_TIMEOUT_SEC",
)


def _json(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, indent=2, default=str)


def _navigator_bi_search(
    api: Any,
    _ctx: Any,
    mode: str = "",
    model: str = "",
    plan: dict[str, Any] | None = None,
    limit: int = 20,
    **_extra: Any,
) -> str:
    # Guard get_settings + build_config so any settings/identity exception is
    # scrubbed to ⚠️ TOOL_ERROR and never escapes raw (values may embed the PDI).
    try:
        values = api.get_settings(_SETTING_KEYS)
        settings = navigator_api.build_config(values)
    except Exception as exc:
        # Preserve the cause in server logs (field names only — the ValueError
        # message names the offending key, but never carries the value).
        message = str(exc)
        fields = [key for key in _SETTING_KEYS if key in message]
        api.log("error", "navigator settings rejected", error_type=type(exc).__name__, fields=fields)
        return f"⚠️ TOOL_ERROR: navigator settings invalid: {type(exc).__name__}"

    try:
        deadline = navigator_api.NavigatorDeadline(navigator_api.total_timeout(settings))
        mode_name = str(mode or "").strip()
        if mode_name == "schema":
            return _json(handlers.handle_schema(settings, model=str(model or "").strip(), deadline=deadline))
        if mode_name == "query":
            return _json(
                handlers.handle_query(
                    settings,
                    plan,
                    model=str(model or "").strip(),
                    limit=limit,
                    deadline=deadline,
                )
            )
        return f"⚠️ TOOL_ERROR: Unsupported mode: {mode_name}"
    except PlanValidationError as exc:
        # A clarification request is a soft, parseable signal (the model refines
        # and retries) — not a hard failure. Everything else is a hard failure.
        if exc.status == "needs_clarification":
            return _json({"status": "needs_clarification", "message": exc.message})
        return f"⚠️ TOOL_ERROR: {exc.status}: {exc.message}"
    except NavigatorApiError as exc:
        return f"⚠️ TOOL_ERROR: {navigator_api.redact(settings, str(exc))}"
    except Exception as exc:
        return f"⚠️ TOOL_ERROR: {navigator_api.redact(settings, f'navigator_bi_search failed: {type(exc).__name__}: {exc}')}"


def register(api: Any) -> None:
    def _handler(ctx: Any, **kwargs: Any) -> str:
        return _navigator_bi_search(api, ctx, **kwargs)

    api.register_tool(
        "navigator_bi_search",
        _handler,
        description=_NAVIGATOR_BI_DESCRIPTION,
        schema=_NAVIGATOR_BI_PARAMETERS,
        timeout_sec=_NAVIGATOR_BI_TIMEOUT_SEC,
        result_limit=_RESULT_LIMIT,
        retrieval_aliases=("navigator_bi", "navigator", "навигатор"),
        parallel_safe=True,
        # Read-only BI queries: nothing to vet, but they do leave the host, so
        # the class is external_network (skip keeps the pre-#84 behavior).
        safety_policy="skip",
        capability_class="external_network",
    )


__all__ = ["register"]
