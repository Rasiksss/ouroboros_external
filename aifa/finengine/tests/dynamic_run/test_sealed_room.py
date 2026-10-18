"""Клиент комнаты станции (`sealed_room.run_code`) на подставном HTTP-сервисе."""
from __future__ import annotations

import base64
import json
import os
import re
from dataclasses import dataclass, field

import httpx
import pytest

from finengine.config import settings
from finengine.skills.spec import sealed_room
from finengine.skills.spec.sealed_room import CodeRun, run_code

STATION_URL = "http://station.test"
_OK_STDOUT = json.dumps({"total": 6})

requires_room = pytest.mark.skipif(
    not os.environ.get("FINENGINE_WORKSTATION_URL"),
    reason="FINENGINE_WORKSTATION_URL не задан: станция недоступна",
)


@dataclass
class Station:
    """Подставной сервис Workstation: пишет запросы, отвечает по заданным статусам."""

    exec_reply: dict = field(default_factory=lambda: {
        "exitCode": 0, "stdout": _OK_STDOUT, "stderr": "", "timedOut": False,
    })
    create_status: int = 200
    exec_status: int = 200
    create_body: dict | None = None      # None — штатный {"token": ...}
    exec_raw: bytes | None = None        # не None — exec отвечает 200 с этим телом
    requests: list[httpx.Request] = field(default_factory=list)

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        if path == "/v1/sessions":
            body = self.create_body
            if body is None:
                body = {"token": "room-token"} if self.create_status == 200 else {"error": "занято"}
            return httpx.Response(self.create_status, json=body)
        if path.endswith("/exec"):
            if self.exec_raw is not None:
                return httpx.Response(200, content=self.exec_raw)
            return httpx.Response(self.exec_status, json=self.exec_reply)
        if request.headers.get("Authorization") != "Bearer room-token":
            return httpx.Response(401, json={"code": "auth-required"})   # как настоящая станция
        return httpx.Response(200, json={"status": "terminated"})

    def steps(self) -> list[str]:
        return [_step(request) for request in self.requests]

    def exec_body(self) -> dict:
        exec_request = next(r for r in self.requests if r.url.path.endswith("/exec"))
        return json.loads(exec_request.content)


def _step(request: httpx.Request) -> str:
    path = request.url.path
    if path == "/v1/sessions":
        return "create"
    return "terminate" if path.endswith(":terminate") else "exec"


@pytest.fixture(autouse=True)
def _station_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "workstation_url", STATION_URL)


def _run(station: Station, code: str = "код", inputs: dict | None = None, **kwargs) -> CodeRun:
    return run_code(code, inputs or {}, transport=httpx.MockTransport(station), **kwargs)


def test_calls_go_create_exec_terminate_and_result_is_parsed() -> None:
    station = Station()

    result = _run(station)

    assert station.steps() == ["create", "exec", "terminate"]
    assert result == CodeRun({"total": 6}, None)


def test_exec_body_carries_entry_point_and_base64_stdin() -> None:
    station = Station()

    _run(station, code="def run(inputs): return {}", inputs={"xs": [1, 2], "имя": "выручка"}, timeout=42)

    body = station.exec_body()
    assert body["command"] == ["python3", "-I", "-c", sealed_room._ENTRY]
    assert body["cwd"] == "/workspace"
    assert body["env"] == {}
    assert body["timeoutSec"] == 42
    assert body["stdinEncoding"] == "base64"
    assert json.loads(base64.b64decode(body["stdin"])) == {
        "code": "def run(inputs): return {}", "inputs": {"xs": [1, 2], "имя": "выручка"},
    }


def test_exec_carries_room_token() -> None:
    station = Station()

    _run(station)

    exec_request = station.requests[1]
    assert exec_request.headers["Authorization"] == "Bearer room-token"


def test_terminate_carries_room_token_or_the_station_refuses_with_401() -> None:
    """Без Bearer станция отвечает 401 и комната живёт до idleTimeoutSec (проверено на dev)."""
    station = Station()

    _run(station)

    terminate = station.requests[2]
    assert terminate.headers["Authorization"] == "Bearer room-token"


def test_one_room_per_run_with_same_id_in_all_its_calls() -> None:
    first, second = Station(), Station()

    _run(first)
    _run(second)

    ids_first = {r.headers["X-Session-Id"] for r in first.requests}
    ids_second = {r.headers["X-Session-Id"] for r in second.requests}
    assert len(ids_first) == 1 and len(ids_second) == 1
    assert ids_first != ids_second
    assert all(re.fullmatch(r"aifa-eph-[0-9a-f]{16}", i) for i in ids_first | ids_second)  # C5.1, ADR-0007
    exec_path = first.requests[1].url.path
    assert exec_path == f"/v1/sessions/{next(iter(ids_first))}/exec"
    assert first.requests[2].url.path == f"/v1/sessions/{next(iter(ids_first))}:terminate"


