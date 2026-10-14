import fnmatch
import json
import pathlib
import re

from tests.helpers.parametrize import parametrize_from_dict

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent


def _read(relative_path: str) -> str:
    return (REPO_ROOT / relative_path).read_text(encoding="utf-8")


def test_governance_documents_do_not_duplicate_operational_content() -> None:
    agents = _read("AGENTS.md")
    development = _read("docs/DEVELOPMENT.md")
    architecture = _read("docs/ARCHITECTURE.md")

    assert "## Runtime profile (this branch)" not in agents
    assert "## Runtime profile (this branch)" not in development
    assert "## Runtime profile (this branch)" in architecture
    assert "## Known Debt Ratchets" not in architecture
    assert "AGENTS.md" in architecture


def _backticked_paths(text: str) -> set[str]:
    """Backticked tokens that name a repo path: contain '/', no glob/placeholder/prose."""
    candidates = set(re.findall(r"`([^`]+)`", text))
    return {
        candidate.rstrip("/")
        for candidate in candidates
        if "/" in candidate
        and not candidate.startswith(("/", "~"))
        and not any(character in candidate for character in " *<>()")
    }


def _rule_files() -> list[pathlib.Path]:
    return sorted((REPO_ROOT / ".rules").glob("*.md"))


def test_every_path_named_in_agent_instructions_exists() -> None:
    """A routing guide that points at a path that moved is worse than no guide."""
    sources = [REPO_ROOT / "AGENTS.md", REPO_ROOT / "CLAUDE.md", *_rule_files()]

    missing: list[str] = []
    for source in sources:
        for named in sorted(_backticked_paths(source.read_text(encoding="utf-8"))):
            if not (REPO_ROOT / named).exists():
                missing.append(f"{source.relative_to(REPO_ROOT)} -> {named}")

    assert not missing, "instructions name paths that do not exist: " + ", ".join(missing)


def test_rules_are_reachable_where_claude_code_loads_them() -> None:
    """Rule content lives in .rules/, but Claude Code only auto-loads path-scoped rules from
    .claude/rules. Either may be the symlink; what matters is that they are one directory,
    so a new rule file is picked up with no extra step."""
    canonical = REPO_ROOT / ".rules"
    loaded_from = REPO_ROOT / ".claude" / "rules"

    assert loaded_from.resolve() == canonical.resolve(), ".claude/rules must resolve to .rules/, or rules never load"
    assert {path.name for path in loaded_from.iterdir()} == {rule.name for rule in _rule_files()}


def test_every_rule_file_declares_the_paths_it_applies_to() -> None:
    for rule in _rule_files():
        text = rule.read_text(encoding="utf-8")
        assert text.startswith("---\npaths:\n"), f"{rule.name} has no paths: frontmatter"


def test_claude_md_imports_agents_md() -> None:
    """Claude Code reads CLAUDE.md, not AGENTS.md; without the import nothing loads."""
    assert (REPO_ROOT / "CLAUDE.md").read_text(encoding="utf-8").startswith("@AGENTS.md")


def test_invariants_are_one_short_file_not_a_section_of_a_long_one() -> None:
    """An invariant you must read before every behavior change cannot cost a 1400-line read."""
    invariants = _read("docs/INVARIANTS.md")

    numbered = [line for line in invariants.splitlines() if re.match(r"^\d+\. \*\*", line)]
    assert len(numbered) >= 1, "no numbered invariants found"
    assert len(invariants.splitlines()) < 60, "INVARIANTS.md stopped being one screen"

    architecture = _read("docs/ARCHITECTURE.md")
    assert "docs/INVARIANTS.md" in architecture, "ARCHITECTURE.md must route to the invariants"
    assert "**Config SSOT:**" not in architecture, "invariants restated in ARCHITECTURE.md — second source of truth"

    agents = _read("AGENTS.md")
    assert "docs/INVARIANTS.md" in agents
    assert "ARCHITECTURE.md` \u00a7Key invariants" not in agents, "AGENTS.md still routes to the moved section"


def test_codex_can_load_rules_without_a_dedicated_read_tool() -> None:
    """Shell-only agents need an explicit route independent of Claude's rule loader."""
    agents = " ".join(_read("AGENTS.md").split())
    for required in (
        "Codex and other agents",
        "explicitly read",
        "every matching",
        "before editing",
        "shell",
        "does not load",
        "scripts/resolve_rules.py",
        "all `paths:` declarations",
        "inspect every rule's frontmatter",
    ):
        assert required in agents, f"missing rule-delivery instruction: {required}"
    assert "Rules load through the file-reading tool only" not in agents


