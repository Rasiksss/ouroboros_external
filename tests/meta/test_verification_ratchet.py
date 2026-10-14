"""No green nothing: a suite may not go green by refusing to run.

`skip` and `xfail` are the cheapest way to turn a failing test into a passing run, so this
ratchet separates the legitimate use from the escape hatch:

* `@pytest.mark.skipif(condition, ...)` — allowed. Environment gating (no Postgres, no Windows,
  no Cyrillic font) is real, and the condition documents when the test does run.
* `pytest.skip(reason)` inside a test — allowed, and the reason is mandatory.
* `@pytest.mark.skip(...)` — forbidden. It is unconditional by construction; nothing records
  when the test should come back.
* `@pytest.mark.xfail(...)` — forbidden. A known-broken test that reports green is worse than
  a red one, because nobody looks at it again.

A marker is a marker wherever it is attached, so all three attachment points are scanned: the
decorator list, a module-level `pytestmark`, and `marks=` on a `pytest.param`. The last two are
live mechanisms here (`test_browser_tools_smoke.py`, `test_packaging_sync.py`), so checking only
decorators would leave the ratchet with two doors it never looks at. The runtime calls are
scanned the same way: `pytest.xfail(...)` is forbidden outright, and `pytest.skip(...)` must
carry an actual reason — `pytest.skip(allow_module_level=True)` is an argument, not a reason.

Baseline when this landed: 18 conditional skips, 0 unconditional, 0 xfail. The ratchet holds
that, it does not ask anyone to remove a legitimate environment gate.
"""

from __future__ import annotations

import ast
import pathlib

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
TEST_ROOTS = ("tests", "tests_headless", "skills")

#: Forbidden marker suffix → what to do instead.
_FORBIDDEN_MARKERS = {
    "mark.skip": "use skipif with a condition",
    "mark.xfail": "fix the test or delete it",
}


def _test_modules() -> list[pathlib.Path]:
    modules: list[pathlib.Path] = []
    for root in TEST_ROOTS:
        modules.extend(sorted((REPO_ROOT / root).rglob("test_*.py")))
    return modules


def _decorator_name(node: ast.expr) -> str:
    """Dotted name of a decorator, with any call arguments stripped."""
    target = node.func if isinstance(node, ast.Call) else node
    parts: list[str] = []
    while isinstance(target, ast.Attribute):
        parts.append(target.attr)
        target = target.value
    if isinstance(target, ast.Name):
        parts.append(target.id)
    return ".".join(reversed(parts))


def _forbidden_marker(node: ast.expr) -> tuple[str, str] | None:
    """``(name, remedy)`` when ``node`` is a forbidden marker expression, else ``None``.

    ``mark.skipif`` does not match ``mark.skip``: the suffixes differ, which is the whole
    distinction this ratchet draws.
    """
    name = _decorator_name(node)
    for suffix, remedy in _FORBIDDEN_MARKERS.items():
        if name.endswith(suffix):
            return name, remedy
    return None


def _marker_expressions(node: ast.expr) -> list[ast.expr]:
    """One marker, or the several in a list/tuple of them."""
    if isinstance(node, ast.List | ast.Tuple):
        return list(node.elts)
    return [node]


def _skip_states_a_reason(call: ast.Call) -> bool:
    """A positional message or ``reason=``. ``allow_module_level=`` is neither."""
    return bool(call.args) or any(keyword.arg == "reason" for keyword in call.keywords)


def _where(module: pathlib.Path | str, node: ast.AST) -> str:
    label = module if isinstance(module, str) else module.relative_to(REPO_ROOT)
    return f"{label}:{getattr(node, 'lineno', 0)}"


def _marker_offenders(source: str, label: pathlib.Path | str) -> list[str]:
    """Forbidden skip/xfail markers, wherever they are attached."""
    offenders: list[str] = []

    def report(marker: ast.expr, attached_to: str) -> None:
        found = _forbidden_marker(marker)
        if found is not None:
            name, remedy = found
            offenders.append(f"{_where(label, marker)} @{name} on {attached_to} — {remedy}")

    for node in ast.walk(ast.parse(source, filename=str(label))):
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            for decorator in node.decorator_list:
                report(decorator, node.name)
        elif isinstance(node, ast.Assign | ast.AnnAssign):
            # A module- or class-level `pytestmark` applies to every test in scope.
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            named_pytestmark = any(isinstance(t, ast.Name) and t.id == "pytestmark" for t in targets)
            if named_pytestmark and node.value is not None:
                for marker in _marker_expressions(node.value):
                    report(marker, "pytestmark")
        elif isinstance(node, ast.Call):
            # `pytest.param(..., marks=...)` attaches markers to one parametrize case.
            for keyword in node.keywords:
                if keyword.arg != "marks":
                    continue
                for marker in _marker_expressions(keyword.value):
                    report(marker, "a parametrize case")

    return offenders


