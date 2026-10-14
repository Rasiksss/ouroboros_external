"""OwnerScope: fail-closed construction, closed surface dictionary, strict round-trip."""

from __future__ import annotations

import ast
import pathlib

import pytest
from pydantic import ValidationError

from new_core.identity import OwnerScope, ServiceJob, ServiceScope, SurfaceId
from ouroboros.contracts.owner_scope import configured_agent_id, configured_owner_id, service_identity

_AXES = ("owner_id", "agent_id", "surface_id", "chat_id")


def _axes(**overrides: str) -> dict[str, str]:
    return {"owner_id": "global", "agent_id": "main", "surface_id": "dialogs", "chat_id": "7", **overrides}


def test_the_value_has_exactly_the_four_identity_axes() -> None:
    assert tuple(OwnerScope.model_fields) == _AXES


def test_every_axis_must_be_non_empty() -> None:
    for axis in _AXES:
        with pytest.raises(ValueError):
            OwnerScope(**_axes(**{axis: ""}))


def test_every_axis_is_required() -> None:
    """No axis may default: a scope that filled one in itself would key rows to a guess."""
    for axis in _AXES:
        incomplete = _axes()
        incomplete.pop(axis)
        with pytest.raises(ValidationError, match=axis):
            OwnerScope(**incomplete)


def test_unknown_surface_is_rejected() -> None:
    with pytest.raises(ValueError):
        OwnerScope(**_axes(surface_id="telepathy"))


def test_extra_field_is_rejected() -> None:
    """``trace_id`` and ``conversation_id`` are the two a caller actually reaches for."""
    with pytest.raises(ValidationError):
        OwnerScope(**_axes(), trace_id="t-1")  # type: ignore[call-arg]
    with pytest.raises(ValidationError):
        OwnerScope.from_dict({**_axes(), "conversation_id": "x"})


def test_scope_is_frozen() -> None:
    scope = OwnerScope(**_axes())
    with pytest.raises(Exception):
        scope.chat_id = "8"  # type: ignore[misc]


def test_round_trip_through_storage() -> None:
    scope = OwnerScope(owner_id="global", agent_id="main", surface_id="a2a", chat_id="-4100")

    assert OwnerScope.from_dict(scope.to_dict()) == scope
    assert set(scope.to_dict()) == set(_AXES)


def test_from_dict_refuses_a_payload_missing_any_axis() -> None:
    for axis in _AXES:
        incomplete = _axes()
        incomplete.pop(axis)
        with pytest.raises(ValueError, match=axis):
            OwnerScope.from_dict(incomplete)


def test_from_dict_refuses_a_malformed_payload() -> None:
    with pytest.raises(ValueError):
        OwnerScope.from_dict(_axes(surface_id="nope"))
    with pytest.raises(ValueError):
        OwnerScope.from_dict("dialogs:7")


@pytest.mark.parametrize(
    ("legacy", "closed"),
    [("sberchat", "dialogs"), ("web", "browser"), ("internal", "skill-bridge"), ("unknown", "skill-bridge")],
)
def test_legacy_spellings_normalize_on_read(legacy: str, closed: str) -> None:
    scope = OwnerScope.from_dict(_axes(surface_id=legacy))
    assert scope.surface_id == closed


def test_no_code_path_produces_a_legacy_spelling() -> None:
    assert not {surface.value for surface in SurfaceId} & {"sberchat", "web", "internal", "unknown"}


def test_a_surface_is_a_closed_type_not_a_validated_string() -> None:
    """The point of the enum: an unknown surface cannot be constructed at all."""
    assert OwnerScope(**_axes()).surface_id is SurfaceId.DIALOGS
    # A str-mixin member renders as its value everywhere, including str() on 3.10.
    assert str(SurfaceId.DIALOGS) == "dialogs"
    assert SurfaceId.coerce("nonsense") is None


def test_an_axis_is_stripped_rather_than_letting_padding_split_a_scope() -> None:
    """Two scopes that differ only by whitespace would key two sets of rows."""
    assert OwnerScope(**_axes(chat_id="  7  ")).chat_id == "7"


