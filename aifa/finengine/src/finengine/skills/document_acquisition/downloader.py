"""Безопасное скачивание бинарного PDF — Срез 7 A5.

`llm/tools.py::_h_fetch_url` НЕ переиспользуется и не расширяется: она отдаёт
`resp.text` (декодированный HTML), безусловно следует redirect'ам
(`follow_redirects=True`, без единой проверки хоста) и рассчитана на текстовые
страницы, а не на бинарный файл под SSRF-чувствительным сценарием — сам бриф
среза прямо запрещает считать её безопасным PDF-загрузчиком.

Redirect'ы обрабатываются ВРУЧНУЮ (`follow_redirects=False`) — КАЖДЫЙ hop
проходит ТУ ЖЕ проверку `url_safety.validate_url_for_fetch`, что исходный URL:
иначе первый адрес безопасен, а `Location` одного из редиректов ведёт на
`169.254.169.254`, и загрузчик послушно идёт следом.

Размер ограничивается ВО ВРЕМЯ стриминга, а не только по `Content-Length` —
тот можно не прислать или указать заведомо меньшим, чем реальное тело.

Разбор глубже сигнатуры (шифрование, битая структура, число страниц) —
не здесь: `probe_document`/`textify_document` (Срез 1/5a) уже умеют это и
запускаются сразу после регистрации (`registration.py`) — не дублируем."""
from __future__ import annotations

from dataclasses import dataclass

import httpx

from .url_safety import Resolver, domain_matches, split_scheme_and_host, validate_url_for_fetch

MAX_DOWNLOAD_BYTES = 50 * 1024 * 1024  # тот же порядок, что sources.py:MAX_BYTES (ручная загрузка)
DOWNLOAD_TIMEOUT_S = 30.0
MAX_REDIRECTS = 5
MAX_PUBLIC_FILE_BYTES = 2 * 1024 * 1024  # атом-загрузка: ряды данных, не документы

_PDF_MAGIC = b"%PDF-"
_HTML_MARKERS = (b"<!doctype html", b"<html", b"<head", b"<body", b"<?xml")


class DownloadRejected(Exception):
    """Скачивание отвергнуто до или во время передачи — причина в `.args[0]`.

    Отдельно от `UnsafeUrlError` (SSRF/адрес): здесь про содержимое, лимиты
    и HTTP-статус, а не про то, куда идёт запрос."""


@dataclass(frozen=True)
class DownloadResult:
    content: bytes
    final_url: str
    content_type: str
    http_status: int
    redirect_chain: tuple[str, ...]  # URL'ы, через которые прошли (без исходного)
    charset: str = ""  # кодировка из заголовка Content-Type; пусто — сервер её не назвал


def _looks_like_html(head: bytes) -> bool:
    """Дешёвая сигнатурная проверка «это HTML под видом PDF», ДО того как
    содержимое дойдёт до `probe_document` — чтобы отбить sec-005/17.md п.5
    сразу на шаге скачивания, с понятной причиной в проброшенной ошибке."""
    lowered = head[:512].lower().lstrip()
    return any(lowered.startswith(marker) for marker in _HTML_MARKERS)


async def download_pdf_candidate(
    url: str,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
    resolver: Resolver | None = None,
    max_bytes: int = MAX_DOWNLOAD_BYTES,
    timeout_s: float = DOWNLOAD_TIMEOUT_S,
    max_redirects: int = MAX_REDIRECTS,
) -> DownloadResult:
    """Скачать `url` как бинарный PDF с полной проверкой на каждом шаге.

    `transport`/`resolver` — точки подмены для тестов, тот же приём DI, что
    `ocr_pages` у `doc.textify`: реальная сеть не требуется ни разу в тестах
    этого модуля (`httpx.MockTransport` + канонический резолвер-двойник).

    Бросает `UnsafeUrlError` (адрес/SSRF) или `DownloadRejected` (содержимое/
    лимиты/HTTP-статус) — вызывающий (`registration.py`) не обязан их
    различать для пользовательского сообщения, но разные типы дают точную
    причину в логах без сетевых деталей (C12)."""
    result = await _download_checked(
        url, accept="application/pdf,*/*;q=0.1", allowed_domain=None, transport=transport,
        resolver=resolver, max_bytes=max_bytes, timeout_s=timeout_s, max_redirects=max_redirects,
    )
    if _looks_like_html(result.content):
        raise DownloadRejected(
            f"{result.final_url}: тело — HTML, а не PDF (content-type был {result.content_type!r})"
        )
    if not result.content.startswith(_PDF_MAGIC):
        raise DownloadRejected(
            f"{result.final_url}: сигнатура файла не %PDF- (content-type был {result.content_type!r})"
        )
    return result


