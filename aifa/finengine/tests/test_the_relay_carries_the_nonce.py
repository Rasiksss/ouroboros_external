"""Slice 13.5: the station wrapper launches one canonical runtime process."""
from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1] / "station-skills"
WRAPPER = ROOT / "aifa-extract" / "scripts" / "extract_run.sh"


def _code_lines() -> list[str]:
    return [
        line for line in WRAPPER.read_text(encoding="utf-8").splitlines()
        if not line.lstrip().startswith("#")
    ]


def test_wrapper_exports_nonce_before_the_single_canonical_launcher():
    lines = _code_lines()
    export = next(i for i, line in enumerate(lines) if "export" in line and "FE_PROGRESS_NONCE" in line)
    launch = next(i for i, line in enumerate(lines) if "/../run.sh" in line)
    assert export < launch


def test_wrapper_has_no_extract_calc_relay_or_parallel_legacy_process():
    text = "\n".join(_code_lines())
    assert "aifa-calc" not in text
    assert "--no-final" not in text
    assert sum("/../run.sh" in line for line in _code_lines()) == 1


def test_wrapper_passes_stable_document_id_to_the_ingress():
    assert '--document-id "$DOC"' in "\n".join(_code_lines())