def test_room_is_closed_when_exec_fails_with_http_error() -> None:
    station = Station(exec_status=500)

    result = _run(station)

    assert station.steps() == ["create", "exec", "terminate"]
    assert result.output is None
    assert result.error.startswith("станция не исполнила код:")


def test_http_409_is_an_error_value_not_an_exception() -> None:
    station = Station(create_status=409)

    result = _run(station)

    assert result.output is None
    assert "409" in result.error
    assert station.steps() == ["create"]      # комнаты нет, токена нет — закрывать нечего


def test_reply_without_token_is_an_error_value() -> None:
    station = Station(create_body={"id": "x"})

    result = _run(station)

    assert result.output is None
    assert result.error.startswith("станция не исполнила код:")
    assert station.steps() == ["create"]


def test_exec_reply_that_is_not_json_is_an_error_value_and_room_is_closed() -> None:
    station = Station(exec_raw=b"<html>proxy error</html>")

    result = _run(station)

    assert result.output is None
    assert result.error.startswith("станция не исполнила код:")
    assert station.steps() == ["create", "exec", "terminate"]


def test_timeout_flag_is_an_error() -> None:
    station = Station(exec_reply={"exitCode": -1, "stdout": "", "stderr": "", "timedOut": True})

    assert _run(station, timeout=7) == CodeRun(None, "код работал дольше 7 с")


def test_nonzero_exit_code_returns_stderr_tail() -> None:
    stderr = "начало\n" + "x" * 5000 + "\nValueError: конец"
    station = Station(exec_reply={"exitCode": 1, "stdout": "", "stderr": stderr, "timedOut": False})

    result = _run(station)

    assert result.output is None
    assert len(result.error) == 2000
    assert result.error.endswith("ValueError: конец")


def test_nonzero_exit_code_without_stderr_names_the_code() -> None:
    station = Station(exec_reply={"exitCode": 137, "stdout": "", "stderr": "", "timedOut": False})

    assert _run(station) == CodeRun(None, "код завершился с кодом 137")


def test_output_over_limit_is_an_error() -> None:
    big = json.dumps({"x": "a" * sealed_room.MAX_OUTPUT_CHARS})
    station = Station(exec_reply={"exitCode": 0, "stdout": big, "stderr": "", "timedOut": False})

    assert _run(station) == CodeRun(None, "результат больше 1 МБ")


def test_stdout_that_is_not_json_is_an_error() -> None:
    station = Station(exec_reply={"exitCode": 0, "stdout": "не json", "stderr": "", "timedOut": False})

    assert _run(station) == CodeRun(None, "комната вернула не JSON")


@pytest.mark.parametrize("stdout", ['{"x": NaN}', '{"x": Infinity}', '[1, 2]', '"текст"', "42"])
def test_stdout_with_non_finite_numbers_or_not_an_object_is_an_error(stdout: str) -> None:
    """Точка входа не даёт NaN и не-dict, но код атома пишет в fd 1 в обход неё
    (os.write + os._exit): последнюю границу держит клиент, а не точка входа."""
    station = Station(exec_reply={"exitCode": 0, "stdout": stdout, "stderr": "", "timedOut": False})

    result = _run(station)

    assert result.output is None
    assert result.error


def test_atom_cannot_smuggle_nan_past_the_entry_point(room_entry) -> None:
    """Сквозной случай на настоящей точке входа: код пишет результат мимо `json.dumps`."""
    code = "import os\ndef run(inputs):\n    os.write(1, b'{\"x\": NaN}')\n    os._exit(0)\n"
    done = room_entry({"code": code, "inputs": {}})
    reply = {"exitCode": done.returncode, "stdout": done.stdout.decode("utf-8"),
             "stderr": done.stderr.decode("utf-8"), "timedOut": False}

    result = sealed_room._read_reply(reply, 60)

    assert result.output is None


def test_empty_station_url_is_an_error_without_network(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "workstation_url", "")
    station = Station()

    result = _run(station)

    assert result == CodeRun(
        None, "комната станции не настроена: FINENGINE_WORKSTATION_URL пуст", station_fault=True,
    )
    assert station.requests == []


