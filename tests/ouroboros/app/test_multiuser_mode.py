"""The multi-user mode: one declaration, validated at boot, read nowhere else.

The behaviours the mode governs only hold together, so what is pinned here is that they
have one switch rather than several that must agree, that a configuration which cannot
serve several owners is refused before the process serves anything, and that the flag
shapes what the composition root builds instead of being asked again on every path.
"""

from __future__ import annotations

import ast
import pathlib

import pytest

from ouroboros.app.multiuser_validation import (
    MultiuserConfigurationError,
    validate_multiuser_configuration,
)
from ouroboros.storage.settings.store import load_settings
from tests.helpers.shared import TEST_SCOPE

REPO_ROOT = pathlib.Path(__file__).resolve().parents[3]
MODE_SETTING = "OUROBOROS_MULTIUSER"


@pytest.fixture
def multiuser(monkeypatch):
    """Declare the mode on, as a deployment's environment would."""
    from tests.conftest import refresh_test_config

    monkeypatch.setenv(MODE_SETTING, "true")
    refresh_test_config()
    yield
    monkeypatch.delenv(MODE_SETTING, raising=False)
    refresh_test_config()


@pytest.fixture
def postgres_runtime_db(monkeypatch):
    """The runtime database the start-up refusal accepts."""
    from tests.conftest import refresh_test_config

    monkeypatch.setenv("OUROBOROS_RUNTIME_DB_URL", "postgresql+psycopg://ouroboros@localhost:5432/ouroboros")
    refresh_test_config()
    yield
    monkeypatch.delenv("OUROBOROS_RUNTIME_DB_URL", raising=False)
    refresh_test_config()


@pytest.fixture
def shared_object_storage(monkeypatch):
    from tests.conftest import refresh_test_config

    monkeypatch.setenv("OUROBOROS_S3_BUCKET", "ouroboros-shared")
    refresh_test_config()
    yield
    monkeypatch.delenv("OUROBOROS_S3_BUCKET", raising=False)
    refresh_test_config()


def _write_skill(repo_dir: pathlib.Path, name: str, skill_type: str) -> None:
    """One discovered skill of the given type, in the tree the loader walks."""
    folder = repo_dir / name
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "SKILL.md").write_text(
        "---\n"
        f"name: {name}\n"
        f"description: {name} for the multi-user check\n"
        "version: 1.0.0\n"
        f"type: {skill_type}\n"
        "---\n\nbody\n",
        encoding="utf-8",
    )


# --- one flag ---------------------------------------------------------------------


def test_exactly_one_setting_declares_the_mode() -> None:
    """A second setting expressing part of the same fact is a rule about which wins."""
    schema = (REPO_ROOT / "ouroboros" / "storage" / "settings" / "schema.py").read_text(encoding="utf-8")

    assert f"{MODE_SETTING}: bool = False" in schema
    assert "BROWSER_TRUSTED_IDENTITY_HEADERS" not in schema
    assert load_settings().OUROBOROS_MULTIUSER is False


def test_the_removed_flag_is_gone_from_the_tree() -> None:
    """It named one half of the mode; two sources of truth for one fact is the defect."""
    offenders = [
        str(path.relative_to(REPO_ROOT))
        for root in ("ouroboros", "supervisor", "transport", "new_core", "tests", "tests_headless")
        for path in (REPO_ROOT / root).rglob("*.py")
        if path != pathlib.Path(__file__).resolve()
        and "BROWSER_TRUSTED_IDENTITY_HEADERS" in path.read_text(encoding="utf-8")
    ]

    assert offenders == []


# --- composition, not branching ---------------------------------------------------

#: Where a message is handled, a task runs, or a tool executes. The mode may shape what
#: these are given; none of them may ask what world it is in.
_TASK_PATH_ROOTS = ("ouroboros/agent", "ouroboros/tools", "new_core", "supervisor", "transport")
#: The composition root and the boot-time validation are where reading it is the point;
#: task/tool behavior receives the resulting value. ``availability.py`` lives under
#: ``ouroboros/skills`` which is outside ``_TASK_PATH_ROOTS`` and needs no exemption.
_MODE_READERS = (
    "ouroboros/app/multiuser_validation.py",
    "server.py",
)


def test_no_message_task_or_tool_path_reads_the_mode_setting() -> None:
    offenders: list[str] = []
    for root in _TASK_PATH_ROOTS:
        for source_file in (REPO_ROOT / root).rglob("*.py"):
            relative = str(source_file.relative_to(REPO_ROOT))
            if relative in _MODE_READERS:
                continue
            tree = ast.parse(source_file.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.Attribute) and node.attr == MODE_SETTING:
                    offenders.append(f"{relative}:{node.lineno}")
                elif isinstance(node, ast.Constant) and node.value == MODE_SETTING:
                    offenders.append(f"{relative}:{node.lineno}")

    assert offenders == [], f"the mode is read on a path that should have been handed a collaborator: {offenders}"


