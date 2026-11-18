import asyncio
import importlib
import json
import logging
from types import SimpleNamespace

import httpx
import pytest


def test_logging_writes_json_to_stdout_without_duplicate_handlers(monkeypatch, capsys):
    diagnostics = importlib.import_module("finengine.diagnostics")
    logger = logging.getLogger("finengine")
    monkeypatch.setattr(logger, "handlers", [])
    monkeypatch.setattr(logger, "level", logger.level)
    monkeypatch.setattr(logger, "propagate", logger.propagate)
    for name in ("openai", "httpx", "httpcore"):
        library = logging.getLogger(name)
        monkeypatch.setattr(library, "level", library.level)
    monkeypatch.setenv("FINENGINE_LOG_LEVEL", "INFO")
    diagnostics.configure_logging()
    diagnostics.configure_logging()
    logger.info("diagnostic marker")
    try:
        raise RuntimeError("secret exception body")
    except RuntimeError:
        logger.exception("diagnostic failure")
    records = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert len(logger.handlers) == 1
    assert sum(record["message"] == "diagnostic marker" for record in records) == 1
    failure = next(record for record in records if record["message"] == "diagnostic failure")
    assert failure["error_type"] == "RuntimeError" and failure["stack"]
    assert "secret exception body" not in json.dumps(records)


def test_request_logging_preserves_streaming_and_omits_sensitive_input(caplog):
    diagnostics = importlib.import_module("finengine.diagnostics")
    sent = []

    async def application(scope, receive, send):
        scope["route"] = SimpleNamespace(path="/documents/{doc_id}")
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"first", "more_body": True})
        assert len(sent) == 2
        await send({"type": "http.response.body", "body": b"second", "more_body": False})

    async def receive():
        raise AssertionError("Logging must not consume request bodies")

    async def send(message):
        sent.append(message)

    scope = {"type": "http", "method": "POST", "path": "/documents/private-name",
             "query_string": b"token=secret-query", "headers": [(b"authorization", b"secret-token")]}
    with caplog.at_level(logging.INFO):
        asyncio.run(diagnostics.RequestLoggingMiddleware(application)(scope, receive, send))
    assert [message.get("body") for message in sent[1:]] == [b"first", b"second"]
    assert "event=http_complete" in caplog.text
    assert "/documents/{doc_id}" in caplog.text
    assert "secret" not in caplog.text
    assert "private-name" not in caplog.text


def test_outbound_http_logs_status_without_credentials_or_payload(caplog):
    diagnostics = importlib.import_module("finengine.diagnostics")

    async def run():
        async with httpx.AsyncClient(
            event_hooks=diagnostics.http_event_hooks(),
            transport=httpx.MockTransport(lambda request: httpx.Response(502, text="secret-response")),
        ) as client:
            await client.post("http://user:secret-password@service/api/extract?token=secret-query",
                              content=b"secret-document")

    with caplog.at_level(logging.INFO, logger="finengine"):
        asyncio.run(run())
    own_logs = "\n".join(record.getMessage() for record in caplog.records
                         if record.name.startswith("finengine"))
    assert "event=http_out_response" in own_logs and "status=502" in own_logs
    assert "http://service/api/extract" in own_logs
    assert "secret" not in own_logs


def test_failed_operation_logs_location_and_preserves_original_error(caplog):
    diagnostics = importlib.import_module("finengine.diagnostics")
    failure = ValueError("secret provider body")
    with caplog.at_level(logging.INFO), pytest.raises(ValueError) as caught:
        with diagnostics.operation(logging.getLogger("finengine.test"), "ocr", pages=3):
            raise failure
    assert caught.value is failure
    assert "event=ocr_failed" in caplog.text
    assert "error_type=ValueError" in caplog.text
    assert "test_failed_operation" in caplog.text
    assert "secret provider body" not in caplog.text


def test_llm_attempts_log_retry_and_success_without_document_text(monkeypatch, caplog):
    module = importlib.import_module("finengine.llm.openrouter")
    calls = []

    async def create(**kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            raise RuntimeError("secret provider body")
        return SimpleNamespace(choices=[SimpleNamespace(
            message=SimpleNamespace(content="secret answer"), finish_reason="stop"
        )], usage=None)

    async def close():
        pass

    async def sleep(delay):
        pass

    monkeypatch.setattr(module, "_client", lambda: SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=create)), close=close
    ))
    monkeypatch.setattr(module.asyncio, "sleep", sleep)
    with caplog.at_level(logging.INFO):
        text, usage = asyncio.run(module.complete_with_usage("secret document", model="glm-test", retries=2))
    assert text == "secret answer" and len(calls) == 2
    assert "event=llm_attempt_failed" in caplog.text
    assert "event=llm_attempt_complete" in caplog.text
    assert "attempt=2" in caplog.text
    assert "secret" not in caplog.text


def test_silent_model_does_not_log_reasoning_content(monkeypatch, caplog):
    module = importlib.import_module("finengine.llm.openrouter")

    async def create(**kwargs):
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(
            content="", reasoning="secret document in reasoning"
        ), finish_reason="length")], usage=None)

    monkeypatch.setattr(module, "_client", lambda: SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=create))
    ))
    with caplog.at_level(logging.INFO), pytest.raises(module.ModelSilent):
        asyncio.run(module.complete_with_usage("secret prompt", model="glm-test", retries=1))
    assert "event=llm_attempt_failed" in caplog.text
    assert "secret" not in caplog.text
