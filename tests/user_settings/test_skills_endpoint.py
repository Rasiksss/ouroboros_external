"""Catalog reads share runtime policy and owner selection without changing either."""

import asyncio
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi import FastAPI
from starlette.testclient import TestClient

import config_registry.user_settings.api.http.endpoint as endpoint
from config_registry.user_settings.reader import UserSettings
from infra.cache.null import AsyncNullCache, NullCache
from new_core.skills.catalog import SkillCatalog
from new_core.skills.providers.distribution import DistributionSkillProvider
from new_core.skills.surface import SkillAvailability, SkillSurface
from ouroboros.skills import loader as skill_loader
from ouroboros.skills.loader import DistributionSkillLoader
from ouroboros.skills.presentation import load_skill_presentations
from ouroboros.storage.settings.schema import AppSettings
from tests.helpers.agent_registry import FakeAgentRegistry
from tests.helpers.parametrize import parametrize_from_dict
from tests.helpers.user_settings import NoUserSettingsWrites, StubAsyncUserSettingsStore, StubUserSettingsStore


def _catalog_app(
    tmp_path, *, multiuser=True, selections=None, extra_packages=(), availability=None, surface_type=SkillSurface
):
    settings = AppSettings.model_validate(
        {
            "OUROBOROS_MULTIUSER": multiuser,
            "OUROBOROS_DISABLED_SKILLS": "distribution:hidden",
        }
    )
    for name, kind in (
        ("alpha", "instruction"),
        ("beta", "instruction"),
        ("blocked", "instruction"),
        ("hidden", "instruction"),
        ("script", "script"),
        *extra_packages,
    ):
        package = tmp_path / "skills" / name
        package.mkdir(parents=True, exist_ok=True)
        (package / "SKILL.md").write_text(
            f"---\nname: {name}\ndescription: {name} description\ntype: {kind}\nversion: 1.0\n---\nInstructions\n"
        )
    catalog = SkillCatalog()
    catalog.initialize(
        [
            DistributionSkillProvider(
                distribution_root=tmp_path / "skills",
                loader=DistributionSkillLoader(settings),
            )
        ]
    )
    registry = FakeAgentRegistry()
    for owner in ("mine", "other"):
        asyncio.run(registry.get_or_create_default_agent(owner))
    app = FastAPI()
    app.include_router(endpoint.router, prefix="/api/owner-settings")
    app.state.skill_presentations = load_skill_presentations(catalog.snapshot())
    app.state.skill_surface = surface_type(
        snapshot=catalog.snapshot(),
        availability=availability
        or SkillAvailability.from_values(
            defaults=["distribution:alpha", "distribution:beta", "distribution:script"],
            agents=[],
            executable_skills=not multiuser,
        ),
    )
    app.dependency_overrides[endpoint.get_registry] = lambda: registry
    app.dependency_overrides[endpoint.get_settings] = lambda: UserSettings(
        settings,
        StubUserSettingsStore(),
        StubAsyncUserSettingsStore(by_pair=selections or {}),
        NoUserSettingsWrites(),
        NullCache(),
        AsyncNullCache(),
    )
    return app


@pytest.mark.usefixtures("multiuser_mode")
@parametrize_from_dict({"multiuser": {"multiuser": True}, "single-user": {"multiuser": False}})
def test_catalog_separates_selection_from_availability_and_uses_runtime_restrictions(tmp_path, multiuser):
    selections = {
        ("mine", "main"): {
            "OUROBOROS_USER_ENABLED_SKILL_IDS": '["distribution:alpha", "distribution:blocked"]',
            "OUROBOROS_REASONING_PROGRESS_ENABLED": "malformed unrelated flag",
        },
        ("other", "main"): {"OUROBOROS_USER_ENABLED_SKILL_IDS": "distribution:beta"},
    }
    with TestClient(_catalog_app(tmp_path, multiuser=multiuser, selections=selections)) as client:
        response = client.get("/api/owner-settings/main/skills", headers={"X-Ouroboros-Owner-Id": "mine"})
        assert response.status_code == 200
        rows = {row["id"]: row for row in response.json()["skills"]}
        names = {"alpha", "beta", "blocked"} | ({"script"} if not multiuser else set())
        assert set(rows) == {f"distribution:{name}" for name in names}
        assert rows["distribution:alpha"] == {
            "id": "distribution:alpha",
            "name": "alpha",
            "display_name": "alpha",
            "description": "alpha description",
            "short_description": "alpha description",
            "version": "1.0",
            "selected": True,
            "available": True,
            "blocked_reasons": [],
        }
        assert not rows["distribution:beta"]["selected"] and rows["distribution:beta"]["available"]
        assert rows["distribution:blocked"]["selected"] and not rows["distribution:blocked"]["available"]
        assert rows["distribution:blocked"]["blocked_reasons"] == ["agent allowlist blocks this skill"]
        other = client.get("/api/owner-settings/main/skills", headers={"X-Ouroboros-Owner-Id": "other"}).json()
        assert [r["id"] for r in other["skills"] if r["selected"]] == ["distribution:beta"]


