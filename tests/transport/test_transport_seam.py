"""Properties of the seam itself, independent of any implementation.

The behavioural contract lives in ``tests/helpers/transport_conformance.py`` and
runs against each implementation; what is checked here is the shape of the
interfaces, which no implementation can fix afterwards.
"""

from __future__ import annotations

import ast
import dataclasses
import inspect
import pathlib
import re
import typing

import pytest

from tests.helpers.scope import scope_for
from tests.helpers.shared import scope_with
from transport.interfaces import (
    AttachmentRef,
    DeliveredMessage,
    IInbox,
    IMessageGateway,
    InboundEnvelope,
    IOutbox,
    IOutboxReader,
    OutboundMessage,
    SubscriptionMode,
)

_SELECTOR_NAMES = {"chat_id", "source", "kind", "is_progress", "final_only", "filter", "predicate", "selector"}


def test_the_outbox_interfaces_offer_no_filters():
    """A reader receives everything; a skipped, unacked message would stall the rest."""
    for interface in (IOutbox, IOutboxReader):
        for name, method in inspect.getmembers(interface, inspect.isfunction):
            if name.startswith("__"):
                continue
            parameters = set(inspect.signature(method).parameters) - {"self"}
            assert not (parameters & _SELECTOR_NAMES), f"{interface.__name__}.{name} narrows the stream"


def test_a_reader_is_opened_with_a_name_and_a_mode():
    parameters = inspect.signature(IOutbox.reader).parameters
    assert set(parameters) == {"self", "name", "mode"}
    assert parameters["name"].kind is inspect.Parameter.KEYWORD_ONLY
    assert parameters["mode"].kind is inspect.Parameter.KEYWORD_ONLY
    assert {mode.value for mode in SubscriptionMode} == {"shared", "broadcast"}


def test_the_inbox_declares_two_methods_and_no_settlement_member():
    """Settling an inbound message is `poll`'s second job, not a third method.

    A receipt would name something that was never ambiguous — at most one
    envelope per inbox is unsettled — while adding a map to every backend and a
    second way for a consumer to be wrong.
    """
    methods = {name for name, _ in inspect.getmembers(IInbox, inspect.isfunction) if not name.startswith("__")}

    assert methods == {"submit", "poll"}
    for name in methods:
        parameters = set(inspect.signature(getattr(IInbox, name)).parameters)
        assert not (parameters & {"receipt", "handle", "delivered", "token"})


def test_the_reader_gains_no_liveness_call() -> None:
    """Releasing a subscription whose process did not return is the backend's job.

    A `heartbeat` would be the instance identity by another name: every transport
    would grow a timer whose period has to match a backend policy it cannot see.
    The backend observes the calls a live reader already makes instead. The other
    half — no lease or instance argument on `reader` — is pinned by
    `test_a_reader_is_opened_with_a_name_and_a_mode` above, which asserts its
    parameters exactly.
    """
    methods = {name for name, _ in inspect.getmembers(IOutboxReader, inspect.isfunction) if not name.startswith("__")}

    assert methods == {"poll", "ack", "nack", "defer", "close"}


def test_submission_answers_nothing_about_what_the_message_became():
    """Intent, task id and slash command are decided elsewhere — possibly on another machine."""
    import transport.interfaces as seam

    assert typing.get_type_hints(IInbox.submit)["return"] is type(None)
    assert not hasattr(seam, "SubmissionResult")


def test_the_seam_is_synchronous() -> None:
    seam_methods = [
        IInbox.submit,
        IInbox.poll,
        IOutbox.publish,
        IOutbox.reader,
        IOutboxReader.poll,
        IOutboxReader.ack,
        IOutboxReader.nack,
        IOutboxReader.defer,
        IOutboxReader.close,
    ]
    for method in seam_methods:
        assert not inspect.iscoroutinefunction(method), f"{method.__qualname__} must be synchronous"


def test_poll_takes_a_timeout_so_a_reader_loop_needs_no_sleep():
    timeout = inspect.signature(IOutboxReader.poll).parameters["timeout"]
    assert timeout.default == 0.0


def test_the_gateway_exposes_the_two_directions_separately():
    assert isinstance(inspect.getattr_static(IMessageGateway, "inbox"), property)
    assert isinstance(inspect.getattr_static(IMessageGateway, "outbox"), property)


