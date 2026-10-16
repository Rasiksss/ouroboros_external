"""Запись сценария: вход → проект → документы → ходы → файлы → `Recording` (ADR-0005, F5).

Ход проводится так же, как экран: `POST /chat/turns`, затем чтение `GET /chat/runs/{id}/events`
до `completed`. Кадры-биения в запись не попадают: они не записи журнала и не нужны экрану.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from golden_client import ApiSession, ClientError, ClientSettings
from golden_format import DocumentRef, FileRef, Meta, Recording, TurnRecord
from golden_normalize import unwrap_event

MAX_RESUMES = 5
# `arch` — как у экрана с 18.08.2026 (use-chat-run.ts): путь хода один.
SCREEN_ARCH = "aifa_gateway"
NO_CASE = "нет: в API службы нет ручки Case; доступны только артефакты проекта"


@dataclass(frozen=True)
class Scenario:
    name: str
    documents: tuple[Path, ...]
    turns: tuple[str, ...]
    web_search: bool = True


def load_scenario(path: Path) -> Scenario:
    raw = json.loads(path.read_text(encoding="utf-8"))
    missing = [key for key in ("scenario", "turns") if key not in raw]
    if missing:
        raise ClientError(f"в сценарии {path.name} нет поля {', '.join(missing)}")
    return Scenario(
        name=str(raw["scenario"]),
        documents=tuple(path.parent / name for name in raw.get("documents", [])),
        turns=tuple(str(turn) for turn in raw["turns"]),
        web_search=bool(raw.get("web_search", True)),
    )


def record_scenario(scenario: Scenario, settings: ClientSettings) -> Recording:
    session = ApiSession(settings)
    session.login()
    project_id = f"qa-golden-{_slug(scenario.name)}-{uuid.uuid4().hex[:8]}"
    documents, doc_ids = _upload_all(session, scenario.documents, settings)
    turns: list[TurnRecord] = []
    session_id = ""
    for text in scenario.turns:
        body = {
            "session_id": session_id,
            "message": text,
            "doc_ids": doc_ids,
            "arch": SCREEN_ARCH,
            "project_id": project_id,
            "role": "",
            "pinned_tool": "",
            "pinned_tool_args": {},
            "web_search": scenario.web_search,
            "active_doc_id": doc_ids[0] if doc_ids else "",
        }
        events = _run_turn(session, body, settings)
        session_id = _session_id(events) or session_id
        turns.append(
            TurnRecord(
                user=text,
                events=tuple(events),
                answer=final_answer(events),
                files=_download_files(session, events),
            )
        )
    meta = Meta(
        scenario=scenario.name,
        stand=session.stand,
        build=settings.build,
        recorded_at=datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
    )
    return Recording(meta, tuple(documents), tuple(turns), _snapshot(session, project_id))


def final_answer(events: Sequence[Mapping[str, Any]]) -> str:
    """Текст после последнего вызова инструмента — то, что экран показывает ответом."""
    text = ""
    for item in events:
        event = unwrap_event(item)
        if event is None:
            continue
        if event.get("type") == "tool_call":
            text = ""
        elif event.get("type") == "text_delta":
            text += str(event.get("text", ""))
    return text


def _upload_all(
    session: ApiSession, paths: Sequence[Path], settings: ClientSettings
) -> tuple[list[DocumentRef], list[str]]:
    refs: list[DocumentRef] = []
    accepted: list[str] = []
    for path in paths:
        content = path.read_bytes()
        reply = session.upload(path.name, content)
        digest = hashlib.sha256(content).hexdigest()
        detail = reply.detail() if reply.status >= 400 else ""
        refs.append(DocumentRef(path.name, digest, len(content), reply.status, detail))
        if reply.status == 200:
            doc_id = str(reply.json()["doc_id"])
            _wait_parsed(session, doc_id, settings)
            accepted.append(doc_id)
    return refs, accepted


def _wait_parsed(session: ApiSession, doc_id: str, settings: ClientSettings) -> None:
    """Как экран на отправке: запустить отложенный разбор и дождаться текста."""
    session.request("POST", f"/documents/{doc_id}/parse")
    deadline = time.monotonic() + settings.parse_timeout_s
    while time.monotonic() < deadline:
        status = session.request("GET", f"/documents/{doc_id}/status")
        if status.status == 200 and not status.json().get("processing", True):
            return
        time.sleep(settings.poll_interval_s)
    raise ClientError(f"документ {doc_id} не распознан за {settings.parse_timeout_s:.0f} с")


def _run_turn(
    session: ApiSession, body: Mapping[str, Any], settings: ClientSettings
) -> list[dict[str, Any]]:
    started = session.request("POST", "/chat/turns", json_body=body)
    if started.status != 200:
        raise ClientError(f"ход не начат: HTTP {started.status}: {started.detail()}")
    run_id = started.json()["run_id"]
    deadline = time.monotonic() + settings.turn_timeout_s
    events: list[dict[str, Any]] = []
    for _ in range(MAX_RESUMES + 1):
        last = events[-1]["sequence"] if events else 0
        try:
            for payload in session.stream_events(f"/chat/runs/{run_id}/events?after={last}"):
                if payload.get("event_type") != "heartbeat":
                    events.append(payload)
                if _is_terminal(payload):
                    return events
                if time.monotonic() > deadline:
                    raise ClientError(f"ход не завершился за {settings.turn_timeout_s:.0f} с")
        except OSError:
            pass  # обрыв связи: дочитываем с последнего события, как экран при догоне
        time.sleep(settings.poll_interval_s)
    raise ClientError(f"поток хода {run_id} обрывается: {MAX_RESUMES} переподключений не хватило")


def _is_terminal(payload: Mapping[str, Any]) -> bool:
    if payload.get("event_type") == "completed":
        return True
    legacy = payload.get("legacy_event")
    return isinstance(legacy, dict) and legacy.get("type") == "session_lost"


def _session_id(events: Sequence[Mapping[str, Any]]) -> str:
    for item in events:
        event = unwrap_event(item)
        if event and event.get("type") == "session_id":
            return str(event.get("session_id", ""))
    return ""


def _download_files(
    session: ApiSession, events: Sequence[Mapping[str, Any]]
) -> tuple[FileRef, ...]:
    refs: list[FileRef] = []
    seen: set[str] = set()
    for item in events:
        event = unwrap_event(item)
        if not event or event.get("type") != "file" or event.get("file_id") in seen:
            continue
        file_id = str(event["file_id"])
        seen.add(file_id)
        name = str(event.get("filename", file_id))
        reply = session.request("GET", f"/documents/{file_id}/file")
        if reply.status == 200:
            refs.append(FileRef(name, len(reply.body), hashlib.sha256(reply.body).hexdigest()))
        else:
            refs.append(FileRef(name, None, None, note=f"HTTP {reply.status}"))
    return tuple(refs)


def _snapshot(session: ApiSession, project_id: str) -> dict[str, Any]:
    reply = session.request("GET", f"/projects/{project_id}/artifacts")
    artifacts: Any = reply.json() if reply.status == 200 else f"нет: HTTP {reply.status}"
    return {"case": NO_CASE, "artifacts": artifacts}


def _slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "scenario"
