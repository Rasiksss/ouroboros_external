from __future__ import annotations

import asyncio
import json

from ouroboros.storage.runtime_persistence import runtime_persistence
from tests.helpers.fake_gateway import RecordingMemoryEvents
from tests.helpers.parametrize import parametrize_from_dict
from transport.a2a.interfaces import IAgentSkillCatalog, ISupervisorReadiness


class _EmptyCatalog(IAgentSkillCatalog):
    """No live tools: the card falls back to its generic skill."""

    def skills(self):
        return []


class _ReadyNow(ISupervisorReadiness):
    def __init__(self):
        self.waits = 0

    async def wait_ready(self, *, timeout_sec: float) -> None:
        del timeout_sec
        self.waits += 1


class _StubTaskStore:
    async def get(self, task_id, context=None):
        del task_id, context
        return None


def _compat_executor(gateway, drive_root):
    from transport.a2a.delivery import A2AResponseRouter
    from transport.a2a.executor import OuroborosA2AExecutor

    return OuroborosA2AExecutor(
        max_concurrent=3,
        task_store=_StubTaskStore(),
        gateway=gateway,
        router=A2AResponseRouter(gateway.outbox),
        readiness=_ReadyNow(),
        session_id="sess-test",
        memory_events=RecordingMemoryEvents(),
        drive_root=drive_root,
    )


def _agent_card_service_url(card: object) -> str:
    direct = getattr(card, "url", "") or ""
    if direct:
        return str(direct)
    interfaces = getattr(card, "supported_interfaces", None) or []
    if interfaces:
        return str(getattr(interfaces[0], "url", "") or "")
    return ""


def _agent_card_payload_url(payload: dict[str, object]) -> str:
    direct = payload.get("url") or ""
    if direct:
        return str(direct)
    interfaces = payload.get("supportedInterfaces") or payload.get("supported_interfaces") or []
    if interfaces:
        first = interfaces[0]
        if isinstance(first, dict):
            return str(first.get("url") or "")
    return ""


def test_a2a_ingress_imports_with_installed_sdk():
    from transport.a2a import executor, proto, server, task_store

    assert executor._A2A_AVAILABLE is True
    assert server._A2A_AVAILABLE is True
    assert task_store._A2A_AVAILABLE is True

    message = proto.new_user_text_message("hello")
    assert executor.OuroborosA2AExecutor._extract_text(message) == "hello"


def test_a2a_agent_card_uses_installed_sdk_shape(tmp_path, monkeypatch):
    import ouroboros.app.config as config
    from transport.a2a.proto import task_state_name
    from transport.a2a.server import _build_agent_card

    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    card = _build_agent_card(
        {"A2A_AGENT_NAME": "CompatAgent", "A2A_AGENT_DESCRIPTION": "Compat description"},
        "127.0.0.1",
        18800,
        _EmptyCatalog(),
    )

    assert card.name == "CompatAgent"
    assert card.description == "Compat description"
    assert _agent_card_service_url(card) == "http://127.0.0.1:18800/"
    assert task_state_name("completed") == "completed"


@parametrize_from_dict({"defaults": {"configured": False}, "configured": {"configured": True}})
def test_a2a_starlette_app_serves_agent_card_without_memory(tmp_path, monkeypatch, configured):
    import inspect

    from a2a.server.request_handlers import DefaultRequestHandler
    from starlette.testclient import TestClient

    import ouroboros.app.config as config
    from tests.helpers.fake_gateway import FakeGateway
    from transport.a2a.server import _build_agent_card, _build_starlette_app
    from transport.a2a.task_store import RuntimeDbA2ATaskStore

    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    runtime_persistence.initialize()
    catalog = _EmptyCatalog()
    settings = {"A2A_AGENT_NAME": "CompatAgent", "A2A_AGENT_DESCRIPTION": "Compat description"} if configured else {}
    card = _build_agent_card(
        settings,
        "127.0.0.1",
        18800,
        catalog,
    )
    handler_kwargs = {
        "agent_executor": _compat_executor(FakeGateway(), tmp_path),
        "task_store": RuntimeDbA2ATaskStore(),
    }
    if "agent_card" in inspect.signature(DefaultRequestHandler).parameters:
        handler_kwargs["agent_card"] = card
    app = _build_starlette_app(card, DefaultRequestHandler(**handler_kwargs), catalog, max_chars=10)

    response = TestClient(app).get("/.well-known/agent-card.json")

    assert response.status_code == 200
    payload = response.json()
    assert payload["name"] == ("CompatAgent" if configured else "ГигаАгент")
    assert payload["description"] == ("Compat description" if configured else "Personal AI agent")
    assert _agent_card_payload_url(payload) == "http://127.0.0.1:18800/"


