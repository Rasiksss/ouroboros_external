"""Dialogs registers an owned document and an active turn before agent tools run."""

from types import SimpleNamespace
from unittest.mock import Mock

import httpx
import pytest
from fastapi import FastAPI, HTTPException


@pytest.fixture
def ingress(monkeypatch, tmp_path):
    from finengine.assistant import dialogs_ingress as module
    from finengine.store import cache
    from finengine.store.blobs import LocalBlobStore

    rows, documents = {}, {}
    monkeypatch.setattr(cache, "_cached", None)
    monkeypatch.setattr(cache.settings, "redis_url", "")
    monkeypatch.setattr(module, "owner_user", lambda identity: 77)
    monkeypatch.setattr(module.state, "get", lambda kind, *, owner, key: rows.get((kind, owner, key)))
    monkeypatch.setattr(module.state, "put", lambda kind, *, owner, key, doc: rows.__setitem__((kind, owner, key), doc))
    def claim(kind, *, owner, key, doc):
        return rows.setdefault((kind, owner, key), doc)
    monkeypatch.setattr(module.state, "claim", claim)
    store = SimpleNamespace(get_document=documents.get,
                            add_document=lambda meta, chunks: documents.__setitem__(meta.doc_id, meta))
    monkeypatch.setattr(module, "get_store", lambda: store)
    blob = LocalBlobStore(tmp_path / "blobs")
    monkeypatch.setattr(module.blobs, "get_blob_store", lambda: blob)
    monkeypatch.setattr(module.blobs, "document_text", lambda doc_id: "")
    return module, documents, rows


def payload(module, **changes):
    data = dict(owner_id="tenant", agent_id="main", chat_id="chat1", source_message_id="msg1",
                message="Extract the report", attachment_ids=["file1"])
    return module.DialogsTurn(**(data | changes))


def test_document_and_turn_are_bound_before_first_open(ingress):
    module, documents, _ = ingress
    from finengine.assistant.binding import Binding
    from finengine.assistant.tool_call import BoundCall, ToolCall
    from finengine.assistant.turn_record import run_is_active
    result = module.register_turn(payload(module), [("report.pdf", b"%PDF-1.7\nexample")])
    record = result.record
    assert run_is_active(record.run_id)
    assert len(record.doc_ids) == len(result.pending) == 1
    doc = documents[record.doc_ids[0]]
    assert doc.owner_id == 77 and doc.ocr_engine == "idp"
    assert module.blobs.get_blob_store().get(doc.file_path).startswith(b"%PDF")
    ctx = dict(owner_id="tenant", agent_id="main", chat_id="chat1", task_id="root", run_id=record.run_id)
    gateway = SimpleNamespace(root_known=Mock(return_value=True))
    assert isinstance(Binding(gateway).bind(ToolCall.model_validate({"ctx": ctx})), BoundCall)


def test_replay_keeps_root_and_does_not_duplicate_document_or_ocr(ingress):
    module, documents, _ = ingress
    from finengine.assistant.turn_record import with_root
    data = payload(module)
    first = module.register_turn(data, [("report.pdf", b"%PDF-1.7\nexample")])
    with_root(first.record, "original-root")
    again = module.register_turn(data, [("report.pdf", b"%PDF-1.7\nexample")])
    assert again.record.run_id == first.record.run_id
    assert again.record.root_task_id == "original-root"
    assert len(documents) == 1 and not again.pending


def test_followup_reuses_chat_document_but_has_its_own_run(ingress):
    module, _, _ = ingress
    first = module.register_turn(payload(module), [("report.pdf", b"%PDF-1.7\nexample")])
    second = module.register_turn(payload(module, source_message_id="msg2", attachment_ids=[]), [])
    assert second.record.doc_ids == first.record.doc_ids
    assert second.record.run_id != first.record.run_id
    other = module.register_turn(payload(module, chat_id="chat2", attachment_ids=[]), [])
    assert not other.record.doc_ids


@pytest.mark.parametrize("field,value", [("owner_id", "other"), ("chat_id", "other"), ("agent_id", "other")])
def test_registered_turn_rejects_another_identity(ingress, field, value):
    module, _, _ = ingress
    from finengine.assistant.binding import Binding, Refused
    from finengine.assistant.tool_call import ToolCall
    result = module.register_turn(payload(module), [("report.pdf", b"%PDF-1.7\nexample")])
    ctx = dict(owner_id="tenant", agent_id="main", chat_id="chat1", task_id="root", run_id=result.record.run_id)
    ctx[field] = value
    assert isinstance(Binding(SimpleNamespace(root_known=lambda *a: True)).bind(ToolCall.model_validate({"ctx": ctx})), Refused)


