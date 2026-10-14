from __future__ import annotations

import logging

from sqlalchemy import event

import server
from new_core.identity import OwnerScope
from ouroboros.storage.runtime_persistence import runtime_persistence
from supervisor.interfaces.owner_state import OwnerStateDocument
from supervisor.state import configured_owner_state_scope
from tests.helpers.owner_state import seed_owner_state
from tests.helpers.parametrize import parametrize_from_dict
from tests.helpers.supervisor_owner_state import sql_owner_state_repository


@parametrize_from_dict(
    {
        "dialogs owner": {"source": "dialogs", "reported": True},
        "browser owner": {"source": "browser", "reported": False},
    }
)
def test_disabled_dialogs_composition_reports_the_stored_owner_source(tmp_path, monkeypatch, caplog, source, reported):
    monkeypatch.setenv("DIALOGS_GRPC_ENDPOINT", "")
    state_repo = seed_owner_state(tmp_path)
    scope = configured_owner_state_scope()
    owner_scope = OwnerScope(owner_id=scope.owner_id, agent_id=scope.agent_id, surface_id=source, chat_id="1")
    sql_owner_state_repository(runtime_persistence.current.engine).save(
        OwnerStateDocument({"owner_scope": owner_scope.to_dict()}), scope
    )

    with caplog.at_level(logging.ERROR, logger="ouroboros.app.transport_lifecycle"):
        assert server._build_dialogs_transport(tmp_path, "session", state_repo) is None

    assert any("dialogs transport is disabled" in record.message for record in caplog.records) is reported


def test_disabled_dialogs_composition_tolerates_an_unreadable_owner_state(tmp_path, monkeypatch, caplog) -> None:
    monkeypatch.setenv("DIALOGS_GRPC_ENDPOINT", "")
    state_repo = seed_owner_state(tmp_path)
    engine = runtime_persistence.current.engine

    def refuse_owner_read(conn, cursor, statement, parameters, context, executemany):
        if "SELECT payload_json FROM owner_runtime_state" in statement:
            raise OSError("state unavailable")

    event.listen(engine, "before_cursor_execute", refuse_owner_read)
    try:
        with caplog.at_level(logging.DEBUG, logger="server"):
            assert server._build_dialogs_transport(tmp_path, "session", state_repo) is None
    finally:
        event.remove(engine, "before_cursor_execute", refuse_owner_read)

    assert "Failed to read the owner chat source" in caplog.text
    assert not any("every message published for it is discarded" in record.message for record in caplog.records)
