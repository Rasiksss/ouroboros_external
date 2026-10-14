"""The browser surface resolving owner and chat from deployment-trusted headers.

Every test here drives the real handshake or the real resolvers: the property being
proved is that two people talking to one instance stay two people, which a stand-in for
the transport would not prove at all.
"""

from __future__ import annotations

import asyncio
import json

import pytest
from starlette.websockets import WebSocketDisconnect

from ouroboros.app.task_stop_notifications import TaskStopNotifier, create_task_stop_transport
from ouroboros.contracts.owner_scope import configured_owner_id
from ouroboros.storage.core_memory.memory import get_memory
from ouroboros.storage.core_memory.scratchpad import get_scratchpad
from ouroboros.storage.runtime_models import ContextSummary
from ouroboros.storage.runtime_persistence import runtime_persistence
from ouroboros.utils import emit_ui_log
from tests.helpers.conversations import sql_conversation_repository
from tests.helpers.server_client import patched_server_client
from tests.helpers.shared import TEST_SCOPE, scope_with
from tests.helpers.supervisor import bootstrap_supervisor_for_tests
from transport.browser import ws as browser_ws
from transport.browser.identity import AGENT_HEADER_NAME, browser_identity
from transport.browser.ws import (
    CHAT_HEADER_NAME,
    OWNER_HEADER_NAME,
    IdentityRefused,
    get_browser_ws_hub,
    trusted_chat_id,
    trusted_owner_id,
)


@pytest.fixture
def trusted_headers():
    """Compose the resolver the mode implies, as the composition root would.

    The mode itself is a startup declaration; what the request paths hold is this object,
    so a test of those paths installs it rather than setting a flag they never read. It
    yields the registry it composed, because the agent axis now comes from there.
    """
    from tests.helpers.agent_registry import FakeAgentRegistry
    from transport.browser.identity import ConfiguredOwnerIdentity, TrustedHeaderIdentity, browser_identity

    registry = FakeAgentRegistry()
    browser_identity.initialize(TrustedHeaderIdentity(registry))
    yield registry
    browser_identity.initialize(ConfiguredOwnerIdentity(FakeAgentRegistry()))


@pytest.fixture
def configured_identity():
    """The resolver an instance without the mode composes, over a controllable registry.

    The trust-off resolver now records the instance's default agent through the registry on
    the first arrival, so it is composed the same way the composition root composes it —
    with a repository to mint the row through.
    """
    from tests.helpers.agent_registry import FakeAgentRegistry
    from transport.browser.identity import ConfiguredOwnerIdentity, browser_identity

    registry = FakeAgentRegistry()
    browser_identity.initialize(ConfiguredOwnerIdentity(registry))
    yield registry
    browser_identity.initialize(ConfiguredOwnerIdentity(FakeAgentRegistry()))


@pytest.fixture
def unreachable_registry():
    """The resolver over a registry that cannot answer, for the fail-closed path."""
    from tests.helpers.agent_registry import FakeAgentRegistry, UnreachableAgentRegistry
    from transport.browser.identity import ConfiguredOwnerIdentity, TrustedHeaderIdentity, browser_identity

    browser_identity.initialize(TrustedHeaderIdentity(UnreachableAgentRegistry()))
    yield
    browser_identity.initialize(ConfiguredOwnerIdentity(FakeAgentRegistry()))


def _scope_for(headers: dict[str, str]):
    """The resolver's answer for one set of headers. Asynchronous since the registry is."""
    return asyncio.get_event_loop().run_until_complete(browser_identity.resolver.scope_for(headers))


def test_a_valid_pair_resolves() -> None:
    headers = {OWNER_HEADER_NAME: "alice", CHAT_HEADER_NAME: "7"}

    assert trusted_owner_id(headers) == "alice"
    assert trusted_chat_id(headers) == "7"


def test_the_lookup_is_case_insensitive_and_strips() -> None:
    """A proxy chooses its own casing, and whitespace never distinguishes two owners."""
    headers = {OWNER_HEADER_NAME.lower(): "  alice  ", CHAT_HEADER_NAME.upper(): " 7 "}

    assert trusted_owner_id(headers) == "alice"
    # Surrounding whitespace is the one thing the boundary normalizes about the chat.
    assert trusted_chat_id(headers) == "7"


def test_the_owner_is_opaque() -> None:
    """A login, a subject claim and an internal id are the same thing to this transport."""
    for value in ("alice@example.com", "sub|1234", "17"):
        assert trusted_owner_id({OWNER_HEADER_NAME: value}) == value


@pytest.mark.parametrize(
    "headers",
    [
        {},
        {CHAT_HEADER_NAME: "7"},
        {OWNER_HEADER_NAME: ""},
        {OWNER_HEADER_NAME: "   "},
    ],
    ids=["absent", "chat-only", "empty", "whitespace"],
)
def test_an_unusable_owner_header_is_refused_with_a_reason(headers: dict[str, str]) -> None:
    with pytest.raises(IdentityRefused) as excinfo:
        trusted_owner_id(headers)

    assert OWNER_HEADER_NAME in excinfo.value.reason


@pytest.mark.parametrize(
    "raw",
    ["", "   ", "-1", "-3999", "-2000"],
    ids=["empty", "whitespace", "minus-one", "window-floor", "inside-window"],
)
def test_an_unusable_chat_header_is_refused_with_a_reason(raw: str) -> None:
    """The skill bridge's window is negative, so a value inside it names no conversation."""
    with pytest.raises(IdentityRefused) as excinfo:
        trusted_chat_id({OWNER_HEADER_NAME: "alice", CHAT_HEADER_NAME: raw})

    assert CHAT_HEADER_NAME in excinfo.value.reason


