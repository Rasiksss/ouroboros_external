from __future__ import annotations

import logging
from typing import get_args

import pytest
from pydantic import ValidationError

from ouroboros.storage.settings.schema import (
    A2aRemoteAgentEntry,
    EgressAuth,
    EgressAuthKind,
    EgressEntry,
    McpServerEntry,
    NoAuth,
    UserTokenAuth,
    _coerce_a2a_remote_agents,
    connectable_a2a_agents,
)
from tests.helpers.parametrize import parametrize_from_dict

_LOGGER = "ouroboros.storage.settings.schema"
_URL = "https://agent.example.test/a2a"
_USER_HEADERS = {"kind": "user_headers", "headers": ["X-Sudir-Token"], "cookies": ["sudir"]}
_USER_TOKEN = {"kind": "user_token", "auth_header": "X-API-Key", "auth_token": "static-token-42"}


def _entry(**overrides) -> dict:
    return {"id": "crm", "url": _URL, **overrides}


def _refusal_and_drop_log(raw: dict, caplog: pytest.LogCaptureFixture) -> tuple[str, str]:
    with pytest.raises(ValidationError) as caught:
        A2aRemoteAgentEntry.model_validate(raw)
    with caplog.at_level(logging.WARNING, logger=_LOGGER):
        assert _coerce_a2a_remote_agents([raw]) == []
    return str(caught.value), " ".join(record.getMessage() for record in caplog.records)


def test_an_a2a_entry_without_auth_is_unauthenticated() -> None:
    entry = A2aRemoteAgentEntry(id="crm", url=_URL)

    assert entry.auth == NoAuth()
    assert entry.auth.kind is EgressAuthKind.NONE
    assert entry.takes_personal_token is False


def test_top_level_auth_header_and_token_are_refused_and_named_without_the_value(caplog) -> None:
    raw = _entry(auth_header="X-API-Key", auth_token="static-token-42")

    refusal, drop_log = _refusal_and_drop_log(raw, caplog)

    assert "crm" in drop_log
    for rendered in (refusal, drop_log):
        assert "auth_header" in rendered
        assert "auth_token" in rendered
        assert "static-token-42" not in rendered


@parametrize_from_dict(
    {
        "none": {"auth": {"kind": "none"}, "personal": False},
        "mtls": {"auth": {"kind": "mtls"}, "personal": False},
        "user_headers": {"auth": _USER_HEADERS, "personal": False},
        "user_token": {"auth": _USER_TOKEN, "personal": True},
    }
)
def test_every_kind_may_address_http(auth, personal) -> None:
    entry = A2aRemoteAgentEntry.model_validate(_entry(url="http://gateway.test/a2a", auth=auth))

    assert entry.auth.kind == auth["kind"]
    assert entry.takes_personal_token is personal


_PROTOCOL_HEADERS = {
    "user_headers A2A-Version": {
        "auth": {"kind": "user_headers", "headers": ["A2A-Version"]},
        "name": "A2A-Version",
    },
    "user_headers X-A2A-Extensions": {
        "auth": {"kind": "user_headers", "headers": ["X-A2A-Extensions"]},
        "name": "X-A2A-Extensions",
    },
    "user_token A2A-Extensions": {
        "auth": {**_USER_TOKEN, "auth_header": "A2A-Extensions"},
        "name": "A2A-Extensions",
    },
}


@parametrize_from_dict(_PROTOCOL_HEADERS)
def test_a2a_protocol_headers_cannot_be_forwarded(auth, name) -> None:
    with pytest.raises(ValidationError, match=f"protected header '{name}'"):
        A2aRemoteAgentEntry.model_validate(_entry(auth=auth))


def test_the_static_token_is_not_in_repr() -> None:
    entry = A2aRemoteAgentEntry.model_validate(_entry(auth=_USER_TOKEN))

    assert "X-API-Key" in repr(entry)
    assert "static-token-42" not in repr(entry)
    assert "static-token-42" not in str(entry)


@parametrize_from_dict(
    {
        "missing": {"raw": {"id": "crm"}, "expected": "url\n  Field required"},
        "empty": {"raw": _entry(url=""), "expected": "A2A url must be an http:// or https:// address"},
        "another scheme": {
            "raw": _entry(url="ftp://agent.example.test/a2a"),
            "expected": "A2A url must be an http:// or https:// address",
        },
    }
)
def test_url_is_required_and_must_be_http_or_https(raw, expected) -> None:
    with pytest.raises(ValidationError, match=expected):
        A2aRemoteAgentEntry.model_validate(raw)


def test_the_auth_field_is_the_one_egress_union() -> None:
    assert A2aRemoteAgentEntry in get_args(EgressEntry)
    assert A2aRemoteAgentEntry.model_fields["auth"].annotation == get_args(EgressAuth)[0]
    assert A2aRemoteAgentEntry.model_fields["auth"].annotation == McpServerEntry.model_fields["auth"].annotation
    assert A2aRemoteAgentEntry.model_validate(_entry(auth=_USER_TOKEN)).auth == UserTokenAuth(
        auth_header="X-API-Key", auth_token="static-token-42"
    )


def test_a_user_token_entry_takes_a_personal_token_with_or_without_a_static_one() -> None:
    static = A2aRemoteAgentEntry.model_validate(_entry(auth=_USER_TOKEN))
    personal_only = A2aRemoteAgentEntry.model_validate(_entry(auth={"kind": "user_token", "auth_header": "X-API-Key"}))
    user_headers = A2aRemoteAgentEntry.model_validate(_entry(auth=_USER_HEADERS))

    assert (static.takes_personal_token, personal_only.takes_personal_token) == (True, True)
    assert user_headers.takes_personal_token is False


def test_connectable_agents_keep_the_first_of_each_id_and_skip_disabled() -> None:
    entries = [
        A2aRemoteAgentEntry(id="hr", url="https://hr.example.test/a2a", enabled=False),
        A2aRemoteAgentEntry(id="crm", url="https://crm-one.example.test/a2a"),
        A2aRemoteAgentEntry(id="", url="https://anonymous.example.test/a2a"),
        A2aRemoteAgentEntry(id=" writer ", url="https://writer.example.test/a2a"),
        A2aRemoteAgentEntry(id="crm", url="https://crm-two.example.test/a2a"),
        A2aRemoteAgentEntry(id="hr", url="https://hr-two.example.test/a2a"),
        A2aRemoteAgentEntry(id="writer", url="https://writer-two.example.test/a2a"),
    ]

    connectable, duplicates = connectable_a2a_agents(entries)

    assert [(entry.id, entry.url) for entry in connectable] == [
        ("crm", "https://crm-one.example.test/a2a"),
        ("writer", "https://writer.example.test/a2a"),
        ("hr", "https://hr-two.example.test/a2a"),
    ]
    assert duplicates == ["crm", "writer"]
    assert entries[3].id == " writer "
