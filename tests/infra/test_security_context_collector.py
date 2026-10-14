from __future__ import annotations

import logging
from collections.abc import Iterable

from infra.security_context.collector import DeclaredParametersCollector
from infra.security_context.declaration import HeaderCollectorDeclaration
from infra.security_context.interface import SecurityParameter
from tests.helpers.parametrize import parametrize_from_dict
from tests.helpers.security_context import declaration_of

REQUEST_HEADERS = [
    ("X-Sudir-Token", "t1"),
    ("Authorization", "Bearer undeclared"),
    ("Cookie", "sudir=abc; theme=dark"),
    ("x-other", "o"),
]


def _named(parameters: Iterable[SecurityParameter]) -> dict[tuple[str, str], str]:
    return {(parameter.key.type.value, parameter.key.name): parameter.value for parameter in parameters}


collected_cases: dict[str, dict[str, object]] = {
    "header names fold to lowercase": {
        "declared_headers": ("X-Sudir-Token", "authorization"),
        "declared_cookies": (),
        "headers": [("x-sudir-token", "t1"), ("AUTHORIZATION", "Bearer b")],
        "expected": {("header", "x-sudir-token"): "t1", ("header", "authorization"): "Bearer b"},
    },
    "a header sent once is kept raw": {
        "declared_headers": ("x-sudir-token",),
        "declared_cookies": (),
        "headers": [("x-sudir-token", " Bearer t1, t2 ")],
        "expected": {("header", "x-sudir-token"): " Bearer t1, t2 "},
    },
    "a repeated header collects nothing": {
        "declared_headers": ("x-sudir-token",),
        "declared_cookies": (),
        "headers": [("x-sudir-token", "t1"), ("X-Sudir-Token", "t2")],
        "expected": {},
    },
    "a header repeated with an empty value collects nothing": {
        "declared_headers": ("x-sudir-token",),
        "declared_cookies": (),
        "headers": [("x-sudir-token", "t1"), ("X-Sudir-Token", "")],
        "expected": {},
    },
    "an empty header value collects nothing": {
        "declared_headers": ("x-sudir-token",),
        "declared_cookies": (),
        "headers": [("x-sudir-token", "")],
        "expected": {},
    },
    "a repeat of one header leaves another declared header collected": {
        "declared_headers": ("x-sudir-token", "authorization"),
        "declared_cookies": (),
        "headers": [("x-sudir-token", "t1"), ("authorization", "Bearer b"), ("x-sudir-token", "t2")],
        "expected": {("header", "authorization"): "Bearer b"},
    },
    "a cookie becomes cookie parameters and never a header": {
        "declared_headers": (),
        "declared_cookies": ("sudir", "theme"),
        "headers": [("Cookie", "sudir=abc; theme=dark")],
        "expected": {("cookie", "sudir"): "abc", ("cookie", "theme"): "dark"},
    },
    "cookie names keep case": {
        "declared_headers": (),
        "declared_cookies": ("Sudir", "sudir"),
        "headers": [("cookie", "Sudir=upper; sudir=lower")],
        "expected": {("cookie", "Sudir"): "upper", ("cookie", "sudir"): "lower"},
    },
    "a quoted cookie value is kept raw and claims the name": {
        "declared_headers": (),
        "declared_cookies": ("sudir",),
        "headers": [("cookie", 'sudir="a b"; sudir=later')],
        "expected": {("cookie", "sudir"): '"a b"'},
    },
    "the first occurrence wins across cookie headers": {
        "declared_headers": (),
        "declared_cookies": ("sudir", "x"),
        "headers": [("cookie", "sudir=first; sudir=second"), ("Cookie", "x=1; sudir=third")],
        "expected": {("cookie", "sudir"): "first", ("cookie", "x"): "1"},
    },
    "an empty first occurrence claims the name": {
        "declared_headers": (),
        "declared_cookies": ("sudir",),
        "headers": [("cookie", "sudir=; sudir=later")],
        "expected": {},
    },
    "an empty first occurrence claims the name across cookie headers": {
        "declared_headers": (),
        "declared_cookies": ("sudir", "theme"),
        "headers": [("cookie", "sudir="), ("cookie", "sudir=later; theme=dark")],
        "expected": {("cookie", "theme"): "dark"},
    },
    "a chunk without a separator is skipped and claims nothing": {
        "declared_headers": (),
        "declared_cookies": ("sudir",),
        "headers": [("cookie", "sudir; sudir=later")],
        "expected": {("cookie", "sudir"): "later"},
    },
    "an empty name is skipped": {
        "declared_headers": (),
        "declared_cookies": ("theme",),
        "headers": [("cookie", "=orphan; theme=dark")],
        "expected": {("cookie", "theme"): "dark"},
    },
    "a value containing a separator is kept whole": {
        "declared_headers": (),
        "declared_cookies": ("q",),
        "headers": [("cookie", "q=a=b")],
        "expected": {("cookie", "q"): "a=b"},
    },
    "only spaces and tabs are trimmed": {
        "declared_headers": (),
        "declared_cookies": ("sudir", "theme", "x"),
        "headers": [("cookie", " \tsudir=abc\t ; theme=dark\xa0;\nx=1")],
        "expected": {("cookie", "sudir"): "abc", ("cookie", "theme"): "dark\xa0"},
    },
    "no headers collect nothing": {
        "declared_headers": ("x-sudir-token",),
        "declared_cookies": ("sudir",),
        "headers": [],
        "expected": {},
    },
}