def test_the_security_token_is_a_fixed_digest_of_owner_agent_and_surface() -> None:
    assert OwnerScope(**_axes()).security_token == "a7c56545978856ec8881b02f7fc65c11"


def test_the_security_token_ignores_the_chat() -> None:
    assert OwnerScope(**_axes(chat_id="7")).security_token == OwnerScope(**_axes(chat_id="8")).security_token


@pytest.mark.parametrize(
    ("axis", "other"),
    [("owner_id", "another-owner"), ("agent_id", "another-agent"), ("surface_id", "browser")],
)
def test_the_security_token_differs_by_owner_agent_and_surface(axis: str, other: str) -> None:
    assert OwnerScope(**_axes(**{axis: other})).security_token != OwnerScope(**_axes()).security_token


@pytest.mark.parametrize("separator", [":", '","', "\x00"])
def test_axis_values_containing_a_separator_do_not_collide_in_the_security_token(separator: str) -> None:
    left = OwnerScope(**_axes(owner_id=f"x{separator}y", agent_id="z"))
    right = OwnerScope(**_axes(owner_id="x", agent_id=f"y{separator}z"))

    assert left.security_token != right.security_token


@pytest.mark.parametrize(
    ("stored", "normalized"),
    [({"surface_id": "web"}, {"surface_id": "browser"}), ({"owner_id": "  global  "}, {"owner_id": "global"})],
)
def test_the_security_token_is_computed_from_the_normalized_axes(
    stored: dict[str, str], normalized: dict[str, str]
) -> None:
    assert OwnerScope.from_dict(_axes(**stored)).security_token == OwnerScope(**_axes(**normalized)).security_token


def test_the_security_token_is_never_accepted_as_input() -> None:
    token = OwnerScope(**_axes()).security_token

    with pytest.raises(ValidationError, match="security_token"):
        OwnerScope(**_axes(security_token=token))
    with pytest.raises(ValidationError, match="security_token"):
        OwnerScope.from_dict(_axes(security_token=token))


def test_the_security_token_is_not_part_of_the_value() -> None:
    scope = OwnerScope(**_axes())

    assert "security_token" not in OwnerScope.model_fields
    assert "security_token" not in scope.to_dict()
    assert "security_token" not in repr(scope)
    assert scope.security_token not in repr(scope)
    assert "security_token" not in str(scope)
    assert scope.security_token not in str(scope)


def test_a_transport_builds_its_scope_from_configuration_and_its_own_name() -> None:
    """A real ingress builder, not a test stand-in: the bridge's own ``_synthetic_scope_for``.

    There is no shared resolver any more — each transport constructs its scope itself — so
    what has to hold is that the two axes a sender must never name come from this
    instance's configuration and nowhere else.
    """
    from transport.skill_bridge import _synthetic_scope_for

    scope = _synthetic_scope_for("dialogs", 555)

    assert scope == OwnerScope(
        owner_id=configured_owner_id(),
        agent_id=configured_agent_id(),
        surface_id="dialogs",
        chat_id="555",
    )
    # The same instance the author-less identity is built from, whatever the values are.
    instance = service_identity(job=ServiceJob.STARTUP)
    assert (scope.owner_id, scope.agent_id) == (instance.owner_id, instance.agent_id)


def test_transport_names_equal_the_surface_dictionary() -> None:
    from transport.a2a.delivery import TRANSPORT_NAME as A2A_NAME
    from transport.browser.ws import TRANSPORT_NAME as BROWSER_NAME
    from transport.dialogs.transport import TRANSPORT_NAME as DIALOGS_NAME
    from transport.skill_bridge import TRANSPORT_NAME as SKILL_BRIDGE_NAME

    assert {A2A_NAME, BROWSER_NAME, DIALOGS_NAME, SKILL_BRIDGE_NAME} == set(SurfaceId)


