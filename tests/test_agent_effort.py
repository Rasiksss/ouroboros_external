"""Test reasoning effort resolution via config.resolve_effort()."""

import os
from unittest.mock import patch

from ouroboros.app.config import resolve_effort
from ouroboros.storage.settings.bootstrap import bootstrap_config, reset_config_for_tests
from ouroboros.storage.settings.user_settings import reset_user_settings_for_tests
from tests.helpers.user_settings import StubUserSettingsStore


def _bootstrap_from_environ(
    environ: dict[str, str] | None = None,
    store: StubUserSettingsStore | None = None,
) -> None:
    reset_config_for_tests()
    if environ is not None:
        with patch.dict(os.environ, environ, clear=True):
            bootstrap_config(force=True)
    else:
        bootstrap_config(force=True)
    reset_user_settings_for_tests(store if store is not None else StubUserSettingsStore())


def test_task_effort_default_is_medium():
    """Default task effort is 'medium' when no env var is set."""
    _bootstrap_from_environ({})
    assert resolve_effort() == "medium"


def test_task_effort_via_new_env():
    """OUROBOROS_EFFORT_TASK controls task/chat effort."""
    for effort in ("none", "low", "medium", "high"):
        _bootstrap_from_environ({"OUROBOROS_EFFORT_TASK": effort})
        assert resolve_effort() == effort


def test_task_effort_legacy_alias_no_longer_honoured():
    """v5.15.0 retired OUROBOROS_INITIAL_REASONING_EFFORT — only the new key is read."""
    _bootstrap_from_environ({"OUROBOROS_INITIAL_REASONING_EFFORT": "high"})
    assert resolve_effort() == "medium"


def test_task_effort_invalid_falls_back_to_medium():
    """Invalid effort values fall back to 'medium'."""
    _bootstrap_from_environ({"OUROBOROS_EFFORT_TASK": "extreme"})
    assert resolve_effort() == "medium"


def test_an_owner_override_controls_the_effort():
    _bootstrap_from_environ({}, store=StubUserSettingsStore({"OUROBOROS_EFFORT_TASK": "high"}))
    assert resolve_effort() == "high"


def test_an_override_outside_the_valid_efforts_falls_back_to_the_default():
    _bootstrap_from_environ({}, store=StubUserSettingsStore({"OUROBOROS_EFFORT_TASK": "extreme"}))
    assert resolve_effort() == "medium"
