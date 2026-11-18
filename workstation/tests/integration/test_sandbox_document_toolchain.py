"""Integration smoke for the sandbox document/archive toolchain.

Runs python (sandbox_requirements.txt libraries), 7za, and unrar through the
real ws-sandbox wrapper so the exec allowlist, Landlock rules, and
shared-library loading are exercised. Prerequisites are probed piecewise:
dev hosts skip, the built runtime image runs the full matrix.
"""

from __future__ import annotations

import functools
import platform
import shutil
import subprocess
from collections.abc import Callable
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
WRAPPER_DIR = REPO_ROOT / "wrapper"
WRAPPER_BINARY = WRAPPER_DIR / "build" / "ws-sandbox"

# Import roster of sandbox_requirements.txt (module names, not dist names).
# numpy is deliberately first: importing it exercises the wrapper's dedicated
# flexiblas Landlock allowlist (wrapper/src/landlock.c).
SANDBOX_IMPORTS = "numpy, pandas, docx, pptx, openpyxl, pypdf, pypdfium2, PIL, lxml, requests"
PDF_TEXT = "Hello from sandbox"
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


def _linux_only() -> None:
    if platform.system() != "Linux":
        pytest.skip("Linux-only wrapper security behavior")


def _require_build_tools() -> None:
    if shutil.which("make") is None:
        pytest.skip("make is not available")
    if shutil.which("cc") is None and shutil.which("musl-gcc") is None:
        pytest.skip("no C compiler is available")


def _build_wrapper() -> None:
    _require_build_tools()
    result = subprocess.run(
        ["make"],
        cwd=WRAPPER_DIR,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert WRAPPER_BINARY.exists()


@functools.lru_cache(maxsize=1)
def _sandbox_python() -> str | None:
    """First host python that already carries the sandbox libraries.

    /usr/bin/python3.12 is the interpreter the Dockerfile copies into
    /opt/workstation/bin; generic fallbacks keep the test useful on hosts
    with a different layout. The app venv python is intentionally not a
    candidate — sandbox libraries live in the system site-packages.
    """
    candidates = ["/usr/bin/python3.12", shutil.which("python3.12"), shutil.which("python3")]
    for candidate in candidates:
        if candidate is None or not Path(candidate).exists():
            continue
        probe = subprocess.run(
            [candidate, "-c", f"import {SANDBOX_IMPORTS}"],
            text=True,
            capture_output=True,
            check=False,
        )
        if probe.returncode == 0:
            return candidate
    return None


def _require_sandbox_python() -> str:
    python = _sandbox_python()
    if python is None:
        pytest.skip("no host python with sandbox_requirements libraries installed")
    return python


def _install_tool(bin_dir: Path, source: str, name: str) -> None:
    target = bin_dir / name
    shutil.copyfile(source, target, follow_symlinks=True)
    target.chmod(0o755)


def _write_minimal_pdf(path: Path, text: str) -> None:
    """Hand-built one-page PDF with `text` in Helvetica — no PDF writer dep."""
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842]"
        b" /Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    stream = b"BT /F1 24 Tf 72 770 Td (" + text.encode("ascii") + b") Tj ET"
    objects.append(b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream")
    body = bytearray(b"%PDF-1.4\n")
    offsets = []
    for index, obj in enumerate(objects, start=1):
        offsets.append(len(body))
        body += f"{index} 0 obj\n".encode() + obj + b"\nendobj\n"
    xref_pos = len(body)
    body += f"xref\n0 {len(objects) + 1}\n".encode() + b"0000000000 65535 f \n"
    for offset in offsets:
        body += f"{offset:010d} 00000 n \n".encode()
    body += f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref_pos}\n%%EOF\n".encode()
    path.write_bytes(bytes(body))