def test_an_absent_chat_header_is_refused() -> None:
    with pytest.raises(IdentityRefused):
        trusted_chat_id({OWNER_HEADER_NAME: "alice"})


@pytest.mark.parametrize(
    "raw",
    ["3f0a9c1e-5f4d-4a1b-9a2c-0f7b2c8e1d33", "A", "новый-разговор", "chat/2", "0", "00", "-0"],
    ids=["uuid", "letter", "non-ascii", "slashed", "zero", "padded-zero", "signed-zero"],
)
def test_a_chat_id_that_is_not_a_number_is_served(raw: str) -> None:
    """No length, character-class or Unicode policy: the refusal is the chat-id policy's alone.

    Zero and its spellings come with it — no allocator produces them, nothing turns them
    negative, and the conventional browser chat is 1.
    """
    assert trusted_chat_id({OWNER_HEADER_NAME: "alice", CHAT_HEADER_NAME: f"  {raw}  "}) == raw


def test_a_chat_header_longer_than_the_index_can_hold_is_refused_at_the_door() -> None:
    """`0016` puts the axis in two btree indexes, and PostgreSQL caps an index tuple.

    Unbounded, a multi-kilobyte header connects and reads an empty history, and then every
    insert under it fails — a conversation that is live and can never store anything.
    """
    from transport.browser.identity import MAX_CHAT_ID_LENGTH

    assert trusted_chat_id({CHAT_HEADER_NAME: "c" * MAX_CHAT_ID_LENGTH}) == "c" * MAX_CHAT_ID_LENGTH
    with pytest.raises(IdentityRefused) as refusal:
        trusted_chat_id({CHAT_HEADER_NAME: "c" * (MAX_CHAT_ID_LENGTH + 1)})
    assert CHAT_HEADER_NAME in refusal.value.reason


def test_a_synthetic_chat_id_is_refused_rather_than_clamped() -> None:
    """Clamping would let a caller aim at the synthetic range and land on a real chat."""
    from ouroboros.contracts.chat_id_policy import SKILL_BRIDGE_CHAT_ID_MAX, SKILL_BRIDGE_CHAT_ID_MIN

    for value in (SKILL_BRIDGE_CHAT_ID_MIN, SKILL_BRIDGE_CHAT_ID_MAX, -4100):
        with pytest.raises(IdentityRefused):
            trusted_chat_id({CHAT_HEADER_NAME: str(value)})


def test_a_spelling_that_coerces_into_the_synthetic_range_is_refused() -> None:
    """Wider than any allocator writes, which is the safe direction: every consumer coerces
    through the same function, so a spelling that reads as synthetic anywhere reads as
    synthetic everywhere."""
    for raw in ("-4_000", " -7 ", "-0007"):
        with pytest.raises(IdentityRefused):
            trusted_chat_id({CHAT_HEADER_NAME: raw})


def test_the_owner_is_not_read_from_a_header_while_trust_is_off() -> None:
    """The owner stays the constant; the axes below it are the request's to name."""
    from ouroboros.contracts.owner_scope import configured_agent_id, configured_owner_id

    scope = _scope_for({OWNER_HEADER_NAME: "alice", CHAT_HEADER_NAME: "7"})

    assert scope.owner_id == configured_owner_id()
    assert scope.agent_id == configured_agent_id()
    assert scope.chat_id == "7"


def test_an_absent_chat_header_is_the_conventional_conversation() -> None:
    """With one owner there is a conversation to fall back to, and this is it."""
    scope = _scope_for({})

    assert scope.chat_id == str(browser_ws.BROWSER_CHAT_ID)


def test_a_synthetic_conversation_is_refused_while_trust_is_off() -> None:
    """One rule for what a conversation id may be, whichever resolver reads it."""
    with pytest.raises(IdentityRefused):
        _scope_for({CHAT_HEADER_NAME: "-1"})


def test_an_agent_header_is_honoured_under_the_configured_owner(configured_identity) -> None:
    """One owner means a header can only select within it, so it is read."""
    loop = asyncio.get_event_loop()
    loop.run_until_complete(configured_identity.get_or_create_default_agent(configured_owner_id()))
    sub = loop.run_until_complete(configured_identity.create_agent(configured_owner_id(), "analyst", ""))

    scope = _scope_for({AGENT_HEADER_NAME: sub.agent_id})

    assert scope.agent_id == sub.agent_id


def test_the_configured_resolver_records_the_instances_default_agent(configured_identity) -> None:
    """A first arrival mints the row the owner-settings surface verifies agents against.

    The settings of an agent are keyed by owner and agent, and the surface refuses an
    agent the registry does not hold — so a deployment that never records its default
    agent could not change settings at all. The constant-owner mode is not exempt from
    the rule that a transport arrival creates it.
    """
    assert configured_identity.by_owner == {}

    scope = _scope_for({OWNER_HEADER_NAME: "alice", CHAT_HEADER_NAME: "7"})

    assert configured_identity.created_for == [configured_owner_id()]
    assert configured_identity.by_owner[configured_owner_id()][0].agent_id == scope.agent_id


def test_the_configured_scope_carries_the_registrys_default_agent(configured_identity) -> None:
    """The agent axis is the record's even while trust is off, not a parallel constant.

    The registry is made to hold an agent id configuration would never mint, so the two
    provenances are distinguishable: a scope carrying ``configured_agent_id()`` here
    would mean the lookup was skipped and the row never written.
    """
    from ouroboros.contracts.owner_scope import configured_agent_id

    configured_identity.default_agent_id = "recorded-default"
    scope = _scope_for({OWNER_HEADER_NAME: "alice", CHAT_HEADER_NAME: "7"})

    assert scope.agent_id == "recorded-default"
    assert scope.agent_id != configured_agent_id()


