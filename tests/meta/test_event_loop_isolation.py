"""Behavioral regression for the pytest event-loop isolation hooks."""

from __future__ import annotations

import pathlib


def test_event_loop_hooks_isolate_subsequent_tests_and_finalizers(pytester) -> None:
    pytester.makepyfile(
        test_ordered="""
        import asyncio
        import pytest

        def test_01_contaminates_current_loop():
            asyncio.run(asyncio.sleep(0))

        def test_02_next_test_gets_an_open_loop():
            loop = asyncio.get_event_loop()
            assert not loop.is_closed()
            loop.run_until_complete(asyncio.sleep(0))

        @pytest.fixture
        def finalizer_reads_loop(request):
            def finalize():
                loop = asyncio.get_event_loop()
                assert not loop.is_closed()
            request.addfinalizer(finalize)

        def test_03_finalizer_gets_an_open_loop(finalizer_reads_loop):
            asyncio.run(asyncio.sleep(0))
        """
    )

    without_hooks = pytester.runpytest_subprocess("-q")
    without_hooks.assert_outcomes(passed=2, failed=1, errors=1)

    project_conftest = pathlib.Path(__file__).resolve().parents[1] / "conftest.py"
    project_root = project_conftest.parent.parent
    pytester.makeconftest(
        f"""
        import importlib.util
        import sys

        sys.path.insert(0, {str(project_root)!r})
        spec = importlib.util.spec_from_file_location("_ouroboros_test_hooks", {str(project_conftest)!r})
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        pytest_runtest_call = module.pytest_runtest_call
        pytest_runtest_teardown = module.pytest_runtest_teardown
        """
    )

    with_hooks = pytester.runpytest_subprocess("-q")
    with_hooks.assert_outcomes(passed=3)
