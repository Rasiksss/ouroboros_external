"""Container probe for task-fixed owner skill allowlists on the multi-user stand."""

from __future__ import annotations

import json
import os
import pathlib
import sys
import urllib.error
import urllib.request
import uuid
from typing import Any

from config_registry.user_settings.infra.repositories.repo import (
    SqlAsyncUserSettingsStore,
    SqlSyncUserSettingsRepo,
    SqlUserSettingsStore,
)
from config_registry.user_settings.reader import USER_ENABLED_SKILL_IDS_KEY, UserSettings
from infra.cache import cache_factory
from new_core.identity import OwnerScope, SurfaceId
from new_core.skills.catalog import SkillCatalog
from new_core.skills.models import SkillId
from new_core.skills.providers import DistributionSkillProvider
from ouroboros.agent.task_graph import _build_task_skill_surface
from ouroboros.skills.availability import build_skill_availability
from ouroboros.skills.loader import DistributionSkillLoader
from ouroboros.storage.runtime_persistence import runtime_persistence
from ouroboros.storage.settings.store import load_settings

API_BASE = os.environ.get("SKILL_SELECTION_E2E_API_BASE", "http://balancer:8080").rstrip("/")
RUN_ID = os.environ.get("SKILL_SELECTION_E2E_RUN_ID", uuid.uuid4().hex[:8])
ALPHA = "distribution:calendar-orientation"
BETA = "distribution:tasks-scheduler"