def test_task_workflow_routes_to_decisions_and_behavior_specs() -> None:
    """Shortening the router must not hide scope decisions or the spec-first workflow."""
    agents = " ".join(_read("AGENTS.md").split())
    for required in (
        "BACKLOG.md",
        "matching records",
        "revisit condition",
        "unless explicitly asked",
        "openspec/specs/",
        "OpenSpec change",
        "before implementation",
    ):
        assert required in agents, f"missing task-workflow instruction: {required}"


def test_handoff_is_an_append_only_scoped_decision_log() -> None:
    """The handoff records consequential assumptions without becoming another prompt payload."""
    agents = " ".join(_read("AGENTS.md").split())
    development = " ".join(_read("docs/DEVELOPMENT.md").split())
    assert "HANDOFF.md" in agents and "append" in agents
    for required in (
        "append-only",
        "uncertainty",
        "decision",
        "evidence",
        "revisit",
        "Do not rewrite",
        "secrets",
        "matching records",
        "runtime task context",
        "read-only",
    ):
        assert required in development, f"missing handoff contract: {required}"


@parametrize_from_dict(
    {
        "runtime_context": {
            "fragments": (
                "No development/self-improvement docs in task context",
                "not injected",
                "ouroboros/agent/context.py",
            )
        },
        "tool_surface": {"fragments": ("No self-mod tools", "production tool catalog", "repo_write", "run_shell")},
        "http_surface": {
            "fragments": ("Minimal HTTP attack surface", "browser ingress is opt-in", "explicit product decision")
        },
        "imports": {"fragments": ("Core import boundary", "import no application package", "injected interface")},
        "task_path": {
            "fragments": ("One task path", "build_task_graph", "no second loop entry point", "one catalog per process")
        },
        "trace": {"fragments": ("Two headers on every hop", "X-Trace-Id", "X-Agent-Id", "per request", "as data")},
        "owner_scope": {"fragments": ("OwnerScope minted once", "travel unchanged", "No layer re-derives")},
        "encryption": {
            "fragments": (
                "Encryption key required",
                "OUROBOROS_CREDENTIALS_ENCRYPTION_KEY",
                "exactly 64 bytes",
                "mandatory",
            )
        },
        "shutdown": {
            "fragments": ("Drain before shutdown", "must wait", "OUROBOROS_DRAIN_TIMEOUT_SEC", "before exiting")
        },
    }
)
def test_critical_invariant_obligations_survive_editing(fragments: tuple[str, ...]) -> None:
    """Check each obligation independently of numbering, wrapping, and additional invariants."""
    text = re.sub(r"[`*]", "", " ".join(_read("docs/INVARIANTS.md").split()))
    for fragment in fragments:
        assert fragment in text, f"missing invariant obligation: {fragment}"


def test_independent_review_remains_a_completion_requirement() -> None:
    agents = " ".join(_read("AGENTS.md").split())
    for required in (
        "An independent review",
        "clean context",
        "Self-assessment",
        "not review",
        "docs/INVARIANTS.md",
        "§Coding Rules",
    ):
        assert required in agents


def test_permission_denials_ship_with_the_repo() -> None:
    """Denials that live only on one machine protect only that machine."""
    settings_path = REPO_ROOT / ".claude" / "settings.json"
    assert settings_path.exists(), ".claude/settings.json is missing"

    denied = json.loads(settings_path.read_text(encoding="utf-8"))["permissions"]["deny"]
    assert any(rule.startswith("Read(") and ".env" in rule for rule in denied)
    readers = {"cat", "sed", "grep", "head", "tail"}
    covered = {rule.split("(")[1].split()[0] for rule in denied if rule.startswith("Bash(")}
    assert readers <= covered, f"deny list misses secret readers: {sorted(readers - covered)}"

    gitignore = _read(".gitignore")
    assert "!.claude/settings.json" in gitignore, "settings.json is gitignored — the denials never travel"


def _rule_globs(rule: pathlib.Path) -> list[str]:
    """The quoted patterns inside a rule's `paths:` frontmatter block."""
    _, _, rest = rule.read_text(encoding="utf-8").partition("---\npaths:\n")
    block, _, _ = rest.partition("\n---")
    return re.findall(r'^\s+-\s+"([^"]+)"', block, flags=re.MULTILINE)


def _matches_anything(pattern: str) -> bool:
    # Before 3.13 pathlib's `**` matches directories only, so probe the file form too.
    candidates = [pattern, f"{pattern}/*"] if pattern.endswith("/**") else [pattern]
    return any(next(REPO_ROOT.glob(candidate), None) is not None for candidate in candidates)


