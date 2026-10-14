"""Phase 4 regression tests for ``ouroboros.skills.extensions.loader``.

Covers PluginAPI surface: register_tool / inject_chat + permission gating +
namespace enforcement + unload cleanup.
"""

from __future__ import annotations

import json
import pathlib
import re
from dataclasses import replace

import pytest

from new_core.skills.manifest import SkillManifest
from new_core.skills.models import LoadedSkill, SkillBlockReason, SkillId
from ouroboros.contracts.plugin_api import (
    FORBIDDEN_EXTENSION_SETTINGS,
    VALID_EXTENSION_PERMISSIONS,
    PluginAPI,
)
from ouroboros.skills.extensions import loader as extension_loader
from ouroboros.skills.loader import skill_state_dir
from ouroboros.storage.blob.scopes import SCOPE_STATE
from tests.helpers.blob_double import InMemoryStorageProvider
from tests.helpers.fake_gateway import FakeGateway, RecordingMemoryEvents
from tests.helpers.shared import (
    TEST_SCOPE,
    AllowAllSkillPermissionChecker,
    clean_extension_runtime_state,
    find_test_skill as find_skill,
    make_empty_skill_surface,
    make_skill_surface,
    make_tool_env,
    run_tool,
)
from transport.skill_bridge import SkillBridge


def _skill_bridge(drive_root: pathlib.Path) -> SkillBridge:
    """The seam the composition root hands the loader; unused by tools-only skills."""
    return SkillBridge(
        drive_root,
        gateway=FakeGateway(),
        session_id="sess-test",
        memory_events=RecordingMemoryEvents(),
        permission_checker=AllowAllSkillPermissionChecker(),
    )


def _surface(
    drive_root: pathlib.Path,
    *,
    repo_path: pathlib.Path | str | None = None,
    workspace_root: pathlib.Path | None = None,
):
    if repo_path is None and workspace_root is None:
        return make_empty_skill_surface()
    return make_skill_surface(
        repo_dir=workspace_root or pathlib.Path(repo_path).parent,
        drive_root=drive_root,
        distribution_root=pathlib.Path(repo_path) if repo_path is not None else None,
    )


@pytest.fixture(autouse=True)
def _clear_loader_state():
    """Reset the module-level registries between tests."""
    clean_extension_runtime_state()
    yield
    clean_extension_runtime_state()


def _write_ext_skill(
    repo_root: pathlib.Path,
    name: str,
    *,
    plugin_body: str,
    permissions: list[str],
    env_from_settings: list[str] | None = None,
    entry: str = "plugin.py",
    extra_frontmatter: str = "",
) -> pathlib.Path:
    skill_dir = repo_root / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    perms_yaml = json.dumps(permissions)
    env_yaml = json.dumps(env_from_settings or [])
    (skill_dir / "SKILL.md").write_text(
        (
            "---\n"
            f"name: {name}\n"
            "description: Phase 4 extension.\n"
            "version: 0.1.0\n"
            "type: extension\n"
            f"entry: {entry}\n"
            f"permissions: {perms_yaml}\n"
            f"env_from_settings: {env_yaml}\n"
            f"{extra_frontmatter}"
            "---\n"
            "body\n"
        ),
        encoding="utf-8",
    )
    entry_path = skill_dir / entry
    entry_path.parent.mkdir(parents=True, exist_ok=True)
    entry_path.write_text(plugin_body, encoding="utf-8")
    return skill_dir


def _prepare_extension(
    tmp_path: pathlib.Path,
    name: str,
    plugin_body: str,
    permissions: list[str],
    env_from_settings: list[str] | None = None,
    extra_frontmatter: str = "",
):
    """Write + enable + PASS-review an extension so the loader accepts it."""
    repo_root = tmp_path / "skills"
    drive_root = tmp_path / "drive"
    drive_root.mkdir(exist_ok=True)
    _write_ext_skill(
        repo_root,
        name,
        plugin_body=plugin_body,
        permissions=permissions,
        env_from_settings=env_from_settings,
        extra_frontmatter=extra_frontmatter,
    )
    loaded = find_skill(drive_root, name, repo_path=str(repo_root))
    assert loaded is not None
    # Refetch with fresh state on the loaded struct.
    loaded = find_skill(drive_root, name, repo_path=str(repo_root))
    assert loaded is not None
    return loaded, repo_root, drive_root


def test_same_named_extensions_from_two_sources_keep_separate_runtime_state(tmp_path: pathlib.Path) -> None:
    drive_root = tmp_path / "drive"
    drive_root.mkdir()
    plugin = "def register(api):\n    api.register_tool('ping', lambda ctx: 'pong', description='p', schema={})\n"
    skills = []
    for source_id in ("source-one", "source-two"):
        skill_dir = _write_ext_skill(tmp_path / source_id, "calendar", plugin_body=plugin, permissions=["tool"])
        skills.append(
            LoadedSkill(
                skill_id=SkillId(source_id, "calendar"),
                name="calendar",
                skill_dir=skill_dir,
                manifest=SkillManifest(
                    name="calendar",
                    description="d",
                    version="1",
                    type="extension",
                    entry="plugin.py",
                    permissions=["tool"],
                ),
                content_hash="hash",
                eligible=True,
                distribution_owned=True,
            )
        )

    assert skill_state_dir(drive_root, skills[0].skill_id) != skill_state_dir(drive_root, skills[1].skill_id)
    assert [extension_loader.load_extension(skill, dict, drive_root=drive_root) for skill in skills] == [None, None]

    records = extension_loader.registered_tools()
    assert {record["skill_id"] for record in records} == {str(skill.skill_id) for skill in skills}
    assert len({record["name"] for record in records}) == 2


def test_reload_all_reconciles_only_the_selected_copy_of_one_stable_id(tmp_path: pathlib.Path) -> None:
    plugin = "def register(api):\n    api.register_tool('ping', lambda ctx: 'pong', description='p', schema={})\n"
    selected, _repo_root, drive_root = _prepare_extension(
        tmp_path,
        "selected-copy",
        plugin,
        permissions=["tool"],
    )
    shadowed = replace(
        selected,
        selected=False,
        eligible=False,
        blockers=[SkillBlockReason("invalid_package", "shadowed by selected copy")],
    )

    class DuplicateSurface:
        snapshot = type("Snapshot", (), {"skills": staticmethod(lambda: (selected, shadowed))})()

        @staticmethod
        def deployment_skills():
            return (selected, shadowed)

        @staticmethod
        def deployment_get(_skill):
            return selected

    results = extension_loader.reload_all(
        drive_root,
        dict,
        skill_surface=DuplicateSurface(),  # type: ignore[arg-type]
    )

    assert results == {selected.runtime_key: None}
    assert extension_loader.snapshot()["extensions"] == [selected.runtime_key]


def test_reload_all_does_not_import_an_extension_blocked_by_the_surface(tmp_path: pathlib.Path) -> None:
    workspace = tmp_path / "workspace"
    drive_root = tmp_path / "drive"
    drive_root.mkdir()
    _write_ext_skill(
        workspace / "skills",
        "blocked-extension",
        plugin_body="raise AssertionError('blocked extension code was imported')\n",
        permissions=["tool"],
    )
    surface = make_skill_surface(
        repo_dir=workspace,
        drive_root=drive_root,
        settings={"agents.defaults.skills": [], "agents.list": []},
    )

    results = extension_loader.reload_all(drive_root, dict, skill_surface=surface)

    assert results == {"blocked-extension": "agent allowlist blocks this skill"}
    assert extension_loader.snapshot() == {"extensions": [], "tools": []}


