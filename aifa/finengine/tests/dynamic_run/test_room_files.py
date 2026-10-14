"""К16.2: файлы, которые код атома записал в `out/`, клиент комнаты забирает до закрытия комнаты (часть 16)."""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field

import httpx
import pytest

from finengine.config import settings
from finengine.skills.spec import sealed_room
from finengine.skills.spec.sealed_room import CodeRun, RoomFile, run_code

STATION_URL = "http://station.test"
TOKEN = "room-token"


def _node(name: str, content: bytes | None = None, *, kind: str = "file", children=(), sha: str | None = None) -> dict:
    return {
        "name": name, "path": f"/workspace/out/{name}", "kind": kind,
        "bytes": None if content is None else len(content),
        "sha256": sha if sha is not None else (hashlib.sha256(content).hexdigest() if content is not None else None),
        "children": list(children),
    }


@dataclass
class Station:
    """Подставная станция с `files:list` / `files:download`."""

    files: dict[str, bytes] = field(default_factory=dict)
    extra_nodes: list[dict] = field(default_factory=list)
    list_status: int = 200
    sha_override: dict[str, str] = field(default_factory=dict)
    exec_reply: dict = field(default_factory=lambda: {
        "exitCode": 0, "stdout": json.dumps({"total": 6}), "stderr": "", "timedOut": False,
    })
    requests: list[httpx.Request] = field(default_factory=list)

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        if path == "/v1/sessions":
            return httpx.Response(200, json={"token": TOKEN})
        if request.headers.get("Authorization") != f"Bearer {TOKEN}":
            return httpx.Response(401, json={"code": "auth-required"})
        if path.endswith("/exec"):
            return httpx.Response(200, json=self.exec_reply)
        if path.endswith("/files:list"):
            if self.list_status != 200:
                return httpx.Response(self.list_status, json={"error": "нет каталога"})
            children = [_node(name, data, sha=self.sha_override.get(name)) for name, data in self.files.items()]
            return httpx.Response(200, json=_node("out", None, kind="dir", children=[*children, *self.extra_nodes]))
        if path.endswith("/files:download"):
            name = json.loads(request.content)["path"].rsplit("/", 1)[1]
            return httpx.Response(200, content=self.files[name])
        return httpx.Response(200, json={"status": "terminated"})

    def steps(self) -> list[str]:
        out = []
        for request in self.requests:
            path = request.url.path
            last = path.rsplit("/", 1)[1]
            if path == "/v1/sessions":
                out.append("create")
            elif last.endswith(":terminate"):
                out.append("terminate")
            else:
                out.append(last)
        return out


@pytest.fixture(autouse=True)
def _station_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "workstation_url", STATION_URL)


def _run(station: Station, **kwargs) -> CodeRun:
    return run_code("код", {}, transport=httpx.MockTransport(station), **kwargs)


def test_files_are_listed_and_downloaded_before_the_room_is_closed() -> None:
    station = Station(files={"итоги.xlsx": b"PK\x03\x04book", "слайды.pptx": b"PK\x03\x04deck"})

    result = _run(station, collect_files=True)

    assert station.steps() == ["create", "exec", "files:list", "files:download", "files:download", "terminate"]
    assert result.error is None and result.output == {"total": 6}
    assert result.files == (RoomFile("итоги.xlsx", b"PK\x03\x04book"), RoomFile("слайды.pptx", b"PK\x03\x04deck"))
    listing = next(r for r in station.requests if r.url.path.endswith("files:list"))
    assert json.loads(listing.content)["path"] == "/workspace/out"
    assert listing.headers["Authorization"] == f"Bearer {TOKEN}"


def test_nothing_is_collected_unless_the_caller_asks_for_files() -> None:
    station = Station(files={"итоги.xlsx": b"PK"})

    result = _run(station)

    assert station.steps() == ["create", "exec", "terminate"]
    assert result.files == ()


def test_a_run_without_an_out_directory_has_no_files_and_keeps_its_output() -> None:
    station = Station(list_status=404)

    result = _run(station, collect_files=True)

    assert result == CodeRun({"total": 6}, None)


def test_nested_directories_and_hidden_files_are_not_delivered() -> None:
    station = Station(
        files={"итоги.xlsx": b"PK", ".DS_Store": b"x", "~$итоги.xlsx": b"lock"},
        extra_nodes=[_node("сырьё", kind="dir", children=[_node("a.csv", b"1")])],
    )

    result = _run(station, collect_files=True)

    assert [file.name for file in result.files] == ["итоги.xlsx"]


def test_more_files_than_the_limit_is_an_error_for_the_author() -> None:
    station = Station(files={f"f{index}.csv": b"1" for index in range(sealed_room.MAX_ROOM_FILES + 1)})

    result = _run(station, collect_files=True)

    assert result.files == () and result.output is None
    assert str(sealed_room.MAX_ROOM_FILES) in result.error and not result.station_fault
    assert "files:download" not in station.steps()


def test_a_file_over_the_size_limit_is_an_error_and_is_not_downloaded() -> None:
    big = b"x" * (sealed_room.MAX_ROOM_FILE_BYTES + 1)
    station = Station(files={"большой.xlsx": big})

    result = _run(station, collect_files=True)

    assert result.files == () and "большой.xlsx" in result.error
    assert "files:download" not in station.steps()


def test_an_empty_file_is_an_error_for_the_author() -> None:
    station = Station(files={"пустой.xlsx": b""})

    result = _run(station, collect_files=True)

    assert result.files == () and "пустой.xlsx" in result.error and "пуст" in result.error


def test_a_download_that_does_not_match_the_listing_hash_is_refused() -> None:
    station = Station(files={"итоги.xlsx": b"PK-data"}, sha_override={"итоги.xlsx": "0" * 64})

    result = _run(station, collect_files=True)

    assert result.files == () and "итоги.xlsx" in result.error


def test_a_failed_exec_collects_nothing() -> None:
    station = Station(files={"итоги.xlsx": b"PK"}, exec_reply={
        "exitCode": 1, "stdout": "", "stderr": "Traceback ...", "timedOut": False,
    })

    result = _run(station, collect_files=True)

    assert result.error and "Traceback" in result.error
    assert "files:list" not in station.steps()


def test_a_station_failure_on_listing_is_a_station_fault_not_the_authors_error() -> None:
    station = Station(list_status=500)

    result = _run(station, collect_files=True)

    assert result.station_fault and result.files == ()
    assert station.steps()[-1] == "terminate", "комната закрывается и после сбоя списка"


def test_a_name_that_could_leave_the_directory_is_refused() -> None:
    station = Station(extra_nodes=[_node("../evil.xlsx", b"PK")])

    result = _run(station, collect_files=True)

    assert result.files == () and "evil.xlsx" in result.error
