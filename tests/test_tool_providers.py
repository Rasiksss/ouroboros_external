"""The four tool providers: policy fidelity, resolution order, and the safety exemption."""

from __future__ import annotations

import json
import logging
import pathlib
from typing import Any

import pytest

from new_core.tools.interfaces import IToolProvider, ToolDescriptor
from new_core.tools.providers import (
    BuiltinToolProvider,
    ExtensionToolProvider,
    McpToolProvider,
    SkillToolProvider,
)
from new_core.tools.schema import ToolSchema
from ouroboros.skills.extensions import loader as extension_loader
from ouroboros.tools.catalog import spec_for_tool
from tests._shared import clean_extension_runtime_state, make_catalog, seed_skill_execution_state


@pytest.fixture(autouse=True)
def _clear_loader_state():
    clean_extension_runtime_state()
    yield
    clean_extension_runtime_state()


# --- workspace helpers ------------------------------------------------------------


def _discover_workspace(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch):
    workspace = tmp_path / "workspace"
    drive = tmp_path / "drive"
    workspace.mkdir(parents=True)
    drive.mkdir(parents=True)
    (tmp_path / "home").mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setattr(
        "ouroboros.skills.loader.load_settings",
        lambda: {
            "skills.allowBundled": False,
            "skills.load.extraDirs": [],
            "skills.load.allowSymlinkTargets": [],
            "skills.load.watch": False,
            "skills.load.watchDebounceMs": 250,
            "agents.defaults.skills": None,
            "agents.list": [],
        },
    )
    return workspace, drive


def _write_tool_skill(root: pathlib.Path, name: str, tool_code: str) -> pathlib.Path:
    skill_dir = root / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: provider test skill\ntype: tool\n---\n",
        encoding="utf-8",
    )
    (skill_dir / "tool.py").write_text(tool_code, encoding="utf-8")
    return skill_dir


def _write_ext_skill(repo_root: pathlib.Path, name: str, plugin_body: str) -> pathlib.Path:
    skill_dir = repo_root / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(
        (
            "---\n"
            f"name: {name}\n"
            "description: provider test extension.\n"
            "version: 0.1.0\n"
            "type: extension\n"
            "entry: plugin.py\n"
            f"permissions: {json.dumps(['tool'])}\n"
            "env_from_settings: []\n"
            "---\n"
            "body\n"
        ),
        encoding="utf-8",
    )
    (skill_dir / "plugin.py").write_text(plugin_body, encoding="utf-8")
    return skill_dir


def _load_extension(tmp_path: pathlib.Path, name: str, plugin_body: str):
    """Write, enable, and load an extension. Skills under the repo root are
    distribution-owned, so the tools it registers carry ``distribution_owned=True``."""
    from ouroboros.skills.loader import find_skill

    repo_root = tmp_path / "skills"
    drive_root = tmp_path / "drive"
    drive_root.mkdir(parents=True, exist_ok=True)
    _write_ext_skill(repo_root, name, plugin_body)
    loaded = find_skill(drive_root, name, repo_path=str(repo_root))
    assert loaded is not None
    seed_skill_execution_state(drive_root, loaded)
    loaded = find_skill(drive_root, name, repo_path=str(repo_root))
    assert loaded is not None
    assert extension_loader.load_extension(loaded, dict, drive_root=drive_root) is None
    # The provider takes a workspace root; the skills dir is `<workspace>/skills`.
    return drive_root, tmp_path


_ECHO_TOOL_CODE = (
    "from new_core.tools.entry import ToolEntry\n\n"
    "def _handler(ctx=None, text=''):\n"
    "    return text\n\n"
    "def get_tools():\n"
    "    return [ToolEntry(\n"
    "        name='echo',\n"
    "        schema={'name': 'echo', 'description': 'test', "
    "'parameters': {'type': 'object', 'properties': {'text': {'type': 'string'}}, 'required': []}},\n"
    "        handler=_handler,\n"
    "    )]\n"
)


# --- 3.6 policy fidelity ----------------------------------------------------------


def _assert_matches_catalog_defaults(desc: ToolDescriptor) -> None:
    spec = spec_for_tool(desc.name)
    assert desc.surface == spec.surface
    assert desc.capability_class == spec.capability_class
    assert desc.safety_policy == spec.safety_policy


def test_builtin_descriptors_carry_catalog_policy() -> None:
    provider = BuiltinToolProvider()
    provider.refresh()
    descriptors = provider.descriptors()
    assert descriptors
    for desc in descriptors:
        _assert_matches_catalog_defaults(desc)


def test_skill_tool_descriptors_use_catalog_defaults(tmp_path, monkeypatch) -> None:
    workspace, drive = _discover_workspace(tmp_path, monkeypatch)
    _write_tool_skill(workspace / "skills", "echo-skill", _ECHO_TOOL_CODE)

    provider = SkillToolProvider(drive_root=drive, repo_dir=workspace)
    provider.refresh()
    descs = {d.name: d for d in provider.descriptors()}

    assert "echo_skill.echo" in descs
    desc = descs["echo_skill.echo"]
    # An unregistered name gets the catalog default; this refactor must not tighten it.
    _assert_matches_catalog_defaults(desc)
    assert desc.surface == "discoverable"
    assert desc.capability_class == "read_only"
    assert desc.origin == "skill:echo-skill"


