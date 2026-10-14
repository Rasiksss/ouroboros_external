from __future__ import annotations

import base64
import re
import uuid
from datetime import timedelta
from enum import StrEnum
from pathlib import PurePosixPath

from sqlalchemy import and_, case, func, literal, or_, select, text, tuple_, union_all, update
from sqlalchemy.engine import Connection
from sqlalchemy.orm import Session
from sqlalchemy.sql import Select

from new_core.identity import OwnerScope
from ouroboros.contracts.attachments import ATTACHMENT_IDS_ADAPTER, AttachmentMetadata, Entry, EntryPage
from ouroboros.storage.attachment_validation import validate_attachment_metadata
from ouroboros.storage.engine_setup import database_now
from ouroboros.storage.file_models import CatalogFile, FileBase, Folder, MessageFile, attachment_metadata
from ouroboros.storage.runtime_models import ConversationMessage, source_aliases
from ouroboros.storage.settings.store import load_settings


class FileFailure(StrEnum):
    NOT_FOUND = "not_found"
    INVALID_INPUT = "invalid_input"
    CONFLICT = "conflict"
    TOO_LARGE = "too_large"
    TRANSFER_IN_PROGRESS = "transfer_in_progress"
    OWNER_CAPACITY = "owner_capacity"
    PROCESS_CAPACITY = "process_capacity"


class FileError(ValueError):
    def __init__(self, reason: str = "file not found", kind: FileFailure = FileFailure.NOT_FOUND) -> None:
        super().__init__(reason)
        self.kind = kind


class FileTransferPending(FileError):
    """A transfer can retry after a live claim or local capacity is released."""


class FileUnavailable(FileError):
    """A known file was deleted or expired and cannot be reused on retry."""


def file_name(name: str) -> str:
    name = name.strip()
    if not name or len(name) > 200 or name in {".", ".."} or re.search(r"[/\\\x00-\x1f\x7f]", name):
        raise FileError("invalid name", FileFailure.INVALID_INPUT)
    return name


def file_claim_seconds(io_timeout_seconds: float) -> float:
    return 2 * io_timeout_seconds + 180


