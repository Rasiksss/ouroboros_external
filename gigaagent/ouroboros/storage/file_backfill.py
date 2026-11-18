from __future__ import annotations

import json
import mimetypes
import re
import tempfile
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, TypeAdapter
from sqlalchemy import select, text
from sqlalchemy.engine import Connection, Engine, RowMapping
from sqlalchemy.orm import Session

from new_core.identity import OwnerScope, SurfaceId
from ouroboros.contracts.attachments import AttachmentIdentity, FileStatus
from ouroboros.storage.blob.keys import normalize_storage_key
from ouroboros.storage.blob.protocol import StorageProvider
from ouroboros.storage.engine_setup import database_now
from ouroboros.storage.file_catalog import FileCatalog
from ouroboros.storage.file_models import CatalogFile, MessageFile
from ouroboros.storage.message_field_crypto import decrypt_field
from ouroboros.storage.message_history import message_from_row
from ouroboros.storage.runtime_engine import resolve_runtime_engine
from ouroboros.storage.runtime_serialization import load_json


class LegacyFile(BaseModel):
    id: str = Field(min_length=1)
    owner_id: str = Field(min_length=1)
    agent_id: str = Field(min_length=1)
    surface_id: SurfaceId
    chat_id: str = Field(min_length=1)
    filename: str = Field(validation_alias=AliasChoices("filename", "display_name"))
    storage_key: str
    mime: str = Field(validation_alias=AliasChoices("mime", "content_type"))
    size: int = Field(ge=0)
    source: str
    status: Literal["ready", "draft", "pending", "staging", "deleting", "expired", "failed"]
    created_at: datetime
    expires_at: str = ""
    producer_task: str = ""
    artifact_path: str = ""


class LegacyArtifact(BaseModel):
    schema_version: Literal[1]
    origin: Literal["agent_generated"]
    purpose: Literal["artifact"]
    context_eligible: Literal[False]
    authoritative: Literal[False]
    task_id: str = Field(min_length=1)
    chat_id: str = Field(min_length=1)
    created_at: datetime
    created_by_tool: Literal["skill_exec", "data_write"]


class MigrationIssue(BaseModel):
    key: str
    reason: str


@dataclass
class BackfillResult:
    imported: int = 0
    unchanged: int = 0
    linked: int = 0
    skipped: int = 0


class LegacySnapshot(AttachmentIdentity):
    model_config = ConfigDict(extra="allow")


_LEGACY_SNAPSHOTS = TypeAdapter(list[LegacySnapshot])
TOMBSTONE_PREFIX = "_tombstones/"


def _history_tombstone(payload: LegacySnapshot, scope: OwnerScope, created_at: str) -> CatalogFile:
    legacy = LegacyFile.model_validate(
        {
            **scope.to_dict(),
            "source": str(scope.surface_id),
            "status": "expired",
            "created_at": created_at,
            "storage_key": TOMBSTONE_PREFIX + uuid.uuid5(uuid.NAMESPACE_URL, payload.id).hex,
            **payload.model_dump(),
        }
    )
    expiry = datetime.fromisoformat(legacy.expires_at) if legacy.expires_at else None
    if legacy.created_at.tzinfo is None or (expiry is not None and expiry.tzinfo is None):
        raise ValueError("unproven timestamp")
    if (legacy.owner_id, legacy.agent_id) != (scope.owner_id, scope.agent_id) or (
        legacy.surface_id == "dialogs" and (scope.surface_id != "dialogs" or legacy.chat_id != scope.chat_id)
    ):
        raise ValueError("conflicting historical ownership")
    return _catalog_file(legacy, status="deleted", updated_at=created_at, expiry=expiry)