_DECLARED_POLICY_TOOL_CODE = (
    "from new_core.tools.entry import ToolEntry\n\n"
    "def _handler(ctx=None):\n"
    "    return 'ok'\n\n"
    "def get_tools():\n"
    "    return [ToolEntry(\n"
    "        name='lookup',\n"
    "        schema={'name': 'lookup', 'description': 't', "
    "'parameters': {'type': 'object', 'properties': {'token': {'type': 'string'}}}},\n"
    "        handler=_handler,\n"
    "        safety_policy='skip',\n"
    "        capability_class='read_only',\n"
    "        parallel_safe=True,\n"
    "        result_limit=12345,\n"
    "        retrieval_aliases=('поиск', 'lookup'),\n"
    "        log_redacted_args=frozenset({'token'}),\n"
    "    )]\n"
)


def test_skill_tool_declared_policy_survives_namespacing(tmp_path, monkeypatch) -> None:
    # #84: a type=tool skill declares the full metadata contract, and every field
    # must reach the descriptor under the canonical namespaced name (the core
    # spec table cannot key it).
    workspace, drive = _discover_workspace(tmp_path, monkeypatch)
    _write_tool_skill(workspace / "skills", "lookup-skill", _DECLARED_POLICY_TOOL_CODE)

    provider = SkillToolProvider(drive_root=drive, repo_dir=workspace)
    provider.refresh()
    descs = {d.name: d for d in provider.descriptors()}

    desc = descs["lookup_skill.lookup"]
    assert desc.safety_policy == "skip"
    assert desc.capability_class == "read_only"
    assert desc.parallel_safe is True
    assert desc.result_limit == 12345
    assert desc.retrieval_aliases == ("поиск", "lookup")
    assert desc.log_redacted_args == frozenset({"token"})


def test_entry_from_decl_carries_the_specs_declared_metadata() -> None:
    # A decl-only spec has no row in TOOL_SPECS; whatever the conversion drops
    # is silently replaced by defaults (a declared 80k limit became 15k).
    from new_core.tools.providers.builtin import descriptor_from_entry, entry_from_decl
    from ouroboros.tools.spec import ToolSpec

    decl = ToolSpec(
        name="decl_only_tool",
        safety_policy="skip",
        capability_class="external_network",
        parallel=True,
        result_limit=80_000,
        schema={"name": "decl_only_tool", "description": "d", "parameters": {"type": "object", "properties": {}}},
        handler=lambda ctx: "ok",
    )

    desc = descriptor_from_entry(entry_from_decl(decl), "builtin")

    assert desc.safety_policy == "skip"
    assert desc.capability_class == "external_network"
    assert desc.parallel_safe is True
    assert desc.result_limit == 80_000


def _tool_skill_provider(tmp_path, monkeypatch, tool_code: str) -> SkillToolProvider:
    workspace, drive = _discover_workspace(tmp_path, monkeypatch)
    _write_tool_skill(workspace / "skills", "bad-skill", tool_code)
    provider = SkillToolProvider(drive_root=drive, repo_dir=workspace)
    provider.refresh()
    return provider


def test_skill_tool_with_a_forbidden_policy_loads_no_tools(tmp_path, monkeypatch) -> None:
    # check_conditional is extension-forbidden: declared on a skill tool its
    # shell whitelist would allow {"cmd": "ls ..."} without any LLM check.
    code = _ECHO_TOOL_CODE.replace("handler=_handler,", "handler=_handler,\n        safety_policy='check_conditional',")
    provider = _tool_skill_provider(tmp_path, monkeypatch, code)
    assert provider.descriptors() == []


def test_skill_tool_redacted_args_must_exist_in_the_schema(tmp_path, monkeypatch) -> None:
    # A typo in log_redacted_args must fail loudly, not leak the real argument.
    code = _ECHO_TOOL_CODE.replace(
        "handler=_handler,", "handler=_handler,\n        log_redacted_args=frozenset({'txet'}),"
    )
    provider = _tool_skill_provider(tmp_path, monkeypatch, code)
    assert provider.descriptors() == []


def test_skill_tool_parallel_safe_without_skip_is_demoted(tmp_path, monkeypatch) -> None:
    # Same rule as the extension loader: the declaration keeps loading, the
    # flag does not take effect without an explicit safety "skip".
    code = _ECHO_TOOL_CODE.replace("handler=_handler,", "handler=_handler,\n        parallel_safe=True,")
    provider = _tool_skill_provider(tmp_path, monkeypatch, code)
    descs = {d.name: d for d in provider.descriptors()}
    assert descs["bad_skill.echo"].parallel_safe is False


def test_skill_tool_with_a_forbidden_capability_class_loads_no_tools(tmp_path, monkeypatch) -> None:
    code = _ECHO_TOOL_CODE.replace("handler=_handler,", "handler=_handler,\n        capability_class='shell',")
    provider = _tool_skill_provider(tmp_path, monkeypatch, code)
    assert provider.descriptors() == []


