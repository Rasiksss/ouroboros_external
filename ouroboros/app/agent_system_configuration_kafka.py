from __future__ import annotations

import datetime as dt
import json
import logging
import re
from collections.abc import Mapping
from typing import Literal, TypedDict, TypeVar

from infra.kafka_client import parse_kafka_bootstrap_servers
from new_core.skills.surface import SkillSurface

ToolConfiguration = TypedDict(
    "ToolConfiguration",
    {"tool-name": str, "active-tool": bool, "tool-short-description": str},
)
SkillConfiguration = TypedDict("SkillConfiguration", {"skill-name": str, "skill-short-description": str})
ChannelConfiguration = TypedDict("ChannelConfiguration", {"channel-name": str, "channel-active": bool})
GigaAgentStartAttributes = TypedDict(
    "GigaAgentStartAttributes",
    {
        "agent-version": str,
        "aef-workstation": bool,
        "list-tools": list[ToolConfiguration],
        "list-skills": list[SkillConfiguration],
        "list-channels": list[ChannelConfiguration],
    },
)


class SystemPromptAttributes(TypedDict):
    repoCode: str
    appCI: str
    mmsAgentVersion: str
    versionNo: str
    filename: str


SystemPromptConfiguration = TypedDict(
    "SystemPromptConfiguration",
    {"config-type": Literal["system_prompt"], "attributes": SystemPromptAttributes},
)
GigaAgentStartConfiguration = TypedDict(
    "GigaAgentStartConfiguration",
    {"config-type": Literal["giga-agent-start"], "attributes": GigaAgentStartAttributes},
)
AgentSystemConfigurationEvent = TypedDict(
    "AgentSystemConfigurationEvent",
    {
        "contract-version": str,
        "timestamp": str,
        "event-type": Literal["AGENT_SYSTEM_CONFIGURATION"],
        "metadata": dict[str, str],
        "payload": list[SystemPromptConfiguration | GigaAgentStartConfiguration],
    },
)
_InventoryItem = TypeVar("_InventoryItem", bound=Mapping[str, object])

log = logging.getLogger(__name__)

_ENABLED_ENV = "AGENT_SYSTEM_CONFIGURATION_KAFKA_ENABLED"
_BOOTSTRAP_SERVERS_ENV = "AGENT_SYSTEM_CONFIGURATION_KAFKA_BOOTSTRAP_SERVERS"
_TOPIC_ENV = "AGENT_SYSTEM_CONFIGURATION_KAFKA_TOPIC"
_FILENAME_RE = re.compile(r"^[.a-zA-Z0-9-_/]{3,300}$")
_DISTRIBUTIVE_RE = re.compile(r"^[A-Za-z0-9:/._@+-]{1,255}$")
_CLUSTER_ID_RE = re.compile(r"^[A-Za-z0-9.-]+$")
_NAMESPACE_RE = re.compile(r"^[A-Za-z0-9-]+$")
_REPO_CODE_RE = re.compile(r"^[A-Za-z0-9_-]{1,100}$")
_APP_CI_RE = re.compile(r"^[Cc][Ii][0-9]{8}$")
_MMS_AGENT_VERSION_RE = re.compile(r"^[0-9]{3,64}$")
_PROMPT_VERSION_RE = re.compile(r"^(D-|SP-|P-|d-|sp-|p-)?\d{1,4}\.\d{1,4}\.\d{1,4}([-.][_0-9a-zA-Z.-]{1,64})?$")
_AGENT_VERSION_RE = re.compile(r"^\d{1,3}(?:\.\d{1,3}){0,2}$")
_CONTRACT_TEXT_RE = re.compile(r"[^\w\u0400-\u04FF\s-]+", re.UNICODE)
_CONTRACT_DESCRIPTION_RE = re.compile(r"[^\w\u0400-\u04FF\s.,!?;:'\"()\-/]+", re.UNICODE)
_MAX_LIST_ITEMS = 250
_MAX_CONTRACT_TEXT = 255