def test_changed_replay_and_invalid_pdf_do_not_register(ingress):
    module, documents, _ = ingress
    data = payload(module)
    module.register_turn(data, [("report.pdf", b"%PDF-1.7\nexample")])
    with pytest.raises(HTTPException) as conflict:
        module.register_turn(data.model_copy(update={"message": "changed"}), [("report.pdf", b"%PDF-1.7\nexample")])
    assert conflict.value.status_code == 409
    with pytest.raises(HTTPException):
        module.register_turn(payload(module, source_message_id="bad", attachment_ids=["bad"]), [("broken.pdf", b"not a PDF")])
    assert len(documents) == 1


def test_restart_restores_registration_from_receipt(ingress, monkeypatch):
    module, documents, _ = ingress
    from finengine.assistant.turn_record import run_is_active
    from finengine.store import cache
    first = module.register_turn(payload(module), [("report.pdf", b"%PDF-1.7\nexample")])
    monkeypatch.setattr(cache, "_cached", None)
    restored = module.register_turn(payload(module), [("report.pdf", b"%PDF-1.7\nexample")])
    assert restored.record.run_id == first.record.run_id and run_is_active(first.record.run_id)
    assert len(documents) == 1


@pytest.mark.asyncio
async def test_registration_requires_service_token_before_reading_body(ingress, monkeypatch):
    module, _, _ = ingress
    from finengine.assistant import tool_api
    monkeypatch.setattr(tool_api.settings, "aifa_engine_token", "test-secret")
    app = FastAPI()
    app.include_router(module.router)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://aifa") as client:
        response = await client.post("/internal/dialogs/v1/turns", content=b"not multipart")
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_http_registration_then_real_tool_binding(ingress, monkeypatch):
    module, _, _ = ingress
    from unittest.mock import AsyncMock

    from finengine.assistant import tool_api
    monkeypatch.setattr(tool_api.settings, "aifa_engine_token", "test-secret")
    parsed = []
    monkeypatch.setattr(module, "_parse_document", lambda meta: parsed.append(meta.doc_id))
    api = tool_api.ToolApi(SimpleNamespace(root_known=lambda *a: True), AsyncMock())
    execute = AsyncMock(return_value="ГОТОВО")
    api._tools["aifa_open"] = SimpleNamespace(name="aifa_open", root_only=True, run=execute)
    app = FastAPI()
    app.include_router(module.router)
    app.include_router(tool_api.router)
    app.dependency_overrides[tool_api.get_tool_api] = lambda: api
    data = payload(module)
    headers = {"Authorization": "Bearer test-secret"}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://aifa") as client:
        registered = await client.post("/internal/dialogs/v1/turns", headers=headers,
            files=[("registration", (None, data.model_dump_json(), "application/json")),
                   ("files", ("report.pdf", b"%PDF-1.7\nexample", "application/pdf"))])
        assert registered.status_code == 200
        run_id = registered.json()["run_id"]
        ctx = dict(owner_id=data.owner_id, agent_id=data.agent_id, chat_id=data.chat_id,
                   task_id="root", run_id=run_id)
        opened = await client.post("/internal/ouro/v1/aifa_open", headers=headers, json={"ctx":ctx,"args":{}})
    assert opened.status_code == 200 and opened.text == "ГОТОВО"
    execute.assert_awaited_once()
    assert parsed == registered.json()["doc_ids"]


def test_service_user_mapping_is_stable_and_not_shared_between_chats(ingress, monkeypatch):
    module, _, _ = ingress
    # The fixture replaces owner_user; exercise the real function from a fresh module load.
    import importlib

    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from finengine.auth.db import User
    owner_user = importlib.reload(module).owner_user
    engine = create_engine("sqlite://")
    User.__table__.create(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    monkeypatch.setattr(module, "SessionLocal", factory)
    first = owner_user("a" * 64)
    assert first == owner_user("a" * 64)
    assert first != owner_user("b" * 64)
    with factory() as session:
        user = session.get(User, first)
        assert user.password_hash == "!" and not user.is_admin


def test_missing_turn_is_not_reported_as_a_stuck_previous_run(ingress, caplog):
    from finengine.assistant.binding import Binding, Refused
    from finengine.assistant.tool_call import ToolCall
    call = ToolCall.model_validate({"ctx":dict(owner_id="tenant", agent_id="main", chat_id="chat1", task_id="root", run_id="absent")})
    result = Binding(SimpleNamespace(root_known=lambda *a: True)).bind(call)
    assert isinstance(result, Refused)
    assert "register" in result.why.lower() or "зарегистрирован" in result.why.lower()
