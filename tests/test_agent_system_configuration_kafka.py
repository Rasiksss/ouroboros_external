from __future__ import annotations

import types


def test_build_agent_system_configuration_event_uses_distributive_fallback():
    import ouroboros.app.agent_system_configuration_kafka as mod

    env = {
        "AGENT_SYSTEM_CONFIGURATION_AGENT_ID": "CI00001234",
        "AGENT_SYSTEM_CONFIGURATION_CLUSTER_ID": "dev1.apps.azwx5oj1.k8s.delta.sbrf.ru",
        "AGENT_SYSTEM_CONFIGURATION_NAMESPACE": "dev1",
        "AGENT_SYSTEM_CONFIGURATION_DISTRIBUTIVE": " ",
        "DISTRIBUTIVE": "https://example.invalid/distrib.zip",
        "AGENT_SYSTEM_CONFIGURATION_REPO_CODE": "aiagents",
        "AGENT_SYSTEM_CONFIGURATION_APP_CI": "CI04549012",
        "AGENT_SYSTEM_CONFIGURATION_MMS_AGENT_VERSION": "456787",
        "AGENT_SYSTEM_CONFIGURATION_VERSION_NO": "1.2.1",
        "AGENT_SYSTEM_CONFIGURATION_FILENAME": "prompts.txt",
    }

    assert mod.build_agent_system_configuration_event(
        env=env,
        timestamp="2026-04-14T09:15:30Z",
    ) == {
        "contract-version": "1.0",
        "timestamp": "2026-04-14T09:15:30Z",
        "event-type": "AGENT_SYSTEM_CONFIGURATION",
        "metadata": {
            "agent-id": "CI00001234",
            "cluster-id": "dev1.apps.azwx5oj1.k8s.delta.sbrf.ru",
            "namespace": "dev1",
            "distributive": "https://example.invalid/distrib.zip",
        },
        "payload": [
            {
                "config-type": "system_prompt",
                "attributes": {
                    "repoCode": "aiagents",
                    "appCI": "CI04549012",
                    "mmsAgentVersion": "456787",
                    "versionNo": "1.2.1",
                    "filename": "prompts.txt",
                },
            }
        ],
    }


def test_publish_agent_system_configuration_event_uses_configured_values(monkeypatch):
    import ouroboros.app.agent_system_configuration_kafka as mod

    captured = {}

    def _fake_send_json_to_kafka(*, bootstrap_servers, topic, payload):
        captured["bootstrap_servers"] = bootstrap_servers
        captured["topic"] = topic
        captured["payload"] = payload

    monkeypatch.setattr(mod, "_send_json_to_kafka", _fake_send_json_to_kafka)
    env = {
        "AGENT_SYSTEM_CONFIGURATION_KAFKA_ENABLED": "true",
        "AGENT_SYSTEM_CONFIGURATION_KAFKA_BOOTSTRAP_SERVERS": "tvldq-gwaef0005.delta.sigma.com:9093, tvldq-gwaef0006.delta.sigma.com:9093",
        "AGENT_SYSTEM_CONFIGURATION_KAFKA_TOPIC": "agent.system.configuration",
        "AGENT_SYSTEM_CONFIGURATION_AGENT_ID": "CI00001234",
        "AGENT_SYSTEM_CONFIGURATION_CLUSTER_ID": "dev1.apps.azwx5oj1.k8s.delta.sbrf.ru",
        "AGENT_SYSTEM_CONFIGURATION_NAMESPACE": "dev1",
        "AGENT_SYSTEM_CONFIGURATION_DISTRIBUTIVE": "https://example.invalid/distrib.zip",
        "DISTRIBUTIVE": "https://example.invalid/fallback.zip",
        "AGENT_SYSTEM_CONFIGURATION_REPO_CODE": "aiagents",
        "AGENT_SYSTEM_CONFIGURATION_APP_CI": "CI04549012",
        "AGENT_SYSTEM_CONFIGURATION_MMS_AGENT_VERSION": "456787",
        "AGENT_SYSTEM_CONFIGURATION_VERSION_NO": "1.2.1",
        "AGENT_SYSTEM_CONFIGURATION_FILENAME": "prompts.txt",
    }

    assert (
        mod.publish_agent_system_configuration_event(
            env=env,
            timestamp="2026-04-14T09:15:30Z",
        )
        is True
    )
    assert captured["bootstrap_servers"] == [
        "tvldq-gwaef0005.delta.sigma.com:9093",
        "tvldq-gwaef0006.delta.sigma.com:9093",
    ]
    assert captured["topic"] == "agent.system.configuration"
    assert captured["payload"]["event-type"] == "AGENT_SYSTEM_CONFIGURATION"
    assert captured["payload"]["metadata"]["distributive"] == "https://example.invalid/distrib.zip"


def test_publish_agent_system_configuration_event_logs_invalid_config():
    import ouroboros.app.agent_system_configuration_kafka as mod

    warnings = []
    logger = types.SimpleNamespace(warning=lambda msg, *args, **kwargs: warnings.append(msg % args if args else msg))

    assert (
        mod.publish_agent_system_configuration_event(
            env={
                "AGENT_SYSTEM_CONFIGURATION_KAFKA_ENABLED": "1",
                "AGENT_SYSTEM_CONFIGURATION_KAFKA_BOOTSTRAP_SERVERS": "tvldq-gwaef0005.delta.sigma.com:9093",
                "AGENT_SYSTEM_CONFIGURATION_KAFKA_TOPIC": "agent.system.configuration",
            },
            logger=logger,
            timestamp="2026-04-14T09:15:30Z",
        )
        is False
    )
    assert any("AGENT_SYSTEM_CONFIGURATION" in item for item in warnings)