@parametrize_from_dict(collected_cases)
def test_declared_parameters_collector(
    declared_headers: tuple[str, ...],
    declared_cookies: tuple[str, ...],
    headers: list[tuple[str, str]],
    expected: dict[tuple[str, str], str],
) -> None:
    collector = DeclaredParametersCollector(declaration_of(headers=declared_headers, cookies=declared_cookies))

    assert _named(collector.collect(headers)) == expected


def test_a_repeated_declared_header_is_warned_by_name_without_its_values(caplog) -> None:
    collector = DeclaredParametersCollector(declaration_of(headers=("X-Sudir-Token",)))

    with caplog.at_level(logging.WARNING, logger="infra.security_context.collector"):
        collected = collector.collect([("X-Sudir-Token", "t1-secret"), ("x-sudir-token", "t2-secret")])

    assert collected == ()
    (record,) = caplog.records
    assert record.levelno == logging.WARNING
    assert "x-sudir-token" in record.getMessage()
    assert "t1-secret" not in record.getMessage()
    assert "t2-secret" not in record.getMessage()


def test_an_undeclared_repeated_header_is_not_warned(caplog) -> None:
    collector = DeclaredParametersCollector(declaration_of(headers=("X-Sudir-Token",)))

    with caplog.at_level(logging.WARNING, logger="infra.security_context.collector"):
        collector.collect([("Accept", "a"), ("accept", "b"), ("X-Sudir-Token", "t1")])

    assert caplog.records == []


def test_an_undeclared_header_or_cookie_is_not_collected() -> None:
    collector = DeclaredParametersCollector(declaration_of(headers=("X-Sudir-Token",), cookies=("sudir",)))

    collected = collector.collect([*REQUEST_HEADERS, ("Cookie", "session=s1; sudir=later")])

    assert _named(collected) == {("header", "x-sudir-token"): "t1", ("cookie", "sudir"): "abc"}


def test_parameters_are_collected_in_declaration_order_with_their_declared_ttl() -> None:
    declaration = HeaderCollectorDeclaration.model_validate(
        {
            "parameters": [
                {"type": "cookie", "name": "theme", "ttl_sec": 60},
                {"type": "header", "name": "X-Sudir-Token", "ttl_sec": 3600},
                {"type": "cookie", "name": "sudir", "ttl_sec": 600},
            ]
        }
    )

    collected = DeclaredParametersCollector(declaration).collect(REQUEST_HEADERS)

    assert [(parameter.key.label, parameter.value, parameter.ttl_sec) for parameter in collected] == [
        ("cookie:theme", "dark", 60),
        ("header:x-sudir-token", "t1", 3600),
        ("cookie:sudir", "abc", 600),
    ]


def test_a_claimed_empty_cookie_still_hides_a_later_value_of_a_declared_name() -> None:
    collector = DeclaredParametersCollector(declaration_of(cookies=("sudir",)))

    collected = collector.collect([("Cookie", "other=1; sudir="), ("Cookie", "sudir=later")])

    assert collected == ()


def test_a_narrower_declaration_collects_only_its_names() -> None:
    wide = DeclaredParametersCollector(
        declaration_of(headers=("X-Sudir-Token", "Authorization", "X-Other"), cookies=("sudir", "theme"))
    )
    narrow = DeclaredParametersCollector(declaration_of(headers=("X-Sudir-Token",), cookies=("sudir",)))

    assert _named(wide.collect(REQUEST_HEADERS)) == {
        ("header", "x-sudir-token"): "t1",
        ("header", "authorization"): "Bearer undeclared",
        ("header", "x-other"): "o",
        ("cookie", "sudir"): "abc",
        ("cookie", "theme"): "dark",
    }
    assert _named(narrow.collect(REQUEST_HEADERS)) == {("header", "x-sudir-token"): "t1", ("cookie", "sudir"): "abc"}