@parametrize_from_dict({field: {"field": field} for field in ("display_name", "short_description")})
def test_invalid_optional_unicode_does_not_break_the_http_catalog(tmp_path, field):
    metadata = tmp_path / "skills" / "alpha" / "agents" / "gigaagent.yaml"
    metadata.parent.mkdir(parents=True)
    metadata.write_text(f'interface:\n  {field}: "\\uD800"\n')
    with TestClient(_catalog_app(tmp_path)) as client:
        response = client.get("/api/owner-settings/main/skills", headers={"X-Ouroboros-Owner-Id": "mine"})
    assert response.status_code == 200
    alpha = next(row for row in response.json()["skills"] if row["id"] == "distribution:alpha")
    assert (alpha["display_name"], alpha["short_description"]) == ("alpha", "alpha description")


@pytest.mark.usefixtures("multiuser_mode")
def test_catalog_requires_authenticated_owner_and_an_owned_agent(tmp_path):
    with TestClient(_catalog_app(tmp_path)) as client:
        assert client.get("/api/owner-settings/main/skills").status_code == 403
        assert (
            client.get("/api/owner-settings/unknown/skills", headers={"X-Ouroboros-Owner-Id": "mine"}).status_code
            == 404
        )


@pytest.mark.usefixtures("multiuser_mode")
def test_catalog_uses_the_named_agents_allowlist_instead_of_the_deployment_default(tmp_path):
    app = _catalog_app(
        tmp_path,
        availability=SkillAvailability.from_values(
            defaults=["distribution:alpha"],
            agents=[{"name": "main", "skills": ["distribution:beta"]}],
            executable_skills=False,
        ),
    )
    with TestClient(app) as client:
        rows = client.get("/api/owner-settings/main/skills", headers={"X-Ouroboros-Owner-Id": "mine"}).json()["skills"]
    assert [row["id"] for row in rows if row["available"]] == ["distribution:beta"]


@pytest.mark.usefixtures("multiuser_mode")
def test_catalog_load_failures_never_publish_local_diagnostic_paths(tmp_path):
    app = _catalog_app(tmp_path, extra_packages=(("a b", "instruction"), ("a@b", "instruction")))
    with TestClient(app) as client:
        listing = client.get("/api/owner-settings/main/skills", headers={"X-Ouroboros-Owner-Id": "mine"})
    assert listing.status_code == 200
    assert str(tmp_path) not in listing.text
    broken = next(row for row in listing.json()["skills"] if row["id"] == "distribution:a_b")
    assert not broken["available"]
    assert broken["blocked_reasons"] == ["skill package unavailable", "agent allowlist blocks this skill"]


@pytest.mark.usefixtures("multiuser_mode")
def test_catalog_public_reasons_survive_reworded_internal_diagnostics(tmp_path, monkeypatch):
    package = tmp_path / "skills" / "requirements"
    package.mkdir(parents=True)
    (package / "SKILL.md").write_text(
        "---\nname: requirements\ndescription: Example\ntype: instruction\n"
        "metadata:\n  openclaw:\n    os: [unsupported-test-os]\n    requires:\n"
        "      bins: [ouroboros-test-missing-executable]\n"
        "      env: [TEST_MISSING_ENV]\n      config: [test.missing.config]\n---\nInstructions\n"
    )
    monkeypatch.setattr(skill_loader, "_MAX_SKILL_FILE_BYTES", 1)
    monkeypatch.setattr(skill_loader, "_MAX_SKILL_TREE_BYTES", 1)

    class RewordedDiagnostics(SkillSurface):
        def skills(self, *, agent_name=None):
            skills = super().skills(agent_name=agent_name)
            for skill in skills:
                skill.blockers = [
                    replace(reason, detail=f"Reworded internal diagnostic: {tmp_path}") for reason in skill.blockers
                ]
            return skills

    with TestClient(_catalog_app(tmp_path, surface_type=RewordedDiagnostics)) as client:
        response = client.get("/api/owner-settings/main/skills", headers={"X-Ouroboros-Owner-Id": "mine"})

    assert response.status_code == 200
    row = next(row for row in response.json()["skills"] if row["id"] == "distribution:requirements")
    assert not row["available"]
    assert row["blocked_reasons"] == [
        "unsupported operating system",
        "required executable is unavailable",
        "required configuration is unavailable",
        "skill package exceeds size limits",
        "agent allowlist blocks this skill",
    ]
    assert str(tmp_path) not in response.text


@pytest.mark.usefixtures("multiuser_mode")
def test_catalog_serves_startup_text_metadata_without_icon_routes(tmp_path):
    package = tmp_path / "skills" / "nested" / "example"
    (package / "agents").mkdir(parents=True)
    (package / "agents" / "gigaagent.yaml").write_text(
        "interface:\n  display_name: Календарь\n  short_description: Даты и дни недели\n  icon_large: ./icon.png\n"
    )
    app = _catalog_app(tmp_path, extra_packages=(("nested/example", "instruction"),))
    owner = {"X-Ouroboros-Owner-Id": "mine"}
    with TestClient(app) as client, patch.object(Path, "open", side_effect=AssertionError("request reads metadata")):
        listing = client.get("/api/owner-settings/main/skills", headers=owner)
        skill = next(r for r in listing.json()["skills"] if r["id"] == "distribution:nested/example")
        assert (skill["display_name"], skill["short_description"]) == ("Календарь", "Даты и дни недели")
        assert not any(key.startswith("icon_") for key in skill)
        assert (
            client.get(
                "/api/owner-settings/main/skills/distribution:nested%2Fexample/icons/large", headers=owner
            ).status_code
            == 404
        )
        assert str(tmp_path) not in listing.text
