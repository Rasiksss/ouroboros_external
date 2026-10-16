"""``OUROBOROS_TRUSTED_EXECUTABLE_SKILL_IDS``: the deployment's named exemption from the multi-user ceiling."""

from __future__ import annotations

import pytest

from ouroboros.skills.availability import build_skill_availability
from ouroboros.storage.settings.schema import AppSettings


def test_trusted_executable_skill_ids_default_to_empty_and_the_ceiling_stays_closed() -> None:
    settings = AppSettings.model_validate({"OUROBOROS_MULTIUSER": True})

    assert settings.OUROBOROS_TRUSTED_EXECUTABLE_SKILL_IDS == []
    availability = build_skill_availability(settings)
    assert availability.executable_skills is False
    assert availability.trusted_executable_skills == frozenset()


def test_trusted_distribution_ids_are_normalized_and_carried_into_availability() -> None:
    settings = AppSettings.model_validate(
        {
            "OUROBOROS_MULTIUSER": True,
            "OUROBOROS_TRUSTED_EXECUTABLE_SKILL_IDS": '[" distribution:aifa-engine ", "distribution:aifa-engine"]',
        }
    )

    assert settings.OUROBOROS_TRUSTED_EXECUTABLE_SKILL_IDS == ["distribution:aifa-engine"]
    availability = build_skill_availability(settings)
    assert availability.trusted_executable_skills == frozenset({"distribution:aifa-engine"})


def test_a_comma_list_is_accepted_like_the_owner_allowlist() -> None:
    settings = AppSettings.model_validate(
        {"OUROBOROS_TRUSTED_EXECUTABLE_SKILL_IDS": "distribution:alpha,distribution:beta"}
    )

    assert settings.OUROBOROS_TRUSTED_EXECUTABLE_SKILL_IDS == ["distribution:alpha", "distribution:beta"]


@pytest.mark.parametrize(
    "value",
    [
        "hub:alpha",
        "user:alpha",
        "distribution:alpha,hub:beta",
        "alpha",
        '["distribution:alpha", 1]',
        "distribution:alpha,,distribution:beta",
        '["distribution:alpha"',
    ],
)
def test_a_trusted_id_that_is_not_a_distribution_id_is_refused_at_settings_load(value: str) -> None:
    with pytest.raises(ValueError, match="OUROBOROS_TRUSTED_EXECUTABLE_SKILL_IDS"):
        AppSettings.model_validate({"OUROBOROS_TRUSTED_EXECUTABLE_SKILL_IDS": value})
