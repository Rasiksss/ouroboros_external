"""HTTP routes for session lifecycle."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, File, Form, Header, Request, Response, UploadFile, status
from starlette.responses import FileResponse, StreamingResponse
from starlette.responses import Response as StarletteResponse

from app.core.config import settings
from app.routers.v1.session.dependencies import (
    AuthenticatedSession,
    document_session_auth,
    get_authenticated_session,
    get_session_serializer,
    get_session_service,
    get_session_stream_encoder,
    log_endpoint,
)
from app.schemas.common import ErrorResponse
from app.schemas.session import (
    ExecRequest,
    ExecResponse,
    FileDownloadRequest,
    FileListRequest,
    FileNodeResponse,
    FileUploadResponse,
    SessionCreateRequest,
    SessionCreateResponse,
    SessionRead,
    SessionTerminateResponse,
)
from app.serializers.session import SessionSerializer
from app.services.session.service import SessionService
from app.services.session.stream_encoder import SessionStreamEncoder
from app.utils.exceptions.common import QuotaExceededException

SESSION_ID_HEADER_DESCRIPTION = (
    "Непрозрачный идентификатор сессии. Для защищенных endpoint'ов должен совпадать "
    "с session_id в path и bearer token этой сессии."
)

CREATE_SESSION_ID_HEADER_DESCRIPTION = (
    "Непрозрачный идентификатор для создания или переиспользования сессии. "
    "Разрешены буквы, цифры, точка, underscore и дефис."
)

SESSION_AUTH_CONTRACT = (
    "Требует `Authorization: Bearer <token>` и `X-Session-Id`. "
    "Token возвращается только один раз при первом создании сессии."
)

ERROR_RESPONSES = {
    400: {"model": ErrorResponse, "description": "Некорректный запрос или ошибка валидации схемы."},
    401: {"model": ErrorResponse, "description": "Отсутствует, некорректен или не совпадает session auth."},
    403: {"model": ErrorResponse, "description": "Операция запрещена политикой команд, путей или environment."},
    404: {"model": ErrorResponse, "description": "Сессия или файл не найдены."},
    409: {"model": ErrorResponse, "description": "Операция недоступна в текущем состоянии сессии."},
    413: {"model": ErrorResponse, "description": "Превышен размер файла или суммарная файловая квота сессии."},
    500: {"model": ErrorResponse, "description": "Неожиданная ошибка сервиса или runtime."},
}

router = APIRouter(prefix="/v1/sessions", tags=["sessions"])


@router.post(
    "",
    status_code=status.HTTP_201_CREATED,
    response_model=SessionCreateResponse,
    response_model_exclude_none=True,
    summary="Создать или переиспользовать сессию",
    description=(
        "Создает изолированную рабочую сессию для переданного `X-Session-Id`. "
        "Если создается новая сессия, ответ будет HTTP 201 и содержит bearer token. "
        "Если переиспользуется существующая ready-сессия, ответ будет HTTP 200, а token повторно не возвращается. "
        "На стороне сервиса хранится только hash token."
    ),
    responses={
        200: {
            "model": SessionCreateResponse,
            "description": "Существующая ready-сессия переиспользована. Token не возвращается.",
        },
        201: {"model": SessionCreateResponse, "description": "Создана новая сессия. Token возвращается один раз."},
        400: ERROR_RESPONSES[400],
        409: ERROR_RESPONSES[409],
        500: ERROR_RESPONSES[500],
    },
)
async def create_session(
    payload: SessionCreateRequest,
    response: Response,
    x_session_id: Annotated[str, Header(description=CREATE_SESSION_ID_HEADER_DESCRIPTION)],
    service: SessionService = Depends(get_session_service),
    serializer: SessionSerializer = Depends(get_session_serializer),
    _: None = Depends(log_endpoint("create_session")),
) -> SessionCreateResponse:
    result = await service.create_session(session_id=x_session_id, payload=payload)
    response.status_code = status.HTTP_201_CREATED if result.created else status.HTTP_200_OK
    return serializer.serialize_create(result.session, token=result.token)


@router.get(
    "/{session_id}",
    response_model=SessionRead,
    dependencies=[Depends(document_session_auth)],
    summary="Получить состояние сессии",
    description=(
        f"{SESSION_AUTH_CONTRACT} Возвращает lifecycle-статус, timestamps, запрошенный network mode, "
        "отслеживаемое файловое использование и эффективные квоты."
    ),
    responses={code: ERROR_RESPONSES[code] for code in (401, 404, 500)},
)
async def get_session(
    authenticated: AuthenticatedSession = Depends(get_authenticated_session),
    service: SessionService = Depends(get_session_service),
    serializer: SessionSerializer = Depends(get_session_serializer),
    _: None = Depends(log_endpoint("get_session")),
) -> SessionRead:
    session = await service.get_session(authenticated.auth)
    return serializer.serialize_read(session)


@router.post(
    "/{session_id}:terminate",
    response_model=SessionTerminateResponse,
    dependencies=[Depends(document_session_auth)],
    summary="Завершить сессию",
    description=(
        f"{SESSION_AUTH_CONTRACT} Завершает сессию, удаляет ее rootfs и запрещает дальнейшие "
        "exec/file операции. Для уже завершенной сессии операция идемпотентна."
    ),
    responses={code: ERROR_RESPONSES[code] for code in (401, 404, 500)},
)
async def terminate_session(
    authenticated: AuthenticatedSession = Depends(get_authenticated_session),
    service: SessionService = Depends(get_session_service),
    serializer: SessionSerializer = Depends(get_session_serializer),
    _: None = Depends(log_endpoint("terminate_session")),
) -> SessionTerminateResponse:
    session = await service.terminate_session(authenticated.auth)
    return serializer.serialize_terminate(session)


@router.post(
    "/{session_id}/exec",
    response_model=ExecResponse,
    dependencies=[Depends(document_session_auth)],
    summary="Выполнить команду в сессии",
    description=(
        f"{SESSION_AUTH_CONTRACT} Выполняет одну команду через настроенный runtime. В local runtime "
        "короткие имена executable резолвятся через `EXEC_ALLOWLIST`, затем команда запускается через `ws-sandbox`. "
        "Policy layer валидирует cwd, env keys и запрещенные command tokens до вызова wrapper. "
        "Несколько exec-запросов в одной ready-сессии могут выполняться параллельно."
    ),
    responses={code: ERROR_RESPONSES[code] for code in (400, 401, 403, 404, 409, 500)},
)
async def exec_session(
    payload: ExecRequest,
    authenticated: AuthenticatedSession = Depends(get_authenticated_session),
    service: SessionService = Depends(get_session_service),
    serializer: SessionSerializer = Depends(get_session_serializer),
    _: None = Depends(log_endpoint("exec_session")),
) -> ExecResponse:
    result = await service.exec_command(authenticated.auth, payload)
    return serializer.serialize_exec(result, workspace_root=authenticated.session.rootfs_path)


@router.post(
    "/{session_id}/exec:stream",
    response_class=StreamingResponse,
    dependencies=[Depends(document_session_auth)],
    summary="Выполнить команду и стримить NDJSON events",
    description=(
        f"{SESSION_AUTH_CONTRACT} Контракт выполнения такой же, как у `/exec`, но stdout/stderr/exit возвращаются "
        "как события `application/x-ndjson`. При disconnect клиента процесс завершается. "
        "Стриминговые и обычные exec-запросы могут выполняться параллельно в одной ready-сессии."
    ),
    responses={
        200: {
            "description": "NDJSON-stream событий выполнения команды.",
            "content": {
                "application/x-ndjson": {
                    "schema": {"type": "string", "format": "binary"},
                }
            },
        },
        **{code: ERROR_RESPONSES[code] for code in (400, 401, 403, 404, 409, 500)},
    },
)
async def exec_session_stream(
    request: Request,
    payload: ExecRequest,
    authenticated: AuthenticatedSession = Depends(get_authenticated_session),
    service: SessionService = Depends(get_session_service),
    stream_encoder: SessionStreamEncoder = Depends(get_session_stream_encoder),
    _: None = Depends(log_endpoint("exec_session_stream")),
) -> StreamingResponse:
    stream = service.stream_command(
        authenticated.auth,
        payload,
        disconnect_checker=request.is_disconnected,
    )
    first_event = await anext(stream, None)

    async def event_stream():
        try:
            if first_event is not None:
                yield stream_encoder.encode(first_event)
            async for event in stream:
                yield stream_encoder.encode(event)
        finally:
            await stream.aclose()

    return StreamingResponse(
        event_stream(),
        media_type="application/x-ndjson",
    )


@router.post(
    "/{session_id}/files:upload",
    response_model=FileUploadResponse,
    dependencies=[Depends(document_session_auth)],
    summary="Загрузить файл в rootfs сессии",
    description=(
        f"{SESSION_AUTH_CONTRACT} Загружает один multipart-файл в виртуальный путь внутри rootfs сессии. "
        "`MAX_FILE_SIZE` проверяется во время чтения upload, а `MAX_TOTAL_BYTES` — перед записью файла. "
        "Path traversal и symlink escape за пределы rootfs запрещены."
    ),
    responses={code: ERROR_RESPONSES[code] for code in (400, 401, 403, 404, 409, 413, 500)},
)
async def upload_file(
    path: Annotated[
        str,
        Form(description="Виртуальный путь назначения внутри rootfs сессии, например /workspace/data.txt."),
    ],
    file: Annotated[
        UploadFile,
        File(description="Содержимое multipart-файла. Размер должен быть <= maxFileBytes для сессии."),
    ],
    authenticated: AuthenticatedSession = Depends(get_authenticated_session),
    service: SessionService = Depends(get_session_service),
    serializer: SessionSerializer = Depends(get_session_serializer),
    _: None = Depends(log_endpoint("upload_file")),
) -> FileUploadResponse:
    result = await service.upload_file(
        authenticated.auth,
        path=path,
        content=await _read_upload_bytes(file),
    )
    return serializer.serialize_upload(result)


@router.post(
    "/{session_id}/files:download",
    response_class=StarletteResponse,
    dependencies=[Depends(document_session_auth)],
    summary="Скачать файл из rootfs сессии",
    description=(
        f"{SESSION_AUTH_CONTRACT} Скачивает один обычный файл по виртуальному пути. Gateway резолвит путь "
        "внутри rootfs сессии и запрещает traversal или symlink escape."
    ),
    responses={
        200: {
            "description": "Бинарное содержимое файла.",
            "content": {
                "application/octet-stream": {
                    "schema": {"type": "string", "format": "binary"},
                }
            },
            "headers": {
                "X-File-Sha256": {
                    "description": "SHA-256 скачанного файла.",
                    "schema": {"type": "string"},
                },
                "X-File-Bytes": {
                    "description": "Размер скачанного файла в байтах.",
                    "schema": {"type": "string"},
                },
            },
        },
        **{code: ERROR_RESPONSES[code] for code in (400, 401, 403, 404, 409, 500)},
    },
)
async def download_file(
    payload: FileDownloadRequest,
    authenticated: AuthenticatedSession = Depends(get_authenticated_session),
    service: SessionService = Depends(get_session_service),
    _: None = Depends(log_endpoint("download_file")),
) -> StarletteResponse:
    result = await service.download_file(authenticated.auth, path=payload.path)
    return FileResponse(
        path=result.host_path,
        media_type="application/octet-stream",
        headers={
            "X-File-Sha256": result.sha256,
            "X-File-Bytes": str(result.bytes_count),
        },
    )


@router.post(
    "/{session_id}/files:list",
    response_model=FileNodeResponse,
    dependencies=[Depends(document_session_auth)],
    summary="Посмотреть файлы в rootfs сессии",
    description=(
        f"{SESSION_AUTH_CONTRACT} Возвращает список для одного виртуального пути внутри rootfs сессии. "
        "Рекурсивность, максимальная глубина и расчет sha256 управляются телом запроса."
    ),
    responses={code: ERROR_RESPONSES[code] for code in (400, 401, 403, 404, 409, 500)},
)
async def list_files(
    payload: FileListRequest,
    authenticated: AuthenticatedSession = Depends(get_authenticated_session),
    service: SessionService = Depends(get_session_service),
    serializer: SessionSerializer = Depends(get_session_serializer),
    _: None = Depends(log_endpoint("list_files")),
) -> FileNodeResponse:
    result = await service.list_files(authenticated.auth, payload)
    return serializer.serialize_file_node(result)


async def _read_upload_bytes(file: UploadFile) -> bytes:
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = await file.read(64 * 1024)
        if not chunk:
            break
        total += len(chunk)
        if total > settings.MAX_FILE_SIZE:
            raise QuotaExceededException(
                "file exceeds max file size quota",
                details={"maxFileBytes": settings.MAX_FILE_SIZE},
            )
        chunks.append(chunk)
    return b"".join(chunks)
