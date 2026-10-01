import pathlib

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent


def _read(relative_path: str) -> str:
    return (REPO_ROOT / relative_path).read_text(encoding="utf-8")


def test_agents_is_a_short_router_with_bounded_refactoring_policy() -> None:
    agents = _read("AGENTS.md")

    for phrase in (
        "## Scoped Refactoring",
        "Priority: requested behavior",
        "Adjacent means a direct caller or callee",
        "Do not search for cleanup beyond the touched path",
        "same narrow tests",
        "debt ratchet",
        "defer",
        "docs/DEVELOPMENT.md",
    ):
        assert phrase in agents

    for stale_path in (
        "web/modules/",
        "ouroboros/safety.py",
        "ouroboros/config.py",
        "ouroboros/gateway/settings.py",
    ):
        assert stale_path not in agents


def test_development_owns_detailed_refactoring_and_debt_rules() -> None:
    development = _read("docs/DEVELOPMENT.md")

    for phrase in (
        "AGENTS.md",
        "short routing guide",
        "## Scoped Refactoring",
        "| Required |",
        "| Adjacent cleanup |",
        "| Broad |",
        "### Agent decision procedure",
        "## Known Debt Ratchets",
        "redaction",
        "exact command token",
        "Deferred work",
        "AppSettings",
        "local imports inside loops",
        "pass-through wrappers",
        "tests_headless/test_governance_docs.py",
    ):
        assert phrase in development


def test_development_names_the_current_core_governance_artifacts() -> None:
    development = _read("docs/DEVELOPMENT.md")

    assert (
        "`AGENTS.md`, `docs/ARCHITECTURE.md`, and `docs/DEVELOPMENT.md` are core governance artifacts."
    ) in development


def test_architecture_owns_current_dependency_map_not_change_procedure() -> None:
    architecture = _read("docs/ARCHITECTURE.md")

    for phrase in (
        "## Dependency Direction and Ownership",
        "app / gateway / ingress",
        "supervisor",
        "agent",
        "llm / tools / policy / storage",
        "legacy reverse imports",
    ):
        assert phrase in architecture

    assert "## Scoped Refactoring" not in architecture
    assert "Agent decision procedure" not in architecture


def test_architecture_has_one_current_runtime_map_and_names_the_active_migration() -> None:
    architecture = _read("docs/ARCHITECTURE.md")
    lines = architecture.splitlines()

    assert architecture.count("2. **`supervisor/`**") == 1
    for package in ("agent", "gateway", "ingress"):
        assert sum(line.startswith(f"  {package}/") for line in lines) == 1
    assert architecture.count("## Active new_core migration") == 1
    for owner in ("new_core/graph/", "new_core/loop/", "ouroboros/agent/loop_adapters.py"):
        assert owner in architecture


def test_governance_documents_do_not_duplicate_operational_content() -> None:
    agents = _read("AGENTS.md")
    development = _read("docs/DEVELOPMENT.md")
    architecture = _read("docs/ARCHITECTURE.md")

    assert "## Runtime profile (this branch)" not in agents
    assert "## Runtime profile (this branch)" not in development
    assert "## Runtime profile (this branch)" in architecture
    assert "## Known Debt Ratchets" not in architecture
    assert "AGENTS.md" in architecture
