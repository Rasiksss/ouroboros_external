"""Infrastructure fixtures for agent/loop tests."""

from __future__ import annotations

import pytest

from tests.helpers.factories import make_loop_ctx, make_mock_llm
from tests.helpers.shared import collecting_event_sink


@pytest.fixture
def mock_llm():
    """A MagicMock LLM with default_model set; .chat.return_value is the test's to set."""
    return make_mock_llm()


@pytest.fixture
def event_sink():
    """A no-op event sink that records payloads for assertion."""
    return collecting_event_sink()


@pytest.fixture
def loop_ctx_factory():
    """Return a callable that builds a SimpleNamespace loop context for drive_loop tests."""
    return make_loop_ctx
