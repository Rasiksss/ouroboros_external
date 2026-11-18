"""Circuit breaker for LLM provider targets."""

from __future__ import annotations

import json
import logging
import re
import socket
import ssl
import threading
import time
from dataclasses import dataclass, replace
from enum import Enum
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import httpx

from ouroboros.llm.models import parse_model_ref
from ouroboros.llm.providers.base import ModelUnavailableError, ProviderRequestError, ProviderResponseError
from ouroboros.llm.registry import get_registry

log = logging.getLogger(__name__)

PROVIDER_OUTAGE_MESSAGE = "Провайдер модели сейчас не отвечает. Задача не запущена/остановлена, попробуйте позже."


class LLMFailureCategory(str, Enum):
    TRANSIENT = "transient"
    AUTH = "auth"
    #: The model version is disabled upstream, not an access problem: no retry, breaker untouched.
    MODEL_UNAVAILABLE = "model_unavailable"
    CONFIG = "config"
    CONTEXT = "context"
    BAD_REQUEST = "bad_request"
    INCOMPLETE = "incomplete"
    INTERNAL = "internal"
    #: Wire-format limitation of one provider: never opens its breaker, but the
    #: fallback provider may still accept the same request.
    REQUEST = "unsupported_request"


@dataclass(frozen=True)
class ProviderTarget:
    provider: str
    base_url: str
    resolved_model: str
    transport_mode: str

    @property
    def identity(self) -> str:
        return "|".join(
            [
                self.provider.strip(),
                sanitize_base_url(self.base_url),
                self.resolved_model.strip(),
                self.transport_mode.strip() or "remote",
            ]
        )


@dataclass(frozen=True)
class BreakerIdentity:
    provider: str
    base_url: str
    resolved_model: str
    use_local: bool
    config_error: str = ""

    @property
    def key(self) -> str:
        return ProviderTarget(
            self.provider,
            self.base_url,
            self.resolved_model,
            "local" if self.use_local else "remote",
        ).identity


@dataclass
class CircuitState:
    state: str = "closed"
    failures: int = 0
    opened_until: float = 0.0
    last_error: str = ""
    identity: str = ""


