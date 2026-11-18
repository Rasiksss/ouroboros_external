"""Pydantic schemas for session lifecycle API."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import ConfigDict, Field

from app.schemas.base import BaseSchema, ReadSchema


def _to_camel(name: str) -> str:
    parts = name.split("_")
    return parts[0] + "".join(part.capitalize() for part in parts[1:])


class CamelBaseSchema(BaseSchema):
    model_config = ConfigDict(extra="forbid", alias_generator=_to_camel, populate_by_name=True)


class CamelReadSchema(ReadSchema):
    model_config = ConfigDict(extra="ignore", from_attributes=True, alias_generator=_to_camel, populate_by_name=True)


class SessionQuotaSchema(CamelReadSchema):
    max_file_bytes: int = Field(description="Максимальный размер одного загружаемого файла в байтах.")
    max_total_bytes: int = Field(description="Максимальный суммарный размер файлов в rootfs одной сессии.")
    exec_timeout_sec: int = Field(description="Timeout выполнения команды по умолчанию, если timeoutSec не передан.")


class SessionWorkspacePersistenceRequest(CamelBaseSchema):
    scope_id: str = Field(
        min_length=1,
        max_length=128,
        description=(
            "Opaque идентификатор durable workspace scope. Не должен содержать сырой пользовательский sessionKey."
        ),
        examples=["3c8f9f4d8b6a..."],
    )
    root: Literal["/workspace"] = Field(
        default="/workspace",
        description="Единственный поддерживаемый durable root внутри Workstation session.",
    )


class SessionWorkspacePersistenceState(CamelReadSchema):
    enabled: bool = Field(description="true, если durable workspace store включен на сервере.")
    root: str | None = Field(default=None, description="Persisted root для этой сессии, если запрошен.")
    scope_id: str | None = Field(default=None, description="Opaque persisted workspace scope.")
    restored_revision_id: str | None = Field(
        default=None,
        description="Revision, синхронно восстановленная при create.",
    )
    snapshot_revision_id: str | None = Field(default=None, description="Revision, сохраненная при terminate.")


class SessionCreateRequest(CamelBaseSchema):
    idle_timeout_sec: int | None = Field(
        default=None,
        description="Опциональный idle timeout в секундах. Не должен превышать жесткий лимит сервиса.",
        examples=[900],
    )
    absolute_ttl_sec: int | None = Field(
        default=None,
        description="Опциональный абсолютный TTL сессии в секундах. Не должен превышать жесткий лимит сервиса.",
        examples=[3600],
    )
    workspace_persistence: SessionWorkspacePersistenceRequest | None = Field(
        default=None,
        description="Опциональное синхронное восстановление durable /workspace по opaque scope.",
    )


class ExecRequest(CamelBaseSchema):
    command: list[str] = Field(
        min_length=1,
        examples=[["bash", "-lc", "pwd && ls"]],
        description=(
            "Команда в формате argv. В local runtime короткое имя executable резолвится "
            "через EXEC_ALLOWLIST перед execve() внутри ws-sandbox."
        ),
    )
    cwd: str = Field(
        default="/",
        description=(
            "Виртуальная рабочая директория внутри rootfs сессии. По умолчанию используется корень "
            "сессии (`/`), который всегда существует. Любой другой путь создаётся лениво на первом exec."
        ),
    )
    env: dict[str, str] = Field(
        default_factory=dict,
        description="Переопределения environment. Ключи должны быть разрешены в ENV_ALLOWLIST.",
        examples=[{"LANG": "C.UTF-8"}],
    )
    stdin: str | None = Field(default=None, description="Опциональный stdin payload для команды.")
    stdin_encoding: Literal["utf8", "base64"] = Field(
        default="utf8",
        description="Кодировка stdin. API принимает utf8 или base64.",
    )
    timeout_sec: int | None = Field(
        default=None,
        ge=1,
        description="Опциональный timeout команды в секундах. Если не передан, используется quota execTimeoutSec.",
        examples=[30],
    )


class ExecResponse(CamelReadSchema):
    exit_code: int = Field(description="Код завершения процесса. Для timeout используется 124.")
    stdout: str = Field(description="Захваченный stdout, при необходимости обрезанный до EXEC_OUTPUT_LIMIT_BYTES.")
    stderr: str = Field(description="Захваченный stderr, при необходимости обрезанный до EXEC_OUTPUT_LIMIT_BYTES.")
    stdout_truncated: bool = Field(description="true, если stdout был больше настроенного лимита вывода.")
    stderr_truncated: bool = Field(description="true, если stderr был больше настроенного лимита вывода.")
    duration_ms: int = Field(description="Длительность выполнения команды в миллисекундах.")
    timed_out: bool = Field(description="true, если сервис остановил команду по timeout.")
    workspace_root: str = Field(
        description=(
            "Абсолютный host-путь корня сессии. Все виртуальные пути (cwd, /workspace/...) "
            "разрешаются относительно него — агент может строить абсолютные пути как "
            "`{workspaceRoot}/<virtual_path_без_ведущего_слэша>`."
        ),
    )


class FileUploadResponse(CamelReadSchema):
    path: str = Field(description="Нормализованный виртуальный путь, записанный внутри rootfs сессии.")
    bytes: int = Field(description="Количество записанных байт.")
    sha256: str = Field(description="SHA-256 загруженного содержимого.")


class FileDownloadRequest(CamelBaseSchema):
    path: str = Field(description="Виртуальный путь скачиваемого файла. Выход за пределы rootfs запрещен.")


class FileListRequest(CamelBaseSchema):
    path: str = Field(
        default="/",
        description=(
            "Виртуальный путь файла или директории для просмотра. По умолчанию — корень сессии (`/`), "
            "который всегда существует."
        ),
    )
    recursive: bool = Field(default=True, description="Если true, дочерние директории обходятся рекурсивно.")
    max_depth: int = Field(
        default=5,
        description="Максимальная глубина рекурсивного обхода. Не может быть отрицательной.",
    )
    include_sha256: bool = Field(default=True, description="Если true, для файлов рассчитывается sha256.")


class FileNodeResponse(CamelReadSchema):
    name: str = Field(description="Имя файлового узла.")
    path: str = Field(description="Нормализованный виртуальный путь.")
    kind: str = Field(description="Тип узла: file или dir.")
    bytes: int | None = Field(default=None, description="Размер файла в байтах. Для директорий null.")
    sha256: str | None = Field(default=None, description="sha256 файла, если расчет был запрошен и применим.")
    children: list[FileNodeResponse] = Field(default_factory=list, description="Дочерние узлы для директорий.")


class SessionRead(CamelReadSchema):
    id: str = Field(description="Непрозрачный идентификатор сессии из X-Session-Id.")
    status: str = Field(description="Текущий lifecycle-статус сессии.")
    created_at: datetime = Field(description="UTC-время создания сессии.")
    updated_at: datetime | None = Field(description="UTC-время последнего обновления, если есть.")
    last_used_at: datetime = Field(description="UTC-время последнего успешного действия в сессии.")
    expires_at: datetime = Field(description="UTC-время абсолютного истечения сессии.")
    file_bytes_used: int = Field(description="Отслеживаемый суммарный размер файлов в rootfs сессии.")
    fail_reason: str | None = Field(description="Причина ошибки для failed-сессий, иначе null.")
    quotas: SessionQuotaSchema = Field(description="Эффективные файловые и exec-квоты для клиента.")
    workspace_persistence: SessionWorkspacePersistenceState | None = Field(
        default=None,
        description="Состояние durable /workspace для этой сессии.",
    )


class SessionCreateResponse(CamelReadSchema):
    id: str = Field(description="Непрозрачный идентификатор сессии из X-Session-Id.")
    status: str = Field(description="Текущий lifecycle-статус сессии.")
    token: str | None = Field(
        default=None,
        description=(
            "Bearer token возвращается только при создании новой сессии. При reuse ready-сессии не возвращается."
        ),
    )
    created_at: datetime = Field(description="UTC-время создания сессии.")
    expires_at: datetime = Field(description="UTC-время абсолютного истечения сессии.")
    quotas: SessionQuotaSchema = Field(description="Эффективные файловые и exec-квоты для клиента.")
    workspace_persistence: SessionWorkspacePersistenceState | None = Field(
        default=None,
        description="Состояние durable /workspace для этой сессии.",
    )


class SessionTerminateResponse(CamelReadSchema):
    id: str = Field(description="Идентификатор завершенной сессии.")
    status: str = Field(description="Терминальный статус сессии.")
    terminated_at: datetime = Field(
        serialization_alias="terminatedAt",
        description="UTC-время перехода сессии в терминальное состояние.",
    )


class SessionAuthInfo(CamelReadSchema):
    session_id: str
    bearer_token: str
    actor: str


class SessionAuditEventRead(CamelReadSchema):
    id: str
    session_id: str
    ts: datetime
    kind: str
    actor: str
    payload: dict[str, Any]


FileNodeResponse.model_rebuild()
