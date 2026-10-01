from __future__ import annotations

import importlib.util
import pathlib
import sys

import pytest

_SKILL_DIR = pathlib.Path(__file__).resolve().parents[1]
_REPO_ROOT = pathlib.Path(__file__).resolve().parents[3]
_PACKAGE = "test_assignment_delegation_skill_payload"


class _FakeApi:
    def __init__(self, values: dict | None = None):
        self.values = dict(values or {})
        self.requested_keys: list[str] | None = None

    def get_settings(self, keys):
        self.requested_keys = list(keys)
        return dict(self.values)


@pytest.fixture
def plugin_module():
    for name in list(sys.modules):
        if name == _PACKAGE or name.startswith(f"{_PACKAGE}."):
            sys.modules.pop(name, None)
    spec = importlib.util.spec_from_file_location(
        _PACKAGE,
        _SKILL_DIR / "plugin.py",
        submodule_search_locations=[str(_SKILL_DIR)],
    )
    assert spec is not None and spec.loader is not None
    plugin = importlib.util.module_from_spec(spec)
    sys.modules[_PACKAGE] = plugin
    try:
        spec.loader.exec_module(plugin)
        yield plugin
    finally:
        for name in list(sys.modules):
            if name == _PACKAGE or name.startswith(f"{_PACKAGE}."):
                sys.modules.pop(name, None)


def test_send_assignment_tool_rejects_extra_delivery_arguments(monkeypatch, tmp_path, plugin_module) -> None:
    from ouroboros.tools.registry import ToolContext

    monkeypatch.setattr(
        plugin_module,
        "deliver_assignment",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("delivery must not run")),
    )
    ctx = ToolContext(repo_dir=tmp_path, drive_root=tmp_path)

    result = plugin_module._send_assignment(
        _FakeApi(),
        ctx,
        subject="Тема",
        body="Текст",
        to="other@example.test",
    )

    assert result.startswith("⚠️ ASSIGNMENT_ARG_ERROR")
    assert "to" in result


def test_send_assignment_tool_returns_json_without_secrets(monkeypatch, tmp_path, plugin_module) -> None:
    from ouroboros.tools.registry import ToolContext

    def fake_deliver_assignment(*_args, **_kwargs):
        return {
            "status": "sent",
            "assignment_id": "abc",
            "email": {"status": "sent", "detail": "ok"},
            "sberchat": {"status": "sent", "detail": "ok"},
        }

    monkeypatch.setattr(plugin_module, "deliver_assignment", fake_deliver_assignment)
    ctx = ToolContext(repo_dir=tmp_path, drive_root=tmp_path)

    result = plugin_module._send_assignment(_FakeApi(), ctx, subject="Тема", body="Текст")

    assert '"status": "sent"' in result
    assert '"assignment_id": "abc"' in result
    assert "smtp-secret" not in result
    assert "dialogs-secret" not in result


def test_send_assignment_tool_redacts_delivery_targets(monkeypatch, tmp_path, plugin_module) -> None:
    from ouroboros.tools.registry import ToolContext

    def fake_deliver_assignment(*_args, **_kwargs):
        return {
            "status": "partial_failure",
            "assignment_id": "abc",
            "email": {
                "status": "partial_failure",
                "attempts": 2,
                "recipients": ["one@example.test"],
                "failed_recipients": ["two@example.test"],
                "smtp_code": 550,
                "error": "bad recipient",
            },
            "sberchat": {
                "status": "sent",
                "group_id": "42",
                "message_id": "mid-1",
            },
        }

    monkeypatch.setattr(plugin_module, "deliver_assignment", fake_deliver_assignment)
    ctx = ToolContext(repo_dir=tmp_path, drive_root=tmp_path, task_id="task-42")

    result = plugin_module._send_assignment(_FakeApi(), ctx, subject="Тема", body="Текст")

    assert result.startswith("⚠️ TOOL_ERROR (send_assignment):")
    assert '"assignment_id": "abc"' in result
    assert '"partial_failure"' in result
    assert "Some targets may already have received the assignment." in result
    assert "Do not retry automatically." in result
    assert "Report the partial delivery to the user" in result
    assert "one@example.test" not in result
    assert "two@example.test" not in result
    assert "group_id" not in result
    assert "message_id" not in result


