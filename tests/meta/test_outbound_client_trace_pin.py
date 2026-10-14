"""No outbound client is built without the operation identifier being carried on it.

The per-integration tests each prove one client sends `X-Trace-Id`. None of them fails when a
*new* client appears — and a new outbound client with no header is exactly the regression this
capability keeps having: `with_trace_header` omits rather than raises, so the missing header is
invisible in review, in types, and in every other test.

So the pin is on the construction sites rather than on a count of instrumented integrations.
Adding an outbound client fails this test until the file is listed here with the mechanism that
carries the identifier on it, which is the moment to wire one.
"""

from __future__ import annotations

import ast
import pathlib

import pytest

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
_PRODUCTION_DIRS = ("ouroboros", "new_core", "supervisor", "transport", "background_memory")

#: Constructors that open a connection to something outside this process. Both HTTP stacks are
#: listed: `ouroboros/mcp/` is on `httpx2` because the MCP SDK is, the rest of the tree is on
#: `httpx`, and a scan that knew only one of them would let a new client on the other walk past.
_CLIENT_CALLS = {
    ("httpx", "Client"),
    ("httpx", "AsyncClient"),
    ("httpx2", "Client"),
    ("httpx2", "AsyncClient"),
    ("boto3", "client"),
    ("boto3", "resource"),
    # A library factory that returns one. It opens a connection to something outside this process
    # exactly as the constructors above do, and a scan that saw only the constructors would call a
    # file "no longer building a client" the moment it stopped assembling one by hand — retiring
    # the file's entry below while the client, and the chance to ship it untraced, both remain.
    ("mcp.shared._httpx_utils", "create_mcp_http_client"),
}

#: boto3 hands the same two constructors out on a session object, under a name this scan cannot
#: resolve back to a module (``session.client("s3")``). Matched by shape instead: the service is a
#: string literal in first position, which no unrelated ``.client(...)`` in this tree has.
_SESSION_CONSTRUCTORS = {"client", "resource"}

#: Every production file that builds one, and how the identifier reaches the wire from it.
#: A file belongs here only once a test proves the header is actually sent.
_COVERED = {
    # httpx event hook reading the context variable per request.
    "ouroboros/egress/a2a/transports.py": "request event hook (outbound_request_event_hooks)",
    # Per-call headers, built inside the caller's own scope.
    "ouroboros/llm/providers/gigachat_b2bank.py": "per-call with_trace_header",
    "ouroboros/llm/providers/openai_compat.py": "request event hook (_prepare_request)",
    "ouroboros/tools/gigasearch/gigasearch.py": "per-call with_trace_header",
    "ouroboros/tools/image_generate.py": "per-call with_trace_header",
    "ouroboros/tools/gigasearch/dlp.py": "per-call with_trace_header",
    "ouroboros/policy/guardrails.py": "per-call with_trace_header",
    "ouroboros/egress/workstation/client.py": "per-call with_trace_header (_headers)",
    # Sends from a connection-owned pump task, which runs each message under the context of the
    # task that queued it: the session's own requests carry the scope it minted, a tool call
    # carries its caller's. Read per request either way.
    "ouroboros/mcp/transports.py": "session scope + request event hook",
    # botocore's own event system; stamped after signing so it cannot break the signature.
    "ouroboros/storage/blob/s3_backend.py": "botocore before-send handler",
}


def _import_names(tree: ast.Module) -> tuple[dict[str, str], set[str]]:
    """What this file calls the modules and constructors it imported.

    Without this the scan only sees ``httpx.AsyncClient(...)``, and ``from httpx import
    AsyncClient`` — the spelling a new client is at least as likely to use — walks straight
    past a pin whose whole job is to stop it.
    """
    modules: dict[str, str] = {}
    constructors: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                modules[alias.asname or alias.name.split(".")[0]] = alias.name
        elif isinstance(node, ast.ImportFrom) and node.module:
            for alias in node.names:
                if (node.module, alias.name) in _CLIENT_CALLS:
                    constructors.add(alias.asname or alias.name)
    return modules, constructors


def _builds_outbound_client(node: ast.Call, modules: dict[str, str], constructors: set[str]) -> bool:
    func = node.func
    if isinstance(func, ast.Name):
        return func.id in constructors
    if not isinstance(func, ast.Attribute):
        return False
    if isinstance(func.value, ast.Name) and (modules.get(func.value.id, func.value.id), func.attr) in _CLIENT_CALLS:
        return True
    if func.attr in _SESSION_CONSTRUCTORS:
        first = node.args[0] if node.args else None
        return isinstance(first, ast.Constant) and isinstance(first.value, str)
    return False