def test_an_unreachable_registry_refuses_the_configured_resolver_too() -> None:
    """The same fail-closed answer the trusted path gets: no scope over an absent record.

    A single-owner deployment is one owner, not no registry: serving a scope whose agent
    the record does not hold would write rows under a key nothing resolves, and the
    settings surface would refuse the agent anyway.
    """
    from ouroboros.contracts.owner_scope import configured_agent_id
    from tests.helpers.agent_registry import UnreachableAgentRegistry
    from transport.browser.identity import ConfiguredOwnerIdentity, browser_identity

    previous = browser_identity.resolver
    browser_identity.initialize(ConfiguredOwnerIdentity(UnreachableAgentRegistry()))
    try:
        with pytest.raises(IdentityRefused) as refusal:
            _scope_for({OWNER_HEADER_NAME: "alice", CHAT_HEADER_NAME: "7"})
    finally:
        browser_identity.initialize(previous)

    assert "registry" in refusal.value.reason
    assert configured_agent_id() not in refusal.value.reason
    assert refusal.value.status == 503


def test_the_scope_comes_from_the_headers_while_trust_is_on(trusted_headers) -> None:
    scope = _scope_for({OWNER_HEADER_NAME: "alice", CHAT_HEADER_NAME: "7"})

    assert (scope.owner_id, scope.chat_id) == ("alice", "7")
    assert str(scope.surface_id) == browser_ws.TRANSPORT_NAME


def test_the_scope_carries_the_registrys_default_agent(trusted_headers) -> None:
    """The axis is what the record says, not what configuration says.

    The registry is made to hold an agent id configuration would never mint, so the two
    provenances are distinguishable: a scope carrying ``configured_agent_id()`` here would
    mean the lookup was skipped.
    """
    from ouroboros.contracts.owner_scope import configured_agent_id

    trusted_headers.default_agent_id = "recorded-default"
    scope = _scope_for({OWNER_HEADER_NAME: "alice", CHAT_HEADER_NAME: "7"})

    assert scope.agent_id == "recorded-default"
    assert scope.agent_id != configured_agent_id()


def test_a_first_time_owner_is_served_rather_than_refused(trusted_headers) -> None:
    """An owner appearing for the first time needs no administrative step."""
    assert trusted_headers.by_owner == {}

    scope = _scope_for({OWNER_HEADER_NAME: "newcomer", CHAT_HEADER_NAME: "7"})

    assert scope.owner_id == "newcomer"
    assert trusted_headers.created_for == ["newcomer"]
    assert scope.agent_id == trusted_headers.by_owner["newcomer"][0].agent_id


def test_the_registry_is_consulted_per_request_rather_than_remembered(trusted_headers) -> None:
    """The scope reflects the record at the moment it is minted."""
    first = _scope_for({OWNER_HEADER_NAME: "alice", CHAT_HEADER_NAME: "7"})
    trusted_headers.by_owner.clear()
    trusted_headers.default_agent_id = "a-later-default"
    second = _scope_for({OWNER_HEADER_NAME: "alice", CHAT_HEADER_NAME: "7"})

    assert first.agent_id != second.agent_id
    assert second.agent_id == "a-later-default"


def test_an_agent_header_the_owner_holds_is_honoured(trusted_headers) -> None:
    """The agent is selected by the caller and looked up inside the settled owner."""
    sub = asyncio.get_event_loop().run_until_complete(trusted_headers.create_agent("alice", "analyst", ""))

    scope = _scope_for({OWNER_HEADER_NAME: "alice", CHAT_HEADER_NAME: "7", AGENT_HEADER_NAME: sub.agent_id})

    assert scope.agent_id == sub.agent_id


def test_a_second_spelling_of_the_agent_header_is_ignored(trusted_headers) -> None:
    """One spelling is honoured; a second would be a second door onto the same decision."""
    sub = asyncio.get_event_loop().run_until_complete(trusted_headers.create_agent("alice", "analyst", ""))

    scope = _scope_for({OWNER_HEADER_NAME: "alice", CHAT_HEADER_NAME: "7", "X-Agent-Id": sub.agent_id})

    assert scope.agent_id == trusted_headers.default_agent_id


def test_no_agent_header_is_the_default_agent(trusted_headers) -> None:
    """Every client built before the header existed keeps the scope it had."""
    scope = _scope_for({OWNER_HEADER_NAME: "alice", CHAT_HEADER_NAME: "7"})

    assert scope.agent_id == trusted_headers.by_owner["alice"][0].agent_id


def test_an_agent_of_another_owner_is_refused(trusted_headers) -> None:
    """The lookup names the settled owner, so another owner's agent resolves to nothing."""
    theirs = asyncio.get_event_loop().run_until_complete(trusted_headers.create_agent("bob", "theirs", ""))

    with pytest.raises(IdentityRefused) as refusal:
        _scope_for({OWNER_HEADER_NAME: "alice", CHAT_HEADER_NAME: "7", AGENT_HEADER_NAME: theirs.agent_id})

    assert refusal.value.status == 403


def test_an_unknown_agent_is_refused_rather_than_softened(trusted_headers) -> None:
    """Serving the default agent here would show one conversation under another's name."""
    with pytest.raises(IdentityRefused):
        _scope_for({OWNER_HEADER_NAME: "alice", CHAT_HEADER_NAME: "7", AGENT_HEADER_NAME: "no-such-agent"})


def test_a_deactivated_agent_is_refused(trusted_headers) -> None:
    """The refusal the axis below already makes for a retired conversation, one level up."""
    loop = asyncio.get_event_loop()
    sub = loop.run_until_complete(trusted_headers.create_agent("alice", "retired", ""))
    loop.run_until_complete(trusted_headers.deactivate_agent("alice", sub.agent_id))

    with pytest.raises(IdentityRefused):
        _scope_for({OWNER_HEADER_NAME: "alice", CHAT_HEADER_NAME: "7", AGENT_HEADER_NAME: sub.agent_id})


