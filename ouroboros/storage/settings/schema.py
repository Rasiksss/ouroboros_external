"""Pydantic schema for flat UPPER_SNAKE configuration keys (env + memory)."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Literal, TypeAlias

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    field_validator,
    model_validator,
)

from ouroboros.llm.provider_models import CLOUDRU_FOUNDATION_MODELS_BASE_URL

from .retired import is_retired_setting_key

_CUSTOM_KEY_RE = re.compile(r"^[A-Z][A-Z0-9_]{2,}$")
_TRUTHY = frozenset({"1", "true", "yes", "on"})
_FALSY = frozenset({"0", "false", "no", "off"})
# Shared by every configured external tool source that may cap its own results.
_MAX_TOOL_RESULT_LIMIT = 120_000

_ASSIGNMENT_SMTP_TLS_VERSIONS = {
    "1.2": "TLSv1.2",
    "TLS1.2": "TLSv1.2",
    "TLSV1.2": "TLSv1.2",
    "TLSV1_2": "TLSv1.2",
    "TLS_1_2": "TLSv1.2",
    "1.3": "TLSv1.3",
    "TLS1.3": "TLSv1.3",
    "TLSV1.3": "TLSv1.3",
    "TLSV1_3": "TLSv1.3",
    "TLS_1_3": "TLSv1.3",
}


EffortLevel = Literal["none", "low", "medium", "high"]


class McpServerEntry(BaseModel):
    """One MCP server entry from settings.json."""

    model_config = ConfigDict(extra="allow")

    id: str = ""
    name: str = ""
    enabled: bool = True
    transport: Literal["stdio", "sse", "streamable_http"] = "streamable_http"
    url: str = ""
    auth_header: str = ""
    auth_token: str = ""
    command: str = ""
    args: list[str] = Field(default_factory=list)
    env: dict[str, str] = Field(default_factory=dict)
    allowed_tools: list[str] = Field(default_factory=list)
    retrieval_aliases: list[str] = Field(default_factory=list)
    tool_result_limits: dict[str, int] = Field(default_factory=dict)

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
    """One outbound A2A remote agent from settings.

    Shaped like :class:`McpServerEntry`, with two differences: an A2A agent yields exactly one
    tool, so its result cap is a single number rather than a per-tool map; and ``featured_skills``
    selects what the tool description mentions rather than gating dispatch.
    """

    model_config = ConfigDict(extra="allow")

    id: str = ""
    name: str = ""
    enabled: bool = True
    url: str = ""
    auth_header: str = ""
    auth_token: str = ""
    featured_skills: list[str] = Field(default_factory=list)
    retrieval_aliases: list[str] = Field(default_factory=list)
    tool_result_limit: int = 0
    poll_interval_sec: float = 2.0

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

    @field_validator("tool_result_limit", mode="before")
    @classmethod
    def _validate_tool_result_limit(cls, value: Any) -> int:
        if value in (None, ""):
            return 0
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError("A2A tool_result_limit must be an integer")
        if value == 0:
            # No entry-level cap; the runtime's own default applies.
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


def _strict_int(value: Any, *, default: int, label: str) -> int:
    if value is None or value == "":
        return default
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} must be an integer") from exc


def _strict_float(value: Any, *, default: float, label: str) -> float:
    if value is None or value == "":
        return default
    try:
        return float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} must be a number") from exc


def _secret_plain(value: Any) -> str:
    getter = getattr(value, "get_secret_value", None)
    if callable(getter):
        return str(getter() or "").strip()
    return str(value or "").strip()


def _coerce_mcp_servers(value: Any) -> list[McpServerEntry]:
    if isinstance(value, list):
        out: list[McpServerEntry] = []
        for item in value:
            if isinstance(item, McpServerEntry):
                out.append(item)
            elif isinstance(item, dict):
                try:
                    out.append(McpServerEntry.model_validate(item))
                except Exception:
                    continue
        return out
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return []
        try:
            parsed = json.loads(text)
        except (TypeError, ValueError):
            return []
        if isinstance(parsed, list):
            return _coerce_mcp_servers(parsed)
    return []


def _coerce_a2a_remote_agents(value: Any) -> list[A2aRemoteAgentEntry]:
    """A list or a JSON string. One malformed entry is dropped rather than failing the load."""
    if isinstance(value, list):
        out: list[A2aRemoteAgentEntry] = []
        for item in value:
            if isinstance(item, A2aRemoteAgentEntry):
                out.append(item)
            elif isinstance(item, dict):
                try:
                    out.append(A2aRemoteAgentEntry.model_validate(item))
                except Exception:
                    continue
        return out
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return []
        try:
            parsed = json.loads(text)
        except (TypeError, ValueError):
            return []
        if isinstance(parsed, list):
            return _coerce_a2a_remote_agents(parsed)
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

    DIALOGS_GRPC_ENDPOINT: str = ""
    DIALOGS_BOT_TOKEN: SecretStr = SecretStr("")
    DIALOGS_GROUP_ID: str = ""
    DIALOGS_USER_ID: str = ""
    DIALOGS_SECURE: bool = True
    DIALOGS_FILE_URL_PORT: int = Field(default=80, ge=1, le=65535)
    DIALOGS_ROOT_CERTIFICATES: str = ""

    ASSISTANT_ASSIGNMENT_SMTP_HOST: str = ""
    ASSISTANT_ASSIGNMENT_SMTP_PORT: int = 25
    ASSISTANT_ASSIGNMENT_SMTP_USERNAME: str = ""
    ASSISTANT_ASSIGNMENT_SMTP_PASSWORD: SecretStr = SecretStr("")
    ASSISTANT_ASSIGNMENT_MAIL_FROM: str = ""
    ASSISTANT_ASSIGNMENT_MAIL_TO: str = ""
    ASSISTANT_ASSIGNMENT_SMTP_STARTTLS_REQUIRED: bool = True
    ASSISTANT_ASSIGNMENT_SMTP_ROOT_CERTIFICATES: str = ""
    ASSISTANT_ASSIGNMENT_SMTP_CLIENT_CERTIFICATE: str = ""
    ASSISTANT_ASSIGNMENT_SMTP_TLS_VERSION: str = ""
    ASSISTANT_ASSIGNMENT_SMTP_VERIFY_TLS: bool = True
    ASSISTANT_ASSIGNMENT_SMTP_TIMEOUT_SEC: float = 30.0
    ASSISTANT_ASSIGNMENT_DELIVERY_TIMEOUT_SEC: float = 30.0
    ASSISTANT_ASSIGNMENT_MAX_ATTEMPTS: int = 20
    ASSISTANT_ASSIGNMENT_RETRY_MIN_SECONDS: float = 1.0
    ASSISTANT_ASSIGNMENT_RETRY_MAX_SECONDS: float = 120.0
    ASSISTANT_DIALOGS_BOT_TOKEN: SecretStr = SecretStr("")
    ASSISTANT_DIALOGS_BOT_GROUP_ID: str = ""

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
    OUROBOROS_MODEL: str = "gigachat-b2bank::GigaChat-Pro"
    OUROBOROS_MODEL_LIGHT: str = "gigachat-b2bank::GigaChat"
    OUROBOROS_MODEL_FALLBACK: str = "gigachat-b2bank::GigaChat"
    # Dedicated vision-capable model used by the vlm_query tool for image
    # analysis. Empty = fall back to OUROBOROS_MODEL (assumed multimodal).
    OUROBOROS_MODEL_VISION: str = ""
    OUROBOROS_MODEL_CONTEXT_TOKENS: int = 200_000
    OUROBOROS_CHAT_SUMMARY_MAX_TOKENS: int = 32_768
    OUROBOROS_CONTEXT_COMPACTION_THRESHOLD_TOKENS: int = 25_000
    OUROBOROS_CONTEXT_COMPACTION_THRESHOLD_MESSAGES: int = 100
    OUROBOROS_SOFT_TIMEOUT_SEC: int = 600
    OUROBOROS_HARD_TIMEOUT_SEC: int = 1800
    # PostgreSQL URL for runtime metadata; empty → SQLite under OUROBOROS_DATA_DIR/state/runtime.sqlite3
    OUROBOROS_RUNTIME_DB_URL: str = ""
    # Max numbered failover URLs (OUROBOROS_RUNTIME_DB_URL_1..N); hard-capped at 64 in db_url helpers
    OUROBOROS_RUNTIME_DB_URL_MAX: int = 16
    OUROBOROS_CHAT_ENCRYPTION_ENABLED: bool = False
    OUROBOROS_CHAT_ENCRYPTION_KEY: SecretStr = SecretStr("")
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
    OUROBOROS_TOOL_LOCAL_TMP_ARTIFACTS_ENABLED: bool = False
    OUROBOROS_BROWSER_BASE_URL: str = ""
    OUROBOROS_WORKER_SLOTS: int = 1
    OUROBOROS_POLIGON: str = "DEV"
    MCP_ENABLED: bool = False
    MCP_SERVERS: list[McpServerEntry] = Field(default_factory=list)
    MCP_TOOL_TIMEOUT_SEC: int = 60
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

    OUROBOROS_STORAGE_BACKEND: str = ""
    OUROBOROS_S3_BUCKET: str = ""
    OUROBOROS_S3_REGION: str = ""
    OUROBOROS_S3_ENDPOINT: str = ""
    OUROBOROS_S3_ACCESS_KEY: SecretStr = SecretStr("")
    OUROBOROS_S3_SECRET_KEY: SecretStr = SecretStr("")
    OUROBOROS_STORAGE_TENANT_ID: str = "default"
    OUROBOROS_S3_PREFIX: str = ""
    OUROBOROS_ALLOW_FILE_STORAGE_IN_PRODUCTION: str = ""
    OUROBOROS_USER_FILE_TTL_HOURS: int = 720
    OUROBOROS_JOB_SCRATCH_TTL_HOURS: int = 24

    OUROBOROS_SUPERVISOR_STARTUP_TIMEOUT_SEC: float = 300.0
    OUROBOROS_LOG_TRACEBACK: bool = False
    OUROBOROS_LLM_HTTP_READ_TIMEOUT_SEC: float = 300.0
    OUROBOROS_BACKGROUND_LLM_HTTP_READ_TIMEOUT_SEC: float = 300.0
    OUROBOROS_LLM_HTTP_CONNECT_TIMEOUT_SEC: float = 30.0
    OUROBOROS_LLM_MAX_CONCURRENCY: int = 2
    OUROBOROS_MAX_ROUNDS: int = 200
    OUROBOROS_CHECKPOINT_PROGRESS_ENABLED: bool = False
    OUROBOROS_EMERGENCY_COMPACTION_CHARS: int = 0
    OUROBOROS_SYSTEM_PROMPT_TOKENS: int = 0
    OUROBOROS_ENABLED_TOOLS: str = ""
    # Comma/space separated tool names to hide from the agent and block from execution.
    OUROBOROS_DISABLED_TOOLS: str = ""
    OUROBOROS_MEMORY_DISTILL: bool = True
    # Identity self-edit has no setting on purpose: the switch is hardcoded off
    # in ouroboros/storage/core_memory/memory/domain/models.py, so no deployment
    # can open the agent's identity document for rewriting.
    OUROBOROS_CONTAINER: str = ""

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

    NAVIGATOR_BI_BASE_URL: str = ""
    NAVIGATOR_BI_MODEL_IDS: str = ""
    NAVIGATOR_BI_SBER_PDI: SecretStr = SecretStr("")
    NAVIGATOR_BI_EMAIL: str = ""
    NAVIGATOR_BI_TIMEOUT_SEC: float = 0.0
    NAVIGATOR_BI_TOTAL_TIMEOUT_SEC: float = 0.0

    TOOL_CALCULATOR_MAX_NUMBERS: int = 0
    DRIVE_ROOT: str = ""

    AGENT_SYSTEM_CONFIGURATION_KAFKA_ENABLED: str = ""
    AGENT_SYSTEM_CONFIGURATION_KAFKA_BOOTSTRAP_SERVERS: str = ""
    AGENT_SYSTEM_CONFIGURATION_KAFKA_TOPIC: str = ""
    AGENT_SYSTEM_CONFIGURATION_AGENT_ID: str = ""
    AGENT_SYSTEM_CONFIGURATION_CLUSTER_ID: str = ""
    AGENT_SYSTEM_CONFIGURATION_NAMESPACE: str = ""
    AGENT_SYSTEM_CONFIGURATION_DISTRIBUTIVE: str = ""
    DISTRIBUTIVE: str = ""
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
        "SELFHOSTED_LLM_VERIFY_SSL",
        "GIGASEARCH_VERIFY_SSL",
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
        "A2A_ENABLED",
        "A2A_EGRESS_ENABLED",
        "AEF_TRACING_ENABLED",
        "AEF_TOOL_TRACE_PAYLOAD_ENABLED",
        "OUROBOROS_LOG_TRACEBACK",
        "AEF_REFLEX_ENABLED",
        "DYNATRACE_ENABLED",
        "ENABLE_REFLEX",
        "OUROBOROS_CHAT_ENCRYPTION_ENABLED",
        "OUROBOROS_CONTEXT_RETRIEVAL_ENABLED",
        "OUROBOROS_CONTEXT_CLEANUP_ENABLED",
        "OUROBOROS_TOOL_LOCAL_TMP_ARTIFACTS_ENABLED",
        "OUROBOROS_CHECKPOINT_PROGRESS_ENABLED",
        "ASSISTANT_ASSIGNMENT_SMTP_STARTTLS_REQUIRED",
        "ASSISTANT_ASSIGNMENT_SMTP_VERIFY_TLS",
        mode="before",
    )
    @classmethod
    def _validate_bool_fields(cls, value: Any) -> bool:
        return _coerce_bool(value)

    @field_validator(
        "GIGACHAT_B2BANK_MAX_TOKENS",
        "OUROBOROS_SOFT_TIMEOUT_SEC",
        "OUROBOROS_HARD_TIMEOUT_SEC",
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
        "OUROBOROS_CONTEXT_COMPACTION_THRESHOLD_TOKENS",
        "OUROBOROS_CONTEXT_COMPACTION_THRESHOLD_MESSAGES",
        "OUROBOROS_MAX_ROUNDS",
        "OUROBOROS_LLM_MAX_CONCURRENCY",
        "OUROBOROS_EMERGENCY_COMPACTION_CHARS",
        "OUROBOROS_SYSTEM_PROMPT_TOKENS",
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
        mode="before",
    )
    @classmethod
    def _validate_int_fields(cls, value: Any) -> int:
        return _coerce_int(value)

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
        mode="before",
    )
    @classmethod
    def _validate_float_fields(cls, value: Any) -> float:
        return _coerce_float(value)

    @field_validator("ASSISTANT_ASSIGNMENT_SMTP_PORT", mode="before")
    @classmethod
    def _validate_assignment_smtp_port(cls, value: Any) -> int:
        port = _strict_int(value, default=25, label="SMTP port")
        if not 1 <= port <= 65535:
            raise ValueError("SMTP port must be between 1 and 65535")
        return port

    @field_validator("ASSISTANT_ASSIGNMENT_MAX_ATTEMPTS", mode="before")
    @classmethod
    def _validate_assignment_max_attempts(cls, value: Any) -> int:
        attempts = _strict_int(value, default=20, label="assignment max attempts")
        if attempts < 1:
            raise ValueError("assignment max attempts must be >= 1")
        return attempts

    @field_validator("ASSISTANT_ASSIGNMENT_SMTP_TIMEOUT_SEC", mode="before")
    @classmethod
    def _validate_assignment_smtp_timeout(cls, value: Any) -> float:
        timeout = _strict_float(value, default=30.0, label="SMTP timeout")
        if timeout <= 0:
            raise ValueError("SMTP timeout must be > 0")
        return timeout

    @field_validator("ASSISTANT_ASSIGNMENT_SMTP_TLS_VERSION", mode="before")
    @classmethod
    def _validate_assignment_smtp_tls_version(cls, value: Any) -> str:
        text = str(value or "").strip()
        if not text:
            return ""
        normalized = text.upper().replace(" ", "").replace("-", "_")
        if normalized in _ASSIGNMENT_SMTP_TLS_VERSIONS:
            return _ASSIGNMENT_SMTP_TLS_VERSIONS[normalized]
        raise ValueError("SMTP TLS version must be TLSv1.2 or TLSv1.3")

    @field_validator("ASSISTANT_ASSIGNMENT_DELIVERY_TIMEOUT_SEC", mode="before")
    @classmethod
    def _validate_assignment_delivery_timeout(cls, value: Any) -> float:
        timeout = _strict_float(value, default=30.0, label="assignment delivery timeout")
        if timeout <= 0:
            raise ValueError("assignment delivery timeout must be > 0")
        return timeout

    @field_validator("ASSISTANT_ASSIGNMENT_RETRY_MIN_SECONDS", mode="before")
    @classmethod
    def _validate_assignment_retry_min_delay(cls, value: Any) -> float:
        delay = _strict_float(value, default=1.0, label="assignment retry delay")
        if delay < 0:
            raise ValueError("assignment retry delay must be >= 0")
        return delay

    @field_validator("ASSISTANT_ASSIGNMENT_RETRY_MAX_SECONDS", mode="before")
    @classmethod
    def _validate_assignment_retry_max_delay(cls, value: Any) -> float:
        delay = _strict_float(value, default=120.0, label="assignment retry delay")
        if delay < 0:
            raise ValueError("assignment retry delay must be >= 0")
        return delay

    @field_validator("OUROBOROS_LOG_LEVEL", mode="before")
    @classmethod
    def _normalize_log_level(cls, value: Any) -> str:
        text = str(value or "").strip().upper()
        return text or "INFO"

    @field_validator("MCP_SERVERS", mode="before")
    @classmethod
    def _validate_mcp_servers(cls, value: Any) -> list[McpServerEntry]:
        return _coerce_mcp_servers(value)

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

    @field_validator(
        "GIGACHAT_B2BANK_CERT_PATH",
        "GIGACHAT_B2BANK_KEY_PATH",
        "GIGACHAT_B2BANK_CA_PATH",
        "CLOUDRU_FOUNDATION_MODELS_API_KEY",
        "SELFHOSTED_LLM_API_KEY",
        "GIGASEARCH_API_KEY",
        "DIALOGS_BOT_TOKEN",
        "ASSISTANT_ASSIGNMENT_SMTP_PASSWORD",
        "ASSISTANT_DIALOGS_BOT_TOKEN",
        "OUROBOROS_S3_ACCESS_KEY",
        "OUROBOROS_S3_SECRET_KEY",
        "OPENAI_API_KEY",
        "ANTHROPIC_API_KEY",
        "OPENROUTER_API_KEY",
        "OPENAI_COMPATIBLE_API_KEY",
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
    def _validate_assignment_delivery_settings(self) -> AppSettings:
        if self.ASSISTANT_ASSIGNMENT_RETRY_MAX_SECONDS < self.ASSISTANT_ASSIGNMENT_RETRY_MIN_SECONDS:
            raise ValueError("assignment retry max must be >= retry min")

        smtp_selected = any(
            [
                self.ASSISTANT_ASSIGNMENT_SMTP_HOST.strip(),
                self.ASSISTANT_ASSIGNMENT_SMTP_USERNAME.strip(),
                _secret_plain(self.ASSISTANT_ASSIGNMENT_SMTP_PASSWORD),
                self.ASSISTANT_ASSIGNMENT_MAIL_FROM.strip(),
                self.ASSISTANT_ASSIGNMENT_MAIL_TO.strip(),
                self.ASSISTANT_ASSIGNMENT_SMTP_PORT != 25,
                self.ASSISTANT_ASSIGNMENT_SMTP_TIMEOUT_SEC != 30.0,
                self.ASSISTANT_ASSIGNMENT_MAX_ATTEMPTS != 20,
                self.ASSISTANT_ASSIGNMENT_RETRY_MIN_SECONDS != 1.0,
                self.ASSISTANT_ASSIGNMENT_RETRY_MAX_SECONDS != 120.0,
                self.ASSISTANT_ASSIGNMENT_SMTP_STARTTLS_REQUIRED is not True,
                self.ASSISTANT_ASSIGNMENT_SMTP_ROOT_CERTIFICATES.strip(),
                self.ASSISTANT_ASSIGNMENT_SMTP_CLIENT_CERTIFICATE.strip(),
                self.ASSISTANT_ASSIGNMENT_SMTP_TLS_VERSION.strip(),
                self.ASSISTANT_ASSIGNMENT_SMTP_VERIFY_TLS is not True,
            ]
        )
        dialogs_selected = bool(
            _secret_plain(self.ASSISTANT_DIALOGS_BOT_TOKEN) or self.ASSISTANT_DIALOGS_BOT_GROUP_ID.strip()
        )
        if not smtp_selected and not dialogs_selected:
            return self

        missing: list[str] = []
        if smtp_selected:
            if not self.ASSISTANT_ASSIGNMENT_SMTP_HOST.strip():
                missing.append("ASSISTANT_ASSIGNMENT_SMTP_HOST")
            if not self.ASSISTANT_ASSIGNMENT_SMTP_USERNAME.strip():
                missing.append("ASSISTANT_ASSIGNMENT_SMTP_USERNAME")
            if not _secret_plain(self.ASSISTANT_ASSIGNMENT_SMTP_PASSWORD):
                missing.append("ASSISTANT_ASSIGNMENT_SMTP_PASSWORD")
            if not self.ASSISTANT_ASSIGNMENT_MAIL_FROM.strip():
                missing.append("ASSISTANT_ASSIGNMENT_MAIL_FROM")
            if not any(
                recipient.strip() for recipient in self.ASSISTANT_ASSIGNMENT_MAIL_TO.replace(";", ",").split(",")
            ):
                missing.append("ASSISTANT_ASSIGNMENT_MAIL_TO")
        if dialogs_selected:
            if not self.DIALOGS_GRPC_ENDPOINT.strip():
                missing.append("DIALOGS_GRPC_ENDPOINT")
            if not _secret_plain(self.ASSISTANT_DIALOGS_BOT_TOKEN):
                missing.append("ASSISTANT_DIALOGS_BOT_TOKEN")
            if not self.ASSISTANT_DIALOGS_BOT_GROUP_ID.strip():
                missing.append("ASSISTANT_DIALOGS_BOT_GROUP_ID")
        if missing:
            raise ValueError(f"Incomplete assignment delivery settings: missing {', '.join(missing)}")
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
            "DIALOGS_BOT_TOKEN",
            "ASSISTANT_ASSIGNMENT_SMTP_PASSWORD",
            "ASSISTANT_DIALOGS_BOT_TOKEN",
            "OUROBOROS_S3_ACCESS_KEY",
            "OUROBOROS_S3_SECRET_KEY",
            "OPENAI_API_KEY",
            "ANTHROPIC_API_KEY",
            "OPENROUTER_API_KEY",
            "OPENAI_COMPATIBLE_API_KEY",
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
