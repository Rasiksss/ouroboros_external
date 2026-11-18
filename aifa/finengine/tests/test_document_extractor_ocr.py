"""Контурный OCR: PDF → gateway → extractor → страницы и таблицы AIFA."""

import asyncio
import importlib
import json
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest


TABLE = "| Показатель | 2024 |\n| --- | --- |\n| Активы | 242 988 046 |"
TEXT = f"===PAGE 1===\nТитульный лист\n\n===PAGE 3===\nБаланс\n\n{TABLE}"


@pytest.fixture
def ocr(monkeypatch):
    module = importlib.import_module("finengine.skills.aifa_baseline.idp_ocr_client")

    class Pdf:
        page_count = 3

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

    monkeypatch.setattr(module, "pymupdf", SimpleNamespace(open=lambda path: Pdf()))
    monkeypatch.setattr(module, "settings", SimpleNamespace(
        ocr_gateway_url="http://gateway.internal/",
        document_extractor_url="http://extractor.internal/",
        ocr_client_id="aifa-test-agent",
        ocr_processing_timeout_seconds=600.0,
        idp_ocr_parallel=4,
        idp_ocr_url="https://legacy.invalid/predict",
    ))

    def no_legacy_render(*args):
        raise AssertionError("Internal OCR must upload the PDF, not use legacy PNG /predict")

    monkeypatch.setattr(module, "_render_page_png", no_legacy_render)
    return module


@pytest.fixture
def payload():
    return {
        "text": TEXT,
        "tables": [{"idx": 0, "page": 3, "text": TABLE,
                    "original_text": TABLE, "position_range": [40, 120]}],
    }


def install_transport(monkeypatch, ocr, payload, *, fail_at=None):
    requests = []

    def handle(request):
        requests.append(request)
        step = len(requests)
        if step == fail_at:
            return httpx.Response(503, json={"detail": "unavailable"})
        if step == 1:
            assert str(request.url) == "http://gateway.internal/api/v2/files/upload"
            assert request.method == "POST"
            assert request.headers["client-id"] == "aifa-test-agent"
            assert b'name="file"' in request.content
            assert b"application/pdf" in request.content
            assert b"%PDF synthetic complete document" in request.content
            return httpx.Response(200, json={"resource_id": "source-id"})
        if step == 2:
            assert str(request.url) == "http://extractor.internal/api/extract"
            assert request.method == "POST"
            assert json.loads(request.content) == {"resource_id": "source-id", "spec": {}}
            assert request.extensions["timeout"]["read"] == 600.0
            return httpx.Response(200, json={"resource_id": "result /id"})
        assert step == 3
        assert request.url.path == "/api/v2/files/download_bytes"
        assert request.url.params["resource_id"] == "result /id"
        assert request.method == "GET"
        assert request.headers["client-id"] == "aifa-test-agent"
        return httpx.Response(200, json=payload)

    original_client = httpx.AsyncClient
    monkeypatch.setattr(ocr.httpx, "AsyncClient", lambda **kwargs: original_client(
        **kwargs, transport=httpx.MockTransport(handle)
    ))
    return requests


@pytest.mark.parametrize("wrapped", [False, True])
def test_internal_ocr_preserves_pages_tables_and_saved_response(
    monkeypatch, tmp_path, ocr, payload, wrapped
):
    downloaded = {"document": "report.pdf", "elapsed": 54.4, "ocr_result": payload} if wrapped else payload
    result = ocr._document_extractor_result(downloaded, 3)

    assert result.full_text == TEXT
    assert [page.page for page in result.pages] == [1, 3]
    assert result.pages[1].tables == [TABLE]
    assert result.full_tables == payload["tables"]
    assert result.diag["pages_total"] == 3
    assert result.diag["pages_success"] == 2
    assert result.diag["pages_failed"] == 1
    assert ocr.markdown_from_saved_idp_response(downloaded) == TEXT


@pytest.mark.parametrize("step", [1, 2, 3])
def test_internal_ocr_http_failure_stops_without_legacy_fallback(
    monkeypatch, tmp_path, ocr, payload, step
):
    requests = install_transport(monkeypatch, ocr, payload, fail_at=step)
    pdf = tmp_path / "report.pdf"
    pdf.write_bytes(b"%PDF synthetic complete document")

    with pytest.raises(httpx.HTTPStatusError):
        asyncio.run(ocr.call_idp_ocr(pdf))

    assert len(requests) == step
    assert all(request.url.host != "legacy.invalid" for request in requests)