class FileCatalog:
    def __init__(self, connection: Connection, scope: OwnerScope) -> None:
        self.conn = connection
        self.session = Session(connection, expire_on_commit=False, join_transaction_mode="rollback_only")
        self.scope = scope
        self._locked = False

    def __enter__(self) -> FileCatalog:
        return self

    def __exit__(self, *exc: object) -> None:
        self.session.close()

    def lock(self) -> None:
        if self._locked:
            return
        self.conn.execute(text("SELECT set_config('lock_timeout', '5000', true)"))
        self.conn.execute(
            text("SELECT pg_advisory_xact_lock(hashtext(:owner), hashtext(:agent))"),
            {"owner": self.scope.owner_id, "agent": self.scope.agent_id},
        )
        self._locked = True

    def _files(self) -> Select[tuple[CatalogFile]]:
        return select(CatalogFile).where(
            CatalogFile.owner_id == self.scope.owner_id,
            CatalogFile.agent_id == self.scope.agent_id,
            or_(
                CatalogFile.surface_id != "dialogs",
                and_(literal(str(self.scope.surface_id)) == "dialogs", CatalogFile.chat_id == self.scope.chat_id),
            ),
        )

    def file(self, file_id: str, *, available: bool = True) -> CatalogFile:
        statement = self._files().where(CatalogFile.id == file_id)
        if self._locked:
            statement = statement.with_for_update().execution_options(populate_existing=True)
        file = self.session.scalar(statement)
        if file is None:
            raise FileError()
        if available and (
            file.status in {"deleting", "deleted"}
            or (
                file.expires_at is not None
                and file.expires_at <= database_now(self.conn).isoformat(timespec="microseconds")
            )
        ):
            raise FileUnavailable()
        if available and file.status not in {"draft", "ready"}:
            raise FileError()
        return file

    def folder(self, folder_id: str) -> Folder:
        folder = self.session.scalar(
            select(Folder).where(
                Folder.id == folder_id,
                Folder.owner_id == self.scope.owner_id,
                Folder.agent_id == self.scope.agent_id,
            )
        )
        if folder is None:
            raise FileError("folder not found")
        return folder

    def _reachable(self, explicit_ids: list[str]) -> Select[tuple[CatalogFile]]:
        messages = FileBase.metadata.tables["messages"]
        linked = (
            select(MessageFile.file_id)
            .join(messages, MessageFile.message_id == messages.c.message_id)
            .where(
                messages.c.owner_id == self.scope.owner_id,
                messages.c.agent_id == self.scope.agent_id,
                messages.c.chat_id == self.scope.chat_id,
                messages.c.source.in_(source_aliases(self.scope.surface_id)),
            )
        )
        return self._files().where(
            or_(
                CatalogFile.id.in_(linked),
                CatalogFile.id.in_(explicit_ids),
                and_(
                    CatalogFile.producer_task != "",
                    CatalogFile.surface_id == self.scope.surface_id,
                    CatalogFile.chat_id == self.scope.chat_id,
                ),
            ),
        )

    def resolve_reference(
        self, reference: str, *, explicit_ids: list[str], fallback_path: str = ""
    ) -> CatalogFile | None:
        reachable = self._reachable(explicit_ids).with_only_columns(CatalogFile.id).correlate(None)
        identified = reachable.where(CatalogFile.id == reference).scalar_subquery()
        ready = reachable.where(
            CatalogFile.status == "ready",
            or_(CatalogFile.expires_at.is_(None), CatalogFile.expires_at > database_now(self.conn).isoformat()),
        )
        exact_path = or_(CatalogFile.storage_key == reference, CatalogFile.artifact_path == reference)
        matched = (
            ready.where(
                or_(
                    exact_path,
                    and_(literal(bool(fallback_path)), CatalogFile.artifact_path == fallback_path),
                )
            )
            .order_by(
                case((exact_path, 0), else_=1),
                CatalogFile.created_at.desc(),
                CatalogFile.artifact_sequence.desc(),
                CatalogFile.id.desc(),
            )
            .limit(1)
            .scalar_subquery()
        )
        named = ready.where(CatalogFile.name == reference, literal("/" not in reference)).limit(2).subquery()
        unique_name = select(func.min(named.c.id)).having(func.count() == 1).scalar_subquery()
        return self.session.scalar(
            select(CatalogFile).where(
                CatalogFile.id == func.coalesce(identified, matched, unique_name),
                CatalogFile.id.in_(ready),
            )
        )

    def tool_files(self, *, explicit_ids: list[str], prefix: str, limit: int) -> list[str]:
        path = case(
            (CatalogFile.artifact_path != "", CatalogFile.artifact_path),
            else_=CatalogFile.storage_key,
        )
        return list(
            self.session.scalars(
                self._reachable(explicit_ids)
                .with_only_columns(path)
                .where(
                    CatalogFile.status == "ready",
                    or_(CatalogFile.expires_at.is_(None), CatalogFile.expires_at > database_now(self.conn).isoformat()),
                    path.startswith(prefix, autoescape=True),
                )
                .distinct()
                .order_by(path)
                .limit(max(1, min(limit, 500)))
            )
        )

    def create_folder(self, name: str, parent_id: str | None) -> Folder:
        self.lock()
        if parent_id is not None:
            self.folder(parent_id)
        name = file_name(name)
        if self.session.scalar(
            select(Folder.id).where(
                Folder.owner_id == self.scope.owner_id,
                Folder.agent_id == self.scope.agent_id,
                Folder.parent_id == parent_id,
                Folder.name == name,
            )
        ):
            raise FileError("folder name already exists", FileFailure.CONFLICT)
        folder = Folder(
            id=uuid.uuid4().hex,
            owner_id=self.scope.owner_id,
            agent_id=self.scope.agent_id,
            parent_id=parent_id,
            name=name,
        )
        self.session.add(folder)
        self.session.flush()
        return folder

    def path(self, folder_id: str | None) -> list[Entry]:
        if folder_id is None:
            return []
        ancestors = (
            select(Folder.id, Folder.name, Folder.parent_id, literal(0).label("depth"))
            .where(
                Folder.id == folder_id, Folder.owner_id == self.scope.owner_id, Folder.agent_id == self.scope.agent_id
            )
            .cte(recursive=True)
        )
        ancestors = ancestors.union_all(
            select(Folder.id, Folder.name, Folder.parent_id, ancestors.c.depth + 1).join(
                ancestors, Folder.id == ancestors.c.parent_id
            )
        )
        path = [
            Entry(id=row.id, name=row.name, kind="folder")
            for row in self.conn.execute(select(ancestors).order_by(ancestors.c.depth.desc()))
        ]
        if not path:
            raise FileError("folder not found")
        return path

    def edit_folder(
        self, folder_id: str, *, name: str | None = None, parent_id: str | None = None, move: bool = False
    ) -> Folder:
        self.lock()
        folder = self.folder(folder_id)
        target = parent_id if move else folder.parent_id
        if any(part.id == folder_id for part in self.path(target)):
            raise FileError("folder cycle", FileFailure.CONFLICT)
        name = file_name(name) if name is not None else folder.name
        if self.session.scalar(
            select(Folder.id).where(
                Folder.owner_id == self.scope.owner_id,
                Folder.agent_id == self.scope.agent_id,
                Folder.parent_id == target,
                Folder.name == name,
                Folder.id != folder_id,
            )
        ):
            raise FileError("folder name already exists", FileFailure.CONFLICT)
        folder.name, folder.parent_id = name, target
        self.session.flush()
        return folder

    def delete_folder(self, folder_id: str) -> None:
        self.lock()
        folder = self.folder(folder_id)
        children = self.session.scalar(
            select(Folder.id)
            .where(
                Folder.owner_id == self.scope.owner_id,
                Folder.agent_id == self.scope.agent_id,
                Folder.parent_id == folder_id,
            )
            .limit(1)
        )
        files = self.session.scalar(
            select(CatalogFile.id)
            .where(
                CatalogFile.owner_id == self.scope.owner_id,
                CatalogFile.agent_id == self.scope.agent_id,
                CatalogFile.folder_id == folder_id,
                CatalogFile.status.not_in(("deleting", "deleted")),
            )
            .limit(1)
        )
        if children or files:
            raise FileError("folder not empty", FileFailure.CONFLICT)
        self.conn.execute(
            update(CatalogFile)
            .where(
                CatalogFile.owner_id == self.scope.owner_id,
                CatalogFile.agent_id == self.scope.agent_id,
                CatalogFile.folder_id == folder_id,
            )
            .values(folder_id=None)
        )
        self.session.delete(folder)
        self.session.flush()

    def _new_file_name(self, name: str, folder_id: str | None, file_id: str) -> str:
        name = file_name(name)
        path = PurePosixPath(name)
        files = (
            self._files()
            .with_only_columns(CatalogFile.name)
            .where(
                CatalogFile.folder_id == folder_id,
                CatalogFile.id != file_id,
                CatalogFile.status.in_(("staging", "draft", "ready")),
            )
        )
        candidate, attempt = name, 0
        while self.session.scalar(files.where(CatalogFile.name == candidate).limit(1)) is not None:
            suffix = file_id[:8] if attempt == 0 else uuid.uuid5(uuid.NAMESPACE_OID, f"{file_id}:{attempt}").hex[:8]
            ending = "_" + suffix + path.suffix
            candidate = file_name(path.stem[: 200 - len(ending)] + ending)
            attempt += 1
        return candidate

    def reserve(
        self,
        name: str,
        mime: str,
        *,
        origin: str,
        folder_id: str | None = None,
        file_id: str = "",
        storage_key: str = "",
        producer_task: str = "",
        artifact_path: str = "",
        fingerprint: str = "",
    ) -> CatalogFile:
        self.lock()
        if folder_id is not None:
            self.folder(folder_id)
        now = database_now(self.conn)
        timestamp = now.isoformat(timespec="microseconds")
        settings = load_settings()
        claim_until = (now + timedelta(seconds=file_claim_seconds(settings.OUROBOROS_FILE_IO_TIMEOUT_SEC))).isoformat()
        file_id = file_id or uuid.uuid4().hex
        sequence = 0
        previous = None
        if producer_task and artifact_path:
            previous = self.session.scalar(
                self._files()
                .where(
                    CatalogFile.producer_task == producer_task,
                    CatalogFile.artifact_path == artifact_path,
                )
                .order_by(CatalogFile.artifact_sequence.desc())
                .limit(1)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
            if previous is not None:
                sequence = previous.artifact_sequence + 1
                file_id = (
                    previous.id
                    if previous.fingerprint == fingerprint and previous.status not in {"deleting", "deleted"}
                    else uuid.uuid4().hex
                )
        file = previous if previous is not None and previous.id == file_id else None
        if file is None:
            file = self.session.scalar(
                select(CatalogFile)
                .where(CatalogFile.id == file_id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
            if file is not None and (
                (file.owner_id, file.agent_id) != (self.scope.owner_id, self.scope.agent_id)
                or (
                    file.surface_id == "dialogs"
                    and (self.scope.surface_id != "dialogs" or file.chat_id != self.scope.chat_id)
                )
            ):
                raise FileError()
        if file is not None:
            if file.fingerprint != fingerprint:
                raise FileError("file identity conflicts with content", FileFailure.CONFLICT)
            if file.status in {"ready", "draft"}:
                if file.expires_at is not None and file.expires_at <= timestamp:
                    raise FileUnavailable()
                return file
            if file.status != "staging" or (file.expires_at or "") <= timestamp:
                raise FileUnavailable("upload is unavailable", FileFailure.CONFLICT)
            if (file.claim_until or "") > timestamp:
                raise FileTransferPending("upload is in progress", FileFailure.TRANSFER_IN_PROGRESS)
            self.session.add(
                CatalogFile(
                    id=uuid.uuid4().hex,
                    owner_id=file.owner_id,
                    agent_id=file.agent_id,
                    name=file.name,
                    mime=file.mime,
                    origin=file.origin,
                    surface_id=file.surface_id,
                    chat_id=file.chat_id,
                    storage_scope=file.storage_scope,
                    storage_key=file.storage_key,
                    status="deleting",
                    created_at=timestamp,
                    updated_at=timestamp,
                    expires_at=timestamp,
                )
            )
            file.claim_token = uuid.uuid4().hex
            file.storage_key = f"chat/{file.id}/{file.claim_token}/{file.name}"
            file.claim_until = claim_until
            self.session.flush()
            return file
        file = CatalogFile(
            id=file_id,
            owner_id=self.scope.owner_id,
            agent_id=self.scope.agent_id,
            folder_id=folder_id,
            name=self._new_file_name(name, folder_id, file_id),
            mime=mime,
            origin=origin,
            producer_task=producer_task,
            artifact_path=artifact_path,
            artifact_sequence=sequence,
            fingerprint=fingerprint,
            claim_token=uuid.uuid4().hex,
            claim_until=claim_until,
            surface_id=str(self.scope.surface_id),
            chat_id=self.scope.chat_id,
            storage_key=storage_key or f"chat/{file_id}/{name}",
            status="staging",
            created_at=timestamp,
            updated_at=timestamp,
            expires_at=(now + timedelta(hours=settings.OUROBOROS_USER_FILE_TTL_HOURS)).isoformat(),
        )
        self.session.add(file)
        self.session.flush()
        return file

    def complete(
        self, file_id: str, *, size: int, fingerprint: str, draft: bool, token: str | None, validation_version: int = 0
    ) -> CatalogFile:
        self.lock()
        file = self.file(file_id, available=False)
        now = database_now(self.conn).isoformat(timespec="microseconds")
        if (
            file.status != "staging"
            or file.claim_token != token
            or (file.expires_at is not None and file.expires_at <= now)
        ):
            raise FileError("upload no longer active", FileFailure.CONFLICT)
        file.size, file.fingerprint, file.validation_version = size, fingerprint, validation_version
        file.claim_token, file.claim_until = None, None
        file.status, file.updated_at = (
            ("draft" if draft else "ready"),
            now,
        )
        if not draft:
            file.expires_at = None
        self.session.flush()
        return file

    def release_claim(self, file_id: str, token: str | None) -> None:
        if token is None:
            return
        self.conn.execute(
            update(CatalogFile)
            .where(
                CatalogFile.id.in_(self._files().with_only_columns(CatalogFile.id)),
                CatalogFile.id == file_id,
                CatalogFile.status == "staging",
                CatalogFile.claim_token == token,
            )
            .values(claim_token=None, claim_until=None)
        )

    def record_fingerprint(self, file_id: str, fingerprint: str) -> None:
        self.lock()
        file = self.file(file_id)
        if not file.fingerprint:
            file.fingerprint = fingerprint
            self.session.flush()

    def record_validation(self, file_id: str, *, fingerprint: str, size: int, version: int) -> None:
        self.lock()
        result = self.conn.execute(
            update(CatalogFile)
            .where(
                CatalogFile.id == file_id,
                CatalogFile.owner_id == self.scope.owner_id,
                CatalogFile.agent_id == self.scope.agent_id,
                CatalogFile.status == "ready",
                CatalogFile.fingerprint.in_(("", fingerprint)),
            )
            .values(fingerprint=fingerprint, size=size, validation_version=version)
        )
        if not result.rowcount:
            raise FileError("file changed during validation", FileFailure.CONFLICT)

    def delete_file(self, file_id: str, *, draft_only: bool = False) -> None:
        self.lock()
        file = self.file(file_id, available=False)
        if draft_only and file.status != "draft":
            raise FileError("upload is already attached to a message", FileFailure.CONFLICT)
        if file.status in {"deleting", "deleted"}:
            return
        if file.status in {"staging", "draft"}:
            file.artifact_path = ""
        now = database_now(self.conn).isoformat(timespec="microseconds")
        file.status, file.expires_at, file.updated_at = "deleting", now, now
        self.session.flush()

    def delete_unlinked_file(self, file_id: str) -> None:
        self.lock()
        if self.conn.scalar(select(MessageFile.file_id).where(MessageFile.file_id == file_id).limit(1)) is None:
            self.delete_file(file_id)

    @staticmethod
    def claim_cleanup(conn: Connection, *, limit: int, lease_seconds: float) -> list[CatalogFile]:
        now = database_now(conn)
        timestamp = now.isoformat(timespec="microseconds")
        with Session(conn, expire_on_commit=False, join_transaction_mode="rollback_only") as session:
            rows = list(
                session.scalars(
                    select(CatalogFile)
                    .where(
                        or_(
                            CatalogFile.status == "deleting",
                            and_(CatalogFile.status.in_(("draft", "staging")), CatalogFile.expires_at <= timestamp),
                        ),
                        or_(CatalogFile.claim_until.is_(None), CatalogFile.claim_until <= timestamp),
                    )
                    .order_by(CatalogFile.claim_until.asc().nulls_first(), CatalogFile.expires_at, CatalogFile.id)
                    .limit(limit)
                    .with_for_update(skip_locked=True)
                )
            )
            for file in rows:
                if file.status in {"staging", "draft"}:
                    file.artifact_path = ""
                file.status = "deleting"
                file.claim_token = uuid.uuid4().hex
                file.claim_until = (now + timedelta(seconds=lease_seconds)).isoformat()
            session.flush()
            return rows

    def finish_cleanup(self, file_id: str, token: str | None) -> bool:
        if token is None:
            return False
        now = database_now(self.conn).isoformat(timespec="microseconds")
        result = self.conn.execute(
            update(CatalogFile)
            .where(
                CatalogFile.id == file_id,
                CatalogFile.owner_id == self.scope.owner_id,
                CatalogFile.agent_id == self.scope.agent_id,
                CatalogFile.status == "deleting",
                CatalogFile.claim_token == token,
                CatalogFile.claim_until > now,
            )
            .values(status="deleted", claim_token=None, claim_until=None, expires_at=None, updated_at=now)
        )
        self.session.expire_all()
        return bool(result.rowcount)

    def edit_file(
        self, file_id: str, *, name: str | None = None, folder_id: str | None = None, move: bool = False
    ) -> CatalogFile:
        self.lock()
        file = self.file(file_id)
        if file.status != "ready":
            raise FileError()
        if name is not None:
            name = file_name(name)
            validate_attachment_metadata(name, file.mime, 0)
        if move:
            if folder_id is not None:
                self.folder(folder_id)
            file.folder_id = folder_id
        file.name = self._new_file_name(name if name is not None else file.name, file.folder_id, file.id)
        file.updated_at = database_now(self.conn).isoformat(timespec="microseconds")
        self.session.flush()
        return file

    def bind(self, file_ids: list[str], message_id: str, *, historical: bool = False) -> list[CatalogFile]:
        self.lock()
        ids = sorted(set(file_ids))
        if not ids:
            return []
        files = list(
            self.session.scalars(
                self._files()
                .where(CatalogFile.id.in_(ids))
                .order_by(CatalogFile.id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        )
        if len(files) != len(ids):
            raise FileError()
        now = database_now(self.conn).isoformat(timespec="microseconds")
        for file in files:
            if not historical:
                if file.status in {"deleting", "deleted"} or (file.expires_at is not None and file.expires_at <= now):
                    raise FileUnavailable()
                if file.status not in {"draft", "ready"}:
                    raise FileError()
                file.status, file.expires_at, file.updated_at = (
                    "ready",
                    None,
                    now,
                )
            self.session.add(
                MessageFile(
                    file_id=file.id, message_id=message_id, owner_id=self.scope.owner_id, agent_id=self.scope.agent_id
                )
            )
        self.session.flush()
        return files

    def entries(self, folder_id: str | None, *, q: str = "", cursor: str = "", limit: int = 50) -> EntryPage:
        if not 1 <= limit <= 100 or len(q) > 200:
            raise FileError("invalid page parameters", FileFailure.INVALID_INPUT)
        path = self.path(folder_id)
        query = union_all(
            select(Folder.id, Folder.name, literal("folder").label("kind")).where(
                Folder.owner_id == self.scope.owner_id,
                Folder.agent_id == self.scope.agent_id,
                Folder.parent_id == folder_id,
                Folder.name.contains(q, autoescape=True),
            ),
            self._files()
            .with_only_columns(CatalogFile.id, CatalogFile.name, literal("file").label("kind"))
            .where(
                CatalogFile.folder_id == folder_id,
                CatalogFile.status == "ready",
                CatalogFile.name.contains(q, autoescape=True),
            ),
        ).subquery()
        statement = select(query).order_by(query.c.kind.desc(), query.c.name, query.c.id)
        if cursor:
            try:
                parts = base64.urlsafe_b64decode(cursor.encode()).decode().split("\0")
                parent, search, kind, name, id_ = parts
                if (parent, search) != (folder_id or "", q) or kind not in {"file", "folder"}:
                    raise ValueError("cursor context differs")
            except (ValueError, UnicodeError) as exc:
                raise FileError("invalid cursor", FileFailure.INVALID_INPUT) from exc
            statement = statement.where(
                or_(query.c.kind < kind, and_(query.c.kind == kind, tuple_(query.c.name, query.c.id) > (name, id_)))
            )
        items = [Entry.model_validate(row) for row in self.conn.execute(statement.limit(limit + 1)).mappings()]
        next_cursor = None
        if len(items) > limit:
            items = items[:limit]
            last = items[-1]
            next_cursor = base64.urlsafe_b64encode(
                "\0".join((folder_id or "", q, last.kind, last.name, last.id)).encode()
            ).decode()
        return EntryPage(items=items, next_cursor=next_cursor, path=path)


MessageAttachments = dict[str, dict[str, AttachmentMetadata]]


def load_message_attachments(conn: Connection, message_ids: list[str]) -> MessageAttachments:
    attachments: MessageAttachments = {key: {} for key in message_ids}
    if message_ids:
        with Session(conn) as session:
            for message_id, file in session.execute(
                select(MessageFile.message_id, CatalogFile)
                .join(CatalogFile, CatalogFile.id == MessageFile.file_id)
                .where(MessageFile.message_id.in_(message_ids))
                .order_by(MessageFile.file_id)
            ):
                attachments[message_id][file.id] = attachment_metadata(file)
    return attachments


def merge_message_attachments(
    messages: list[ConversationMessage], attachments: MessageAttachments
) -> list[ConversationMessage]:
    for message in messages:
        ids = ATTACHMENT_IDS_ADAPTER.validate_python(message.metadata.pop("attachments", None) or [])
        remaining = dict(attachments[message.message_id])
        ordered = [remaining.pop(item.id) for item in ids if item.id in remaining]
        ordered.extend(remaining.values())
        if ordered:
            message.metadata["attachments"] = [{**item, "message_id": message.source_message_id} for item in ordered]
    return messages


def hydrate_attachments(conn: Connection, messages: list[ConversationMessage]) -> list[ConversationMessage]:
    return merge_message_attachments(messages, load_message_attachments(conn, [m.message_id for m in messages]))