def test_skill_tool_declared_values_are_normalized(tmp_path, monkeypatch) -> None:
    # register_tool strips and lowercases declarations; the skill path must not
    # be stricter (' SKIP ' silently dropping every tool of the skill).
    code = _ECHO_TOOL_CODE.replace("handler=_handler,", "handler=_handler,\n        safety_policy=' SKIP ',")
    provider = _tool_skill_provider(tmp_path, monkeypatch, code)
    descs = {d.name: d for d in provider.descriptors()}
    assert descs["bad_skill.echo"].safety_policy == "skip"


def test_skill_tool_string_parallel_safe_loads_no_tools(tmp_path, monkeypatch) -> None:
    # "false" is truthy: bool(descriptor.parallel_safe) would batch the tool
    # against its own declaration. Only a real bool is accepted.
    code = _ECHO_TOOL_CODE.replace("handler=_handler,", "handler=_handler,\n        parallel_safe='false',")
    provider = _tool_skill_provider(tmp_path, monkeypatch, code)
    assert provider.descriptors() == []


def test_skill_tool_string_parallel_safe_with_skip_loads_no_tools(tmp_path, monkeypatch) -> None:
    # With "skip" declared the demotion branch would not mask the bug: this is
    # the configuration where the truthy string would actually batch.
    code = _ECHO_TOOL_CODE.replace(
        "handler=_handler,", "handler=_handler,\n        parallel_safe='false',\n        safety_policy='skip',"
    )
    provider = _tool_skill_provider(tmp_path, monkeypatch, code)
    assert provider.descriptors() == []


def test_skill_tool_bare_string_aliases_load_no_tools(tmp_path, monkeypatch) -> None:
    # tuple("lookup") would explode into single-character search terms.
    code = _ECHO_TOOL_CODE.replace("handler=_handler,", "handler=_handler,\n        retrieval_aliases='lookup',")
    provider = _tool_skill_provider(tmp_path, monkeypatch, code)
    assert provider.descriptors() == []


def test_decorated_builtin_specs_match_their_catalog_rows() -> None:
    # entry_from_decl carries a ToolSpec decl's declared metadata, so for
    # @tool-decorated modules the decorator wins over TOOL_SPECS. This pin
    # keeps the two sources equal so the precedence flip stays invisible.
    import importlib

    from new_core.tools.providers.builtin import FROZEN_TOOL_MODULES
    from ouroboros.tools.catalog import TOOL_SPECS
    from ouroboros.tools.spec import ToolSpec

    checked = 0
    for modname in FROZEN_TOOL_MODULES:
        mod = importlib.import_module(f"ouroboros.tools.{modname}")
        for decl in mod.get_tools():
            if not isinstance(decl, ToolSpec) or decl.name not in TOOL_SPECS:
                continue
            row = TOOL_SPECS[decl.name]
            assert decl.safety_policy == row.safety_policy, decl.name
            assert decl.capability_class == row.capability_class, decl.name
            assert decl.parallel == row.parallel, decl.name
            assert decl.result_limit == row.result_limit, decl.name
            checked += 1
    assert checked >= 2  # calculator and calendar_resolve today


def test_extension_descriptors_use_catalog_defaults(tmp_path) -> None:
    plugin = "def register(api):\n    api.register_tool('ping', lambda ctx: 'pong', description='p', schema={})\n"
    drive_root, workspace_root = _load_extension(tmp_path, "ext_policy", plugin)

    provider = ExtensionToolProvider(drive_root, workspace_root)
    provider.refresh()
    descs = provider.descriptors()

    assert len(descs) == 1
    _assert_matches_catalog_defaults(descs[0])
    assert descs[0].origin == "ext:ext_policy"


def test_mcp_descriptors_use_catalog_defaults() -> None:
    class _Tool:
        server_id = "srv"
        original_name = "ping"
        full_name = "mcp__srv__ping"
        schema = ToolSchema(name="mcp__srv__ping", description="p", parameters={})

    class _Manager:
        def is_available(self):
            return True

        def discovered_tools(self):
            return [_Tool()]

        def resolve(self, name):
            return ("srv", "ping") if name == "mcp__srv__ping" else None

    provider = McpToolProvider(manager=_Manager(), timeout_reader=lambda: 30)
    provider.refresh()
    descs = provider.descriptors()

    assert len(descs) == 1
    _assert_matches_catalog_defaults(descs[0])
    assert descs[0].origin == "mcp:srv"
    assert descs[0].timeout_sec == 30


def test_mcp_timeout_is_read_at_call_time() -> None:
    seen: list[float] = []
    current = {"value": 10.0}

    class _Manager:
        def is_available(self):
            return True

        def discovered_tools(self):
            return []

        def resolve(self, name):
            return ("srv", "ping")

        def call_tool_sync(self, server_id, tool_name, arguments, timeout_sec):
            seen.append(timeout_sec)
            return "ok"

    provider = McpToolProvider(manager=_Manager(), timeout_reader=lambda: current["value"])
    provider.execute(None, "mcp__srv__ping", {})
    current["value"] = 99.0
    provider.execute(None, "mcp__srv__ping", {})

    assert seen == [10.0, 99.0]


# --- 3.7 collision ----------------------------------------------------------------


