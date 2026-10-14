from __future__ import annotations

import asyncio
from collections.abc import Mapping

import pytest

from infra.security_context.interface import (
    ISecurityContextStore,
    SecurityContext,
    SecurityParameterKey,
    SecurityParameterType,
)
from new_core.identity import OwnerScope, SurfaceId
from ouroboros.egress.auth.authenticators import UserHeadersAuthenticator
from ouroboros.egress.auth.interfaces import EgressAuthRefused, IEgressAuthenticator
from ouroboros.storage.settings.schema import UserHeadersAuth
from tests.helpers.parametrize import parametrize_from_dict
from tests.helpers.security_context import FixedSecurityContextStore, context_of

TOKEN_VALUE = "t1-very-secret"
COOKIE_VALUE = "abc-cookie-secret"
AUTHORIZATION_VALUE = "Bearer undeclared-secret"
STORED_VALUES = (TOKEN_VALUE, COOKIE_VALUE, AUTHORIZATION_VALUE, "dark")


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


def _scope(surface: SurfaceId = SurfaceId.BROWSER, *, owner: str = "o", agent: str = "a") -> OwnerScope:
    return OwnerScope(owner_id=owner, agent_id=agent, surface_id=surface, chat_id="1")


def _context() -> SecurityContext:
    return context_of(
        headers={"x-sudir-token": TOKEN_VALUE, "authorization": AUTHORIZATION_VALUE},
        cookies={"sudir": COOKIE_VALUE, "theme": "dark"},
    )


def _store(*, error: Exception | None = None) -> FixedSecurityContextStore:
    return FixedSecurityContextStore({_scope().security_token: _context()}, error=error)


def _stored(
    *, headers: Mapping[str, str] | None = None, cookies: Mapping[str, str] | None = None
) -> FixedSecurityContextStore:
    return FixedSecurityContextStore({_scope().security_token: context_of(headers=headers, cookies=cookies)})


def _user_headers(store: ISecurityContextStore, headers=("X-Sudir-Token",), cookies=()) -> UserHeadersAuthenticator:
    return UserHeadersAuthenticator(UserHeadersAuth(headers=list(headers), cookies=list(cookies)), store)


def _refusal(authenticator: IEgressAuthenticator, scope: OwnerScope, fragments=STORED_VALUES) -> str:
    with pytest.raises(EgressAuthRefused) as caught:
        _run(authenticator.request_headers(scope))
    reason = caught.value.reason
    for fragment in fragments:
        assert fragment not in reason
    return reason


def test_user_headers_forward_their_declared_headers_and_the_cookie() -> None:
    assert _user_headers(_store(), headers=("X-Sudir-Token", "X-Other")).forwarded_headers == frozenset(
        {"X-Sudir-Token", "X-Other", "Cookie"}
    )


def test_user_headers_forward_only_the_declared_header() -> None:
    assert _run(_user_headers(_store()).request_headers(_scope())) == {"X-Sudir-Token": TOKEN_VALUE}


def test_user_headers_build_the_cookie_from_declared_cookies_only() -> None:
    headers = _run(_user_headers(_store(), headers=(), cookies=("sudir",)).request_headers(_scope()))

    assert headers == {"Cookie": f"sudir={COOKIE_VALUE}"}


def test_user_headers_request_exactly_the_declared_keys() -> None:
    store = _store()

    _run(_user_headers(store, headers=("X-Sudir-Token",), cookies=("sudir", "theme")).request_headers(_scope()))

    assert store.loads == [
        (
            _scope().security_token,
            frozenset(
                {
                    SecurityParameterKey(SecurityParameterType.HEADER, "x-sudir-token"),
                    SecurityParameterKey(SecurityParameterType.COOKIE, "sudir"),
                    SecurityParameterKey(SecurityParameterType.COOKIE, "theme"),
                }
            ),
        )
    ]


def test_declared_cookies_are_joined_in_declared_order() -> None:
    headers = _run(_user_headers(_store(), headers=(), cookies=("theme", "sudir")).request_headers(_scope()))

    assert headers == {"Cookie": f"theme=dark; sudir={COOKIE_VALUE}"}


forwarded_as_stored_cases = {
    "a header value with a comma": {
        "stored": {"headers": {"x-sudir-token": "t1, t2"}},
        "declared": {"headers": ("X-Sudir-Token",)},
        "expected": {"X-Sudir-Token": "t1, t2"},
    },
    "a quoted cookie with an inner space": {
        "stored": {"cookies": {"sudir": '"a b"'}},
        "declared": {"headers": (), "cookies": ("sudir",)},
        "expected": {"Cookie": 'sudir="a b"'},
    },
    "a header value with a tab": {
        "stored": {"headers": {"x-sudir-token": "Bearer\tt1"}},
        "declared": {"headers": ("X-Sudir-Token",)},
        "expected": {"X-Sudir-Token": "Bearer\tt1"},
    },
}


