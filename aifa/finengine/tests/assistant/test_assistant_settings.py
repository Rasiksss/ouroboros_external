"""Адрес ouroboros и сроки хода — поля `Settings` службы (CONTRACTS.md §2, ADR-0021 п. 1)."""
from __future__ import annotations

import pytest

from finengine.assistant.ouro_client import OuroClient
from finengine.assistant.turn_gateway import TurnTimings
from finengine.config import Settings


def test_the_turn_deadline_is_the_hard_timeout_of_ouroboros_plus_a_minute() -> None:
    """ADR-0021 п. 1: крайний срок хода = `OUROBOROS_HARD_TIMEOUT_SEC` + 60 с, настройкой службы."""
    settings = Settings(ouroboros_hard_timeout_sec=1800)

    timings = TurnTimings.from_settings(settings)

    assert timings.deadline_sec == 1860.0
    assert (timings.stop_wait_sec, timings.reconnect_attempts) == (30.0, 3)


def test_the_defaults_follow_the_dev_circuit() -> None:
    """Умолчания — контур dev: ouroboros во внутренней сети (ADR-0028), `HARD_TIMEOUT` 2700 (`ouroboros.env.example`)."""
    settings = Settings()

    assert settings.ouroboros_url == "http://ouroboros:8080"
    assert TurnTimings.from_settings(settings).deadline_sec == 2760.0
    assert settings.ouroboros_max_inbound_chars == 50_000


def test_the_environment_sets_the_address_and_the_limit_of_the_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FINENGINE_OUROBOROS_URL", "http://10.0.0.5:8080/")
    monkeypatch.setenv("FINENGINE_OUROBOROS_MAX_INBOUND_CHARS", "1000")

    client = OuroClient.from_settings(Settings())

    assert client.max_message_chars == 1000
    assert client._ws_url == "ws://10.0.0.5:8080/ws"