def test_a_deactivated_default_agent_is_refused_for_an_absent_header(trusted_headers) -> None:
    """A retirement honoured for every agent except the one nobody has to name is not honoured."""
    loop = asyncio.get_event_loop()
    loop.run_until_complete(trusted_headers.get_or_create_default_agent("alice"))
    loop.run_until_complete(trusted_headers.deactivate_agent("alice", trusted_headers.default_agent_id))

    with pytest.raises(IdentityRefused) as refusal:
        _scope_for({OWNER_HEADER_NAME: "alice", CHAT_HEADER_NAME: "7"})

    assert refusal.value.status == 403


def test_an_unbounded_agent_header_is_refused_before_the_registry_is_read(trusted_headers) -> None:
    """The registry mints at most 32 hex characters, so anything longer names nothing anyway."""
    from transport.browser.identity import MAX_AGENT_ID_LENGTH

    with pytest.raises(IdentityRefused):
        _scope_for(
            {OWNER_HEADER_NAME: "alice", CHAT_HEADER_NAME: "7", AGENT_HEADER_NAME: "a" * (MAX_AGENT_ID_LENGTH + 1)}
        )

    assert trusted_headers.created_for == []


def test_a_request_naming_an_agent_creates_nothing(trusted_headers) -> None:
    """Only an unnamed agent mints a first-time owner's default record."""
    with pytest.raises(IdentityRefused):
        _scope_for({OWNER_HEADER_NAME: "newcomer", CHAT_HEADER_NAME: "7", AGENT_HEADER_NAME: "anything"})

    assert trusted_headers.created_for == []


def test_an_unreachable_registry_refuses_rather_than_defaulting(unreachable_registry) -> None:
    from ouroboros.contracts.owner_scope import configured_agent_id

    with pytest.raises(IdentityRefused) as refusal:
        _scope_for({OWNER_HEADER_NAME: "alice", CHAT_HEADER_NAME: "7"})

    assert "registry" in refusal.value.reason
    # No scope was produced at all, so none carries the configured agent.
    assert configured_agent_id() not in refusal.value.reason
    assert refusal.value.status == 503


def test_a_bad_chat_header_is_refused_before_the_registry_is_written(trusted_headers) -> None:
    with pytest.raises(IdentityRefused) as refusal:
        _scope_for({OWNER_HEADER_NAME: "alice", CHAT_HEADER_NAME: "-1"})

    assert refusal.value.status == 403
    assert trusted_headers.created_for == []


def test_no_branch_falls_back_to_the_configured_owner_while_trust_is_on(trusted_headers) -> None:
    with pytest.raises(IdentityRefused):
        _scope_for({})
    with pytest.raises(IdentityRefused):
        _scope_for({OWNER_HEADER_NAME: "alice"})
    with pytest.raises(IdentityRefused):
        _scope_for({CHAT_HEADER_NAME: "7"})


# -- the handshake ------------------------------------------------------------


def _client(monkeypatch, tmp_path):
    return patched_server_client(monkeypatch, tmp_path, browser_enabled=True, reset_browser_ws=True)


def _headers(owner: str, chat: int | str) -> dict[str, str]:
    return {OWNER_HEADER_NAME: owner, CHAT_HEADER_NAME: str(chat)}


def test_two_connections_are_attributed_to_the_owners_they_presented(monkeypatch, tmp_path, trusted_headers):
    """The property the whole change exists for, driven through the real handshake."""
    runtime = bootstrap_supervisor_for_tests(tmp_path / "data")
    submitted: list = []
    monkeypatch.setattr(runtime.gateway.inbox, "submit", submitted.append)

    with _client(monkeypatch, tmp_path) as client:
        with client.websocket_connect("/ws", headers=_headers("alice", 7)) as alice:
            alice.send_json({"type": "chat", "content": "from alice", "client_message_id": "a-1"})
            json.loads(alice.receive_text())
        with client.websocket_connect("/ws", headers=_headers("bob", 9)) as bob:
            # A body naming an owner and a chat: ignored for identity on every path.
            bob.send_json(
                {
                    "type": "chat",
                    "content": "from bob",
                    "client_message_id": "b-1",
                    "owner_id": "alice",
                    "chat_id": 7,
                }
            )
            json.loads(bob.receive_text())

    attributed = {
        envelope.effective_text(): (envelope.scope.owner_id, envelope.scope.chat_id) for envelope in submitted
    }
    assert attributed == {"from alice": ("alice", "7"), "from bob": ("bob", "9")}


@pytest.mark.parametrize(
    "headers",
    [{}, {CHAT_HEADER_NAME: "7"}, {OWNER_HEADER_NAME: "alice"}, {OWNER_HEADER_NAME: "alice", CHAT_HEADER_NAME: "-7"}],
    ids=["no-headers", "no-owner", "no-chat", "negative-chat"],
)
def test_a_handshake_without_a_usable_identity_is_refused(monkeypatch, tmp_path, trusted_headers, headers):
    """Refused at the handshake, so the caller never has a connection to send on."""
    bootstrap_supervisor_for_tests(tmp_path / "data")

    with _client(monkeypatch, tmp_path) as client:
        # The connect itself fails: nothing was accepted, so there is no socket to send on.
        with pytest.raises(WebSocketDisconnect) as refusal:
            client.websocket_connect("/ws", headers=headers).__enter__()

    assert refusal.value.code == 1008
    assert refusal.value.reason
    assert not get_browser_ws_hub().has_clients()