def test_the_mode_composes_no_tool_that_runs_a_skill() -> None:
    """Withheld by not existing: an uncomposed tool has no descriptor for heal mode to reach."""
    from new_core.tools.providers.builtin import (
        FROZEN_TOOL_MODULES,
        SKILL_EXECUTION_TOOL_MODULES,
        BuiltinToolProvider,
        frozen_tool_modules,
    )

    modules = frozen_tool_modules(skill_execution=False)

    assert set(FROZEN_TOOL_MODULES) - set(modules) == set(SKILL_EXECUTION_TOOL_MODULES)
    provider = BuiltinToolProvider(modules)
    provider.refresh()
    resolved = {descriptor.schema.name for descriptor in provider.descriptors(TEST_SCOPE)}
    assert "skill_exec" not in resolved
    # Reading and listing survive: an instruction skill is delivered by being read.
    assert {"skill_read", "list_skills", "check_skills"} <= resolved


def test_every_tool_is_composed_while_the_mode_is_off() -> None:
    from new_core.tools.providers.builtin import FROZEN_TOOL_MODULES, frozen_tool_modules

    assert frozen_tool_modules(skill_execution=True) == FROZEN_TOOL_MODULES


# --- startup validation -----------------------------------------------------------


def test_a_single_writer_database_is_refused_regardless_of_the_mode(tmp_path, monkeypatch) -> None:
    """The refusal is made before the mode is consulted, so it reads the same either way.

    An absent configuration and one naming another dialect are the same refusal, and the
    mode contributes none of it — a database refusal attributed to the mode would be a
    second place that can disagree with this one.
    """
    from ouroboros.app.multiuser_validation import mode_refusals
    from ouroboros.storage.engine_setup import runtime_engine_urls
    from tests.conftest import refresh_test_config

    messages = []
    for mode in ("false", "true"):
        monkeypatch.setenv(MODE_SETTING, mode)
        refresh_test_config()
        for configured in ([], [f"sqlite:///{tmp_path}/runtime.sqlite3"]):
            with pytest.raises(ValueError) as refusal:
                runtime_engine_urls(configured)
            messages.append(str(refusal.value))

    assert messages[:2] == messages[2:], "the mode changes nothing about this refusal"
    assert all("OUROBOROS_RUNTIME_DB_URL" in message for message in messages)
    assert all("OUROBOROS_RUNTIME_DB_URL_1..N" in message for message in messages)
    assert "PostgreSQL" in messages[1]
    assert not any("runtime database" in refusal for refusal in mode_refusals())


def test_an_owner_minting_ingress_is_refused(tmp_path, monkeypatch, multiuser, postgres_runtime_db) -> None:
    from tests.conftest import refresh_test_config

    monkeypatch.setenv("A2A_ENABLED", "true")
    refresh_test_config()

    with pytest.raises(MultiuserConfigurationError) as refusal:
        validate_multiuser_configuration()

    assert "A2A_ENABLED" in str(refusal.value)


def test_a_tree_of_executable_skills_is_not_a_refusal(
    tmp_path, multiuser, postgres_runtime_db, shared_object_storage
) -> None:
    """What the image ships is not a contradiction the mode can only survive by not starting.

    It used to be one, and a deployment had to mask its whole skills tree to boot —
    which took the instruction skills down with the executable ones. The common surface withholds
    them now, so the start is the honest place to be quiet about them.
    """
    _write_skill(tmp_path / "repo", "runs_a_script", "script")
    _write_skill(tmp_path / "repo", "loads_a_module", "extension")
    _write_skill(tmp_path / "repo", "just_words", "instruction")

    validate_multiuser_configuration()


def test_a_supported_configuration_starts(tmp_path, multiuser, postgres_runtime_db, shared_object_storage) -> None:
    _write_skill(tmp_path / "repo", "just_words", "instruction")

    validate_multiuser_configuration()


def test_the_other_refusals_survive_the_skill_one_leaving(tmp_path, monkeypatch, multiuser) -> None:
    """The set lost a member; the remaining ones are still made, and still all at once."""
    from ouroboros.app.multiuser_validation import mode_refusals
    from tests.conftest import refresh_test_config

    monkeypatch.setenv("A2A_ENABLED", "true")
    refresh_test_config()
    _write_skill(tmp_path / "repo", "runs_a_script", "script")

    refusals = mode_refusals()

    assert any("A2A_ENABLED" in refusal for refusal in refusals)
    assert not any("runs_a_script" in refusal for refusal in refusals)


def test_none_of_the_refusals_applies_while_the_mode_is_off(tmp_path, monkeypatch) -> None:
    """The default is the behaviour that shipped: A2A and every skill type."""
    from tests.conftest import refresh_test_config

    monkeypatch.setenv("A2A_ENABLED", "true")
    refresh_test_config()
    _write_skill(tmp_path / "repo", "runs_a_script", "script")

    validate_multiuser_configuration()


# --- what the mode discovers ------------------------------------------------------


def _catalog(tmp_path: pathlib.Path, distribution_root: pathlib.Path):
    from new_core.skills.catalog import SkillCatalog
    from new_core.skills.providers import DistributionSkillProvider
    from ouroboros.skills.loader import DistributionSkillLoader

    provider = DistributionSkillProvider(
        distribution_root=distribution_root,
        loader=DistributionSkillLoader(load_settings()),
    )
    catalog = SkillCatalog()
    catalog.initialize([provider])
    return catalog