def _call_sites(needle: str) -> set[str]:
    """Every ``file:line`` under the production roots whose line contains ``needle``.

    A source scan rather than a type check, because what is being enforced is where a
    construction may appear, not what it returns — and because ``ctx.send_with_budget``
    is typed ``Callable[..., Any]``, so mypy sees none of the seams this guards.
    """
    import pathlib

    repo_root = pathlib.Path(__file__).resolve().parents[2]
    roots = [
        *(repo_root / root for root in ("ouroboros", "supervisor", "transport", "background_memory", "new_core")),
        repo_root / "server.py",
    ]
    sites = set()
    for root in roots:
        paths = [root] if root.is_file() else sorted(root.rglob("*.py"))
        for path in paths:
            # The modules that define the mints are not call sites of them.
            if "__pycache__" in path.parts or path.name in {"owner_scope.py", "service_scope.py"}:
                continue
            for line_no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
                if needle in line:
                    sites.add(f"{path.relative_to(repo_root)}:{line_no}")
    return sites


def _owner_scope_constructions() -> dict[str, ast.Call]:
    """Every direct ``OwnerScope(...)`` call under the production roots, by ``file:line``.

    Parsed rather than grepped, so the keyword arguments can be inspected: what matters is
    not only *where* a scope is built but *what each axis is built from*. The module that
    defines the type and the one that widens a service identity are excluded — they are the
    definitions, not construction sites.
    """
    import pathlib

    repo_root = pathlib.Path(__file__).resolve().parents[2]
    roots = [
        *(repo_root / root for root in ("ouroboros", "supervisor", "transport", "background_memory", "new_core")),
        repo_root / "server.py",
    ]
    found: dict[str, ast.Call] = {}
    for root in roots:
        paths = [root] if root.is_file() else sorted(root.rglob("*.py"))
        for path in paths:
            if "__pycache__" in path.parts or path.name in {"owner_scope.py", "service_scope.py"}:
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "OwnerScope":
                    found[f"{path.relative_to(repo_root)}:{node.lineno}"] = node
    return found


def _root_name(value: ast.expr | None) -> str:
    """The local an axis expression stands on: ``agent`` for ``agent.agent_id``."""
    node = value
    while isinstance(node, ast.Attribute):
        node = node.value
    return node.id if isinstance(node, ast.Name) else ""


def _binding_of(site: str, name: str) -> str:
    """The single expression ``name`` is assigned in the function holding ``site``.

    ``""`` when the name is bound more than once or not at all: a local that is rebound is
    not a local whose provenance a reader can state, so it fails rather than passing on the
    first assignment found.
    """
    import pathlib

    path_text, _, line_text = site.rpartition(":")
    repo_root = pathlib.Path(__file__).resolve().parents[2]
    tree = ast.parse((repo_root / path_text).read_text(encoding="utf-8"))
    holders = [
        node
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.lineno <= int(line_text) <= (node.end_lineno or node.lineno)
    ]
    if not holders:
        return ""
    innermost = max(holders, key=lambda node: node.lineno)
    bindings = [
        node.value
        for node in ast.walk(innermost)
        if isinstance(node, ast.Assign)
        and len(node.targets) == 1
        and isinstance(node.targets[0], ast.Name)
        and node.targets[0].id == name
    ]
    return ast.unparse(bindings[0]) if len(bindings) == 1 else ""


