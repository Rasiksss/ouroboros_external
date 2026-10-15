"""downloader.py — безопасное потоковое скачивание PDF. Полностью офлайн:
`httpx.MockTransport` вместо реальной сети (`pytest-asyncio_mode=auto`, см.
pyproject.toml — тесты `async def` без декоратора)."""
from __future__ import annotations

import httpx
import pytest

from finengine.skills.document_acquisition.downloader import (
    DownloadRejected,
    download_pdf_candidate,
)
from finengine.skills.document_acquisition.url_safety import UnsafeUrlError

_REAL_PDF = b"%PDF-1.4\n1 0 obj<<>>endobj\n%%EOF"
_HTML_BODY = b"<!DOCTYPE html><html><head><title>Oops</title></head><body>Not found</body></html>"


def _public_resolver(host_to_ip: dict[str, str]):
    def _resolve(hostname: str, port: int) -> list[str]:
        if hostname not in host_to_ip:
            raise OSError(f"нет такого хоста в тестовом резолвере: {hostname}")
        return [host_to_ip[hostname]]
    return _resolve


def _transport(handler):
    return httpx.MockTransport(handler)


def _responder(by_url: dict[str, httpx.Response]):
    def _handle(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        try:
            return by_url[url]
        except KeyError:
            raise AssertionError(f"неожиданный запрос к {url}; известны: {list(by_url)}") from None
    return _handle


# ── успешный путь ─────────────────────────────────────────────────────────


async def test_successful_official_pdf_download():
    resolver = _public_resolver({"www.cbr.ru": "93.184.216.34"})
    transport = _transport(_responder({
        "https://www.cbr.ru/report.pdf": httpx.Response(
            200, content=_REAL_PDF, headers={"content-type": "application/pdf"},
        ),
    }))
    result = await download_pdf_candidate(
        "https://www.cbr.ru/report.pdf", transport=transport, resolver=resolver,
    )
    assert result.content == _REAL_PDF
    assert result.final_url == "https://www.cbr.ru/report.pdf"
    assert result.http_status == 200
    assert result.redirect_chain == ()


# ── редиректы: безопасный проходит, каждый hop проверяется заново ─────────


async def test_redirect_to_safe_host_is_followed():
    resolver = _public_resolver({"old.example.com": "93.184.216.34", "new.example.com": "93.184.216.35"})
    transport = _transport(_responder({
        "https://old.example.com/report.pdf": httpx.Response(
            302, headers={"location": "https://new.example.com/report.pdf"},
        ),
        "https://new.example.com/report.pdf": httpx.Response(
            200, content=_REAL_PDF, headers={"content-type": "application/pdf"},
        ),
    }))
    result = await download_pdf_candidate(
        "https://old.example.com/report.pdf", transport=transport, resolver=resolver,
    )
    assert result.final_url == "https://new.example.com/report.pdf"
    assert result.redirect_chain == ("https://new.example.com/report.pdf",)


async def test_redirect_to_private_address_is_rejected():
    """Первый URL безопасен, но Location ведёт на метаданные облака — каждый
    hop проходит ТУ ЖЕ проверку, что исходный URL, поэтому это отвергается,
    а не тихо проходит следом за httpx."""
    resolver = _public_resolver({"old.example.com": "93.184.216.34"})
    transport = _transport(_responder({
        "https://old.example.com/report.pdf": httpx.Response(
            302, headers={"location": "http://169.254.169.254/latest/meta-data/"},
        ),
    }))
    with pytest.raises(UnsafeUrlError):
        await download_pdf_candidate(
            "https://old.example.com/report.pdf", transport=transport, resolver=resolver,
        )


async def test_too_many_redirects_is_rejected():
    resolver = _public_resolver({f"h{i}.example.com": "93.184.216.34" for i in range(10)})
    responses = {
        f"https://h{i}.example.com/": httpx.Response(302, headers={"location": f"https://h{i+1}.example.com/"})
        for i in range(6)
    }
    transport = _transport(_responder(responses))
    with pytest.raises(DownloadRejected):
        await download_pdf_candidate("https://h0.example.com/", transport=transport, resolver=resolver)


# ── содержимое: HTML-как-PDF, огромный файл, битый PDF, недоступность ────


async def test_html_masquerading_as_pdf_is_rejected_even_with_pdf_content_type():
    """Content-Type врёт (`application/pdf`), но тело — HTML: магическая
    сигнатура важнее заголовка."""
    resolver = _public_resolver({"evil.example.com": "93.184.216.34"})
    transport = _transport(_responder({
        "https://evil.example.com/report.pdf": httpx.Response(
            200, content=_HTML_BODY, headers={"content-type": "application/pdf"},
        ),
    }))
    with pytest.raises(DownloadRejected):
        await download_pdf_candidate(
            "https://evil.example.com/report.pdf", transport=transport, resolver=resolver,
        )


async def test_html_masquerading_as_pdf_is_rejected():
    resolver = _public_resolver({"evil.example.com": "93.184.216.34"})
    transport = _transport(_responder({
        "https://evil.example.com/report.pdf": httpx.Response(
            200, content=_HTML_BODY, headers={"content-type": "text/html"},
        ),
    }))
    with pytest.raises(DownloadRejected):
        await download_pdf_candidate(
            "https://evil.example.com/report.pdf", transport=transport, resolver=resolver,
        )


async def test_huge_file_is_rejected_via_content_length_header():
    resolver = _public_resolver({"big.example.com": "93.184.216.34"})
    transport = _transport(_responder({
        "https://big.example.com/report.pdf": httpx.Response(
            200, content=_REAL_PDF,
            headers={"content-type": "application/pdf", "content-length": str(200 * 1024 * 1024)},
        ),
    }))
    with pytest.raises(DownloadRejected):
        await download_pdf_candidate(
            "https://big.example.com/report.pdf", transport=transport, resolver=resolver,
        )


async def test_huge_file_is_rejected_during_streaming_even_without_content_length():
    """Content-Length можно не прислать вовсе — лимит обязан сработать по
    факту переданных байт, не полагаясь только на заголовок."""
    resolver = _public_resolver({"big.example.com": "93.184.216.34"})
    oversized = _REAL_PDF + b"0" * (1024 * 1024)
    transport = _transport(_responder({
        "https://big.example.com/report.pdf": httpx.Response(
            200, content=oversized, headers={"content-type": "application/pdf"},
        ),
    }))
    with pytest.raises(DownloadRejected):
        await download_pdf_candidate(
            "https://big.example.com/report.pdf", transport=transport, resolver=resolver,
            max_bytes=1024,
        )


async def test_corrupt_pdf_signature_present_is_accepted_by_downloader():
    """Скачивание — не полный разбор. %PDF- в начале — этого достаточно для
    downloader.py; повреждённую ВНУТРЕННЮЮ структуру ловит `probe_document`
    сразу после регистрации (разделение ответственности, см. докстринг
    модуля) — downloader не должен дублировать эту проверку."""
    resolver = _public_resolver({"corrupt.example.com": "93.184.216.34"})
    corrupt = b"%PDF-1.4\ngarbage garbage no xref no trailer"
    transport = _transport(_responder({
        "https://corrupt.example.com/report.pdf": httpx.Response(
            200, content=corrupt, headers={"content-type": "application/pdf"},
        ),
    }))
    result = await download_pdf_candidate(
        "https://corrupt.example.com/report.pdf", transport=transport, resolver=resolver,
    )
    assert result.content == corrupt


async def test_wrong_content_type_but_real_pdf_bytes_is_accepted():
    """Content-Type — подсказка, не источник истины: сервер, отдающий PDF
    под `application/octet-stream`, не должен быть отвергнут."""
    resolver = _public_resolver({"octet.example.com": "93.184.216.34"})
    transport = _transport(_responder({
        "https://octet.example.com/report.pdf": httpx.Response(
            200, content=_REAL_PDF, headers={"content-type": "application/octet-stream"},
        ),
    }))
    result = await download_pdf_candidate(
        "https://octet.example.com/report.pdf", transport=transport, resolver=resolver,
    )
    assert result.content == _REAL_PDF


async def test_http_error_status_is_rejected():
    resolver = _public_resolver({"gone.example.com": "93.184.216.34"})
    transport = _transport(_responder({
        "https://gone.example.com/report.pdf": httpx.Response(404, content=b"not found"),
    }))
    with pytest.raises(DownloadRejected):
        await download_pdf_candidate(
            "https://gone.example.com/report.pdf", transport=transport, resolver=resolver,
        )


async def test_unavailable_source_connection_error_is_rejected():
    def _boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    resolver = _public_resolver({"down.example.com": "93.184.216.34"})
    transport = _transport(_boom)
    with pytest.raises(DownloadRejected):
        await download_pdf_candidate(
            "https://down.example.com/report.pdf", transport=transport, resolver=resolver,
        )


async def test_initial_url_pointing_at_private_ip_is_rejected_before_any_request():
    def _fail_if_called(request: httpx.Request) -> httpx.Response:
        raise AssertionError("не должен был дойти до HTTP-запроса — SSRF-проверка обязана остановить раньше")

    resolver = _public_resolver({"internal.example.com": "127.0.0.1"})
    transport = _transport(_fail_if_called)
    with pytest.raises(UnsafeUrlError):
        await download_pdf_candidate(
            "https://internal.example.com/report.pdf", transport=transport, resolver=resolver,
        )
