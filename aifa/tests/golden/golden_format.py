"""Формат опорной записи одного сценария (ADR-0005, F5 (б)).

Запись — один JSON-файл: метаданные, документы с `sha256`, реплики пользователя по порядку,
полный поток SSE каждого хода как пришёл от службы, текст ответа, файлы-результаты и снимок
состояния. Токенов и cookie в записи нет: клиент их не сохраняет (`golden_client.py`).
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

FORMAT = "aifa-golden-recording/1"

JsonObject = dict[str, Any]


class RecordingError(ValueError):
    """Файл не является записью этого формата."""


@dataclass(frozen=True)
class Meta:
    scenario: str
    stand: str  # только хост: адрес без пути и без учётных данных
    build: str  # коммит/сборка стенда, как назвал оператор; `не указана`, если не знаем
    recorded_at: str


@dataclass(frozen=True)
class DocumentRef:
    name: str
    sha256: str
    size: int
    upload_status: int  # HTTP-код загрузки: XLS/DOC на новой системе — отказ (ADR-0026)
    upload_detail: str = ""


@dataclass(frozen=True)
class FileRef:
    name: str
    size: int | None
    sha256: str | None
    note: str = ""  # почему не скачан, если size/sha256 пусты


@dataclass(frozen=True)
class TurnRecord:
    user: str
    events: tuple[JsonObject, ...]
    answer: str
    files: tuple[FileRef, ...]


@dataclass(frozen=True)
class Recording:
    meta: Meta
    documents: tuple[DocumentRef, ...]
    turns: tuple[TurnRecord, ...]
    snapshot: JsonObject  # `Case` и артефакты проекта; чего нет в API — пометка «нет: …»


def dump(recording: Recording) -> str:
    body = {"format": FORMAT, **asdict(recording)}
    return json.dumps(body, ensure_ascii=False, indent=1) + "\n"


def write(recording: Recording, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(dump(recording), encoding="utf-8")


def read(path: Path) -> Recording:
    return load(path.read_text(encoding="utf-8"))


def load(text: str) -> Recording:
    try:
        raw = json.loads(text)
    except json.JSONDecodeError as exc:
        raise RecordingError(f"файл не JSON: {exc}") from exc
    if not isinstance(raw, dict) or raw.get("format") != FORMAT:
        found = raw.get("format") if isinstance(raw, dict) else type(raw).__name__
        raise RecordingError(f"формат записи {found!r}, ждали {FORMAT!r}")
    for section in ("meta", "documents", "turns", "snapshot"):
        if section not in raw:
            raise RecordingError(
                f"в записи нет раздела `{section}` (turns/documents/meta/snapshot)"
            )
    try:
        return Recording(
            meta=Meta(**raw["meta"]),
            documents=tuple(DocumentRef(**doc) for doc in raw["documents"]),
            turns=tuple(_turn(turn) for turn in raw["turns"]),
            snapshot=dict(raw["snapshot"]),
        )
    except (TypeError, KeyError) as exc:
        raise RecordingError(f"запись повреждена: {exc!r}") from exc


def _turn(raw: Mapping[str, Any]) -> TurnRecord:
    return TurnRecord(
        user=raw["user"],
        events=tuple(raw["events"]),
        answer=raw["answer"],
        files=tuple(FileRef(**item) for item in raw["files"]),
    )
