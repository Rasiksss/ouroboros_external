from __future__ import annotations

import sys
import types
from unittest.mock import Mock

import pytest


def test_build_agent_system_configuration_event_uses_distributive_fallback(monkeypatch):
    import ouroboros.app.agent_system_configuration_kafka as mod

    monkeypatch.setattr(mod, "_collect_tools", list)
    monkeypatch.setattr(mod, "_collect_skills", lambda _surface: [])

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
        "AGENT_SYSTEM_CONFIGURATION_AGENT_VERSION": "1.2.1",
        "WORKSTATION_BASE_URL": "https://workstation.example",
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
            },
            {
                "config-type": "giga-agent-start",
                "attributes": {
                    "agent-version": "1.2.1",
                    "aef-workstation": True,
                    "list-tools": [],
                    "list-skills": [],
                    "list-channels": [],
                },
            },
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
    monkeypatch.setattr(mod, "_collect_tools", list)
    monkeypatch.setattr(mod, "_collect_skills", lambda _surface: [])
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
        "AGENT_SYSTEM_CONFIGURATION_AGENT_VERSION": "1.2.1",
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
    assert captured["payload"]["payload"][1]["config-type"] == "giga-agent-start"


def test_publication_reuses_loaded_settings_for_event_fields(monkeypatch):
    import ouroboros.app.agent_system_configuration_kafka as mod

    source = _announcement_env()
    settings = types.SimpleNamespace(to_persist_dict=Mock(return_value=source))
    load_settings = Mock(return_value=settings)
    monkeypatch.setattr("ouroboros.storage.settings.store.load_settings", load_settings)
    monkeypatch.setattr(mod, "_collect_tools", list)
    monkeypatch.setattr(mod, "_collect_skills", lambda _surface: [])
    sent = []
    monkeypatch.setattr(mod, "_send_json_to_kafka", lambda **kwargs: sent.append(kwargs))

    assert mod.publish_agent_system_configuration_event()

    load_settings.assert_called_once_with()
    settings.to_persist_dict.assert_called_once_with()
    assert sent[0]["topic"] == source["AGENT_SYSTEM_CONFIGURATION_KAFKA_TOPIC"]
    assert sent[0]["payload"]["metadata"]["namespace"] == source["AGENT_SYSTEM_CONFIGURATION_NAMESPACE"]


def test_publish_agent_system_configuration_event_logs_missing_kafka_config():
    import ouroboros.app.agent_system_configuration_kafka as mod

    warnings = []
    logger = types.SimpleNamespace(warning=lambda msg, *args, **kwargs: warnings.append(msg % args if args else msg))

    assert (
        mod.publish_agent_system_configuration_event(
            env={
                "AGENT_SYSTEM_CONFIGURATION_KAFKA_ENABLED": "1",
                "AGENT_SYSTEM_CONFIGURATION_KAFKA_TOPIC": "agent.system.configuration",
            },
            logger=logger,
            timestamp="2026-04-14T09:15:30Z",
        )
        is False
    )
    assert any("AGENT_SYSTEM_CONFIGURATION" in item for item in warnings)


def _announcement_env() -> dict[str, str]:
    return {
        "AGENT_SYSTEM_CONFIGURATION_KAFKA_ENABLED": "true",
        "AGENT_SYSTEM_CONFIGURATION_KAFKA_BOOTSTRAP_SERVERS": "kafka-1:9093",
        "AGENT_SYSTEM_CONFIGURATION_KAFKA_TOPIC": "agent.system.configuration",
        "AGENT_SYSTEM_CONFIGURATION_AGENT_ID": "CI00001234",
        "AGENT_SYSTEM_CONFIGURATION_CLUSTER_ID": "dev1.apps.example",
        "AGENT_SYSTEM_CONFIGURATION_NAMESPACE": "dev1",
        "AGENT_SYSTEM_CONFIGURATION_DISTRIBUTIVE": "https://example.invalid/distrib.zip",
        "AGENT_SYSTEM_CONFIGURATION_REPO_CODE": "aiagents",
        "AGENT_SYSTEM_CONFIGURATION_APP_CI": "CI04549012",
        "AGENT_SYSTEM_CONFIGURATION_MMS_AGENT_VERSION": "456787",
        "AGENT_SYSTEM_CONFIGURATION_VERSION_NO": "1.2.1",
        "AGENT_SYSTEM_CONFIGURATION_FILENAME": "prompts.txt",
    }