def test_skill_tool_cannot_shadow_a_builtin(tmp_path, monkeypatch, caplog) -> None:
    workspace, drive = _discover_workspace(tmp_path, monkeypatch)
    collision_code = (
        "from new_core.tools.entry import ToolEntry\n\n"
        "def _handler(ctx=None, **kw):\n"
        "    return 'shadow'\n\n"
        "def get_tools():\n"
        "    return [ToolEntry(\n"
        "        name='calculator',\n"
        "        schema={'name': 'calculator', 'description': 'shadow', "
        "'parameters': {'type': 'object', 'properties': {}, 'required': []}},\n"
        "        handler=_handler,\n"
        "    )]\n"
    )
    # A skill named "calculator" produces the alias "calculator.calculator"; to collide on
    # the bare built-in name the skill must be named so the namespace is empty.
    _write_tool_skill(workspace / "skills", "shadow-calc", collision_code)

    builtin = BuiltinToolProvider()
    skills = SkillToolProvider(drive_root=drive, repo_dir=workspace)

    # Force the collision the resolution order exists to settle.
    class _Shadow(IToolProvider):
        def refresh(self):
            return

        def descriptors(self):
            desc = builtin.get("calculator")
            assert desc is not None
            return [
                ToolDescriptor(
                    schema=desc.schema,
                    timeout_sec=1,
                    origin="skill:shadow-calc",
                    surface=desc.surface,
                    capability_class=desc.capability_class,
                    safety_policy=desc.safety_policy,
                )
            ]

        def get(self, name):
            return self.descriptors()[0] if name == "calculator" else None

        def refuse_dispatch(self, name):
            return None

        def execute(self, ctx, name, args):
            return "shadow"

    catalog = make_catalog([builtin, _Shadow(), skills])
    caplog.clear()  # wiring already refreshed once; count the warnings of one refresh
    with caplog.at_level(logging.WARNING, logger="new_core.tools.catalog"):
        catalog.refresh()

    collisions = [r for r in caplog.records if "Tool name collision: calculator" in r.getMessage()]
    assert len(collisions) == 1

    resolved = catalog.find("calculator")
    assert resolved is not None
    assert resolved.origin == "builtin"

    names = [d.name for d in catalog.descriptors()]
    assert names.count("calculator") == 1


# --- 3.8 safety exemption ---------------------------------------------------------


def test_distribution_owned_skill_no_longer_bypasses_safety(tmp_path) -> None:
    # #84: distribution_owned must NOT buy a safety exemption. A bundled tool
    # that states no policy is treated like any unknown tool — safety "check",
    # never skipped — and its safety runs through the normal supervisor.
    plugin = "def register(api):\n    api.register_tool('own', lambda ctx: 'ok', description='o', schema={})\n"
    drive_root, workspace_root = _load_extension(tmp_path, "ext_owned", plugin)

    # The record the loader registered really is distribution-owned...
    record = extension_loader.get_tool(extension_loader.extension_surface_name("ext_owned", "own"))
    assert record is not None
    assert record["distribution_owned"] is True

    # ...yet the descriptor no longer grants the exemption, and defaults to check.
    provider = ExtensionToolProvider(drive_root, workspace_root)
    descs = provider.descriptors()

    assert len(descs) == 1
    assert descs[0].skips_llm_safety is False
    assert descs[0].safety_policy == "check"


def test_non_distribution_owned_tool_keeps_the_safety_check(tmp_path) -> None:
    provider = ExtensionToolProvider(tmp_path, tmp_path)
    desc = provider._descriptor_from_record(
        {
            "name": "ext_5_r_open_ping",
            "description": "p",
            "schema": {},
            "skill": "open",
            "distribution_owned": False,
        }
    )

    assert desc.skips_llm_safety is False


# --- alias resolution -------------------------------------------------------------


def test_extension_alias_resolves_to_the_canonical_name(tmp_path) -> None:
    plugin = "def register(api):\n    api.register_tool('ping', lambda ctx: 'pong', description='p', schema={})\n"
    drive_root, workspace_root = _load_extension(tmp_path, "ext_alias", plugin)

    provider = ExtensionToolProvider(drive_root, workspace_root)
    canonical = extension_loader.extension_surface_name("ext_alias", "ping")

    by_alias = provider.get("ext_alias.ping")
    assert by_alias is not None
    assert by_alias.name == canonical


def test_two_skill_providers_with_different_roots_stay_separate(tmp_path, monkeypatch) -> None:
    workspace_a, drive_a = _discover_workspace(tmp_path / "a", monkeypatch)
    workspace_b, drive_b = _discover_workspace(tmp_path / "b", monkeypatch)
    _write_tool_skill(workspace_a / "skills", "echo-skill", _ECHO_TOOL_CODE)

    provider_a = SkillToolProvider(drive_root=drive_a, repo_dir=workspace_a)
    provider_b = SkillToolProvider(drive_root=drive_b, repo_dir=workspace_b)
    provider_a.refresh()
    provider_b.refresh()

    assert [d.name for d in provider_a.descriptors()] == ["echo_skill.echo"]
    assert list(provider_b.descriptors()) == []


# --- liveness ---------------------------------------------------------------------


def _dead_extension_loader(monkeypatch, records, live: bool):
    monkeypatch.setattr(extension_loader, "is_extension_live", lambda *a, **kw: live)
    monkeypatch.setattr(extension_loader, "registered_tools", lambda: list(records))
    by_name = {r["name"]: r for r in records}
    monkeypatch.setattr(extension_loader, "get_tool", lambda name: by_name.get(name))


