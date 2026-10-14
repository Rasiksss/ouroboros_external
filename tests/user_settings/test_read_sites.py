"""Every reader passes the scope of the work in hand, and two of them do not collide."""

from __future__ import annotations

import ast
import pathlib

from config_registry.user_settings.api.http.endpoint import OWNER_SETTING_KEYS
from new_core.identity import OwnerScope, SurfaceId
from ouroboros.app.config import get_light_model, get_main_model, resolve_effort
from ouroboros.runtime_time import runtime_time_config
from ouroboros.storage.settings.store import load_settings
from tests.helpers.user_settings import StubUserSettingsStore, reset_user_settings_for_tests

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
#: The packages a run reads settings from. Tests and the composition roots are elsewhere.
_PRODUCTION = ("background_memory", "config_registry/user_settings", "new_core", "ouroboros", "supervisor", "transport")
#: The accessors, their `async_` twins and the functions that wrap them; each takes the scope as
#: its first argument.
_SCOPED_CALLS = (
    frozenset(OWNER_SETTING_KEYS)
    | {f"async_{key}" for key in OWNER_SETTING_KEYS}
    | {
        "get_light_model",
        "get_main_model",
        "resolve_effort",
        "runtime_time_config",
        "default_model",
    }
)
#: The instance's own owner, in both of the spellings the tree has for it.
_MINTS = ("SETTINGS_OWNER_ID", "configured_owner_id")


def _scope(owner_id: str, agent_id: str) -> OwnerScope:
    return OwnerScope(owner_id=owner_id, agent_id=agent_id, surface_id=SurfaceId.BROWSER, chat_id="c1")


def _called_name(node: ast.Call) -> str:
    func = node.func
    if isinstance(func, ast.Attribute):
        return func.attr
    return func.id if isinstance(func, ast.Name) else ""


def _substitutes_the_instance_owner(argument: ast.expr) -> bool:
    if isinstance(argument, ast.Constant):
        return True
    if isinstance(argument, ast.Name):
        return argument.id in _MINTS
    if isinstance(argument, ast.Call):
        return _called_name(argument) in _MINTS
    return False


def test_every_scanned_package_exists() -> None:
    assert [package for package in _PRODUCTION if not (REPO_ROOT / package).is_dir()] == []


def test_no_read_site_passes_a_constant_owner_in_place_of_a_scope() -> None:
    offenders: list[str] = []
    for package in _PRODUCTION:
        for path in (REPO_ROOT / package).rglob("*.py"):
            if "__pycache__" in path.parts:
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call) or _called_name(node) not in _SCOPED_CALLS:
                    continue
                if node.args and _substitutes_the_instance_owner(node.args[0]):
                    offenders.append(f"{path.relative_to(REPO_ROOT)}:{node.lineno}")
    assert not offenders, f"these readers name an owner instead of passing the scope they hold: {offenders}"


def test_the_instance_owner_is_named_only_by_the_contract_that_defines_it() -> None:
    named = {
        str(path.relative_to(REPO_ROOT))
        for package in _PRODUCTION
        for path in (REPO_ROOT / package).rglob("*.py")
        if "__pycache__" not in path.parts and "SETTINGS_OWNER_ID" in path.read_text(encoding="utf-8")
    }
    assert named == {"ouroboros/contracts/owner_scope.py"}


def test_two_scopes_resolve_their_own_overrides_in_one_process() -> None:
    alice = _scope("alice", "main")
    bob = _scope("bob", "main")
    alice_sub = _scope("alice", "sub-1")
    reset_user_settings_for_tests(
        StubUserSettingsStore(
            by_pair={
                ("alice", "main"): {
                    "OUROBOROS_MODEL": "alice-main",
                    "OUROBOROS_EFFORT_TASK": "high",
                    "OUROBOROS_TIMEZONE": "Asia/Vladivostok",
                },
                ("alice", "sub-1"): {"OUROBOROS_MODEL": "alice-sub", "OUROBOROS_MODEL_LIGHT": "alice-sub-light"},
                ("bob", "main"): {"OUROBOROS_MODEL": "bob-main", "OUROBOROS_TIMEZONE": "Asia/Tokyo"},
            },
        )
    )
    try:
        assert [get_main_model(scope) for scope in (alice, bob, alice_sub)] == ["alice-main", "bob-main", "alice-sub"]
        assert get_light_model(alice_sub) == "alice-sub-light"
        assert resolve_effort(alice) == "high"
        assert runtime_time_config(alice).timezone_name == "Asia/Vladivostok"
        assert runtime_time_config(bob).timezone_name == "Asia/Tokyo"
        assert runtime_time_config(alice_sub).timezone_name == load_settings().OUROBOROS_TIMEZONE
    finally:
        reset_user_settings_for_tests(None)