def test_neither_message_type_can_settle_itself():
    """Settling belongs to the reader: it, not the payload, holds the position."""
    published = OutboundMessage(scope=scope_for("browser", 1), source="browser", text="hi")
    delivered = DeliveredMessage(published, "receipt-1")

    for message in (published, delivered):
        assert not hasattr(message, "ack")
        assert not hasattr(message, "nack")
        assert not any(callable(getattr(message, field.name)) for field in dataclasses.fields(message))


def test_the_delivered_message_carries_a_receipt_without_a_default() -> None:
    fields = {field.name: field for field in dataclasses.fields(DeliveredMessage)}

    assert set(fields) == {"message", "receipt", "attempts"}
    assert fields["receipt"].default is dataclasses.MISSING
    assert fields["attempts"].default == 0


def test_publishing_without_a_source_fails_visibly():
    with pytest.raises(ValueError, match="explicit source"):
        OutboundMessage(scope=scope_for("browser", 1), source="")


def test_the_published_payload_names_the_conversation_once():
    """A pin: two fields carrying one fact will eventually name two different chats.

    The chat a reader routes on is derived from the scope, so a later caller cannot
    reintroduce the second copy without this failing.
    """
    field_names = {field.name for field in dataclasses.fields(OutboundMessage)}

    assert "chat_id" not in field_names
    assert "scope" in field_names
    assert isinstance(inspect.getattr_static(OutboundMessage, "chat_id"), property)


def test_the_scope_survives_to_the_reader_with_its_source_unchanged():
    """Identity crosses the seam in both directions; the source is a separate key."""
    scope = scope_for("browser", 7)

    published = OutboundMessage(scope=scope, source="dialogs", text="hi")

    assert published.scope == scope
    # The surface the scope names and the transport that carries it may legitimately
    # differ, so one must not be derived from the other.
    assert published.source == "dialogs"
    assert published.chat_id == 7


def test_two_owners_holding_one_chat_id_stay_two_conversations():
    """Chat ids are unique within an owner, not across owners."""
    mine = OutboundMessage(scope=scope_with(scope_for("browser", 7), owner_id="alice"), source="browser", text="hi")
    theirs = OutboundMessage(scope=scope_with(scope_for("browser", 7), owner_id="bob"), source="browser", text="hi")

    assert mine.chat_id == theirs.chat_id == 7
    assert mine.scope.owner_id != theirs.scope.owner_id
    assert mine != theirs


def test_media_travels_by_reference_not_inline():
    """A photo must not inflate the queued message with its own bytes."""
    field_names = {field.name for field in dataclasses.fields(OutboundMessage)}

    assert "image_base64" not in field_names
    assert "attachment" in field_names
    assert {field.name for field in dataclasses.fields(AttachmentRef)} == {
        "attachment_id",
        "filename",
        "mime",
        "size",
    }


def test_an_envelope_cannot_be_built_without_a_scope():
    with pytest.raises(TypeError):
        InboundEnvelope(user_id=1, text="hello")  # type: ignore[call-arg]


def test_the_envelope_exposes_the_scopes_address_not_its_own():
    """Two identity fields beside the scope could disagree with it; there is only one."""
    scope = scope_for("dialogs", 42)
    envelope = InboundEnvelope(scope=scope, user_id=42, text="hello")

    assert (envelope.chat_id, envelope.source) == (scope.chat_id, scope.surface_id)
    field_names = {field.name for field in dataclasses.fields(InboundEnvelope)}
    assert "chat_id" not in field_names and "source" not in field_names


def test_one_inbound_envelope_carries_plain_types_only():
    envelope = InboundEnvelope(scope=scope_for("browser", 1), user_id=1, text="hello")

    assert dataclasses.is_dataclass(envelope)
    assert envelope.detached is False
    assert InboundEnvelope(scope=scope_for("browser", 1), detached=True).detached is True
    assert envelope.effective_text() == "hello"
    assert (
        InboundEnvelope(scope=scope_for("skill-bridge", 1), image_base64="AAA").effective_text() == "(image attached)"
    )
    assert (
        InboundEnvelope(scope=scope_for("skill-bridge", 1), attachment_ids=["a"]).effective_text() == "(file attached)"
    )