def _catalog_file(legacy: LegacyFile, *, status: FileStatus, updated_at: str, expiry: datetime | None) -> CatalogFile:
    return CatalogFile(
        id=legacy.id,
        owner_id=legacy.owner_id,
        agent_id=legacy.agent_id,
        name=legacy.filename,
        storage_scope="uploads",
        storage_key=normalize_storage_key(legacy.storage_key),
        mime=legacy.mime,
        size=legacy.size,
        origin=legacy.source,
        surface_id=str(legacy.surface_id),
        chat_id=legacy.chat_id,
        producer_task=legacy.producer_task,
        artifact_path=legacy.artifact_path,
        status=status,
        created_at=legacy.created_at.astimezone(timezone.utc).isoformat(),
        updated_at=updated_at,
        expires_at=expiry.astimezone(timezone.utc).isoformat() if expiry else None,
    )


def _read_legacy(
    engine: Engine, scope: OwnerScope, provider: StorageProvider, key: str
) -> tuple[LegacyFile, datetime | None]:
    with tempfile.TemporaryDirectory(prefix="ouroboros-backfill-") as directory:
        path = Path(directory) / "metadata.json"
        provider.copy_to_path(scope, "uploads", key, path, max_bytes=65536)
        payload = json.loads(path.read_bytes())
        if not isinstance(payload, dict):
            raise ValueError("invalid legacy metadata")
        match = re.fullmatch(r"_meta/(?:chat/([^/]+)/[^/]+|attachments/([^/]+))\.json", key)
        if match:
            key_id = match[1] or match[2]
            if payload.get("id") not in (None, "", key_id):
                raise ValueError("conflicting legacy file ID")
            payload["id"] = key_id
        if key.endswith(".metadata.json"):
            artifact = LegacyArtifact.model_validate(payload)
            with engine.connect() as conn:
                row = (
                    conn.execute(
                        text("SELECT * FROM tasks WHERE task_id=:task AND owner_id=:owner AND agent_id=:agent"),
                        {"task": artifact.task_id, "owner": scope.owner_id, "agent": scope.agent_id},
                    )
                    .mappings()
                    .first()
                )
                if row is None:
                    raise ValueError("artifact producer is not provable")
                producer_scope = _historical_scope(row, table="tasks", dialect=conn.dialect.name)
            if artifact.chat_id != producer_scope.chat_id:
                raise ValueError("conflicting artifact chat")
            storage_key = normalize_storage_key(key.removesuffix(".metadata.json"))
            identity = json.dumps([scope.owner_id, scope.agent_id, "uploads", storage_key])
            legacy = LegacyFile(
                owner_id=producer_scope.owner_id,
                agent_id=producer_scope.agent_id,
                surface_id=producer_scope.surface_id,
                chat_id=producer_scope.chat_id,
                id=uuid.uuid5(uuid.NAMESPACE_URL, identity).hex,
                filename=Path(storage_key).name,
                storage_key=storage_key,
                mime=mimetypes.guess_type(storage_key)[0] or "application/octet-stream",
                size=provider.size(scope, "uploads", storage_key),
                source=artifact.origin,
                status="ready",
                created_at=artifact.created_at,
                producer_task=artifact.task_id,
                artifact_path=storage_key,
            )
        else:
            legacy = LegacyFile.model_validate(payload)
    if (legacy.owner_id, legacy.agent_id) != (scope.owner_id, scope.agent_id):
        raise ValueError("unproven owner")
    if legacy.created_at.tzinfo is None:
        raise ValueError("unproven timestamp")
    legacy.storage_key = normalize_storage_key(legacy.storage_key)
    expiry = datetime.fromisoformat(legacy.expires_at) if legacy.expires_at else None
    if expiry is not None and expiry.tzinfo is None:
        raise ValueError("unproven expiry")
    if legacy.status in {"draft", "pending", "staging"} and expiry is None:
        raise ValueError("missing draft expiry")
    return legacy, expiry