@pytest.mark.parametrize("missing", ["AGENT_SYSTEM_CONFIGURATION_NAMESPACE", "AGENT_SYSTEM_CONFIGURATION_REPO_CODE"])
def test_missing_event_attributes_are_empty_without_suppressing_publication(monkeypatch, caplog, missing):
    import ouroboros.app.agent_system_configuration_kafka as mod

    env = _announcement_env()
    env.pop(missing)
    sent = []
    monkeypatch.setattr(mod, "_send_json_to_kafka", lambda **kwargs: sent.append(kwargs["payload"]))
    assert mod.publish_agent_system_configuration_event(env=env)
    event = sent[0]
    value = (
        event["metadata"]["namespace"]
        if missing.endswith("NAMESPACE")
        else event["payload"][0]["attributes"]["repoCode"]
    )
    assert value == ""
    assert missing in caplog.text


@pytest.mark.parametrize(
    "key,value,section,field",
    [
        ("CLUSTER_ID", "a" * 101, "metadata", "cluster-id"),
        ("NAMESPACE", "bad_namespace", "metadata", "namespace"),
        ("DISTRIBUTIVE", "https://example.invalid/" + "a" * 260, "metadata", "distributive"),
        ("REPO_CODE", "a" * 101, "system_prompt", "repoCode"),
        ("APP_CI", "not_a_ci", "system_prompt", "appCI"),
        ("MMS_AGENT_VERSION", "abc", "system_prompt", "mmsAgentVersion"),
        ("VERSION_NO", "1.2", "system_prompt", "versionNo"),
        ("FILENAME", "a" * 301, "system_prompt", "filename"),
    ],
)
def test_invalid_event_attribute_is_empty_and_logs_only_the_key(monkeypatch, caplog, key, value, section, field):
    import ouroboros.app.agent_system_configuration_kafka as mod

    env = {**_announcement_env(), "AGENT_VERSION": "1.2.1", f"AGENT_SYSTEM_CONFIGURATION_{key}": value}
    event = mod.build_agent_system_configuration_event(env=env)
    attributes = event["metadata"] if section == "metadata" else event["payload"][0]["attributes"]
    assert attributes[field] == ""
    assert f"AGENT_SYSTEM_CONFIGURATION_{key}" in caplog.text
    assert value not in caplog.text


def test_event_accepts_contract_boundary_lengths():
    import ouroboros.app.agent_system_configuration_kafka as mod

    values = {
        "CLUSTER_ID": "a" * 100,
        "NAMESPACE": "a" * 100,
        "DISTRIBUTIVE": "a" * 255,
        "REPO_CODE": "a" * 100,
        "MMS_AGENT_VERSION": "1" * 64,
        "FILENAME": "a" * 300,
    }
    env = {**_announcement_env(), **{f"AGENT_SYSTEM_CONFIGURATION_{key}": value for key, value in values.items()}}
    event = mod.build_agent_system_configuration_event(env=env)
    assert event["metadata"]["distributive"] == values["DISTRIBUTIVE"]
    assert event["payload"][0]["attributes"]["filename"] == values["FILENAME"]


def test_every_server_start_publishes_unchanged_configuration(monkeypatch, tmp_path):
    import ouroboros.app.agent_system_configuration_kafka as mod
    import server
    from ouroboros.storage.runtime_persistence import runtime_persistence
    from tests.helpers.shared import make_empty_skill_surface

    env = _announcement_env()
    runtime_persistence.initialize()
    sent = []
    monkeypatch.setattr(server, "is_server_profile", lambda: True)
    monkeypatch.setattr(mod, "_config_source", lambda _env: env)
    monkeypatch.setattr(mod, "_send_json_to_kafka", lambda **kwargs: sent.append(kwargs["payload"]))
    skill_surface = make_empty_skill_surface()
    server._publish_agent_system_configuration_startup_event(skill_surface)
    server._publish_agent_system_configuration_startup_event(skill_surface)
    assert len(sent) == 2


def test_configured_disabled_tool_is_reported_inactive(monkeypatch):
    import ouroboros.app.agent_system_configuration_kafka as mod
    from new_core.tools.catalog import ToolCatalog
    from new_core.tools.providers.builtin import BuiltinToolProvider

    monkeypatch.setenv("OUROBOROS_DISABLED_TOOLS", "data_read")
    catalog = ToolCatalog()
    catalog.initialize([BuiltinToolProvider(modules=["core"])])
    monkeypatch.setattr("new_core.tools.catalog.tool_catalog", catalog)
    tools = {item["tool-name"]: item for item in mod._collect_tools()}
    assert tools["data_read"]["active-tool"] is False
    assert tools["data_list"]["active-tool"] is True
    assert "run_shell" not in tools


