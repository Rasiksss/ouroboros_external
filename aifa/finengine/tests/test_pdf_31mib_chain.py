import asyncio

import httpx
import pytest
from fastapi import FastAPI

from test_dialogs_ingress import ingress, payload  # noqa: F401
from test_document_extractor_ocr import ocr  # noqa: F401


@pytest.mark.asyncio
async def test_aifa_multipart_accepts_31mib_and_persists_exact_bytes(ingress, monkeypatch):  # noqa: F811
    module, documents, _ = ingress
    from finengine.assistant import tool_api
    monkeypatch.setattr(tool_api.settings, "aifa_engine_token", "token")
    monkeypatch.setattr(module, "_parse_document", lambda meta: None)
    app = FastAPI()
    app.include_router(module.router)
    body = b"%PDF-1.7\n" + b" " * (31 * 1024 * 1024)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://aifa") as client:
        response = await client.post("/internal/dialogs/v1/turns", headers={"Authorization": "Bearer token"},
                                     files=[("registration", (None, payload(module).model_dump_json(), "application/json")),
                                            ("files", ("report.pdf", body, "application/pdf"))])
    assert response.status_code == 200, response.text
    doc = documents[response.json()["doc_ids"][0]]
    assert module.blobs.get_blob_store().get(doc.file_path) == body


def test_ocr_upload_carries_31mib_as_one_multipart_file(ocr, tmp_path, monkeypatch):  # noqa: F811
    pdf = tmp_path / "report.pdf"
    pdf.write_bytes(b"%PDF-1.7\n" + b" " * (31 * 1024 * 1024))
    calls = []

    def respond(request):
        calls.append(request.url.path)
        if request.url.path.endswith("upload"):
            assert int(request.headers["content-length"]) > pdf.stat().st_size
            assert pdf.read_bytes() in request.content
            return httpx.Response(200, json={"resource_id": "source"})
        if request.url.path == "/api/extract":
            return httpx.Response(200, json={"resource_id": "result"})
        return httpx.Response(200, json={"text": "Full report without page markers", "tables": []})

    original = httpx.AsyncClient
    monkeypatch.setattr(ocr.httpx, "AsyncClient", lambda **kw: original(**kw, transport=httpx.MockTransport(respond)))
    result = asyncio.run(ocr.call_idp_ocr(pdf))
    assert result.full_text == "Full report without page markers"
    assert calls == ["/api/v2/files/upload", "/api/extract", "/api/v2/files/download_bytes"]
