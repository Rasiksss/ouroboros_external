"""End to end: a configured remote agent is reachable as a tool.

Everything between the settings entry and the tool result is real — the manager, the provider,
the dispatch gates, the thread placement, and the cross-thread bridge. Only the peer is a fake,
and it is a real A2A server too.
"""

from __future__ import annotations

import asyncio
import threading

import pytest
from pydantic import SecretStr

# Requires a2a-sdk[http-server]==1.0.3 (see pyproject.toml).
pytest.importorskip("a2a.server.routes.jsonrpc_routes", reason="a2a-sdk not installed; skip A2A tests")

from new_core.tools.providers import A2aToolProvider
from ouroboros.egress.a2a.manager import A2aManager
from ouroboros.storage.settings.schema import A2aRemoteAgentEntry
from tests.helpers.egress_auth import a2a_egress_dependencies, unread_egress_auth_dependencies
from tests.helpers.factories import permissive_safety
from tests.helpers.shared import TEST_SCOPE, make_catalog, make_tool_ctx, run_tool
from tests.test_helper.a2a import build_fake_agent, install_fake_transport


@pytest.fixture
def background_loop():
    """A real loop on its own thread — the shape ``server.py`` hands the manager."""
    loop = asyncio.new_event_loop()
    thread = threading.Thread(target=loop.run_forever, daemon=True)
    thread.start()
    yield loop
    loop.call_soon_threadsafe(loop.stop)
    thread.join(timeout=5)
    loop.close()


def _run_on(loop, coro):
    return asyncio.run_coroutine_threadsafe(coro, loop).result(timeout=30)


def test_a_configured_agent_is_reachable_as_a_tool(background_loop, tmp_path, monkeypatch):
    from ouroboros.policy import safety

    monkeypatch.setattr(safety, "check_safety", permissive_safety)

    agent = build_fake_agent("helper", skills=[("summarise", "Condenses long text")])
    install_fake_transport(monkeypatch, agent)
    dependencies = a2a_egress_dependencies(unread_egress_auth_dependencies())

    manager = A2aManager()
    _run_on(
        background_loop,
        manager.initialize(
            [A2aRemoteAgentEntry(id="helper", name="Helper", url=agent.url, poll_interval_sec=0.05)],
            background_loop,
            True,
            dependencies,
        ),
    )
    try:
        catalog = make_catalog([A2aToolProvider(manager, 20, agents=None, loop=None, dependencies=dependencies)])
        ctx = make_tool_ctx(repo_dir=tmp_path, drive_root=tmp_path)

        names = [descriptor.name for descriptor in catalog.descriptors(TEST_SCOPE)]
        assert names == ["a2a__helper", "a2a_task_status", "a2a_task_cancel"]
        assert "summarise — Condenses long text" in catalog.find("a2a__helper", TEST_SCOPE).schema.description

        answered = run_tool(catalog, ctx, "a2a__helper", {"message": "slow:0.1 summarise this"})
        assert "answer: slow:0.1 summarise this" in answered

        submitted = run_tool(catalog, ctx, "a2a__helper", {"message": "slow:1 long job", "wait": False})
        assert "task_id=" in submitted
        task_id = submitted.split("task_id=", 1)[1].split()[0]

        collected = run_tool(
            catalog,
            ctx,
            "a2a_task_status",
            {"agent_id": "helper", "task_id": task_id, "wait_sec": 15},
        )
        assert "answer: slow:1 long job" in collected
    finally:
        _run_on(background_loop, manager.shutdown())


def test_nothing_is_offered_while_egress_is_disabled(background_loop, tmp_path, monkeypatch):
    agent = build_fake_agent("helper")
    install_fake_transport(monkeypatch, agent)
    dependencies = a2a_egress_dependencies(unread_egress_auth_dependencies())

    manager = A2aManager()
    _run_on(
        background_loop,
        manager.initialize([A2aRemoteAgentEntry(id="helper", url=agent.url)], background_loop, False, dependencies),
    )
    try:
        catalog = make_catalog([A2aToolProvider(manager, 20, agents=None, loop=None, dependencies=dependencies)])
        assert list(catalog.descriptors(TEST_SCOPE)) == []
    finally:
        _run_on(background_loop, manager.shutdown())


def test_the_multiuser_composition_reaches_a_remote_agent(
    background_loop, tmp_path, monkeypatch, restore_process_singletons
):
    from multiuser_config.agent_runtime.run import compose_process
    from multiuser_config.bootstrap import ProcessBootstrap
    from new_core.tools.catalog import tool_catalog
    from ouroboros.policy import safety
    from ouroboros.storage.settings.schema import AppSettings
    from tests.helpers.security_context import TEST_CREDENTIALS_KEY

    monkeypatch.setattr(safety, "check_safety", permissive_safety)
    agent = build_fake_agent("helper", skills=[("summarise", "Condenses long text")])
    install_fake_transport(monkeypatch, agent)
    manager = A2aManager()
    entry = A2aRemoteAgentEntry(id="helper", name="Helper", url=agent.url, poll_interval_sec=0.05)
    monkeypatch.setattr("ouroboros.egress.a2a.manager.a2a_manager", manager)
    monkeypatch.setattr("security_config.a2a.a2a_remote_agents", [entry])
    import os

    from tests.helpers.schema_harness import RUNTIME_DB_URL_PREFIX

    boot = ProcessBootstrap(
        settings=AppSettings(
            **{
                "A2A_EGRESS_ENABLED": True,
                "OUROBOROS_MULTIUSER": True,
                "OUROBOROS_CREDENTIALS_ENCRYPTION_KEY": SecretStr(TEST_CREDENTIALS_KEY),
                f"{RUNTIME_DB_URL_PREFIX}_1": os.environ[f"{RUNTIME_DB_URL_PREFIX}_1"],
            }
        ),
        drive_root=tmp_path,
        repo_dir=tmp_path,
    )

    compose_process(boot, tool_client_loop=background_loop)
    try:
        ctx = make_tool_ctx(repo_dir=tmp_path, drive_root=tmp_path)
        result = run_tool(tool_catalog, ctx, "a2a__helper", {"message": "summarise this"})

        assert "answer: summarise this" in result
    finally:
        _run_on(background_loop, manager.shutdown())
