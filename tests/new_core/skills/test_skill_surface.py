from __future__ import annotations

import pathlib
from collections.abc import Sequence

from new_core.skills.catalog import SkillCatalog
from new_core.skills.interfaces import ISkillProvider
from new_core.skills.manifest import SkillManifest
from new_core.skills.models import LoadedSkill, SkillBlockReason, SkillId
from new_core.skills.permissions import CatalogSkillPermissionChecker
from new_core.skills.surface import SkillSurface
from tests.helpers.shared import make_skill_availability


def _skill(name: str, *, source_id: str = "distribution", skill_type: str = "instruction") -> LoadedSkill:
    return LoadedSkill(
        skill_id=SkillId(source_id, name),
        name=name,
        skill_dir=pathlib.Path("/skills") / name,
        manifest=SkillManifest(
            name=name,
            description=name,
            version="1",
            type=skill_type,
            raw_extra={"metadata": {"openclaw": {"skillKey": f"key-{name}"}}},
        ),
        content_hash=f"hash-{name}",
        eligible=True,
    )


class _Provider(ISkillProvider):
    def __init__(self, source_id: str, values: Sequence[LoadedSkill]) -> None:
        self._source_id = source_id
        self._skills = tuple(values)

    @property
    def source_id(self) -> str:
        return self._source_id

    def refresh(self) -> None:
        return

    def skills(self) -> Sequence[LoadedSkill]:
        return self._skills

    def get(self, local_id: str) -> LoadedSkill | None:
        return next((skill for skill in self._skills if skill.skill_id.local_id == local_id), None)


def _surface(skills: Sequence[LoadedSkill], settings: dict) -> tuple[SkillCatalog, SkillSurface]:
    catalog = SkillCatalog()
    catalog.initialize([_Provider("distribution", skills)])
    availability = make_skill_availability(
        defaults=settings.get("agents.defaults.skills"),
        agents=settings.get("agents.list"),
    )
    return catalog, SkillSurface(snapshot=catalog.snapshot(), availability=availability)


def test_surface_without_user_policy_matches_catalog() -> None:
    alpha = _skill("alpha")
    catalog, surface = _surface([alpha], {"agents.defaults.skills": None, "agents.list": []})

    assert surface.skills() == catalog.snapshot().skills()
    assert surface.get(alpha.skill_id) == alpha
    assert surface.get("alpha") is None


def test_admitted_ids_require_task_selection_and_eligible_agent_records() -> None:
    allowed = _skill("allowed")
    blocked = _skill("blocked")
    blocked.eligible = False
    unselected = _skill("unselected")
    unselected.selected = False
    agent_blocked = _skill("agent-blocked")
    executable = _skill("executable", skill_type="extension")
    values = [allowed, blocked, unselected, agent_blocked, executable]
    catalog, deployment = _surface(values, {})
    selected = SkillSurface(
        snapshot=catalog.snapshot(),
        availability=make_skill_availability(
            executable_skills=False,
            agents=[{"name": "main", "skills": [str(skill.skill_id) for skill in values if skill != agent_blocked]}],
        ),
        agent_name="main",
        task_selection=frozenset(skill.skill_id for skill in values),
    )
    empty = SkillSurface(
        snapshot=catalog.snapshot(), availability=make_skill_availability(), task_selection=frozenset()
    )

    assert selected.admitted_ids() == frozenset({allowed.skill_id})
    assert deployment.admitted_ids() == empty.admitted_ids() == frozenset()


