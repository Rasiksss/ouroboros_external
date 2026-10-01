from __future__ import annotations

import importlib
import sys
from contextlib import ExitStack
from unittest.mock import patch

import pytest


def _refresh_config() -> None:
    from ouroboros.storage.settings.bootstrap import bootstrap_config, reset_config_for_tests

    reset_config_for_tests()
    bootstrap_config(force=True)


class _InitResult:
    def __init__(self, error: Exception | None = None) -> None:
        self.error = error


class _FakeSdk:
    agent_state = "active"
    agent_found = True
    agent_is_compatible = True
    agent_version_string = "1.2.3"

    def __init__(self) -> None:
        self.diagnostic_callback = None
        self.verbose_callback = None
        self.payload_calls: list[str] = []

    def set_diagnostic_callback(self, callback) -> None:
        self.diagnostic_callback = callback

    def set_verbose_callback(self, callback) -> None:
        self.verbose_callback = callback

    def add_custom_request_attribute(self, *args, **kwargs) -> None:
        del args, kwargs
        self.payload_calls.append("add_custom_request_attribute")

    def trace_custom_service(self, *args, **kwargs):
        del args, kwargs
        self.payload_calls.append("trace_custom_service")


class _FakeOneAgent:
    def __init__(self, *, init_error: Exception | None = None) -> None:
        self.init_error = init_error
        self.sdk = _FakeSdk()
        self.initialize_calls: list[tuple[tuple, dict]] = []
        self.shutdown_calls = 0

    def initialize(self, *args, **kwargs) -> _InitResult:
        self.initialize_calls.append((args, kwargs))
        return _InitResult(self.init_error)

    def get_sdk(self) -> _FakeSdk:
        return self.sdk

    def shutdown(self) -> None:
        self.shutdown_calls += 1


def _reset_flags(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in ("AEF_REFLEX_ENABLED", "DYNATRACE_ENABLED", "ENABLE_REFLEX"):
        monkeypatch.delenv(key, raising=False)
    _refresh_config()


def test_dynatrace_disabled_does_not_import_oneagent(monkeypatch: pytest.MonkeyPatch) -> None:
    from ouroboros.observability import dynatrace

    dynatrace.reset_dynatrace_monitoring_for_tests()
    _reset_flags(monkeypatch)

    def fail_import(name: str):
        if name == "oneagent":
            raise AssertionError("oneagent should not be imported when Reflex is disabled")
        return importlib.import_module(name)

    monkeypatch.setattr(dynatrace.importlib, "import_module", fail_import)

    assert dynatrace.initialize_dynatrace_monitoring() is False


def test_dynatrace_initializes_oneagent_without_payload_hooks(monkeypatch: pytest.MonkeyPatch) -> None:
    from ouroboros.observability import dynatrace

    dynatrace.reset_dynatrace_monitoring_for_tests()
    _reset_flags(monkeypatch)
    monkeypatch.setenv("ENABLE_REFLEX", "true")
    _refresh_config()
    fake_oneagent = _FakeOneAgent()
    monkeypatch.setattr(dynatrace.importlib, "import_module", lambda name: fake_oneagent)

    assert dynatrace.initialize_dynatrace_monitoring() is True

    assert fake_oneagent.initialize_calls == [((), {})]
    assert fake_oneagent.sdk.diagnostic_callback is not None
    assert fake_oneagent.sdk.verbose_callback is None
    assert fake_oneagent.sdk.payload_calls == []


def test_dynatrace_import_error_is_non_fatal(monkeypatch: pytest.MonkeyPatch) -> None:
    from ouroboros.observability import dynatrace

    dynatrace.reset_dynatrace_monitoring_for_tests()
    _reset_flags(monkeypatch)
    monkeypatch.setenv("ENABLE_REFLEX", "true")
    _refresh_config()
    monkeypatch.setattr(dynatrace.importlib, "import_module", lambda name: (_ for _ in ()).throw(RuntimeError(name)))

    assert dynatrace.initialize_dynatrace_monitoring() is False


def test_dynatrace_init_error_is_non_fatal(monkeypatch: pytest.MonkeyPatch) -> None:
    from ouroboros.observability import dynatrace

    dynatrace.reset_dynatrace_monitoring_for_tests()
    _reset_flags(monkeypatch)
    monkeypatch.setenv("ENABLE_REFLEX", "true")
    _refresh_config()
    fake_oneagent = _FakeOneAgent(init_error=RuntimeError("sdk init failed"))
    monkeypatch.setattr(dynatrace.importlib, "import_module", lambda name: fake_oneagent)

    assert dynatrace.initialize_dynatrace_monitoring() is False
    dynatrace.shutdown_dynatrace_monitoring()

    assert fake_oneagent.initialize_calls == [((), {})]
    assert fake_oneagent.shutdown_calls == 0


def test_dynatrace_lifecycle_is_idempotent(monkeypatch: pytest.MonkeyPatch) -> None:
    from ouroboros.observability import dynatrace

    dynatrace.reset_dynatrace_monitoring_for_tests()
    _reset_flags(monkeypatch)
    monkeypatch.setenv("ENABLE_REFLEX", "true")
    _refresh_config()
    fake_oneagent = _FakeOneAgent()
    monkeypatch.setattr(dynatrace.importlib, "import_module", lambda name: fake_oneagent)

    assert dynatrace.initialize_dynatrace_monitoring() is True
    assert dynatrace.initialize_dynatrace_monitoring() is True
    dynatrace.shutdown_dynatrace_monitoring()
    dynatrace.shutdown_dynatrace_monitoring()

    assert fake_oneagent.initialize_calls == [((), {})]
    assert fake_oneagent.shutdown_calls == 1


def test_server_lifespan_starts_and_stops_dynatrace(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    from ouroboros.storage.settings.bootstrap import bootstrap_config, reset_config_for_tests

    monkeypatch.setenv("OUROBOROS_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("OUROBOROS_ENV", "development")
    reset_config_for_tests()
    bootstrap_config(force=True)
    sys.modules.pop("server", None)
    srv = importlib.import_module("server")
    from ouroboros.observability import dynatrace

    calls: list[str] = []
    monkeypatch.setattr(dynatrace, "initialize_dynatrace_monitoring", lambda: calls.append("init") or True)
    monkeypatch.setattr(dynatrace, "shutdown_dynatrace_monitoring", lambda: calls.append("shutdown"))

    from starlette.testclient import TestClient

    with ExitStack() as stack:
        stack.enter_context(patch.object(srv, "_start_supervisor_if_needed", lambda *_a, **_k: None))
        stack.enter_context(patch.object(srv, "_apply_settings_to_env", lambda *_a, **_k: None))
        stack.enter_context(patch.object(srv, "apply_runtime_provider_defaults", lambda s: (s, False, [])))
        stack.enter_context(patch.object(srv, "has_supervisor_provider", lambda _s: False))
        srv._supervisor_ready.set()
        with TestClient(srv.app) as client:
            assert client.get("/health/liveness").status_code == 200

    assert calls == ["init", "shutdown"]