def test_reload_all_loads_an_extension_allowed_to_a_configured_agent(tmp_path: pathlib.Path) -> None:
    workspace = tmp_path / "workspace"
    drive_root = tmp_path / "drive"
    drive_root.mkdir()
    _write_ext_skill(
        workspace / "skills",
        "agent-extension",
        plugin_body="def register(api):\n    api.register_tool('ping', lambda ctx: 'pong', description='p', schema={})\n",
        permissions=["tool"],
    )
    surface = make_skill_surface(
        repo_dir=workspace,
        drive_root=drive_root,
        settings={
            "agents.defaults.skills": [],
            "agents.list": [{"name": "worker", "skills": ["distribution:agent-extension"]}],
        },
    )

    results = extension_loader.reload_all(drive_root, dict, skill_surface=surface)

    assert results == {"agent-extension": None}
    assert extension_loader.snapshot()["extensions"] == ["agent-extension"]


# ---------------------------------------------------------------------------
# PluginAPI contract shape
# ---------------------------------------------------------------------------


def test_plugin_api_impl_matches_protocol():
    """Runtime-checkable Protocol must structurally accept PluginAPIImpl."""
    impl = extension_loader.PluginAPIImpl(
        skill_name="x",
        permissions=(),
        env_allowlist=(),
        state_dir=pathlib.Path("/tmp"),
        settings_reader=dict,
    )
    assert isinstance(impl, PluginAPI)
    info = impl.get_runtime_info()
    assert sorted(info) == [
        "data_dir",
        "server_port",
        "skill_dir",
        "state_dir",
    ]


def test_plugin_api_runtime_info_uses_port_file(tmp_path, monkeypatch):
    """server_port must reflect the actual bound server port written by
    server.py, not the static AGENT_SERVER_PORT fallback."""
    import ouroboros.app.config as cfg

    port_file = tmp_path / "state" / "server_port"
    port_file.parent.mkdir()
    port_file.write_text("9012\n", encoding="utf-8")
    monkeypatch.setattr(cfg, "PORT_FILE", port_file)
    impl = extension_loader.PluginAPIImpl(
        skill_name="x",
        permissions=(),
        env_allowlist=(),
        state_dir=tmp_path / "state",
        settings_reader=dict,
    )

    assert impl.get_runtime_info()["server_port"] == 9012


def _state_api(
    tmp_path: pathlib.Path,
    provider: InMemoryStorageProvider,
    skill_name: str,
    *,
    permissions: tuple[str, ...] = ("fs",),
    state_dir_name: str | None = None,
):
    return extension_loader.PluginAPIImpl(
        skill_name=skill_name,
        permissions=permissions,
        env_allowlist=(),
        state_dir=tmp_path / "state" / "skills" / (state_dir_name or skill_name),
        settings_reader=dict,
        storage_provider=provider,
    )


def test_plugin_state_store_requires_fs(tmp_path):
    provider = InMemoryStorageProvider()
    denied = _state_api(tmp_path, provider, "denied", permissions=())

    with pytest.raises(extension_loader.ExtensionRegistrationError, match="fs"):
        denied.get_state_store()


def test_plugin_state_store_isolates_skill_namespaces(tmp_path):
    provider = InMemoryStorageProvider()
    first = _state_api(tmp_path, provider, "first").get_state_store()
    second = _state_api(tmp_path, provider, "second").get_state_store()
    assert first.create_json_if_absent("schedules/one.json", {"revision": 1})
    assert first.get_bytes("schedules/one.json") is not None
    assert second.get_bytes("schedules/one.json") is None
    assert provider.get_json(TEST_SCOPE, SCOPE_STATE, "skills/first/schedules/one.json") == {"revision": 1}


def test_plugin_state_store_uses_canonical_skill_name(tmp_path):
    provider = InMemoryStorageProvider()
    store = _state_api(tmp_path, provider, "canonical", state_dir_name="legacy-path").get_state_store()

    store.put_json("settings.json", {"enabled": True})

    assert provider.get_json(TEST_SCOPE, SCOPE_STATE, "skills/canonical/settings.json") == {"enabled": True}
    assert provider.get_json(TEST_SCOPE, SCOPE_STATE, "skills/legacy-path/settings.json") is None


def test_plugin_state_store_marks_bytes_as_binary(tmp_path):
    class RecordingProvider(InMemoryStorageProvider):
        content_type = ""

        def put_bytes(self, owner_scope, scope, key, data, *, content_type="application/octet-stream"):
            self.content_type = content_type
            return super().put_bytes(owner_scope, scope, key, data, content_type=content_type)

    provider = RecordingProvider()
    store = _state_api(tmp_path, provider, "binary").get_state_store()

    store.put_bytes("payload.bin", b"\x00\x01")

    assert provider.content_type == "application/octet-stream"
    assert store.get_bytes("payload.bin") == b"\x00\x01"


def test_forbidden_extension_settings_carries_repo_secrets():
    """The forbidden-settings tuple must match the repo-credentials set
    ``skill_exec`` already refuses to forward."""
    assert "OPENROUTER_API_KEY" in FORBIDDEN_EXTENSION_SETTINGS
    assert "GITHUB_TOKEN" in FORBIDDEN_EXTENSION_SETTINGS


def test_valid_permissions_is_closed_set():
    for needed in (
        "tool",
        "inject_chat",
        "send_message",
        "read_settings",
        "net",
        "fs",
        "subprocess",
        "companion_process",
        "supervised_task",
        "subscribe_event",
    ):
        assert needed in VALID_EXTENSION_PERMISSIONS


def test_send_message_requires_capability_and_preserves_delivery(tmp_path):
    calls: list[dict] = []

    class Bridge:
        def send_skill_message(self, skill_name, text, **kwargs):
            calls.append({"skill_name": skill_name, "text": text, **kwargs})

    denied = extension_loader.PluginAPIImpl(
        skill_name="scheduler",
        skill_id="distribution:scheduler",
        permissions=(),
        env_allowlist=(),
        state_dir=tmp_path,
        settings_reader=dict,
        skill_bridge=Bridge(),
    )
    with pytest.raises(extension_loader.ExtensionRegistrationError, match="send_message"):
        denied.send_message("Lunch", chat_id=42, source="browser")

    allowed = extension_loader.PluginAPIImpl(
        skill_name="scheduler",
        skill_id="distribution:scheduler",
        permissions=("send_message",),
        env_allowlist=(),
        state_dir=tmp_path,
        settings_reader=dict,
        skill_bridge=Bridge(),
    )
    allowed.send_message("Lunch", chat_id=42, source="browser", task_id="schedule:slot")

    assert calls == [
        {
            "skill_name": "distribution:scheduler",
            "text": "Lunch",
            "chat_id": 42,
            "source": "browser",
            "task_id": "schedule:slot",
        }
    ]


def test_send_message_propagates_transport_error(tmp_path):
    class Bridge:
        def send_skill_message(self, *_args, **_kwargs):
            raise extension_loader.SkillBridgeError("transport unavailable")

    api = extension_loader.PluginAPIImpl(
        skill_name="scheduler",
        permissions=("send_message",),
        env_allowlist=(),
        state_dir=tmp_path,
        settings_reader=dict,
        skill_bridge=Bridge(),
    )

    with pytest.raises(extension_loader.ExtensionRegistrationError, match="transport unavailable"):
        api.send_message("Lunch", chat_id=42, source="dialogs")