def test_a_failed_publish_leaves_the_announcement_to_the_next_start(monkeypatch, tmp_path):
    """A failed send must not suppress publication on the next process start."""
    import ouroboros.app.agent_system_configuration_kafka as mod
    from ouroboros.storage.runtime_persistence import runtime_persistence

    runtime_persistence.initialize()
    env = _announcement_env()

    def _explode(**_kwargs):
        raise RuntimeError("kafka is unreachable")

    monkeypatch.setattr(mod, "_send_json_to_kafka", _explode)
    assert mod.publish_agent_system_configuration_event(env=env) is False

    sent: list[dict] = []
    monkeypatch.setattr(mod, "_send_json_to_kafka", lambda **kwargs: sent.append(kwargs["payload"]))
    assert mod.publish_agent_system_configuration_event(env=env) is True
    assert len(sent) == 1


def test_the_announcement_is_silent_when_the_switch_is_off(monkeypatch, tmp_path):
    import ouroboros.app.agent_system_configuration_kafka as mod
    from ouroboros.storage.runtime_persistence import runtime_persistence

    runtime_persistence.initialize()
    monkeypatch.setattr(mod, "_send_json_to_kafka", lambda **_kwargs: pytest.fail("published with the switch off"))

    assert (
        mod.publish_agent_system_configuration_event(
            env={**_announcement_env(), "AGENT_SYSTEM_CONFIGURATION_KAFKA_ENABLED": "false"},
        )
        is False
    )


def test_agent_id_falls_back_to_the_platforms_own_variable():
    """The deployment contract names `AGENT_ID`, the way it names `DISTRIBUTIVE`; a pod that
    sets only what the contract asks for must still register, not raise."""
    import ouroboros.app.agent_system_configuration_kafka as mod

    metadata = mod.build_agent_system_configuration_metadata(
        env={
            "AGENT_ID": "CI00001234",
            "AGENT_SYSTEM_CONFIGURATION_CLUSTER_ID": "dev1.apps.azwx5oj1.k8s.delta.sbrf.ru",
            "AGENT_SYSTEM_CONFIGURATION_NAMESPACE": "dev1",
            "DISTRIBUTIVE": "https://example.invalid/distrib.zip",
        }
    )

    assert metadata["agent-id"] == "CI00001234"


def test_giga_agent_start_uses_version_fallback_and_channel_configuration():
    import ouroboros.app.agent_system_configuration_kafka as mod

    env = {
        "AGENT_SYSTEM_CONFIGURATION_AGENT_ID": "CI00001234",
        "AGENT_SYSTEM_CONFIGURATION_CLUSTER_ID": "cluster",
        "AGENT_SYSTEM_CONFIGURATION_NAMESPACE": "namespace",
        "AGENT_SYSTEM_CONFIGURATION_DISTRIBUTIVE": "dist",
        "AGENT_SYSTEM_CONFIGURATION_REPO_CODE": "repo",
        "AGENT_SYSTEM_CONFIGURATION_APP_CI": "CI04549012",
        "AGENT_SYSTEM_CONFIGURATION_MMS_AGENT_VERSION": "456787",
        "AGENT_SYSTEM_CONFIGURATION_VERSION_NO": "1.2.1",
        "AGENT_SYSTEM_CONFIGURATION_FILENAME": "prompts.txt",
        "AGENT_VERSION": "2.0.0",
        "WORKSTATION_BASE_URL": "",
        "BROWSER_INGRESS_ENABLED": "true",
        "A2A_ENABLED": "false",
    }

    event = mod.build_agent_system_configuration_event(env=env, timestamp="2026-04-14T09:15:30Z")
    attrs = event["payload"][1]["attributes"]
    assert attrs["agent-version"] == "2.0.0"
    assert attrs["aef-workstation"] is False
    assert attrs["list-channels"] == [
        {"channel-name": "a2a", "channel-active": False},
        {"channel-name": "browser", "channel-active": True},
    ]


