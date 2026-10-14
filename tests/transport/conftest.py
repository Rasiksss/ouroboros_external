"""Infrastructure fixtures for transport tests."""

from __future__ import annotations

import pytest

from tests.helpers.fake_gateway import FakeGateway, RecordingMemoryEvents, SettlingReader


@pytest.fixture
def gateway():
    """A FakeGateway: records submissions, can publish answers back."""
    return FakeGateway()


@pytest.fixture
def memory_events():
    """A RecordingMemoryEvents: records published events for assertion."""
    return RecordingMemoryEvents()


@pytest.fixture
def settling_reader():
    """A SettlingReader: drains the outbox and settles delivery on read."""
    return SettlingReader()