def test_a_plugin_api_without_a_bridge_refuses_instead_of_finding_a_global(tmp_path):
    """The seam arrives through the constructor; there is no process-wide instance to fall back to."""
    api = extension_loader.PluginAPIImpl(
        skill_name="scheduler",
        permissions=("send_message", "inject_chat"),
        env_allowlist=(),
        state_dir=tmp_path,
        settings_reader=dict,
    )

    for call in (
        lambda: api.send_message("Lunch", chat_id=42, source="dialogs"),
        lambda: api.inject_chat("Lunch", chat_id=42),
        lambda: api.allocate_internal_chat_id(),
    ):
        with pytest.raises(extension_loader.ExtensionRegistrationError, match="skill bridge is not configured"):
            call()


def test_loaded_extension_can_send_message_after_registration_closes(tmp_path):
    calls: list[dict] = []

    class Bridge:
        def send_skill_message(self, skill_name, text, **kwargs):
            calls.append({"skill_name": skill_name, "text": text, **kwargs})

    plugin = (
        "def register(api):\n"
        "    def deliver(ctx):\n"
        "        api.send_message('Lunch', chat_id=42, source='dialogs', task_id='schedule:slot')\n"
        "        return 'sent'\n"
        "    api.register_tool('deliver', deliver, description='deliver', schema={})\n"
    )
    loaded, _, drive_root = _prepare_extension(
        tmp_path,
        "runtime_sender",
        plugin,
        permissions=["tool", "send_message"],
    )

    error = extension_loader.load_extension(loaded, dict, drive_root=drive_root, skill_bridge=Bridge())
    assert error is None
    tool_name = extension_loader.extension_surface_name("runtime_sender", "deliver")
    tool = extension_loader.get_tool(tool_name)
    assert tool is not None

    assert tool["handler"](None) == "sent"
    assert calls == [
        {
            "skill_name": "distribution:runtime_sender",
            "text": "Lunch",
            "chat_id": 42,
            "source": "dialogs",
            "task_id": "schedule:slot",
        }
    ]


def test_loaded_extension_cannot_register_tool_after_registration_closes(tmp_path):
    plugin = (
        "def register(api):\n"
        "    def register_late(ctx):\n"
        "        api.register_tool('late', lambda ctx: 'late', description='late', schema={})\n"
        "    api.register_tool('trigger', register_late, description='trigger', schema={})\n"
    )
    loaded, _, drive_root = _prepare_extension(
        tmp_path,
        "closed_registration",
        plugin,
        permissions=["tool"],
    )

    error = extension_loader.load_extension(loaded, dict, drive_root=drive_root)
    assert error is None
    trigger_name = extension_loader.extension_surface_name("closed_registration", "trigger")
    trigger = extension_loader.get_tool(trigger_name)
    assert trigger is not None

    with pytest.raises(extension_loader.ExtensionRegistrationError, match="registration has closed"):
        trigger["handler"](None)
    late_name = extension_loader.extension_surface_name("closed_registration", "late")
    assert extension_loader.get_tool(late_name) is None


# ---------------------------------------------------------------------------
# Successful load + registration
# ---------------------------------------------------------------------------


def test_load_extension_registers_tool(tmp_path):
    plugin = (
        "def _echo(ctx, message='hi'):\n"
        "    return f'echo: {message}'\n"
        "def register(api):\n"
        "    api.register_tool(\n"
        "        'echo',\n"
        "        _echo,\n"
        "        description='echo',\n"
        "        schema={'type': 'object', 'properties': {'message': {'type': 'string'}}},\n"
        "    )\n"
    )
    loaded, _, drive_root = _prepare_extension(tmp_path, "ext1", plugin, permissions=["tool"])
    err = extension_loader.load_extension(loaded, dict, drive_root=drive_root)
    assert err is None, err
    tool_name = extension_loader.extension_surface_name("ext1", "echo")
    tool = extension_loader.get_tool(tool_name)
    assert tool is not None
    assert tool["name"] == tool_name
    assert tool["timeout_sec"] == 300
    assert callable(tool["handler"])
    # result_limit defaults to None (core runtime default applies).
    assert tool["result_limit"] is None


def test_load_extension_stores_declared_result_limit(tmp_path):
    plugin = (
        "def register(api):\n"
        "    api.register_tool(\n"
        "        'big',\n"
        "        lambda ctx: 'x',\n"
        "        description='big',\n"
        "        schema={'type': 'object', 'properties': {}},\n"
        "        result_limit=30000,\n"
        "    )\n"
    )
    loaded, _, drive_root = _prepare_extension(tmp_path, "extlim", plugin, permissions=["tool"])
    err = extension_loader.load_extension(loaded, dict, drive_root=drive_root)
    assert err is None, err
    tool = extension_loader.get_tool(extension_loader.extension_surface_name("extlim", "big"))
    assert tool is not None
    assert tool["result_limit"] == 30000


def test_register_tool_rejects_non_positive_result_limit(tmp_path):
    api = extension_loader.PluginAPIImpl(
        skill_name="reject_ext",
        permissions=("tool",),
        env_allowlist=(),
        state_dir=tmp_path / "state",
        settings_reader=dict,
    )
    for bad in (0, -1):
        with pytest.raises(extension_loader.ExtensionRegistrationError, match="result_limit"):
            api.register_tool(
                "bad",
                lambda ctx: "x",
                description="bad",
                schema={"type": "object", "properties": {}},
                result_limit=bad,
            )


def _plain_api(tmp_path, skill_name: str) -> extension_loader.PluginAPIImpl:
    return extension_loader.PluginAPIImpl(
        skill_name=skill_name,
        permissions=("tool",),
        env_allowlist=(),
        state_dir=tmp_path / "state",
        settings_reader=dict,
    )


def test_register_tool_enforces_result_limit_hard_max(tmp_path):
    from ouroboros.tools.catalog import RESULT_LIMIT_HARD_MAX

    api = _plain_api(tmp_path, "hardmax_ext")
    with pytest.raises(extension_loader.ExtensionRegistrationError, match="result_limit"):
        api.register_tool(
            "over",
            lambda ctx: "x",
            description="over",
            schema={"type": "object", "properties": {}},
            result_limit=RESULT_LIMIT_HARD_MAX + 1,
        )
    # Exactly the hard max is accepted.
    api.register_tool(
        "atmax",
        lambda ctx: "x",
        description="atmax",
        schema={"type": "object", "properties": {}},
        result_limit=RESULT_LIMIT_HARD_MAX,
    )
    tool = extension_loader.get_tool(extension_loader.extension_surface_name("hardmax_ext", "atmax"))
    assert tool is not None
    assert tool["result_limit"] == RESULT_LIMIT_HARD_MAX


def test_register_tool_validates_retrieval_aliases_and_parallel_safe(tmp_path):
    api = _plain_api(tmp_path, "aliases_ext")
    for bad_aliases in ("navigator", b"navigator", 42, ["ok", ""], [None]):
        with pytest.raises(extension_loader.ExtensionRegistrationError, match="retrieval_aliases"):
            api.register_tool(
                "bad",
                lambda ctx: "x",
                description="bad",
                schema={"type": "object", "properties": {}},
                retrieval_aliases=bad_aliases,
            )
    for bad_parallel in (1, "true", None):
        with pytest.raises(extension_loader.ExtensionRegistrationError, match="parallel_safe"):
            api.register_tool(
                "bad",
                lambda ctx: "x",
                description="bad",
                schema={"type": "object", "properties": {}},
                parallel_safe=bad_parallel,
            )
    api.register_tool(
        "good",
        lambda ctx: "x",
        description="good",
        schema={"type": "object", "properties": {}},
        retrieval_aliases=(" navigator ", "навигатор"),
        parallel_safe=True,
        safety_policy="skip",
    )
    tool = extension_loader.get_tool(extension_loader.extension_surface_name("aliases_ext", "good"))
    assert tool is not None
    assert tool["retrieval_aliases"] == ["navigator", "навигатор"]
    assert tool["parallel_safe"] is True


