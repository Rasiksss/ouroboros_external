from __future__ import annotations

import datetime as dt
import json
import logging
import re
from collections.abc import Mapping

log = logging.getLogger(__name__)

_ENABLED_ENV = "AGENT_SYSTEM_CONFIGURATION_KAFKA_ENABLED"
_BOOTSTRAP_SERVERS_ENV = "AGENT_SYSTEM_CONFIGURATION_KAFKA_BOOTSTRAP_SERVERS"
_TOPIC_ENV = "AGENT_SYSTEM_CONFIGURATION_KAFKA_TOPIC"
_FILENAME_RE = re.compile(r"^[.a-zA-Z0-9-_/]{3,300}$")


def _truthy(value: str) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _env_text(env: Mapping[str, str], key: str) -> str:
    return str(env.get(key, "") or "").strip()


def _required_env(
    env: Mapping[str, str],
    key: str,
    *,
    max_length: int,
    pattern: re.Pattern[str] | None = None,
    fallback_key: str | None = None,
) -> str:
    value = _env_text(env, key) or (_env_text(env, fallback_key) if fallback_key else "")
    if not value:
        raise ValueError(f"{key} is required")
    if len(value) > max_length:
        raise ValueError(f"{key} exceeds max length {max_length}")
    if pattern is not None and not pattern.fullmatch(value):
        raise ValueError(f"{key} has invalid format")
    return value


def _default_timestamp() -> str:
    return dt.datetime.now(tz=dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def parse_kafka_bootstrap_servers(value: str) -> list[str]:
    return [item.strip() for item in str(value or "").split(",") if item.strip()]


def _config_source(env: Mapping[str, str] | None) -> Mapping[str, str]:
    if env is not None:
        return env
    from ouroboros.storage.settings.store import load_settings

    return load_settings().to_persist_dict()


def build_agent_system_configuration_metadata(
    *,
    env: Mapping[str, str] | None = None,
) -> dict[str, str]:
    source = _config_source(env)
    return {
        "agent-id": _required_env(source, "AGENT_SYSTEM_CONFIGURATION_AGENT_ID", max_length=10),
        "cluster-id": _required_env(source, "AGENT_SYSTEM_CONFIGURATION_CLUSTER_ID", max_length=255),
        "namespace": _required_env(source, "AGENT_SYSTEM_CONFIGURATION_NAMESPACE", max_length=255),
        "distributive": _required_env(
            source,
            "AGENT_SYSTEM_CONFIGURATION_DISTRIBUTIVE",
            max_length=500,
            fallback_key="DISTRIBUTIVE",
        ),
    }


def build_agent_system_configuration_event(
    *,
    env: Mapping[str, str] | None = None,
    timestamp: str | None = None,
) -> dict:
    source = _config_source(env)
    return {
        "contract-version": "1.0",
        "timestamp": str(timestamp or _default_timestamp()),
        "event-type": "AGENT_SYSTEM_CONFIGURATION",
        "metadata": build_agent_system_configuration_metadata(env=source),
        "payload": [
            {
                "config-type": "system_prompt",
                "attributes": {
                    "repoCode": _required_env(source, "AGENT_SYSTEM_CONFIGURATION_REPO_CODE", max_length=255),
                    "appCI": _required_env(source, "AGENT_SYSTEM_CONFIGURATION_APP_CI", max_length=10),
                    "mmsAgentVersion": _required_env(
                        source,
                        "AGENT_SYSTEM_CONFIGURATION_MMS_AGENT_VERSION",
                        max_length=255,
                    ),
                    "versionNo": _required_env(source, "AGENT_SYSTEM_CONFIGURATION_VERSION_NO", max_length=255),
                    "filename": _required_env(
                        source,
                        "AGENT_SYSTEM_CONFIGURATION_FILENAME",
                        max_length=255,
                        pattern=_FILENAME_RE,
                    ),
                },
            }
        ],
    }


def _send_json_to_kafka(*, bootstrap_servers: list[str], topic: str, payload: dict) -> None:
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
) -> bool:
    source = _config_source(env)
    if not _truthy(_env_text(source, _ENABLED_ENV)):
        return False

    try:
        bootstrap_servers = parse_kafka_bootstrap_servers(_env_text(source, _BOOTSTRAP_SERVERS_ENV))
        if not bootstrap_servers:
            raise ValueError(f"{_BOOTSTRAP_SERVERS_ENV} is required")
        topic = _required_env(source, _TOPIC_ENV, max_length=255)
        payload = build_agent_system_configuration_event(env=source, timestamp=timestamp)
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