def _skill_surface(tmp_path: pathlib.Path, distribution_root: pathlib.Path):
    from new_core.skills.surface import SkillSurface
    from ouroboros.skills.availability import build_skill_availability

    catalog = _catalog(tmp_path, distribution_root)
    return SkillSurface(
        snapshot=catalog.snapshot(),
        availability=build_skill_availability(load_settings()),
    )


def _discovered(tmp_path) -> set[str]:
    return {skill.name for skill in _skill_surface(tmp_path, tmp_path / "repo").skills()}


def test_only_executable_skills_are_withheld_from_the_mode(tmp_path, multiuser) -> None:
    _write_skill(tmp_path / "repo", "runs_a_script", "script")
    _write_skill(tmp_path / "repo", "loads_a_module", "extension")
    _write_skill(tmp_path / "repo", "just_words", "instruction")
    _write_skill(tmp_path / "repo", "from_the_hub", "skill")

    assert _discovered(tmp_path) == {"just_words", "from_the_hub"}


def test_owner_selection_cannot_restore_executable_skills_in_the_mode(tmp_path, multiuser) -> None:
    from new_core.skills.models import SkillId
    from new_core.skills.surface import SkillSurface
    from ouroboros.skills.availability import build_skill_availability

    for name, skill_type in (
        ("runs_a_script", "script"),
        ("loads_a_module", "extension"),
        ("registers_tools", "tool"),
        ("just_words", "instruction"),
        ("from_the_hub", "future-kind"),
    ):
        _write_skill(tmp_path / "repo", name, skill_type)
    catalog = _catalog(tmp_path, tmp_path / "repo")
    selection = frozenset(
        SkillId("distribution", name)
        for name in ("runs_a_script", "loads_a_module", "registers_tools", "just_words", "from_the_hub")
    )
    surface = SkillSurface(
        snapshot=catalog.snapshot(),
        availability=build_skill_availability(load_settings()),
        agent_name="main",
        task_selection=selection,
    )

    assert {skill.name for skill in surface.skills()} == {"just_words", "from_the_hub"}
    for name in ("runs_a_script", "loads_a_module", "registers_tools"):
        assert surface.get(SkillId("distribution", name)) is None


def test_a_withheld_skill_cannot_be_found_by_name(tmp_path, multiuser) -> None:
    """The answer for a skill the mode withholds is the answer for one that never existed."""
    _write_skill(tmp_path / "repo", "runs_a_script", "script")

    assert _skill_surface(tmp_path, tmp_path / "repo").skills() == ()


def test_a_withheld_skill_reaches_no_prompt_index(tmp_path, multiuser) -> None:
    """The summary is what the prompt's skill index is built from."""
    from new_core.tools.providers import EmptyExtensionRuntime
    from ouroboros.skills.loader import summarize_skills

    # summarize_skills takes a workspace, whose skills tree is the directory below it.
    _write_skill(tmp_path / "repo" / "skills", "runs_a_script", "script")
    _write_skill(tmp_path / "repo" / "skills", "just_words", "instruction")

    summary = summarize_skills(
        _skill_surface(tmp_path, tmp_path / "repo" / "skills").skills(),
        extension_runtime=EmptyExtensionRuntime(),
    )

    assert [row["name"] for row in summary["skills"]] == ["just_words"]


def test_every_skill_type_is_discovered_while_the_mode_is_off(tmp_path) -> None:
    """The single-process profile is untouched: the filter is the mode's, not discovery's."""
    _write_skill(tmp_path / "repo", "runs_a_script", "script")
    _write_skill(tmp_path / "repo", "just_words", "instruction")

    assert _discovered(tmp_path) == {"runs_a_script", "just_words"}


# --- the catalogue stops moving ---------------------------------------------------


def test_a_task_surface_keeps_its_catalogue_snapshot(tmp_path, multiuser) -> None:
    """A later refresh prepares another run; it cannot change the run already in progress."""
    from new_core.skills.surface import SkillSurface
    from ouroboros.skills.availability import build_skill_availability

    _write_skill(tmp_path / "repo", "first", "instruction")
    from new_core.skills.catalog import SkillCatalog
    from new_core.skills.providers import DistributionSkillProvider
    from ouroboros.skills.loader import DistributionSkillLoader

    provider = DistributionSkillProvider(
        distribution_root=tmp_path / "repo",
        loader=DistributionSkillLoader(load_settings()),
    )
    catalog = SkillCatalog()
    catalog.initialize([provider])
    surface = SkillSurface(
        snapshot=catalog.snapshot(),
        availability=build_skill_availability(load_settings()),
    )
    _write_skill(tmp_path / "repo", "enabled_between_tasks", "instruction")

    catalog.initialize([provider])

    assert {skill.name for skill in catalog.snapshot().skills()} == {"first", "enabled_between_tasks"}
    assert {skill.name for skill in surface.skills()} == {"first"}


# --- the shared tool server -------------------------------------------------------