def _runtime_offenders(source: str, label: pathlib.Path | str) -> list[str]:
    """Runtime `pytest.xfail(...)`, and `pytest.skip(...)` that states no reason."""
    offenders: list[str] = []

    for node in ast.walk(ast.parse(source, filename=str(label))):
        if not isinstance(node, ast.Call):
            continue
        name = _decorator_name(node)
        if name.endswith("pytest.xfail"):
            offenders.append(f"{_where(label, node)} pytest.xfail() — fix the test or delete it")
        elif name.endswith("pytest.skip") and not _skip_states_a_reason(node):
            offenders.append(f"{_where(label, node)} pytest.skip() with no reason")

    return offenders


def test_no_unconditional_skip_or_xfail_markers() -> None:
    offenders: list[str] = []
    for module in _test_modules():
        offenders.extend(_marker_offenders(module.read_text(encoding="utf-8"), module))

    assert not offenders, "tests are passing by not running:\n  " + "\n  ".join(offenders)


def test_every_runtime_skip_states_why() -> None:
    """A skip with no reason is indistinguishable from a skip that hides a bug."""
    offenders: list[str] = []
    for module in _test_modules():
        offenders.extend(_runtime_offenders(module.read_text(encoding="utf-8"), module))

    assert not offenders, "skips without a stated reason:\n  " + "\n  ".join(offenders)


# --- the ratchet's own detection, on synthetic sources ----------------------
#
# A ratchet that silently stops detecting is worse than no ratchet, and the repo is clean, so
# nothing here would fail if the scan quietly went blind. These feed it the bypasses instead.


def test_a_decorator_marker_is_caught() -> None:
    assert _marker_offenders("@pytest.mark.skip\ndef test_x(): ...\n", "x.py")
    assert _marker_offenders('@pytest.mark.xfail(reason="broken")\ndef test_x(): ...\n', "x.py")


def test_a_module_level_pytestmark_is_caught() -> None:
    """`pytestmark = pytest.mark.skip(...)` skips every test in the module."""
    assert _marker_offenders('pytestmark = pytest.mark.skip("later")\ndef test_x(): ...\n', "x.py")
    assert _marker_offenders("pytestmark = [pytest.mark.slow, pytest.mark.xfail]\n", "x.py")


def test_a_parametrize_case_marker_is_caught() -> None:
    source = '@pytest.mark.parametrize("n", [pytest.param(1, marks=pytest.mark.xfail)])\ndef test_x(n): ...\n'
    assert _marker_offenders(source, "x.py")


def test_conditional_gating_is_left_alone() -> None:
    """skipif is the legitimate use; the ratchet must not chase it away."""
    assert not _marker_offenders('@pytest.mark.skipif(NO_PG, reason="no postgres")\ndef test_x(): ...\n', "x.py")
    assert not _marker_offenders("pytestmark = pytest.mark.browser\n", "x.py")
    source = '@pytest.mark.parametrize("n", [pytest.param(1, marks=pytest.mark.skipif(X, reason="r"))])\ndef test_x(n): ...\n'
    assert not _marker_offenders(source, "x.py")


def test_a_runtime_skip_without_a_reason_is_caught() -> None:
    assert _runtime_offenders("def test_x():\n    pytest.skip()\n", "x.py")
    # An argument is not a reason: this one skips the whole module and says nothing.
    assert _runtime_offenders("pytest.skip(allow_module_level=True)\n", "x.py")


def test_a_runtime_xfail_is_caught() -> None:
    assert _runtime_offenders('def test_x():\n    pytest.xfail("known broken")\n', "x.py")


def test_a_runtime_skip_with_a_reason_is_left_alone() -> None:
    assert not _runtime_offenders('def test_x():\n    pytest.skip("no display available")\n', "x.py")
    assert not _runtime_offenders(
        'def test_x():\n    pytest.skip(reason="no display", allow_module_level=True)\n', "x.py"
    )