_DEAD_RECORD = {
    "name": "ext_6_r_demo_ping",
    "description": "d",
    "schema": {},
    "skill": "demo",
    "timeout_sec": 5,
}


def test_a_dead_extensions_tool_is_invisible_to_get_as_well_as_descriptors(tmp_path, monkeypatch) -> None:
    """`get` and `descriptors` must answer the same question.

    They used to disagree: `list_available_tools` reads `descriptors()` and correctly hid a
    dead extension's tool, while `enable_tools` resolves through `get()` and would put the
    schema on the prompt surface — where every call then returns EXTENSION_NOT_LIVE for the
    rest of the task.
    """
    _dead_extension_loader(monkeypatch, [_DEAD_RECORD], live=False)
    provider = ExtensionToolProvider(tmp_path, tmp_path)

    assert list(provider.descriptors()) == []
    assert provider.get("ext_6_r_demo_ping") is None


def test_a_live_extensions_tool_resolves_through_get(tmp_path, monkeypatch) -> None:
    _dead_extension_loader(monkeypatch, [_DEAD_RECORD], live=True)
    provider = ExtensionToolProvider(tmp_path, tmp_path)

    desc = provider.get("ext_6_r_demo_ping")

    assert desc is not None and desc.name == "ext_6_r_demo_ping"
    assert [d.name for d in provider.descriptors()] == ["ext_6_r_demo_ping"]


def test_a_dead_extensions_tool_is_refused_by_name_and_unloaded(tmp_path, monkeypatch) -> None:
    """What `get` returning `None` costs, and how the provider pays it back.

    Hiding the tool is right — it must not reach the prompt surface — but it leaves dispatch
    with a name nothing resolves. `refuse_dispatch` is the provider saying "mine, and here
    is why not", ahead of the safety gate, and unloading what it just refused.
    """
    called: list[str] = []
    _dead_extension_loader(monkeypatch, [_DEAD_RECORD], live=False)
    monkeypatch.setattr(extension_loader, "unload_extension", lambda skill: called.append(skill))

    provider = ExtensionToolProvider(tmp_path, tmp_path)

    assert provider.refuse_dispatch("ext_6_r_demo_ping") == (
        "⚠️ EXTENSION_NOT_LIVE: extension 'demo' is not allowed to dispatch right now."
    )
    assert called == ["demo"]


def test_a_dead_extension_reaches_the_model_as_not_live_not_as_unknown(tmp_path, monkeypatch) -> None:
    """End to end, the way a task actually calls it: the real provider behind real dispatch.

    The two halves each looked right on their own — `get` correctly hides the tool, and
    `execute` correctly refuses it — while the path between them reported `Unknown tool`,
    because dispatch stops at an unresolved name and never reaches `execute`. Only a test
    that crosses both notices.
    """
    from new_core.tools.context import ToolContext
    from tests._shared import run_tool

    called: list[str] = []
    handler_ran: list[int] = []
    record = dict(_DEAD_RECORD, handler=lambda ctx, **kw: handler_ran.append(1) or "pong")
    _dead_extension_loader(monkeypatch, [record], live=False)
    monkeypatch.setattr(extension_loader, "unload_extension", lambda skill: called.append(skill))

    catalog = make_catalog([ExtensionToolProvider(tmp_path, tmp_path)])
    ctx = ToolContext(repo_dir=tmp_path, drive_root=tmp_path)

    result = run_tool(catalog, ctx, "ext_6_r_demo_ping", {})

    assert "EXTENSION_NOT_LIVE" in result
    assert "Unknown tool" not in result
    assert called == ["demo"], "the refusal is also what unloads it"
    assert handler_ran == []


def test_a_live_extension_has_nothing_to_refuse(tmp_path, monkeypatch) -> None:
    """`get` returned it, so dispatch never asks — and nothing gets unloaded behind its back."""
    called: list[str] = []
    _dead_extension_loader(monkeypatch, [_DEAD_RECORD], live=True)
    monkeypatch.setattr(extension_loader, "unload_extension", lambda skill: called.append(skill))

    provider = ExtensionToolProvider(tmp_path, tmp_path)

    assert provider.refuse_dispatch("ext_6_r_demo_ping") is None
    assert called == []


def test_a_name_that_is_not_an_extensions_stays_someone_elses_problem(tmp_path, monkeypatch) -> None:
    _dead_extension_loader(monkeypatch, [_DEAD_RECORD], live=False)
    provider = ExtensionToolProvider(tmp_path, tmp_path)

    assert provider.refuse_dispatch("repo_read") is None


def test_the_other_three_providers_withhold_nothing(tmp_path) -> None:
    """Each answers `None` on its own, and for its own reason — none of them inherits it.

    Builtin and skill tools are either in the provider's map or absent from it; MCP has no
    memory of a name `resolve` does not know. Only the extension provider reads its source
    live and can still name what it hides.
    """
    assert BuiltinToolProvider(modules=()).refuse_dispatch("anything") is None
    assert McpToolProvider().refuse_dispatch("anything") is None
    assert SkillToolProvider(drive_root=tmp_path, repo_dir=tmp_path).refuse_dispatch("anything") is None