def test_register_tool_parallel_safe_without_skip_is_demoted(tmp_path):
    # A parallel batch must not stack blocking LLM safety calls on the loop,
    # but parallel_safe predates safety_policy (1.8), so the registration must
    # keep loading: the flag is demoted, not rejected.
    api = _plain_api(tmp_path, "parallel_ext")
    for index, declared in enumerate((None, "check")):
        name = f"demoted_{index}"
        api.register_tool(
            name,
            lambda ctx: "x",
            description="d",
            schema={"type": "object", "properties": {}},
            parallel_safe=True,
            safety_policy=declared,
        )
        tool = extension_loader.get_tool(extension_loader.extension_surface_name("parallel_ext", name))
        assert tool is not None
        assert tool["parallel_safe"] is False


def test_register_tool_without_new_kwargs_gets_safe_defaults(tmp_path):
    """1.8 default-compat: plugins written against <=1.7 keep working."""
    api = _plain_api(tmp_path, "compat_ext")
    api.register_tool(
        "legacy",
        lambda ctx: "x",
        description="legacy",
        schema={"type": "object", "properties": {}},
    )
    tool = extension_loader.get_tool(extension_loader.extension_surface_name("compat_ext", "legacy"))
    assert tool is not None
    assert tool["retrieval_aliases"] == []
    assert tool["parallel_safe"] is False


def test_distribution_extension_auto_loads_without_state_and_uses_tmp_staging(tmp_path):
    workspace = tmp_path / "workspace"
    repo_root = workspace / "skills"
    drive_root = tmp_path / "drive"
    drive_root.mkdir()
    _write_ext_skill(
        repo_root,
        "trusted_ext",
        plugin_body="def register(api): pass\n",
        permissions=[],
    )

    loaded = find_skill(drive_root, "trusted_ext", workspace_root=workspace)

    assert loaded is not None
    assert loaded.distribution_owned is True
    error = extension_loader.load_extension(loaded, dict, drive_root=drive_root)
    assert error is None
    import_root = pathlib.Path(extension_loader._extensions["trusted_ext"].import_root or "")
    assert str(import_root).startswith("/tmp/")
    assert drive_root not in import_root.parents
    assert import_root.is_dir()

    extension_loader.unload_extension("trusted_ext")

    assert not import_root.exists()


def test_extension_surface_names_are_provider_safe_without_renaming_skill_identity():
    from ouroboros.skills.loader import _sanitize_skill_name

    dotted = "foo.bar"
    unicode_name = "погода"
    dotted_tool = extension_loader.extension_surface_name(dotted, "fetch")
    unicode_tool = extension_loader.extension_surface_name(unicode_name, "fetch")
    generated_token_twin = "foo_bar_336d1b3d72"

    assert _sanitize_skill_name(dotted) == dotted
    assert _sanitize_skill_name("foo_bar") == "foo_bar"
    assert dotted_tool != extension_loader.extension_surface_name("foo_bar", "fetch")
    assert dotted_tool != extension_loader.extension_surface_name(generated_token_twin, "fetch")
    assert extension_loader.extension_surface_name("foo", "bar_baz") != extension_loader.extension_surface_name(
        "foo_bar", "baz"
    )
    for tool_name in (dotted_tool, unicode_tool):
        assert re.match(r"^[A-Za-z0-9_-]{1,64}$", tool_name)
        assert "." not in tool_name
        assert extension_loader.parse_extension_surface_name(tool_name) is not None


def test_load_extension_rejects_outward_symlink_in_skill_tree(tmp_path):
    import os
    import platform

    if platform.system() == "Windows":
        pytest.skip("symlink creation requires admin on Windows")
    skill_dir = tmp_path / "skills" / "symlinked"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        (
            "---\n"
            "name: symlinked\n"
            "description: Symlink escape regression.\n"
            "version: 0.1.0\n"
            "type: extension\n"
            "entry: plugin.py\n"
            'permissions: ["tool"]\n'
            "env_from_settings: []\n"
            "---\n"
            "body\n"
        ),
        encoding="utf-8",
    )
    (skill_dir / "plugin.py").write_text(
        (
            "from .helper import SECRET\n"
            "def _echo(ctx):\n"
            "    return SECRET\n"
            "def register(api):\n"
            "    api.register_tool('echo', _echo, description='echo', schema={})\n"
        ),
        encoding="utf-8",
    )
    drive_root = tmp_path / "drive"
    drive_root.mkdir()
    loaded = find_skill(drive_root, "symlinked", repo_path=str(skill_dir.parent))
    assert loaded is not None
    outside = tmp_path / "outside_helper.py"
    outside.write_text("SECRET = 'escape'\n", encoding="utf-8")
    os.symlink(outside, skill_dir / "helper.py")

    loaded = find_skill(drive_root, "symlinked", repo_path=str(skill_dir.parent))
    assert loaded is not None
    err = extension_loader.load_extension(loaded, dict, drive_root=drive_root)
    assert err is not None
    assert "symlink" in err.lower()
    assert extension_loader.get_tool(extension_loader.extension_surface_name("symlinked", "echo")) is None


def test_load_extension_ignores_isolated_env_symlinks_when_staging(tmp_path):
    import os
    import platform

    if platform.system() == "Windows":
        pytest.skip("symlink creation requires admin on Windows")
    loaded, repo_root, drive_root = _prepare_extension(
        tmp_path,
        "env_symlink",
        ("def register(api):\n    api.register_tool('ok', lambda ctx: 'ok', description='ok', schema={})\n"),
        permissions=["tool"],
    )
    skill_dir = repo_root / "env_symlink"
    bin_dir = skill_dir / ".ouroboros_env" / "python" / "bin"
    bin_dir.mkdir(parents=True)
    os.symlink("/usr/bin/env", bin_dir / "python")

    err = extension_loader.load_extension(loaded, dict, drive_root=drive_root)

    assert err is None, err
    with extension_loader._lock:
        import_root = pathlib.Path(extension_loader._extensions["env_symlink"].import_root)
    assert not (import_root / "skill" / ".ouroboros_env").exists()
    extension_loader.unload_extension("env_symlink")


def test_on_unload_callback_runs_during_unload(tmp_path):
    loaded, _, drive_root = _prepare_extension(
        tmp_path,
        "cleanup_ext",
        "import pathlib\n"
        "def register(api):\n"
        "    state_dir = pathlib.Path(api.get_state_dir())\n"
        "    api.on_unload(lambda: (state_dir / 'cleanup.txt').write_text('done', encoding='utf-8'))\n"
        "    api.register_tool('ping', lambda **kw: 'pong', description='ping', schema={})\n",
        permissions=["tool"],
    )
    err = extension_loader.load_extension(loaded, dict, drive_root=drive_root)
    assert err is None, err

    extension_loader.unload_extension("cleanup_ext")

    cleanup_file = drive_root / "state" / "skills" / "cleanup_ext" / "cleanup.txt"
    assert cleanup_file.read_text(encoding="utf-8") == "done"
    assert extension_loader.snapshot()["tools"] == []


def test_on_unload_callback_error_does_not_block_teardown(tmp_path):
    loaded, _, drive_root = _prepare_extension(
        tmp_path,
        "bad_cleanup_ext",
        "def register(api):\n"
        "    api.on_unload(lambda: (_ for _ in ()).throw(RuntimeError('boom')))\n"
        "    api.register_tool('ping', lambda **kw: 'pong', description='ping', schema={})\n",
        permissions=["tool"],
    )
    err = extension_loader.load_extension(loaded, dict, drive_root=drive_root)
    assert err is None, err

    extension_loader.unload_extension("bad_cleanup_ext")

    assert extension_loader.snapshot()["tools"] == []