@parametrize_from_dict(forwarded_as_stored_cases)
def test_a_stored_value_is_forwarded_as_stored(stored, declared, expected) -> None:
    headers = _run(_user_headers(_stored(**stored), **declared).request_headers(_scope()))

    assert headers == expected


refused_value_cases = {
    "a header holding CR LF": {
        "stored": {"headers": {"x-sudir-token": "tok\r\nX-Injected: yes"}},
        "declared": {"headers": ("X-Sudir-Token",)},
        "fragments": ("tok", "Injected", "\\r"),
    },
    "a cookie holding CR LF": {
        "stored": {"cookies": {"sudir": "tok\r\nX-Injected: yes"}},
        "declared": {"headers": (), "cookies": ("sudir",)},
        "fragments": ("tok", "Injected", "\\r"),
    },
    "a header holding a latin-1 byte": {
        "stored": {"headers": {"x-sudir-token": "caf\xe9"}},
        "declared": {"headers": ("X-Sudir-Token",)},
        "fragments": ("caf", "\\xe9"),
    },
    "a cookie ending in a latin-1 space": {
        "stored": {"cookies": {"sudir": "tok\xa0", "x": "1"}},
        "declared": {"headers": (), "cookies": ("sudir",)},
        "fragments": ("tok", "\\xa0"),
    },
    "a header with a leading space": {
        "stored": {"headers": {"x-sudir-token": " tok"}},
        "declared": {"headers": ("X-Sudir-Token",)},
        "fragments": ("tok",),
    },
    "a header with a trailing tab": {
        "stored": {"headers": {"x-sudir-token": "tok\t"}},
        "declared": {"headers": ("X-Sudir-Token",)},
        "fragments": ("tok", "\\t"),
    },
}


@parametrize_from_dict(refused_value_cases)
def test_a_value_a_request_header_cannot_hold_is_refused_without_rendering_it(stored, declared, fragments) -> None:
    reason = _refusal(_user_headers(_stored(**stored), **declared), _scope(), fragments)

    assert "cannot hold" in reason


missing_cases = {
    "an empty cookie": {
        "stored": {"cookies": {"sudir": ""}},
        "declared": {"headers": (), "cookies": ("sudir",)},
    },
    "an empty header value": {
        "stored": {"headers": {"x-sudir-token": ""}},
        "declared": {"headers": ("X-Sudir-Token",)},
    },
}


@parametrize_from_dict(missing_cases)
def test_an_empty_declared_credential_is_refused_as_missing(stored, declared) -> None:
    reason = _refusal(_user_headers(_stored(**stored), **declared), _scope())

    assert "lack" in reason


def test_another_chat_of_the_same_owner_and_agent_resolves_the_context() -> None:
    scope = OwnerScope(owner_id="o", agent_id="a", surface_id=SurfaceId.BROWSER, chat_id="another-chat")

    assert _run(_user_headers(_store()).request_headers(scope)) == {"X-Sudir-Token": TOKEN_VALUE}


def test_another_surface_of_the_same_owner_and_agent_does_not_resolve() -> None:
    a2a = _scope(SurfaceId.A2A)
    store = FixedSecurityContextStore({_scope().security_token: _context(), a2a.security_token: _context()})

    reason = _refusal(_user_headers(store), a2a)

    assert "surface a2a" in reason
    assert store.loads == []


def test_a_token_with_no_parameters_is_refused_by_the_first_declared_name() -> None:
    reason = _refusal(_user_headers(_store(), cookies=("sudir",)), _scope(agent="b"))

    assert reason == "the user credentials lack the header 'X-Sudir-Token'"


@pytest.mark.parametrize("surface", [SurfaceId.A2A, SurfaceId.SKILL_BRIDGE, SurfaceId.DIALOGS])
def test_a_surface_that_records_no_context_is_refused(surface) -> None:
    store = _store()

    reason = _refusal(_user_headers(store), _scope(surface))

    assert str(surface.value) in reason
    assert store.loads == []


def test_a_missing_declared_header_is_refused_by_name() -> None:
    assert "'X-Other'" in _refusal(_user_headers(_store(), headers=("X-Sudir-Token", "X-Other")), _scope())


def test_a_missing_declared_cookie_is_refused_by_name() -> None:
    assert "'absent'" in _refusal(_user_headers(_store(), headers=(), cookies=("sudir", "absent")), _scope())


def test_a_store_error_is_refused() -> None:
    store = _store(error=RuntimeError(f"backend down {TOKEN_VALUE}"))

    assert "unavailable" in _refusal(_user_headers(store), _scope())