def test_send_assignment_builds_settings_from_api_get_settings(monkeypatch, tmp_path, plugin_module) -> None:
    from ouroboros.storage.settings.schema import AppSettings
    from ouroboros.tools.registry import ToolContext

    captured: dict = {}

    def fake_deliver_assignment(*_args, **kwargs):
        captured.update(kwargs)
        return {"status": "sent", "assignment_id": "abc"}

    monkeypatch.setattr(plugin_module, "deliver_assignment", fake_deliver_assignment)
    api = _FakeApi(
        {
            "ASSISTANT_ASSIGNMENT_SMTP_HOST": "smtp.example.test",
            "ASSISTANT_ASSIGNMENT_SMTP_PORT": 2525,
            "ASSISTANT_ASSIGNMENT_SMTP_USERNAME": "smtp-user",
            "ASSISTANT_ASSIGNMENT_SMTP_PASSWORD": "smtp-secret",
            "ASSISTANT_ASSIGNMENT_MAIL_FROM": "agent@example.test",
            "ASSISTANT_ASSIGNMENT_MAIL_TO": "one@example.test",
            "DIALOGS_GRPC_ENDPOINT": "dialogs.example.test:443",
            "ASSISTANT_DIALOGS_BOT_TOKEN": "dialogs-secret",
            "ASSISTANT_DIALOGS_BOT_GROUP_ID": "42",
            "ASSISTANT_ASSIGNMENT_SMTP_TLS_VERSION": None,
        }
    )
    ctx = ToolContext(repo_dir=tmp_path, drive_root=tmp_path)

    result = plugin_module._send_assignment(api, ctx, subject="Тема", body="Текст")

    assert '"status": "sent"' in result
    # The sanctioned settings path: exactly the SKILL.md env_from_settings keys.
    assert api.requested_keys == list(plugin_module._SETTING_KEYS)
    assert len(plugin_module._SETTING_KEYS) == 21
    settings = captured["settings"]
    assert isinstance(settings, AppSettings)
    assert settings.ASSISTANT_ASSIGNMENT_SMTP_HOST == "smtp.example.test"
    assert settings.ASSISTANT_ASSIGNMENT_SMTP_PORT == 2525
    assert settings.ASSISTANT_ASSIGNMENT_SMTP_PASSWORD.get_secret_value() == "smtp-secret"
    # None values fall back to schema defaults instead of being passed through.
    assert settings.ASSISTANT_ASSIGNMENT_SMTP_TLS_VERSION == ""


def test_send_assignment_no_longer_gates_safety_in_plugin(monkeypatch, tmp_path, plugin_module) -> None:
    # #84: the in-plugin check_safety gate is gone; safety is the core
    # dispatcher's job via the declared safety_policy="check". The plugin must
    # not import or call check_safety anymore.
    # The plugin no longer imports check_safety as a module-level name to call.
    assert not hasattr(plugin_module, "check_safety")


def _load_assignment_extension(tmp_path):
    from ouroboros.skills.extensions import loader as extension_loader
    from ouroboros.skills.loader import find_skill

    extension_loader.unload_extension("assignment-delegation")
    drive_root = tmp_path / "drive"
    drive_root.mkdir()
    loaded = find_skill(drive_root, "assignment-delegation", repo_path=str(_SKILL_DIR.parent))
    assert loaded is not None
    assert extension_loader.load_extension(loaded, dict, drive_root=drive_root) is None
    surface_name = next(n for n in extension_loader.snapshot()["tools"] if n.endswith("_send_assignment"))
    return drive_root, surface_name


def _dispatch_send_assignment(tmp_path, monkeypatch, verdict):
    """Run send_assignment end-to-end through the core dispatch gate (#84).

    Returns (result, executed): what dispatch answered and which tool names
    actually reached the extension provider's execute.
    """
    import asyncio

    import ouroboros.policy.safety as safety
    from new_core.tools.catalog import ToolCatalog
    from new_core.tools.context import ToolContext
    from new_core.tools.providers import ExtensionToolProvider
    from ouroboros.skills.extensions import loader as extension_loader
    from ouroboros.tools.dispatch import execute_tool

    drive_root, surface_name = _load_assignment_extension(tmp_path)
    try:
        executed: list = []
        original = ExtensionToolProvider.execute

        def _spy(self, ctx, name, args):
            executed.append(name)
            return original(self, ctx, name, args)

        monkeypatch.setattr(ExtensionToolProvider, "execute", _spy)
        checked: dict = {}

        def _stub_check_safety(tool_name, arguments, messages=None, ctx=None, policy=None):
            checked["tool_name"] = tool_name
            checked["policy"] = policy
            return verdict

        monkeypatch.setattr(safety, "check_safety", _stub_check_safety)

        catalog = ToolCatalog()
        catalog.initialize([ExtensionToolProvider(drive_root, _SKILL_DIR.parents[1])])
        ctx = ToolContext(repo_dir=tmp_path, drive_root=drive_root)
        result = asyncio.run(execute_tool(catalog, ctx, surface_name, {"subject": "s", "body": "b"}))
        # The gate must receive the tool's declared policy on the canonical name.
        assert checked == {"tool_name": surface_name, "policy": "check"}
        return result, executed, surface_name
    finally:
        extension_loader.unload_extension("assignment-delegation")


def test_safety_deny_blocks_delivery_through_dispatch(tmp_path, monkeypatch) -> None:
    # The behavior the removed in-plugin gate used to pin: a deny verdict must
    # stop the delivery handler from running at all.
    result, executed, _ = _dispatch_send_assignment(tmp_path, monkeypatch, (False, "⚠️ SAFETY_DENIED: no"))

    assert result == "⚠️ SAFETY_DENIED: no"
    assert executed == [], "a denied call must never reach the delivery handler"