def test_the_handshake_is_unchanged_while_the_setting_is_off(monkeypatch, tmp_path):
    """No header is read on any path, and the scope is the one it has always been."""
    runtime = bootstrap_supervisor_for_tests(tmp_path / "data")
    submitted: list = []
    monkeypatch.setattr(runtime.gateway.inbox, "submit", submitted.append)

    with _client(monkeypatch, tmp_path) as client:
        with client.websocket_connect("/ws") as websocket:
            websocket.send_json({"type": "chat", "content": "hello", "client_message_id": "c-1"})
            assert json.loads(websocket.receive_text())["role"] == "user"

    assert [envelope.scope.owner_id for envelope in submitted] == [configured_owner_id()]
    assert [envelope.scope.chat_id for envelope in submitted] == [str(browser_ws.BROWSER_CHAT_ID)]


# -- delivery -----------------------------------------------------------------


def _next_frame(websocket) -> dict:
    """The next frame this socket receives.

    Asserting on the *first* frame is what makes the negative claims here real: a frame
    published for another owner would arrive ahead of the one published after it, so
    "the first frame is mine" is also "the other owner's never came".

    Heartbeats are skipped, not returned: they are a liveness broadcast to every owner and
    carry no owner-specific data, so one can land on a socket while a test holds it open
    (the interval fires the moment a client connects and idles), and returning it would
    hide the message frame the assertion is waiting for.
    """
    while True:
        frame = json.loads(websocket.receive_text())
        if frame.get("type") != "heartbeat":
            return frame


def test_a_message_reaches_only_the_owner_it_belongs_to(monkeypatch, tmp_path, trusted_headers):
    runtime = bootstrap_supervisor_for_tests(tmp_path / "data")
    monkeypatch.setattr(runtime.gateway.inbox, "submit", lambda _envelope: None)

    with _client(monkeypatch, tmp_path) as client:
        with client.websocket_connect("/ws", headers=_headers("alice", 7)) as alice:
            with client.websocket_connect("/ws", headers=_headers("bob", 9)) as bob:
                runtime.bridge.send_message(
                    "for alice",
                    source="browser",
                    scope=scope_with(TEST_SCOPE, owner_id="alice", chat_id="7"),
                )
                runtime.bridge.send_message(
                    "for bob",
                    source="browser",
                    scope=scope_with(TEST_SCOPE, owner_id="bob", chat_id="9"),
                )

                for_alice = _next_frame(alice)
                for_bob = _next_frame(bob)

    assert for_alice["content"] == "for alice"
    # Published first, so it would be bob's first frame had it reached him at all.
    assert for_bob["content"] == "for bob"


def test_a_message_with_no_open_connection_is_acknowledged_and_dropped(monkeypatch, tmp_path, trusted_headers):
    """Dropped, never held back: the messages behind it must still deliver."""
    runtime = bootstrap_supervisor_for_tests(tmp_path / "data")
    monkeypatch.setattr(runtime.gateway.inbox, "submit", lambda _envelope: None)

    with _client(monkeypatch, tmp_path) as client:
        with client.websocket_connect("/ws", headers=_headers("alice", 7)) as alice:
            runtime.bridge.send_message(
                "for nobody",
                source="browser",
                scope=scope_with(TEST_SCOPE, owner_id="carol", chat_id="7"),
            )
            runtime.bridge.send_message(
                "for alice",
                source="browser",
                scope=scope_with(TEST_SCOPE, owner_id="alice", chat_id="7"),
            )

            delivered = _next_frame(alice)

    assert delivered["content"] == "for alice"


def test_a_runtime_log_line_reaches_no_connection_while_trust_is_on(monkeypatch, tmp_path, trusted_headers):
    """A log record carries no owner, so there is no connection it correctly belongs to."""
    runtime = bootstrap_supervisor_for_tests(tmp_path / "data")
    monkeypatch.setattr(runtime.gateway.inbox, "submit", lambda _envelope: None)

    with _client(monkeypatch, tmp_path) as client:
        with client.websocket_connect("/ws", headers=_headers("alice", 7)) as alice:
            emit_ui_log({"type": "log", "data": {"level": "info", "message": "one owner's output"}})
            runtime.bridge.send_message(
                "an answer",
                source="browser",
                scope=scope_with(TEST_SCOPE, owner_id="alice", chat_id="7"),
            )

            # The log line was emitted first: had it been forwarded, it would arrive first.
            first = _next_frame(alice)

    assert first["type"] == "chat"
    assert first["content"] == "an answer"


def test_a_runtime_log_line_still_reaches_the_connection_while_trust_is_off(monkeypatch, tmp_path):
    runtime = bootstrap_supervisor_for_tests(tmp_path / "data")
    monkeypatch.setattr(runtime.gateway.inbox, "submit", lambda _envelope: None)

    with _client(monkeypatch, tmp_path) as client:
        with client.websocket_connect("/ws") as websocket:
            emit_ui_log({"type": "log", "data": {"level": "info", "message": "the only owner's output"}})

            assert _next_frame(websocket)["data"]["message"] == "the only owner's output"


# -- the history route --------------------------------------------------------


def test_history_is_scoped_to_the_owner_the_request_presents(monkeypatch, tmp_path, trusted_headers):
    runtime = bootstrap_supervisor_for_tests(tmp_path / "data")
    monkeypatch.setattr(runtime.gateway.inbox, "submit", lambda _envelope: None)

    with _client(monkeypatch, tmp_path) as client:
        with client.websocket_connect("/ws", headers=_headers("alice", 7)) as alice:
            alice.send_json({"type": "chat", "content": "alice said this", "client_message_id": "a-1"})
            _next_frame(alice)
        with client.websocket_connect("/ws", headers=_headers("bob", 9)) as bob:
            bob.send_json({"type": "chat", "content": "bob said this", "client_message_id": "b-1"})
            _next_frame(bob)

        hers = client.get("/api/chat/history", headers=_headers("alice", 7)).json()
        his = client.get("/api/chat/history", headers=_headers("bob", 9)).json()
        refused = client.get("/api/chat/history")

    assert [message["text"] for message in hers["messages"]] == ["alice said this"]
    assert [message["text"] for message in his["messages"]] == ["bob said this"]
    assert refused.status_code == 403
    assert refused.json()["error"]


