import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from pydantic import SecretStr


@pytest.fixture
def module():
    from transport.dialogs import aifa_ingress
    return aifa_ingress


def settings(**changes):
    return SimpleNamespace(**({"AIFA_ENGINE_URL": "http://aifa:8080", "AIFA_ENGINE_TOKEN": SecretStr("test-secret"),
                                  "OUROBOROS_TRUSTED_EXECUTABLE_SKILL_IDS": ["distribution:aifa-engine"]} | changes))


def test_registration_streams_scoped_attachment_and_passes_run_id(module, monkeypatch):
    from new_core.identity import OwnerScope, SurfaceId
    scope = OwnerScope(owner_id="tenant", agent_id="main", surface_id=SurfaceId.DIALOGS, chat_id="chat1")
    requests = []
    def respond(request):
        requests.append(request)
        assert request.headers["authorization"] == "Bearer test-secret"
        assert request.url.path == "/internal/dialogs/v1/turns"
        assert b"%PDF-1.7" in request.content
        assert b'"source_message_id": "msg1"' in request.content
        return httpx.Response(200, json={"run_id": "registered-run"})
    original = httpx.Client
    monkeypatch.setattr(module.httpx, "Client", lambda **kwargs: original(transport=httpx.MockTransport(respond), **kwargs))
    copied = []
    def copy(owner, storage_scope, key, path, *, max_bytes):
        copied.append((owner, storage_scope, key, max_bytes))
        path.write_bytes(b"%PDF-1.7\nexample")
        return 17
    storage = SimpleNamespace(copy_to_path=copy)
    attachment = {"id": "file1", "filename": "report.pdf", "mime": "application/pdf", "storage_key": "chat/file.pdf",
                      "owner_id": "tenant", "agent_id": "main", "chat_id": "chat1", "surface_id": "dialogs"}
    result = module.register_turn(settings(), scope, "msg1", "Extract", [attachment], storage)
    assert result == "registered-run" and len(requests) == len(copied) == 1
    assert copied[0][0] == scope and copied[0][1] == "uploads"


def test_null_url_and_http_failure_do_not_submit_a_run(module, monkeypatch):
    from new_core.identity import OwnerScope, SurfaceId
    scope = OwnerScope(owner_id="tenant", agent_id="main", surface_id=SurfaceId.DIALOGS, chat_id="chat1")
    with pytest.raises(RuntimeError, match="AIFA_ENGINE_URL"):
        module.register_turn(settings(AIFA_ENGINE_URL="null"), scope, "msg1", "Extract", [], None)
    original = httpx.Client
    monkeypatch.setattr(module.httpx, "Client", lambda **kwargs: original(transport=httpx.MockTransport(lambda r: httpx.Response(503)), **kwargs))
    with pytest.raises(httpx.HTTPStatusError):
        module.register_turn(settings(), scope, "msg1", "Extract", [], None)


def test_generic_dialogs_bot_does_not_contact_aifa(module):
    assert module.register_turn(settings(OUROBOROS_TRUSTED_EXECUTABLE_SKILL_IDS=[]), None, "msg1", "Hello", [], None) == ""


def test_plugin_uses_registered_id_for_root_and_subtask():
    path = Path(__file__).resolve().parents[1] / "skills/aifa-engine/plugin.py"
    spec = importlib.util.spec_from_file_location("test_dialogs_engine_plugin", path)
    plugin = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = plugin
    spec.loader.exec_module(plugin)
    ctx = SimpleNamespace(scope=SimpleNamespace(owner_id="tenant", agent_id="main", chat_id="chat1"),
                          task_id="root", root_task_id="", task_depth=0, tool_call_id="call1",
                          transport={"source_message_id":"dialogs-msg", "aifa_run_id":"registered-run"})
    assert plugin.context_of(ctx)["run_id"] == "registered-run"
    ctx.task_id, ctx.root_task_id, ctx.task_depth = "child", "root", 1
    assert plugin.context_of(ctx)["run_id"] == "registered-run"
    del ctx.transport["aifa_run_id"]
    assert plugin.context_of(ctx)["run_id"] == "dialogs-msg"


@pytest.mark.parametrize("fail", [False, True])
def test_inbox_is_reached_only_after_successful_registration(monkeypatch, fail):
    from new_core.identity import OwnerScope, SurfaceId
    from transport.dialogs import transport
    scope = OwnerScope(owner_id="tenant", agent_id="main", surface_id=SurfaceId.DIALOGS, chat_id="chat1")
    events = []
    def register(*args):
        events.append("registration")
        if fail:
            raise RuntimeError("registration failed")
        return "registered-run"
    monkeypatch.setattr(transport, "register_aifa_turn", register)
    envelopes = []
    def submit(envelope):
        events.append("inbox")
        envelopes.append(envelope)
    receiver = SimpleNamespace(settings=settings(), _storage=None, drive_root=Path("."),
        _scope_for_chat=lambda _: scope, gateway=SimpleNamespace(inbox=SimpleNamespace(submit=submit)))
    message = SimpleNamespace(source_message_id="msg1", sender_session_id="sender", chat_id="chat1",
                              user_id="user", history_only=False, sender_label="Sender")
    kwargs = {"text":"Extract", "transport":{"source_message_id":"msg1"}, "attachment_ids":[],
              "attachments":[], "persisted":SimpleNamespace(metadata={}, message_id=""), "session_id":"session"}
    if fail:
        with pytest.raises(RuntimeError, match="registration failed"):
            transport.DialogsTransport._commit_inbound(receiver, message, **kwargs)
        assert events == ["registration"] and not envelopes
    else:
        transport.DialogsTransport._commit_inbound(receiver, message, **kwargs)
        assert events == ["registration", "inbox"]
        assert envelopes[0].transport["aifa_run_id"] == "registered-run"


def test_journal_replay_also_registers_before_submitting(monkeypatch):
    from transport.dialogs import transport
    monkeypatch.setattr(transport, "stored_attachments", lambda record: ([], []))
    calls = []
    monkeypatch.setattr(transport, "register_aifa_turn", lambda *args: calls.append(args) or "restored-run")
    record = SimpleNamespace(owner_id="tenant", agent_id="main", chat_id="chat1", content="Extract",
                             source_message_id="msg1", metadata={"transport":{"source_message_id":"msg1"}})
    envelope = transport._DialogsEnvelopeBuilder(settings(), None).build(record)
    assert len(calls) == 1 and envelope.transport["aifa_run_id"] == "restored-run"