@pytest.fixture
def live_station(monkeypatch: pytest.MonkeyPatch) -> None:
    """Возвращает настоящий адрес станции поверх подмены из `_station_url`."""
    monkeypatch.setattr(settings, "workstation_url", os.environ["FINENGINE_WORKSTATION_URL"])


@requires_room
def test_live_room_runs_code_and_reports_errors(live_station) -> None:
    ok = run_code("def run(inputs):\n    return {'total': sum(inputs['xs'])}\n", {"xs": [1, 2, 3]})
    broken = run_code("def run(inputs):\n    raise ValueError('boom')\n", {})

    assert ok == CodeRun({"total": 6}, None)
    assert broken.output is None and "ValueError: boom" in broken.error


@requires_room
def test_live_room_has_no_network(live_station) -> None:
    code = (
        "import socket\n"
        "def run(inputs):\n"
        "    socket.create_connection(('1.1.1.1', 80), timeout=3)\n"
        "    return {'reached': True}\n"
    )

    result = run_code(code, {})

    assert result.output is None
    assert "PermissionError" in result.error     # seccomp станции, а не сбой связи с ней


def test_station_failures_are_marked_as_station_fault() -> None:
    """Автор атомов не должен «чинить код» из-за сбоя станции."""
    for station in (Station(exec_status=500), Station(create_status=409),
                    Station(create_body={"id": "x"}), Station(exec_raw=b"<html>")):
        assert _run(station).station_fault is True


def test_code_failures_are_not_station_fault() -> None:
    replies = [
        {"exitCode": 1, "stdout": "", "stderr": "ValueError: x", "timedOut": False},
        {"exitCode": -1, "stdout": "", "stderr": "", "timedOut": True},
        {"exitCode": 0, "stdout": "не json", "stderr": "", "timedOut": False},
        {"exitCode": 0, "stdout": '{"x": NaN}', "stderr": "", "timedOut": False},
        {"exitCode": 0, "stdout": json.dumps({"x": "a" * sealed_room.MAX_OUTPUT_CHARS}),
         "stderr": "", "timedOut": False},
    ]
    for reply in replies:
        assert _run(Station(exec_reply=reply)).station_fault is False


def test_successful_run_is_not_station_fault() -> None:
    assert _run(Station()).station_fault is False


def test_every_call_names_the_protocol_version() -> None:
    """C5.1 (ADR-0007): `X-Workstation-Protocol: 1` на создании, исполнении, файлах и закрытии комнаты."""
    station = Station()

    _run(station, collect_files=True)

    assert station.steps()[0] == "create" and station.steps()[-1] == "terminate"
    assert {r.headers["X-Workstation-Protocol"] for r in station.requests} == {"1"}


def test_the_create_secret_goes_with_the_creation_only(monkeypatch: pytest.MonkeyPatch) -> None:
    """C5.1: секрет создания сессии — только в создании комнаты и только если задан."""
    monkeypatch.setattr(settings, "workstation_create_secret", "room-secret")
    station = Station()

    _run(station, collect_files=True)

    create, *others = station.requests
    assert create.headers["X-Session-Create-Secret"] == "room-secret"
    assert all("X-Session-Create-Secret" not in r.headers for r in others)


def test_without_a_create_secret_the_header_is_absent() -> None:
    station = Station()

    _run(station)

    assert all("X-Session-Create-Secret" not in r.headers for r in station.requests)


def test_the_trace_of_the_turn_goes_with_every_call() -> None:
    """C5.1: `X-Trace-Id` хода — на каждом вызове; нет трассы — нет заголовка (не пустое значение)."""
    traced, untraced = Station(), Station()

    _run(traced, collect_files=True, trace_id="9c1e7a52-0d3b-4e8a-9a41-6f2b7d3c5e10")
    _run(untraced)

    assert {r.headers["X-Trace-Id"] for r in traced.requests} == {"9c1e7a52-0d3b-4e8a-9a41-6f2b7d3c5e10"}
    assert all("X-Trace-Id" not in r.headers for r in untraced.requests)


@requires_room
def test_live_room_has_the_packages_and_no_service_environment(live_station) -> None:
    """C5.2: пакеты комнаты импортируются, переменных окружения службы нет."""
    code = (
        "import os\n"
        "def run(inputs):\n"
        "    import numpy, pandas, openpyxl, pptx, docx\n"
        "    return {'env': sorted(k for k in os.environ if k.startswith('FINENGINE')), 'ok': True}\n"
    )

    result = run_code(code, {})

    assert result == CodeRun({"env": [], "ok": True}, None)
