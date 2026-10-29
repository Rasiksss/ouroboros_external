"""Pydantic schema for flat UPPER_SNAKE configuration keys (env + memory)."""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Mapping, Sequence
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Any, ClassVar, Literal, Self, TypeAlias, cast, get_args

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    ValidationError,
    ValidationInfo,
    field_validator,
    model_validator,
)

from ouroboros.observability.aef_tracing import AGENT_HEADER_NAME, TRACE_HEADER_NAME

from .retired import is_retired_setting_key

log = logging.getLogger(__name__)

CLOUDRU_FOUNDATION_MODELS_BASE_URL = "https://foundation-models.api.cloud.ru/v1"

_CUSTOM_KEY_RE = re.compile(r"^[A-Z][A-Z0-9_]{2,}$")
_TRUTHY = frozenset({"1", "true", "yes", "on"})
_FALSY = frozenset({"0", "false", "no", "off"})
# Shared by every configured external tool source that may cap its own results.
_MAX_TOOL_RESULT_LIMIT = 120_000
# One place for the GR timeouts: the field default and the fallback for an empty value.
_GR_TIMEOUT_DEFAULT_SEC = 30.0
#: TN09's hard ceiling on the share of traffic a preview model version may receive.
PREVIEW_MAX_PROBABILITY = 0.05


EffortLevel = Literal["none", "low", "medium", "high"]

HTTP_TOKEN_RE = re.compile(r"[!#$%&'*+\-.^_`|~0-9A-Za-z]+")
FORWARDABLE_HEADER_VALUE_RE = re.compile(r"[\x21-\x7e]+(?:[\t ]+[\x21-\x7e]+)*")
PROTECTED_FORWARDED_HEADERS = frozenset(
    {
        "host",
        "content-length",
        "content-type",
        "transfer-encoding",
        "connection",
        "keep-alive",
        "proxy-connection",
        "proxy-authorization",
        "te",
        "trailer",
        "upgrade",
        "expect",
        "accept",
        "cookie",
        "last-event-id",
        TRACE_HEADER_NAME.lower(),
        AGENT_HEADER_NAME.lower(),
    }
)
PROTECTED_FORWARDED_HEADER_PREFIXES = ("mcp-", "sec-websocket-", "a2a-", "x-a2a-")