def test_safety_warning_prefixes_the_delivery_result(tmp_path, monkeypatch) -> None:
    result, executed, surface_name = _dispatch_send_assignment(tmp_path, monkeypatch, (True, "⚠️ careful"))

    assert result.startswith("⚠️ careful\n\n---\n")
    assert executed == [surface_name], "an allowed call runs, with the warning prefixed by the core"


def test_extension_registers_send_assignment_through_project_loader(tmp_path) -> None:
    from new_core.tools.catalog import ToolCatalog
    from new_core.tools.providers import ExtensionToolProvider
    from ouroboros.skills.extensions import loader as extension_loader
    from ouroboros.skills.loader import find_skill
    from ouroboros.tools.runtime_metadata import resolve_tool_runtime_metadata

    extension_loader.unload_extension("assignment-delegation")
    drive_root = tmp_path / "drive"
    drive_root.mkdir()
    loaded = find_skill(drive_root, "assignment-delegation", repo_path=str(_SKILL_DIR.parent))
    assert loaded is not None

    try:
        error = extension_loader.load_extension(loaded, dict, drive_root=drive_root)
        assert error is None
        tool_names = [name for name in extension_loader.snapshot()["tools"] if name.endswith("_send_assignment")]
        assert len(tool_names) == 1
        surface_name = tool_names[0]
        tool = extension_loader.get_tool(surface_name)
        assert tool is not None
        assert tool["timeout_sec"] == 360
        # #84: declared policy travels on the tool record to the descriptor.
        assert tool["safety_policy"] == "check"
        assert tool["capability_class"] == "external_network"
        description = str(tool["description"]).lower()
        assert "email" not in description
        assert "sberchat" not in description
        assert "recipient" not in description
        schema = tool["schema"]
        assert schema["required"] == ["subject", "body"]
        assert set(schema["properties"]) == {"subject", "body"}
        assert schema["additionalProperties"] is False
        assert tool["log_redacted_args"] == ["subject", "body"]

        # What the loop resolves per call: the declaration reaches the snapshot through
        # the extension provider's descriptor, which is the only path production uses.
        catalog = ToolCatalog()
        catalog.initialize([ExtensionToolProvider(drive_root, _SKILL_DIR.parents[1])])
        metadata = resolve_tool_runtime_metadata(surface_name, catalog)
        assert metadata.log_redacted_args == frozenset({"subject", "body"})
        # Delivery is not parallel-safe and declares no aliases.
        assert metadata.parallel_safe is False
        assert metadata.retrieval_aliases == ()
    finally:
        extension_loader.unload_extension("assignment-delegation")


def test_assignment_delegation_manifest(tmp_path) -> None:
    from ouroboros.skills.loader import find_skill

    skill_path = _SKILL_DIR / "SKILL.md"

    skill = find_skill(tmp_path, "assignment-delegation", workspace_root=_REPO_ROOT)

    assert skill is not None
    assert skill.manifest.type == "extension"
    assert skill.manifest.entry == "plugin.py"
    assert skill.manifest.version
    assert skill.manifest.validate() == []
    assert {"tool", "net", "read_settings"} <= set(skill.manifest.permissions)
    assert "ASSISTANT_ASSIGNMENT_SMTP_HOST" in skill.manifest.env_from_settings
    assert "ASSISTANT_ASSIGNMENT_DELIVERY_TIMEOUT_SEC" in skill.manifest.env_from_settings
    assert "ASSISTANT_DIALOGS_BOT_GROUP_ID" in skill.manifest.env_from_settings
    assert "If the send_assignment tool is absent" in skill.manifest.description
    text = skill_path.read_text(encoding="utf-8")
    text_lower = text.lower()
    assert "send_assignment" in text
    assert "не додумывай" in text_lower
    assert "как есть" in text_lower
    assert "уточни" in text_lower


def test_setting_keys_match_manifest_env_from_settings(tmp_path, plugin_module) -> None:
    from ouroboros.skills.loader import find_skill

    skill = find_skill(tmp_path, "assignment-delegation", workspace_root=_REPO_ROOT)

    assert skill is not None
    assert list(plugin_module._SETTING_KEYS) == list(skill.manifest.env_from_settings)


def test_send_assignment_reports_config_error_on_invalid_settings(monkeypatch, tmp_path, plugin_module) -> None:
    from ouroboros.tools.registry import ToolContext

    logged: list[dict] = []

    class _BrokenApi:
        def get_settings(self, keys):
            return {"ASSISTANT_ASSIGNMENT_SMTP_HOST": "smtp.test"}  # partial config fails validation

        def log(self, level, message, **fields):
            logged.append({"level": level, "message": message, **fields})

    ctx = ToolContext(repo_dir=tmp_path, drive_root=tmp_path)

    result = plugin_module._send_assignment(_BrokenApi(), ctx, subject="Тема", body="Текст")

    assert result.startswith("⚠️ TOOL_ERROR (send_assignment): ASSIGNMENT_CONFIG_ERROR")
    assert "smtp.test" not in result
    assert logged and logged[0]["level"] == "error"
    assert "smtp.test" not in str(logged)
