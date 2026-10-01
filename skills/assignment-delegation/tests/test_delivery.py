from __future__ import annotations

import importlib.util
import json
import logging
import smtplib
import ssl
import sys
import threading
import time
from pathlib import Path
from typing import Any

import pytest
from pydantic import SecretStr

from ouroboros.storage.runtime_store import get_runtime_store
from ouroboros.storage.settings.schema import AppSettings

_SKILL_DIR = Path(__file__).resolve().parents[1]
_DELIVERY_MODULE = "assignment_delegation_delivery_payload"


def _delivery_module():
    module = sys.modules.get(_DELIVERY_MODULE)
    if module is not None:
        return module
    spec = importlib.util.spec_from_file_location(_DELIVERY_MODULE, _SKILL_DIR / "delivery.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[_DELIVERY_MODULE] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        sys.modules.pop(_DELIVERY_MODULE, None)
        raise
    return module


def _settings(**overrides: Any) -> AppSettings:
    values = {
        "ASSISTANT_ASSIGNMENT_SMTP_HOST": "smtp.example.test",
        "ASSISTANT_ASSIGNMENT_SMTP_PORT": 25,
        "ASSISTANT_ASSIGNMENT_SMTP_USERNAME": "smtp-user",
        "ASSISTANT_ASSIGNMENT_SMTP_PASSWORD": SecretStr("smtp-secret"),
        "ASSISTANT_ASSIGNMENT_MAIL_FROM": "assignments@example.test",
        "ASSISTANT_ASSIGNMENT_MAIL_TO": "one@example.test,two@example.test",
        "ASSISTANT_ASSIGNMENT_SMTP_STARTTLS_REQUIRED": True,
        "ASSISTANT_ASSIGNMENT_SMTP_TIMEOUT_SEC": 3.0,
        "ASSISTANT_ASSIGNMENT_DELIVERY_TIMEOUT_SEC": 30.0,
        "ASSISTANT_ASSIGNMENT_MAX_ATTEMPTS": 2,
        "ASSISTANT_ASSIGNMENT_RETRY_MIN_SECONDS": 1.0,
        "ASSISTANT_ASSIGNMENT_RETRY_MAX_SECONDS": 5.0,
        "DIALOGS_GRPC_ENDPOINT": "grpc://dialogs.example.test",
        "DIALOGS_SECURE": True,
        "DIALOGS_ROOT_CERTIFICATES": "/tmp/root.pem",
        "ASSISTANT_DIALOGS_BOT_TOKEN": SecretStr("dialogs-secret"),
        "ASSISTANT_DIALOGS_BOT_GROUP_ID": "42",
    }
    values.update(overrides)
    return AppSettings(**values)


class _FakeSMTP:
    def __init__(self, action: Any = None):
        self.action = action
        self.ehlo_calls = 0
        self.starttls_calls = 0
        self.login_calls: list[tuple[str, str]] = []
        self.send_calls: list[dict[str, Any]] = []
        self.quit_calls = 0

    def ehlo(self):
        self.ehlo_calls += 1
        return 250, b"ok"

    def starttls(self, context=None):
        self.starttls_calls += 1
        self.tls_context = context
        return 220, b"ready"

    def login(self, username: str, password: str):
        self.login_calls.append((username, password))
        return 235, b"authenticated"

    def send_message(self, message, *, from_addr: str, to_addrs: list[str]):
        self.send_calls.append(
            {
                "message": message,
                "from_addr": from_addr,
                "to_addrs": list(to_addrs),
            }
        )
        if isinstance(self.action, BaseException):
            raise self.action
        return dict(self.action or {})

    def quit(self):
        self.quit_calls += 1


class _FakeSSLContext:
    def __init__(self, *, cafile: str | None = None):
        self.cafile = cafile
        self.check_hostname = True
        self.verify_mode = ssl.CERT_REQUIRED
        self.minimum_version = None
        self.maximum_version = None
        self.cert_chain_calls: list[tuple[str, str | None]] = []

    def load_cert_chain(self, certfile: str, keyfile: str | None = None):
        self.cert_chain_calls.append((certfile, keyfile))


class _SMTPFactory:
    def __init__(self, actions: list[Any]):
        self.actions = list(actions)
        self.instances: list[_FakeSMTP] = []
        self.calls: list[tuple[str, int, float]] = []

    def __call__(self, host: str, port: int, timeout: float):
        self.calls.append((host, port, timeout))
        action = self.actions.pop(0) if self.actions else {}
        if isinstance(action, type) and issubclass(action, BaseException):
            raise action()
        if isinstance(action, BaseException) and not isinstance(action, smtplib.SMTPResponseException):
            raise action
        instance = _FakeSMTP(action)
        self.instances.append(instance)
        return instance


class _FakeDialogsProvider:
    def __init__(self, cfg, *, ok: bool = True, status: str = "mid-1"):
        self.cfg = cfg
        self.ok = ok
        self.status = status
        self.connect_calls = 0
        self.authenticate_calls = 0
        self.send_calls: list[tuple[str, str, str]] = []

    def connect(self):
        self.connect_calls += 1

    def authenticate(self):
        self.authenticate_calls += 1
        raise AssertionError("outbound-only assignment bot must not authenticate")

    def send_text(self, chat_id, text: str, fmt: str = ""):
        self.send_calls.append((str(chat_id), text, fmt))
        return self.ok, self.status


def test_assignment_delivery_uses_fixed_smtp_and_dialogs_settings(tmp_path: Path) -> None:
    deliver_assignment = _delivery_module().deliver_assignment

    smtp_factory = _SMTPFactory([{}])
    provider_box: dict[str, _FakeDialogsProvider] = {}

    def provider_factory(cfg):
        provider = _FakeDialogsProvider(cfg)
        provider_box["provider"] = provider
        return provider

    result = deliver_assignment(
        "Поручение",
        "Подготовить отчет",
        repo_dir=tmp_path,
        drive_root=tmp_path,
        settings=_settings(),
        smtp_factory=smtp_factory,
        dialogs_provider_factory=provider_factory,
        sleeper=lambda _seconds: None,
    )

    assert result["status"] == "sent"
    assert result["assignment_id"]
    assert result["email"]["status"] == "sent"
    assert result["sberchat"]["status"] == "sent"
    assert smtp_factory.calls == [("smtp.example.test", 25, 3.0)]

    smtp = smtp_factory.instances[0]
    assert smtp.ehlo_calls == 2
    assert smtp.starttls_calls == 1
    assert smtp.login_calls == [("smtp-user", "smtp-secret")]
    assert smtp.send_calls[0]["from_addr"] == "assignments@example.test"
    assert smtp.send_calls[0]["to_addrs"] == ["one@example.test", "two@example.test"]
    message = smtp.send_calls[0]["message"]
    assert message["From"] == "assignments@example.test"
    assert message["To"] == "one@example.test, two@example.test"
    assert message["Subject"] == "Поручение"

    provider = provider_box["provider"]
    assert provider.cfg.DIALOGS_GRPC_ENDPOINT == "grpc://dialogs.example.test"
    assert provider.cfg.DIALOGS_BOT_TOKEN.get_secret_value() == "dialogs-secret"
    assert provider.cfg.DIALOGS_GROUP_ID == "42"
    assert provider.connect_calls == 1
    assert provider.authenticate_calls == 0
    assert provider.send_calls == [("42", "Поручение\n\nПодготовить отчет", "markdown")]

    state_path = tmp_path / "state" / "assignment_delivery" / f"{result['assignment_id']}.json"
    assert not state_path.exists()


def test_assignment_delivery_dialogs_only_skips_smtp(tmp_path: Path) -> None:
    result = _delivery_module().deliver_assignment(
        "Поручение",
        "Подготовить отчет",
        drive_root=tmp_path,
        settings=AppSettings(
            DIALOGS_GRPC_ENDPOINT="grpc://dialogs.example.test",
            ASSISTANT_DIALOGS_BOT_TOKEN=SecretStr("dialogs-secret"),
            ASSISTANT_DIALOGS_BOT_GROUP_ID="42",
        ),
        smtp_factory=lambda *_args: pytest.fail("SMTP must not run"),
        dialogs_provider_factory=lambda cfg: _FakeDialogsProvider(cfg),
    )

    assert result["status"] == "sent"
    assert result["email"]["status"] == "skipped"
    assert result["sberchat"]["status"] == "sent"


def test_assignment_delivery_smtp_only_skips_dialogs(tmp_path: Path) -> None:
    result = _delivery_module().deliver_assignment(
        "Поручение",
        "Подготовить отчет",
        drive_root=tmp_path,
        settings=_settings(
            ASSISTANT_DIALOGS_BOT_TOKEN=SecretStr(""),
            ASSISTANT_DIALOGS_BOT_GROUP_ID="",
        ),
        smtp_factory=_SMTPFactory([{}]),
        dialogs_provider_factory=lambda _cfg: pytest.fail("Dialogs must not run"),
        sleeper=lambda _seconds: None,
    )

    assert result["status"] == "sent"
    assert result["email"]["status"] == "sent"
    assert result["sberchat"]["status"] == "skipped"


def test_assignment_delivery_without_channels_returns_config_error(tmp_path: Path) -> None:
    result = _delivery_module().deliver_assignment(
        "Поручение",
        "Подготовить отчет",
        drive_root=tmp_path,
        settings=AppSettings(),
        smtp_factory=lambda *_args: pytest.fail("SMTP must not run"),
        dialogs_provider_factory=lambda _cfg: pytest.fail("Dialogs must not run"),
    )

    assert result["status"] == "failed"
    assert result["error"].startswith("ASSIGNMENT_CONFIG_ERROR:")
    assert result["email"]["status"] == "skipped"
    assert result["sberchat"]["status"] == "skipped"


def test_assignment_delivery_configures_smtp_starttls_context(monkeypatch, tmp_path: Path) -> None:
    assignment_delivery = _delivery_module()

    contexts: list[_FakeSSLContext] = []

    def create_default_context(*, cafile: str | None = None):
        context = _FakeSSLContext(cafile=cafile)
        contexts.append(context)
        return context

    monkeypatch.setattr(assignment_delivery.ssl, "create_default_context", create_default_context)

    smtp_factory = _SMTPFactory([{}])
    result = assignment_delivery.deliver_assignment(
        "Поручение",
        "Подготовить отчет",
        repo_dir=tmp_path,
        drive_root=tmp_path,
        settings=_settings(
            ASSISTANT_ASSIGNMENT_SMTP_ROOT_CERTIFICATES="/certs/root.pem",
            ASSISTANT_ASSIGNMENT_SMTP_CLIENT_CERTIFICATE="/certs/client.pem",
            ASSISTANT_ASSIGNMENT_SMTP_TLS_VERSION="TLSv1.3",
        ),
        smtp_factory=smtp_factory,
        dialogs_provider_factory=lambda cfg: _FakeDialogsProvider(cfg),
        sleeper=lambda _seconds: None,
    )

    assert result["email"]["status"] == "sent"
    assert len(contexts) == 1
    assert contexts[0].cafile == "/certs/root.pem"
    assert contexts[0].cert_chain_calls == [("/certs/client.pem", None)]
    assert contexts[0].minimum_version is ssl.TLSVersion.TLSv1_3
    assert contexts[0].maximum_version is ssl.TLSVersion.TLSv1_3
    assert smtp_factory.instances[0].tls_context is contexts[0]


def test_assignment_delivery_allows_smtp_starttls_without_client_certificate(
    monkeypatch,
    tmp_path: Path,
) -> None:
    assignment_delivery = _delivery_module()

    contexts: list[_FakeSSLContext] = []

    def create_default_context(*, cafile: str | None = None):
        context = _FakeSSLContext(cafile=cafile)
        contexts.append(context)
        return context

    monkeypatch.setattr(assignment_delivery.ssl, "create_default_context", create_default_context)

    smtp_factory = _SMTPFactory([{}])
    result = assignment_delivery.deliver_assignment(
        "Поручение",
        "Подготовить отчет",
        repo_dir=tmp_path,
        drive_root=tmp_path,
        settings=_settings(
            ASSISTANT_ASSIGNMENT_SMTP_CLIENT_CERTIFICATE="",
            ASSISTANT_ASSIGNMENT_SMTP_TLS_VERSION="TLSv1.2",
        ),
        smtp_factory=smtp_factory,
        dialogs_provider_factory=lambda cfg: _FakeDialogsProvider(cfg),
        sleeper=lambda _seconds: None,
    )

    assert result["email"]["status"] == "sent"
    assert len(contexts) == 1
    assert contexts[0].cafile is None
    assert contexts[0].cert_chain_calls == []
    assert contexts[0].minimum_version is ssl.TLSVersion.TLSv1_2
    assert contexts[0].maximum_version is ssl.TLSVersion.TLSv1_2
    assert smtp_factory.instances[0].tls_context is contexts[0]


def test_assignment_delivery_can_disable_smtp_server_tls_verification(
    monkeypatch,
    tmp_path: Path,
) -> None:
    assignment_delivery = _delivery_module()

    contexts: list[_FakeSSLContext] = []

    def create_default_context(*, cafile: str | None = None):
        context = _FakeSSLContext(cafile=cafile)
        contexts.append(context)
        return context

    monkeypatch.setattr(assignment_delivery.ssl, "create_default_context", create_default_context)

    smtp_factory = _SMTPFactory([{}])
    result = assignment_delivery.deliver_assignment(
        "Поручение",
        "Подготовить отчет",
        repo_dir=tmp_path,
        drive_root=tmp_path,
        settings=_settings(
            ASSISTANT_ASSIGNMENT_SMTP_ROOT_CERTIFICATES="/certs/root.pem",
            ASSISTANT_ASSIGNMENT_SMTP_CLIENT_CERTIFICATE="/certs/client.pem",
            ASSISTANT_ASSIGNMENT_SMTP_TLS_VERSION="TLSv1.2",
            ASSISTANT_ASSIGNMENT_SMTP_VERIFY_TLS=False,
        ),
        smtp_factory=smtp_factory,
        dialogs_provider_factory=lambda cfg: _FakeDialogsProvider(cfg),
        sleeper=lambda _seconds: None,
    )

    assert result["email"]["status"] == "sent"
    assert len(contexts) == 1
    assert contexts[0].cafile == "/certs/root.pem"
    assert contexts[0].check_hostname is False
    assert contexts[0].verify_mode is ssl.CERT_NONE
    assert contexts[0].cert_chain_calls == [("/certs/client.pem", None)]
    assert contexts[0].minimum_version is ssl.TLSVersion.TLSv1_2
    assert contexts[0].maximum_version is ssl.TLSVersion.TLSv1_2
    assert smtp_factory.instances[0].tls_context is contexts[0]


def test_assignment_delivery_retries_only_transient_refused_recipients(tmp_path: Path) -> None:
    deliver_assignment = _delivery_module().deliver_assignment

    smtp_factory = _SMTPFactory(
        [
            {"two@example.test": (451, b"try later")},
            {},
        ]
    )
    sleeps: list[float] = []

    result = deliver_assignment(
        "Тема",
        "Текст",
        repo_dir=tmp_path,
        drive_root=tmp_path,
        settings=_settings(),
        smtp_factory=smtp_factory,
        dialogs_provider_factory=lambda cfg: _FakeDialogsProvider(cfg),
        sleeper=sleeps.append,
    )

    assert result["email"]["status"] == "sent"
    assert result["email"]["attempts"] == 2
    assert sleeps == [1.0]
    assert smtp_factory.instances[0].send_calls[0]["to_addrs"] == ["one@example.test", "two@example.test"]
    assert smtp_factory.instances[1].send_calls[0]["to_addrs"] == ["two@example.test"]


def test_assignment_delivery_preserves_smtp_only_partial_failure_after_transient_retry(tmp_path: Path) -> None:
    deliver_assignment = _delivery_module().deliver_assignment

    smtp_factory = _SMTPFactory(
        [
            {
                "two@example.test": (451, b"try later"),
                "three@example.test": (550, b"bad recipient"),
            },
            {},
        ]
    )

    result = deliver_assignment(
        "Тема",
        "Текст",
        repo_dir=tmp_path,
        drive_root=tmp_path,
        settings=_settings(
            ASSISTANT_ASSIGNMENT_MAIL_TO="one@example.test,two@example.test,three@example.test",
            DIALOGS_GRPC_ENDPOINT="",
            ASSISTANT_DIALOGS_BOT_TOKEN=SecretStr(""),
            ASSISTANT_DIALOGS_BOT_GROUP_ID="",
        ),
        smtp_factory=smtp_factory,
        dialogs_provider_factory=lambda _cfg: pytest.fail("Dialogs must not run"),
        sleeper=lambda _seconds: None,
    )

    assert result["status"] == "partial_failure"
    assert result["email"]["status"] == "partial_failure"
    assert result["sberchat"]["status"] == "skipped"
    assert result["email"]["recipients"] == ["one@example.test", "two@example.test"]
    assert result["email"]["failed_recipients"] == ["three@example.test"]
    assert smtp_factory.instances[1].send_calls[0]["to_addrs"] == ["two@example.test"]


def test_assignment_delivery_does_not_retry_permanent_smtp_errors(tmp_path: Path) -> None:
    deliver_assignment = _delivery_module().deliver_assignment

    smtp_factory = _SMTPFactory([smtplib.SMTPDataError(550, b"bad sender")])
    sleeps: list[float] = []

    result = deliver_assignment(
        "Тема",
        "Текст",
        repo_dir=tmp_path,
        drive_root=tmp_path,
        settings=_settings(),
        smtp_factory=smtp_factory,
        dialogs_provider_factory=lambda cfg: _FakeDialogsProvider(cfg),
        sleeper=sleeps.append,
    )

    assert result["email"]["status"] == "failed"
    assert result["email"]["smtp_code"] == 550
    assert result["email"]["attempts"] == 1
    assert sleeps == []
    assert len(smtp_factory.instances) == 1


def test_assignment_delivery_retries_network_eof_with_new_smtp_connection(tmp_path: Path) -> None:
    deliver_assignment = _delivery_module().deliver_assignment

    smtp_factory = _SMTPFactory([EOFError("network eof"), {}])
    sleeps: list[float] = []

    result = deliver_assignment(
        "Тема",
        "Текст",
        repo_dir=tmp_path,
        drive_root=tmp_path,
        settings=_settings(),
        smtp_factory=smtp_factory,
        dialogs_provider_factory=lambda cfg: _FakeDialogsProvider(cfg),
        sleeper=sleeps.append,
    )

    assert result["email"]["status"] == "sent"
    assert result["email"]["attempts"] == 2
    assert len(smtp_factory.calls) == 2
    assert sleeps == [1.0]


def test_assignment_dialogs_starts_while_smtp_is_retrying(tmp_path: Path) -> None:
    deliver_assignment = _delivery_module().deliver_assignment

    dialogs_started = threading.Event()

    def provider_factory(cfg):
        dialogs_started.set()
        return _FakeDialogsProvider(cfg)

    def sleeper(_seconds: float) -> None:
        assert dialogs_started.wait(1.0), "sberchat must start while SMTP is waiting before retry"

    result = deliver_assignment(
        "Тема",
        "Текст",
        repo_dir=tmp_path,
        drive_root=tmp_path,
        settings=_settings(),
        smtp_factory=_SMTPFactory([EOFError("network eof"), {}]),
        dialogs_provider_factory=provider_factory,
        sleeper=sleeper,
    )

    assert result["email"]["status"] == "sent"
    assert result["sberchat"]["status"] == "sent"


def test_assignment_delivery_deadline_returns_without_waiting_for_hung_dialogs(tmp_path: Path) -> None:
    deliver_assignment = _delivery_module().deliver_assignment
    from ouroboros.storage.runtime_store import init_runtime_store

    # In production the runtime store is initialized at startup; init it here
    # so the first tool event doesn't spend the delivery deadline on
    # running DB migrations.
    init_runtime_store(tmp_path)

    # The sberchat channel blocks until released — far longer than the delivery
    # deadline — so delivery must return by hitting the deadline rather than by
    # waiting for the channel. The event is released in the test's ``finally`` so
    # the pool thread never lingers past the test. Using an event instead of a
    # fixed ``sleep`` keeps the "hang" reliably longer than the deadline on slow
    # CI runners, where a short real-time sleep used to race the deadline.
    release = threading.Event()

    class HungDialogsProvider(_FakeDialogsProvider):
        def send_text(self, chat_id, text: str, fmt: str = ""):
            release.wait(30.0)
            return super().send_text(chat_id, text, fmt)

    started_at = time.perf_counter()
    try:
        result = deliver_assignment(
            "Тема",
            "Текст",
            repo_dir=tmp_path,
            drive_root=tmp_path,
            settings=_settings(ASSISTANT_ASSIGNMENT_DELIVERY_TIMEOUT_SEC=0.5),
            smtp_factory=_SMTPFactory([{}]),
            dialogs_provider_factory=lambda cfg: HungDialogsProvider(cfg),
            sleeper=lambda _seconds: None,
        )
    finally:
        release.set()
    elapsed = time.perf_counter() - started_at

    # Returned by hitting the ~0.5s deadline, not by waiting out the 30s hang.
    assert elapsed < 5.0
    assert result["email"]["status"] == "sent"
    assert result["sberchat"]["status"] == "failed"
    assert "deadline_exceeded" in result["sberchat"]["error"]


def test_assignment_delivery_logs_channels_attempts_and_final_result(tmp_path: Path, caplog) -> None:
    deliver_assignment = _delivery_module().deliver_assignment

    caplog.set_level(logging.INFO, logger=_DELIVERY_MODULE)

    result = deliver_assignment(
        "Секретная тема",
        "Секретный текст",
        repo_dir=tmp_path,
        drive_root=tmp_path,
        settings=_settings(),
        smtp_factory=_SMTPFactory([{}]),
        dialogs_provider_factory=lambda cfg: _FakeDialogsProvider(cfg),
        sleeper=lambda _seconds: None,
        task_id="task-42",
    )

    app_log_text = "\n".join(record.getMessage() for record in caplog.records)
    assert not (tmp_path / "logs" / "tools.jsonl").exists()
    store = get_runtime_store(tmp_path)
    assert store is not None
    tool_events = [dict(event.metadata or {}) for event in store.list_audit_events(log_stream="tools", limit=100)]
    event_types = [event["type"] for event in tool_events]

    assert result["status"] == "sent"
    assert "assignment_delivery_started" in event_types
    assert event_types.count("assignment_channel_started") == 2
    assert "assignment_smtp_attempt_started" in event_types
    assert "assignment_smtp_attempt_finished" in event_types
    assert event_types.count("assignment_channel_finished") == 2
    assert "assignment_delivery_finished" in event_types
    assert result["assignment_id"] in app_log_text
    assert "task-42" in app_log_text
    assert "smtp.example.test" in app_log_text
    assert '"smtp_code": 250' in app_log_text
    assert '"group_id": "42"' in app_log_text

    serialized_events = json.dumps(tool_events, ensure_ascii=False)
    assert "smtp-secret" not in app_log_text
    assert "dialogs-secret" not in app_log_text
    assert "Секретная тема" not in app_log_text
    assert "Секретный текст" not in app_log_text
    assert "smtp-secret" not in serialized_events
    assert "dialogs-secret" not in serialized_events
    assert "Секретная тема" not in serialized_events
    assert "Секретный текст" not in serialized_events
    assert "subject_hash" not in serialized_events
    assert "body_hash" not in serialized_events


def test_assignment_delivery_reports_partial_failure_and_redacts_secrets(tmp_path: Path) -> None:
    deliver_assignment = _delivery_module().deliver_assignment

    smtp_factory = _SMTPFactory([smtplib.SMTPDataError(550, b"smtp-secret rejected")])

    def provider_factory(cfg):
        return _FakeDialogsProvider(cfg, ok=False, status="dialogs-secret unavailable")

    result = deliver_assignment(
        "Тема",
        "Текст",
        repo_dir=tmp_path,
        drive_root=tmp_path,
        settings=_settings(),
        smtp_factory=smtp_factory,
        dialogs_provider_factory=provider_factory,
        sleeper=lambda _seconds: None,
    )

    serialized = json.dumps(result, ensure_ascii=False)
    assert result["status"] == "failed"
    assert result["email"]["status"] == "failed"
    assert result["sberchat"]["status"] == "failed"
    assert "smtp-secret" not in serialized
    assert "dialogs-secret" not in serialized


def test_assignment_dialogs_failure_after_email_success_is_partial(tmp_path: Path) -> None:
    deliver_assignment = _delivery_module().deliver_assignment

    result = deliver_assignment(
        "Тема",
        "Текст",
        repo_dir=tmp_path,
        drive_root=tmp_path,
        settings=_settings(),
        smtp_factory=_SMTPFactory([{}]),
        dialogs_provider_factory=lambda cfg: _FakeDialogsProvider(cfg, ok=False, status="not delivered"),
        sleeper=lambda _seconds: None,
    )

    assert result["status"] == "partial_failure"
    assert result["email"]["status"] == "sent"
    assert result["sberchat"]["status"] == "failed"


def test_assignment_retry_is_unavailable_without_persisted_delivery_state(tmp_path: Path) -> None:
    deliver_assignment = _delivery_module().deliver_assignment

    result = deliver_assignment(
        "Тема",
        "Текст",
        repo_dir=tmp_path,
        drive_root=tmp_path,
        settings=_settings(),
        smtp_factory=lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("smtp must not run")),
        dialogs_provider_factory=lambda _cfg: (_ for _ in ()).throw(AssertionError("dialogs must not run")),
        retry_assignment_id="abc123",
        sleeper=lambda _seconds: None,
    )

    assert result["assignment_id"] == "abc123"
    assert result["status"] == "failed"
    assert "ASSIGNMENT_RETRY_UNAVAILABLE" in result["error"]


