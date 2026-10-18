"""Подставной OpenAI-совместимый сервер модели (ARCHITECTURE.md §2, шаг 07).

Отвечает по сценарию (`scenario.py`), ведёт журнал запросов. Состояния между запросами нет, кроме
журнала и текущего сценария: один и тот же запрос получает один и тот же ответ. Только стандартная
библиотека. Потоковый режим не поддержан намеренно: клиент ouroboros его не использует
(`ouroboros/llm/providers/openai_compat.py` зовёт `chat.completions.create` без `stream`).

Рабочие пути: `POST /v1/chat/completions`, `GET /v1/models`, `GET /health`.
Служебные (для тестов): `PUT /_scenario`, `GET /_journal`, `DELETE /_journal`.
"""

from __future__ import annotations

import json
import sys
import threading
from dataclasses import dataclass, field
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from stub_model.scenario import (
    Reply,
    RequestView,
    Scenario,
    ScenarioError,
    parse_scenario,
    request_view,
)

CHARS_PER_TOKEN = 4  # грубая и детерминированная оценка usage: настоящей модели нет
SNIPPET_CHARS = 200


@dataclass
class StubState:
    """Сценарий и журнал; меняются из потоков обработчика, поэтому под замком."""

    scenario: Scenario | None
    journal: list[dict[str, Any]] = field(default_factory=list)
    lock: threading.Lock = field(default_factory=threading.Lock)

    def record(self, view: RequestView, payload: dict[str, Any], rule_id: str | None) -> int:
        with self.lock:
            seq = len(self.journal) + 1
            tools = [
                str((tool.get("function") or {}).get("name", ""))
                for tool in payload.get("tools") or []
                if isinstance(tool, dict)
            ]
            self.journal.append(
                {
                    "seq": seq,
                    "model": view.model,
                    "round": view.round,
                    "user": view.user,
                    "last_tool_result": view.last_tool_result,
                    "tools": tools,
                    "messages": payload.get("messages") or [],
                    "rule": rule_id,
                }
            )
            return seq


class _Httpd(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address: tuple[str, int], state: StubState) -> None:
        super().__init__(address, _Handler)
        self.state = state


def _error_body(message: str, kind: str) -> dict[str, Any]:
    return {"error": {"message": message, "type": kind, "code": None}}


def _snippet(text: str) -> str:
    return text if len(text) <= SNIPPET_CHARS else text[:SNIPPET_CHARS] + "…"


def _no_rule_message(scenario: Scenario, view: RequestView) -> str:
    return (
        f"stub-model: ни одно правило сценария {scenario.name!r} не подошло. "
        f"Запрос: model={view.model!r}, круг={view.round}, "
        f"реплика пользователя={_snippet(view.user)!r}, "
        f"последний результат инструмента={_snippet(view.last_tool_result)!r}. "
        f"Правила: {[rule.id for rule in scenario.rules]}"
    )


def completion_body(
    payload: dict[str, Any], view: RequestView, reply: Reply, seq: int
) -> dict[str, Any]:
    """Ответ `chat.completion`; идентификаторы вызовов детерминированы порядком запросов."""
    message: dict[str, Any] = {"role": "assistant", "content": reply.text}
    if reply.tool_calls:
        message["tool_calls"] = [
            {
                "id": f"call_{seq}_{index}",
                "type": "function",
                "function": {
                    "name": call.name,
                    "arguments": json.dumps(call.arguments, ensure_ascii=False),
                },
            }
            for index, call in enumerate(reply.tool_calls)
        ]
    prompt = len(json.dumps(payload.get("messages") or [], ensure_ascii=False)) // CHARS_PER_TOKEN
    completion = len(json.dumps(message, ensure_ascii=False)) // CHARS_PER_TOKEN
    return {
        "id": f"chatcmpl-stub-{seq}",
        "object": "chat.completion",
        "created": 0,
        "model": view.model,
        "choices": [
            {
                "index": 0,
                "message": message,
                "finish_reason": "tool_calls" if reply.tool_calls else "stop",
            }
        ],
        "usage": {
            "prompt_tokens": prompt,
            "completion_tokens": completion,
            "total_tokens": prompt + completion,
        },
    }


