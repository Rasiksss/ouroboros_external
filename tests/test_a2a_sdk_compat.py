from __future__ import annotations


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
    from ouroboros.ingress import a2a_executor, a2a_proto, a2a_server, a2a_task_store

    assert a2a_executor._A2A_AVAILABLE is True
    assert a2a_server._A2A_AVAILABLE is True
    assert a2a_task_store._A2A_AVAILABLE is True

    message = a2a_proto.new_user_text_message("hello")
    assert a2a_executor.OuroborosA2AExecutor._extract_text(message) == "hello"


def test_a2a_agent_card_uses_installed_sdk_shape(tmp_path, monkeypatch):
    import ouroboros.app.config as config
    from ouroboros.ingress.a2a_proto import task_state_name
    from ouroboros.ingress.a2a_server import _build_agent_card

    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    card = _build_agent_card(
        {"A2A_AGENT_NAME": "CompatAgent", "A2A_AGENT_DESCRIPTION": "Compat description"},
        "127.0.0.1",
        18800,
        "",
    )

    assert card.name == "CompatAgent"
    assert card.description == "Compat description"
    assert _agent_card_service_url(card) == "http://127.0.0.1:18800/"
    assert task_state_name("completed") == "completed"


def test_a2a_starlette_app_serves_agent_card(tmp_path, monkeypatch):
    import inspect

    from a2a.server.request_handlers import DefaultRequestHandler
    from starlette.testclient import TestClient

    import ouroboros.app.config as config
    from ouroboros.ingress.a2a_executor import OuroborosA2AExecutor
    from ouroboros.ingress.a2a_server import _build_agent_card, _build_starlette_app
    from ouroboros.ingress.a2a_task_store import FileTaskStore

    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    card = _build_agent_card(
        {"A2A_AGENT_NAME": "CompatAgent", "A2A_AGENT_DESCRIPTION": "Compat description"},
        "127.0.0.1",
        18800,
        "",
    )
    handler_kwargs = {
        "agent_executor": OuroborosA2AExecutor(),
        "task_store": FileTaskStore(tmp_path),
    }
    if "agent_card" in inspect.signature(DefaultRequestHandler).parameters:
        handler_kwargs["agent_card"] = card
    app = _build_starlette_app(card, DefaultRequestHandler(**handler_kwargs))

    response = TestClient(app).get("/.well-known/agent-card.json")

    assert response.status_code == 200
    payload = response.json()
    assert payload["name"] == "CompatAgent"
    assert _agent_card_payload_url(payload) == "http://127.0.0.1:18800/"


def test_a2a_starlette_app_accepts_message_send(tmp_path, monkeypatch):
    import inspect

    from a2a.server.request_handlers import DefaultRequestHandler
    from starlette.testclient import TestClient

    from ouroboros.ingress.a2a_executor import OuroborosA2AExecutor
    from ouroboros.ingress.a2a_server import _build_agent_card, _build_starlette_app
    from ouroboros.ingress.a2a_task_store import FileTaskStore

    async def fake_dispatch(self, text, event_queue, task_id, context_id, message_id=""):
        return f"echo: {text}"

    monkeypatch.setattr(OuroborosA2AExecutor, "_dispatch_to_supervisor", fake_dispatch)
    card = _build_agent_card(
        {"A2A_AGENT_NAME": "CompatAgent", "A2A_AGENT_DESCRIPTION": "Compat description"},
        "127.0.0.1",
        18800,
        "",
    )
    handler_kwargs = {
        "agent_executor": OuroborosA2AExecutor(),
        "task_store": FileTaskStore(tmp_path),
    }
    if "agent_card" in inspect.signature(DefaultRequestHandler).parameters:
        handler_kwargs["agent_card"] = card
    app = _build_starlette_app(card, DefaultRequestHandler(**handler_kwargs))

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
                    "parts": [{"kind": "text", "text": "hello"}],
                }
            },
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["id"] == "compat-send"
    assert payload["result"]["status"]["state"] == "completed"
    assert payload["result"]["artifacts"][0]["parts"][0]["text"] == "echo: hello"
