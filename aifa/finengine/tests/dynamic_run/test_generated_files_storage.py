"""К16.3: сгенерированный файл хранится у настоящего владельца и не привязан к списку форматов (часть 16)."""
from __future__ import annotations

import pytest

from finengine.config import settings
from finengine.skills import sources


@pytest.fixture(autouse=True)
def _local_blobs(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "s3_endpoint", "")
    monkeypatch.setattr(settings, "data_dir", tmp_path)


@pytest.mark.parametrize("name", ["итоги.pptx", "отчёт.docx", "книга.xlsx", "схема.png", "архив.zip", "a.odp"])
def test_a_generated_file_of_any_document_format_is_stored_and_readable_by_its_owner(name) -> None:
    meta, key = sources.save_generated_bytes(7, "chat-1", name, b"PK\x03\x04data", "application/octet-stream")

    content, read_meta = sources.read_source_bytes(7, "chat-1", meta.file_id)

    assert content == b"PK\x03\x04data" and read_meta.filename == name
    assert key.endswith(f"{meta.file_id}.{name.rsplit('.', 1)[1]}")


def test_another_user_cannot_read_the_generated_file() -> None:
    meta, _ = sources.save_generated_bytes(7, "chat-1", "итоги.pptx", b"x", "application/octet-stream")

    assert sources.read_source_bytes(8, "chat-1", meta.file_id) is None
    assert sources.read_source_bytes(7, "chat-2", meta.file_id) is None


@pytest.mark.parametrize("name", ["run.exe", "go.bat", "x.sh", "y.ps1", "z.js", "w.dll", "без_расширения", "a.toolongext1"])
def test_executables_and_malformed_extensions_are_refused(name) -> None:
    with pytest.raises(ValueError):
        sources.save_generated_bytes(7, "chat-1", name, b"x", "application/octet-stream")


def test_an_empty_file_is_refused() -> None:
    with pytest.raises(ValueError, match="empty"):
        sources.save_generated_bytes(7, "chat-1", "итоги.pptx", b"", "application/octet-stream")