def test_no_construction_names_an_owner_or_an_agent_of_its_own() -> None:
    """The property the deleted resolver used to hold, kept without it.

    A transport handles attacker-controlled data, so the two axes a sender must never be
    able to name may come from exactly three places: this instance's configuration, the
    stored row being read back — which was itself written under a scope built this way —
    or, for the owner only, the named resolver that reads a header a deployment has
    declared trusted. Anything else, including a value lifted out of a message, fails here.

    The third and fourth are real widenings of a security-relevant invariant, so each is
    declared by the expression it is bound from rather than merely by shape: assigning a
    resolver's result to a local and passing the variable would otherwise slip past a shape
    check without ever being stated. Every local that reaches an axis is therefore resolved
    back to its binding in the same function, and that binding is compared against this
    list. The attribute branch is narrowed to the one stored-row reader shape, so an
    arbitrary ``something.owner_id`` no longer passes for a row's own column.
    """
    allowed_calls = {
        "configured_owner_id": "owner_id",
        "configured_agent_id": "agent_id",
        "trusted_owner_id": "owner_id",
    }
    #: What a local named in an axis must have been bound from, wherever one is used.
    #: Both resolvers need two: the owner is bound before the agent because the agent is
    #: looked up *within* it — the configured owner for the constant-owner resolver, the
    #: header for the trusted one — and the lookup is awaited, so neither fits inside
    #: the call. Each key is the axis expression; each value is the axis it fills and
    #: the binding texts its root local may have been assigned.
    allowed_bindings = {
        "owner_id": ("owner_id", {"trusted_owner_id(headers)", "configured_owner_id()"}),
        "agent.agent_id": (
            "agent_id",
            {"await _resolve_scoped_agent(self._agent_registry, owner_id, selected_agent_id(headers))"},
        ),
    }
    offenders = []
    for site, call in _owner_scope_constructions().items():
        axes = {kw.arg: kw.value for kw in call.keywords}
        for axis in ("owner_id", "agent_id"):
            value = axes.get(axis)
            # The explicit administrative backfill command takes the ownership pair from its operator.
            if site.startswith("ouroboros/app/cli.py:") and value and ast.unparse(value) == f"args.{axis}":
                continue
            if isinstance(value, ast.Call) and isinstance(value.func, ast.Name):
                if allowed_calls.get(value.func.id) == axis:
                    continue
            # ``record.owner_id`` / ``record.agent_id``: the row's own ownership columns,
            # off the row itself and off nothing else.
            if (
                isinstance(value, ast.Attribute)
                and value.attr == axis
                and isinstance(value.value, ast.Name)
                and value.value.id == "record"
            ):
                continue
            declared = allowed_bindings.get(ast.unparse(value) if value else "")
            if declared and declared[0] == axis and _binding_of(site, _root_name(value)) in declared[1]:
                continue
            offenders.append(f"{site}: {axis}={ast.unparse(value) if value else '(missing)'}")

    assert offenders == []


def test_no_construction_passes_a_security_token() -> None:
    constructions = _owner_scope_constructions()

    offenders = [
        site
        for site, call in constructions.items()
        if any(keyword.arg == "security_token" for keyword in call.keywords)
    ]

    assert constructions
    assert offenders == []


def test_a_trusted_header_supplies_the_owner_and_the_chat_directly() -> None:
    """The carve-out is one surface's, and it fills two axes without a lookup.

    Two properties at once: exactly one construction in the system reads a trusted header,
    and the two axes it fills from one are the owner and the chat. The agent is not among
    them — see the test below for what it is filled from instead.
    """
    from_header = {
        site: call
        for site, call in _owner_scope_constructions().items()
        if any(
            f"trusted_{axis}" in ast.unparse(kw.value) or f"trusted_{axis}" in _binding_of(site, axis)
            for kw in call.keywords
            for axis in ("owner_id", "chat_id")
        )
    }

    site, call = next(iter(from_header.items()))
    axes = {kw.arg: ast.unparse(kw.value) for kw in call.keywords}
    # Both headers are read into locals before the registry is asked, so a bad chat
    # header is refused without a registry write.
    # The axis is the string the boundary admitted, not a re-wrapping of it: the chat is
    # opaque and the value that reaches storage must be the value that was compared.
    assert axes["chat_id"] == "chat_id"
    assert _binding_of(site, "chat_id") == "trusted_chat_id(headers)"
    assert _binding_of(site, "owner_id") == "trusted_owner_id(headers)"