def _truthy(value: str) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _env_text(env: Mapping[str, str], key: str) -> str:
    return str(env.get(key, "") or "").strip()


def _validated_env(
    env: Mapping[str, str],
    key: str,
    *,
    max_length: int,
    pattern: re.Pattern[str] | None = None,
    fallback_key: str | None = None,
    required: bool = True,
) -> str:
    value = _env_text(env, key) or (_env_text(env, fallback_key) if fallback_key else "")
    reason = ""
    if not value:
        reason = "is required"
    elif len(value) > max_length:
        reason = f"exceeds max length {max_length}"
    elif pattern is not None and not pattern.fullmatch(value):
        reason = "has invalid format"
    if reason:
        keys = f"{key} / {fallback_key}" if fallback_key else key
        if required:
            raise ValueError(f"{keys} {reason}")
        log.warning("AGENT_SYSTEM_CONFIGURATION %s %s; sending empty value", keys, reason)
        return ""
    return value


def _default_timestamp() -> str:
    return dt.datetime.now(tz=dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _config_source(env: Mapping[str, str] | None) -> Mapping[str, str]:
    if env is not None:
        return env
    from ouroboros.storage.settings.store import load_settings

    return load_settings().to_persist_dict()


def _normalize_contract_text(value: object, *, description: bool = False) -> str:
    text = str(value or "").strip()
    pattern = _CONTRACT_DESCRIPTION_RE if description else _CONTRACT_TEXT_RE
    text = pattern.sub("", text)
    return text[:_MAX_CONTRACT_TEXT]


def _bounded_unique(items: list[_InventoryItem], *, key: str, label: str) -> list[_InventoryItem]:
    result: list[_InventoryItem] = []
    seen: set[str] = set()
    # Discovery/connect order is not configuration. Sort before selecting the bounded
    # inventory; the tie-breaker also makes normalized-name collisions deterministic.
    ordered = sorted(
        items,
        key=lambda item: (str(item.get(key) or ""), json.dumps(item, sort_keys=True, ensure_ascii=False)),
    )
    for item in ordered:
        name = str(item.get(key) or "")
        if not name or name in seen:
            continue
        seen.add(name)
        result.append(item)
        if len(result) >= _MAX_LIST_ITEMS:
            if len(items) > _MAX_LIST_ITEMS:
                log.warning("AGENT_SYSTEM_CONFIGURATION %s truncated to %d items", label, _MAX_LIST_ITEMS)
            break
    return result


def _collect_tools() -> list[ToolConfiguration]:
    """Return the process-wide tool catalog snapshot for the startup event."""
    try:
        from new_core.identity import ServiceJob
        from new_core.tools.catalog import tool_catalog
        from ouroboros.contracts.owner_scope import service_identity
        from ouroboros.mcp.manager import mcp_manager
        from ouroboros.tools.catalog import disabled_tool_names, excluded_tool_names, spec_for_tool

        disabled = disabled_tool_names()
        excluded = excluded_tool_names()
        tools = {
            descriptor.name: (descriptor.surface, descriptor.schema.description)
            for descriptor in tool_catalog.descriptors(service_identity(job=ServiceJob.STARTUP).as_scope())
        }
        if mcp_manager.is_available():
            for tool in mcp_manager.discovered_tools():
                tools.setdefault(tool.schema.name, (spec_for_tool(tool.schema.name).surface, tool.schema.description))
        items: list[ToolConfiguration] = []
        for tool_name, (surface, description) in tools.items():
            if tool_name in excluded or (surface == "excluded" and tool_name not in disabled):
                continue
            name = _normalize_contract_text(tool_name)
            if not name:
                continue
            description = _normalize_contract_text(description, description=True)
            items.append(
                {
                    "tool-name": name,
                    "active-tool": surface != "excluded" and tool_name not in disabled,
                    "tool-short-description": description,
                }
            )
        return _bounded_unique(items, key="tool-name", label="tools")
    except Exception:
        log.warning("AGENT_SYSTEM_CONFIGURATION tools collection failed", exc_info=True)
        return []


def _collect_skills(skill_surface: SkillSurface | None) -> list[SkillConfiguration]:
    """Return enabled, eligible skills from the configured distribution."""
    if skill_surface is None:
        return []
    try:
        items: list[SkillConfiguration] = []
        for skill in skill_surface.deployment_skills():
            if skill.load_error or not skill.selected or not skill.eligible:
                continue
            name = _normalize_contract_text(skill.name)
            if not name:
                continue
            items.append(
                {
                    "skill-name": name,
                    "skill-short-description": _normalize_contract_text(
                        skill.description,
                        description=True,
                    ),
                }
            )
        return _bounded_unique(items, key="skill-name", label="skills")
    except Exception:
        log.warning("AGENT_SYSTEM_CONFIGURATION skills collection failed", exc_info=True)
        return []


def _collect_channels(source: Mapping[str, str]) -> list[ChannelConfiguration]:
    """Build channel configuration status without probing external services.

    The headless runtime has no generic channel registry. These are the configured
    ingress surfaces currently owned by this process; a channel is reported only
    when its configuration is present, and ``channel-active`` reflects configuration
    rather than network reachability.
    """
    items: list[ChannelConfiguration] = []
    dialogs_endpoint = _env_text(source, "DIALOGS_GRPC_ENDPOINT")
    dialogs_token = _env_text(source, "DIALOGS_BOT_TOKEN")
    if dialogs_endpoint or dialogs_token or _env_text(source, "DIALOGS_BOT_NICK"):
        items.append(
            {
                "channel-name": "sber-chat",
                "channel-active": bool(dialogs_endpoint and dialogs_token),
            }
        )
    if "BROWSER_INGRESS_ENABLED" in source:
        items.append(
            {
                "channel-name": "browser",
                "channel-active": _truthy(_env_text(source, "BROWSER_INGRESS_ENABLED")),
            }
        )
    if "A2A_ENABLED" in source:
        items.append(
            {
                "channel-name": "a2a",
                "channel-active": _truthy(_env_text(source, "A2A_ENABLED")),
            }
        )
    return _bounded_unique(items, key="channel-name", label="channels")


def _aef_workstation_enabled(source: Mapping[str, str]) -> bool:
    """Whether the AEF WorkStation HTTP integration is configured."""
    return bool(_env_text(source, "WORKSTATION_BASE_URL"))


def _build_giga_agent_start_attributes(
    source: Mapping[str, str], skill_surface: SkillSurface | None
) -> GigaAgentStartAttributes:
    version = _validated_env(
        source,
        "AGENT_SYSTEM_CONFIGURATION_AGENT_VERSION",
        fallback_key="AGENT_VERSION",
        max_length=10,
        pattern=_AGENT_VERSION_RE,
        required=False,
    )
    return {
        "agent-version": version,
        "aef-workstation": _aef_workstation_enabled(source),
        "list-tools": _collect_tools(),
        "list-skills": _collect_skills(skill_surface),
        "list-channels": _collect_channels(source),
    }


def build_agent_system_configuration_metadata(
    *,
    env: Mapping[str, str] | None = None,
    allow_empty: bool = False,
) -> dict[str, str]:
    source = _config_source(env)
    return {
        "agent-id": _validated_env(
            source,
            "AGENT_SYSTEM_CONFIGURATION_AGENT_ID",
            max_length=10,
            fallback_key="AGENT_ID",
            required=not allow_empty,
        ),
        "cluster-id": _validated_env(
            source,
            "AGENT_SYSTEM_CONFIGURATION_CLUSTER_ID",
            max_length=100,
            pattern=_CLUSTER_ID_RE,
            required=not allow_empty,
        ),
        "namespace": _validated_env(
            source,
            "AGENT_SYSTEM_CONFIGURATION_NAMESPACE",
            max_length=100,
            pattern=_NAMESPACE_RE,
            required=not allow_empty,
        ),
        "distributive": _validated_env(
            source,
            "AGENT_SYSTEM_CONFIGURATION_DISTRIBUTIVE",
            max_length=255,
            pattern=_DISTRIBUTIVE_RE,
            fallback_key="DISTRIBUTIVE",
            required=not allow_empty,
        ),
    }


def build_agent_system_configuration_event(
    *,
    env: Mapping[str, str] | None = None,
    timestamp: str | None = None,
    skill_surface: SkillSurface | None = None,
) -> AgentSystemConfigurationEvent:
    source = _config_source(env)
    return {
        "contract-version": "1.0",
        "timestamp": str(timestamp or _default_timestamp()),
        "event-type": "AGENT_SYSTEM_CONFIGURATION",
        "metadata": build_agent_system_configuration_metadata(env=source, allow_empty=True),
        "payload": [
            {
                "config-type": "system_prompt",
                "attributes": {
                    "repoCode": _validated_env(
                        source,
                        "AGENT_SYSTEM_CONFIGURATION_REPO_CODE",
                        max_length=100,
                        pattern=_REPO_CODE_RE,
                        required=False,
                    ),
                    "appCI": _validated_env(
                        source, "AGENT_SYSTEM_CONFIGURATION_APP_CI", max_length=10, pattern=_APP_CI_RE, required=False
                    ),
                    "mmsAgentVersion": _validated_env(
                        source,
                        "AGENT_SYSTEM_CONFIGURATION_MMS_AGENT_VERSION",
                        max_length=64,
                        pattern=_MMS_AGENT_VERSION_RE,
                        required=False,
                    ),
                    "versionNo": _validated_env(
                        source,
                        "AGENT_SYSTEM_CONFIGURATION_VERSION_NO",
                        max_length=82,
                        pattern=_PROMPT_VERSION_RE,
                        required=False,
                    ),
                    "filename": _validated_env(
                        source,
                        "AGENT_SYSTEM_CONFIGURATION_FILENAME",
                        max_length=300,
                        pattern=_FILENAME_RE,
                        required=False,
                    ),
                },
            },
            {
                "config-type": "giga-agent-start",
                "attributes": _build_giga_agent_start_attributes(source, skill_surface),
            },
        ],
    }


def _send_json_to_kafka(*, bootstrap_servers: list[str], topic: str, payload: AgentSystemConfigurationEvent) -> None:
    from kafka import KafkaProducer

    producer = KafkaProducer(
        bootstrap_servers=bootstrap_servers,
        value_serializer=lambda value: json.dumps(value, ensure_ascii=False).encode("utf-8"),
    )
    try:
        producer.send(topic, payload).get(timeout=10)
        producer.flush(timeout=10)
    finally:
        producer.close(timeout=10)


def publish_agent_system_configuration_event(
    *,
    logger=None,
    env: Mapping[str, str] | None = None,
    timestamp: str | None = None,
    skill_surface: SkillSurface | None = None,
) -> bool:
    source = _config_source(env)
    if not _truthy(_env_text(source, _ENABLED_ENV)):
        return False

    try:
        bootstrap_servers = parse_kafka_bootstrap_servers(_env_text(source, _BOOTSTRAP_SERVERS_ENV))
        if not bootstrap_servers:
            raise ValueError(f"{_BOOTSTRAP_SERVERS_ENV} is required")
        topic = _validated_env(source, _TOPIC_ENV, max_length=255)
        payload = build_agent_system_configuration_event(
            env=source,
            timestamp=timestamp,
            skill_surface=skill_surface,
        )
        _send_json_to_kafka(
            bootstrap_servers=bootstrap_servers,
            topic=topic,
            payload=payload,
        )
        return True
    except Exception as exc:
        (logger or log).warning(
            "AGENT_SYSTEM_CONFIGURATION Kafka publish failed: %s",
            exc,
            exc_info=True,
        )
        return False