def test_job_detail_is_owner_scoped_and_exposes_only_the_closed_projection(monkeypatch, tmp_path, trusted_headers):
    from starlette.applications import Starlette
    from starlette.testclient import TestClient

    from ouroboros.storage.runtime_models import TaskRecord
    from tests.conftest import refresh_test_config
    from transport.browser.router import collect_routes

    monkeypatch.setenv("OUROBOROS_DURABLE_JOBS_ENABLED", "true")
    refresh_test_config()
    bootstrap_supervisor_for_tests(tmp_path / "data")
    alice = scope_with(TEST_SCOPE, owner_id="alice", chat_id="7", surface_id="browser")
    runtime_persistence.current.tasks_repo.upsert_task(
        TaskRecord(
            task_id="internal-attempt",
            job_id="public-job",
            detached=True,
            owner_id="alice",
            agent_id=alice.agent_id,
            status="completed",
            result="full result",
            input={"scope": alice.to_dict(), "text": "full task", "inherited_context_seed": "secret seed"},
        )
    )

    app = Starlette(routes=collect_routes(data_dir=tmp_path, repo_dir=tmp_path))
    app.state.task_stop_notifier = TaskStopNotifier(create_task_stop_transport()[0])
    with TestClient(app) as client:
        detail = client.get("/api/jobs/public-job", headers=_headers("alice", 7))
        absent = client.get("/api/jobs/public-job", headers=_headers("bob", 9))

    assert detail.json() == {
        "job": {
            "job_id": "public-job",
            "surface_id": "browser",
            "chat_id": "7",
            "detached": True,
            "status": "succeeded",
            "created_at": detail.json()["job"]["created_at"],
            "started_at": None,
            "preview": "full task",
            "task": "full task",
            "finished_at": None,
            "result": "full result",
            "error": None,
        }
    }
    assert absent.status_code == 404


def test_job_management_isolates_two_agents_of_one_owner(monkeypatch, tmp_path, trusted_headers):
    from types import SimpleNamespace

    from starlette.applications import Starlette
    from starlette.testclient import TestClient

    from ouroboros.storage.runtime_models import TaskRequest
    from supervisor.commands import handle_slash_command
    from transport.browser.router import collect_routes

    bootstrap_supervisor_for_tests(tmp_path / "data")
    scope = _scope_for(_headers("alice", 7))
    foreign = scope_with(scope, agent_id="second-agent")
    for job, identity, status in (
        ("mine", scope, "queued"),
        ("foreign", foreign, "queued"),
        ("secret", foreign, "completed"),
    ):
        runtime_persistence.current.tasks_repo.create_task(
            TaskRequest(id=job, detached=True, text=job, scope=identity.to_dict()), status=status
        )
    monkeypatch.setenv("OUROBOROS_DURABLE_JOBS_ENABLED", "true")
    sent = []
    ctx = SimpleNamespace(
        tasks_repo=runtime_persistence.current.tasks_repo, send_with_budget=lambda text, **kw: sent.append(text)
    )
    app = Starlette(routes=collect_routes(data_dir=tmp_path, repo_dir=tmp_path))
    app.state.detached_enabled = True
    app.state.task_stop_notifier = TaskStopNotifier(create_task_stop_transport()[0])
    with TestClient(app) as client:
        headers = _headers("alice", 7)
        assert [job["job_id"] for job in client.get("/api/jobs", headers=headers).json()["jobs"]] == ["mine"]
        for job in ("foreign", "secret", "missing"):
            assert client.get(f"/api/jobs/{job}", headers=headers).status_code == 404
            response = client.post(f"/api/jobs/{job}/stop", headers=headers)
            assert response.status_code == 404 and response.json() == {"error": "job not found"}
            handle_slash_command(f"/stop {job}", ctx=ctx, scope=scope, source="browser")
            assert sent[-1] == "Задача не найдена."
    handle_slash_command("/jobs", ctx=ctx, scope=scope, source="browser")
    assert "mine" in sent[-1] and "foreign" not in sent[-1]
    handle_slash_command("/stop", ctx=ctx, scope=scope, source="browser")
    assert runtime_persistence.current.tasks_repo.get_task("mine").status == "cancelled"
    assert runtime_persistence.current.tasks_repo.get_task("foreign").status == "queued"
    assert runtime_persistence.current.tasks_repo.get_task("secret").status == "completed"
    assert runtime_persistence.current.tasks_repo.get_current_job("foreign", scope=foreign).job_id == "foreign"


def test_job_list_contains_only_active_jobs_and_breaks_time_ties_by_job_id(monkeypatch, tmp_path, trusted_headers):
    from starlette.applications import Starlette
    from starlette.testclient import TestClient

    from ouroboros.storage.runtime_models import TaskRecord
    from transport.browser.router import collect_routes

    bootstrap_supervisor_for_tests(tmp_path / "data")
    scope = scope_with(TEST_SCOPE, owner_id="alice", chat_id="7", surface_id="browser")
    common = {
        "detached": True,
        "owner_id": "alice",
        "agent_id": scope.agent_id,
        "scope": scope.to_dict(),
        "created_at": "2026-01-01",
    }
    for task_id, job_id, status in (
        ("z-attempt", "a-job", "queued"),
        ("a-attempt", "z-job", "queued"),
        ("terminal", "done-job", "completed"),
    ):
        runtime_persistence.current.tasks_repo.upsert_task(
            TaskRecord(task_id=task_id, job_id=job_id, status=status, **common)
        )

    app = Starlette(routes=collect_routes(data_dir=tmp_path, repo_dir=tmp_path))
    app.state.detached_enabled = False
    app.state.task_stop_notifier = TaskStopNotifier(create_task_stop_transport()[0])
    with TestClient(app) as client:
        payload = client.get("/api/jobs", headers=_headers("alice", 7)).json()

    assert [job["job_id"] for job in payload["jobs"]] == ["z-job", "a-job"]
    assert payload["truncated"] is False


