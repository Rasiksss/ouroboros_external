from __future__ import annotations

import pytest
from pydantic import ValidationError

from infra.security_context.declaration import HeaderCollectorDeclaration
from infra.security_context.interface import SecurityParameterKey, SecurityParameterType
from tests.helpers.parametrize import parametrize_from_dict


def _declared(*parameters: dict[str, object]) -> dict[str, object]:
    return {"parameters": list(parameters)}


def _header(name: str, ttl_sec: object = 3600) -> dict[str, object]:
    return {"type": "header", "name": name, "ttl_sec": ttl_sec}


def _cookie(name: str, ttl_sec: object = 3600) -> dict[str, object]:
    return {"type": "cookie", "name": name, "ttl_sec": ttl_sec}


def test_a_valid_mapping_parses_into_keys_and_ttls() -> None:
    declaration = HeaderCollectorDeclaration.model_validate(_declared(_header("X-Sudir-Token"), _cookie("Sudir", 60)))

    assert [(parameter.key, parameter.ttl_sec) for parameter in declaration.parameters] == [
        (SecurityParameterKey(SecurityParameterType.HEADER, "x-sudir-token"), 3600),
        (SecurityParameterKey(SecurityParameterType.COOKIE, "Sudir"), 60),
    ]


def test_an_empty_parameter_list_is_accepted() -> None:
    assert HeaderCollectorDeclaration.model_validate({"parameters": []}).parameters == ()


refused_cases: dict[str, dict[str, object]] = {
    "unknown type": {
        "raw": _declared({"type": "certificate", "name": "x", "ttl_sec": 1}),
        "match": "'header' or 'cookie'",
    },
    "unknown key": {
        "raw": _declared({"type": "header", "name": "x", "ttl_sec": 1, "ttl": 1}),
        "match": "Extra inputs are not permitted",
    },
    "name with a space": {"raw": _declared(_header("X Sudir")), "match": "HTTP token"},
    "name with a trailing newline": {"raw": _declared(_header("x-sudir-token\n")), "match": "HTTP token"},
    "empty name": {"raw": _declared(_cookie("")), "match": "HTTP token"},
    "zero ttl": {"raw": _declared(_header("x", 0)), "match": "greater than 0"},
    "negative ttl": {"raw": _declared(_header("x", -1)), "match": "greater than 0"},
    "boolean ttl": {"raw": _declared(_header("x", True)), "match": "valid integer"},
    "textual ttl": {"raw": _declared(_header("x", "3600")), "match": "valid integer"},
    "fractional ttl": {"raw": _declared(_header("x", 1.5)), "match": "valid integer"},
    "missing ttl": {"raw": _declared({"type": "header", "name": "x"}), "match": "Field required"},
    "duplicate header ignoring case": {
        "raw": _declared(_header("X-Sudir-Token"), _header("x-sudir-token", 60)),
        "match": "header:x-sudir-token is declared more than once",
    },
    "duplicate cookie": {
        "raw": _declared(_cookie("sudir"), _cookie("sudir", 60)),
        "match": "cookie:sudir is declared more than once",
    },
    "cookie declared as a header": {
        "raw": _declared(_header("Cookie")),
        "match": "declare cookies with type cookie",
    },
    "missing parameters": {"raw": {}, "match": "Field required"},
    "none": {"raw": None, "match": "valid dictionary"},
}


@parametrize_from_dict(refused_cases)
def test_an_invalid_declaration_is_refused(raw: object, match: str) -> None:
    with pytest.raises(ValidationError, match=match):
        HeaderCollectorDeclaration.model_validate(raw)


allowed_cases: dict[str, dict[str, object]] = {
    "cookies differing in case": {
        "raw": _declared(_cookie("Sudir"), _cookie("sudir")),
        "keys": [
            SecurityParameterKey(SecurityParameterType.COOKIE, "Sudir"),
            SecurityParameterKey(SecurityParameterType.COOKIE, "sudir"),
        ],
    },
    "a header and a cookie of one name": {
        "raw": _declared(_header("sudir"), _cookie("sudir")),
        "keys": [
            SecurityParameterKey(SecurityParameterType.HEADER, "sudir"),
            SecurityParameterKey(SecurityParameterType.COOKIE, "sudir"),
        ],
    },
}


@parametrize_from_dict(allowed_cases)
def test_distinct_keys_of_one_name_are_accepted(raw: object, keys: list[SecurityParameterKey]) -> None:
    declaration = HeaderCollectorDeclaration.model_validate(raw)

    assert [parameter.key for parameter in declaration.parameters] == keys