def test_exactly_one_inbound_envelope_is_defined_in_the_repository():
    """Two types with one name, bridged by an adapter, is the shim that outlives the migration."""
    repo_root = pathlib.Path(__file__).resolve().parents[2]
    searched = ("transport", "supervisor", "ouroboros", "new_core", "background_memory")
    definitions = []
    for package in searched:
        for path in (repo_root / package).rglob("*.py"):
            for line in path.read_text(encoding="utf-8").splitlines():
                if re.match(r"class InboundEnvelope\b", line):
                    definitions.append(str(path.relative_to(repo_root)))

    assert definitions == ["transport/interfaces.py"], definitions


#: What lives above the seam. A transport that names any of these stops being
#: replaceable by a broker-backed one, so the rule holds for a lazy import inside
#: a function exactly as it does for a module-level one.
_ABOVE_THE_SEAM = ("supervisor", "new_core", "ouroboros.agent", "ouroboros.app")

#: Narrow engine contracts the seam carries. Importing them pulls in no application wiring.
_SEAM_SHARED = ("new_core.identity", "new_core.skills.models", "new_core.skills.permissions")


def _imports_above_the_seam(source: str, where: str) -> set[str]:
    """Every forbidden import in ``source``, parsed rather than grepped.

    A regex over lines missed both live violations: one was indented inside a
    function, the other named a dotted submodule.
    """
    offenders = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            names = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom) and not node.level:
            names = [node.module or ""]
        else:
            continue
        for name in names:
            if any(name == root or name.startswith(f"{root}.") for root in _SEAM_SHARED):
                continue
            if any(name == root or name.startswith(f"{root}.") for root in _ABOVE_THE_SEAM):
                offenders.add(f"{where}: {name}")
    return offenders


def test_the_transport_package_imports_nothing_above_the_seam():
    import transport

    repo_root = pathlib.Path(__file__).resolve().parents[2]
    package_root = pathlib.Path(transport.__file__).parent
    offenders: set[str] = set()
    for path in sorted(package_root.rglob("*.py")):
        relative = path.relative_to(repo_root).as_posix()
        offenders |= _imports_above_the_seam(path.read_text(encoding="utf-8"), relative)

    assert offenders == set()


def test_the_boundary_guard_fires_on_the_imports_it_exists_for():
    """Both live violations were a lazy import and a dotted submodule."""
    violating = (
        "import new_core.tools.catalog\n"
        "\n"
        "def build():\n"
        "    from ouroboros.app.supervisor_readiness import wait_for_supervisor_ready\n"
        "    from supervisor.state import get_state_repository\n"
        "    import ouroboros.agent.core as core\n"
    )

    assert _imports_above_the_seam(violating, "sample.py") == {
        "sample.py: new_core.tools.catalog",
        "sample.py: ouroboros.app.supervisor_readiness",
        "sample.py: supervisor.state",
        "sample.py: ouroboros.agent.core",
    }
    allowed = (
        "from ouroboros.storage.chat_store import append_chat_message\n"
        "from ouroboros.observability import aef_tracing\n"
        "import ouroboros.utils\n"
    )
    assert _imports_above_the_seam(allowed, "sample.py") == set()


def test_the_supervisors_runtime_modules_name_the_seam_and_never_a_backend():
    """Naming a backend belongs to a composition root, not to the loop or its wiring."""
    repo_root = pathlib.Path(__file__).resolve().parents[2]
    runtime_modules = ("supervisor/runtime.py", "supervisor/bootstrap.py", "supervisor/runtime_context.py")
    offenders = []
    for relative in runtime_modules:
        path = repo_root / relative
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            if re.search(r"\btransport\.infra\b", line):
                offenders.append(f"{relative}:{number}: {line.strip()}")

    assert offenders == []


def test_a_runtime_cannot_be_built_without_a_gateway():
    """The default that used to build one made the backend reachable from the supervisor."""
    from supervisor.bootstrap import build_supervisor_runtime
    from supervisor.runtime_context import SupervisorRuntime

    assert inspect.signature(build_supervisor_runtime).parameters["gateway"].default is inspect.Parameter.empty
    assert "gateway" not in {field.name for field in dataclasses.fields(SupervisorRuntime) if _has_default(field)}


def _has_default(field: dataclasses.Field) -> bool:
    return field.default is not dataclasses.MISSING or field.default_factory is not dataclasses.MISSING