def test_surface_preserves_default_agent_and_unknown_agent_allowlists() -> None:
    alpha = _skill("alpha")
    beta = _skill("beta")
    catalog, surface = _surface(
        [alpha, beta],
        {
            "agents.defaults.skills": ["distribution:alpha"],
            "agents.list": [{"name": "beta-agent", "skills": ["distribution:beta"]}],
        },
    )

    default_rows = {skill.name: skill for skill in surface.skills()}
    agent_rows = {skill.name: skill for skill in surface.skills(agent_name="beta-agent")}
    unknown_rows = {skill.name: skill for skill in surface.skills(agent_name="unknown")}

    assert default_rows["alpha"].eligible is True
    assert default_rows["beta"].blocked_reasons == ["agent allowlist blocks this skill"]
    assert agent_rows["alpha"].blocked_reasons == ["agent allowlist blocks this skill"]
    assert agent_rows["beta"].eligible is True
    assert unknown_rows["alpha"].eligible is True
    assert unknown_rows["beta"].eligible is False
    assert catalog.snapshot().get(beta.skill_id) == beta
    assert beta.eligible is True
    assert beta.blocked_reasons == []


def test_run_bound_surface_applies_its_agent_and_cannot_be_widened_by_a_tool_argument() -> None:
    alpha = _skill("alpha")
    beta = _skill("beta")
    catalog, _surface_unused = _surface(
        [alpha, beta],
        {
            "agents.defaults.skills": ["distribution:alpha"],
            "agents.list": [{"name": "beta-agent", "skills": ["distribution:beta"]}],
        },
    )
    surface = SkillSurface(
        snapshot=catalog.snapshot(),
        availability=make_skill_availability(
            defaults=["distribution:alpha"],
            agents=[{"name": "beta-agent", "skills": ["distribution:beta"]}],
        ),
        agent_name="beta-agent",
    )

    assert surface.get(alpha.skill_id).eligible is False
    assert surface.get(beta.skill_id).eligible is True
    assert surface.get(alpha.skill_id, agent_name="unknown").eligible is False
    assert surface.get(beta.skill_id, agent_name="unknown").eligible is True


def test_task_selection_distinguishes_unbound_deny_all_and_an_allowed_subset() -> None:
    alpha = _skill("alpha")
    beta = _skill("beta")
    catalog, deployment = _surface([alpha, beta], {"agents.defaults.skills": None, "agents.list": []})
    denied = SkillSurface(
        snapshot=catalog.snapshot(),
        availability=make_skill_availability(),
        agent_name="main",
        task_selection=frozenset(),
    )
    selected = SkillSurface(
        snapshot=catalog.snapshot(),
        availability=make_skill_availability(),
        agent_name="main",
        task_selection=frozenset({alpha.skill_id}),
    )

    assert {skill.skill_id for skill in deployment.skills()} == {alpha.skill_id, beta.skill_id}
    assert denied.skills() == ()
    assert denied.get(alpha.skill_id) is None
    assert selected.skills() == (alpha,)
    assert selected.get(alpha.skill_id) == alpha
    assert selected.get(beta.skill_id) is None


def test_task_selections_isolate_owners_over_one_snapshot() -> None:
    alpha = _skill("alpha")
    beta = _skill("beta")
    catalog, _ = _surface([alpha, beta], {"agents.defaults.skills": None, "agents.list": []})
    first = SkillSurface(
        snapshot=catalog.snapshot(),
        availability=make_skill_availability(),
        agent_name="main",
        task_selection=frozenset({alpha.skill_id}),
    )
    second = SkillSurface(
        snapshot=catalog.snapshot(),
        availability=make_skill_availability(),
        agent_name="main",
        task_selection=frozenset({beta.skill_id}),
    )

    assert first.skills() == (alpha,)
    assert second.skills() == (beta,)
    assert catalog.snapshot().skills() == (alpha, beta)


def test_task_selection_only_narrows_deployment_policy_and_executable_cutoff() -> None:
    alpha = _skill("alpha")
    blocked = _skill("blocked")
    script = _skill("script", skill_type="script")
    catalog = SkillCatalog()
    catalog.initialize([_Provider("distribution", [alpha, blocked, script])])
    surface = SkillSurface(
        snapshot=catalog.snapshot(),
        availability=make_skill_availability(
            defaults=["distribution:alpha", "distribution:script"],
            executable_skills=False,
        ),
        agent_name="main",
        task_selection=frozenset({alpha.skill_id, blocked.skill_id, script.skill_id}),
    )

    rows = surface.skills()

    assert [skill.skill_id for skill in rows] == [alpha.skill_id, blocked.skill_id]
    assert rows[0].eligible is True
    assert rows[1].eligible is False
    assert rows[1].blocked_reasons == ["agent allowlist blocks this skill"]
    assert surface.get(script.skill_id) is None
    assert surface.deployment_get(script.skill_id) is None