def test_no_header_reaches_the_agent_axis_without_a_scoped_lookup() -> None:
    """A request names an agent; the axis is still filled from the record, never the header.

    This is what keeps the selection safe: the header is only ever an *argument* to a lookup
    whose predicate also names the settled owner, so a value that resolves to nothing inside
    that owner is refused rather than carried. See
    `openspec/specs/trusted-identity-headers/spec.md`.
    """
    from transport.browser import identity as identity_module

    source = pathlib.Path(identity_module.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)

    for name in ("TrustedHeaderIdentity", "ConfiguredOwnerIdentity"):
        resolver = next(node for node in ast.walk(tree) if isinstance(node, ast.ClassDef) and node.name == name)
        constructions = [
            node
            for node in ast.walk(resolver)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "OwnerScope"
        ]
        assert len(constructions) == 1, name
        axes = {kw.arg: ast.unparse(kw.value) for kw in constructions[0].keywords}
        # Off the record the lookup returned, and off nothing else.
        assert axes["agent_id"] == "agent.agent_id", name

    # The one place the header value is consumed, and it is consumed as an argument.
    reads = [
        ast.unparse(node)
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "selected_agent_id"
    ]
    assert reads == ["selected_agent_id(headers)"] * 2
    assert "_resolve_scoped_agent(self._agent_registry, owner_id, selected_agent_id(headers))" in source


def test_a_service_identity_is_not_an_owner_scope() -> None:
    """Not a subclass either: subclassing would put one everywhere a scope is accepted."""
    service = service_identity(job=ServiceJob.STARTUP)

    assert not isinstance(service, OwnerScope)
    assert not issubclass(ServiceScope, OwnerScope)
    with pytest.raises(ValidationError):
        OwnerScope.from_dict(service.model_dump(mode="json"))


def test_a_service_identity_names_a_job_and_no_conversation() -> None:
    service = service_identity(job=ServiceJob.JOURNAL_SWEEP)

    assert str(service.job) == "journal-sweep"
    assert set(ServiceScope.model_fields) == {"owner_id", "agent_id", "job"}
    assert {job.value for job in ServiceJob} == {
        "startup",
        "journal-sweep",
        "agent-card",
        "a2a-task-store",
        "context-cleanup",
        "dialogs-cursor",
        "owner-state",
        "runtime-log",
        "skill-state",
    }


def test_widening_a_service_identity_is_explicit_and_one_way() -> None:
    """``as_scope()`` is how author-less work addresses a store that requires a scope.

    It keeps the instance's ownership pair — the only two axes any of those statements
    filter on — and names the job where a conversation would go, because there is none.
    The widening runs one way: the result is an ``OwnerScope``, and no ``OwnerScope``
    turns back into a ``ServiceScope``.
    """
    service = ServiceScope(owner_id="global", agent_id="main", job=ServiceJob.JOURNAL_SWEEP)

    widened = service.as_scope()

    assert isinstance(widened, OwnerScope)
    assert (widened.owner_id, widened.agent_id) == ("global", "main")
    assert widened.surface_id is SurfaceId.SKILL_BRIDGE
    assert widened.chat_id == "journal-sweep"
    assert not hasattr(widened, "job")


def test_every_job_widens_to_a_scope_of_its_own() -> None:
    """The sentinel chat is the job, so two jobs never collide into one address."""
    scopes = [service_identity(job=job).as_scope() for job in ServiceJob]

    assert len({scope.chat_id for scope in scopes}) == len(list(ServiceJob))


def test_a_service_identity_cannot_announce_a_persisted_message() -> None:
    """The hazard the split closes: a system write must not start owner-scoped work.

    ``ConsolidationHandler`` is keyed on the ownership pair, so an announcement under a
    service identity would run consolidation over the real owner's backlog.
    """
    import typing

    from background_memory.infra.events import announce_chat_message_persisted
    from background_memory.interfaces import ChatMessagePersistedEvent

    # A compile-time distinction, deliberately: the announcement asks for an ``OwnerScope``
    # and a ``ServiceScope`` is not one. ``as_scope()` does not undo that — a widening is a
    # decision spelled out at a call site, and no author-less writer takes it on this path.
    assert typing.get_type_hints(announce_chat_message_persisted)["scope"] is OwnerScope
    assert typing.get_type_hints(ChatMessagePersistedEvent)["scope"] is OwnerScope
    assert not isinstance(service_identity(job=ServiceJob.STARTUP), OwnerScope)