@pytest.mark.parametrize("missing", ["ocr_gateway_url", "document_extractor_url"])
def test_internal_ocr_requires_both_service_urls(ocr, tmp_path, missing):
    setattr(ocr.settings, missing, "")
    with pytest.raises(RuntimeError, match="configured together"):
        asyncio.run(ocr.call_idp_ocr(tmp_path / "not-opened.pdf"))


@pytest.mark.parametrize("change", [
    {"text": ""},
    {"text": "===PAGE 1===\nOne\n===PAGE 1===\nDuplicate"},
    {"text": "===PAGE 4===\nOutside PDF"},
    {"tables": [{"page": 2, "text": TABLE}]},
    {"tables": [{"page": 3}]},
    {"tables": "not a list"},
])
def test_internal_ocr_rejects_results_without_reliable_page_binding(
    monkeypatch, tmp_path, ocr, payload, change
):
    payload.update(change)
    with pytest.raises(ValueError):
        ocr._document_extractor_result(payload, 3)


def test_internal_ocr_settings_read_environment(monkeypatch, tmp_path):
    from finengine.config import Settings

    monkeypatch.setenv("FINENGINE_OCR_GATEWAY_URL", "http://gateway.internal")
    monkeypatch.setenv("FINENGINE_DOCUMENT_EXTRACTOR_URL", "http://extractor.internal")
    monkeypatch.setenv("FINENGINE_OCR_CLIENT_ID", "aifa-test-agent")
    monkeypatch.setenv("FINENGINE_OCR_PROCESSING_TIMEOUT_SECONDS", "600")
    settings = Settings(_env_file=None, data_dir=tmp_path, docs_dir=tmp_path)
    assert settings.ocr_gateway_url == "http://gateway.internal"
    assert settings.document_extractor_url == "http://extractor.internal"
    assert settings.ocr_client_id == "aifa-test-agent"
    assert settings.ocr_processing_timeout_seconds == 600


def test_direct_idp_remains_available_when_internal_urls_are_unset(monkeypatch, tmp_path, ocr):
    ocr.settings.ocr_gateway_url = ""
    ocr.settings.document_extractor_url = ""
    monkeypatch.setattr(ocr, "_render_page_png", lambda *args: b"PNG page")
    requests = []

    def handle(request):
        requests.append(request)
        assert str(request.url) == "https://legacy.invalid/predict"
        assert b'name="structuredDocument"' in request.content
        return httpx.Response(200, json={"modelOCR": {"pages": [
            {"markdown": {"content": "Recognized page"}},
        ]}})

    original_client = httpx.AsyncClient
    monkeypatch.setattr(ocr.httpx, "AsyncClient", lambda **kwargs: original_client(
        **kwargs, transport=httpx.MockTransport(handle)
    ))
    raw_path = tmp_path / "legacy.json"
    result = asyncio.run(ocr.call_idp_ocr(tmp_path / "report.pdf", save_raw_to=raw_path))
    assert len(requests) == 3
    assert [page.page for page in result.pages] == [1, 2, 3]
    assert ocr.markdown_from_saved_idp_response(
        json.loads(raw_path.read_text(encoding="utf-8"))
    ) == result.full_text


def test_deployment_wires_internal_ocr_services_without_public_fallback():
    import yaml

    root = Path(__file__).resolve().parents[3]
    platform = yaml.safe_load((root / "main.yaml").read_text(encoding="utf-8"))
    app = yaml.safe_load((root / "aifa/desc.yaml").read_text(encoding="utf-8"))["app"]
    aliases = {entry["alias"] for entry in app["connections"]["endpoints"]}
    endpoints = {
        "FINENGINE_OCR_GATEWAY_URL": "gateway-v1",
        "FINENGINE_DOCUMENT_EXTRACTOR_URL": "document-extractor-v1",
    }
    for key, service in endpoints.items():
        assert key in aliases
        assert platform["endpoints"][key] == (
            f"http://{service}.ci09529287-aif-agnbf-af-rfa-dev.apps.a4x8eda3.k8s.delta.sbrf.ru:8080"
        )
    assert app["envs"]["FINENGINE_IDP_OCR_URL"] == ""