def _mcp_entries(monkeypatch, *urls: tuple[str, str], enabled: bool = True) -> None:
    """The exported MCP list, as a polygon branch would build it."""
    from ouroboros.storage.settings.schema import McpServerEntry

    monkeypatch.setattr(
        "security_config.mcp.mcp_servers",
        [McpServerEntry(id=entry_id, url=url, enabled=enabled) for entry_id, url in urls],
    )


@pytest.fixture
def mcp_enabled(monkeypatch):
    """The kill switch on, which is what makes the tool list worth examining."""
    from tests.conftest import refresh_test_config

    monkeypatch.setenv("MCP_ENABLED", "true")
    refresh_test_config()
    yield
    monkeypatch.delenv("MCP_ENABLED", raising=False)
    refresh_test_config()


def _mcp_refusals(tmp_path) -> list[str]:
    from ouroboros.app.multiuser_validation import mode_refusals

    return [refusal for refusal in mode_refusals() if "MCP" in refusal]


def test_a_tool_server_at_a_loopback_address_is_refused(tmp_path, monkeypatch, multiuser, mcp_enabled) -> None:
    """The child-process form of this went with `stdio`; the address form did not.

    A loopback URL resolves inside each container, so it names a different server on every replica
    or none — and the failure reads as a tool present in one catalogue and absent from the next,
    which says nothing about an address.
    """
    _mcp_entries(monkeypatch, ("local-tools", "http://127.0.0.1:8931/mcp"))

    with pytest.raises(MultiuserConfigurationError) as refusal:
        validate_multiuser_configuration()

    message = str(refusal.value)
    assert "local-tools" in message
    assert "security_config/mcp.py" in message


_INSTANCE_LOCAL_URLS = [
    "http://127.0.0.1:8931/mcp",
    "http://127.9.9.9:8931/mcp",
    "http://localhost:8931/mcp",
    "http://LOCALHOST:8931/mcp",
    "http://[::1]:8931/mcp",
    "http://0.0.0.0:8931/mcp",
    "http://[::]:8931/mcp",
    "http://localhost.:8931/mcp",
    "http://127.0.0.1.:8931/mcp",
    "http://2130706433:8931/mcp",
    "http://0177.0.0.1:8931/mcp",
    "http://127.1:8931/mcp",
]


@pytest.mark.parametrize("url", _INSTANCE_LOCAL_URLS)
def test_every_spelling_of_this_host_is_refused(tmp_path, monkeypatch, multiuser, mcp_enabled, url) -> None:
    """The literal and the name produce one defect, so one spelling checked is none of them."""
    _mcp_entries(monkeypatch, ("local-tools", url))

    assert len(_mcp_refusals(tmp_path)) == 1, url


def test_the_refusal_does_not_carry_the_entrys_credentials_into_the_startup_log(
    tmp_path, monkeypatch, multiuser, mcp_enabled
) -> None:
    """`_validate_url` checks the scheme prefix and nothing else, so userinfo is a valid entry.

    The refusal is raised and logged at start-up, so rendering the entry's own `url` would put a
    password there — the failure `describe_validation_error` avoids one module over. The address
    is what is wrong and what has to change, so the address is what is named.
    """
    _mcp_entries(monkeypatch, ("local-tools", "http://svc:pa55w0rd@127.0.0.1:8931/mcp"))

    refusals = _mcp_refusals(tmp_path)

    assert len(refusals) == 1
    assert "pa55w0rd" not in refusals[0]
    assert "svc" not in refusals[0]
    assert "127.0.0.1:8931" in refusals[0], "the address still has to be named or the refusal is unactionable"


def test_an_address_no_parser_accepts_does_not_take_the_refusal_batch_down(
    tmp_path, monkeypatch, multiuser, mcp_enabled
) -> None:
    """`_validate_url` checks the scheme prefix and nothing else, so `urlsplit` can still reject one.

    `http://[::1/mcp` is an unterminated IPv6 literal: a valid entry that `urlsplit` answers with
    `ValueError`. Raised out of `mode_refusals`'s comprehension it takes the boot down with a
    traceback naming neither the entry nor the setting, and discards every other refusal the same
    boot found — the opposite of listing problems of the same kind together.
    """
    _mcp_entries(
        monkeypatch,
        ("sidecar", "http://[::1/mcp"),
        ("local-tools", "http://127.0.0.1:8931/mcp"),
    )

    refusals = _mcp_refusals(tmp_path)

    assert len(refusals) == 1, "the batch has to survive the entry nothing can parse"
    assert "local-tools" in refusals[0]
    # No instance reaches it, so it is not one server per replica — it is none on any of them, and
    # the manager already names it and skips it when the connection fails.
    assert "sidecar" not in refusals[0]


def test_every_unshareable_tool_server_is_named(tmp_path, monkeypatch, multiuser, mcp_enabled) -> None:
    _mcp_entries(
        monkeypatch,
        ("local-tools", "http://127.0.0.1:8931/mcp"),
        ("shared", "https://mcp.internal/mcp"),
        ("other-local", "http://localhost:9000/sse"),
    )

    refusals = _mcp_refusals(tmp_path)

    assert len(refusals) == 1
    assert "local-tools" in refusals[0]
    assert "other-local" in refusals[0]
    assert "shared" not in refusals[0]