def test_giga_agent_start_invalid_version_is_empty(monkeypatch):
    import ouroboros.app.agent_system_configuration_kafka as mod

    monkeypatch.setattr(mod, "_collect_tools", list)
    monkeypatch.setattr(mod, "_collect_skills", lambda _surface: [])
    attrs = mod._build_giga_agent_start_attributes(
        {"AGENT_SYSTEM_CONFIGURATION_AGENT_VERSION": "D-1.1.0"},
        None,
    )
    assert attrs["agent-version"] == ""


def test_collect_tools_reports_effective_entries_with_startup_scope(monkeypatch):
    import ouroboros.app.agent_system_configuration_kafka as mod
    from new_core.identity import ServiceJob
    from ouroboros.contracts.owner_scope import service_identity

    descriptors = [
        types.SimpleNamespace(
            name="data_read",
            surface="core",
            schema=types.SimpleNamespace(description="Read data"),
        ),
        types.SimpleNamespace(
            name="calculator",
            surface="discoverable",
            schema=types.SimpleNamespace(description="Calculate"),
        ),
        types.SimpleNamespace(
            name="run_shell",
            surface="excluded",
            schema=types.SimpleNamespace(description="Run shell"),
        ),
    ]

    def scoped_descriptors(scope):
        assert scope == service_identity(job=ServiceJob.STARTUP).as_scope()
        return descriptors

    catalog_module = types.SimpleNamespace(tool_catalog=types.SimpleNamespace(descriptors=scoped_descriptors))
    monkeypatch.setitem(sys.modules, "new_core.tools.catalog", catalog_module)

    assert mod._collect_tools() == [
        {
            "tool-name": "calculator",
            "active-tool": True,
            "tool-short-description": "Calculate",
        },
        {
            "tool-name": "data_read",
            "active-tool": True,
            "tool-short-description": "Read data",
        },
    ]


def test_collect_skills_reads_the_injected_deployment_surface():
    import ouroboros.app.agent_system_configuration_kafka as mod

    visible = types.SimpleNamespace(
        name="calendar",
        description="Calendar tools",
        load_error=None,
        selected=True,
        eligible=True,
    )
    blocked = types.SimpleNamespace(
        name="blocked",
        description="Blocked",
        load_error=None,
        selected=True,
        eligible=False,
    )

    class PublishedSurface:
        def deployment_skills(self):
            return (visible, blocked)

    assert mod._collect_skills(PublishedSurface()) == [
        {
            "skill-name": "calendar",
            "skill-short-description": "Calendar tools",
        }
    ]


def test_collect_channels_keeps_configured_disabled_channels():
    import ouroboros.app.agent_system_configuration_kafka as mod

    assert mod._collect_channels(
        {
            "DIALOGS_GRPC_ENDPOINT": "grpc://dialogs",
            "DIALOGS_BOT_TOKEN": "",
            "BROWSER_INGRESS_ENABLED": "false",
        }
    ) == [
        {"channel-name": "browser", "channel-active": False},
        {"channel-name": "sber-chat", "channel-active": False},
    ]


@pytest.mark.parametrize("count", [2, 251])
def test_inventory_discovery_order_does_not_change_the_event(monkeypatch, count):
    import ouroboros.app.agent_system_configuration_kafka as mod

    descriptors = [
        types.SimpleNamespace(
            name=f"tool_{index:03d}", surface="core", schema=types.SimpleNamespace(description=f"Tool {index}")
        )
        for index in range(count)
    ]
    monkeypatch.setitem(
        sys.modules,
        "new_core.tools.catalog",
        types.SimpleNamespace(tool_catalog=types.SimpleNamespace(descriptors=lambda scope: descriptors)),
    )
    monkeypatch.setattr(mod, "_collect_skills", lambda _surface: [])
    env = _announcement_env()
    before = mod.build_agent_system_configuration_event(env=env, timestamp="2026-09-07T10:00:00Z")

    descriptors.reverse()

    after = mod.build_agent_system_configuration_event(env=env, timestamp="2026-09-07T10:00:00Z")
    assert after == before
    assert len(after["payload"][1]["attributes"]["list-tools"]) == min(count, 250)


def test_inventory_normalized_name_collisions_are_deterministic():
    import ouroboros.app.agent_system_configuration_kafka as mod

    items = [
        {"tool-name": "same", "tool-short-description": "Second"},
        {"tool-name": "same", "tool-short-description": "First"},
        {"tool-name": "another", "tool-short-description": "Another"},
    ]
    forward = mod._bounded_unique(items, key="tool-name", label="tools")
    backward = mod._bounded_unique(list(reversed(items)), key="tool-name", label="tools")

    assert forward == backward
    assert len(forward) == 2