def test_dispatch_to_a_dead_extension_refuses_and_unloads_it(tmp_path, monkeypatch) -> None:
    """The gate that stops a mid-task teardown from going on dispatching.

    An extension can go not-live *while* a task holds its schema — disabled, or its content
    hash invalidated. The refusal is what tells the model to stop; the unload is what stops
    the next task from paying for it again.
    """
    from types import SimpleNamespace

    called: list[str] = []
    handler_ran: list[int] = []
    record = dict(_DEAD_RECORD, handler=lambda ctx, **kw: handler_ran.append(1) or "pong")
    _dead_extension_loader(monkeypatch, [record], live=False)
    monkeypatch.setattr(extension_loader, "unload_extension", lambda skill: called.append(skill))

    provider = ExtensionToolProvider(tmp_path, tmp_path)
    ctx = SimpleNamespace(drive_root=tmp_path, repo_dir=tmp_path)

    result = provider.execute(ctx, "ext_6_r_demo_ping", {})

    assert "EXTENSION_NOT_LIVE" in result
    assert called == ["demo"], "a dead extension must be unloaded on the failed dispatch"
    assert handler_ran == [], "the handler must not run"


def test_dispatch_to_a_live_extension_runs_the_handler(tmp_path, monkeypatch) -> None:
    from types import SimpleNamespace

    handler_ran: list[int] = []
    record = dict(_DEAD_RECORD, handler=lambda ctx, **kw: handler_ran.append(1) or "pong")
    _dead_extension_loader(monkeypatch, [record], live=True)

    provider = ExtensionToolProvider(tmp_path, tmp_path)
    ctx = SimpleNamespace(drive_root=tmp_path, repo_dir=tmp_path)

    assert provider.execute(ctx, "ext_6_r_demo_ping", {}) == "pong"
    assert handler_ran == [1]


def test_the_liveness_walk_runs_once_per_skill_not_once_per_tool(tmp_path, monkeypatch) -> None:
    """`is_extension_live` walks the whole skills tree; a skill registering N tools paid N walks."""
    calls = {"n": 0}

    def _counting(*args, **kwargs):
        calls["n"] += 1
        return True

    records = [dict(_DEAD_RECORD, name=f"ext_6_r_demo_t{i}") for i in range(9)]
    _dead_extension_loader(monkeypatch, records, live=True)
    monkeypatch.setattr(extension_loader, "is_extension_live", _counting)

    descs = list(ExtensionToolProvider(tmp_path, tmp_path).descriptors())

    assert len(descs) == 9
    assert calls["n"] == 1, "one skill, one walk"


# --- hot-path cost ----------------------------------------------------------------


def test_the_code_tool_lookup_does_not_walk_the_filesystem(tmp_path, monkeypatch) -> None:
    """`is_code_tool` runs on every tool call; it must not trip the liveness walk.

    `ExtensionToolProvider.descriptors()` calls `is_extension_live` per tool, and that
    walks the skills tree. Sourcing code tools from local entries keeps the call free.
    """

    calls = {"n": 0}

    def _counting(*args, **kwargs):
        calls["n"] += 1
        return True

    monkeypatch.setattr(extension_loader, "is_extension_live", _counting)
    monkeypatch.setattr(
        extension_loader,
        "registered_tools",
        lambda: [
            {
                "name": "ext_6_r_demo_ping",
                "description": "d",
                "schema": {},
                "skill": "demo",
                "timeout_sec": 5,
            }
        ],
    )

    builtin = BuiltinToolProvider()
    catalog = make_catalog([builtin, ExtensionToolProvider(tmp_path, tmp_path)])
    builtin.refresh()
    # Wiring the catalog scans descriptors once for collisions, which does walk. That is a
    # startup cost paid once; what must stay free is the per-call lookup below.
    calls["n"] = 0

    names = catalog.code_tool_names()

    assert calls["n"] == 0
    # Still correct: the built-in code tools are all there.
    assert names == {d.name for d in builtin.descriptors() if d.is_code_tool}


# --- 4.3 handler arity ------------------------------------------------------------


def test_a_typeerror_inside_a_handler_is_not_retried(tmp_path) -> None:
    calls: list[str] = []

    def _handler(ctx, **kwargs):
        calls.append("call")
        raise TypeError("boom from inside the handler")

    provider = ExtensionToolProvider(tmp_path, tmp_path)
    record = {"name": "ext_x", "handler": _handler, "skill": "", "timeout_sec": 5}
    monkey = type("L", (), {"get_tool": staticmethod(lambda name: record)})
    provider._loader = staticmethod(lambda: monkey)  # type: ignore[method-assign]

    result = provider.execute(None, "ext_x", {})

    # The old `except TypeError` retry called the handler a second time without ctx.
    assert calls == ["call"]
    assert "TypeError: boom from inside the handler" in result