def test_surface_allowlist_selects_one_of_two_same_named_source_qualified_skills() -> None:
    distribution = _skill("calendar", source_id="distribution")
    openclaw = _skill("calendar", source_id="openclaw")
    catalog = SkillCatalog()
    catalog.initialize(
        [
            _Provider("distribution", [distribution]),
            _Provider("openclaw", [openclaw]),
        ]
    )
    surface = SkillSurface(
        snapshot=catalog.snapshot(),
        availability=make_skill_availability(defaults=["openclaw:calendar"]),
    )

    rows = {str(skill.skill_id): skill for skill in surface.skills()}

    assert rows["distribution:calendar"].eligible is False
    assert rows["openclaw:calendar"].eligible is True
    assert surface.get(openclaw.skill_id) == rows["openclaw:calendar"]


def test_surface_get_and_skills_return_the_same_projected_record() -> None:
    alpha = _skill("alpha")
    _catalog, surface = _surface([alpha], {"agents.defaults.skills": [], "agents.list": []})

    listed = surface.skills()[0]
    resolved = surface.get(alpha.skill_id)

    assert resolved == listed
    assert resolved is not alpha
    assert resolved is not None
    assert resolved.eligible is False
    assert resolved.blocked_reasons == ["agent allowlist blocks this skill"]


def test_deployment_projection_includes_a_skill_allowed_to_any_configured_agent() -> None:
    alpha = _skill("alpha")
    beta = _skill("beta")
    blocked = _skill("blocked")
    _catalog, surface = _surface(
        [alpha, beta, blocked],
        {
            "agents.defaults.skills": ["distribution:alpha"],
            "agents.list": [{"name": "beta-agent", "skills": ["distribution:beta"]}],
        },
    )

    rows = {skill.name: skill for skill in surface.deployment_skills()}

    assert rows["alpha"].eligible is True
    assert rows["beta"].eligible is True
    assert rows["blocked"].eligible is False
    assert surface.deployment_get(beta.skill_id) == rows["beta"]


def test_deployment_projection_uses_the_first_duplicate_agent_entry() -> None:
    alpha = _skill("alpha")
    _catalog, surface = _surface(
        [alpha],
        {
            "agents.defaults.skills": [],
            "agents.list": [
                {"name": "worker", "skills": []},
                {"name": "worker", "skills": ["distribution:alpha"]},
            ],
        },
    )

    assert surface.get(alpha.skill_id, agent_name="worker").eligible is False
    assert surface.deployment_get(alpha.skill_id).eligible is False


def test_catalog_permission_checker_returns_typed_refusals(tmp_path: pathlib.Path) -> None:
    alpha = _skill("alpha")
    alpha.distribution_owned = True
    alpha.trusted_permissions = ("inject_chat",)
    _catalog, _unused_surface = _surface([alpha], {"agents.defaults.skills": None, "agents.list": []})
    checker = CatalogSkillPermissionChecker(
        skill_surface=SkillSurface(snapshot=_catalog.snapshot(), availability=make_skill_availability()),
    )

    assert (
        checker.refusal(SkillId("distribution", "missing"), "inject_chat")
        == "skill 'distribution:missing' is not installed"
    )
    assert checker.refusal(alpha.skill_id, "inject_chat") is None
    assert checker.refusal(alpha.skill_id, "send_message") == (
        "skill 'alpha' does not declare trusted capability 'send_message'"
    )