def test_a_tool_server_every_instance_reaches_is_not_refused(tmp_path, monkeypatch, multiuser, mcp_enabled) -> None:
    _mcp_entries(monkeypatch, ("shared", "https://mcp.internal/mcp"), ("events", "https://events.internal/sse"))

    assert _mcp_refusals(tmp_path) == []


def test_a_disabled_entry_is_not_refused(tmp_path, monkeypatch, multiuser, mcp_enabled) -> None:
    """The manager connects to enabled entries only, so a disabled one configures nothing."""
    _mcp_entries(monkeypatch, ("local-tools", "http://127.0.0.1:8931/mcp"), enabled=False)

    assert _mcp_refusals(tmp_path) == []


def test_the_kill_switch_off_leaves_the_tool_list_unexamined(tmp_path, monkeypatch, multiuser) -> None:
    """Nothing will connect to it, so the configuration is not one the mode has to serve."""
    _mcp_entries(monkeypatch, ("local-tools", "http://127.0.0.1:8931/mcp"))

    assert _mcp_refusals(tmp_path) == []


@pytest.fixture
def a2a_egress_enabled(monkeypatch):
    from tests.conftest import refresh_test_config

    monkeypatch.setenv("A2A_EGRESS_ENABLED", "true")
    refresh_test_config()
    yield
    monkeypatch.delenv("A2A_EGRESS_ENABLED", raising=False)
    refresh_test_config()


class _UnexaminedAgents(list):
    def __iter__(self):
        raise AssertionError("the A2A list behind A2A_EGRESS_ENABLED off was examined")


def _a2a_refusals(tmp_path) -> list[str]:
    from ouroboros.app.multiuser_validation import mode_refusals

    return [refusal for refusal in mode_refusals() if "security_config/a2a.py" in refusal]


def _a2a_refusals_once_egress_is_on(tmp_path, monkeypatch, agents) -> list[str]:
    from tests.conftest import refresh_test_config

    monkeypatch.setattr("security_config.a2a.a2a_remote_agents", _UnexaminedAgents())
    assert _a2a_refusals(tmp_path) == []
    monkeypatch.setenv("A2A_EGRESS_ENABLED", "true")
    refresh_test_config()
    monkeypatch.setattr("security_config.a2a.a2a_remote_agents", agents)
    return _a2a_refusals(tmp_path)


def test_a2a_egress_at_an_instance_local_address_is_refused(tmp_path, monkeypatch, multiuser) -> None:
    from ouroboros.storage.settings.schema import A2aRemoteAgentEntry

    agents = [
        A2aRemoteAgentEntry(id="loopback-agent", url="http://svc:pa55w0rd@127.0.0.1:9000/a2a?token=q-secret"),
        A2aRemoteAgentEntry(id="named-agent", url="http://localhost:9001/a2a"),
        A2aRemoteAgentEntry(id="unspecified-agent", url="http://0.0.0.0:9002/a2a"),
        A2aRemoteAgentEntry(id="shared-agent", url="https://agents.internal/a2a"),
        A2aRemoteAgentEntry(id="paused-agent", url="http://127.0.0.1:9003/a2a", enabled=False),
    ]

    (refusal,) = _a2a_refusals_once_egress_is_on(tmp_path, monkeypatch, agents)

    assert "loopback-agent (127.0.0.1:9000)" in refusal
    assert "named-agent (localhost:9001)" in refusal
    assert "unspecified-agent (0.0.0.0:9002)" in refusal
    assert "shared-agent" not in refusal
    assert "paused-agent" not in refusal
    assert "pa55w0rd" not in refusal
    assert "svc" not in refusal
    assert "q-secret" not in refusal
    assert "A2A_EGRESS_ENABLED=false" in refusal


def test_a2a_egress_forwarding_an_undeclared_name_is_refused(tmp_path, monkeypatch, multiuser) -> None:
    from ouroboros.storage.settings.schema import A2aRemoteAgentEntry, UserHeadersAuth, UserTokenAuth

    agents = [
        A2aRemoteAgentEntry(
            id="hr", url="https://hr.internal/a2a", auth=UserHeadersAuth(headers=["X-Sudir-Token", "X-Other"])
        ),
        A2aRemoteAgentEntry(
            id="paused", url="https://paused.internal/a2a", enabled=False, auth=UserHeadersAuth(headers=["X-Paused"])
        ),
        A2aRemoteAgentEntry(
            id="billing",
            url="https://billing.internal/a2a",
            auth=UserTokenAuth(auth_header="Authorization", auth_token="Bearer a2a-static-secret"),
        ),
    ]

    (refusal,) = _a2a_refusals_once_egress_is_on(tmp_path, monkeypatch, agents)

    assert "hr header:x-other" in refusal
    assert "x-sudir-token" not in refusal
    assert "paused" not in refusal
    assert "billing" not in refusal
    assert "https://" not in refusal
    assert ".internal" not in refusal
    assert "a2a-static-secret" not in refusal