def test_a_handler_that_takes_no_ctx_is_called_without_one(tmp_path) -> None:
    seen: list[dict] = []

    def _handler(**kwargs):
        seen.append(dict(kwargs))
        return "ok"

    provider = ExtensionToolProvider(tmp_path, tmp_path)
    record = {"name": "ext_y", "handler": _handler, "skill": "", "timeout_sec": 5}
    monkey = type("L", (), {"get_tool": staticmethod(lambda name: record)})
    provider._loader = staticmethod(lambda: monkey)  # type: ignore[method-assign]

    assert provider.execute("CTX", "ext_y", {"a": 1}) == "ok"
    assert seen == [{"a": 1}]


def test_a_handler_that_takes_ctx_receives_it(tmp_path) -> None:
    seen: list[Any] = []

    def _handler(ctx, a=None):
        seen.append(ctx)
        return "ok"

    provider = ExtensionToolProvider(tmp_path, tmp_path)
    record = {"name": "ext_z", "handler": _handler, "skill": "", "timeout_sec": 5}
    monkey = type("L", (), {"get_tool": staticmethod(lambda name: record)})
    provider._loader = staticmethod(lambda: monkey)  # type: ignore[method-assign]

    assert provider.execute("CTX", "ext_z", {"a": 1}) == "ok"
    assert seen == ["CTX"]


@pytest.mark.parametrize(
    ("handler_src", "expects_ctx"),
    [
        ("def handler(query=''):\n    return f'noctx:{query}'", False),
        ("def handler(ctx, query=''):\n    return f'ctx:{query}'", True),
        ("def handler(**kwargs):\n    return f'kw:{kwargs[\"query\"]}'", False),
    ],
    ids=["no-ctx", "ctx", "kwargs-only"],
)
def test_arity_is_decided_on_the_handler_the_loader_actually_stores(tmp_path, handler_src, expects_ctx) -> None:
    """The three cases above inject a raw handler; production stores a *wrapped* one.

    ``PluginAPIImpl._wrap_runtime_handler`` puts a site-dirs scope around every handler
    registered from disk. That wrapper takes ``(*args, **kwargs)``, so without
    ``functools.wraps`` to carry ``__wrapped__`` the signature check sees the wrapper and
    reads every handler as taking ``ctx`` — the ctx-less form then fails on every call.
    Going through the real loader is the only way to catch that.
    """
    from ouroboros.skills.extensions.loader import PluginAPIImpl

    api = PluginAPIImpl.__new__(PluginAPIImpl)
    api._skill_dir = tmp_path
    api._dependency_site_dirs_enabled = False

    namespace: dict[str, Any] = {}
    exec(handler_src, namespace)
    stored = api._wrap_runtime_handler(namespace["handler"])

    provider = ExtensionToolProvider(tmp_path, tmp_path)
    record = {"name": "ext_w", "handler": stored, "skill": "", "timeout_sec": 5}
    monkey = type("L", (), {"get_tool": staticmethod(lambda name: record)})
    provider._loader = staticmethod(lambda: monkey)  # type: ignore[method-assign]

    result = provider.execute("CTX", "ext_w", {"query": "x"})

    assert result == ("ctx:x" if expects_ctx else ("noctx:x" if "noctx" in handler_src else "kw:x"))


def test_no_module_outside_the_loader_imports_the_private_globals() -> None:
    import ast

    loader_path = pathlib.Path(extension_loader.__file__).resolve()
    offenders: list[tuple[str, str]] = []
    for path in pathlib.Path("ouroboros").rglob("*.py"):
        if path.resolve() == loader_path:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.ImportFrom):
                continue
            if node.module != "ouroboros.skills.extensions.loader":
                continue
            for alias in node.names:
                if alias.name in {"_tools", "_lock"}:
                    offenders.append((str(path), alias.name))
    assert not offenders, f"private extension globals are still a cross-module surface: {offenders}"


# --- async extension handlers ------------------------------------------------------


def _async_extension_catalog(tmp_path):
    plugin = (
        "import asyncio\n\n"
        "def register(api):\n"
        "    async def quick(ctx):\n"
        "        await asyncio.sleep(0)\n"
        "        return 'quick-done'\n\n"
        "    async def slow(ctx):\n"
        "        await asyncio.sleep(30)\n"
        "        return 'never'\n\n"
        "    api.register_tool('quick', quick, description='q', schema={}, timeout_sec=5)\n"
        "    api.register_tool('slow', slow, description='s', schema={}, timeout_sec=1)\n"
    )
    drive_root, workspace_root = _load_extension(tmp_path, "ext_async", plugin)
    return ExtensionToolProvider(drive_root, workspace_root), drive_root, workspace_root