def _forwarded_names(kind: str, names: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for raw in names:
        name = str(raw).strip()
        if not HTTP_TOKEN_RE.fullmatch(name):
            raise ValueError(f"user_headers {kind} must be non-empty HTTP token names")
        folded = name.lower()
        if folded in seen:
            raise ValueError(f"user_headers {kind} names {name!r} more than once")
        seen.add(folded)
        out.append(name)
    return out


class EgressAuthKind(StrEnum):
    NONE = "none"
    USER_HEADERS = "user_headers"
    USER_TOKEN = "user_token"
    MTLS = "mtls"


class EgressAuthBase(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    kind: EgressAuthKind

    @classmethod
    def _unprotected_header(cls, field: str, name: str) -> str:
        folded = name.lower()
        if folded in PROTECTED_FORWARDED_HEADERS or folded.startswith(PROTECTED_FORWARDED_HEADER_PREFIXES):
            raise ValueError(f"{field} cannot forward the protected header {name!r}")
        return name


class NoAuth(EgressAuthBase):
    kind: Literal[EgressAuthKind.NONE] = EgressAuthKind.NONE


class UserTokenAuth(EgressAuthBase):
    kind: Literal[EgressAuthKind.USER_TOKEN] = EgressAuthKind.USER_TOKEN
    auth_header: str
    auth_token: str | None = Field(default=None, repr=False)

    @field_validator("auth_header")
    @classmethod
    def _validate_auth_header(cls, value: str) -> str:
        name = value.strip()
        if not HTTP_TOKEN_RE.fullmatch(name):
            raise ValueError("user_token auth_header must be a non-empty HTTP token name")
        return cls._unprotected_header("user_token auth_header", name)

    @field_validator("auth_token")
    @classmethod
    def _validate_auth_token(cls, value: str | None) -> str | None:
        if not value:
            return None
        if not FORWARDABLE_HEADER_VALUE_RE.fullmatch(value):
            raise ValueError("user_token auth_token carries characters a request header cannot hold")
        return value


class MTLSAuth(EgressAuthBase):
    kind: Literal[EgressAuthKind.MTLS] = EgressAuthKind.MTLS


class UserHeadersAuth(EgressAuthBase):
    kind: Literal[EgressAuthKind.USER_HEADERS] = EgressAuthKind.USER_HEADERS
    headers: list[str] = Field(default_factory=list)
    cookies: list[str] = Field(default_factory=list)

    @field_validator("headers")
    @classmethod
    def _validate_headers(cls, value: list[str]) -> list[str]:
        return [cls._unprotected_header("user_headers headers", name) for name in _forwarded_names("headers", value)]

    @field_validator("cookies")
    @classmethod
    def _validate_cookies(cls, value: list[str]) -> list[str]:
        return _forwarded_names("cookies", value)

    @model_validator(mode="after")
    def _names_something(self) -> UserHeadersAuth:
        if not self.headers and not self.cookies:
            raise ValueError("user_headers must name at least one header or cookie")
        return self


EgressAuth = Annotated[NoAuth | UserHeadersAuth | UserTokenAuth | MTLSAuth, Field(discriminator="kind")]


class McpServerEntry(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    protocol: ClassVar[str] = "MCP"

    id: str = ""
    name: str = ""
    enabled: bool = True
    url: str
    auth: EgressAuth = Field(default_factory=NoAuth)
    transport: Literal["sse", "streamable_http"] = "streamable_http"
    allowed_tools: list[str] = Field(default_factory=list)
    retrieval_aliases: list[str] = Field(default_factory=list)
    tool_result_limits: dict[str, int] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _validate_authorization(self) -> Self:
        auth = self.auth
        if isinstance(auth, UserHeadersAuth) and self.transport == "sse":
            raise ValueError(
                "auth kind 'user_headers' requires transport 'streamable_http'; transport 'sse' is refused"
            )
        if isinstance(auth, UserTokenAuth) and self.transport == "sse" and auth.auth_token is None:
            raise ValueError(
                "auth kind 'user_token' over transport 'sse' requires a static auth_token; "
                "a personal token cannot ride sse"
            )
        return self

    @property
    def takes_personal_token(self) -> bool:
        return isinstance(self.auth, UserTokenAuth) and self.transport == "streamable_http"

    @field_validator("enabled", mode="before")
    @classmethod
    def _coerce_bool(cls, value: Any) -> bool:
        if isinstance(value, bool):
            return value
        text = str(value or "").strip().lower()
        if text in _TRUTHY:
            return True
        if text in _FALSY:
            return False
        return bool(value)

    @field_validator("url")
    @classmethod
    def _validate_url(cls, value: str) -> str:
        url = value.strip()
        if not url.startswith(("http://", "https://")):
            raise ValueError(f"{cls.protocol} url must be an http:// or https:// address")
        return url

    @field_validator("tool_result_limits", mode="before")
    @classmethod
    def _validate_tool_result_limits(cls, value: Any) -> dict[str, int]:
        if value in (None, ""):
            return {}
        if not isinstance(value, Mapping):
            raise ValueError("MCP tool_result_limits must be an object")

        limits: dict[str, int] = {}
        for raw_name, raw_limit in value.items():
            name = str(raw_name or "").strip()
            if not name:
                raise ValueError("MCP tool_result_limits keys must be non-empty tool names")
            if isinstance(raw_limit, bool) or not isinstance(raw_limit, int):
                raise ValueError("MCP tool result limits must be integers")
            limit = raw_limit
            if not 1 <= limit <= _MAX_TOOL_RESULT_LIMIT:
                raise ValueError(f"MCP tool result limits must be between 1 and {_MAX_TOOL_RESULT_LIMIT}")
            limits[name] = limit
        return limits


class A2aRemoteAgentEntry(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    protocol: ClassVar[str] = "A2A"

    id: str = ""
    name: str = ""
    enabled: bool = True
    url: str
    auth: EgressAuth = Field(default_factory=NoAuth)
    featured_skills: list[str] = Field(default_factory=list)
    retrieval_aliases: list[str] = Field(default_factory=list)
    tool_result_limit: int = 0
    poll_interval_sec: float = 2.0

    @property
    def takes_personal_token(self) -> bool:
        return isinstance(self.auth, UserTokenAuth)

    @field_validator("enabled", mode="before")
    @classmethod
    def _coerce_bool(cls, value: Any) -> bool:
        if isinstance(value, bool):
            return value
        text = str(value or "").strip().lower()
        if text in _TRUTHY:
            return True
        if text in _FALSY:
            return False
        return bool(value)

    @field_validator("url")
    @classmethod
    def _validate_url(cls, value: str) -> str:
        url = value.strip()
        if not url.startswith(("http://", "https://")):
            raise ValueError(f"{cls.protocol} url must be an http:// or https:// address")
        return url

    @field_validator("tool_result_limit", mode="before")
    @classmethod
    def _validate_tool_result_limit(cls, value: Any) -> int:
        if value in (None, ""):
            return 0
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError("A2A tool_result_limit must be an integer")
        if value == 0:
            return 0
        if not 1 <= value <= _MAX_TOOL_RESULT_LIMIT:
            raise ValueError(f"A2A tool_result_limit must be between 1 and {_MAX_TOOL_RESULT_LIMIT}")
        return value

    @field_validator("poll_interval_sec", mode="before")
    @classmethod
    def _validate_poll_interval(cls, value: Any) -> float:
        if value in (None, ""):
            return 2.0
        try:
            interval = float(value)
        except (TypeError, ValueError):
            raise ValueError("A2A poll_interval_sec must be a number") from None
        if not 0.05 <= interval <= 300.0:
            raise ValueError("A2A poll_interval_sec must be between 0.05 and 300")
        return interval


EgressEntry: TypeAlias = McpServerEntry | A2aRemoteAgentEntry


def connectable_a2a_agents(entries: Sequence[A2aRemoteAgentEntry]) -> tuple[list[A2aRemoteAgentEntry], list[str]]:
    connectable: list[A2aRemoteAgentEntry] = []
    duplicates: list[str] = []
    seen: set[str] = set()
    for entry in entries:
        agent_id = entry.id.strip()
        if not (entry.enabled and agent_id):
            continue
        if agent_id in seen:
            duplicates.append(agent_id)
            continue
        seen.add(agent_id)
        connectable.append(entry if entry.id == agent_id else entry.model_copy(update={"id": agent_id}))
    return connectable, duplicates


def _coerce_bool(value: Any, *, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    text = str(value or "").strip().lower()
    if text in _TRUTHY:
        return True
    if text in _FALSY:
        return False
    return default


def _coerce_int(value: Any, *, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _coerce_float(value: Any, *, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _coerce_choice(value: Any, *, allowed: tuple[str, ...], default: str) -> str:
    text = str(value or "").strip().lower()
    return text if text in allowed else default


LogSink: TypeAlias = Literal["stdout", "file", "both"]


def normalize_log_sink(value: Any) -> LogSink:
    """The one owner of the sink allow-list, schema and raw-string callers alike."""
    return cast(LogSink, _coerce_choice(value, allowed=get_args(LogSink), default="stdout"))


def describe_validation_error(error: ValidationError, *, limit: int | None = None) -> str:
    """Which keys were rejected and why, without the values that were rejected.

    `include_input=False` withholds the offending value at the source rather than trusting each
    caller not to read it: an MCP entry carries `auth_token`, and `str(error)` embeds an
    `input_value=` for every arm.
    """
    entries = error.errors(include_url=False, include_context=False, include_input=False)
    shown = [
        f"{'.'.join(str(part) for part in entry['loc']) or '<root>'}: {entry['msg']}"
        for entry in (entries[:limit] if limit is not None else entries)
    ]
    if limit is not None and len(entries) > limit:
        shown.append(f"and {len(entries) - limit} more not shown")
    return "; ".join(shown)


def _coerce_mcp_servers(value: Any) -> list[McpServerEntry]:
    """A list or a JSON string. A malformed entry is dropped **and named**, never dropped quietly.

    A drop here is one MCP server's tools gone from the catalogue, and an empty result skips
    manager initialization altogether. Only the reason is logged; the entry carries `auth_token`.
    """
    if isinstance(value, list):
        out: list[McpServerEntry] = []
        for index, item in enumerate(value):
            if isinstance(item, McpServerEntry):
                out.append(item)
                continue
            if not isinstance(item, dict):
                log.warning("Dropping MCP_SERVERS entry %d: expected an object, got %s", index, type(item).__name__)
                continue
            try:
                out.append(McpServerEntry.model_validate(item))
            except ValidationError as rejection:
                label = str(item.get("id") or "").strip() or f"#{index}"
                log.warning(
                    "Dropping MCP server entry %s from MCP_SERVERS: %s", label, describe_validation_error(rejection)
                )
        return out
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return []
        try:
            parsed = json.loads(text)
        except (TypeError, ValueError) as rejection:
            # The value is not logged: it is the place an operator keeps MCP auth tokens.
            log.warning("MCP_SERVERS is not valid JSON and no MCP server is configured: %s", rejection)
            return []
        if isinstance(parsed, list):
            return _coerce_mcp_servers(parsed)
        log.warning("MCP_SERVERS must be a JSON array; got %s, so no MCP server is configured", type(parsed).__name__)
    return []


def _coerce_user_enabled_skill_ids(value: Any) -> list[str]:
    """Normalize an environment allowlist without accepting malformed values."""
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return []
        try:
            parsed = json.loads(text)
        except (TypeError, ValueError) as failure:
            if text.startswith(("[", "{")):
                raise ValueError("enabled skill ids must be a list of non-empty strings") from failure
            parsed = text.split(",")
    else:
        parsed = value
    if not isinstance(parsed, list):
        raise ValueError("enabled skill ids must be a list of non-empty strings")
    normalized: list[str] = []
    for item in parsed:
        if not isinstance(item, str) or not item.strip():
            raise ValueError("enabled skill ids must be a list of non-empty strings")
        skill_id = item.strip()
        if skill_id not in normalized:
            normalized.append(skill_id)
    return normalized


def _coerce_a2a_remote_agents(value: Any) -> list[A2aRemoteAgentEntry]:
    """A list or a JSON string. A malformed entry is dropped **and named**, as in
    `_coerce_mcp_servers` and for the same reason."""
    if isinstance(value, list):
        out: list[A2aRemoteAgentEntry] = []
        for index, item in enumerate(value):
            if isinstance(item, A2aRemoteAgentEntry):
                out.append(item)
                continue
            if not isinstance(item, dict):
                log.warning(
                    "Dropping A2A_REMOTE_AGENTS entry %d: expected an object, got %s", index, type(item).__name__
                )
                continue
            try:
                out.append(A2aRemoteAgentEntry.model_validate(item))
            except ValidationError as rejection:
                label = str(item.get("id") or "").strip() or f"#{index}"
                log.warning(
                    "Dropping A2A remote agent entry %s from A2A_REMOTE_AGENTS: %s",
                    label,
                    describe_validation_error(rejection),
                )
        return out
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return []
        try:
            parsed = json.loads(text)
        except (TypeError, ValueError) as rejection:
            # The value is not logged: it is the place an operator keeps A2A auth tokens.
            log.warning("A2A_REMOTE_AGENTS is not valid JSON and no remote agent is configured: %s", rejection)
            return []
        if isinstance(parsed, list):
            return _coerce_a2a_remote_agents(parsed)
        log.warning(
            "A2A_REMOTE_AGENTS must be a JSON array; got %s, so no remote agent is configured", type(parsed).__name__
        )
    return []


class AppSettings(BaseModel):
    """Instance configuration (flat UPPER_SNAKE keys; env + in-memory only)."""

    model_config = ConfigDict(extra="allow", populate_by_name=True)

    GIGACHAT_B2BANK_BASE_URL: str = ""
    GIGACHAT_B2BANK_CERT_PATH: SecretStr = SecretStr("")
    GIGACHAT_B2BANK_KEY_PATH: SecretStr = SecretStr("")
    GIGACHAT_B2BANK_CA_PATH: SecretStr = SecretStr("")
    GIGACHAT_B2BANK_VERIFY_SSL: bool = True
    GIGACHAT_B2BANK_CHAT_API_VERSION: Literal["v1", "v2"] = "v1"
    GIGACHAT_B2BANK_CLIENT_ID: str = ""
    GIGACHAT_B2BANK_TEMPERATURE: float = 0.0
    GIGACHAT_B2BANK_TOP_P: float = 0.0
    GIGACHAT_B2BANK_MAX_TOKENS: int = 0
    GIGACHAT_B2BANK_PARALLEL_TOOL_CALLS: bool = False
    #: Canary check of a new GigaChat version (TN09). Off by default; share clamped on read.
    #: The version under test is named outright rather than derived from a suffix, so the
    #: check cannot route traffic to a model the platform never published.
    GIGACHAT_PREVIEW_ENABLED: bool = False
    GIGACHAT_PREVIEW_MODEL: str = ""
    GIGACHAT_PREVIEW_PROBABILITY: float = 0.0

    CLOUDRU_FOUNDATION_MODELS_API_KEY: SecretStr = SecretStr("")
    CLOUDRU_FOUNDATION_MODELS_BASE_URL: str = CLOUDRU_FOUNDATION_MODELS_BASE_URL

    SELFHOSTED_LLM_BASE_URL: str = ""
    SELFHOSTED_LLM_API_KEY: SecretStr = SecretStr("")
    SELFHOSTED_LLM_CERT_PATH: str = ""
    SELFHOSTED_LLM_KEY_PATH: str = ""
    SELFHOSTED_LLM_CA_PATH: str = ""
    SELFHOSTED_LLM_VERIFY_SSL: bool = True

    GIGASEARCH_BASE_URL: str = ""
    GIGASEARCH_USER_ID: str = ""
    GIGASEARCH_SURFACE_ID: str = ""
    GIGASEARCH_API_KEY: SecretStr = SecretStr("")
    GIGASEARCH_TIMEOUT_SEC: float = 0.0
    GIGASEARCH_CERT_PATH: str = ""
    GIGASEARCH_KEY_PATH: str = ""
    GIGASEARCH_CA_PATH: str = ""
    GIGASEARCH_VERIFY_SSL: bool = True

    FUSIONBRAIN_BASE_URL: str = ""
    FUSIONBRAIN_PIPELINE_ID: str = ""
    FUSIONBRAIN_CERT_PATH: SecretStr = SecretStr("")
    FUSIONBRAIN_KEY_PATH: SecretStr = SecretStr("")
    FUSIONBRAIN_CA_PATH: SecretStr = SecretStr("")
    FUSIONBRAIN_VERIFY_SSL: bool = True

    DLP_BASE_URL: str = ""
    DLP_MAPPING_SCHEMA: str = "orion"
    DLP_SENDER_EMAIL: str = ""
    DLP_RECIPIENT_EMAIL: str = "IHAPI-Internal"
    DLP_SERVER_IP: str = "1.1.1.1"
    DLP_CLIENT_IP: str = "1.1.1.1"
    DLP_SUBJECT: str = "Поиск в интернете"
    DLP_CONTENT_TYPE: str = "application/octet-stream"
    DLP_ALLOW: str = "204"
    DLP_TIMEOUT_SEC: float = 30.0
    # Workstation
    WORKSTATION_BASE_URL: str = ""
    WORKSTATION_CREATE_SECRET: SecretStr = SecretStr("")
    WORKSTATION_SESSION_STRATEGY: Literal["ephemeral", "recover"] = "ephemeral"
    WORKSTATION_IDLE_TIMEOUT_SEC: int = 900
    WORKSTATION_ABSOLUTE_TTL_SEC: int = 3600
    WORKSTATION_COMMAND_TIMEOUT_SEC: int = 300
    WORKSTATION_HTTP_TIMEOUT_SEC: float = 30.0
    WORKSTATION_MAX_ATTEMPTS: int = 3
    WORKSTATION_RETRY_BASE_DELAY_MS: int = 100
    WORKSTATION_ACCESS_LEVEL: Literal["rw", "ro", "none"] = "rw"
    WORKSTATION_WORKSPACE_ROOT: str = "/workspace"
    WORKSTATION_ATTACHMENT_MAX_BYTES: int = 20 * 1024 * 1024

    GR_ENABLED: bool = False
    GR_CHECK_ONLY_URL: str = ""
    GR_AGENT_ID: str = ""
    GR_MODEL: str = "GigaChat"
    GR_TIMEOUT_SEC: float = Field(default=_GR_TIMEOUT_DEFAULT_SEC, gt=0)
    GR_CONNECT_TIMEOUT_SEC: float = Field(default=_GR_TIMEOUT_DEFAULT_SEC, gt=0)
    GR_VERIFY_SSL: bool = True

    DIALOGS_GRPC_ENDPOINT: str = ""
    DIALOGS_BOT_TOKEN: SecretStr = SecretStr("")
    DIALOGS_GROUP_ID: str = ""
    DIALOGS_USER_ID: str = ""
    DIALOGS_SECURE: bool = True
    DIALOGS_FILE_URL_PORT: int = Field(default=80, ge=1, le=65535)
    DIALOGS_ROOT_CERTIFICATES: str = ""

    DIALOGS_REPLY_ENABLED: bool = False
    DIALOGS_UPDATE_PROGRESS_MESSAGE_ENABLED: bool = False
    DIALOGS_TARGETED_GROUP_MESSAGES_ENABLED: bool = False
    DIALOGS_HISTORY_RECOVERY_ENABLED: bool = False
    DIALOGS_HISTORY_RECOVERY_LIMIT: int = 10
    DIALOGS_FORWARDING_DISABLED: bool = False
    DIALOGS_TYPING_ENABLED: bool = False
    DIALOGS_PARTICIPANT_IDENTITY_ENABLED: bool = False
    DIALOGS_BOT_NICK: str = ""

    OUROBOROS_SERVER_HOST: str = "127.0.0.1"
    OUROBOROS_LOG_LEVEL: str = "INFO"
    OUROBOROS_LOG_FORMAT: Literal["json", "text"] = "json"
    OUROBOROS_LOG_SINK: LogSink = "stdout"
    OUROBOROS_LOG_DIR: str = "/var/log/app"
    OUROBOROS_LOG_ROTATION: Literal["size", "time"] = "size"
    OUROBOROS_LOG_FILE_MAX_BYTES: int = 52_428_800
    OUROBOROS_LOG_FILE_BACKUP_COUNT: int = 5
    OUROBOROS_LOG_ERROR_FILE_ENABLED: bool = False
    #: Selects the backend for *all* broker seams at once — the message gateway and the two
    #: event transports — so a deployment cannot run one on Redis and another in process.
    #: `Literal` so an unknown name is refused here, at load, rather than at first use.
    OUROBOROS_BROKER_BACKEND: Literal["inprocess", "redis", "kafka"] = "inprocess"
    #: Required when the backend is `redis`; the composition refuses the pair when it is empty.
    OUROBOROS_REDIS_URL: str = ""
    #: Every broker key of one deployment starts with it. Two instances configured with
    #: different prefixes form two silent deployments.
    OUROBOROS_REDIS_PREFIX: str = "ouroboros"
    OUROBOROS_KAFKA_BOOTSTRAP_SERVERS: str = ""
    OUROBOROS_KAFKA_TOPIC_PREFIX: str = "ouroboros"
    OUROBOROS_KAFKA_SECURITY_PROTOCOL: Literal["PLAINTEXT", "SSL", "SASL_PLAINTEXT", "SASL_SSL"] = "PLAINTEXT"
    OUROBOROS_KAFKA_SASL_MECHANISM: Literal["PLAIN", "SCRAM-SHA-256", "SCRAM-SHA-512"] = "PLAIN"
    OUROBOROS_KAFKA_SASL_USERNAME: str = ""
    OUROBOROS_KAFKA_SASL_PASSWORD: SecretStr = SecretStr("")
    OUROBOROS_KAFKA_SSL_CA_PATH: str = ""
    OUROBOROS_KAFKA_SSL_CERT_PATH: str = ""
    OUROBOROS_KAFKA_SSL_KEY_PATH: str = ""
    OUROBOROS_KAFKA_API_VERSION: str = ""
    OUROBOROS_KAFKA_MAX_MESSAGE_BYTES: int = Field(default=33_554_432, gt=0)
    OUROBOROS_SETTINGS_CACHE_BACKEND: Literal["none", "inmemory", "redis"] = "none"
    OUROBOROS_SETTINGS_CACHE_TTL_SEC: int = 300
    OUROBOROS_MODEL: str = "gigachat-b2bank::GigaChat-Pro"
    OUROBOROS_MODEL_LIGHT: str = "gigachat-b2bank::GigaChat"
    OUROBOROS_MODEL_FALLBACK: str = "gigachat-b2bank::GigaChat"
    # Model (and optionally reasoning effort) of a subtask by the skill its description names:
    # ``distribution:aifa-plan-author=cloudru::zai-org/GLM-5.2@high, <skill>=<model>``. Environment-only;
    # empty = every subtask runs on OUROBOROS_MODEL like the root.
    OUROBOROS_MODEL_BY_SKILL: str = ""
    # Dedicated vision-capable model used by the vlm_query tool for image
    # analysis. Empty = fall back to OUROBOROS_MODEL (assumed multimodal).
    OUROBOROS_MODEL_VISION: str = ""
    OUROBOROS_MODEL_CONTEXT_TOKENS: int = 200_000
    OUROBOROS_CHAT_SUMMARY_MAX_TOKENS: int = 32_768
    OUROBOROS_CONTEXT_COMPACTION_THRESHOLD_TOKENS: int = 25_000
    OUROBOROS_CONTEXT_COMPACTION_THRESHOLD_MESSAGES: int = 100
    OUROBOROS_SOFT_TIMEOUT_SEC: int = 600
    OUROBOROS_HARD_TIMEOUT_SEC: int = 1800
    # How long a terminating instance waits for its running bodies before releasing their
    # records anyway. Must sit under the orchestrator's termination grace period, or the
    # drain is killed part-way through — which is the failure it exists to prevent. Not the
    # slot grace period: that one is sized above a tool deadline and governs a body that
    # never returns at all.
    OUROBOROS_DRAIN_TIMEOUT_SEC: int = 20
    #: Declares that this instance serves several owners. One flag for the whole set of
    #: behaviours that only hold together — identity resolved at the boundary, per-owner
    #: storage, a catalogue fixed for the process's lifetime, and the exclusions the startup
    #: validation enforces. It also declares that the browser port is fronted by a proxy which
    #: authenticates the caller and overwrites the identity headers on whatever the client
    #: sent: on a directly reachable port that lets any caller claim any owner, so it defaults
    #: off. Read once, at startup, to decide what the composition root builds.
    OUROBOROS_MULTIUSER: bool = False
    OUROBOROS_RUNTIME_DB_URL: str = ""
    # Max numbered failover URLs (OUROBOROS_RUNTIME_DB_URL_1..N); hard-capped at 64 in db_url helpers
    OUROBOROS_RUNTIME_DB_URL_MAX: int = 16
    OUROBOROS_CHAT_ENCRYPTION_ENABLED: bool = False
    OUROBOROS_CHAT_ENCRYPTION_KEY: SecretStr = SecretStr("")
    OUROBOROS_CREDENTIALS_ENCRYPTION_KEY: SecretStr = SecretStr("")
    OUROBOROS_TIMEZONE: str = "Europe/Moscow"
    OUROBOROS_LOCALE: str = "ru-RU"
    OUROBOROS_CONTEXT_RETRIEVAL_ENABLED: bool = True
    OUROBOROS_EPISODE_RETRIEVAL_TOP_K: int = 5
    OUROBOROS_EPISODE_CARD_MAX_CHARS: int = 1000
    OUROBOROS_EPISODE_CONTEXT_TOKENS: int = 8000
    OUROBOROS_EPISODE_RETRIEVAL_CANDIDATES: int = 100
    OUROBOROS_EPISODE_RETENTION_DAYS: int = 90
    OUROBOROS_TOOL_SUMMARY_RETENTION_DAYS: int = 30
    OUROBOROS_TOOL_RAW_RETENTION_DAYS: int = 0
    OUROBOROS_CONTEXT_CLEANUP_ENABLED: bool = True
    OUROBOROS_CONTEXT_CLEANUP_INTERVAL_HOURS: int = 24
    OUROBOROS_CONTEXT_CLEANUP_MAX_ROWS: int = 5000
    OUROBOROS_TOOL_TIMEOUT_SEC: int = 600
    OUROBOROS_ATTACHMENT_DOCUMENT_MAX_BYTES: int = Field(default=50 * 1024 * 1024, gt=0, strict=True)
    OUROBOROS_ATTACHMENT_IMAGE_MAX_BYTES: int = Field(default=20 * 1024 * 1024, gt=0, strict=True)
    OUROBOROS_ATTACHMENT_SLOT_TIMEOUT_SEC: int = Field(default=45, ge=0)
    OUROBOROS_ATTACHMENT_PARSE_TIMEOUT_SEC: int = Field(default=45, gt=0)
    OUROBOROS_ATTACHMENT_PARSER_CPU_SEC: int = Field(default=30, gt=0)
    OUROBOROS_ATTACHMENT_PARSER_JOIN_TIMEOUT_SEC: int = Field(default=1, ge=1)
    OUROBOROS_ATTACHMENT_READ_TIMEOUT_SEC: int = Field(default=120, gt=0)
    # Deployment knobs, latency against database load; out of bounds is rejected, not clamped.
    # Ceiling, not a suggestion: the claim transaction holds its rows across one insert per due
    # row, so lock-hold time on PostgreSQL grows with this number times the round trip.
    OUROBOROS_SCHEDULER_DUE_BATCH_LIMIT: int = Field(default=200, ge=1, le=1_000)
    OUROBOROS_SCHEDULER_POLL_INTERVAL_SEC: float = Field(default=20.0, ge=1.0, le=3600.0)
    OUROBOROS_SCHEDULER_POLL_JITTER_SEC: float = Field(default=5.0, ge=0.0, le=600.0)
    OUROBOROS_TOOL_LOCAL_TMP_ARTIFACTS_ENABLED: bool = False
    OUROBOROS_TOOL_CONFIRMATION_ENABLED: bool = False
    OUROBOROS_BROWSER_BASE_URL: str = ""
    OUROBOROS_WORKER_SLOTS: int = 1
    OUROBOROS_DURABLE_JOBS_ENABLED: bool = False
    OUROBOROS_MAX_ACTIVE_ROOT_JOBS_PER_OWNER: int = Field(default=5, gt=0)
    OUROBOROS_MAX_RUNNING_ROOT_JOBS_PER_OWNER: int = Field(default=3, gt=0)
    OUROBOROS_MAX_INBOUND_MESSAGE_CHARS: int = Field(default=50_000, gt=0)
    OUROBOROS_LLM_CLAIM_POLL_INTERVAL_SEC: float = Field(default=1.0, ge=0.05, le=30, allow_inf_nan=False)
    OUROBOROS_POLIGON: str = "DEV"
    MCP_ENABLED: bool = False
    MCP_SERVERS: list[McpServerEntry] = Field(default_factory=list)
    OUROBOROS_USER_ENABLED_SKILL_IDS: list[str] = Field(default_factory=list)
    #: Distribution skills of an executable type the deployment trusts under ``OUROBOROS_MULTIUSER``;
    #: the ceiling hides every other executable skill. Empty by default.
    OUROBOROS_TRUSTED_EXECUTABLE_SKILL_IDS: list[str] = Field(default_factory=list)
    MCP_TOOL_TIMEOUT_SEC: int = 60
    #: How long one JSON-RPC request to an MCP server may take — a property of *their* server. The
    #: connection's start-up budget is computed from this, so raising it raises that too.
    MCP_REQUEST_TIMEOUT_SEC: float = 30.0
    #: How long a stream the transport holds may be silent before the client treats it as gone — a
    #: property of *your* network. Set it under the shortest idle timeout on the path.
    MCP_STREAM_READ_TIMEOUT_SEC: float = 300.0
    OUROBOROS_USER_ENABLED_MCP_IDS: list[str] = Field(default_factory=list)
    OUROBOROS_EFFORT_TASK: EffortLevel = "medium"

    BROWSER_INGRESS_ENABLED: bool = False
    A2A_ENABLED: bool = False
    A2A_PORT: int = 18800
    A2A_HOST: str = "127.0.0.1"
    A2A_AGENT_NAME: str = ""
    A2A_AGENT_DESCRIPTION: str = ""
    A2A_PUBLIC_URL: str = ""
    A2A_MAX_CONCURRENT: int = 3
    A2A_TASK_TTL_HOURS: int = 24

    # Outbound A2A: reaching *other* agents, unlike the A2A_* keys above, which describe this
    # agent's own server.
    A2A_EGRESS_ENABLED: bool = False
    A2A_REMOTE_AGENTS: list[A2aRemoteAgentEntry] = Field(default_factory=list)
    A2A_TOOL_TIMEOUT_SEC: int = 300

    AEF_TRACING_ENABLED: bool = False
    AEF_TRACING_KAFKA_TOPIC: str = ""
    AEF_TRACING_SDK_LIST: str = ""
    AEF_TRACING_SDK_CONTRACT_VERSION: str = "1.5"
    AEF_TRACING_AUTO_INSTRUMENTATION_ENABLED: bool = False
    # Debug contours only: tool spans carry sanitized arguments instead of the
    # hard "***" mask. Declared log_redacted_args stay hidden; results stay masked.
    AEF_TOOL_TRACE_PAYLOAD_ENABLED: bool = False
    AEF_REFLEX_ENABLED: bool = False
    DYNATRACE_ENABLED: bool = False
    ENABLE_REFLEX: bool = False

    OUROBOROS_SANDBOX_BACKEND: str = "soft"
    OUROBOROS_SANDBOX_DENY_PATHS: list[str] = Field(default_factory=list)
    OUROBOROS_SKILL_EXEC_MAX_STDOUT_BYTES: int = 256 * 1024
    OUROBOROS_SKILL_EXEC_MAX_STDERR_BYTES: int = 128 * 1024
    # Char cap on the rendered skill_exec result the model receives; 0 keeps the
    # skill_exec catalog limit.
    OUROBOROS_SKILL_EXEC_RESULT_LIMIT: int = Field(default=0, ge=0, le=_MAX_TOOL_RESULT_LIMIT)

    OUROBOROS_S3_BUCKET: str = ""
    OUROBOROS_S3_REGION: str = ""
    OUROBOROS_S3_ENDPOINT: str = ""
    OUROBOROS_S3_ACCESS_KEY: SecretStr = SecretStr("")
    OUROBOROS_S3_SECRET_KEY: SecretStr = SecretStr("")
    OUROBOROS_STORAGE_TENANT_ID: str = "default"
    OUROBOROS_S3_PREFIX: str = ""
    OUROBOROS_USER_FILE_TTL_HOURS: int = 720
    OUROBOROS_FILE_TRANSFERS: int = Field(default=8, ge=1, le=128)
    OUROBOROS_FILE_OWNER_TRANSFERS: int = Field(default=2, ge=1, le=128)
    OUROBOROS_FILE_IO_TIMEOUT_SEC: int = Field(default=120, ge=1, le=900)
    OUROBOROS_S3_POOL_SIZE: int = Field(default=8, ge=1, le=128)
    OUROBOROS_S3_CONNECT_TIMEOUT_SEC: float = Field(default=5, gt=0, le=120)
    OUROBOROS_S3_READ_TIMEOUT_SEC: float = Field(default=30, gt=0, le=300)
    OUROBOROS_S3_TOTAL_MAX_ATTEMPTS: int = Field(default=3, ge=1, le=10)
    OUROBOROS_DB_ASYNC_POOL_SIZE: int = Field(default=4, ge=1)
    OUROBOROS_DB_ASYNC_MAX_OVERFLOW: int = Field(default=4, ge=0)
    OUROBOROS_DB_POOL_TIMEOUT_SEC: int = Field(default=5, ge=1)
    OUROBOROS_DB_POOL_PRE_PING: bool = True
    OUROBOROS_JOB_SCRATCH_TTL_HOURS: int = 24

    WORKSTATION_S3_CACHE_ENABLED: bool = True
    WORKSTATION_S3_CACHE_MAX_BYTES: int = 10 * 1024 * 1024

    OUROBOROS_SUPERVISOR_STARTUP_TIMEOUT_SEC: float = 300.0
    OUROBOROS_SUPERVISOR_RETRY_INITIAL_DELAY_SEC: float = Field(default=2.0, gt=0, allow_inf_nan=False)
    OUROBOROS_SUPERVISOR_RETRY_MAX_DELAY_SEC: float = Field(default=30.0, gt=0, le=30, allow_inf_nan=False)
    OUROBOROS_LOG_TRACEBACK: bool = False
    OUROBOROS_LLM_HTTP_READ_TIMEOUT_SEC: float = 300.0
    OUROBOROS_BACKGROUND_LLM_HTTP_READ_TIMEOUT_SEC: float = 300.0
    OUROBOROS_LLM_HTTP_CONNECT_TIMEOUT_SEC: float = 30.0
    OUROBOROS_LLM_MAX_CONCURRENCY: int = 2
    OUROBOROS_MAX_ROUNDS: int = 200
    OUROBOROS_REASONING_PROGRESS_ENABLED: bool = True
    OUROBOROS_CHECKPOINT_PROGRESS_ENABLED: bool = False
    OUROBOROS_SLOW_LLM_STATUS_ENABLED: bool = True
    OUROBOROS_SLOW_LLM_STATUS_SEC: int = 30
    OUROBOROS_EMERGENCY_COMPACTION_CHARS: int = 0
    OUROBOROS_SYSTEM_PROMPT_TOKENS: int = 0
    # JSON-encoded bytes of extension tool schemas allowed into the first prompt.
    OUROBOROS_INITIAL_EXTENSION_SCHEMA_BUDGET: int = Field(default=8000, gt=0)
    OUROBOROS_ENABLED_TOOLS: str = ""
    # Comma/space separated tool names to hide from the agent and block from execution.
    OUROBOROS_DISABLED_TOOLS: str = ""
    # Comma/space separated canonical skill ids to drop from discovery, together with their tools.
    OUROBOROS_DISABLED_SKILLS: str = ""
    OUROBOROS_MEMORY_DISTILL: bool = True
    OUROBOROS_MEMORY_DISTILL_MAX_TOKENS: int = Field(default=32_768, gt=0)

    USE_LOCAL_MAIN: str = ""
    USE_LOCAL_LIGHT: str = ""
    USE_LOCAL_FALLBACK: str = ""
    LOCAL_MODEL_CONTEXT_LENGTH: int = 0

    OPENAI_API_KEY: SecretStr = SecretStr("")
    OPENAI_BASE_URL: str = ""
    ANTHROPIC_API_KEY: SecretStr = SecretStr("")
    OPENROUTER_API_KEY: SecretStr = SecretStr("")
    OPENAI_COMPATIBLE_API_KEY: SecretStr = SecretStr("")
    OPENAI_COMPATIBLE_BASE_URL: str = ""

    TOOL_CALCULATOR_MAX_NUMBERS: int = 0
    DRIVE_ROOT: str = ""

    AGENT_SYSTEM_CONFIGURATION_KAFKA_ENABLED: str = ""
    AGENT_SYSTEM_CONFIGURATION_KAFKA_BOOTSTRAP_SERVERS: str = ""
    AGENT_SYSTEM_CONFIGURATION_KAFKA_TOPIC: str = ""
    AGENT_SYSTEM_CONFIGURATION_AGENT_ID: str = ""
    AGENT_ID: str = ""
    AGENT_SYSTEM_CONFIGURATION_CLUSTER_ID: str = ""
    AGENT_SYSTEM_CONFIGURATION_NAMESPACE: str = ""
    AGENT_SYSTEM_CONFIGURATION_DISTRIBUTIVE: str = ""
    DISTRIBUTIVE: str = ""
    AGENT_SYSTEM_CONFIGURATION_AGENT_VERSION: str = ""
    AGENT_VERSION: str = ""
    AGENT_SYSTEM_CONFIGURATION_REPO_CODE: str = ""
    AGENT_SYSTEM_CONFIGURATION_APP_CI: str = ""
    AGENT_SYSTEM_CONFIGURATION_MMS_AGENT_VERSION: str = ""
    AGENT_SYSTEM_CONFIGURATION_VERSION_NO: str = ""
    AGENT_SYSTEM_CONFIGURATION_FILENAME: str = ""

    GITHUB_USER: str = ""
    GITHUB_REPO: str = ""

    @field_validator(
        "GIGACHAT_B2BANK_VERIFY_SSL",
        "GIGACHAT_B2BANK_PARALLEL_TOOL_CALLS",
        "GIGACHAT_PREVIEW_ENABLED",
        "SELFHOSTED_LLM_VERIFY_SSL",
        "GIGASEARCH_VERIFY_SSL",
        "FUSIONBRAIN_VERIFY_SSL",
        "GR_ENABLED",
        "GR_VERIFY_SSL",
        "DIALOGS_SECURE",
        "DIALOGS_REPLY_ENABLED",
        "DIALOGS_UPDATE_PROGRESS_MESSAGE_ENABLED",
        "DIALOGS_TARGETED_GROUP_MESSAGES_ENABLED",
        "DIALOGS_HISTORY_RECOVERY_ENABLED",
        "DIALOGS_FORWARDING_DISABLED",
        "DIALOGS_TYPING_ENABLED",
        "DIALOGS_PARTICIPANT_IDENTITY_ENABLED",
        "MCP_ENABLED",
        "BROWSER_INGRESS_ENABLED",
        "OUROBOROS_MULTIUSER",
        "A2A_ENABLED",
        "A2A_EGRESS_ENABLED",
        "AEF_TRACING_ENABLED",
        "AEF_TOOL_TRACE_PAYLOAD_ENABLED",
        "OUROBOROS_LOG_TRACEBACK",
        "OUROBOROS_LOG_ERROR_FILE_ENABLED",
        "AEF_REFLEX_ENABLED",
        "DYNATRACE_ENABLED",
        "ENABLE_REFLEX",
        "OUROBOROS_CHAT_ENCRYPTION_ENABLED",
        "OUROBOROS_CONTEXT_RETRIEVAL_ENABLED",
        "OUROBOROS_CONTEXT_CLEANUP_ENABLED",
        "OUROBOROS_TOOL_LOCAL_TMP_ARTIFACTS_ENABLED",
        "OUROBOROS_TOOL_CONFIRMATION_ENABLED",
        "OUROBOROS_CHECKPOINT_PROGRESS_ENABLED",
        "OUROBOROS_REASONING_PROGRESS_ENABLED",
        "OUROBOROS_SLOW_LLM_STATUS_ENABLED",
        mode="before",
    )
    @classmethod
    def _validate_bool_fields(cls, value: Any) -> bool:
        return _coerce_bool(value)

    @field_validator(
        "GIGACHAT_B2BANK_MAX_TOKENS",
        "OUROBOROS_SOFT_TIMEOUT_SEC",
        "OUROBOROS_HARD_TIMEOUT_SEC",
        "OUROBOROS_DRAIN_TIMEOUT_SEC",
        "OUROBOROS_TOOL_TIMEOUT_SEC",
        "OUROBOROS_EPISODE_RETRIEVAL_TOP_K",
        "OUROBOROS_EPISODE_CARD_MAX_CHARS",
        "OUROBOROS_EPISODE_CONTEXT_TOKENS",
        "OUROBOROS_EPISODE_RETRIEVAL_CANDIDATES",
        "OUROBOROS_EPISODE_RETENTION_DAYS",
        "OUROBOROS_TOOL_SUMMARY_RETENTION_DAYS",
        "OUROBOROS_TOOL_RAW_RETENTION_DAYS",
        "OUROBOROS_CONTEXT_CLEANUP_INTERVAL_HOURS",
        "OUROBOROS_CONTEXT_CLEANUP_MAX_ROWS",
        "MCP_TOOL_TIMEOUT_SEC",
        "A2A_PORT",
        "A2A_MAX_CONCURRENT",
        "A2A_TASK_TTL_HOURS",
        "A2A_TOOL_TIMEOUT_SEC",
        "DIALOGS_HISTORY_RECOVERY_LIMIT",
        "OUROBOROS_USER_FILE_TTL_HOURS",
        "OUROBOROS_JOB_SCRATCH_TTL_HOURS",
        "OUROBOROS_MODEL_CONTEXT_TOKENS",
        "OUROBOROS_CHAT_SUMMARY_MAX_TOKENS",
        "OUROBOROS_MEMORY_DISTILL_MAX_TOKENS",
        "OUROBOROS_CONTEXT_COMPACTION_THRESHOLD_TOKENS",
        "OUROBOROS_CONTEXT_COMPACTION_THRESHOLD_MESSAGES",
        "OUROBOROS_MAX_ROUNDS",
        "OUROBOROS_SLOW_LLM_STATUS_SEC",
        "OUROBOROS_LLM_MAX_CONCURRENCY",
        "OUROBOROS_EMERGENCY_COMPACTION_CHARS",
        "OUROBOROS_SYSTEM_PROMPT_TOKENS",
        "OUROBOROS_INITIAL_EXTENSION_SCHEMA_BUDGET",
        "OUROBOROS_SKILL_EXEC_MAX_STDOUT_BYTES",
        "OUROBOROS_SKILL_EXEC_MAX_STDERR_BYTES",
        "LOCAL_MODEL_CONTEXT_LENGTH",
        "TOOL_CALCULATOR_MAX_NUMBERS",
        "OUROBOROS_EPISODE_RETRIEVAL_TOP_K",
        "OUROBOROS_EPISODE_CARD_MAX_CHARS",
        "OUROBOROS_EPISODE_CONTEXT_TOKENS",
        "OUROBOROS_EPISODE_RETRIEVAL_CANDIDATES",
        "OUROBOROS_EPISODE_RETENTION_DAYS",
        "OUROBOROS_TOOL_SUMMARY_RETENTION_DAYS",
        "OUROBOROS_TOOL_RAW_RETENTION_DAYS",
        "OUROBOROS_CONTEXT_CLEANUP_INTERVAL_HOURS",
        "OUROBOROS_CONTEXT_CLEANUP_MAX_ROWS",
        "WORKSTATION_IDLE_TIMEOUT_SEC",
        "WORKSTATION_ABSOLUTE_TTL_SEC",
        "WORKSTATION_COMMAND_TIMEOUT_SEC",
        "WORKSTATION_MAX_ATTEMPTS",
        "WORKSTATION_RETRY_BASE_DELAY_MS",
        "WORKSTATION_ATTACHMENT_MAX_BYTES",
        mode="before",
    )
    @classmethod
    def _validate_int_fields(cls, value: Any) -> int:
        return _coerce_int(value)

    @field_validator(
        "OUROBOROS_SCHEDULER_DUE_BATCH_LIMIT",
        "OUROBOROS_SCHEDULER_POLL_INTERVAL_SEC",
        "OUROBOROS_SCHEDULER_POLL_JITTER_SEC",
        mode="before",
    )
    @classmethod
    def _blank_scheduler_knob_keeps_its_default(cls, value: Any, info: ValidationInfo) -> Any:
        # A secret file or a rendered template can arrive as whitespace, and only a value someone
        # typed on purpose should ever reach the bounds below.
        if value is None or (isinstance(value, str) and not value.strip()):
            return cls.model_fields[str(info.field_name)].default
        return value

    @field_validator("OUROBOROS_ATTACHMENT_DOCUMENT_MAX_BYTES", "OUROBOROS_ATTACHMENT_IMAGE_MAX_BYTES", mode="before")
    @classmethod
    def _validate_attachment_bytes(cls, value: Any) -> Any:
        return int(value) if isinstance(value, str) else value

    @field_validator("GIGACHAT_PREVIEW_PROBABILITY", mode="before")
    @classmethod
    def _clamp_gigachat_preview_probability(cls, value: Any) -> float:
        # Clamping rather than rejecting keeps a mistyped deploy value safe.
        share = _coerce_float(value)
        # NaN slides through min/max, so it would only surface as a crash in the selector.
        if share != share:
            return 0.0
        return min(max(share, 0.0), PREVIEW_MAX_PROBABILITY)

    @field_validator("DIALOGS_HISTORY_RECOVERY_LIMIT")
    @classmethod
    def _validate_dialogs_history_recovery_limit(cls, value: int) -> int:
        if not 1 <= value <= 100:
            raise ValueError("DIALOGS_HISTORY_RECOVERY_LIMIT must be between 1 and 100")
        return value

    @field_validator(
        "GIGACHAT_B2BANK_TEMPERATURE",
        "GIGACHAT_B2BANK_TOP_P",
        "OUROBOROS_SUPERVISOR_STARTUP_TIMEOUT_SEC",
        "OUROBOROS_LLM_HTTP_READ_TIMEOUT_SEC",
        "OUROBOROS_BACKGROUND_LLM_HTTP_READ_TIMEOUT_SEC",
        "OUROBOROS_LLM_HTTP_CONNECT_TIMEOUT_SEC",
        "GIGASEARCH_TIMEOUT_SEC",
        "WORKSTATION_HTTP_TIMEOUT_SEC",
        "MCP_REQUEST_TIMEOUT_SEC",
        "MCP_STREAM_READ_TIMEOUT_SEC",
        "GR_TIMEOUT_SEC",
        mode="before",
    )
    @classmethod
    def _validate_float_fields(cls, value: Any) -> float:
        return _coerce_float(value)

    @field_validator("GR_TIMEOUT_SEC", "GR_CONNECT_TIMEOUT_SEC", mode="before")
    @classmethod
    def _validate_gr_timeouts(cls, value: Any) -> float:
        return _coerce_float(value, default=_GR_TIMEOUT_DEFAULT_SEC)

    @field_validator("OUROBOROS_LOG_LEVEL", mode="before")
    @classmethod
    def _normalize_log_level(cls, value: Any) -> str:
        text = str(value or "").strip().upper()
        return text or "INFO"

    @field_validator("OUROBOROS_LOG_FORMAT", mode="before")
    @classmethod
    def _normalize_log_format(cls, value: Any) -> str:
        return _coerce_choice(value, allowed=("json", "text"), default="json")

    @field_validator("OUROBOROS_LOG_SINK", mode="before")
    @classmethod
    def _normalize_log_sink(cls, value: Any) -> str:
        return normalize_log_sink(value)

    @field_validator("OUROBOROS_LOG_ROTATION", mode="before")
    @classmethod
    def _normalize_log_rotation(cls, value: Any) -> str:
        return _coerce_choice(value, allowed=("size", "time"), default="size")

    @field_validator("OUROBOROS_LOG_DIR", mode="before")
    @classmethod
    def _normalize_log_dir(cls, value: Any) -> str:
        return str(value or "").strip() or "/var/log/app"

    # Not in the shared int list: its garbage fallback of 0 disables rotation,
    # so only an explicit 0 may mean that — negatives are garbage too.
    @field_validator("OUROBOROS_LOG_FILE_MAX_BYTES", mode="before")
    @classmethod
    def _validate_log_file_max_bytes(cls, value: Any) -> int:
        size = _coerce_int(value, default=52_428_800)
        return size if size >= 0 else 52_428_800

    # 0 means "keep every file" under time rotation; the size handler floors it.
    @field_validator("OUROBOROS_LOG_FILE_BACKUP_COUNT", mode="before")
    @classmethod
    def _validate_log_file_backup_count(cls, value: Any) -> int:
        count = _coerce_int(value, default=5)
        return count if count >= 0 else 5

    @field_validator("MCP_SERVERS", mode="before")
    @classmethod
    def _validate_mcp_servers(cls, value: Any) -> list[McpServerEntry]:
        return _coerce_mcp_servers(value)

    @field_validator("OUROBOROS_USER_ENABLED_SKILL_IDS", mode="before")
    @classmethod
    def _validate_user_enabled_skill_ids(cls, value: Any) -> list[str]:
        return _coerce_user_enabled_skill_ids(value)

    @field_validator("OUROBOROS_TRUSTED_EXECUTABLE_SKILL_IDS", mode="before")
    @classmethod
    def _validate_trusted_executable_skill_ids(cls, value: Any) -> list[str]:
        ids = _coerce_user_enabled_skill_ids(value)
        foreign = [item for item in ids if not item.startswith("distribution:") or item == "distribution:"]
        if foreign:
            raise ValueError(f"trusted executable skill ids must be distribution:<skill> ids, got {foreign}")
        return ids

    @field_validator("A2A_REMOTE_AGENTS", mode="before")
    @classmethod
    def _validate_a2a_remote_agents(cls, value: Any) -> list[A2aRemoteAgentEntry]:
        return _coerce_a2a_remote_agents(value)

    @field_validator("OUROBOROS_SANDBOX_DENY_PATHS", mode="before")
    @classmethod
    def _coerce_sandbox_deny_paths(cls, value: Any) -> list[str]:
        if isinstance(value, list):
            return [str(v) for v in value if str(v).strip()]
        if isinstance(value, str):
            text = value.strip()
            if not text:
                return []
            try:
                parsed = json.loads(text)
                if isinstance(parsed, list):
                    return [str(v) for v in parsed if str(v).strip()]
            except (TypeError, ValueError):
                pass
        return []

    @field_validator("OUROBOROS_SANDBOX_DENY_PATHS")
    @classmethod
    def _normalize_sandbox_deny_paths(cls, value: list[str]) -> list[str]:
        normalized: list[str] = []
        for raw in value:
            text = str(raw).strip()
            if not text:
                continue
            path = Path(text).expanduser()
            try:
                normalized.append(str(path.resolve()))
            except OSError:
                normalized.append(str(path.absolute()))
        return normalized

    @field_validator("OUROBOROS_USER_ENABLED_MCP_IDS", mode="before")
    @classmethod
    def _coerce_user_enabled_mcp_ids(cls, value: Any) -> list[str]:
        if isinstance(value, list):
            return [str(v).strip() for v in value if str(v).strip()]
        if isinstance(value, str):
            text = value.strip()
            if not text:
                return []
            try:
                parsed = json.loads(text)
                if isinstance(parsed, list):
                    return [str(v).strip() for v in parsed if str(v).strip()]
                return []
            except (TypeError, ValueError):
                pass
            return [part.strip() for part in text.split(",") if part.strip()]
        return []

    @field_validator(
        "GIGACHAT_B2BANK_CERT_PATH",
        "GIGACHAT_B2BANK_KEY_PATH",
        "GIGACHAT_B2BANK_CA_PATH",
        "CLOUDRU_FOUNDATION_MODELS_API_KEY",
        "SELFHOSTED_LLM_API_KEY",
        "GIGASEARCH_API_KEY",
        "FUSIONBRAIN_CERT_PATH",
        "FUSIONBRAIN_KEY_PATH",
        "FUSIONBRAIN_CA_PATH",
        "DIALOGS_BOT_TOKEN",
        "OUROBOROS_S3_ACCESS_KEY",
        "OUROBOROS_S3_SECRET_KEY",
        "OPENAI_API_KEY",
        "ANTHROPIC_API_KEY",
        "OPENROUTER_API_KEY",
        "OPENAI_COMPATIBLE_API_KEY",
        "WORKSTATION_CREATE_SECRET",
        "OUROBOROS_CREDENTIALS_ENCRYPTION_KEY",
        "OUROBOROS_KAFKA_SASL_PASSWORD",
        mode="before",
    )
    @classmethod
    def _coerce_secret_fields(cls, value: Any) -> SecretStr:
        if isinstance(value, SecretStr):
            return value
        return SecretStr(str(value or ""))

    @model_validator(mode="before")
    @classmethod
    def _filter_retired_and_reject_lowercase_extra_keys(cls, data: Any) -> Any:
        if not isinstance(data, Mapping):
            return data
        filtered = {key: value for key, value in data.items() if not is_retired_setting_key(key)}
        for key in filtered:
            if key in cls.model_fields:
                continue
            text = str(key)
            if text != text.upper() or not _CUSTOM_KEY_RE.match(text):
                raise ValueError(f"Invalid settings key: {key!r}")
        return filtered

    @model_validator(mode="after")
    def _validate_durable_job_limits(self) -> AppSettings:
        if self.OUROBOROS_MAX_RUNNING_ROOT_JOBS_PER_OWNER > self.OUROBOROS_MAX_ACTIVE_ROOT_JOBS_PER_OWNER:
            raise ValueError("maximum running root jobs cannot exceed maximum active root jobs")
        return self

    @property
    def attachment_parser_budget_sec(self) -> int:
        return (
            self.OUROBOROS_ATTACHMENT_SLOT_TIMEOUT_SEC
            + self.OUROBOROS_ATTACHMENT_PARSE_TIMEOUT_SEC
            + 3 * self.OUROBOROS_ATTACHMENT_PARSER_JOIN_TIMEOUT_SEC
            + 1
        )

    @model_validator(mode="after")
    def _validate_browser_transfer_reserve(self) -> AppSettings:
        if (self.BROWSER_INGRESS_ENABLED or self.OUROBOROS_MULTIUSER) and min(
            self.OUROBOROS_FILE_TRANSFERS, self.OUROBOROS_FILE_OWNER_TRANSFERS
        ) < 2:
            raise ValueError("browser file transfer limits require at least two slots, including one outgoing slot")
        return self

    def known_field_names(self) -> frozenset[str]:
        return frozenset(type(self).model_fields.keys())

    def get_field_value(self, key: str) -> Any:
        return getattr(self, key, None)

    def extra_fields(self) -> dict[str, Any]:
        known = self.known_field_names()
        return {key: value for key, value in self.model_dump(mode="json").items() if key not in known}

    def get(self, key: str, default: Any = None) -> Any:
        """Dict-like access for transitional call sites."""
        if key in type(self).model_fields:
            value = getattr(self, key)
            if isinstance(value, SecretStr):
                return value.get_secret_value()
            if key in ("MCP_SERVERS", "A2A_REMOTE_AGENTS"):
                return [entry.model_dump(mode="json") for entry in value]
            return value
        extras = self.__pydantic_extra__ or {}
        if key in extras:
            return extras[key]
        return default

    def __contains__(self, key: object) -> bool:
        text = str(key)
        if text in type(self).model_fields:
            return True
        return text in (self.__pydantic_extra__ or {})

    def __getitem__(self, key: str) -> Any:
        if key not in self:
            raise KeyError(key)
        value = self.get(key)
        if value is None and key in self.model_fields:
            field = self.model_fields[key]
            if field.default is not None or field.default_factory is not None:
                return value
        return value

    def to_persist_dict(self) -> dict[str, Any]:
        """Serialize to a plain dict (plain strings, entry lists as lists of dicts)."""
        payload = self.model_dump(mode="json")
        payload["MCP_SERVERS"] = [entry.model_dump(mode="json") for entry in self.MCP_SERVERS]
        payload["A2A_REMOTE_AGENTS"] = [entry.model_dump(mode="json") for entry in self.A2A_REMOTE_AGENTS]
        for key in (
            "GIGACHAT_B2BANK_CERT_PATH",
            "GIGACHAT_B2BANK_KEY_PATH",
            "GIGACHAT_B2BANK_CA_PATH",
            "CLOUDRU_FOUNDATION_MODELS_API_KEY",
            "SELFHOSTED_LLM_API_KEY",
            "GIGASEARCH_API_KEY",
            "FUSIONBRAIN_CERT_PATH",
            "FUSIONBRAIN_KEY_PATH",
            "FUSIONBRAIN_CA_PATH",
            "DIALOGS_BOT_TOKEN",
            "OUROBOROS_S3_ACCESS_KEY",
            "OUROBOROS_S3_SECRET_KEY",
            "OPENAI_API_KEY",
            "ANTHROPIC_API_KEY",
            "OPENROUTER_API_KEY",
            "OPENAI_COMPATIBLE_API_KEY",
            "WORKSTATION_CREATE_SECRET",
            "OUROBOROS_CHAT_ENCRYPTION_KEY",
            "OUROBOROS_CREDENTIALS_ENCRYPTION_KEY",
            "OUROBOROS_KAFKA_SASL_PASSWORD",
        ):
            val = getattr(self, key)
            if isinstance(val, SecretStr):
                payload[key] = val.get_secret_value()
        extras = self.__pydantic_extra__ or {}
        for key, value in extras.items():
            payload[key] = value
        return payload

    @classmethod
    def from_mapping(cls, data: dict[str, Any]) -> AppSettings:
        return cls.model_validate(data)


SettingsLike: TypeAlias = "AppSettings | dict[str, Any]"


def get_defaults() -> AppSettings:
    return AppSettings()


def get_defaults_dict() -> dict[str, Any]:
    return get_defaults().to_persist_dict()