def test_a2a_starlette_app_accepts_message_send_at_limit(tmp_path, monkeypatch):
    import inspect

    from a2a.server.request_handlers import DefaultRequestHandler
    from starlette.testclient import TestClient

    from tests.helpers.fake_gateway import FakeGateway
    from transport.a2a.executor import OuroborosA2AExecutor
    from transport.a2a.server import _build_agent_card, _build_starlette_app
    from transport.a2a.task_store import RuntimeDbA2ATaskStore

    async def fake_dispatch(self, text, task_id, context_id, message_id=""):
        return f"echo: {text}"

    monkeypatch.setattr(OuroborosA2AExecutor, "_dispatch_to_supervisor", fake_dispatch)
    monkeypatch.setenv("OUROBOROS_MAX_INBOUND_MESSAGE_CHARS", "1")
    runtime_persistence.initialize()
    catalog = _EmptyCatalog()
    card = _build_agent_card(
        {"A2A_AGENT_NAME": "CompatAgent", "A2A_AGENT_DESCRIPTION": "Compat description"},
        "127.0.0.1",
        18800,
        catalog,
    )
    handler_kwargs = {
        "agent_executor": _compat_executor(FakeGateway(), tmp_path),
        "task_store": RuntimeDbA2ATaskStore(),
    }
    if "agent_card" in inspect.signature(DefaultRequestHandler).parameters:
        handler_kwargs["agent_card"] = card
    app = _build_starlette_app(card, DefaultRequestHandler(**handler_kwargs), catalog, max_chars=10)

    response = TestClient(app).post(
        "/",
        json={
            "jsonrpc": "2.0",
            "id": "compat-send",
            "method": "message/send",
            "params": {
                "message": {
                    "messageId": "m-compat",
                    "role": "user",
                    "parts": [{"kind": "text", "text": "я" * 10}],
                }
            },
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["id"] == "compat-send"
    assert payload["result"]["status"]["state"] == "completed"
    assert payload["result"]["artifacts"][0]["parts"][0]["text"] == "echo: " + "я" * 10


@parametrize_from_dict({"send": {"method": "message/send"}, "stream": {"method": "message/stream"}})
def test_oversized_a2a_request_returns_invalid_params_without_persistence(tmp_path, monkeypatch, caplog, method):
    # Exercise the optional SDK through its actual v0.3 JSON-RPC adapter.
    from a2a.server.request_handlers import DefaultRequestHandler
    from starlette.testclient import TestClient

    from tests.helpers.fake_gateway import FakeGateway
    from transport.a2a.proto import new_task_event
    from transport.a2a.server import _build_agent_card, _build_starlette_app
    from transport.a2a.task_store import RuntimeDbA2ATaskStore

    monkeypatch.setenv("OUROBOROS_MAX_INBOUND_MESSAGE_CHARS", "10")
    gateway = FakeGateway()
    gateway.inbox.fail_with(RuntimeError("oversized input reached the supervisor"))
    runtime_persistence.initialize()
    store = RuntimeDbA2ATaskStore()
    task = new_task_event("existing-task", "existing-context", "working")
    asyncio.run(store.save(task))
    before = runtime_persistence.current.tasks_repo.list_tasks()
    catalog = _EmptyCatalog()
    card = _build_agent_card({}, "localhost", 18800, catalog)
    handler = DefaultRequestHandler(
        agent_executor=_compat_executor(gateway, tmp_path), task_store=store, agent_card=card
    )
    app = _build_starlette_app(card, handler, catalog, max_chars=10)

    with TestClient(app) as client:
        response = client.post(
            "/",
            headers={"X-Trace-Id": "463ac35c-9f64-13ad-4848-5a3953bb6124"},
            json={
                "jsonrpc": "2.0",
                "id": "oversized",
                "method": method,
                "params": {
                    "message": {
                        "messageId": "too-long",
                        "role": "user",
                        "taskId": task.id,
                        "contextId": task.context_id,
                        "parts": [
                            {"kind": "text", "text": "x" * 10 + " "},
                            {"kind": "file", "file": {"bytes": "invalid-base64", "mimeType": "text/plain"}},
                        ],
                    }
                },
            },
        )

    assert response.status_code == 200
    payload = (
        json.loads(next(line[6:] for line in response.text.splitlines() if line.startswith("data: ")))
        if response.headers["content-type"].startswith("text/event-stream")
        else response.json()
    )
    assert payload["id"] == "oversized"
    assert response.headers["X-Trace-Id"] == "463ac35c-9f64-13ad-4848-5a3953bb6124"
    assert payload["error"]["code"] == -32602
    assert "Максимальная длина — 10 символов" in payload["error"]["message"]
    assert not any(record.exc_info for record in caplog.records)
    assert runtime_persistence.current.tasks_repo.list_tasks() == before
    assert gateway.submitted == []