def test_job_stop_route_is_idempotent_and_preserves_the_first_stop_time(monkeypatch, tmp_path, trusted_headers):
    from starlette.applications import Starlette
    from starlette.testclient import TestClient

    from ouroboros.storage.runtime_models import TaskRequest
    from transport.browser.router import collect_routes

    bootstrap_supervisor_for_tests(tmp_path / "data")
    scope = scope_with(TEST_SCOPE, owner_id="alice", chat_id="7", surface_id="browser")
    runtime_persistence.current.tasks_repo.create_task(
        TaskRequest(id="job", detached=True, text="work", scope=scope.to_dict())
    )
    runtime_persistence.current.tasks_repo.claim_next_task(runtime_id="runtime")
    app = Starlette(routes=collect_routes(data_dir=tmp_path, repo_dir=tmp_path))
    app.state.task_stop_notifier = TaskStopNotifier(create_task_stop_transport()[0])

    with TestClient(app) as client:
        first = client.post("/api/jobs/job/stop", headers=_headers("alice", 7))
        first_stop = runtime_persistence.current.tasks_repo.get_task("job").stop_requested_at
        second = client.post("/api/jobs/job/stop", headers=_headers("alice", 7))

    assert first.status_code == second.status_code == 200
    assert first.json()["job"]["status"] == second.json()["job"]["status"] == "stopping"
    assert runtime_persistence.current.tasks_repo.get_task("job").stop_requested_at == first_stop


def test_job_routes_run_synchronous_store_calls_off_the_event_loop(monkeypatch, tmp_path, trusted_headers):
    import asyncio

    from starlette.applications import Starlette
    from starlette.testclient import TestClient

    from ouroboros.storage.runtime_models import TaskRecord
    from transport.browser.router import collect_routes

    store = runtime_persistence.current
    record = TaskRecord(
        task_id="job",
        job_id="job",
        detached=True,
        owner_id="alice",
        agent_id=TEST_SCOPE.agent_id,
        status="running",
        input={"text": "work"},
    )
    on_event_loop: dict[str, bool] = {}

    def observe(name, result):
        def call(*_args, **_kwargs):
            try:
                asyncio.get_running_loop()
            except RuntimeError:
                on_event_loop[name] = False
            else:
                on_event_loop[name] = True
            return result

        return call

    monkeypatch.setattr(store.tasks_repo, "list_current_jobs", observe("list", [record]))
    monkeypatch.setattr(store.tasks_repo, "get_current_job", observe("detail", record))
    monkeypatch.setattr(store.tasks_repo, "stop_jobs", observe("stop", [record]))
    app = Starlette(routes=collect_routes(data_dir=tmp_path, repo_dir=tmp_path))
    app.state.detached_enabled = False
    app.state.task_stop_notifier = TaskStopNotifier(create_task_stop_transport()[0])

    with TestClient(app) as client:
        assert client.get("/api/jobs", headers=_headers("alice", 7)).status_code == 200
        assert client.get("/api/jobs/job", headers=_headers("alice", 7)).status_code == 200
        assert client.post("/api/jobs/job/stop", headers=_headers("alice", 7)).status_code == 200

    assert on_event_loop == {"list": False, "detail": False, "stop": False}


def test_history_is_the_shared_conversation_while_the_setting_is_off(monkeypatch, tmp_path):
    runtime = bootstrap_supervisor_for_tests(tmp_path / "data")
    monkeypatch.setattr(runtime.gateway.inbox, "submit", lambda _envelope: None)

    with _client(monkeypatch, tmp_path) as client:
        with client.websocket_connect("/ws") as websocket:
            websocket.send_json({"type": "chat", "content": "hello", "client_message_id": "c-1"})
            _next_frame(websocket)

        # The socket named no conversation, so the history of the conventional one is asked
        # for the same way, and a header naming another conversation reads that one instead.
        shared = client.get("/api/chat/history").json()
        other = client.get("/api/chat/history", headers=_headers("alice", 7)).json()

    assert [message["text"] for message in shared["messages"]] == ["hello"]
    assert other["messages"] == []


def test_an_opaque_chat_connects_and_carries_its_own_axis(monkeypatch, tmp_path, trusted_headers):
    """A browser client names a conversation the server has never counted to."""
    runtime = bootstrap_supervisor_for_tests(tmp_path / "data")
    submitted: list = []
    monkeypatch.setattr(runtime.gateway.inbox, "submit", submitted.append)
    opaque = "3f0a9c1e-5f4d-4a1b-9a2c-0f7b2c8e1d33"

    with _client(monkeypatch, tmp_path) as client:
        with client.websocket_connect("/ws", headers=_headers("alice", opaque)) as websocket:
            websocket.send_json({"type": "chat", "content": "a brand new thread", "client_message_id": "n-1"})
            json.loads(websocket.receive_text())

    assert [(envelope.scope.owner_id, envelope.scope.chat_id) for envelope in submitted] == [("alice", opaque)]


