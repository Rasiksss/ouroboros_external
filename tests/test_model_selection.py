"""Model selection honours per-owner overrides."""

from __future__ import annotations

from ouroboros.app.config import get_light_model, get_main_model
from ouroboros.llm.client import LLMClient
from ouroboros.storage.settings.user_settings import reset_user_settings_for_tests
from tests.helpers.user_settings import StubUserSettingsStore

_MAIN_OVERRIDE = "gigachat-b2bank::GigaChat-Max"
_LIGHT_OVERRIDE = "gigachat-b2bank::GigaChat-Lite"


def _override_models() -> None:
    reset_user_settings_for_tests(
        StubUserSettingsStore({"OUROBOROS_MODEL": _MAIN_OVERRIDE, "OUROBOROS_MODEL_LIGHT": _LIGHT_OVERRIDE})
    )


def test_an_owner_override_controls_the_main_and_light_models():
    _override_models()
    assert get_main_model() == _MAIN_OVERRIDE
    assert get_light_model() == _LIGHT_OVERRIDE


def test_the_client_default_model_follows_the_override():
    _override_models()
    assert LLMClient().default_model() == _MAIN_OVERRIDE
