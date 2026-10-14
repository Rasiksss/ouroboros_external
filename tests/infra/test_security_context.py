from __future__ import annotations

import pathlib
from typing import cast

import pytest

from infra.security_context import SecurityContextCollectorFactory, SecurityContextFactory
from infra.security_context.collector import DeclaredParametersCollector
from infra.security_context.interface import SecurityParameter, SecurityParameterKey, SecurityParameterType
from tests.helpers.parametrize import parametrize_from_dict
from tests.helpers.security_context import InMemorySecurityContextStore, context_of, declaration_of

SECRET = "Bearer s3cr3t-value"
AUTHORIZATION = SecurityParameterKey(SecurityParameterType.HEADER, "authorization")
SUDIR_TOKEN = SecurityParameterKey(SecurityParameterType.HEADER, "x-sudir-token")
SUDIR = SecurityParameterKey(SecurityParameterType.COOKIE, "sudir")


def test_header_names_fold_to_lowercase_and_cookie_names_keep_case() -> None:
    header = SecurityParameterKey(SecurityParameterType.HEADER, "X-Sudir-Token")
    cookie = SecurityParameterKey(SecurityParameterType.COOKIE, "Sudir")

    assert header.name == "x-sudir-token"
    assert header == SUDIR_TOKEN
    assert cookie.name == "Sudir"
    assert cookie != SUDIR


def test_a_header_and_a_cookie_of_one_name_are_different_keys() -> None:
    header = SecurityParameterKey(SecurityParameterType.HEADER, "sudir")
    context = context_of(headers={"sudir": "from-header"}, cookies={"sudir": "from-cookie"})

    assert header != SUDIR
    assert context.value(header) == "from-header"
    assert context.value(SUDIR) == "from-cookie"


def test_a_parameter_type_given_by_its_value_is_the_enum_member() -> None:
    key = SecurityParameterKey(cast(SecurityParameterType, "header"), "Authorization")

    assert key.type is SecurityParameterType.HEADER
    assert key == AUTHORIZATION


def test_an_unknown_parameter_type_is_refused() -> None:
    with pytest.raises(ValueError, match="certificate"):
        SecurityParameterKey(cast(SecurityParameterType, "certificate"), "authorization")


def test_a_parameter_renders_type_name_and_ttl_but_never_its_value() -> None:
    parameter = SecurityParameter(SecurityParameterKey(SecurityParameterType.HEADER, "X-Sudir-Token"), SECRET, 3600)

    for rendered in (repr(parameter), str(parameter), f"{parameter}"):
        assert rendered == "SecurityParameter(header:x-sudir-token, ttl_sec=3600)"


@parametrize_from_dict(
    {
        "zero": {"ttl_sec": 0},
        "negative": {"ttl_sec": -1},
        "true": {"ttl_sec": True},
        "false": {"ttl_sec": False},
    }
)
def test_a_non_positive_or_boolean_ttl_is_refused(ttl_sec: int) -> None:
    with pytest.raises(ValueError, match="header:x-sudir-token") as refused:
        SecurityParameter(SUDIR_TOKEN, SECRET, ttl_sec)

    assert SECRET not in str(refused.value)


def test_the_representation_of_a_context_names_type_and_name_only() -> None:
    context = context_of(headers={"Authorization": SECRET}, cookies={"Sudir": "abc-cookie"})

    for rendered in (repr(context), str(context), f"{context}"):
        assert rendered == "SecurityContext(parameters=[header:authorization, cookie:Sudir])"


def test_an_uninstalled_holder_raises() -> None:
    holder = SecurityContextFactory()

    with pytest.raises(RuntimeError, match=r"security_context\.initialize\(\)"):
        _ = holder.store


def test_a_holder_offers_no_reset_and_a_later_initialize_replaces_the_store() -> None:
    holder = SecurityContextFactory()
    first = InMemorySecurityContextStore()
    second = InMemorySecurityContextStore()

    holder.initialize(first)
    holder.initialize(second)

    assert not hasattr(holder, "reset_for_tests")
    assert holder.store is second


def test_an_uninstalled_collector_holder_raises() -> None:
    holder = SecurityContextCollectorFactory()

    with pytest.raises(RuntimeError, match=r"security_context_collector\.initialize\(\)"):
        _ = holder.collector


def test_a_collector_holder_offers_no_reset_and_a_later_initialize_replaces_the_collector() -> None:
    holder = SecurityContextCollectorFactory()
    first = DeclaredParametersCollector(declaration_of(headers=("X-Sudir-Token",)))
    second = DeclaredParametersCollector(declaration_of(cookies=("sudir",)))

    holder.initialize(first)
    holder.initialize(second)

    assert not hasattr(holder, "reset_for_tests")
    assert holder.collector is second


def test_every_test_starts_on_a_fresh_empty_store_and_a_fresh_collector(pytester) -> None:
    project_conftest = pathlib.Path(__file__).resolve().parents[1] / "conftest.py"
    pytester.makeconftest(
        f"""
        import importlib.util
        import sys

        sys.path.insert(0, {str(project_conftest.parent.parent)!r})
        spec = importlib.util.spec_from_file_location("_ouroboros_fixtures", {str(project_conftest)!r})
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        _reset_security_context = module._reset_security_context
        """
    )
    pytester.makepyfile(
        test_ordered="""
        import asyncio

        from infra.security_context import security_context, security_context_collector
        from infra.security_context.collector import DeclaredParametersCollector
        from infra.security_context.interface import SecurityParameterKey, SecurityParameterType
        from tests.helpers.security_context import InMemorySecurityContextStore

        TOKEN = "3f1c0e7a9b2d4c58a6e1f0b7c2d9e4a1"
        SUDIR_TOKEN = SecurityParameterKey(SecurityParameterType.HEADER, "x-sudir-token")
        SEEN = []

        def test_01_saves():
            store = security_context.store
            collector = security_context_collector.collector
            SEEN.append((store, collector))
            asyncio.run(store.save(TOKEN, collector.collect([("X-Sudir-Token", "secret")])))
            assert asyncio.run(store.load(TOKEN, {SUDIR_TOKEN})).value(SUDIR_TOKEN) == "secret"

        def test_02_starts_fresh():
            (store, collector), = SEEN
            assert isinstance(security_context.store, InMemorySecurityContextStore)
            assert isinstance(security_context_collector.collector, DeclaredParametersCollector)
            assert store is not security_context.store
            assert collector is not security_context_collector.collector
            assert asyncio.run(security_context.store.load(TOKEN, {SUDIR_TOKEN})).parameters == {}
        """
    )

    pytester.runpytest_subprocess("-q", "-p", "no:xdist", "-p", "no:cacheprovider").assert_outcomes(passed=2)