def test_on_unload_callback_cannot_reregister_surfaces(tmp_path):
    loaded, _, drive_root = _prepare_extension(
        tmp_path,
        "ghost_ext",
        "def register(api):\n"
        "    api.on_unload(lambda: api.register_tool('ghost', lambda **kw: 'boo', description='ghost', schema={}))\n"
        "    api.register_tool('ping', lambda **kw: 'pong', description='ping', schema={})\n",
        permissions=["tool"],
    )
    err = extension_loader.load_extension(loaded, dict, drive_root=drive_root)
    assert err is None, err

    extension_loader.unload_extension("ghost_ext")

    snap = extension_loader.snapshot()
    assert snap["extensions"] == []
    assert snap["tools"] == []


def test_on_unload_delayed_callback_cannot_reregister_surfaces(tmp_path):
    import time

    loaded, _, drive_root = _prepare_extension(
        tmp_path,
        "delayed_ghost_ext",
        "import threading, time\n"
        "def register(api):\n"
        "    def cleanup():\n"
        "        def later():\n"
        "            time.sleep(0.1)\n"
        "            try:\n"
        "                api.register_tool('ghost', lambda **kw: 'boo', description='ghost', schema={})\n"
        "            except Exception:\n"
        "                pass\n"
        "        threading.Thread(target=later).start()\n"
        "    api.on_unload(cleanup)\n"
        "    api.register_tool('ping', lambda **kw: 'pong', description='ping', schema={})\n",
        permissions=["tool"],
    )
    err = extension_loader.load_extension(loaded, dict, drive_root=drive_root)
    assert err is None, err

    extension_loader.unload_extension("delayed_ghost_ext")
    time.sleep(0.3)

    snap = extension_loader.snapshot()
    assert snap["extensions"] == []
    assert snap["tools"] == []


def test_delayed_post_load_registration_is_rejected(tmp_path):
    import time

    loaded, _, drive_root = _prepare_extension(
        tmp_path,
        "late_register_ext",
        "import threading, time\n"
        "def register(api):\n"
        "    api.register_tool('ping', lambda **kw: 'pong', description='ping', schema={})\n"
        "    def later():\n"
        "        time.sleep(0.1)\n"
        "        try:\n"
        "            api.register_tool('ghost', lambda **kw: 'boo', description='ghost', schema={})\n"
        "        except Exception:\n"
        "            pass\n"
        "    threading.Thread(target=later).start()\n",
        permissions=["tool"],
    )
    err = extension_loader.load_extension(loaded, dict, drive_root=drive_root)
    assert err is None, err
    time.sleep(0.3)

    snap = extension_loader.snapshot()
    assert snap["tools"] == [extension_loader.extension_surface_name("late_register_ext", "ping")]


def test_reconcile_unload_callbacks_do_not_hold_loader_lock(tmp_path):
    loaded, _, drive_root = _prepare_extension(
        tmp_path,
        "lock_probe",
        "import pathlib, threading\n"
        "def register(api):\n"
        "    state_dir = pathlib.Path(api.get_state_dir())\n"
        "    def cleanup():\n"
        "        done = state_dir / 'snapshot_done.txt'\n"
        "        def worker():\n"
        "            from ouroboros.skills.extensions import loader as extension_loader\n"
        "            extension_loader.snapshot()\n"
        "            done.write_text('done', encoding='utf-8')\n"
        "        thread = threading.Thread(target=worker)\n"
        "        thread.start()\n"
        "        thread.join(timeout=1.0)\n"
        "        if not done.exists():\n"
        "            raise RuntimeError('snapshot blocked by loader lock')\n"
        "    api.on_unload(cleanup)\n"
        "    api.register_tool('ping', lambda **kw: 'pong', description='ping', schema={})\n",
        permissions=["tool"],
    )
    err = extension_loader.load_extension(loaded, dict, drive_root=drive_root)
    assert err is None, err

    # Make the extension undesired so reconcile unloads it through the normal path.
    state = extension_loader.reconcile_extension(
        SkillId("distribution", "lock_probe"),
        drive_root,
        dict,
        skill_surface=_surface(drive_root),
    )

    done_file = drive_root / "state" / "skills" / "lock_probe" / "snapshot_done.txt"
    assert done_file.read_text(encoding="utf-8") == "done"
    assert state["action"] == "extension_unloaded"


def test_concurrent_reconcile_converges_to_one_live_extension(tmp_path):
    import threading

    _loaded, _, drive_root = _prepare_extension(
        tmp_path,
        "race_ext",
        "import time\n"
        "def register(api):\n"
        "    time.sleep(0.05)\n"
        "    api.register_tool('ping', lambda **kw: 'pong', description='ping', schema={})\n",
        permissions=["tool"],
    )

    results = []
    repo_path = str(tmp_path / "skills")
    skill_surface = _surface(drive_root, repo_path=repo_path)
    threads = [
        threading.Thread(
            target=lambda: results.append(
                extension_loader.reconcile_extension(
                    SkillId("distribution", "race_ext"),
                    drive_root,
                    dict,
                    skill_surface=skill_surface,
                )
            )
        )
        for _ in range(2)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=2.0)

    assert len(results) == 2
    assert {r["action"] for r in results} <= {"extension_loaded", "extension_already_live"}
    snap = extension_loader.snapshot()
    assert snap["extensions"] == ["race_ext"]
    assert snap["tools"] == [extension_loader.extension_surface_name("race_ext", "ping")]
    assert (
        extension_loader.runtime_state_for_skill_name(SkillId("distribution", "race_ext"), skill_surface)["reason"]
        == "ready"
    )


def test_load_extension_permission_gate_tool(tmp_path):
    """Extension without 'tool' permission cannot register a tool."""
    plugin = "def _h(ctx): return 'ok'\ndef register(api):\n    api.register_tool('x', _h, description='', schema={})\n"
    loaded, _, drive_root = _prepare_extension(tmp_path, "nopoerm", plugin, permissions=["route"])
    err = extension_loader.load_extension(loaded, dict, drive_root=drive_root)
    assert err is not None
    assert "'tool'" in err


def test_reconcile_extension_keeps_live_extension_loaded(tmp_path, monkeypatch):
    plugin = (
        "def _echo(ctx):\n"
        "    return 'ok'\n"
        "def register(api):\n"
        "    api.register_tool('echo', _echo, description='echo', schema={})\n"
    )
    loaded, repo_root, drive_root = _prepare_extension(
        tmp_path,
        "steady",
        plugin,
        permissions=["tool"],
    )
    err = extension_loader.load_extension(loaded, dict, drive_root=drive_root)
    assert err is None, err
    unload_calls: list[str] = []
    monkeypatch.setattr(extension_loader, "unload_extension", unload_calls.append)

    state = extension_loader.reconcile_extension(
        SkillId("distribution", "steady"),
        drive_root,
        dict,
        skill_surface=_surface(drive_root, repo_path=repo_root),
    )
    assert state["reason"] == "ready"
    assert state["action"] == "extension_already_live"
    assert unload_calls == []
    assert "steady" in extension_loader.snapshot()["extensions"]


