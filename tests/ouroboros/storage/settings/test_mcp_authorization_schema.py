from __future__ import annotations

import logging
import subprocess
import sys
from pathlib import Path
from typing import get_args

import pytest
from pydantic import ValidationError

from ouroboros.observability.aef_tracing import AGENT_HEADER_NAME, TRACE_HEADER_NAME
from ouroboros.storage.settings.schema import (
    EgressAuth,
    EgressAuthBase,
    EgressAuthKind,
    McpServerEntry,
    MTLSAuth,
    NoAuth,
    UserHeadersAuth,
    UserTokenAuth,
    _coerce_mcp_servers,
)
from tests.helpers.parametrize import parametrize_from_dict

_LOGGER = "ouroboros.storage.settings.schema"
_REPO_ROOT = Path(__file__).resolve().parents[4]
_USER_HEADERS = {"kind": "user_headers", "headers": ["X-Sudir-Token"], "cookies": ["sudir"]}
_USER_TOKEN = {"kind": "user_token", "auth_header": "X-API-Key", "auth_token": "static-token-42"}


def _entry(**overrides) -> dict:
    return {"id": "sudir-mcp", "url": "https://mcp.example.test/mcp", "auth": _USER_HEADERS, **overrides}


def _refusal_rendering(raw: dict, caplog: pytest.LogCaptureFixture) -> str:
    with pytest.raises(ValidationError) as caught:
        McpServerEntry.model_validate(raw)
    with caplog.at_level(logging.WARNING, logger=_LOGGER):
        assert _coerce_mcp_servers([raw]) == []
    return " ".join([str(caught.value), *(record.getMessage() for record in caplog.records)])


def test_an_entry_without_auth_declares_no_authorization() -> None:
    entry = McpServerEntry(id="s", url="https://e.example/mcp")

    assert entry.auth == NoAuth()
    assert entry.auth.kind is EgressAuthKind.NONE


def test_the_kinds_are_the_egress_kinds() -> None:
    members = get_args(get_args(EgressAuth)[0])

    assert [kind.value for kind in EgressAuthKind] == ["none", "user_headers", "user_token", "mtls"]
    assert all(issubclass(member, EgressAuthBase) for member in members)
    assert McpServerEntry.model_fields["auth"].annotation == get_args(EgressAuth)[0]


def test_the_retired_tls_key_is_refused_as_unknown() -> None:
    with pytest.raises(ValidationError, match="tls"):
        McpServerEntry.model_validate({"id": "s", "url": "https://e.example/mcp", "tls": "egress_mtls"})


def test_a_top_level_static_token_is_refused_and_named_on_the_dev_path_without_its_value(caplog) -> None:
    raw = {"id": "sudir-mcp", "url": "https://e.example/mcp", "auth_header": "X-Key", "auth_token": "static-token-42"}

    rendered = _refusal_rendering(raw, caplog)

    assert "sudir-mcp" in rendered
    assert "auth_header" in rendered
    assert "auth_token" in rendered
    assert "static-token-42" not in rendered


def test_user_headers_naming_nothing_is_refused() -> None:
    with pytest.raises(ValidationError, match="at least one header or cookie"):
        McpServerEntry.model_validate(_entry(auth={"kind": "user_headers"}))


def test_an_unknown_key_inside_auth_is_refused_and_named_on_the_dev_path(caplog) -> None:
    raw = _entry(auth={"kind": "user_headers", "cookie": ["sudir"]})

    with pytest.raises(ValidationError, match="cookie"):
        McpServerEntry.model_validate(raw)

    with caplog.at_level(logging.WARNING, logger=_LOGGER):
        assert _coerce_mcp_servers([raw]) == []
    rendered = " ".join(record.getMessage() for record in caplog.records)
    assert "sudir-mcp" in rendered
    assert "auth.user_headers.cookie" in rendered


@pytest.mark.parametrize(
    "auth",
    [
        {"kind": "user_headers", "headers": ["X-Sudir-Token", "x-sudir-token"]},
        {"kind": "user_headers", "cookies": ["sudir", "SUDIR"]},
    ],
)
def test_duplicate_names_are_refused_regardless_of_case(auth) -> None:
    with pytest.raises(ValidationError, match="more than once"):
        McpServerEntry.model_validate(_entry(auth=auth))


_NON_TOKEN_NAMES = {
    "empty": {"name": ""},
    "blank": {"name": "   "},
    "space inside": {"name": "X Sudir"},
    "colon inside": {"name": "x-sudir:token"},
}


@parametrize_from_dict(_NON_TOKEN_NAMES)
def test_user_headers_names_that_are_not_http_tokens_are_refused(name) -> None:
    with pytest.raises(ValidationError, match="HTTP token"):
        UserHeadersAuth(headers=[name])


@parametrize_from_dict(_NON_TOKEN_NAMES)
def test_a_user_token_header_that_is_not_an_http_token_is_refused(name) -> None:
    with pytest.raises(ValidationError, match="HTTP token"):
        UserTokenAuth(auth_header=name, auth_token="static-token-42")


def test_user_token_without_auth_header_is_refused() -> None:
    with pytest.raises(ValidationError, match="auth_header"):
        McpServerEntry.model_validate(_entry(auth={"kind": "user_token", "auth_token": "static-token-42"}))