def _uses_factory_alias(node: ast.AST, modules: dict[str, str], constructors: set[str]) -> bool:
    """Detect ``x or httpx.Client`` — the factory-alias pattern.

    The constructor is stored as a value, not called; the call scan alone sees the file
    as no longer building a client, which is exactly the regression this pin exists to catch.
    """
    if not isinstance(node, ast.BoolOp):
        return False
    for value in node.values:
        if isinstance(value, ast.Attribute) and isinstance(value.value, ast.Name):
            if (modules.get(value.value.id, value.value.id), value.attr) in _CLIENT_CALLS:
                return True
        if isinstance(value, ast.Name) and value.id in constructors:
            return True
    return False


def _client_constructions(source: str, filename: str = "<memory>") -> bool:
    tree = ast.parse(source, filename=filename)
    modules, constructors = _import_names(tree)
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and _builds_outbound_client(node, modules, constructors):
            return True
        if _uses_factory_alias(node, modules, constructors):
            return True
    return False


def _client_construction_files() -> set[str]:
    found: set[str] = set()
    for directory in _PRODUCTION_DIRS:
        for path in sorted((_REPO_ROOT / directory).rglob("*.py")):
            if "__pycache__" in path.parts or "migrations" in path.parts:
                continue
            if _client_constructions(path.read_text(encoding="utf-8"), str(path)):
                found.add(path.relative_to(_REPO_ROOT).as_posix())
    return found


def test_every_outbound_client_carries_the_operation_identifier():
    built = _client_construction_files()
    unlisted = sorted(built - set(_COVERED))

    assert not unlisted, (
        "a new outbound client was added without the trace identifier on it: "
        f"{unlisted}. Wire it — a per-request hook or per-call headers, never a value merged "
        "in at construction — prove it with a test, then list the file in _COVERED."
    )


def test_the_listed_files_still_build_a_client():
    """A stale entry would let an untraced client slip in under a name that is already listed."""
    built = _client_construction_files()
    stale = sorted(set(_COVERED) - built)

    assert not stale, f"_COVERED lists files that no longer build an outbound client: {stale}"


@pytest.mark.parametrize(
    "source",
    [
        pytest.param("import httpx\nc = httpx.AsyncClient()\n", id="module-attribute"),
        pytest.param("import httpx as hx\nc = hx.AsyncClient()\n", id="aliased-module"),
        pytest.param("from httpx import AsyncClient\nc = AsyncClient()\n", id="imported-name"),
        pytest.param("from httpx import Client as C\nc = C()\n", id="aliased-name"),
        pytest.param("import httpx2\nc = httpx2.AsyncClient()\n", id="httpx2-module-attribute"),
        pytest.param("from httpx2 import AsyncClient\nc = AsyncClient()\n", id="httpx2-imported-name"),
        pytest.param(
            "from mcp.shared._httpx_utils import create_mcp_http_client\nc = create_mcp_http_client()\n",
            id="sdk-client-factory",
        ),
        pytest.param("import boto3\nc = boto3.client('s3')\n", id="boto3-client"),
        pytest.param("import boto3\nc = boto3.resource('s3')\n", id="boto3-resource"),
        pytest.param("s = make_session()\nc = s.client('s3')\n", id="session-client"),
        pytest.param(
            "import httpx\nf = client_factory or httpx.Client\nc = f()\n",
            id="factory-alias-boolop",
        ),
        pytest.param(
            "from httpx import Client\nf = client_factory or Client\nc = f()\n",
            id="factory-alias-imported-name",
        ),
    ],
)
def test_the_scan_sees_every_spelling_a_new_client_is_likely_to_take(source):
    """A pin is only worth its green: each of these walked past the first version of this scan,
    which matched an attribute over a bare name and nothing else."""
    assert _client_constructions(source)


@pytest.mark.parametrize(
    "source",
    [
        pytest.param("class Client:\n    pass\nc = Client()\n", id="unrelated-local-class"),
        pytest.param("self.client(payload)\n", id="unrelated-client-method"),
        pytest.param("registry.resource(handle)\n", id="unrelated-resource-method"),
    ],
)
def test_the_scan_does_not_flag_unrelated_names(source):
    """The cost of a false positive is a real one: this test asserts an empty set, so a name that
    merely reads like a client would have to be listed as carrying an identifier it never sends."""
    assert not _client_constructions(source)
