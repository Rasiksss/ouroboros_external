"""Каталог файлов ouroboros — единственное место, где служба кладёт и читает оригиналы (CONTRACTS.md §6).

Только HTTP каталога (`POST/GET/DELETE /api/files*`), клиента S3 ouroboros у службы нет (ADR-0012, F2).
Владельца называет `ouro_client.owner_headers` (F12). Допустимые форматы и пределы — каталога (ADR-0026):
служба своего списка не держит, кроме двух форматов, которые она отклоняет сама до обращения к каталогу.

Отказ по файлу — значение (`FileRefused` с текстом для пользователя); сбой системы — `OuroUnavailable`.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterator
from dataclasses import dataclass
from functools import cache
from pathlib import PurePath

import httpx

from finengine.assistant.ouro_client import OuroUnavailable, owner_headers
from finengine.config import Settings, settings

FILES_PATH = "/api/files"
_CHUNK_BYTES = 64 * 1024
_LEGACY_OFFICE = frozenset({".xls", ".doc"})
_FORMAT_CODE = "attachment_type_not_allowed"
_SIZE_CODE = "attachment_too_large"


@dataclass(frozen=True)
class StoredFile:
    """Файл принят каталогом. `sha256` служба считает сама до отправки: это её запись о байтах."""

    file_id: str
    size: int
    sha256: str


@dataclass(frozen=True)
class FileRefused:
    """Бизнес-отказ: файл не принят; `text` — что сказать пользователю."""

    text: str


def legacy_office_refusal(filename: str) -> FileRefused | None:
    """XLS и DOC служба отклоняет сама, до обращения к каталогу; конвертации нет (ADR-0026)."""
    if PurePath(filename).suffix.lower() not in _LEGACY_OFFICE:
        return None
    return FileRefused(f"«{filename}»: формат XLS/DOC не поддерживается. Сохраните файл как XLSX/DOCX и загрузите снова.")


def _refusal(filename: str, code: str) -> FileRefused:
    if code == _FORMAT_CODE:
        return FileRefused(
            f"«{filename}»: такой формат не поддерживается. Сохраните файл в другом формате "
            "(например, PDF, XLSX или DOCX) и загрузите снова."
        )
    if code == _SIZE_CODE:
        return FileRefused(f"«{filename}»: файл слишком большой. Загрузите файл меньшего размера.")
    return FileRefused(f"«{filename}»: файл повреждён или не соответствует своему формату. Проверьте его и загрузите снова.")


class FileStream:
    """Содержимое файла потоком; закрыть обязан тот, кто открыл (`close`)."""

    def __init__(self, response: httpx.Response) -> None:
        self._response = response

    @property
    def media_type(self) -> str:
        return self._response.headers.get("content-type", "application/octet-stream").split(";")[0].strip()

    def chunks(self) -> Iterator[bytes]:
        yield from self._response.iter_bytes(_CHUNK_BYTES)

    def close(self) -> None:
        self._response.close()


class CatalogFiles:
    def __init__(self, base_url: str, *, timeout_sec: float, transport: httpx.BaseTransport | None = None) -> None:
        # Без переменных окружения: это внутренний переход службы в ouroboros (ADR-0028), системный прокси
        # хоста ему не нужен и ломал бы его.
        self._http = httpx.Client(
            base_url=base_url.rstrip("/"), timeout=timeout_sec, trust_env=False, transport=transport
        )

    @classmethod
    def from_settings(cls, config: Settings) -> CatalogFiles:
        return cls(config.ouroboros_url, timeout_sec=config.ouroboros_file_timeout_sec)

    def upload(self, owner_id: str, filename: str, content: bytes) -> StoredFile | FileRefused:
        # Тип части пуст намеренно: каталог сверяет MIME с расширением и без типа определяет его сам,
        # а любой назначенный нами (`octet-stream`, догадка `mimetypes`) он отклонил бы.
        response = self._request("POST", FILES_PATH, owner_id, files={"file": (filename, content, "")})
        if response.status_code == 201:
            return StoredFile(str(response.json()["id"]), len(content), hashlib.sha256(content).hexdigest())
        if response.status_code in (400, 413, 415, 422):
            return _refusal(filename, _code_of(response))
        raise OuroUnavailable(f"каталог файлов: загрузка — HTTP {response.status_code}")

    def open(self, owner_id: str, file_id: str) -> FileStream | None:
        """Поток содержимого; `None` — у этого владельца такого файла нет."""
        request = self._http.build_request("GET", f"{FILES_PATH}/{file_id}/content", headers=owner_headers(owner_id))
        try:
            response = self._http.send(request, stream=True)
        except httpx.HTTPError as failure:
            raise OuroUnavailable(type(failure).__name__) from failure
        if response.status_code == 200:
            return FileStream(response)
        response.close()
        if response.status_code == 404:
            return None
        raise OuroUnavailable(f"каталог файлов: чтение — HTTP {response.status_code}")

    def read(self, owner_id: str, file_id: str) -> bytes | None:
        stream = self.open(owner_id, file_id)
        if stream is None:
            return None
        try:
            return b"".join(stream.chunks())
        except httpx.HTTPError as failure:
            raise OuroUnavailable(type(failure).__name__) from failure
        finally:
            stream.close()

    def delete(self, owner_id: str, file_id: str) -> bool:
        """`True` — каталог подтвердил удаление (у него оно идемпотентно); `False` — такого файла у владельца нет."""
        response = self._request("DELETE", f"{FILES_PATH}/{file_id}", owner_id)
        if response.status_code in (200, 204):
            return True
        if response.status_code == 404:
            return False
        raise OuroUnavailable(f"каталог файлов: удаление — HTTP {response.status_code}")

    def _request(self, method: str, path: str, owner_id: str, **kwargs: object) -> httpx.Response:
        try:
            response = self._http.request(method, path, headers=owner_headers(owner_id), **kwargs)  # type: ignore[arg-type]
        except httpx.HTTPError as failure:
            raise OuroUnavailable(type(failure).__name__) from failure
        if response.status_code >= 500 or response.status_code == 429:
            raise OuroUnavailable(f"каталог файлов: HTTP {response.status_code}")
        return response


def _code_of(response: httpx.Response) -> str:
    try:
        payload = response.json()
    except ValueError:
        return ""
    return str(payload.get("code") or "") if isinstance(payload, dict) else ""


@cache
def catalog() -> CatalogFiles:
    return CatalogFiles.from_settings(settings)
