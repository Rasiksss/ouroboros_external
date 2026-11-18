"""Exercise the dedicated runtime's real trusted-extension loader and HTTP bridge."""

from __future__ import annotations

import asyncio
import base64
import shutil
import sys
from pathlib import Path

import httpx
import pytest
import yaml
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import create_async_engine

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "aifa/finengine/src"))

from finengine.assistant import tool_api  # noqa: E402
from finengine.assistant.tool_call import ToolCall  # noqa: E402

from config_registry.user_settings.reader import user_settings_factory  # noqa: E402
from infra.async_engine import async_engine_factory  # noqa: E402
from infra.cache import cache_factory  # noqa: E402
from multiuser_config.agent_runtime.run import compose_process  # noqa: E402
from multiuser_config.bootstrap import ProcessBootstrap  # noqa: E402
from new_core.identity import OwnerScope  # noqa: E402
from new_core.skills.catalog import SkillCatalog  # noqa: E402
from new_core.skills.providers import DistributionSkillProvider  # noqa: E402
from new_core.skills.surface import SkillSurface  # noqa: E402
from new_core.tools.catalog import ToolCatalog, tool_catalog  # noqa: E402
from new_core.tools.context import ToolContext  # noqa: E402
from new_core.tools.providers.extension import ExtensionToolProvider  # noqa: E402
from ouroboros.skills.availability import build_skill_availability  # noqa: E402
from ouroboros.skills.extensions import loader  # noqa: E402
from ouroboros.skills.extensions.runtime import ExtensionRuntime  # noqa: E402
from ouroboros.skills.extensions.startup import (  # noqa: E402
    load_distribution_extensions,
    unload_distribution_extensions,
)
from ouroboros.skills.loader import DistributionSkillLoader  # noqa: E402
from ouroboros.storage.settings.bootstrap import bootstrap_config, reset_config_for_tests  # noqa: E402

EXPECTED = {
    "aifa_open",
    "aifa_wait",
    "aifa_state",
    "aifa_doc",
    "aifa_atoms",
    "aifa_move",
    "aifa_register_atom",
    "aifa_report",
    "aifa_finish",
}


@pytest.fixture(autouse=True)
def isolated_runtime(monkeypatch, tmp_path):
    unload_distribution_extensions()
    reset_config_for_tests()
    user_settings_factory.reset_for_tests(None)
    cache_factory.initialize("none", 0)
    path = tmp_path / "secrets"
    path.write_text("AIFA_ENGINE_TOKEN=shared-test-token")
    profile = yaml.safe_load((ROOT / "gigaagent/deploy/mu/agents.ift.example.yaml").read_text(encoding="utf-8"))[0][
        "ENV"
    ]
    cfg = bootstrap_config(
        environ={
            **profile,
            "OUROBOROS_ENV": "production",
            "OUROBOROS_MULTIUSER": "true",
            "OUROBOROS_TRUSTED_EXECUTABLE_SKILL_IDS": "distribution:aifa-engine",
            "OUROBOROS_USER_ENABLED_SKILL_IDS": "distribution:aifa-engine",
            "OUROBOROS_CREDENTIALS_ENCRYPTION_KEY": base64.b64encode(b"x" * 64).decode(),
            "OUROBOROS_REPO_DIR": str(ROOT / "gigaagent"),
            "OUROBOROS_DATA_DIR": str(tmp_path),
            "AIFA_ENGINE_URL": "http://aifa",
            "SECMAN_SECRETS_FILE": str(path),
        }
    ).settings
    yield cfg
    unload_distribution_extensions()
    user_settings_factory.reset_for_tests(None)
    tool_catalog.initialize([])
    reset_config_for_tests()


def surface_for(settings, directory=None):
    catalog = SkillCatalog()
    catalog.initialize(
        [
            DistributionSkillProvider(
                distribution_root=directory or ROOT / "gigaagent/skills", loader=DistributionSkillLoader(settings)
            )
        ]
    )
    return SkillSurface(snapshot=catalog.snapshot(), availability=build_skill_availability(settings))


def ctx_for(surface, runtime, tmp_path, owner):
    return ToolContext(
        repo_dir=ROOT / "gigaagent",
        drive_root=tmp_path,
        scope=OwnerScope(owner_id=owner, agent_id="main", surface_id="browser", chat_id="chat-" + owner),
        skill_surface=surface,
        extension_runtime=runtime,
        task_id="task-" + owner,
        root_task_id="task-" + owner,
        trace_id="trace-" + owner,
        transport={"source_message_id": "run-" + owner},
    )


def test_real_loader_registers_all_nine_tools_and_unloads(isolated_runtime, tmp_path):
    surface = surface_for(isolated_runtime)
    load_distribution_extensions(tmp_path, lambda: isolated_runtime, skill_surface=surface)
    runtime = ExtensionRuntime()
    provider = ExtensionToolProvider(skill_surface=surface, extension_runtime=runtime)
    ctx = ctx_for(surface, runtime, tmp_path, "one")
    descriptors = provider.descriptors(ctx.scope)
    assert {d.stable_name for d in descriptors} == {"aifa-engine." + name for name in EXPECTED}
    unload_distribution_extensions()
    assert not runtime.registered_tools()
    assert not provider.descriptors(ctx.scope)


