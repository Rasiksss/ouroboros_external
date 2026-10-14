"""Timezone and locale resolution for runtime-facing dates."""

from __future__ import annotations

from ouroboros.runtime_time import runtime_time_config
from tests.helpers.scope import scope_for
from tests.helpers.user_settings import StubUserSettingsStore, reset_user_settings_for_tests

_SCOPE = scope_for("web", "c1")


def test_an_owner_override_controls_the_runtime_timezone_and_locale():
    reset_user_settings_for_tests(
        StubUserSettingsStore({"OUROBOROS_TIMEZONE": "Asia/Vladivostok", "OUROBOROS_LOCALE": "en-US"})
    )
    config = runtime_time_config(_SCOPE)
    assert config.timezone_name == "Asia/Vladivostok"
    assert config.locale == "en-US"


def test_an_explicit_argument_beats_the_override():
    reset_user_settings_for_tests(StubUserSettingsStore({"OUROBOROS_TIMEZONE": "Asia/Vladivostok"}))
    assert runtime_time_config(_SCOPE, timezone_name="Europe/Berlin").timezone_name == "Europe/Berlin"
