from pathlib import Path

import pytest
from pypdf import PdfReader, PdfWriter

from ouroboros.attachment_parser import _parse_pdf


def write_pdf(path: Path, password=None, algorithm="RC4-128"):
    writer = PdfWriter()
    writer.add_blank_page(width=100, height=100)
    if password is not None:
        writer.encrypt(user_password=password, owner_password="owner-only", algorithm=algorithm)
    writer.write(path)


def test_plain_pdf_remains_readable(tmp_path):
    path = tmp_path / "plain.pdf"
    write_pdf(path)
    assert _parse_pdf(path, "validate") == ""
    assert "Page 1" in _parse_pdf(path, "extract")


@pytest.mark.parametrize("algorithm", ["RC4-128", "AES-128", "AES-256"])
def test_owner_protected_pdf_opens_with_empty_user_password(tmp_path, algorithm):
    path = tmp_path / "restricted.pdf"
    write_pdf(path, "", algorithm)
    original = path.read_bytes()
    assert PdfReader(path).is_encrypted
    assert _parse_pdf(path, "validate") == ""
    assert "Page 1" in _parse_pdf(path, "extract")
    assert path.read_bytes() == original


def test_nonempty_open_password_is_still_rejected(tmp_path):
    path = tmp_path / "locked.pdf"
    write_pdf(path, "required-password")
    with pytest.raises(ValueError, match="password-protected PDF"):
        _parse_pdf(path, "validate")