@pytest.mark.asyncio
async def test_dedicated_compose_process_installs_real_extensions(isolated_runtime, tmp_path, monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    monkeypatch.setattr(async_engine_factory, "_engine", engine)
    monkeypatch.setattr(async_engine_factory, "initialize_beside_store", lambda settings: engine)
    monkeypatch.setattr("multiuser_config.agent_runtime.run.get_storage_provider", lambda: None)
    boot = ProcessBootstrap(settings=isolated_runtime, drive_root=tmp_path, repo_dir=ROOT / "gigaagent")
    try:
        snapshot, availability, runtime = compose_process(boot, tool_client_loop=asyncio.get_running_loop())
        assert isinstance(runtime, ExtensionRuntime)
        surface = SkillSurface(snapshot=snapshot, availability=availability)
        ctx = ctx_for(surface, runtime, tmp_path, "one")
        assert {d.stable_name for d in tool_catalog.descriptors(ctx.scope) if d.origin.startswith("ext:")} == {
            "aifa-engine." + name for name in EXPECTED
        }
    finally:
        await engine.dispose()


@pytest.mark.parametrize("allowed", [False, True])
def test_mu_trust_gate_is_preserved(isolated_runtime, tmp_path, allowed):
    cfg = isolated_runtime.model_copy(
        update={"OUROBOROS_TRUSTED_EXECUTABLE_SKILL_IDS": ["distribution:aifa-engine"] if allowed else []}
    )
    surface = surface_for(cfg)
    load_distribution_extensions(tmp_path, lambda: cfg, skill_surface=surface)
    assert bool(loader.registered_tools()) is allowed


def test_broken_extension_fails_startup_and_rolls_back_registrations(isolated_runtime, tmp_path):
    skill_root = tmp_path / "skills"
    shutil.copytree(ROOT / "gigaagent/skills/aifa-engine", skill_root / "aifa-engine")
    entry = skill_root / "aifa-engine/plugin.py"
    entry.write_text(
        entry.read_text(encoding="utf-8")
        + "\n_original_register = register\ndef register(api):\n    _original_register(api)\n    raise RuntimeError('broken test plugin')\n",
        encoding="utf-8",
    )
    with pytest.raises(RuntimeError, match="Distribution extension startup failed"):
        load_distribution_extensions(
            tmp_path, lambda: isolated_runtime, skill_surface=surface_for(isolated_runtime, skill_root)
        )
    assert not loader.registered_tools()


@pytest.mark.asyncio
@pytest.mark.parametrize("short_name", sorted(EXPECTED))
async def test_catalog_dispatch_to_real_aifa_route_keeps_two_user_contexts(
    isolated_runtime, tmp_path, monkeypatch, short_name
):
    surface = surface_for(isolated_runtime)
    load_distribution_extensions(tmp_path, lambda: isolated_runtime, skill_surface=surface)
    runtime = ExtensionRuntime()
    catalog = ToolCatalog()
    catalog.initialize([ExtensionToolProvider(skill_surface=surface, extension_runtime=runtime)])
    app = FastAPI()
    app.include_router(tool_api.router)
    calls = []

    class RecordingApi:
        async def handle(self, tool, body):
            call = ToolCall.model_validate_json(body)
            calls.append((tool, call))
            return tool_api.Reply("STATE " + call.ctx.owner_id)

    app.dependency_overrides[tool_api.get_tool_api] = lambda: RecordingApi()
    monkeypatch.setattr(tool_api.settings, "aifa_engine_token", "shared-test-token")
    original = httpx.AsyncClient

    def client_factory(*args, **kwargs):
        kwargs["transport"] = httpx.ASGITransport(app=app)
        return original(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", client_factory)
    contexts = [ctx_for(surface, runtime, tmp_path, owner) for owner in ["alice", "bob"]]
    name = next(d.name for d in catalog.descriptors(contexts[0].scope) if d.stable_name == "aifa-engine." + short_name)
    results = await asyncio.gather(*(catalog.aexecute(ctx, name, {}) for ctx in contexts))
    assert results == ["STATE alice", "STATE bob"]
    assert {call.ctx.owner_id for _, call in calls} == {"alice", "bob"}
    assert {tool for tool, _ in calls} == {short_name}
    for _, call in calls:
        assert call.ctx.chat_id == "chat-" + call.ctx.owner_id
        assert call.ctx.task_id == "task-" + call.ctx.owner_id
        assert call.ctx.run_id == "run-" + call.ctx.owner_id
    # Authentication failure must stop at the actual AIFA HTTP boundary.
    monkeypatch.setattr(tool_api.settings, "aifa_engine_token", "different-token")
    rejected = await catalog.aexecute(contexts[0], name, {})
    assert not rejected.startswith("STATE")
    assert len(calls) == 2


def test_revoking_mu_trust_unloads_live_extension(isolated_runtime, tmp_path):
    allowed = surface_for(isolated_runtime)
    load_distribution_extensions(tmp_path, lambda: isolated_runtime, skill_surface=allowed)
    assert len(loader.registered_tools()) == 9
    denied = isolated_runtime.model_copy(update={"OUROBOROS_TRUSTED_EXECUTABLE_SKILL_IDS": []})
    load_distribution_extensions(tmp_path, lambda: denied, skill_surface=surface_for(denied))
    assert not loader.registered_tools()
