from __future__ import annotations

import functools

import pytest

from ouroboros.app.interfaces.deployment_keyspace import IDeploymentKeyspaceRepository
from ouroboros.storage.runtime_persistence import runtime_persistence
from tests.helpers.parametrize import parametrize_from_dict


def test_the_shared_start_up_compares_the_keyspace_after_asserting_the_schema(tmp_path, monkeypatch) -> None:
    from multiuser_config import bootstrap

    calls: list[str] = []
    _record_the_shared_start_up(tmp_path, monkeypatch, calls, _BrokerStep(calls, refusal=None))

    bootstrap.bootstrap_process("test-tier")

    assert calls.count("keyspace") == 1, f"the shared start-up ran the keyspace comparison {calls.count('keyspace')}x"
    assert calls.count("broker") == 1
    assert calls.index("keyspace") > calls.index("schema") > calls.index("store") > calls.index("broker")
    assert calls.index("broker") > calls.index("cache refusal") > calls.index("refusals")
    assert calls.index("refusals") > calls.index("chat encryption") > calls.index("storage")
    assert calls.index("cache") > calls.index("broker")


def test_the_transport_root_initializes_the_broker_between_the_refusals_and_the_store(monkeypatch) -> None:
    from multiuser_config.transport.browser.app import build_transport_app

    calls: list[str] = []
    _record_the_transport_start_up(monkeypatch, calls, _BrokerStep(calls, refusal=None))

    with pytest.raises(_KeyspaceReached):
        build_transport_app()

    assert calls.count("broker") == 1
    assert calls.index("keyspace") > calls.index("schema") > calls.index("store") > calls.index("broker")
    assert calls.index("broker") > calls.index("cache refusal") > calls.index("refusals")
    assert calls.index("refusals") > calls.index("chat encryption") > calls.index("storage")
    assert calls.index("cache") > calls.index("broker")


_BROKER_REFUSALS = {
    "in-process-under-the-mode": RuntimeError(
        "OUROBOROS_MULTIUSER=true with OUROBOROS_BROKER_BACKEND='inprocess': this process is a component tier"
    ),
    "kafka-without-bootstrap-servers": RuntimeError(
        "A Kafka connection needs OUROBOROS_KAFKA_BOOTSTRAP_SERVERS, which is empty"
    ),
}

refused_broker_cases = {
    f"{root}-{name}": {"root": root, "refusal": refusal}
    for root in ("shared-start-up", "transport-root")
    for name, refusal in _BROKER_REFUSALS.items()
}


@parametrize_from_dict(refused_broker_cases)
def test_a_refused_broker_records_no_keyspace(root, refusal, tmp_path, monkeypatch) -> None:
    from multiuser_config import bootstrap
    from multiuser_config.transport.browser.app import build_transport_app

    calls: list[str] = []
    broker = _BrokerStep(calls, refusal=refusal)
    starts = {
        "shared-start-up": functools.partial(bootstrap.bootstrap_process, "test-tier"),
        "transport-root": build_transport_app,
    }
    if root == "shared-start-up":
        _record_the_shared_start_up(tmp_path, monkeypatch, calls, broker)
    else:
        _record_the_transport_start_up(monkeypatch, calls, broker)

    with pytest.raises(RuntimeError) as raised:
        starts[root]()

    assert raised.value is refusal
    assert "broker" in calls
    assert "store" not in calls and "schema" not in calls and "keyspace" not in calls


class _KeyspaceReached(Exception):
    pass


class _BrokerStep:
    def __init__(self, calls: list[str], *, refusal: BaseException | None) -> None:
        self._calls = calls
        self._refusal = refusal

    def __call__(self) -> None:
        self._calls.append("broker")
        if self._refusal is not None:
            raise self._refusal


def _record_the_shared_start_up(tmp_path, monkeypatch, calls: list[str], broker: _BrokerStep) -> None:
    from infra.cache import cache_factory
    from multiuser_config import bootstrap
    from ouroboros.storage.runtime_persistence import runtime_persistence
    from ouroboros.storage.settings.schema import AppSettings

    monkeypatch.setenv("OUROBOROS_MULTIUSER", "true")
    store = runtime_persistence.current
    initialize_cache = cache_factory.initialize
    monkeypatch.setattr(bootstrap, "init_config", lambda: None)
    monkeypatch.setattr("ouroboros.app.config.DATA_DIR", tmp_path)
    monkeypatch.setattr("ouroboros.app.config.REPO_DIR", tmp_path)
    monkeypatch.setattr(
        "ouroboros.app.server_runtime.apply_runtime_provider_defaults",
        lambda settings: (AppSettings(), False, set()),
    )
    monkeypatch.setattr("ouroboros.app.server_runtime.has_supervisor_provider", lambda _settings: True)
    monkeypatch.setattr("ouroboros.observability.logging_setup.configure_logging", lambda **_kwargs: None)
    monkeypatch.setattr("ouroboros.app.config.validate_production_storage", lambda: calls.append("storage"))
    monkeypatch.setattr(
        "ouroboros.storage.message_field_crypto.validate_chat_encryption", lambda: calls.append("chat encryption")
    )
    monkeypatch.setattr(
        "ouroboros.app.multiuser_validation.validate_multiuser_configuration",
        lambda *_args: calls.append("refusals"),
    )
    monkeypatch.setattr("infra.cache.refuse_a_process_local_settings_cache", lambda _c: calls.append("cache refusal"))
    monkeypatch.setattr(cache_factory, "initialize", lambda *args: calls.append("cache") or initialize_cache(*args))
    monkeypatch.setattr("ouroboros.app.broker_composition.initialize_broker_backend", broker)
    monkeypatch.setattr(
        "ouroboros.storage.runtime_persistence.runtime_persistence.initialize",
        lambda: calls.append("store") or store,
    )
    monkeypatch.setattr(
        "infra.migrations.runner.assert_schema_at_head",
        lambda _store: calls.append("schema"),
    )
    monkeypatch.setattr(
        "ouroboros.app.multiuser_validation.validate_deployment_keyspace",
        lambda repository: calls.append("keyspace"),
    )


def _record_the_transport_start_up(monkeypatch, calls: list[str], broker: _BrokerStep) -> None:
    from multiuser_config.transport.browser import app as transport_app

    def _keyspace(repository: IDeploymentKeyspaceRepository) -> None:
        calls.append("keyspace")
        raise _KeyspaceReached()

    initialize_cache = transport_app.cache_factory.initialize
    store = runtime_persistence.current
    monkeypatch.setattr(transport_app, "validate_production_storage", lambda: calls.append("storage"))
    monkeypatch.setattr(transport_app, "validate_chat_encryption", lambda: calls.append("chat encryption"))
    monkeypatch.setattr(transport_app, "validate_multiuser_configuration", lambda: calls.append("refusals"))
    monkeypatch.setattr(
        transport_app, "refuse_a_process_local_settings_cache", lambda _t: calls.append("cache refusal")
    )
    monkeypatch.setattr(
        transport_app.cache_factory, "initialize", lambda *args: calls.append("cache") or initialize_cache(*args)
    )
    monkeypatch.setattr(transport_app, "initialize_broker_backend", broker)
    monkeypatch.setattr(transport_app.runtime_persistence, "initialize", lambda: calls.append("store") or store)
    monkeypatch.setattr(transport_app, "assert_schema_at_head", lambda _store: calls.append("schema"))
    monkeypatch.setattr(transport_app, "validate_deployment_keyspace", _keyspace)