def test_extension_async_handler_keeps_its_own_timeout_through_the_bridge(tmp_path) -> None:
    """The per-tool extension bound, and its message, survive ``_await_on_thread``'s deletion.

    Two things that helper owned. The bound: it is the only place a *per-tool* extension
    timeout is enforced, and it is tighter than the outer ``_get_tool_timeout`` whenever
    ``OUROBOROS_TOOL_TIMEOUT_SEC`` exceeds the tool's own value. The provider now returns the
    wrapped awaitable instead of running it on a helper thread, so the bound has to travel
    with the coroutine and apply on whatever loop ends up awaiting it — here, one with a far
    larger deadline of its own.

    And the *report*: a handler that runs out of time is this provider's failure to name, as a
    synchronous handler's exception already is. Letting ``asyncio.TimeoutError`` escape instead
    reaches ``_execute_single_tool``, whose ``⚠️ TOOL_ERROR`` spelling appends
    ``str(exc)`` — empty for a 3.10 ``asyncio.TimeoutError``, so the model would be told a tool
    failed and given no reason at all.
    """
    import asyncio
    import time

    from new_core.tools.context import ToolContext

    provider, drive_root, workspace_root = _async_extension_catalog(tmp_path)
    catalog = make_catalog([provider])
    ctx = ToolContext(repo_dir=workspace_root, drive_root=drive_root)

    quick = extension_loader.extension_surface_name("ext_async", "quick")
    slow = extension_loader.extension_surface_name("ext_async", "slow")

    assert asyncio.run(catalog.aexecute(ctx, quick, {})) == "quick-done"

    async def _under_a_larger_call_site_bound() -> tuple[str, float]:
        started = time.monotonic()
        result = await asyncio.wait_for(catalog.aexecute(ctx, slow, {}), timeout=20)
        return result, time.monotonic() - started

    result, elapsed = asyncio.run(_under_a_larger_call_site_bound())

    # The tool's own second — not the 20s above it, and not the 300s default it would
    # otherwise inherit.
    assert elapsed < 5
    assert result == (f"⚠️ extension tool {slow!r} async handler failed: TimeoutError: handler exceeded timeout")


def test_the_provider_applies_the_bound_without_a_thread_or_a_loop(tmp_path) -> None:
    """6.1 — ``execute`` hands back an un-awaited awaitable and starts nothing itself."""
    import asyncio
    import threading

    from new_core.tools.context import ToolContext

    provider, drive_root, workspace_root = _async_extension_catalog(tmp_path)
    ctx = ToolContext(repo_dir=workspace_root, drive_root=drive_root)
    quick = extension_loader.extension_surface_name("ext_async", "quick")

    threads_before = threading.active_count()
    pending = provider.execute(ctx, quick, {})
    try:
        assert asyncio.iscoroutine(pending), "the provider must return the awaitable un-awaited"
        assert threading.active_count() == threads_before
    finally:
        assert asyncio.run(pending) == "quick-done"


def test_an_async_handler_that_raises_is_reported_as_this_provider_s_failure(tmp_path) -> None:
    """The other half of the contract: a raising handler, not a slow one.

    The synchronous path two lines above it in ``execute`` catches and names the exception;
    an asynchronous handler must read the same. Without it the exception escapes to the
    generic ``⚠️ TOOL_ERROR`` spelling, which loses the ``extension tool`` attribution that
    tells a reader whether the plugin or dispatch is at fault.
    """
    import asyncio

    from new_core.tools.context import ToolContext

    plugin = (
        "def register(api):\n"
        "    async def boom(ctx):\n"
        "        raise ValueError('handler blew up')\n\n"
        "    api.register_tool('boom', boom, description='b', schema={}, timeout_sec=5)\n"
    )
    drive_root, workspace_root = _load_extension(tmp_path, "ext_raises", plugin)
    catalog = make_catalog([ExtensionToolProvider(drive_root, workspace_root)])
    ctx = ToolContext(repo_dir=workspace_root, drive_root=drive_root)
    name = extension_loader.extension_surface_name("ext_raises", "boom")

    result = asyncio.run(catalog.aexecute(ctx, name, {}))

    assert result == f"⚠️ extension tool {name!r} async handler failed: ValueError: handler blew up"


def test_a_synchronous_extension_handler_returns_its_result_directly(tmp_path) -> None:
    """6.3 — nothing changes for the handler shape that was never asynchronous."""
    from new_core.tools.context import ToolContext

    plugin = (
        "def register(api):\n"
        "    def ping(ctx):\n"
        "        return 'sync-pong'\n\n"
        "    api.register_tool('ping', ping, description='p', schema={}, timeout_sec=5)\n"
    )
    drive_root, workspace_root = _load_extension(tmp_path, "ext_sync", plugin)
    provider = ExtensionToolProvider(drive_root, workspace_root)
    ctx = ToolContext(repo_dir=workspace_root, drive_root=drive_root)
    name = extension_loader.extension_surface_name("ext_sync", "ping")

    assert provider.execute(ctx, name, {}) == "sync-pong"


# --- where a provider's tools are placed --------------------------------------------


def test_no_synchronous_provider_carries_the_inline_default() -> None:
    """4.2 — the invariant behind the default flip, asserted rather than left to a profile.

    An inline placement wrapping a blocking ``execute`` makes a gathered read-only batch run
    strictly one call after another. No test fails; the round just gets slower. So the rule
    is stated directly: a provider whose ``execute`` is a plain synchronous function must not
    be placed inline. The next provider added is then caught here rather than in a profile.
    """
    import inspect

    from new_core.tools.runtimes import INLINE_RUNTIME

    offenders = []
    for provider_cls in (BuiltinToolProvider, ExtensionToolProvider, McpToolProvider, SkillToolProvider):
        execute = provider_cls.execute
        if inspect.iscoroutinefunction(execute) or inspect.isasyncgenfunction(execute):
            continue
        if provider_cls.default_runtime is INLINE_RUNTIME:
            offenders.append(provider_cls.__name__)

    assert not offenders, f"synchronous providers placed inline serialize a gathered batch: {offenders}"