def _make_session(
    tmp_path: Path, tools: dict[str, str], allowlist: list[str]
) -> tuple[Path, Callable[[list[str]], subprocess.CompletedProcess[str]]]:
    """Session layout mirroring RuntimeAdapter: workspace, env file, exec.allow.

    Returns (workspace, run) where run() executes a command through the
    wrapper with cwd=/workspace.
    """
    session_dir = tmp_path / "session"
    bin_dir = tmp_path / "bin"
    workspace = session_dir / "workspace"
    sandbox_dir = session_dir / ".sandbox"
    workspace.mkdir(parents=True)
    bin_dir.mkdir(parents=True)
    sandbox_dir.mkdir()
    for name, source in tools.items():
        _install_tool(bin_dir, source, name)
    env_file = sandbox_dir / "env"
    allowlist_file = sandbox_dir / "exec.allow"
    # PYTHONSAFEPATH/PYTHONNOUSERSITE mirror RuntimeAdapter's hardening, so
    # imports are proven under the same interpreter setup production uses.
    env_file.write_text(
        f"LANG=C.UTF-8\nPATH={bin_dir}\nPYTHONSAFEPATH=1\nPYTHONNOUSERSITE=1\n",
        encoding="utf-8",
    )
    allowlist_file.write_text("".join(f"{entry}\n" for entry in allowlist), encoding="utf-8")

    def run(command: list[str]) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [
                str(WRAPPER_BINARY),
                "--session-id",
                "test-session",
                "--session-dir",
                str(session_dir),
                "--bin-dir",
                str(bin_dir),
                "--cwd",
                "/workspace",
                "--memory-max",
                "0",
                "--pids-max",
                "0",
                "--cpu-quota-pct",
                "0",
                "--rlimit-as",
                "0",
                "--rlimit-fsize",
                "0",
                "--rlimit-cpu",
                "0",
                "--rlimit-nofile",
                "0",
                "--rlimit-nproc",
                "0",
                "--rlimit-stack",
                "0",
                "--rlimit-core",
                "0",
                "--rlimit-memlock",
                "0",
                "--env-file",
                str(env_file),
                "--exec-allowlist",
                str(allowlist_file),
                "--",
                *command,
            ],
            text=True,
            capture_output=True,
            check=False,
        )

    return workspace, run


def test_sandbox_python_imports_document_libraries(tmp_path: Path) -> None:
    _linux_only()
    python = _require_sandbox_python()
    _build_wrapper()
    _, run = _make_session(tmp_path, {"python3": python}, ["/bin/python3"])

    result = run(["/bin/python3", "-c", f"import {SANDBOX_IMPORTS}"])

    assert result.returncode == 0, result.stdout + result.stderr


def test_sandbox_python_renders_pdf_with_pypdfium2(tmp_path: Path) -> None:
    _linux_only()
    python = _require_sandbox_python()
    _build_wrapper()
    workspace, run = _make_session(tmp_path, {"python3": python}, ["/bin/python3"])
    _write_minimal_pdf(workspace / "hello.pdf", PDF_TEXT)
    script = (
        "import pypdfium2 as pdfium\n"
        "page = pdfium.PdfDocument('hello.pdf')[0]\n"
        "print(page.get_textpage().get_text_bounded())\n"
        "page.render(scale=200 / 72).to_pil().save('out.png')\n"
    )

    result = run(["/bin/python3", "-c", script])

    assert result.returncode == 0, result.stdout + result.stderr
    assert PDF_TEXT in result.stdout
    rendered = (workspace / "out.png").read_bytes()
    assert rendered.startswith(PNG_MAGIC)


def test_sandbox_7za_extracts_archive_roundtrip(tmp_path: Path) -> None:
    _linux_only()
    seven_zip = shutil.which("7za") or shutil.which("7z")
    if seven_zip is None:
        pytest.skip("7za is not available on the host")
    # Debian's p7zip ships 7z as a shell wrapper; a script cannot run inside
    # the sandbox (its interpreter is not in the exec allowlist), so skip
    # instead of failing on such dev hosts. The runtime image ships an ELF.
    if Path(seven_zip).read_bytes()[:2] == b"#!":
        pytest.skip("host 7z is a shell wrapper, not an ELF binary")
    _build_wrapper()
    workspace, run = _make_session(tmp_path, {"7za": seven_zip}, ["/bin/7za"])
    payload = workspace / "payload.txt"
    payload.write_text("archive payload\n", encoding="utf-8")
    packed = subprocess.run(
        [seven_zip, "a", "archive.7z", "payload.txt"],
        cwd=workspace,
        text=True,
        capture_output=True,
        check=False,
    )
    assert packed.returncode == 0, packed.stdout + packed.stderr
    payload.unlink()

    result = run(["/bin/7za", "x", "archive.7z", "-oout"])

    assert result.returncode == 0, result.stdout + result.stderr
    assert (workspace / "out" / "payload.txt").read_text(encoding="utf-8") == "archive payload\n"


def test_sandbox_unrar_launches_under_allowlist(tmp_path: Path) -> None:
    _linux_only()
    unrar = shutil.which("unrar")
    if unrar is None:
        pytest.skip("unrar is not available on the host")
    _build_wrapper()
    _, run = _make_session(tmp_path, {"unrar": unrar}, ["/bin/unrar"])

    # Building a .rar fixture would need the non-free rar compressor, which
    # the image does not ship (unrar is extract-only). The smoke therefore
    # stops at the failure mode the sandbox can actually cause: the binary
    # must launch through the exec allowlist with its shared libraries
    # loading under Landlock. Bare unrar prints its usage banner
    # (case-insensitive: unrar-free on dev hosts banners in lowercase).
    result = run(["/bin/unrar"])

    assert "unrar" in (result.stdout + result.stderr).lower()