def test_a2a_egress_at_a_shared_address_with_declared_names_starts(
    tmp_path, monkeypatch, multiuser, postgres_runtime_db, shared_object_storage, a2a_egress_enabled
) -> None:
    from ouroboros.app.multiuser_validation import mode_refusals
    from ouroboros.storage.settings.schema import A2aRemoteAgentEntry, MTLSAuth, NoAuth, UserHeadersAuth, UserTokenAuth

    monkeypatch.setattr(
        "security_config.a2a.a2a_remote_agents",
        [
            A2aRemoteAgentEntry(id="open-agent", url="https://open.internal/a2a", auth=NoAuth()),
            A2aRemoteAgentEntry(
                id="portal-agent",
                url="https://portal.internal/a2a",
                auth=UserHeadersAuth(headers=["X-Sudir-Token"], cookies=["sudir_session"]),
            ),
            A2aRemoteAgentEntry(
                id="token-agent", url="https://token.internal/a2a", auth=UserTokenAuth(auth_header="Authorization")
            ),
            A2aRemoteAgentEntry(id="gateway-agent", url="http://egress-gateway.internal/a2a", auth=MTLSAuth()),
        ],
    )

    validate_multiuser_configuration()

    assert mode_refusals() == []


# --- the ingress refusal set is total, not a list of noticed cases -----------------


