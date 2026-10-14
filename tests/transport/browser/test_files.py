from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from threading import Event

from sqlalchemy import event
from sqlalchemy.exc import OperationalError

from infra.async_engine import async_engine_factory
from ouroboros.contracts.tool_confirmation import CONFIRMATION_TRANSPORT_KEY
from ouroboros.storage.chat_store import append_chat_message
from ouroboros.storage.file_catalog import FileCatalog
from ouroboros.storage.file_transfers import get_transfer_limits
from ouroboros.storage.runtime_persistence import runtime_persistence
from tests.helpers.files import file_client, link_file, stored_file
from tests.helpers.parametrize import parametrize_from_dict
from tests.helpers.runtime_store import claim_task, park_run
from tests.helpers.shared import TEST_SCOPE, scope_with
from tests.helpers.tool_confirmations import sql_tool_confirmation_reader


@parametrize_from_dict(
    {
        "owner_limit": {"endpoint": "/api/files", "owner_limit": 2, "process_limit": 8, "status": 429},
        "chat_owner_limit": {"endpoint": "/api/chat/upload", "owner_limit": 2, "process_limit": 8, "status": 429},
        "process_limit": {"endpoint": "/api/files", "owner_limit": 3, "process_limit": 2, "status": 503},
    }
)
def test_upload_capacity_keeps_http_retry_contract(tmp_path, monkeypatch, endpoint, owner_limit, process_limit, status):
    monkeypatch.setenv("BROWSER_INGRESS_ENABLED", "false")
    monkeypatch.setenv("OUROBOROS_FILE_OWNER_TRANSFERS", str(owner_limit))
    monkeypatch.setenv("OUROBOROS_FILE_TRANSFERS", str(process_limit))
    with file_client(tmp_path) as client:
        with get_transfer_limits().slot(TEST_SCOPE):
            response = client.post(endpoint, files={"file": ("report.txt", b"hello", "text/plain")})
            assert response.status_code == status
            assert response.headers["Retry-After"] == "1"
            assert response.json()["retryable"] is True
            assert response.json()["code"] == ("file_owner_capacity" if status == 429 else "file_process_capacity")
        retry = client.post(endpoint, files={"file": ("report.txt", b"hello", "text/plain")})
        assert retry.is_success
        assert retry.json()["display_name" if endpoint == "/api/chat/upload" else "name"] == "report.txt"


@parametrize_from_dict(
    {
        "owner_retains_navigation": {"owned": True},
        "other_owner_cannot_navigate": {"owned": False},
    }
)
def test_deleted_file_keeps_authorized_message_links(tmp_path, owned):
    scope = TEST_SCOPE if owned else scope_with(TEST_SCOPE, owner_id="another-owner")
    file = link_file(stored_file(scope=scope), scope=scope)
    with runtime_persistence.current.engine.begin() as conn:
        FileCatalog(conn, scope).delete_file(file["id"])
    with file_client(tmp_path) as client:
        response = client.get(f"/api/files/{file['id']}/messages")
        assert response.status_code == (200 if owned else 404)
        if owned:
            assert len(response.json()["items"]) == 1
        assert client.get(f"/api/files/{file['id']}/content").status_code == 404


def test_download_admission_database_failure_returns_retryable_response(tmp_path):
    file = stored_file()

    def unavailable(conn, cursor, statement, parameters, context, executemany):
        if "file_transfer_leases" in statement:
            raise OperationalError("file transfer admission", {}, OSError("database unavailable"))

    with file_client(tmp_path) as client:
        engine = async_engine_factory.engine.sync_engine
        event.listen(engine, "before_cursor_execute", unavailable)
        try:
            response = client.get(f"/api/files/{file['id']}/content")
            assert response.status_code == 503
            assert response.headers["Retry-After"] == "1"
        finally:
            event.remove(engine, "before_cursor_execute", unavailable)


@parametrize_from_dict(
    {
        "owner_capacity": {"owner_limit": 2, "process_limit": 8, "expires": False},
        "process_capacity": {"owner_limit": 3, "process_limit": 2, "expires": False},
        "deadline": {"owner_limit": 2, "process_limit": 8, "expires": True},
    }
)
def test_download_waits_for_another_transfer(tmp_path, monkeypatch, owner_limit, process_limit, expires):
    monkeypatch.setenv("OUROBOROS_FILE_OWNER_TRANSFERS", str(owner_limit))
    monkeypatch.setenv("OUROBOROS_FILE_TRANSFERS", str(process_limit))
    if expires:
        monkeypatch.setenv("OUROBOROS_FILE_IO_TIMEOUT_SEC", "1")
    file = stored_file()
    attempted = Event()

    def admission_checked(conn, cursor, statement, parameters, context, executemany):
        if "count(" in statement and "file_transfer_leases" in statement:
            attempted.set()

    with file_client(tmp_path) as client, ThreadPoolExecutor(1) as pool:
        engine = async_engine_factory.engine.sync_engine
        event.listen(engine, "after_cursor_execute", admission_checked)
        try:
            with get_transfer_limits().slot(TEST_SCOPE):
                response = pool.submit(client.get, f"/api/files/{file['id']}/content")
                assert attempted.wait(5)
                if expires:
                    assert response.result(timeout=10).status_code == 429
            if expires:
                response = pool.submit(client.get, f"/api/files/{file['id']}/content")
            downloaded = response.result(timeout=10)
            assert downloaded.status_code == 200
            assert downloaded.content == b"hello"
        finally:
            event.remove(engine, "after_cursor_execute", admission_checked)


