"""Tests for Vault-mounted secret file env bootstrap."""

from __future__ import annotations

import threading
import time

import pytest

from ouroboros.storage.settings.bootstrap import bootstrap_config, reset_config_for_tests
from ouroboros.storage.settings.env_files import load_secret_files, wait_for_secrets_dir
from ouroboros.storage.settings.store import load_settings


@pytest.fixture(autouse=True)
def _reset_bootstrap_state(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("OUROBOROS_SECRETS_WAIT_SEC", "0.1")
    monkeypatch.setenv("OUROBOROS_SECRETS_WAIT_INTERVAL_SEC", "0.02")
    reset_config_for_tests()
    yield
    reset_config_for_tests()


def test_secrets_dir_unset_is_noop(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OUROBOROS_SECRETS_DIR", raising=False)
    monkeypatch.delenv("DIALOGS_BOT_TOKEN", raising=False)

    loaded = load_secret_files()

    assert loaded == {}
    bootstrap_config(force=True)
    assert load_settings().DIALOGS_BOT_TOKEN.get_secret_value() == ""


def test_secret_file_wins_over_env(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    secrets_dir = tmp_path / "secrets"
    secrets_dir.mkdir()
    (secrets_dir / "DIALOGS_BOT_TOKEN").write_text("from-file\n", encoding="utf-8")
    monkeypatch.setenv("OUROBOROS_SECRETS_DIR", str(secrets_dir))
    monkeypatch.setenv("OUROBOROS_ENV", "production")
    monkeypatch.setenv("DIALOGS_BOT_TOKEN", "from-env")

    bootstrap_config(force=True)

    token = load_settings().DIALOGS_BOT_TOKEN
    assert token is not None
    assert token.get_secret_value() == "from-file"


def test_file_fallback_loads_settings(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    secrets_dir = tmp_path / "secrets"
    secrets_dir.mkdir()
    (secrets_dir / "DIALOGS_BOT_TOKEN").write_text("vault-token\n", encoding="utf-8")
    monkeypatch.setenv("OUROBOROS_SECRETS_DIR", str(secrets_dir))
    monkeypatch.setenv("OUROBOROS_ENV", "production")
    monkeypatch.delenv("DIALOGS_BOT_TOKEN", raising=False)

    bootstrap_config(force=True)

    token = load_settings().DIALOGS_BOT_TOKEN
    assert token is not None
    assert token.get_secret_value() == "vault-token"


def test_core_secret_files_are_allowed(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    secrets_dir = tmp_path / "secrets"
    secrets_dir.mkdir()
    (secrets_dir / "DIALOGS_BOT_TOKEN").write_text("dialogs-from-file\n", encoding="utf-8")
    (secrets_dir / "SELFHOSTED_LLM_API_KEY").write_text("llm-from-file\n", encoding="utf-8")
    monkeypatch.setenv("OUROBOROS_SECRETS_DIR", str(secrets_dir))
    monkeypatch.setenv("OUROBOROS_ENV", "production")
    monkeypatch.setenv("DIALOGS_GRPC_ENDPOINT", "grpc://dialogs.example.test")
    monkeypatch.delenv("DIALOGS_BOT_TOKEN", raising=False)
    monkeypatch.delenv("SELFHOSTED_LLM_API_KEY", raising=False)

    bootstrap_config(force=True)

    settings = load_settings()
    assert settings.DIALOGS_BOT_TOKEN.get_secret_value() == "dialogs-from-file"
    assert settings.SELFHOSTED_LLM_API_KEY.get_secret_value() == "llm-from-file"


def test_lowercase_filename_is_loaded(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    secrets_dir = tmp_path / "secrets"
    secrets_dir.mkdir()
    (secrets_dir / "dialogs_bot_token").write_text("lower-case-file\n", encoding="utf-8")
    monkeypatch.setenv("OUROBOROS_SECRETS_DIR", str(secrets_dir))
    monkeypatch.setenv("OUROBOROS_ENV", "production")
    monkeypatch.delenv("DIALOGS_BOT_TOKEN", raising=False)

    loaded = load_secret_files()
    bootstrap_config(force=True)

    assert loaded == {"DIALOGS_BOT_TOKEN": "lower-case-file"}
    assert load_settings().DIALOGS_BOT_TOKEN.get_secret_value() == "lower-case-file"


def test_retired_secret_files_are_rejected_before_settings_validation(tmp_path) -> None:
    secrets_dir = tmp_path / "secrets"
    secrets_dir.mkdir()
    (secrets_dir / "HEIMDALL_TOKEN").write_text("retired-heimdall\n", encoding="utf-8")
    (secrets_dir / "pulse_people_token").write_text("retired-pulse\n", encoding="utf-8")
    (secrets_dir / "HEIMDALL_LEGACY_OPTION").write_text("retired-heimdall-extra\n", encoding="utf-8")
    (secrets_dir / "PULSE_PEOPLE_LEGACY_OPTION").write_text("retired-pulse-extra\n", encoding="utf-8")
    (secrets_dir / "CUSTOM_EXTENSION_TOKEN").write_text("extension-secret\n", encoding="utf-8")
    environ = {
        "OUROBOROS_ENV": "production",
        "OUROBOROS_SECRETS_WAIT_SEC": "0",
    }

    loaded = load_secret_files(
        secrets_dir=secrets_dir,
        environ=environ,
        deploy_env="production",
    )
    settings = bootstrap_config(
        environ=environ,
        secrets_dir=secrets_dir,
        force=True,
    ).settings

    assert loaded == {"CUSTOM_EXTENSION_TOKEN": "extension-secret"}
    assert "HEIMDALL_TOKEN" not in settings
    assert "PULSE_PEOPLE_TOKEN" not in settings
    assert "HEIMDALL_LEGACY_OPTION" not in settings
    assert "PULSE_PEOPLE_LEGACY_OPTION" not in settings
    extras = settings.extra_fields()
    assert "HEIMDALL_TOKEN" not in extras
    assert "PULSE_PEOPLE_TOKEN" not in extras
    assert "HEIMDALL_LEGACY_OPTION" not in extras
    assert "PULSE_PEOPLE_LEGACY_OPTION" not in extras
    assert extras["CUSTOM_EXTENSION_TOKEN"] == "extension-secret"
    persisted = settings.to_persist_dict()
    assert "HEIMDALL_TOKEN" not in persisted
    assert "PULSE_PEOPLE_TOKEN" not in persisted
    assert "HEIMDALL_LEGACY_OPTION" not in persisted
    assert "PULSE_PEOPLE_LEGACY_OPTION" not in persisted
    assert persisted["CUSTOM_EXTENSION_TOKEN"] == "extension-secret"


def test_uppercase_filename_wins_over_lowercase_duplicate(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    secrets_dir = tmp_path / "secrets"
    secrets_dir.mkdir()
    (secrets_dir / "dialogs_bot_token").write_text("from-lower\n", encoding="utf-8")
    (secrets_dir / "DIALOGS_BOT_TOKEN").write_text("from-upper\n", encoding="utf-8")
    monkeypatch.setenv("OUROBOROS_SECRETS_DIR", str(secrets_dir))
    monkeypatch.setenv("OUROBOROS_ENV", "production")

    loaded = load_secret_files()

    assert loaded == {"DIALOGS_BOT_TOKEN": "from-upper"}


def test_invalid_filename_is_ignored(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    secrets_dir = tmp_path / "secrets"
    secrets_dir.mkdir()
    (secrets_dir / "ab").write_text("ignored\n", encoding="utf-8")
    monkeypatch.setenv("OUROBOROS_SECRETS_DIR", str(secrets_dir))
    monkeypatch.setenv("OUROBOROS_ENV", "production")

    loaded = load_secret_files()

    assert loaded == {}


def test_selfhosted_secret_file_is_loaded(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    secrets_dir = tmp_path / "secrets"
    secrets_dir.mkdir()
    (secrets_dir / "selfhosted_llm_api_key").write_text("from-file\n", encoding="utf-8")
    monkeypatch.setenv("OUROBOROS_SECRETS_DIR", str(secrets_dir))
    monkeypatch.setenv("OUROBOROS_ENV", "production")
    monkeypatch.delenv("SELFHOSTED_LLM_API_KEY", raising=False)

    bootstrap_config(force=True)

    assert load_settings().SELFHOSTED_LLM_API_KEY.get_secret_value() == "from-file"


def test_production_prefix_filter_blocks_ignored_prefix(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    secrets_dir = tmp_path / "secrets"
    secrets_dir.mkdir()
    (secrets_dir / "OPENROUTER_API_KEY").write_text("blocked-key\n", encoding="utf-8")
    monkeypatch.setenv("OUROBOROS_SECRETS_DIR", str(secrets_dir))
    monkeypatch.setenv("OUROBOROS_ENV", "production")
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)

    loaded = load_secret_files()

    assert loaded == {}


def test_bootstrap_is_idempotent(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    secrets_dir = tmp_path / "secrets"
    secrets_dir.mkdir()
    (secrets_dir / "DIALOGS_USER_ID").write_text("user-1\n", encoding="utf-8")
    monkeypatch.setenv("OUROBOROS_SECRETS_DIR", str(secrets_dir))
    monkeypatch.setenv("OUROBOROS_ENV", "production")
    monkeypatch.delenv("DIALOGS_USER_ID", raising=False)

    bootstrap_config(force=True)
    (secrets_dir / "DIALOGS_USER_ID").write_text("user-2\n", encoding="utf-8")
    second = bootstrap_config()

    assert load_settings().DIALOGS_USER_ID == "user-1"
    assert second.settings.DIALOGS_USER_ID == "user-1"


def test_wait_skipped_when_secrets_dir_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OUROBOROS_SECRETS_DIR", raising=False)
    monkeypatch.delenv("OUROBOROS_SECRETS_WAIT_SEC", raising=False)

    started = time.monotonic()
    wait_for_secrets_dir()
    bootstrap_config(force=True)
    elapsed = time.monotonic() - started

    assert elapsed < 0.2


def test_wait_polls_for_full_budget_when_dir_exists(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    secrets_dir = tmp_path / "secrets"
    secrets_dir.mkdir()
    (secrets_dir / "DIALOGS_BOT_TOKEN").write_text("token\n", encoding="utf-8")
    monkeypatch.setenv("OUROBOROS_SECRETS_DIR", str(secrets_dir))
    monkeypatch.setenv("OUROBOROS_SECRETS_WAIT_SEC", "0.15")
    monkeypatch.setenv("OUROBOROS_SECRETS_WAIT_INTERVAL_SEC", "0.03")

    started = time.monotonic()
    wait_for_secrets_dir()
    elapsed = time.monotonic() - started

    assert elapsed >= 0.12
    assert elapsed < 0.5


def test_wait_until_dir_appears(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    secrets_dir = tmp_path / "delayed_secrets"
    monkeypatch.setenv("OUROBOROS_SECRETS_DIR", str(secrets_dir))
    monkeypatch.setenv("OUROBOROS_ENV", "production")
    monkeypatch.setenv("OUROBOROS_SECRETS_WAIT_SEC", "0.5")
    monkeypatch.setenv("OUROBOROS_SECRETS_WAIT_INTERVAL_SEC", "0.05")
    monkeypatch.delenv("DIALOGS_BOT_TOKEN", raising=False)

    def _create_dir_later() -> None:
        time.sleep(0.2)
        secrets_dir.mkdir()
        (secrets_dir / "DIALOGS_BOT_TOKEN").write_text("delayed-token\n", encoding="utf-8")

    thread = threading.Thread(target=_create_dir_later, daemon=True)
    started = time.monotonic()
    thread.start()
    bootstrap_config(force=True)
    thread.join(timeout=2)
    elapsed = time.monotonic() - started

    assert elapsed >= 0.15
    assert load_settings().DIALOGS_BOT_TOKEN.get_secret_value() == "delayed-token"


def test_poll_collects_files_mounted_one_by_one(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    secrets_dir = tmp_path / "trickle_secrets"
    secrets_dir.mkdir()
    monkeypatch.setenv("OUROBOROS_SECRETS_DIR", str(secrets_dir))
    monkeypatch.setenv("OUROBOROS_ENV", "production")
    monkeypatch.setenv("OUROBOROS_SECRETS_WAIT_SEC", "0.5")
    monkeypatch.setenv("OUROBOROS_SECRETS_WAIT_INTERVAL_SEC", "0.05")
    monkeypatch.delenv("DIALOGS_BOT_TOKEN", raising=False)
    monkeypatch.delenv("DIALOGS_GRPC_ENDPOINT", raising=False)

    def _mount_files_later() -> None:
        time.sleep(0.05)
        (secrets_dir / "DIALOGS_BOT_TOKEN").write_text("first-token\n", encoding="utf-8")
        time.sleep(0.15)
        (secrets_dir / "DIALOGS_GRPC_ENDPOINT").write_text("grpc://dialogs.example.test\n", encoding="utf-8")

    thread = threading.Thread(target=_mount_files_later, daemon=True)
    thread.start()
    bootstrap_config(force=True)
    thread.join(timeout=2)

    settings = load_settings()
    assert settings.DIALOGS_BOT_TOKEN.get_secret_value() == "first-token"
    assert settings.DIALOGS_GRPC_ENDPOINT == "grpc://dialogs.example.test"


def test_wait_timeout_proceeds_without_secrets(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    missing_dir = tmp_path / "never_mounted"
    monkeypatch.setenv("OUROBOROS_SECRETS_DIR", str(missing_dir))
    monkeypatch.setenv("OUROBOROS_ENV", "production")
    monkeypatch.setenv("OUROBOROS_SECRETS_WAIT_SEC", "0.1")
    monkeypatch.setenv("OUROBOROS_SECRETS_WAIT_INTERVAL_SEC", "0.05")
    monkeypatch.delenv("DIALOGS_BOT_TOKEN", raising=False)

    bootstrap_config(force=True)

    assert load_settings().DIALOGS_BOT_TOKEN.get_secret_value() == ""


def test_wait_sec_zero_disables_polling(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    missing_dir = tmp_path / "never_mounted"
    monkeypatch.setenv("OUROBOROS_SECRETS_DIR", str(missing_dir))
    monkeypatch.setenv("OUROBOROS_SECRETS_WAIT_SEC", "0")

    started = time.monotonic()
    wait_for_secrets_dir()
    bootstrap_config(force=True)
    elapsed = time.monotonic() - started

    assert elapsed < 0.2
