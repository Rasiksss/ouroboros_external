"""К16.1: `FileArtifact` — файл любого формата и родословная от динамического результата (часть 16)."""
from __future__ import annotations

import hashlib

import pytest
from pydantic import ValidationError

from finengine.contracts import FileArtifact
from finengine.contracts.transport import ResultEvent


def _file(**changes) -> dict:
    data = {
        "run_id": "run-1", "file_kind": "pptx",
        "mime_type": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        "filename": "итоги.pptx", "template_id": "eph.build_deck", "template_version": "abc123def456",
        "source_result": {
            "result_type": "DynamicArtifact", "result_id": "ev-1", "schema_version": "1.0",
            "run_id": "run-1", "evidence_version": "1.0", "result_hash": "0" * 64,
        },
        "content_hash": hashlib.sha256(b"deck").hexdigest(),
        "object_store_location": "users/1/projects/p/sources/f.pptx", "size_bytes": 4,
    }
    data.update(changes)
    return data


def test_a_file_of_any_format_with_a_dynamic_source_is_a_valid_artifact() -> None:
    artifact = FileArtifact(**_file(summary="слайдов: 3"))

    restored = FileArtifact.model_validate_json(artifact.model_dump_json())

    assert restored.file_kind == "pptx"
    assert restored.source_result.result_type == "DynamicArtifact"
    assert restored.summary == "слайдов: 3"


def test_the_summary_is_optional_so_existing_artifacts_still_validate() -> None:
    assert FileArtifact(**_file()).summary == ""


@pytest.mark.parametrize("kind", ["docx", "xlsm", "png", "zip", "odp", "xlsx", "pdf", "csv", "json"])
def test_any_document_extension_is_a_valid_kind(kind) -> None:
    assert FileArtifact(**_file(file_kind=kind)).file_kind == kind


@pytest.mark.parametrize("kind", ["exe", "bat", "sh", "ps1", "js", "msi", "dll", "jar", "vbs", "cmd"])
def test_an_executable_kind_is_refused(kind) -> None:
    with pytest.raises(ValidationError):
        FileArtifact(**_file(file_kind=kind))


@pytest.mark.parametrize("kind", ["", "XLSX", ".xlsx", "x" * 9, "pp tx", "../x"])
def test_a_malformed_kind_is_refused(kind) -> None:
    with pytest.raises(ValidationError):
        FileArtifact(**_file(file_kind=kind))


def test_a_pptx_artifact_passes_the_transport_envelope() -> None:
    artifact = FileArtifact(**_file())

    event = ResultEvent(run_id="run-1", sequence=1, delivery_status="complete", result=artifact.model_dump(mode="json"))

    assert event.result.file_kind == "pptx"
