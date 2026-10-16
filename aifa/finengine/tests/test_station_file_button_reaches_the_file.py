"""Кнопка файла в ходе станции ведёт к настоящему файлу (P45 в).

`station_turn._stream_canonical` кладёт в событие `file` идентификатор файла, а экран по нему
открывает `/projects/{project_id}/sources/{file_id}/file`. Ручка ждёт имя объекта без расширения
(`result_events.file_event`), а станция отдавала весь `object_store_location`
(`<префикс>/<имя>.<расширение>`): ручка не находила источник и отвечала 404.

Идёт через обе стороны настоящим кодом: файл кладёт `sources.save_generated_bytes` (так же, как
`out.file`), событие собирает `station_turn._stream_canonical`, читает ручка `download_source`.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from finengine.api import main as api_main
from finengine.api import station_turn
from finengine.auth.db import User
from finengine.config import settings
from finengine.contracts.results import FileArtifact
from finengine.skills import result_events, run_orchestrator, sources

OWNER = 7
PROJECT = "chat-1"
CONTENT = b"PK\x03\x04excel-bytes"


@pytest.fixture(autouse=True)
def _local_blobs(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "s3_endpoint", "")
    monkeypatch.setattr(settings, "data_dir", tmp_path)


def _user() -> User:
    return User(id=OWNER, username="test", password_hash="x", is_admin=False)


def _station_file_artifact() -> dict:
    _meta, location = sources.save_generated_bytes(
        OWNER, PROJECT, "итоги.xlsx", CONTENT,
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )
    return {"output_type": "FileArtifact", "filename": "итоги.xlsx", "object_store_location": location}


def _file_event_of_station_turn(monkeypatch, file_artifact: dict) -> dict:
    emitted: list[dict] = []
    monkeypatch.setattr(run_orchestrator, "emit_to_run", lambda _run_id, event: emitted.append(event) or True)
    channel = SimpleNamespace(run_id="run-1", gw_key="gw", delivered=[])
    station_turn._stream_canonical(
        channel, "FormOutput", {}, role_notice="", file_artifact=file_artifact,
    )
    return next(event for event in emitted if event["type"] == "file")


def test_the_station_file_button_opens_the_file_through_the_download_endpoint(monkeypatch) -> None:
    event = _file_event_of_station_turn(monkeypatch, _station_file_artifact())

    response = api_main.download_source(PROJECT, event["file_id"], user=_user())

    assert response.body == CONTENT


def test_the_station_and_the_pipeline_name_one_file_by_one_id(monkeypatch) -> None:
    file_artifact = _station_file_artifact()
    pipeline_event = result_events.file_event(FileArtifact.model_construct(**{
        **file_artifact, "file_kind": "xlsx",
    }))

    station_event = _file_event_of_station_turn(monkeypatch, file_artifact)

    assert station_event["file_id"] == pipeline_event["file_id"]