def sanitize_base_url(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    try:
        parsed = urlsplit(text)
    except ValueError:
        return text.split("?", 1)[0].split("#", 1)[0]
    if not parsed.scheme or not parsed.netloc:
        return text.split("?", 1)[0].split("#", 1)[0]
    hostname = parsed.hostname or ""
    netloc = hostname
    if parsed.port is not None:
        netloc = f"{hostname}:{parsed.port}"
    return urlunsplit((parsed.scheme, netloc, parsed.path.rstrip("/"), "", ""))


def _short_error(value: Any, limit: int = 220) -> str:
    text = " ".join(str(value or "").split()).strip()
    if len(text) <= limit:
        return text
    return text[: limit - 3] + "..."


def _typed_failure_category(error: Any) -> LLMFailureCategory | None:
    if isinstance(error, ProviderRequestError):
        return LLMFailureCategory.REQUEST
    if isinstance(error, ProviderResponseError):
        return LLMFailureCategory.INCOMPLETE
    if isinstance(error, ModelUnavailableError):
        return LLMFailureCategory.MODEL_UNAVAILABLE
    status = getattr(getattr(error, "response", None), "status_code", None)
    if isinstance(status, int):
        if status in {401, 403}:
            return LLMFailureCategory.AUTH
        if status == 404:
            return LLMFailureCategory.CONFIG
        if status == 400:
            return LLMFailureCategory.BAD_REQUEST
        if status in {408, 425, 429} or 500 <= status < 600:
            return LLMFailureCategory.TRANSIENT
    if isinstance(error, (ConnectionError, TimeoutError, httpx.TransportError)):
        return LLMFailureCategory.TRANSIENT
    if isinstance(error, (ssl.SSLError, socket.gaierror, httpx.DecodingError)):
        return LLMFailureCategory.TRANSIENT
    # Only error *types* that prove a programming bug are INTERNAL; provider
    # JSON decode failures are wrapped as ProviderResponseError at the
    # transport boundary, so a bare JSONDecodeError is our own parsing.
    if isinstance(error, (AssertionError, AttributeError, TypeError, json.JSONDecodeError)):
        return LLMFailureCategory.INTERNAL
    return None


def classify_failure(error: Any) -> LLMFailureCategory:
    chain: list[Any] = []
    seen: set[int] = set()
    current = error
    typed = None
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        chain.append(current)
        typed = _typed_failure_category(current)
        if typed == LLMFailureCategory.BAD_REQUEST:
            break
        if typed is not None:
            return typed
        current = getattr(current, "__cause__", None)

    text = " ".join(f"{type(item).__name__} {item}" for item in chain).lower()
    if typed == LLMFailureCategory.BAD_REQUEST:
        try:
            text += " " + str(getattr(current.response, "text", "")).lower()
        except httpx.ResponseNotRead:
            log.debug("Provider error response body was not read", exc_info=True)
    if any(
        marker in text
        for marker in ("rate limit", "rate_limit", "ratelimit", "tokens per min", "tpm", "too many requests")
    ) or re.search(r"\b(?:http|status(?:[_ ]code)?|error code)[:=\s]+429\b", text):
        return typed or LLMFailureCategory.TRANSIENT
    if "context" in text and any(word in text for word in ("too large", "too long", "overflow", "length", "exceed")):
        return LLMFailureCategory.CONTEXT
    if "token" in text and any(word in text for word in ("limit", "exceed", "too many", "maximum allowed")):
        return LLMFailureCategory.CONTEXT
    if typed is not None:
        return typed
    if any(
        marker in text for marker in ("401", "403", "unauthorized", "forbidden", "invalid_api_key", "authentication")
    ):
        return LLMFailureCategory.AUTH
    if any(marker in text for marker in ("bad request", "status 400", "error code: 400", "validation")):
        return LLMFailureCategory.BAD_REQUEST
    if any(marker in text for marker in ("missing", "not configured", "unsupported provider", "config")):
        return LLMFailureCategory.CONFIG
    transient_markers = (
        "error code: 500",
        " 500 ",
        "status 500",
        "status_code=500",
        "connection attempts failed",
        "connection error",
        "connecterror",
        "readtimeout",
        "timeout",
        "timed out",
        "temporarily unavailable",
        "service unavailable",
        "bad gateway",
        "gateway timeout",
    )
    if any(marker in text for marker in transient_markers):
        return LLMFailureCategory.TRANSIENT
    # Unrecognized provider errors default to TRANSIENT so the breaker can still
    # open and background routing may try the fallback; INTERNAL is reserved for
    # errors whose *type* proves a programming bug (_typed_failure_category).
    return LLMFailureCategory.TRANSIENT


def _opens_breaker(category: LLMFailureCategory) -> bool:
    return category in {
        LLMFailureCategory.TRANSIENT,
        LLMFailureCategory.INCOMPLETE,
    }


class CircuitBreaker:
    _GLOBAL: CircuitBreaker | None = None
    _GLOBAL_LOCK = threading.Lock()

    def __init__(self, *, failure_threshold: int = 2, recovery_seconds: float = 30.0) -> None:
        self.failure_threshold = max(1, int(failure_threshold))
        self.recovery_seconds = max(0.1, float(recovery_seconds))
        self._states: dict[str, CircuitState] = {}
        self._lock = threading.RLock()

    @classmethod
    def global_instance(cls) -> CircuitBreaker:
        with cls._GLOBAL_LOCK:
            if cls._GLOBAL is None:
                cls._GLOBAL = CircuitBreaker()
            return cls._GLOBAL

    def clear(self) -> None:
        with self._lock:
            self._states.clear()

    def _state(self, target: ProviderTarget) -> CircuitState:
        identity = target.identity
        state = self._states.get(identity)
        if state is None:
            return CircuitState(identity=identity)
        if state.opened_until:
            if state.opened_until > time.monotonic():
                state.state = "open"
            else:
                state.state = "half_open"
        state.identity = identity
        return state

    def state(self, target: ProviderTarget) -> CircuitState:
        with self._lock:
            return replace(self._state(target))

    def is_open(self, target: ProviderTarget) -> bool:
        with self._lock:
            return self._state(target).state == "open"

    def record_success(self, target: ProviderTarget) -> None:
        with self._lock:
            self._states.pop(target.identity, None)

    def record_failure(
        self,
        target: ProviderTarget,
        category: LLMFailureCategory,
        *,
        error: Any = "",
    ) -> bool:
        with self._lock:
            if not _opens_breaker(category):
                return self._state(target).state == "open"
            identity = target.identity
            state = self._states.setdefault(identity, CircuitState(identity=identity))
            state.failures += 1
            state.last_error = _short_error(error or category.value)
            if state.failures >= self.failure_threshold:
                state.state = "open"
                state.opened_until = time.monotonic() + self.recovery_seconds
            return state.state == "open"

    def reset_targets(self, targets: list[ProviderTarget]) -> bool:
        with self._lock:
            removed = False
            for target in targets:
                removed = self._states.pop(target.identity, None) is not None or removed
            return removed


def resolve_provider_target_identity(model: str, *, use_local: bool = False) -> BreakerIdentity:
    model_text = str(model or "").strip()
    if use_local:
        return BreakerIdentity("local", "", model_text, True)
    try:
        ref = parse_model_ref(model_text)
        target = get_registry().resolve_target(model_text)
        return BreakerIdentity(
            provider=str(target.get("provider") or ref.provider).strip() or ref.provider,
            base_url=sanitize_base_url(target.get("base_url")),
            resolved_model=str(target.get("resolved_model") or ref.model_id).strip() or ref.model_id,
            use_local=False,
        )
    except Exception as exc:
        return BreakerIdentity("config_error", "", model_text, False, config_error=_short_error(exc))


def _provider_identity(model: str, *, use_local: bool = False, base_url: str = "") -> BreakerIdentity:
    identity = resolve_provider_target_identity(model, use_local=use_local)
    override = sanitize_base_url(base_url)
    if identity.config_error or not override:
        return identity
    return BreakerIdentity(identity.provider, override, identity.resolved_model, identity.use_local)


def _target_for_model(model: str, *, use_local: bool = False, base_url: str = "") -> ProviderTarget | None:
    identity = _provider_identity(model, use_local=use_local, base_url=base_url)
    if identity.config_error:
        return None
    return ProviderTarget(
        identity.provider,
        identity.base_url,
        identity.resolved_model,
        "local" if identity.use_local else "remote",
    )


def provider_circuit_key(model: str, *, use_local: bool = False, base_url: str = "") -> str:
    identity = _provider_identity(model, use_local=use_local, base_url=base_url)
    return identity.key


def provider_base_url_for_model(model: str) -> str:
    return resolve_provider_target_identity(model, use_local=False).base_url


def provider_circuit_is_open(model: str, *, use_local: bool = False, base_url: str = "") -> bool:
    target = _target_for_model(model, use_local=use_local, base_url=base_url)
    return bool(target and CircuitBreaker.global_instance().is_open(target))


def provider_circuit_last_error(model: str, *, use_local: bool = False, base_url: str = "") -> str:
    target = _target_for_model(model, use_local=use_local, base_url=base_url)
    if target is None:
        identity = _provider_identity(model, use_local=use_local, base_url=base_url)
        return identity.config_error
    return CircuitBreaker.global_instance().state(target).last_error


def failure_category_label(category: LLMFailureCategory) -> str:
    """Stable public label for a failure category, used in error reports."""
    if category == LLMFailureCategory.CONTEXT:
        return "context_overflow"
    if category == LLMFailureCategory.INCOMPLETE:
        return "incomplete_response"
    if category == LLMFailureCategory.TRANSIENT:
        return "provider_outage"
    return category.value


def provider_failure_category(error: Any) -> str:
    return failure_category_label(classify_failure(error))


def record_provider_failure(
    model: str,
    *,
    use_local: bool = False,
    base_url: str = "",
    error: Any = "",
    category: LLMFailureCategory | None = None,
) -> bool:
    target = _target_for_model(model, use_local=use_local, base_url=base_url)
    if target is None:
        return False
    return CircuitBreaker.global_instance().record_failure(
        target,
        category or classify_failure(error),
        error=error,
    )


def record_provider_success(model: str, *, use_local: bool = False, base_url: str = "") -> None:
    target = _target_for_model(model, use_local=use_local, base_url=base_url)
    if target is not None:
        CircuitBreaker.global_instance().record_success(target)


def reset_provider_circuit(model: str, *, use_local: bool = False, base_url: str = "") -> bool:
    target = _target_for_model(model, use_local=use_local, base_url=base_url)
    if target is None:
        return False
    breaker = CircuitBreaker.global_instance()
    return breaker.reset_targets([target])


def reset_circuit_breakers() -> None:
    CircuitBreaker.global_instance().clear()


def reset_provider_circuit_breakers() -> None:
    reset_circuit_breakers()


def reset_provider_circuits_for_models(models: list[tuple[str, bool]]) -> None:
    targets: list[ProviderTarget] = []
    for model, use_local in models:
        target = _target_for_model(model, use_local=use_local)
        if target is not None:
            targets.append(target)
    CircuitBreaker.global_instance().reset_targets(targets)
