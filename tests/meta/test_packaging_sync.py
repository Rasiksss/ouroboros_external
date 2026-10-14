import pathlib
import re

import pytest

REPO = pathlib.Path(__file__).resolve().parents[2]


def test_pyproject_declares_valid_version():
    pyproject = (REPO / "pyproject.toml").read_text(encoding="utf-8")
    match = re.search(r'^version = "([^"]+)"', pyproject, flags=re.MULTILINE)
    assert match, "pyproject.toml must declare project version"
    assert re.fullmatch(r"\d+\.\d+\.\d+", match.group(1))


def test_pyproject_declares_every_top_level_package():
    """A package missing from the include list vanishes from a non-editable install.

    Editable installs hide the omission — the tree is on the path either way — so
    only a built image would show it, by which point ``ouroboros.agent`` fails to
    import at all because it imports ``background_memory``.
    """
    pyproject = (REPO / "pyproject.toml").read_text(encoding="utf-8")
    include = re.search(r"^include = \[(.*?)\]", pyproject, flags=re.MULTILINE | re.DOTALL)
    assert include, "pyproject.toml must declare packages.find include"
    patterns = set(re.findall(r'"([^"]+)"', include.group(1)))

    packages = {
        path.name
        for path in REPO.iterdir()
        if path.is_dir()
        and (path / "__init__.py").exists()
        and not path.name.startswith((".", "_", "tests"))  # test suites ship with the repo, not the wheel
    }
    missing = {name for name in packages if f"{name}*" not in patterns and name not in patterns}
    assert not missing, f"packages.find must include: {sorted(missing)}"
    assert "background_memory*" in patterns


#: Directories of Python that ship with the repo but not with the image.
_NOT_SHIPPED = {"tests", "tests_headless", "docs", "openspec", "scripts", "docker", "htmlcov", "logs"}


def test_the_local_image_copies_every_source_tree_the_entrypoints_import():
    """A tree missing from the image fails at import, in a tier that may not even use it.

    `security_config/` is how this was found: a namespace package with no `__init__.py`, so
    it is in neither `packages.find` nor the check above, and it was absent from the image
    while `server.py` imported it at module scope. A multi-tier deployment surfaced it as
    `ModuleNotFoundError: No module named 'security_config'` in the background-memory tier,
    which reads no MCP list at all — it only runs the refusal set every tier runs.
    """
    dockerfile = (REPO / "Dockerfile.local").read_text(encoding="utf-8")
    copied = set(re.findall(r"^COPY ([A-Za-z_][A-Za-z0-9_]*) \./", dockerfile, flags=re.MULTILINE))

    required = {
        path.name
        for path in REPO.iterdir()
        if path.is_dir() and path.name not in _NOT_SHIPPED and not path.name.startswith(".") and any(path.rglob("*.py"))
    }

    assert not required - copied, f"Dockerfile.local does not copy: {sorted(required - copied)}"


def test_safety_prompt_lists_current_protected_runtime_paths():
    """Every path the supervisor is told to protect must still exist.

    Pinning the sentence verbatim was worse than no test: when the tool registry was
    split into `ouroboros/tools/dispatch.py` and `new_core/tools/`, the assertion kept
    passing and the prompt went on protecting a deleted file, so edits to the code that
    actually holds the gates were no longer flagged. Resolving the paths catches that.
    """
    safety_md = (REPO / "prompts" / "SAFETY.md").read_text(encoding="utf-8")

    line = next(
        (ln for ln in safety_md.splitlines() if "safety-critical runtime paths" in ln),
        None,
    )
    assert line, "SAFETY.md must name the protected runtime paths"

    quoted = re.findall(r"`([^`]+)`", line)
    repo_paths = [p for p in quoted if "/" in p and not p.startswith(".github")]
    assert repo_paths, "expected the protected-path list to name repo paths"

    missing = [p for p in repo_paths if not (REPO / p).exists()]
    assert not missing, f"SAFETY.md protects paths that no longer exist: {missing}"

    # The tool gates are the reason this list exists; keep them named.
    assert "ouroboros/policy/safety.py" in repo_paths
    assert "ouroboros/tools/dispatch.py" in repo_paths


@pytest.mark.parametrize(
    "filename,deploy_env",
    [
        pytest.param(
            "Dockerfile",
            "production",
            id="headless-production",
            marks=pytest.mark.skipif(not (REPO / "Dockerfile").exists(), reason="Dockerfile not present in repo"),
        ),
        pytest.param(
            "Dockerfile.local",
            "development",
            id="laptop-development",
            marks=pytest.mark.skipif(
                not (REPO / "Dockerfile.local").exists(),
                reason="Dockerfile.local not present in repo",
            ),
        ),
    ],
)
def test_dockerfile_runtime_defaults(filename: str, deploy_env: str):
    dockerfile = (REPO / filename).read_text(encoding="utf-8")
    assert f"OUROBOROS_ENV={deploy_env}" in dockerfile
    assert "OUROBOROS_DATA_DIR=/data" in dockerfile
    assert "USER ouroboros" in dockerfile
    assert "18800" in dockerfile
    assert "alembic.ini" in dockerfile


def test_uv_lock_pins_a2a_sdk():
    lock = (REPO / "uv.lock").read_text(encoding="utf-8")
    assert 'name = "a2a-sdk"' in lock
    assert 'version = "1.0.3"' in lock.split('name = "a2a-sdk"')[1].split("[[package]]")[0]


def test_uv_lock_includes_openai_for_cloudru_llm():
    lock = (REPO / "uv.lock").read_text(encoding="utf-8")
    assert 'name = "openai"' in lock
    pyproject = (REPO / "pyproject.toml").read_text(encoding="utf-8")
    assert "openai>=" in pyproject
