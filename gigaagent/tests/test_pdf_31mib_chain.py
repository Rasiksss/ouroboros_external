from types import SimpleNamespace

import httpx
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, NameObject


def test_31mib_pdf_validation_and_registration(tmp_path, monkeypatch):
    from new_core.identity import OwnerScope, SurfaceId
    from ouroboros.attachment_parser import _parse_pdf
    from ouroboros.storage import attachment_validation
    from ouroboros.storage.settings.schema import AppSettings
    from transport.dialogs import aifa_ingress

    settings = AppSettings()
    monkeypatch.setattr(attachment_validation, "load_settings", lambda: settings)
    pdf = tmp_path / "report.pdf"
    writer = PdfWriter()
    page = writer.add_blank_page(width=100, height=100)
    stream = DecodedStreamObject()
    stream.set_data(b" " * (31 * 1024 * 1024))
    page[NameObject("/Contents")] = writer._add_object(stream)
    writer.write(pdf)
    size = pdf.stat().st_size
    assert 31 * 1024 * 1024 <= size < 32 * 1024 * 1024
    attachment_validation.validate_attachment_metadata("report.pdf", "application/pdf", size)
    assert _parse_pdf(pdf, "validate") == ""

    scope = OwnerScope(owner_id="tenant", agent_id="main", surface_id=SurfaceId.DIALOGS, chat_id="chat")
    attachment = {"id": "file", "filename": "report.pdf", "mime": "application/pdf", "storage_key": "stored",
                  "owner_id": "tenant", "agent_id": "main", "chat_id": "chat", "surface_id": "dialogs"}
    transfer = []

    def copy(owner, storage_scope, key, path, *, max_bytes):
        assert size <= max_bytes
        path.write_bytes(pdf.read_bytes())

    def respond(request):
        assert int(request.headers["content-length"]) > size
        assert len(request.content) > size and b"%PDF-" in request.content
        transfer.append(request.url.path)
        return httpx.Response(200, json={"run_id": "run"})

    original = httpx.Client
    monkeypatch.setattr(aifa_ingress.httpx, "Client", lambda **kw: original(**kw, transport=httpx.MockTransport(respond)))
    configured = SimpleNamespace(OUROBOROS_TRUSTED_EXECUTABLE_SKILL_IDS=["distribution:aifa-engine"],
                                 AIFA_ENGINE_URL="http://aifa:8080", AIFA_ENGINE_TOKEN=SimpleNamespace(get_secret_value=lambda: "token"))
    assert aifa_ingress.register_turn(configured, scope, "message", "Extract", [attachment],
                                    SimpleNamespace(copy_to_path=copy)) == "run"
    assert transfer == ["/internal/dialogs/v1/turns"]