def test_two_spellings_of_one_number_are_two_conversations(monkeypatch, tmp_path, trusted_headers):
    """The cost the opaque rule states rather than mitigates.

    Nothing folds the axis, so `"07"` reads back an empty history and a `200` — which is
    exactly what a conversation nobody has written to looks like, and the boundary cannot
    tell the two apart.
    """
    runtime = bootstrap_supervisor_for_tests(tmp_path / "data")
    monkeypatch.setattr(runtime.gateway.inbox, "submit", lambda _envelope: None)

    plain = _scope_for(_headers("alice", "7"))
    padded = _scope_for(_headers("alice", "07"))
    assert (plain.owner_id, padded.owner_id) == ("alice", "alice")
    assert (plain.chat_id, padded.chat_id) == ("7", "07")

    with _client(monkeypatch, tmp_path) as client:
        with client.websocket_connect("/ws", headers=_headers("alice", "7")) as websocket:
            websocket.send_json({"type": "chat", "content": "said under seven", "client_message_id": "s-1"})
            _next_frame(websocket)

        history = {
            chat: [
                message["text"]
                for message in client.get("/api/chat/history", headers=_headers("alice", chat)).json()["messages"]
            ]
            for chat in ("7", "07")
        }

    assert history == {"7": ["said under seven"], "07": []}


# -- the isolation regression -------------------------------------------------


def test_two_owners_read_back_only_their_own_state(monkeypatch, tmp_path, trusted_headers):
    """The claim this whole change exists to make testable, on one instance.

    Covers what it names and no more: durable memory, the scratchpad, chat history and
    the committed context summary. Tasks, files, attachments, budgets and operator
    commands stay shared and are deliberately not claimed here.
    """
    runtime = bootstrap_supervisor_for_tests(tmp_path / "data")
    monkeypatch.setattr(runtime.gateway.inbox, "submit", lambda _envelope: None)
    get_memory.cache_clear()
    get_scratchpad.cache_clear()
    memory, scratchpad = get_memory(), get_scratchpad()
    store = runtime_persistence.current

    # The same builder the handshake mints from, so what is written is keyed by exactly
    # what the connection presented.
    hers = _scope_for(_headers("alice", 7))
    his = _scope_for(_headers("bob", 9))

    with _client(monkeypatch, tmp_path) as client:
        with client.websocket_connect("/ws", headers=_headers("alice", 7)) as alice:
            alice.send_json({"type": "chat", "content": "alice said this", "client_message_id": "a-1"})
            _next_frame(alice)
        with client.websocket_connect("/ws", headers=_headers("bob", 9)) as bob:
            bob.send_json({"type": "chat", "content": "bob said this", "client_message_id": "b-1"})
            _next_frame(bob)

        for scope, body in ((hers, "alice's fact\n"), (his, "bob's fact\n")):
            memory.save(scope, "notes", "project", "d", body)
            scratchpad.append_block(scope, f"{body.strip()} — scratch")
            sql_conversation_repository(store.engine).upsert_context_summary(
                ContextSummary(
                    owner_id=scope.owner_id,
                    agent_id=scope.agent_id,
                    chat_id=scope.chat_id,
                    summary_ref=f"summary for {scope.owner_id}",
                )
            )

        history = {
            owner: [message["text"] for message in client.get("/api/chat/history", headers=headers).json()["messages"]]
            for owner, headers in (("alice", _headers("alice", 7)), ("bob", _headers("bob", 9)))
        }

    # ``read`` renders the stored row with its front matter; the body is what was saved.
    assert "alice's fact" in memory.read(hers, "notes")[0]
    assert "bob's fact" not in memory.read(hers, "notes")[0]
    assert "bob's fact" in memory.read(his, "notes")[0]
    assert "alice's fact" not in memory.read(his, "notes")[0]
    assert [block.content for block in scratchpad.list_blocks(hers)] == ["alice's fact — scratch"]
    assert [block.content for block in scratchpad.list_blocks(his)] == ["bob's fact — scratch"]
    assert sql_conversation_repository(store.engine).get_latest_context_summary(hers).summary_ref == "summary for alice"
    assert sql_conversation_repository(store.engine).get_latest_context_summary(his).summary_ref == "summary for bob"
    assert history == {"alice": ["alice said this"], "bob": ["bob said this"]}


def test_a_second_owners_colliding_message_is_submitted_not_refused(monkeypatch, tmp_path, trusted_headers):
    """The loss case, end to end: one owner's submission must not adopt another's row.

    A chat id is owner-relative and a client mints its own message ids, so two owners
    behind one proxy collide on the tuple the stored identity used to be. The second
    owner was refused with `message_conflict`, or — when the payloads matched — adopted
    the first owner's unprocessed row and marked it processed, which is the first
    owner's message lost: the sweep that exists to replay it then reads it as handed
    over.
    """
    from ouroboros.storage.chat_store import list_owner_messages

    runtime = bootstrap_supervisor_for_tests(tmp_path / "data")
    submitted: list = []
    monkeypatch.setattr(runtime.gateway.inbox, "submit", submitted.append)

    frames = []
    with _client(monkeypatch, tmp_path) as client:
        with client.websocket_connect("/ws", headers=_headers("alice", 1)) as alice:
            with client.websocket_connect("/ws", headers=_headers("bob", 1)) as bob:
                for websocket in (alice, bob):
                    websocket.send_json({"type": "chat", "content": "same text", "client_message_id": "1"})
                    frames.append(_next_frame(websocket))

    assert [frame.get("type") for frame in frames] == ["chat", "chat"], f"one owner was refused: {frames}"
    assert [envelope.scope.owner_id for envelope in submitted] == ["alice", "bob"]
    for owner in ("alice", "bob"):
        rows = list_owner_messages(
            tmp_path / "data",
            scope=scope_with(TEST_SCOPE, owner_id=owner, chat_id="1"),
        )
        assert [row.content for row in rows if row.actor_type == "owner"] == ["same text"]