def test_assignment_deadline_stops_smtp_retries_before_external_timeout(tmp_path: Path) -> None:
    deliver_assignment = _delivery_module().deliver_assignment

    now = 100.0
    sleeps: list[float] = []

    def clock() -> float:
        return now

    def sleeper(seconds: float) -> None:
        nonlocal now
        sleeps.append(seconds)
        now += seconds

    smtp_factory = _SMTPFactory([EOFError("network eof"), {}])

    result = deliver_assignment(
        "Тема",
        "Текст",
        repo_dir=tmp_path,
        drive_root=tmp_path,
        settings=_settings(
            ASSISTANT_ASSIGNMENT_SMTP_TIMEOUT_SEC=30.0,
            ASSISTANT_ASSIGNMENT_MAX_ATTEMPTS=5,
            ASSISTANT_ASSIGNMENT_RETRY_MIN_SECONDS=5.0,
            ASSISTANT_ASSIGNMENT_RETRY_MAX_SECONDS=30.0,
        ),
        smtp_factory=smtp_factory,
        dialogs_provider_factory=lambda cfg: _FakeDialogsProvider(cfg),
        sleeper=sleeper,
        clock=clock,
        deadline_seconds=2.0,
    )

    assert result["email"]["status"] == "failed"
    assert result["email"]["attempts"] == 1
    assert "deadline_exceeded" in result["email"]["error"]
    assert smtp_factory.calls == [("smtp.example.test", 25, 2.0)]
    assert sleeps == [2.0]
