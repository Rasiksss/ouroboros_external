from __future__ import annotations

import inspect

from finengine.skills.atoms import out_file
from finengine.skills.atoms.graph_atoms import build_registry


def test_all_a11_output_atoms_are_registered_with_publication_gates():
    registry = build_registry()
    report = registry.get("out.report")
    form = registry.get("out.form")
    file = registry.get("out.file")

    assert report is not None and report.mandatory_gates == ("verify.grounding", "verify.citation")
    assert form is not None and form.execution_kind == "deterministic"
    assert file is not None and file.execution_kind == "io"
    assert registry.get("out.narrative") is not None


def test_file_renderer_has_no_domain_or_ocr_dependencies():
    source = inspect.getsource(out_file)
    forbidden = (
        "two_stage_extractor",
        "formula_engine",
        "calc_formula",
        "read_source_text",
        "_extract_pdf",
        "_extract_xlsx",
        "EBITDA",
    )
    assert not [name for name in forbidden if name in source]
    assert "=SUM(" not in source and "cell.data_type = \"f\"" not in source