async def _download_checked(
    url: str,
    *,
    accept: str,
    allowed_domain: str | None,
    transport: httpx.AsyncBaseTransport | None,
    resolver: Resolver | None,
    max_bytes: int,
    timeout_s: float,
    max_redirects: int,
) -> DownloadResult:
    """Общий цикл: проверить адрес → GET → редирект вручную → лимит размера.

    Содержимое не разбирается — это дело вызывающего. `allowed_domain` (если задан)
    сверяется на КАЖДОМ hop: редирект на другой узел отвергается."""
    resolver_kwargs = {} if resolver is None else {"resolver": resolver}
    current_url = url
    redirect_chain: list[str] = []
    client_kwargs: dict = {"timeout": timeout_s, "follow_redirects": False}
    if transport is not None:
        client_kwargs["transport"] = transport

    async with httpx.AsyncClient(**client_kwargs) as client:
        for _hop in range(max_redirects + 1):
            # Проверка адреса — И для исходного URL, И для каждого redirect hop.
            validate_url_for_fetch(current_url, **resolver_kwargs)
            if allowed_domain is not None:
                host = split_scheme_and_host(current_url)[1]
                if not domain_matches(host, allowed_domain):
                    raise DownloadRejected(
                        f"{current_url}: узел {host} вне разрешённого {allowed_domain}"
                    )
            try:
                async with client.stream(
                    "GET", current_url,
                    headers={
                        "User-Agent": "AIFA-CLAW document-acquisition/1.0 (+report downloader)",
                        "Accept": accept,
                    },
                ) as resp:
                    if resp.is_redirect:
                        location = resp.headers.get("location")
                        if not location:
                            raise DownloadRejected(f"{current_url}: redirect без Location")
                        current_url = str(httpx.URL(current_url).join(location))
                        redirect_chain.append(current_url)
                        continue

                    if resp.status_code >= 400:
                        raise DownloadRejected(f"{current_url}: HTTP {resp.status_code}")

                    content_type = (resp.headers.get("content-type") or "").split(";")[0].strip().lower()
                    charset = resp.charset_encoding or ""

                    content_length = resp.headers.get("content-length")
                    if content_length is not None:
                        try:
                            if int(content_length) > max_bytes:
                                raise DownloadRejected(
                                    f"{current_url}: Content-Length {content_length} превышает лимит {max_bytes}"
                                )
                        except ValueError:
                            pass  # некорректный заголовок — полагаемся на потоковый лимит ниже

                    body = bytearray()
                    async for chunk in resp.aiter_bytes():
                        body.extend(chunk)
                        if len(body) > max_bytes:
                            raise DownloadRejected(
                                f"{current_url}: тело превысило лимит {max_bytes} байт во время передачи"
                            )
                    content = bytes(body)
            except httpx.HTTPError as exc:
                raise DownloadRejected(
                    f"{current_url}: сетевая ошибка {exc.__class__.__name__}: {exc}"
                ) from exc

            return DownloadResult(
                content=content, final_url=current_url, content_type=content_type,
                http_status=resp.status_code, redirect_chain=tuple(redirect_chain), charset=charset,
            )
        raise DownloadRejected(f"{url}: превышен лимит redirect'ов ({max_redirects})")


async def download_public_file(
    url: str,
    *,
    allowed_domain: str,
    transport: httpx.AsyncBaseTransport | None = None,
    resolver: Resolver | None = None,
    max_bytes: int = MAX_PUBLIC_FILE_BYTES,
    timeout_s: float = DOWNLOAD_TIMEOUT_S,
    max_redirects: int = MAX_REDIRECTS,
) -> DownloadResult:
    """Публичный файл данных (CSV, JSON, текст) или страница для атома-загрузки гибридного планировщика.

    Те же проверки адреса на каждом hop, что у PDF; кроме того, каждый hop обязан остаться
    на `allowed_domain` — профиль атома разрешает ровно этот узел. Содержимое не проверяется."""
    return await _download_checked(
        url, accept="text/csv,application/json,text/plain,text/html;q=0.9,*/*;q=0.1", allowed_domain=allowed_domain,
        transport=transport, resolver=resolver, max_bytes=max_bytes, timeout_s=timeout_s,
        max_redirects=max_redirects,
    )


__all__ = [
    "MAX_DOWNLOAD_BYTES", "MAX_PUBLIC_FILE_BYTES", "DOWNLOAD_TIMEOUT_S", "MAX_REDIRECTS",
    "DownloadRejected", "DownloadResult", "download_pdf_candidate", "download_public_file",
]
