"""C0.1/C8: форма враждебного набора `tests/golden/security/`.

Не проверяет поведение атомов (их нет) — проверяет, что сам эталонный набор
имеет форму, на которую такие тесты можно будет написать позже: разнообразие
видов атаки, непустой payload, и то, что бинарные фикстуры реально враждебны
по форме (битый PDF нельзя открыть, zip реально имеет высокий коэффициент
сжатия), а не просто произвольные файлы с обманчивым именем.
"""
from __future__ import annotations

import json
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent / "golden" / "security"
CASES = json.loads((ROOT / "cases.json").read_text(encoding="utf-8"))

EXPECTED_KINDS = {
    "prompt_injection_in_document_text",
    "ssrf_url",
    "path_traversal",
    "number_substitution",
    "evidence_bypass",
    "secret_leak_attempt",
    "infinite_chain_marker",
    "memory_poisoning_marker",
}


def test_at_least_twenty_cases():
    assert len(CASES) >= 20


def test_every_case_has_a_non_empty_payload_and_expected_behavior():
    for case in CASES:
        assert case["payload"], f"{case['id']}: пустой payload"
        assert case["expected_safe_behavior"].strip(), f"{case['id']}: не описано ожидаемое поведение"


def test_every_declared_kind_is_covered_at_least_once():
    kinds = {c["kind"] for c in CASES}
    missing = EXPECTED_KINDS - kinds
    assert not missing, f"нет ни одного примера для видов: {missing}"


def test_ids_are_unique():
    ids = [c["id"] for c in CASES]
    assert len(ids) == len(set(ids)), "повторяющиеся id в cases.json"


def test_corrupted_pdf_is_actually_unparseable():
    pdf = fitz_open_or_none(ROOT / "files" / "corrupted.pdf")
    assert pdf is None, "фикстура должна быть НЕ открываемой валидным PDF-парсером"


def fitz_open_or_none(path: Path):
    import fitz  # PyMuPDF

    try:
        return fitz.open(str(path))
    except Exception:
        return None


def test_ratio_bomb_has_a_high_compression_ratio():
    path = ROOT / "files" / "ratio_bomb.zip"
    with zipfile.ZipFile(path) as zf:
        info = zf.infolist()[0]
        ratio = info.file_size / info.compress_size
    assert ratio > 100, f"коэффициент сжатия слишком низкий для фикстуры-бомбы: {ratio:.1f}x"
    assert info.file_size <= 64 * 1024 * 1024, "фикстура обязана быть безопасно маленькой в абсолюте"