def test_load_extension_does_not_publish_a_second_snapshot_hash_during_load(tmp_path):
    plugin_v1 = (
        "def _echo(ctx):\n"
        "    return 'v1'\n"
        "def register(api):\n"
        "    api.register_tool('echo', _echo, description='echo', schema={})\n"
    )
    loaded, repo_root, drive_root = _prepare_extension(tmp_path, "snapshot_ext", plugin_v1, permissions=["tool"])
    (repo_root / "snapshot_ext" / "plugin.py").write_text(plugin_v1.replace("v1", "v2"), encoding="utf-8")

    assert extension_loader.load_extension(loaded, dict, drive_root=drive_root) is None
    assert extension_loader.runtime_state_for_loaded_skill(loaded)["live_loaded"] is True


def test_reconcile_extension_reloads_when_live_code_changes(tmp_path):
    skill_dir = tmp_path / "skills" / "reloadme"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        (
            "---\n"
            "name: reloadme\n"
            "description: Live reload.\n"
            "version: 0.1.0\n"
            "type: extension\n"
            "entry: plugin.py\n"
            'permissions: ["tool"]\n'
            "env_from_settings: []\n"
            "---\n"
            "body\n"
        ),
        encoding="utf-8",
    )
    (skill_dir / "plugin.py").write_text(
        (
            "def _echo(ctx):\n"
            "    return 'v1'\n"
            "def register(api):\n"
            "    api.register_tool('echo', _echo, description='echo', schema={})\n"
        ),
        encoding="utf-8",
    )
    drive_root = tmp_path / "drive"
    drive_root.mkdir()
    loaded = find_skill(drive_root, "reloadme", repo_path=str(skill_dir.parent))
    assert loaded is not None
    loaded = find_skill(drive_root, "reloadme", repo_path=str(skill_dir.parent))
    err = extension_loader.load_extension(loaded, dict, drive_root=drive_root)
    assert err is None, err
    tool = extension_loader.get_tool(extension_loader.extension_surface_name("reloadme", "echo"))
    assert tool is not None
    assert tool["handler"](None) == "v1"

    (skill_dir / "plugin.py").write_text(
        (
            "def _echo(ctx):\n"
            "    return 'v2'\n"
            "def register(api):\n"
            "    api.register_tool('echo', _echo, description='echo', schema={})\n"
        ),
        encoding="utf-8",
    )
    loaded = find_skill(drive_root, "reloadme", repo_path=str(skill_dir.parent))
    assert loaded is not None

    state = extension_loader.reconcile_extension(
        SkillId("distribution", "reloadme"),
        drive_root,
        dict,
        skill_surface=_surface(drive_root, repo_path=skill_dir.parent),
        retry_load_error=True,
    )
    assert state["action"] == "extension_loaded"
    assert state["live_loaded"] is True
    tool = extension_loader.get_tool(extension_loader.extension_surface_name("reloadme", "echo"))
    assert tool is not None
    assert tool["handler"](None) == "v2"


def test_runtime_state_preserves_matching_load_error(tmp_path):
    plugin = "def register(api):\n    api.register_tool('', lambda ctx: 'x', description='', schema={})\n"
    _loaded, repo_root, drive_root = _prepare_extension(
        tmp_path,
        "brokenlive",
        plugin,
        permissions=["tool"],
    )
    state = extension_loader.reconcile_extension(
        SkillId("distribution", "brokenlive"),
        drive_root,
        dict,
        skill_surface=_surface(drive_root, repo_path=repo_root),
        retry_load_error=True,
    )
    assert state["action"] == "extension_load_error"
    refreshed = extension_loader.runtime_state_for_skill_name(
        SkillId("distribution", "brokenlive"),
        _surface(drive_root, repo_path=repo_root),
    )
    assert refreshed["reason"] == "load_error"
    assert "non-empty" in str(refreshed["load_error"])
    assert refreshed["live_loaded"] is False


def test_runtime_state_for_skill_name_reports_missing_skill(tmp_path):
    drive_root = tmp_path / "drive"
    drive_root.mkdir()
    state = extension_loader.runtime_state_for_skill_name(
        SkillId("distribution", "ghost"),
        _surface(drive_root, repo_path=tmp_path / "skills"),
    )
    assert state["desired_live"] is False
    assert state["live_loaded"] is False
    assert state["reason"] == "missing"


def test_reconcile_extension_loads_from_workspace_root(tmp_path):
    workspace = tmp_path / "workspace"
    drive_root = tmp_path / "drive"
    drive_root.mkdir()
    plugin = (
        "def _echo(ctx):\n"
        "    return 'workspace'\n"
        "def register(api):\n"
        "    api.register_tool('echo', _echo, description='echo', schema={})\n"
    )
    _write_ext_skill(
        workspace / "skills",
        "workspace_ext",
        plugin_body=plugin,
        permissions=["tool"],
    )
    loaded = find_skill(drive_root, "workspace_ext", workspace_root=workspace)
    assert loaded is not None

    state = extension_loader.reconcile_extension(
        SkillId("distribution", "workspace_ext"),
        drive_root,
        dict,
        skill_surface=_surface(drive_root, workspace_root=workspace),
        retry_load_error=True,
    )

    assert state["action"] == "extension_loaded"
    tool = extension_loader.get_tool(extension_loader.extension_surface_name("workspace_ext", "echo"))
    assert tool is not None
    assert tool["handler"](None) == "workspace"
    assert extension_loader.get_tool("workspace_ext.echo") == tool