@pytest.fixture
def dialogs_configured(monkeypatch):
    """Configured, the way the composition root reads it: an endpoint and a bot token."""
    from tests.conftest import refresh_test_config

    monkeypatch.setenv("DIALOGS_GRPC_ENDPOINT", "dialogs.example:443")
    monkeypatch.setenv("DIALOGS_BOT_TOKEN", "token-1")
    refresh_test_config()
    yield
    for name in ("DIALOGS_GRPC_ENDPOINT", "DIALOGS_BOT_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    refresh_test_config()


def test_a_configured_dialogs_ingress_is_refused(tmp_path, multiuser, postgres_runtime_db, dialogs_configured) -> None:
    """It builds the scope from configuration alone, so no setting of it attributes a sender."""
    with pytest.raises(MultiuserConfigurationError) as refusal:
        validate_multiuser_configuration()

    message = str(refusal.value)
    assert "Dialogs" in message
    assert "DIALOGS_GRPC_ENDPOINT" in message
    assert "configured owner" in message


def test_an_unconfigured_dialogs_ingress_does_not_refuse(
    tmp_path, multiuser, postgres_runtime_db, monkeypatch, shared_object_storage
) -> None:
    from tests.conftest import refresh_test_config

    monkeypatch.setenv("DIALOGS_GRPC_ENDPOINT", "dialogs.example:443")
    monkeypatch.delenv("DIALOGS_BOT_TOKEN", raising=False)
    refresh_test_config()

    validate_multiuser_configuration()


def test_dialogs_is_untouched_while_the_mode_is_off(tmp_path, dialogs_configured) -> None:
    """The refusal is bound to the mode; a single-owner deployment is unaffected."""
    validate_multiuser_configuration()


def test_every_owner_minting_ingress_is_named_in_one_message(
    tmp_path, monkeypatch, multiuser, postgres_runtime_db, dialogs_configured
) -> None:
    from tests.conftest import refresh_test_config

    monkeypatch.setenv("A2A_ENABLED", "true")
    refresh_test_config()

    with pytest.raises(MultiuserConfigurationError) as refusal:
        validate_multiuser_configuration()

    message = str(refusal.value)
    assert "A2A_ENABLED" in message
    assert "DIALOGS_GRPC_ENDPOINT" in message


def test_the_membership_of_the_owner_minting_set_is_checked_not_assumed() -> None:
    """A single named refusal reads as a complete rule and is not one.

    The property is "builds a scope from the instance's configuration". Every transport
    that has it must be accounted for in one of three ways, and a new one that is in none
    of them fails here rather than widening the hole quietly.
    """
    import ouroboros.app.multiuser_validation as validation

    #: Refused by the mode, because a person sent the message and it names the wrong one.
    refused = {
        "transport/dialogs/state.py": "DIALOGS_GRPC_ENDPOINT",
        "transport/a2a/executor.py": "A2A_ENABLED",
    }
    #: Exempt: acts on the instance's own behalf and has no author in its input, which it
    #: says where it builds the scope. Refusing it would remove a working path to fix a
    #: defect it does not have.
    exempt = {"transport/skill_bridge.py"}
    #: Not refused because the mode replaces the resolver: this path is the single-owner
    #: one, and with the mode on the owner comes from the trusted header instead.
    mode_switched = {"transport/browser/identity.py"}

    #: Work the instance does for itself, which names its identity by calling for one
    #: explicitly. `service_identity(job=…)` *is* the declaration that there is no author —
    #: a cursor or a task store — so these are outside the ingress rule by
    #: construction rather than by exemption.
    instance_work = {
        "transport/a2a/task_store.py",
    }

    # Both ways the tree has of naming the instance's configured owner, not just the
    # helper: a transport reaching for the constant gains the same property and would
    # escape a grep for one call.
    mints = ("configured_owner_id()", "SETTINGS_OWNER_ID")
    minting = {
        str(path.relative_to(REPO_ROOT))
        for path in (REPO_ROOT / "transport").rglob("*.py")
        if any(token in path.read_text(encoding="utf-8") for token in mints)
    }
    service = {
        str(path.relative_to(REPO_ROOT))
        for path in (REPO_ROOT / "transport").rglob("*.py")
        if "service_identity(" in path.read_text(encoding="utf-8")
    }
    assert service - minting == instance_work, (
        "a transport started or stopped naming an author-less service identity; if it now does so "
        "for a message a person sent, it belongs in the refused set instead"
    )

    unaccounted = minting - set(refused) - exempt - mode_switched - instance_work
    assert not unaccounted, (
        f"{sorted(unaccounted)} build a scope from the instance's configuration with no refusal. "
        "An ingress with a sender to attribute needs one; a producer with no author is exempt and "
        "must say so where it builds the scope."
    )

    validator = pathlib.Path(validation.__file__).read_text(encoding="utf-8")
    for path, setting in refused.items():
        assert setting in validator, f"{path} mints the configured owner and has no refusal"

    bridge = (REPO_ROOT / "transport/skill_bridge.py").read_text(encoding="utf-8")
    assert "not of the person" in bridge, "the exempt case must keep saying it acts for the instance"


# --- object storage the instances can actually share -------------------------------


def test_a_missing_bucket_is_refused_regardless_of_the_mode(tmp_path, monkeypatch, postgres_runtime_db) -> None:
    from ouroboros.app.multiuser_validation import mode_refusals
    from ouroboros.storage.blob.guards import ProductionStorageConfigError, validate_production_storage
    from tests.conftest import refresh_test_config

    monkeypatch.delenv("OUROBOROS_S3_BUCKET", raising=False)
    messages = []
    for mode in ("false", "true"):
        monkeypatch.setenv(MODE_SETTING, mode)
        refresh_test_config()
        with pytest.raises(ProductionStorageConfigError) as refusal:
            validate_production_storage()
        messages.append(str(refusal.value))

    assert messages[0] == messages[1], "the mode changes nothing about this refusal"
    assert "OUROBOROS_S3_BUCKET" in messages[0]
    assert not any("OUROBOROS_S3_BUCKET" in refusal for refusal in mode_refusals())
    validate_multiuser_configuration()


# --- a refusal describes the configuration that will actually run ------------------


@pytest.fixture
def numbered_runtime_db(monkeypatch):
    """The documented failover form, which the runtime prefers over the legacy key."""
    from tests.conftest import refresh_test_config

    monkeypatch.delenv("OUROBOROS_RUNTIME_DB_URL", raising=False)
    monkeypatch.setenv("OUROBOROS_RUNTIME_DB_URL_1", "postgresql+psycopg://ouroboros@db-1:5432/ouroboros")
    monkeypatch.setenv("OUROBOROS_RUNTIME_DB_URL_2", "postgresql+psycopg://ouroboros@db-2:5432/ouroboros")
    refresh_test_config()
    yield
    for name in ("OUROBOROS_RUNTIME_DB_URL_1", "OUROBOROS_RUNTIME_DB_URL_2"):
        monkeypatch.delenv(name, raising=False)
    refresh_test_config()


def test_a_fleet_configured_the_documented_failover_way_starts(
    tmp_path, multiuser, numbered_runtime_db, shared_object_storage
) -> None:
    """The check read one setting key while the runtime prefers the numbered ones.

    So a fleet configured the documented way was refused, naming a database nobody set —
    the configuration a 2-3 instance fleet actually wants.
    """
    validate_multiuser_configuration()


def test_a_configuration_the_resolver_rejects_is_a_refusal_not_a_traceback(
    tmp_path, monkeypatch, multiuser, shared_object_storage
) -> None:
    """A configuration contradiction is what the mode exists to name.

    The shared resolution rejects nothing today, so the rejection is injected: the branch
    is live again the moment any rejection returns, and what it must not do is let the
    resolver's `ValueError` leave startup untranslated.
    """
    import ouroboros.storage.db_url as db_url

    def _reject(_settings):
        raise ValueError("two URLs name two databases")

    monkeypatch.setattr(db_url, "collect_runtime_db_urls", _reject)

    with pytest.raises(MultiuserConfigurationError) as refusal:
        validate_multiuser_configuration()

    assert "two URLs name two databases" in str(refusal.value)
    assert "the runtime database configuration is rejected" in str(refusal.value)


# --- nothing durable an owner produced stays on one instance's disk ----------------


def test_a_generic_data_write_cannot_land_on_the_instance_disk(tmp_path, multiuser, shared_object_storage) -> None:
    from ouroboros.storage.blob.keys import build_scope_prefix
    from ouroboros.tools.core import _data_write
    from tests.helpers.shared import TEST_SCOPE, make_tool_ctx

    drive_root = tmp_path / "data"
    drive_root.mkdir()

    with pytest.raises(ValueError):
        build_scope_prefix(TEST_SCOPE, "logs")

    answer = _data_write(
        make_tool_ctx(repo_dir=tmp_path / "repo", drive_root=drive_root),
        "logs/note.md",
        "a note the next instance would never see",
    )

    assert "DATA_WRITE_BLOCKED" in answer
    assert "logs" in answer
    assert not (drive_root / "logs").exists()


def test_a_symlink_cannot_walk_a_write_into_a_scope_its_name_does_not_say(tmp_path) -> None:
    from ouroboros.tools.core import _data_write
    from tests.helpers.shared import make_tool_ctx

    drive_root = tmp_path / "data"
    (drive_root / "logs").mkdir(parents=True)
    (drive_root / "memory").mkdir()
    (drive_root / "uploads").mkdir()
    (drive_root / "uploads" / "sneak").symlink_to(drive_root / "logs", target_is_directory=True)
    (drive_root / "uploads" / "shortcut").symlink_to(drive_root / "memory", target_is_directory=True)
    ctx = make_tool_ctx(repo_dir=tmp_path / "repo", drive_root=drive_root)

    through_logs = _data_write(ctx, "uploads/sneak/leak.md", "a note under somebody else's name")
    through_memory = _data_write(ctx, "uploads/shortcut/probe_journal.jsonl", '{"leaked": true}\n')

    assert "DATA_WRITE_BLOCKED" in through_logs and "logs" in through_logs
    assert "DATA_WRITE_BLOCKED" in through_memory and "memory/probe_journal.jsonl" in through_memory
    assert list((drive_root / "logs").iterdir()) == []
    assert list((drive_root / "memory").iterdir()) == []


def test_the_reported_storage_backend_names_no_local_tier() -> None:
    """An operator reads this name to decide what to provision and what to back up."""
    from ouroboros.storage.blob.guards import storage_status_summary

    assert storage_status_summary()["storage_backend"] == "s3"


def test_an_owners_restart_leaves_no_marker_on_the_instance_disk(tmp_path) -> None:
    """`/restart` used to write two flag files under `state/` and read neither, ever.

    Written straight to the filesystem rather than through the provider, so the shared
    backend never saw them; a fleet wrote one copy per instance and nothing anywhere read
    one back. Worse, a failure to write them cancelled the restart the owner asked for.
    """
    from types import SimpleNamespace

    from supervisor.commands import handle_slash_command
    from tests.helpers.shared import TEST_SCOPE

    drive_root = tmp_path / "drive"
    drive_root.mkdir()
    sent: list[str] = []
    restarted: list[int] = []
    ctx = SimpleNamespace(
        drive_root=drive_root,
        DRIVE_ROOT=drive_root,
        tasks_repo=SimpleNamespace(list_tasks=lambda **_kwargs: []),
        send_with_budget=lambda text, **_kwargs: sent.append(text),
        safe_restart=lambda **_kwargs: (True, "restart requested"),
        request_restart=lambda: restarted.append(1),
    )

    handle_slash_command("/restart", ctx=ctx, scope=TEST_SCOPE, source="browser", transport={})

    assert restarted == [1], "the restart itself is unchanged"
    assert not any("cancelled" in text for text in sent)
    assert sorted(str(path.relative_to(drive_root)) for path in drive_root.rglob("*")) == []


def test_an_owners_restart_is_refused_while_the_instance_serves_several_owners() -> None:
    """One owner's `/restart` would stop every owner's work; the hook the mode composes says no."""
    from types import SimpleNamespace

    import server
    from supervisor.commands import handle_slash_command

    sent: list[str] = []
    restarted: list[int] = []
    consulted: list[dict] = []
    ctx = SimpleNamespace(
        store=SimpleNamespace(list_tasks=lambda **kwargs: consulted.append(kwargs) or []),
        send_with_budget=lambda text, **_kwargs: sent.append(text),
        safe_restart=server.refuse_multiuser_restart,
        request_restart=lambda: restarted.append(1),
    )

    handle_slash_command("/restart", ctx=ctx, scope=TEST_SCOPE, source="browser", transport={})

    assert len(sent) == 1 and "cancelled" in sent[0] and "several owners" in sent[0], sent
    assert restarted == []
    assert consulted == [], "the refusal returns before any owner's records are read"


def test_the_refusal_set_is_declared_once_and_any_root_can_run_it(tmp_path, monkeypatch, multiuser) -> None:
    """The browser transport is its own process now.

    A refusal that lives inside one composition root is a refusal the other process does
    not make — and it would then serve uploads under a configuration the mode refuses.
    """
    from ouroboros.app.multiuser_validation import mode_refusals
    from tests.conftest import refresh_test_config

    monkeypatch.setenv("A2A_ENABLED", "true")
    refresh_test_config()
    # A second root calls the set directly, with no check of its own.
    refusals = mode_refusals()

    assert any("A2A_ENABLED" in refusal for refusal in refusals)
    with pytest.raises(MultiuserConfigurationError):
        validate_multiuser_configuration()

    roots = [
        path for path in (REPO_ROOT / "ouroboros").rglob("*.py") if "_a2a_refusal" in path.read_text(encoding="utf-8")
    ]
    assert [path.name for path in roots] == ["multiuser_validation.py"], "the refusals have one home"