def _historical_scope(row: RowMapping, *, table: str, dialect: str) -> OwnerScope:
    task_scope = None
    if table == "tasks":
        task_input = load_json(decrypt_field(row["input_json"], dialect_name=dialect))
        if task_input.get("scope"):
            task_scope = OwnerScope.from_dict(task_input["scope"])
    scope = OwnerScope.from_dict(
        {
            "owner_id": row["owner_id"],
            "agent_id": row["agent_id"],
            "surface_id": row["surface_id"] if table == "tasks" else row["source"],
            "chat_id": str(row["chat_id"] or (task_scope.chat_id if task_scope else "")),
        }
    )
    if task_scope is not None and task_scope != scope:
        raise ValueError("conflicting task scope")
    return scope


def _import_file(
    session: Session,
    file: CatalogFile,
    *,
    apply: bool,
    planned: dict[str, CatalogFile],
    planned_keys: set[str],
) -> bool:
    existing = planned.get(file.id) or session.get(CatalogFile, file.id)
    if existing is not None:
        if (existing.owner_id, existing.agent_id, existing.storage_key, existing.surface_id, existing.chat_id) != (
            file.owner_id,
            file.agent_id,
            file.storage_key,
            file.surface_id,
            file.chat_id,
        ):
            raise ValueError("file ID conflicts with existing catalogue")
        return False
    shared = session.scalar(
        select(CatalogFile.id).where(
            CatalogFile.owner_id == file.owner_id,
            CatalogFile.agent_id == file.agent_id,
            CatalogFile.storage_scope == "uploads",
            CatalogFile.storage_key == file.storage_key,
        )
    )
    if shared or file.storage_key in planned_keys:
        raise ValueError("different IDs share one object")
    if apply:
        session.add(file)
        session.flush()
    else:
        planned[file.id] = file
        planned_keys.add(file.storage_key)
    return True


def _import_link(conn: Connection, scope: OwnerScope, file_id: str, message_id: str, *, apply: bool) -> bool:
    if not apply:
        return (
            conn.scalar(
                select(MessageFile.file_id).where(MessageFile.file_id == file_id, MessageFile.message_id == message_id)
            )
            is None
        )
    return bool(
        conn.execute(
            text(
                "INSERT INTO message_files (file_id,message_id,owner_id,agent_id) "
                "VALUES (:file,:message,:owner,:agent) ON CONFLICT (file_id,message_id) DO NOTHING"
            ),
            {"file": file_id, "message": message_id, "owner": scope.owner_id, "agent": scope.agent_id},
        ).rowcount
    )


def _restore_history(
    engine: Engine,
    scope: OwnerScope,
    *,
    apply: bool,
    planned: dict[str, CatalogFile],
    planned_keys: set[str],
    result: BackfillResult,
    refuse: Callable[[str, str], None],
) -> None:
    for table, identity, attachment_field in (
        ("messages", "message_id", "attachments"),
        ("tasks", "task_id", "terminal_attachments"),
    ):
        cursor = ""
        while True:
            with engine.begin() as conn, FileCatalog(conn, scope) as catalog:
                rows = (
                    conn.execute(
                        text(
                            f"SELECT * FROM {table} WHERE owner_id=:owner AND agent_id=:agent AND {identity}>:after "
                            f"ORDER BY {identity} LIMIT 200"
                        ),
                        {"owner": scope.owner_id, "agent": scope.agent_id, "after": cursor},
                    )
                    .mappings()
                    .all()
                )
                if not rows:
                    break
                cursor = str(rows[-1][identity])
                catalog.lock()
                records = []
                for row in rows:
                    metadata = (
                        message_from_row(row, dialect_name=conn.dialect.name).metadata
                        if table == "messages"
                        else load_json(row["metadata_json"])
                    )
                    try:
                        payloads = _LEGACY_SNAPSHOTS.validate_python(metadata.get(attachment_field, []))
                        if any(not payload.id for payload in payloads):
                            raise ValueError("missing attachment identity")
                    except ValueError:
                        refuse(str(row[identity]), "invalid historical attachment IDs")
                        continue
                    if payloads:
                        records.append((row, payloads))
                file_ids = {payload.id for _, payloads in records for payload in payloads}
                files = {
                    file.id: file
                    for file in catalog.session.scalars(select(CatalogFile).where(CatalogFile.id.in_(file_ids)))
                }
                for row, payloads in records:
                    try:
                        historical_scope = _historical_scope(row, table=table, dialect=conn.dialect.name)
                    except ValueError:
                        refuse(str(row[identity]), "historical scope is not provable")
                        continue
                    seen: set[str] = set()
                    for payload in payloads:
                        file_id = payload.id
                        if file_id in seen:
                            continue
                        seen.add(file_id)
                        file = planned.get(file_id) or files.get(file_id)
                        if file is None:
                            try:
                                file = _history_tombstone(payload, historical_scope, row["created_at"])
                            except ValueError:
                                refuse(file_id, "historical attachment has no proven metadata")
                                continue
                            try:
                                result.imported += _import_file(
                                    catalog.session, file, apply=apply, planned=planned, planned_keys=planned_keys
                                )
                            except ValueError as error:
                                refuse(file_id, str(error))
                                continue
                        files[file_id] = file
                        if (file.owner_id, file.agent_id) != (scope.owner_id, scope.agent_id) or (
                            file.surface_id == "dialogs"
                            and (historical_scope.chat_id != file.chat_id or historical_scope.surface_id != "dialogs")
                        ):
                            refuse(file_id, "historical link has no proven file ownership")
                            continue
                        if table == "tasks":
                            continue
                        result.linked += _import_link(conn, scope, file.id, str(row[identity]), apply=apply)