def test_catalog_permission_checker_refuses_an_ineligible_skill(
    tmp_path: pathlib.Path,
) -> None:
    alpha = _skill("alpha")
    alpha.distribution_owned = True
    alpha.eligible = False
    alpha.blockers = [SkillBlockReason("invalid_package", "provider policy blocks this skill")]
    alpha.trusted_permissions = ("inject_chat",)
    catalog, surface = _surface([alpha], {"agents.defaults.skills": None, "agents.list": []})
    checker = CatalogSkillPermissionChecker(skill_surface=surface)

    assert catalog.snapshot().get(alpha.skill_id) == alpha
    assert checker.refusal(alpha.skill_id, "inject_chat") == (
        "skill 'alpha' is not eligible: provider policy blocks this skill"
    )


def test_catalog_permission_checker_applies_the_surface_allowlist(
    tmp_path: pathlib.Path,
) -> None:
    alpha = _skill("alpha")
    alpha.distribution_owned = True
    alpha.trusted_permissions = ("inject_chat",)
    _catalog, surface = _surface([alpha], {"agents.defaults.skills": [], "agents.list": []})
    checker = CatalogSkillPermissionChecker(skill_surface=surface)

    assert checker.refusal(alpha.skill_id, "inject_chat") == (
        "skill 'alpha' is not eligible: agent allowlist blocks this skill"
    )


def test_catalog_permission_checker_accepts_a_skill_allowed_to_a_configured_agent(tmp_path: pathlib.Path) -> None:
    alpha = _skill("alpha")
    alpha.distribution_owned = True
    alpha.trusted_permissions = ("inject_chat",)
    _catalog, surface = _surface(
        [alpha],
        {
            "agents.defaults.skills": [],
            "agents.list": [{"name": "worker", "skills": ["distribution:alpha"]}],
        },
    )
    checker = CatalogSkillPermissionChecker(skill_surface=surface)

    assert checker.refusal(alpha.skill_id, "inject_chat") is None


def _mode_surface(skills: Sequence[LoadedSkill], *, trusted: Sequence[str], selection=None) -> SkillSurface:
    """A deployment surface under the multi-user ceiling with a named trusted list."""
    catalog = SkillCatalog()
    catalog.initialize([_Provider("distribution", skills)])
    return SkillSurface(
        snapshot=catalog.snapshot(),
        availability=make_skill_availability(executable_skills=False, trusted_executable_skills=trusted),
        task_selection=selection,
    )


def test_a_named_distribution_extension_is_visible_under_the_ceiling_and_nothing_else_executable_is() -> None:
    engine = _skill("engine", skill_type="extension")
    other = _skill("other", skill_type="extension")
    runner = _skill("runner", skill_type="script")
    doc = _skill("doc")
    surface = _mode_surface([engine, other, runner, doc], trusted=["distribution:engine"])

    assert {skill.skill_id for skill in surface.deployment_skills()} == {engine.skill_id, doc.skill_id}
    assert surface.deployment_get(engine.skill_id) is not None
    assert surface.deployment_get(other.skill_id) is None
    assert surface.deployment_get(runner.skill_id) is None


def test_the_trusted_list_is_empty_by_default_and_the_ceiling_hides_every_executable_skill() -> None:
    engine = _skill("engine", skill_type="extension")
    catalog = SkillCatalog()
    catalog.initialize([_Provider("distribution", [engine])])
    surface = SkillSurface(snapshot=catalog.snapshot(), availability=make_skill_availability(executable_skills=False))

    assert surface.deployment_skills() == ()
    assert surface.deployment_get(engine.skill_id) is None


def test_a_trusted_extension_reaches_a_task_only_when_the_task_selection_names_it() -> None:
    engine = _skill("engine", skill_type="extension")
    doc = _skill("doc")
    unselected = _mode_surface([engine, doc], trusted=["distribution:engine"], selection=frozenset({doc.skill_id}))
    selected = _mode_surface(
        [engine, doc], trusted=["distribution:engine"], selection=frozenset({engine.skill_id, doc.skill_id})
    )

    assert unselected.admitted_ids() == frozenset({doc.skill_id})
    assert selected.admitted_ids() == frozenset({engine.skill_id, doc.skill_id})