def _request(method: str, path: str, owner_id: str, body: dict[str, Any] | None = None) -> Any:
    payload = None if body is None else json.dumps(body).encode()
    request = urllib.request.Request(
        f"{API_BASE}{path}",
        data=payload,
        headers={
            "Content-Type": "application/json",
            "X-Ouroboros-Owner-Id": owner_id,
            "X-Ouroboros-Chat-Id": "skill-selection-e2e",
        },
        method=method,
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            document = response.read()
    except urllib.error.HTTPError as failure:
        raise AssertionError(f"{method} {path} returned {failure.code}: {failure.read().decode()}") from failure
    return json.loads(document) if document else None


def _ensure_main_agent(owner_id: str) -> str:
    _request("GET", "/api/chat/history", owner_id)
    agents = _request("GET", "/api/agents/", owner_id)
    assert [agent["agent_id"] for agent in agents] == ["main"], agents
    return "main"


def _create_agent(owner_id: str, name: str) -> str:
    created = _request("POST", "/api/agents/", owner_id, {"name": name})
    return str(created["agent_id"])


def _set_allowlist(owner_id: str, agent_id: str, skill_ids: list[str]) -> None:
    _request(
        "PUT",
        f"/api/owner-settings/{agent_id}/{USER_ENABLED_SKILL_IDS_KEY}",
        owner_id,
        {"value": json.dumps(skill_ids)},
    )


def _scope(owner_id: str, agent_id: str) -> OwnerScope:
    return OwnerScope(owner_id=owner_id, agent_id=agent_id, surface_id=SurfaceId.BROWSER, chat_id="skills-e2e")


def _skill_ids(surface) -> list[str]:
    return sorted(str(skill.skill_id) for skill in surface.skills() if skill.eligible and skill.selected)


def main() -> int:
    settings = load_settings()
    runtime_persistence.initialize()
    cache_factory.initialize(
        str(settings.OUROBOROS_SETTINGS_CACHE_BACKEND or "none"),
        int(settings.OUROBOROS_SETTINGS_CACHE_TTL_SEC),
    )
    owner_settings = UserSettings(
        settings,
        SqlUserSettingsStore(),
        SqlAsyncUserSettingsStore(),
        SqlSyncUserSettingsRepo(),
        cache_factory.cache(),
        cache_factory.async_cache(),
    )
    catalog = SkillCatalog()
    catalog.initialize(
        [
            DistributionSkillProvider(
                distribution_root=pathlib.Path(str(settings.get("OUROBOROS_REPO_DIR", "/app"))) / "skills",
                loader=DistributionSkillLoader(settings),
            )
        ]
    )
    snapshot = catalog.snapshot()
    availability = build_skill_availability(settings)
    owners = {role: f"skills-e2e-{RUN_ID}-{role}" for role in ("alpha", "beta", "missing")}
    agents: list[tuple[str, str]] = []

    try:
        alpha_main = _ensure_main_agent(owners["alpha"])
        beta_main = _ensure_main_agent(owners["beta"])
        missing_main = _ensure_main_agent(owners["missing"])
        alpha_sub = _create_agent(owners["alpha"], f"sub-{RUN_ID}")
        agents.extend(
            [
                (owners["alpha"], alpha_main),
                (owners["alpha"], alpha_sub),
                (owners["beta"], beta_main),
                (owners["missing"], missing_main),
            ]
        )
        _set_allowlist(owners["alpha"], alpha_main, [BETA])
        _set_allowlist(owners["alpha"], alpha_sub, [ALPHA])
        _set_allowlist(owners["beta"], beta_main, [ALPHA])

        surfaces = {
            "alpha_main": _build_task_skill_surface(
                snapshot=snapshot,
                availability=availability,
                scope=_scope(owners["alpha"], alpha_main),
                settings=owner_settings,
            ),
            "alpha_sub": _build_task_skill_surface(
                snapshot=snapshot,
                availability=availability,
                scope=_scope(owners["alpha"], alpha_sub),
                settings=owner_settings,
            ),
            "beta_main": _build_task_skill_surface(
                snapshot=snapshot,
                availability=availability,
                scope=_scope(owners["beta"], beta_main),
                settings=owner_settings,
            ),
            "missing_main": _build_task_skill_surface(
                snapshot=snapshot,
                availability=availability,
                scope=_scope(owners["missing"], missing_main),
                settings=owner_settings,
            ),
        }
        denied_id = SkillId.parse(ALPHA)
        assert surfaces["alpha_main"].get(denied_id) is None

        _set_allowlist(owners["alpha"], alpha_main, [ALPHA])
        next_alpha = _build_task_skill_surface(
            snapshot=snapshot,
            availability=availability,
            scope=_scope(owners["alpha"], alpha_main),
            settings=owner_settings,
        )
        first_alpha_after_update = _skill_ids(surfaces["alpha_main"])
        _request(
            "DELETE",
            f"/api/owner-settings/{alpha_main}/{USER_ENABLED_SKILL_IDS_KEY}",
            owners["alpha"],
        )
        cleared_alpha = _build_task_skill_surface(
            snapshot=snapshot,
            availability=availability,
            scope=_scope(owners["alpha"], alpha_main),
            settings=owner_settings,
        )

        summary = {
            "alpha_main": first_alpha_after_update,
            "alpha_main_after_update": _skill_ids(next_alpha),
            "alpha_main_after_clear": _skill_ids(cleared_alpha),
            "alpha_sub": _skill_ids(surfaces["alpha_sub"]),
            "beta_main": _skill_ids(surfaces["beta_main"]),
            "denied_lookup": surfaces["alpha_main"].get(denied_id) is None,
            "missing_main": _skill_ids(surfaces["missing_main"]),
        }
        print(f"SKILL_OWNER_SELECTION_E2E={json.dumps(summary, sort_keys=True)}", flush=True)
        return 0
    finally:
        runtime_persistence.close()
        for owner_id, agent_id in reversed(agents):
            try:
                _request(
                    "DELETE",
                    f"/api/owner-settings/{agent_id}/{USER_ENABLED_SKILL_IDS_KEY}",
                    owner_id,
                )
                _request("DELETE", f"/api/agents/{agent_id}", owner_id)
            except Exception as cleanup_failure:
                print(f"cleanup failed for {owner_id}/{agent_id}: {cleanup_failure}", file=sys.stderr)


if __name__ == "__main__":
    raise SystemExit(main())