def test_every_rule_glob_matches_something_in_the_tree() -> None:
    """A glob that matches nothing never fires, and no other check reports it.

    `test_every_path_named_in_agent_instructions_exists` only reads backticked paths out of the
    prose; a typo in the `paths:` frontmatter itself is invisible to it, and the only symptom is
    an owner list that silently stops loading.
    """
    dead: list[str] = []
    for rule in _rule_files():
        patterns = _rule_globs(rule)
        assert patterns, f"{rule.name} declares no paths: entries"
        dead.extend(f"{rule.name} -> {pattern}" for pattern in patterns if not _matches_anything(pattern))

    assert not dead, "rule globs match nothing, so the rule never loads: " + ", ".join(dead)


def _edit_map_rows() -> list[tuple[str, str]]:
    """`(glob, rule file)` pairs from the AGENTS.md §Edit Map routing table."""
    agents = _read("AGENTS.md")
    table = agents[agents.index("## Edit Map") : agents.index("## Scoped Refactoring")]
    return [
        (glob, rule)
        for glob, cell in re.findall(r"^\| `([^`]+)` \| (.+) \|$", table, flags=re.MULTILINE)
        for rule in re.findall(r"`(\.rules/[^`]+\.md)`", cell)
    ]


@parametrize_from_dict(
    {
        "bootstrap": {
            "path": "multiuser_config/bootstrap.py",
            "expected": {"supervisor-process", "agent-runtime-process", "background-memory"},
        },
        "core_skills": {"path": "new_core/skills/surface.py", "expected": {"new-core", "skills"}},
        "browser": {"path": "transport/browser/router.py", "expected": {"transport-seam", "http-contracts"}},
        "settings": {"path": "ouroboros/storage/settings/schema.py", "expected": {"settings"}},
        "headless_tests": {"path": "tests_headless/test_governance_docs.py", "expected": {"testing"}},
    }
)
def test_edit_map_preserves_starting_routes(path: str, expected: set[str]) -> None:
    """The shortcut table retains common starting routes; the resolver owns complete delivery."""
    routed = {pathlib.Path(rule).stem for glob, rule in _edit_map_rows() if fnmatch.fnmatchcase(path, glob)}
    assert routed == expected


def test_edit_map_routes_to_every_rule_file() -> None:
    """The owner lists are no longer in AGENTS.md, so the table is the only way an agent that
    does not auto-load rules can find them. A rule missing from it is invisible to every agent
    outside Claude Code, and a row pointing at a deleted rule sends them nowhere."""
    rows = _edit_map_rows()
    routed = {rule for _, rule in rows}
    on_disk = {f".rules/{rule.name}" for rule in _rule_files()}

    assert routed == on_disk, (
        f"Edit Map and .rules/ disagree: only in table {sorted(routed - on_disk)}, "
        f"only on disk {sorted(on_disk - routed)}"
    )
    assert len(rows) == len(set(rows)), "the Edit Map repeats the same path-to-rule route"


def test_edit_map_globs_match_the_rule_they_point_at() -> None:
    """A row that routes `supervisor/**` to a rule which does not claim that path is a lie the
    reader cannot detect without opening both files."""
    wrong: list[str] = []
    for glob, rule in _edit_map_rows():
        if not (REPO_ROOT / rule).exists():
            continue  # test_edit_map_routes_to_every_rule_file owns the missing-file case
        declared = _rule_globs(REPO_ROOT / rule)
        if not any(glob == pattern or ("*" not in glob and fnmatch.fnmatchcase(glob, pattern)) for pattern in declared):
            wrong.append(f"{glob} -> {rule} (declares {declared})")

    assert not wrong, "Edit Map rows name a path the rule does not declare: " + ", ".join(wrong)


# Packages over the size threshold that deliberately have no owner rule yet. Each entry is debt:
# add the rule or keep the reason current, but do not grow this list to make the test pass.
RULE_COVERAGE_DEBT = {
    "ouroboros": "top-level helpers (platform_layer, utils) have no single owner rule yet",
    "ouroboros/app": "composition root; .rules/settings.md covers only ouroboros/app/config.py",
    "ouroboros/storage": "chat_store and runtime_engine; the three storage subtrees have rules, the root does not",
    "ouroboros/storage/blob": "blob storage has no owner rule yet",
    "ouroboros/agent_events": "the agent event interfaces have no owner rule yet",
    "ouroboros/agent_runtime": "the extracted agent runtime has no owner rule yet",
}

_COVERAGE_THRESHOLD_BYTES = 10_000
_NOT_RUNTIME_CODE = {
    ".claude",
    ".git",
    ".rules",
    ".venv",
    "__pycache__",
    "docs",
    "openspec",
    "prompts",
    "scripts",
    "skills",
    "tests",
    "tests_headless",
}