def test_reconcile_extension_ignores_data_native_skills_with_workspace_root(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    drive_root = tmp_path / "drive"
    drive_root.mkdir()
    plugin = (
        "def _echo(ctx):\n"
        "    return 'native'\n"
        "def register(api):\n"
        "    api.register_tool('echo', _echo, description='echo', schema={})\n"
    )
    _write_ext_skill(
        drive_root / "skills" / "native",
        "native_ext",
        plugin_body=plugin,
        permissions=["tool"],
    )
    loaded = find_skill(drive_root, "native_ext", workspace_root=workspace)
    assert loaded is None

    state = extension_loader.reconcile_extension(
        SkillId("distribution", "native_ext"),
        drive_root,
        dict,
        skill_surface=_surface(drive_root, workspace_root=workspace),
        retry_load_error=True,
    )

    assert state["action"] == "extension_inactive"
    assert state["reason"] == "missing"


def test_reconcile_extension_loads_from_repo_path_with_workspace_root(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    drive_root = tmp_path / "drive"
    drive_root.mkdir()
    repo_root = tmp_path / "repo_skills"
    plugin = (
        "def _echo(ctx):\n"
        "    return 'repo'\n"
        "def register(api):\n"
        "    api.register_tool('echo', _echo, description='echo', schema={})\n"
    )
    _write_ext_skill(
        repo_root,
        "repo_ext",
        plugin_body=plugin,
        permissions=["tool"],
    )
    loaded = find_skill(drive_root, "repo_ext", repo_path=str(repo_root), workspace_root=workspace)
    assert loaded is not None

    state = extension_loader.reconcile_extension(
        SkillId("distribution", "repo_ext"),
        drive_root,
        dict,
        skill_surface=_surface(drive_root, repo_path=repo_root, workspace_root=workspace),
        retry_load_error=True,
    )

    assert state["action"] == "extension_loaded"
    assert extension_loader.is_extension_live(
        SkillId("distribution", "repo_ext"),
        _surface(drive_root, repo_path=repo_root, workspace_root=workspace),
    )
    tool = extension_loader.get_tool(extension_loader.extension_surface_name("repo_ext", "echo"))
    assert tool is not None
    assert tool["handler"](None) == "repo"


def test_reload_all_loads_workspace_extensions(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    drive_root = tmp_path / "drive"
    drive_root.mkdir()
    plugin = (
        "def _echo(ctx):\n"
        "    return 'workspace reload'\n"
        "def register(api):\n"
        "    api.register_tool('echo', _echo, description='echo', schema={})\n"
    )
    _write_ext_skill(
        workspace / "skills",
        "reload_workspace_ext",
        plugin_body=plugin,
        permissions=["tool"],
    )
    loaded = find_skill(drive_root, "reload_workspace_ext", workspace_root=workspace)
    assert loaded is not None

    skill_surface = _surface(drive_root, workspace_root=workspace)
    monkeypatch.setattr(skill_surface, "get", lambda *_args, **_kwargs: pytest.fail("reload_all re-read a skill"))
    results = extension_loader.reload_all(
        drive_root,
        dict,
        skill_bridge=_skill_bridge(drive_root),
        skill_surface=skill_surface,
    )

    assert results["reload_workspace_ext"] is None
    tool = extension_loader.get_tool(extension_loader.extension_surface_name("reload_workspace_ext", "echo"))
    assert tool is not None
    assert tool["handler"](None) == "workspace reload"


def test_reload_all_preserves_repo_path_extension_with_workspace_root(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    drive_root = tmp_path / "drive"
    drive_root.mkdir()
    repo_root = tmp_path / "repo_skills"
    plugin = (
        "def _echo(ctx):\n"
        "    return 'repo reload'\n"
        "def register(api):\n"
        "    api.register_tool('echo', _echo, description='echo', schema={})\n"
    )
    _write_ext_skill(
        repo_root,
        "repo_reload_ext",
        plugin_body=plugin,
        permissions=["tool"],
    )
    loaded = find_skill(drive_root, "repo_reload_ext", repo_path=str(repo_root), workspace_root=workspace)
    assert loaded is not None
    loaded = find_skill(drive_root, "repo_reload_ext", repo_path=str(repo_root), workspace_root=workspace)
    assert loaded is not None
    err = extension_loader.load_extension(loaded, dict, drive_root=drive_root)
    assert err is None, err

    results = extension_loader.reload_all(
        drive_root,
        dict,
        skill_bridge=_skill_bridge(drive_root),
        skill_surface=_surface(drive_root, repo_path=repo_root, workspace_root=workspace),
    )

    assert results["repo_reload_ext"] is None
    assert "repo_reload_ext" in extension_loader.snapshot()["extensions"]


def test_tool_registry_dispatches_extension_alias_from_workspace(tmp_path):

    workspace = tmp_path / "workspace"
    drive_root = tmp_path / "drive"
    drive_root.mkdir()
    plugin = (
        "def _echo(ctx):\n"
        "    return 'alias ok'\n"
        "def register(api):\n"
        # safety_policy='skip' keeps this dispatch test about aliasing, not the safety
        # supervisor (distribution no longer exempts extension tools from safety, #84).
        "    api.register_tool('echo', _echo, description='echo', schema={}, safety_policy='skip')\n"
    )
    _write_ext_skill(
        workspace / "skills",
        "alias_ext",
        plugin_body=plugin,
        permissions=["tool"],
    )
    loaded = find_skill(drive_root, "alias_ext", workspace_root=workspace)
    assert loaded is not None
    results = extension_loader.reload_all(
        drive_root,
        dict,
        skill_bridge=_skill_bridge(drive_root),
        skill_surface=_surface(drive_root, workspace_root=workspace),
    )
    assert results["alias_ext"] is None

    catalog, ctx = make_tool_env(workspace, drive_root)

    assert run_tool(catalog, ctx, "alias_ext.echo", {}) == "alias ok"


def test_distribution_get_settings_auto_authorizes_declared_core_keys(tmp_path):
    plugin = (
        "def register(api):\n"
        "    values = api.get_settings(['OPENROUTER_API_KEY', 'TIMEZONE', 'MY_OK', 'RANDOM_OTHER'])\n"
        "    api.register_tool('n', lambda ctx: values, description='n', schema={})\n"
    )
    loaded, _, drive_root = _prepare_extension(
        tmp_path,
        "envtest",
        plugin,
        permissions=["tool", "read_settings"],
        env_from_settings=["OPENROUTER_API_KEY", "TIMEZONE", "MY_OK"],
    )
    settings_snapshot = {
        "OPENROUTER_API_KEY": "sk-leak",
        "TIMEZONE": "UTC",
        "MY_OK": "visible",
        "RANDOM_OTHER": "not-allowed",
    }
    err = extension_loader.load_extension(loaded, lambda: settings_snapshot, drive_root=drive_root)
    assert err is None
    tool = extension_loader.get_tool(extension_loader.extension_surface_name("envtest", "n"))
    assert tool is not None
    got = tool["handler"](None)
    assert got["OPENROUTER_API_KEY"] == "sk-leak"
    assert got["TIMEZONE"] == "UTC"
    assert got["MY_OK"] == "visible"
    assert "RANDOM_OTHER" not in got


def test_get_settings_rechecks_runtime_close_after_reader_returns(tmp_path):
    import threading

    reader_started = threading.Event()
    release_reader = threading.Event()

    def settings_reader():
        reader_started.set()
        assert release_reader.wait(1.0)
        return {"MY_OK": "visible"}

    impl = extension_loader.PluginAPIImpl(
        skill_name="settings_race",
        permissions=["read_settings"],
        env_allowlist=["MY_OK"],
        state_dir=tmp_path,
        settings_reader=settings_reader,
    )
    result = []
    thread = threading.Thread(target=lambda: result.append(impl.get_settings(["MY_OK"])))
    thread.start()
    assert reader_started.wait(1.0)
    close_done = threading.Event()
    close_thread = threading.Thread(target=lambda: (impl._close_runtime_access(), close_done.set()))
    close_thread.start()
    assert not close_done.wait(0.1)
    release_reader.set()
    thread.join(timeout=1.0)
    close_thread.join(timeout=1.0)

    assert close_done.is_set()
    assert result == [{}]
    assert impl.get_settings(["MY_OK"]) == {}


def test_unload_does_not_deadlock_with_inflight_get_settings(tmp_path):
    import threading
    import time

    reader_started = threading.Event()
    release_reader = threading.Event()

    def settings_reader():
        reader_started.set()
        release_reader.wait()
        return {"MY_OK": "visible"}

    loaded, _, drive_root = _prepare_extension(
        tmp_path,
        "settings_unload_race",
        "import threading\n"
        "def register(api):\n"
        "    threading.Thread(target=lambda: api.get_settings(['MY_OK'])).start()\n"
        "    api.register_tool('ping', lambda **kw: 'pong', description='ping', schema={})\n",
        permissions=["tool", "read_settings"],
        env_from_settings=["MY_OK"],
    )
    err = extension_loader.load_extension(loaded, settings_reader, drive_root=drive_root)
    assert err is None, err
    assert reader_started.wait(1.0)

    unload_done = threading.Event()
    unload_thread = threading.Thread(
        target=lambda: (extension_loader.unload_extension("settings_unload_race"), unload_done.set())
    )
    unload_thread.start()
    time.sleep(0.1)
    release_reader.set()
    unload_thread.join(timeout=1.0)

    assert unload_done.is_set()
    assert extension_loader.snapshot()["extensions"] == []


def test_unload_removes_all_registrations(tmp_path):
    plugin = "def _t(c): return 'x'\ndef register(api):\n    api.register_tool('t', _t, description='', schema={})\n"
    loaded, _, drive_root = _prepare_extension(
        tmp_path,
        "full",
        plugin,
        permissions=["tool"],
    )
    err = extension_loader.load_extension(loaded, dict, drive_root=drive_root)
    assert err is None, err
    snap = extension_loader.snapshot()
    assert snap["tools"]

    extension_loader.unload_extension("full")
    snap = extension_loader.snapshot()
    assert snap["tools"] == []
    assert snap["extensions"] == []


def test_reload_all_tears_down_stale_extensions(tmp_path):
    """reload_all must unload extensions that no longer exist on disk."""
    plugin = "def register(api):\n    api.register_tool('t', lambda ctx: 'ok', description='', schema={})\n"
    loaded, repo_root, drive_root = _prepare_extension(
        tmp_path,
        "staleish",
        plugin,
        permissions=["tool"],
    )
    err = extension_loader.load_extension(loaded, dict, drive_root=drive_root)
    assert err is None
    assert "staleish" in extension_loader.snapshot()["extensions"]
    # Nuke the skill directory; reload_all should tear it down.
    import shutil

    shutil.rmtree(repo_root / "staleish")
    extension_loader.reload_all(
        drive_root,
        dict,
        skill_bridge=_skill_bridge(drive_root),
        skill_surface=_surface(drive_root, repo_path=repo_root),
    )
    assert "staleish" not in extension_loader.snapshot()["extensions"]


def test_unload_clears_child_module_cache(tmp_path):
    """Phase 4 round 3 regression: unload must purge EVERY
    ``ouroboros._extensions.<skill>.*`` entry from sys.modules, not
    just the top-level module. Otherwise a helper-file edit sticks to
    the stale cached module on reload."""
    import sys as _sys

    skill_dir = tmp_path / "skills" / "tree_ext"
    (skill_dir).mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        (
            "---\n"
            "name: tree_ext\n"
            "description: Multi-file extension.\n"
            "version: 0.1.0\n"
            "type: extension\n"
            "entry: plugin.py\n"
            'permissions: ["tool"]\n'
            "env_from_settings: []\n"
            "---\n"
            "body\n"
        ),
        encoding="utf-8",
    )
    (skill_dir / "helper.py").write_text("X = 'v1'\n", encoding="utf-8")
    (skill_dir / "plugin.py").write_text(
        (
            "from .helper import X\n"
            "def _t(ctx): return X\n"
            "def register(api):\n"
            "    api.register_tool('echo', _t, description='', schema={})\n"
        ),
        encoding="utf-8",
    )
    drive_root = tmp_path / "drive"
    drive_root.mkdir()
    loaded = find_skill(drive_root, "tree_ext", repo_path=str(skill_dir.parent))
    assert loaded is not None
    loaded = find_skill(drive_root, "tree_ext", repo_path=str(skill_dir.parent))

    err = extension_loader.load_extension(loaded, dict, drive_root=drive_root)
    assert err is None, err
    # Both the package module and its helper child module must live in
    # sys.modules after import, and BOTH must be purged on unload.
    parent_key = extension_loader._module_key("tree_ext")
    child_key = f"{parent_key}.helper"
    assert parent_key in _sys.modules
    assert child_key in _sys.modules
    extension_loader.unload_extension("tree_ext")
    assert parent_key not in _sys.modules
    assert child_key not in _sys.modules


def test_load_extension_requires_explicit_drive_root(tmp_path):
    loaded, _repo_root, _drive_root = _prepare_extension(
        tmp_path,
        "requires_root",
        "def register(api):\n    pass\n",
        [],
    )

    with pytest.raises(TypeError):
        extension_loader.load_extension(loaded, dict)


def test_clean_extension_runtime_state_unloads_staged_import_root(tmp_path):
    loaded, _repo_root, drive_root = _prepare_extension(
        tmp_path,
        "cleanup_ext",
        "def register(api):\n    pass\n",
        [],
    )
    err = extension_loader.load_extension(loaded, dict, drive_root=drive_root)
    assert err is None, err
    with extension_loader._lock:
        import_root = pathlib.Path(extension_loader._extensions["cleanup_ext"].import_root)
    assert import_root.exists()

    clean_extension_runtime_state()

    assert not import_root.exists()
    assert "cleanup_ext" not in extension_loader.snapshot()["extensions"]


def test_reload_all_ignores_legacy_durable_extension_imports(tmp_path):
    _loaded, repo_root, drive_root = _prepare_extension(
        tmp_path,
        "sweep_ext",
        "def register(api):\n    pass\n",
        [],
    )
    stale_root = drive_root / "state" / "skills" / "sweep_ext" / "__extension_imports" / "stale"
    (stale_root / "skill").mkdir(parents=True)
    results = extension_loader.reload_all(
        drive_root,
        dict,
        skill_bridge=_skill_bridge(drive_root),
        skill_surface=_surface(drive_root, repo_path=repo_root),
    )

    assert results["sweep_ext"] is None
    assert stale_root.exists()
    live_root = pathlib.Path(extension_loader._extensions["sweep_ext"].import_root or "")
    assert str(live_root).startswith("/tmp/")


def test_reload_all_preserves_live_tmp_import_root_and_ignores_legacy_state(tmp_path):
    loaded, repo_root, drive_root = _prepare_extension(
        tmp_path,
        "live_sweep_ext",
        "def register(api):\n    pass\n",
        [],
    )
    err = extension_loader.load_extension(loaded, dict, drive_root=drive_root)
    assert err is None, err
    with extension_loader._lock:
        live_root = pathlib.Path(extension_loader._extensions["live_sweep_ext"].import_root)
    stale_root = drive_root / "state" / "skills" / "live_sweep_ext" / "__extension_imports" / "stale"
    (stale_root / "skill").mkdir(parents=True)
    results = extension_loader.reload_all(
        drive_root,
        dict,
        skill_bridge=_skill_bridge(drive_root),
        skill_surface=_surface(drive_root, repo_path=repo_root),
    )

    assert results["live_sweep_ext"] is None
    assert live_root.exists()
    assert str(live_root).startswith("/tmp/")
    assert stale_root.exists()


def test_tool_registration_collision_raises(tmp_path):
    """Two plugins registering the same tool namespace collide."""
    plugin_a = (
        "def register(api):\n"
        "    api.register_tool('same', lambda ctx: 'a', description='', schema={})\n"
        "    api.register_tool('same', lambda ctx: 'b', description='', schema={})\n"
    )
    loaded, _, drive_root = _prepare_extension(tmp_path, "collider", plugin_a, permissions=["tool"])
    err = extension_loader.load_extension(loaded, dict, drive_root=drive_root)
    assert err is not None
    assert "already registered" in err
    # Collision raised mid-registration must tear down the first tool too.
    assert extension_loader.snapshot()["tools"] == []


def test_operation_scope_mints_an_operation_per_unit_of_work(tmp_path):
    """A task registered at load has no caller; inheriting would freeze one id on the process."""
    from ouroboros.observability.aef_tracing import current_trace_id, normalize_trace_id, trace_scope

    api = extension_loader.PluginAPIImpl(
        skill_name="tasks-scheduler",
        permissions=("supervised_task",),
        env_allowlist=(),
        state_dir=tmp_path / "state" / "skills" / "tasks-scheduler",
        settings_reader=dict,
    )

    unrelated = "463ac35c-9f64-13ad-4848-5a3953bb6124"
    with trace_scope(unrelated):
        with api.operation_scope() as first:
            inside_first = current_trace_id()
        with api.operation_scope() as second:
            inside_second = current_trace_id()
        after = current_trace_id()

    assert normalize_trace_id(first) == first and first == inside_first
    assert normalize_trace_id(second) == second and second == inside_second
    # Minted, not inherited, and one per unit of work rather than one per task.
    assert first != second
    assert unrelated not in (first, second)
    assert after == unrelated
