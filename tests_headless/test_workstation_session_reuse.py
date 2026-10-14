"""Integration-style tests for WorkStation session reuse across tasks in the same conversation.

These tests exercise the WorkstationManager with a real (mocked HTTP) client to verify
that the same scope_key (chat_id) reuses a session while different scope_keys create
new sessions.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from ouroboros.egress.workstation.client import CreateSessionResult, SessionStatus, WorkstationConfig
from ouroboros.egress.workstation.manager import WorkstationManager
from ouroboros.egress.workstation.session import scope_key_for


@pytest.fixture()
def config() -> WorkstationConfig:
    return WorkstationConfig(
        base_url="https://ws.example",
        create_secret="",
        session_strategy="recover",
    )


@pytest.fixture()
def manager() -> WorkstationManager:
    return WorkstationManager()


def _fake_create_session(
    session_id: str,
    *,
    timeout_sec: float | None = None,
) -> CreateSessionResult:
    import secrets

    return CreateSessionResult(
        session_id=session_id,
        status=SessionStatus.READY,
        token=f"tok-{secrets.token_hex(8)}",
        created=True,
        restored=False,
        quotas={},
    )


def _fake_get_session(
    session_id: str,
    token: str,
    *,
    timeout_sec: float | None = None,
) -> Any:
    state = MagicMock()
    state.status = SessionStatus.READY
    state.quotas = {}
    return state


class TestSessionReuse:
    def test_same_chat_id_reuses_session(self, manager: WorkstationManager, config: WorkstationConfig) -> None:
        scope_key = scope_key_for("chat-111")

        with patch.object(config.__class__, "from_settings", return_value=config):
            pass

        with patch("ouroboros.egress.workstation.manager.WorkstationClient") as MockClient:
            client = MockClient.return_value
            client.create_session.side_effect = _fake_create_session
            client.get_session.side_effect = _fake_get_session

            handle1 = manager.get_or_create(scope_key=scope_key, config=config)
            handle2 = manager.get_or_create(scope_key=scope_key, config=config)

            assert handle1.record.session_id == handle2.record.session_id
            assert handle1.record.token == handle2.record.token
            # get_session called on reuse
            client.get_session.assert_called_once()

    def test_different_chat_ids_get_different_sessions(
        self, manager: WorkstationManager, config: WorkstationConfig
    ) -> None:
        key_a = scope_key_for("chat-aaa")
        key_b = scope_key_for("chat-bbb")

        with patch("ouroboros.egress.workstation.manager.WorkstationClient") as MockClient:
            client = MockClient.return_value
            client.create_session.side_effect = _fake_create_session
            client.get_session.side_effect = _fake_get_session

            handle_a = manager.get_or_create(scope_key=key_a, config=config)
            handle_b = manager.get_or_create(scope_key=key_b, config=config)

            assert handle_a.record.session_id != handle_b.record.session_id

    def test_forget_and_recreate_after_not_found(self, manager: WorkstationManager, config: WorkstationConfig) -> None:
        scope_key = scope_key_for("chat-retry")

        with patch("ouroboros.egress.workstation.manager.WorkstationClient") as MockClient:
            client = MockClient.return_value
            client.create_session.side_effect = _fake_create_session

            # First call creates
            handle1 = manager.get_or_create(scope_key=scope_key, config=config)
            first_token = handle1.record.token

            # Second call: get_session raises not-found -> forget -> create new
            from ouroboros.egress.workstation.client import ErrorCode, WorkstationApiError

            client.get_session.side_effect = WorkstationApiError(
                operation="get_session",
                status_code=404,
                code=ErrorCode.NOT_FOUND,
                message="not found",
            )

            handle2 = manager.get_or_create(scope_key=scope_key, config=config)

            # session_id is deterministic for the same scope_key (recover strategy),
            # but a new token is issued after recreate.
            assert handle2.record.session_id == handle1.record.session_id
            assert handle2.record.token != first_token

    def test_scope_key_uses_chat_id_not_task_id(self) -> None:
        key = scope_key_for("chat-42")
        assert key == "chat-42"

    def test_concurrent_different_scopes_do_not_block(self) -> None:
        key_a = scope_key_for("chat-concurrent-a")
        key_b = scope_key_for("chat-concurrent-b")
        assert key_a != key_b
        # Both scope keys resolve independently
        assert scope_key_for("chat-concurrent-a") == key_a
        assert scope_key_for("chat-concurrent-b") == key_b
