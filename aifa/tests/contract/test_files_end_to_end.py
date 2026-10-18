"""Сквозной тест шага 11: файлы и история на настоящем ouroboros.

Загрузка через ручку службы → каталог ouroboros → распознавание → ход с этим документом
(`aifa_goal`) → файл-результат в каталоге → выдача через ручку → история чата из ouroboros →
удаление документа в обоих местах.

Настоящие: приложение службы (ручки по HTTP), её реестр документов (временный SQLite) и хранилище
текста, каталог и чат ouroboros (контейнер профиля `contract`), атом `out.file`. Подставные — модель
(подставной сервер) и границы конвейера (`pipeline_fixture.py`). Файл-результат здесь делает сам
атом `out.file` над готовым текстовым результатом: цель «выгрузи в Excel» планировщик в этом
профиле не строит.
"""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pipeline_fixture as fixture
import pytest
from engine_host import running_engine
from finengine.assistant.ouro_files import CatalogFiles
from finengine.contracts import NarrativeOutput
from finengine.skills import result_events
from finengine.skills.atoms.out_file import out_file_executor
from finengine.skills.spec.graph_executor import NodeArgs, RunInputs
from test_chat_turn_end_to_end import ANSWER, screen_events

from stand import SCENARIOS, ContractStand
from stub_model.client import StubModelAdmin

pytestmark = [pytest.mark.contract, pytest.mark.asyncio]

REPORT_NAME = "severstal-2024.txt"
TEXT_WAIT_SEC = 30.0


async def no_card(_text: str) -> dict[str, Any]:
    return {}