class _Handler(BaseHTTPRequestHandler):
    server: _Httpd

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 — сигнатура базового класса
        pass  # по строке на запрос пишем сами, с правилом (см. _chat)

    def _send(self, status: int, body: Any) -> None:
        raw = json.dumps(body, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _read_json(self) -> Any:
        length = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(length) or b"null")

    def do_GET(self) -> None:  # noqa: N802 — имя задано базовым классом
        state = self.server.state
        if self.path == "/health":
            self._send(HTTPStatus.OK, {"status": "ok"})
        elif self.path in ("/v1/models", "/models"):
            self._send(HTTPStatus.OK, {"object": "list", "data": []})
        elif self.path == "/_journal":
            with state.lock:
                self._send(HTTPStatus.OK, list(state.journal))
        else:
            self._not_found()

    def do_DELETE(self) -> None:  # noqa: N802
        if self.path != "/_journal":
            return self._not_found()
        with self.server.state.lock:
            self.server.state.journal.clear()
        self._send(HTTPStatus.OK, {"cleared": True})

    def do_PUT(self) -> None:  # noqa: N802
        if self.path != "/_scenario":
            return self._not_found()
        try:
            scenario = parse_scenario(self._read_json())
        except (ScenarioError, json.JSONDecodeError) as error:
            return self._send(HTTPStatus.BAD_REQUEST, _error_body(str(error), "stub_bad_scenario"))
        with self.server.state.lock:
            self.server.state.scenario = scenario
        self._send(HTTPStatus.OK, {"scenario": scenario.name, "rules": len(scenario.rules)})

    def do_POST(self) -> None:  # noqa: N802
        if self.path not in ("/v1/chat/completions", "/chat/completions"):
            return self._not_found()
        try:
            payload = self._read_json()
        except json.JSONDecodeError as error:
            return self._send(
                HTTPStatus.BAD_REQUEST,
                _error_body(f"stub-model: не JSON ({error})", "stub_bad_request"),
            )
        if not isinstance(payload, dict):
            return self._send(
                HTTPStatus.BAD_REQUEST,
                _error_body("stub-model: тело запроса — объект", "stub_bad_request"),
            )
        self._chat(payload)

    def _chat(self, payload: dict[str, Any]) -> None:
        state = self.server.state
        view = request_view(payload)
        with state.lock:
            scenario = state.scenario
        rule = scenario.pick(view) if scenario else None
        seq = state.record(view, payload, rule.id if rule else None)
        print(
            f"stub-model #{seq} model={view.model} круг={view.round} "
            f"правило={rule.id if rule else None}",
            file=sys.stderr,
            flush=True,
        )
        if view.stream:
            return self._send(
                HTTPStatus.BAD_REQUEST,
                _error_body(
                    "stub-model: stream=true не поддержан — клиент ouroboros его не использует",
                    "stub_stream",
                ),
            )
        if scenario is None:
            return self._send(
                HTTPStatus.BAD_REQUEST,
                _error_body(
                    "stub-model: сценарий не загружен (PUT /_scenario)", "stub_no_scenario"
                ),
            )
        if rule is None:
            return self._send(
                HTTPStatus.BAD_REQUEST,
                _error_body(_no_rule_message(scenario, view), "stub_no_rule"),
            )
        if rule.reply.error:
            return self._send(
                rule.reply.error.status,
                _error_body(rule.reply.error.message, "stub_scripted_error"),
            )
        self._send(HTTPStatus.OK, completion_body(payload, view, rule.reply, seq))

    def _not_found(self) -> None:
        print(
            f"stub-model: нет такого пути {self.command} {self.path}", file=sys.stderr, flush=True
        )
        self._send(
            HTTPStatus.NOT_FOUND,
            _error_body(f"stub-model: нет пути {self.command} {self.path}", "stub_not_found"),
        )


class StubModelServer:
    """Сервер в фоновом потоке: для тестов и для `python -m stub_model`."""

    def __init__(self, httpd: _Httpd, thread: threading.Thread) -> None:
        self._httpd = httpd
        self._thread = thread

    @classmethod
    def start(cls, host: str, port: int, scenario: Scenario | None) -> StubModelServer:
        httpd = _Httpd((host, port), StubState(scenario))
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        return cls(httpd, thread)

    @property
    def url(self) -> str:
        host, port = self._httpd.server_address[:2]
        return f"http://{host!s}:{port}"

    def wait(self) -> None:
        self._thread.join()

    def stop(self) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()
        self._thread.join(timeout=5)