def backfill(
    scope: OwnerScope, provider: StorageProvider, *, apply: bool, report: Callable[[MigrationIssue], None]
) -> BackfillResult:
    engine = resolve_runtime_engine("file backfill")
    result = BackfillResult()
    planned: dict[str, CatalogFile] = {}
    planned_keys: set[str] = set()

    def refuse(key: str, reason: str) -> None:
        result.skipped += 1
        report(MigrationIssue(key=key, reason=reason))

    cursor = ""
    while keys := provider.list_keys(scope, "uploads", "", limit=200, start_after=cursor):
        cursor = keys[-1]
        for key in keys:
            if not (key.startswith("_meta/") or key.endswith(".metadata.json")):
                continue
            match = re.fullmatch(r"_meta/(chat|attachments)/([^/]+)(?:/[^/]+)?\.json", key)
            if match:
                canonical = f"_meta/chat/{match[2]}.json"
                if key != canonical and provider.exists(scope, "uploads", canonical):
                    continue
                if match[1] == "attachments" and provider.list_keys(
                    scope, "uploads", f"_meta/chat/{match[2]}/", limit=1
                ):
                    continue
            try:
                legacy, expiry = _read_legacy(engine, scope, provider, key)
            except FileNotFoundError:
                refuse(key, "content object is missing")
                continue
            except ValueError:
                refuse(key, "metadata or ownership is not provable")
                continue
            missing = legacy.status in {"ready", "draft", "pending"} and not provider.exists(
                scope, "uploads", legacy.storage_key
            )
            with engine.begin() as conn, FileCatalog(conn, scope) as catalog:
                catalog.lock()
                now = database_now(conn)
                expired = (expiry is not None and expiry <= now) or legacy.status in {"expired", "failed", "deleting"}
                if missing and not expired:
                    refuse(key, "content object is missing")
                    continue
                status: FileStatus = "draft"
                if expired:
                    status = "deleting"
                elif legacy.status == "ready" or legacy.status == "staging":
                    status = legacy.status
                file = _catalog_file(
                    legacy,
                    status=status,
                    updated_at=now.isoformat(timespec="microseconds"),
                    expiry=expiry if expired or legacy.status != "ready" else None,
                )
                try:
                    imported = _import_file(
                        catalog.session, file, apply=apply, planned=planned, planned_keys=planned_keys
                    )
                except ValueError as error:
                    refuse(key, str(error))
                    continue
                result.imported += imported
                result.unchanged += not imported
    _restore_history(
        engine, scope, apply=apply, planned=planned, planned_keys=planned_keys, result=result, refuse=refuse
    )
    return result