@pytest.fixture
def engine(
    stand: ContractStand, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[SimpleNamespace]:
    held = {"doc": ""}
    monkeypatch.setattr("finengine.skills.meta_card.build", no_card)
    with running_engine(
        stand, tmp_path, monkeypatch, doc=lambda: held["doc"], real_documents=True
    ) as base:
        yield SimpleNamespace(
            base=base, held=held, catalog=CatalogFiles(stand.ouro_http_url, timeout_sec=120.0)
        )


async def upload(base: str, content: bytes) -> str:
    async with httpx.AsyncClient(timeout=60.0) as client:
        response = await client.post(
            f"{base}/documents/upload",
            files={"file": (REPORT_NAME, content, "text/plain")},
            data={"ocr_engine": "pymupdf"},
        )
    assert response.status_code == 200, response.text
    return str(response.json()["doc_id"])


async def wait_for_text(doc_id: str) -> str:
    from finengine.store.blobs import document_text

    async with asyncio.timeout(TEXT_WAIT_SEC):
        while (text := document_text(doc_id)) is None:
            await asyncio.sleep(0.2)
    return str(text)


async def answered_turn(
    engine: SimpleNamespace, stub: StubModelAdmin, doc_id: str, **extra: Any
) -> list[dict[str, Any]]:
    """Ход с документом по сценарию `aifa_goal`: события экрана до `completed`."""
    stub.use_scenario(SCENARIOS / "aifa_goal.json")
    async with httpx.AsyncClient(timeout=30.0) as client:
        started = await client.post(
            f"{engine.base}/chat/turns",
            json={
                "message": fixture.QUESTION,
                "project_id": fixture.PROJECT,
                "doc_ids": [doc_id],
                **extra,
            },
        )
    assert started.status_code == 200, started.text
    return await screen_events(engine.base, started.json()["run_id"])


def result_file(source_text: str) -> Any:
    """Файл-результат настоящим атомом `out.file`: xlsx над текстовым результатом, у владельца и
    проекта хода."""
    source = NarrativeOutput(run_id="run-e2e", text=source_text)
    ctx = SimpleNamespace(
        run_id="run-e2e",
        inputs=RunInputs(extra={"owner_id": str(fixture.OWNER), "project_id": fixture.PROJECT}),
        dependencies=lambda: (source,),
    )
    (artifact,) = out_file_executor(
        NodeArgs(extra={"file_kind": "xlsx", "filename": "итоги.xlsx"}), ctx
    )
    return artifact


async def test_a_document_goes_through_the_catalog_to_an_answer_a_result_file_and_back(
    engine: SimpleNamespace, stub: StubModelAdmin
) -> None:
    from finengine.ingestion.document_store import get_store

    report = fixture.document_text().encode()

    # 1. Загрузка через ручку службы → оригинал в каталоге ouroboros, пара «документ ↔ файл» у
    #    службы.
    doc_id = await upload(engine.base, report)
    engine.held["doc"] = doc_id
    meta = get_store().get_document(doc_id)
    assert meta.catalog_file_id and meta.sha256 == hashlib.sha256(report).hexdigest()
    assert engine.catalog.read(str(fixture.OWNER), meta.catalog_file_id) == report
    assert engine.catalog.read("someone-else", meta.catalog_file_id) is None

    # 2. Распознавание читает оригинал через каталог, текст остаётся у службы.
    assert await wait_for_text(doc_id) == report.decode()

    # 3. Ход с этим документом: настоящий ouroboros, `aifa_goal`, конвейер службы.
    events = await answered_turn(engine, stub, doc_id)
    assert events[-1]["status"] == "succeeded"
    asked = [e for e in stub.journal() if e["model"] == "stub-main"]
    assert "987654" in asked[-1]["last_tool_result"], (
        "числа ответа — из таблицы результатов конвейера"
    )
    assert any(
        e["legacy_event"].get("text", "").startswith(ANSWER) for e in events if "legacy_event" in e
    )

    # 4. Файл-результат: в каталоге у владельца; `file_id` экрана — идентификатор службы.
    artifact = result_file(ANSWER)
    file_id = result_events.source_file_id(artifact.object_store_location)
    async with httpx.AsyncClient(timeout=60.0) as client:
        served = await client.get(
            f"{engine.base}/projects/{fixture.PROJECT}/sources/{file_id}/file"
        )
    assert served.status_code == 200
    assert hashlib.sha256(served.content).hexdigest() == artifact.content_hash
    assert served.content[:2] == b"PK", "xlsx из каталога, а не заглушка"
    assert "filename*=UTF-8''" in served.headers["content-disposition"]

    # 5. Удаление документа — и у службы, и в каталоге (C6.4).
    async with httpx.AsyncClient(timeout=60.0) as client:
        deleted = await client.delete(f"{engine.base}/documents/{doc_id}")
        assert deleted.status_code == 200
        assert (await client.get(f"{engine.base}/documents/{doc_id}/file")).status_code == 404
    assert engine.catalog.read(str(fixture.OWNER), meta.catalog_file_id) is None


async def test_the_chat_history_comes_from_ouroboros_without_progress_and_subtasks(
    engine: SimpleNamespace, stub: StubModelAdmin, monkeypatch: pytest.MonkeyPatch
) -> None:
    """C1.3 на живом ouroboros: после хода история чата проекта — реплика и финал корня."""
    from finengine.api import main as app_main

    session = "5f3c2a10-7d3e-4c1b-9a55-0e2f6d1b8a01"
    monkeypatch.setattr(
        app_main,
        "get_dialog_store",
        lambda: SimpleNamespace(
            load=lambda uid: {"projects": [{"id": fixture.PROJECT, "session_id": session}]}
        ),
    )
    doc_id = await upload(engine.base, fixture.document_text().encode())
    engine.held["doc"] = doc_id
    await wait_for_text(doc_id)
    await answered_turn(engine, stub, doc_id, session_id=session)

    async with httpx.AsyncClient(timeout=30.0) as client:
        history = await client.get(f"{engine.base}/chat/history/{session}")

    assert history.status_code == 200, history.text
    messages = history.json()["history"]
    assert messages[0] == {"role": "user", "content": fixture.QUESTION}
    assert messages[-1]["role"] == "assistant" and messages[-1]["content"].startswith(ANSWER)
    assert {m["role"] for m in messages} == {"user", "assistant"}