_PROTECTED_NAMES = {
    name: {"name": name}
    for name in (
        "Host",
        "content-length",
        "Content-Type",
        "transfer-encoding",
        "connection",
        "keep-alive",
        "proxy-connection",
        "Proxy-Authorization",
        "te",
        "trailer",
        "upgrade",
        "expect",
        "accept",
        "Cookie",
        "Last-Event-ID",
        TRACE_HEADER_NAME,
        AGENT_HEADER_NAME,
        "Mcp-Session-Id",
        "mcp-protocol-version",
        "Sec-WebSocket-Key",
        "A2A-Version",
        "a2a-extensions",
        "X-A2A-Extensions",
    )
}


@parametrize_from_dict(_PROTECTED_NAMES)
def test_a_protected_header_name_is_refused_for_user_headers(name) -> None:
    with pytest.raises(ValidationError, match="protected header"):
        McpServerEntry.model_validate(_entry(auth={"kind": "user_headers", "headers": [name]}))


@parametrize_from_dict(_PROTECTED_NAMES)
def test_a_protected_header_name_is_refused_for_user_token(name) -> None:
    with pytest.raises(ValidationError, match="protected header"):
        McpServerEntry.model_validate(_entry(auth={**_USER_TOKEN, "auth_header": name}))


def test_a_cookie_named_like_a_protected_header_is_a_cookie() -> None:
    entry = McpServerEntry.model_validate(_entry(auth={"kind": "user_headers", "cookies": ["host"]}))

    assert entry.auth.kind == "user_headers"


def test_an_empty_static_token_is_no_static_token() -> None:
    entry = McpServerEntry.model_validate(_entry(auth={**_USER_TOKEN, "auth_token": ""}))

    assert entry.auth == UserTokenAuth(auth_header="X-API-Key", auth_token=None)


def test_a_static_token_carrying_crlf_is_refused_without_rendering_it(caplog) -> None:
    raw = _entry(auth={**_USER_TOKEN, "auth_token": "static-token-42\r\nX-Injected: crlf-tail-77"})

    rendered = _refusal_rendering(raw, caplog)

    assert "sudir-mcp" in rendered
    assert "auth_token" in rendered
    for secret in ("static-token-42", "crlf-tail-77"):
        assert secret not in rendered


def test_user_headers_over_sse_is_refused() -> None:
    with pytest.raises(ValidationError, match="transport 'sse' is refused"):
        McpServerEntry.model_validate(_entry(transport="sse"))


def test_user_token_over_sse_without_a_static_token_is_refused() -> None:
    with pytest.raises(ValidationError, match="requires a static auth_token"):
        McpServerEntry.model_validate(_entry(transport="sse", auth={"kind": "user_token", "auth_header": "X-API-Key"}))


def test_user_token_over_sse_with_a_static_token_is_accepted() -> None:
    entry = McpServerEntry.model_validate(_entry(transport="sse", auth=_USER_TOKEN))

    assert entry.auth == UserTokenAuth(auth_header="X-API-Key", auth_token="static-token-42")


def test_mtls_over_sse_is_accepted() -> None:
    entry = McpServerEntry.model_validate(_entry(transport="sse", url="http://gateway.test/mcp", auth={"kind": "mtls"}))

    assert entry.auth == MTLSAuth()


_EVERY_KIND = {
    "none": {"auth": {"kind": "none"}},
    "mtls": {"auth": {"kind": "mtls"}},
    "user_headers": {"auth": _USER_HEADERS},
    "user_token": {"auth": _USER_TOKEN},
}


@parametrize_from_dict(_EVERY_KIND)
def test_every_kind_may_address_plaintext(auth) -> None:
    entry = McpServerEntry.model_validate(_entry(url="http://gateway.test/mcp", auth=auth))

    assert entry.auth.kind == auth["kind"]


_EVERY_KIND = {
    "none": {"entry": _entry(auth={"kind": "none"})},
    "user_headers": {"entry": _entry()},
    "user_token static": {"entry": _entry(auth=_USER_TOKEN)},
    "user_token personal only": {"entry": _entry(auth={"kind": "user_token", "auth_header": "Authorization"})},
    "mtls": {"entry": _entry(url="http://gateway.test/mcp", auth={"kind": "mtls"})},
}


@parametrize_from_dict(_EVERY_KIND)
def test_every_kind_survives_a_serialize_parse_round_trip(entry) -> None:
    parsed = McpServerEntry.model_validate(entry)

    assert McpServerEntry.model_validate(parsed.model_dump(mode="json")) == parsed
    assert _coerce_mcp_servers([parsed.model_dump(mode="json")]) == [parsed]


def test_the_entry_repr_omits_the_static_token() -> None:
    entry = McpServerEntry.model_validate(_entry(auth=_USER_TOKEN))

    assert "X-API-Key" in repr(entry)
    assert "static-token-42" not in repr(entry)
    assert "static-token-42" not in str(entry)


def test_the_schema_imports_in_a_fresh_interpreter_without_a_client_package() -> None:
    code = (
        "import sys\n"
        "import ouroboros.storage.settings.schema\n"
        "loaded = sorted(name for name in sys.modules if name.startswith(('ouroboros.mcp', 'ouroboros.egress')))\n"
        "assert not loaded, loaded\n"
    )
    proc = subprocess.run([sys.executable, "-c", code], cwd=_REPO_ROOT, capture_output=True, text=True, check=False)

    assert proc.returncode == 0, proc.stderr