def test_concurrent_message_retries_keep_one_file_link_and_history_tracks_deletion(tmp_path, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    from tests.helpers.server_client import patched_server_client

    with patched_server_client(monkeypatch, tmp_path, browser_enabled=True, reset_browser_ws=True) as client:
        uploaded = client.post("/api/chat/upload", files={"file": ("report.txt", b"hello", "text/plain")}).json()
        file_id = uploaded["attachment_id"]
        packet = {"type": "chat", "content": "report", "client_message_id": "retry", "attachment_ids": [file_id]}
        barrier = Barrier(2)

        def send():
            with client.websocket_connect("/ws") as ws:
                barrier.wait(timeout=10)
                ws.send_json(packet)
                return ws.receive_json()

        with ThreadPoolExecutor(2) as pool:
            replies = list(pool.map(lambda _: send(), range(2)))
        assert all(reply["attachments"][0]["id"] == file_id for reply in replies)
        history = client.get("/api/chat/history").json()["messages"]
        assert len(history) == 1
        links = client.get(f"/api/files/{file_id}/messages").json()["items"]
        assert [link["message_id"] for link in links] == [history[0]["message_id"]]
        assert client.patch(f"/api/files/{file_id}", json={"name": "renamed.txt"}).status_code == 200
        assert client.get("/api/chat/history").json()["messages"][0]["attachments"][0]["filename"] == "renamed.txt"
        other = client.post("/api/chat/upload", files={"file": ("other.txt", b"other", "text/plain")}).json()
        with client.websocket_connect("/ws") as ws:
            ws.send_json({**packet, "attachment_ids": [other["attachment_id"]]})
            assert ws.receive_json()["code"] == "message_conflict"
        assert client.delete("/api/chat/upload", params={"id": other["attachment_id"]}).status_code == 200
        assert client.delete(f"/api/files/{file_id}").status_code == 204
        attachment = client.get("/api/chat/history").json()["messages"][0]["attachments"][0]
        assert attachment["status"] == "deleted" and "url" not in attachment


@parametrize_from_dict(
    {
        "current_chat": {"confirmation_scope": TEST_SCOPE, "expected_open": True},
        "another_chat": {"confirmation_scope": scope_with(TEST_SCOPE, chat_id="2"), "expected_open": False},
        "another_surface": {
            "confirmation_scope": scope_with(TEST_SCOPE, surface_id="dialogs"),
            "expected_open": False,
        },
        "another_owner": {
            "confirmation_scope": scope_with(TEST_SCOPE, owner_id="another-owner"),
            "expected_open": False,
        },
        "another_agent": {
            "confirmation_scope": scope_with(TEST_SCOPE, agent_id="another-agent"),
            "expected_open": False,
        },
    }
)
def test_history_keeps_confirmation_state_in_its_scope(tmp_path, confirmation_scope, expected_open):
    store = runtime_persistence.initialize()
    claim_task(store, scope=confirmation_scope)
    confirmation = park_run(store, scope=confirmation_scope)
    append_chat_message(
        tmp_path,
        scope=TEST_SCOPE,
        actor_type="agent",
        chat_id=1,
        user_id=None,
        text="Allow the tool?",
        source="browser",
        transport={
            CONFIRMATION_TRANSPORT_KEY: {
                "confirmation_id": confirmation.confirmation_id,
                "task_id": "task1",
                "tool": "data_write",
            }
        },
    )
    with file_client(tmp_path) as client:
        response = client.get("/api/chat/history")
        assert response.status_code == 200, response.text
        assert response.json()["messages"][0]["confirmation"]["open"] is expected_open
        sql_tool_confirmation_reader(store.engine).expire_tool_confirmation(
            confirmation.confirmation_id, now_iso="2099-01-01T00:00:00+00:00"
        )
        assert client.get("/api/chat/history").json()["messages"][0]["confirmation"]["open"] is False


def test_library_crud_preserves_chat_urls_and_keeps_drafts_separate(tmp_path):
    with file_client(tmp_path) as client:
        response = client.post("/api/folders", json={"name": "Reports"})
        assert response.status_code == 201, response.text
        folder_id = response.json()["id"]
        response = client.post(
            "/api/files", files={"file": ("report.txt", b"hello", "text/plain")}, data={"folder_id": folder_id}
        )
        assert response.status_code == 201, response.text
        file = response.json()
        file_id = file["id"]
        assert file["status"] == "ready" and file["expires_at"] is None
        assert "storage_key" not in file
        assert client.get(file["url"]).content == b"hello"
        assert client.delete("/api/folders/" + folder_id).status_code == 409
        assert client.delete("/api/chat/upload", params={"id": file_id}).status_code == 409
        renamed = client.patch("/api/files/" + file_id, json={"name": "renamed.txt", "folder_id": None})
        assert renamed.status_code == 200
        assert renamed.json()["id"] == file_id
        assert client.get(f"/api/chat/files/{file_id}/report.txt").content == b"hello"
        assert [item["id"] for item in client.get("/api/files/entries").json()["items"]] == [folder_id, file_id]
        assert client.delete("/api/folders/" + folder_id).status_code == 204
        draft = client.post("/api/chat/upload", files={"file": ("draft.txt", b"draft", "text/plain")}).json()
        assert draft["status"] == "draft"
        assert [item["id"] for item in client.get("/api/files/entries").json()["items"]] == [file_id]
        assert client.delete("/api/chat/upload", params={"id": draft["attachment_id"]}).status_code == 200
        assert client.get("/" + draft["url"]).status_code == 404
        assert client.patch("/api/files/" + file_id, json={"owner_id": "other"}).status_code == 400
        assert client.delete("/api/files/" + file_id).status_code == 204
        assert client.get(file["url"]).status_code == 404