def _substantial_packages() -> list[pathlib.Path]:
    """Directories holding at least the threshold of Python directly, subdirectories excluded."""
    found: list[pathlib.Path] = []
    for directory in sorted(REPO_ROOT.rglob("*")):
        if not directory.is_dir() or set(directory.relative_to(REPO_ROOT).parts) & _NOT_RUNTIME_CODE:
            continue
        if sum(f.stat().st_size for f in directory.glob("*.py")) >= _COVERAGE_THRESHOLD_BYTES:
            found.append(directory)
    return found


def _covered_by_a_rule(directory: pathlib.Path) -> bool:
    relative = directory.relative_to(REPO_ROOT)
    sources = [str(relative / source.name) for source in directory.glob("*.py")]
    patterns = [pattern for rule in _rule_files() for pattern in _rule_globs(rule)]
    # One pattern must claim the whole package. "A rule names one file in it" is the weaker
    # reading, and it is exactly how ouroboros/agent looked covered while having no rule.
    return any(all(fnmatch.fnmatch(source, pattern) for source in sources) for pattern in patterns)


def test_every_substantial_package_has_an_owner_rule() -> None:
    """A package with no rule is one an agent explores by grepping — which is the cost the whole
    routing layer exists to remove. Ratchet, not audit: new packages must choose a rule or an
    explicit debt entry."""
    uncovered = [
        str(directory.relative_to(REPO_ROOT))
        for directory in _substantial_packages()
        if not _covered_by_a_rule(directory)
    ]

    unrecorded = sorted(set(uncovered) - set(RULE_COVERAGE_DEBT))
    assert not unrecorded, (
        f"packages over {_COVERAGE_THRESHOLD_BYTES} bytes with no rule and no debt entry: {unrecorded}"
    )

    stale = sorted(set(RULE_COVERAGE_DEBT) - set(uncovered))
    assert not stale, f"RULE_COVERAGE_DEBT names packages that are now covered — delete them: {stale}"


# A rule longer than this is restating behavior rather than mapping ownership.
# One threshold for all rules: trim the rule, do not raise this to pass.
_RULE_MAX_LINES = 80


def test_rule_files_stay_bounded() -> None:
    """Rules are ownership maps, not behavioral docs. A rule that grows past this is restating
    behavior that belongs in ARCHITECTURE.md or an OpenSpec change. The threshold is generous
    and shared — a rule that needs more is restating, not mapping."""
    over: list[str] = []
    for rule in _rule_files():
        actual = len(rule.read_text(encoding="utf-8").splitlines())
        if actual > _RULE_MAX_LINES:
            over.append(f"{rule.name}: {actual} lines (max {_RULE_MAX_LINES})")

    assert not over, "rule files over max — trim to ownership + constraints, move behavior to docs:\n  " + "\n  ".join(
        over
    )


def test_agents_md_declares_monthly_rule_review_cadence() -> None:
    """Rules are reviewed on a monthly cadence, not expanded per-PR. Without this stated,
    developers treat rules as living docs and rewrite them on every change."""
    agents = _read("AGENTS.md")

    assert "monthly cadence" in agents, "AGENTS.md must declare the monthly rule review cadence"
    assert "ownership maps and editing constraints" in agents, (
        "AGENTS.md must state that rules are ownership maps, not behavioral docs"
    )
    assert "out of scope for a feature/fix PR" in agents, (
        "AGENTS.md must state that rule changes are out of scope for normal PRs"
    )


_ARCHITECTURE_MAX_LINES = 750


@parametrize_from_dict(
    {
        "router": {"path": "AGENTS.md", "budget": 14_000},
        "engineering": {"path": "docs/DEVELOPMENT.md", "budget": 10_000},
        "invariants": {"path": "docs/INVARIANTS.md", "budget": 6_000},
    }
)
def test_core_instruction_bytes_stay_bounded(path: str, budget: int) -> None:
    """A byte budget still catches growth hidden inside long Markdown lines."""
    assert len(_read(path).encode("utf-8")) <= budget, f"{path} exceeds {budget} bytes"


def test_architecture_stays_bounded() -> None:
    """ARCHITECTURE.md is an operational map, not a prose narrative. A file that grows past this
    is accumulating behavioral detail that belongs in openspec, .rules, or DEVELOPMENT.md."""
    architecture = _read("docs/ARCHITECTURE.md")
    actual = len(architecture.splitlines())
    assert actual <= _ARCHITECTURE_MAX_LINES, (
        f"ARCHITECTURE.md is {actual} lines (max {_ARCHITECTURE_MAX_LINES}) — "
        "trim to structure/contracts/data flow, move behavior to openspec/.rules/DEVELOPMENT.md"
    )
