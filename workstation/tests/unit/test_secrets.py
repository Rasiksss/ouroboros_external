"""Tests for the secman secrets-file loader."""

from __future__ import annotations

import logging
import os
from collections.abc import Iterator
from pathlib import Path

import pytest

from app.core import secrets
from app.core.config import Settings


@pytest.fixture(autouse=True)
def _reset_loader() -> Iterator[None]:
    secrets.secman_secrets.cache_clear()
    yield
    secrets.secman_secrets.cache_clear()


def _use_file(monkeypatch: pytest.MonkeyPatch, path: Path) -> None:
    monkeypatch.setenv("SECMAN_SECRETS_FILE", str(path))


def _write(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, content: str) -> Path:
    secret_file = tmp_path / "workstation-secrets"
    secret_file.write_text(content)
    _use_file(monkeypatch, secret_file)
    return secret_file


def test_missing_file_is_noop(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _use_file(monkeypatch, tmp_path / "absent")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "from-configmap")

    assert secrets.secman_secrets() == {}
    assert Settings().AWS_ACCESS_KEY_ID == "from-configmap"


def test_file_value_overrides_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _write(tmp_path, monkeypatch, "AWS_ACCESS_KEY_ID=from-secman\n")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "from-configmap")

    assert Settings().AWS_ACCESS_KEY_ID == "from-secman"


def test_secrets_never_reach_the_environment(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """The whole point: a child process must not inherit the secrets."""
    _write(tmp_path, monkeypatch, "AWS_ACCESS_KEY_ID=from-secman\nAWS_SECRET_ACCESS_KEY=from-secman-too\n")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "from-configmap")
    monkeypatch.delenv("AWS_SECRET_ACCESS_KEY", raising=False)

    Settings()

    assert os.environ["AWS_ACCESS_KEY_ID"] == "from-configmap"
    assert "AWS_SECRET_ACCESS_KEY" not in os.environ


def test_explicit_kwargs_override_the_file(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _write(tmp_path, monkeypatch, "AWS_ACCESS_KEY_ID=from-secman\n")

    assert Settings(AWS_ACCESS_KEY_ID="explicit").AWS_ACCESS_KEY_ID == "explicit"


def test_empty_value_falls_back_to_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _write(tmp_path, monkeypatch, 'AWS_ACCESS_KEY_ID=\nAWS_SECRET_ACCESS_KEY="  "\nAWS_SESSION_TOKEN=""\n')
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "from-configmap")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "from-configmap-too")
    monkeypatch.setenv("AWS_SESSION_TOKEN", "from-configmap-three")

    local_settings = Settings()

    assert local_settings.AWS_ACCESS_KEY_ID == "from-configmap"
    assert local_settings.AWS_SECRET_ACCESS_KEY == "from-configmap-too"
    assert local_settings.AWS_SESSION_TOKEN == "from-configmap-three"


def test_key_absent_from_env_is_set(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _write(tmp_path, monkeypatch, "AWS_SECRET_ACCESS_KEY=only-in-file\n")
    monkeypatch.delenv("AWS_SECRET_ACCESS_KEY", raising=False)

    assert Settings().AWS_SECRET_ACCESS_KEY == "only-in-file"


def test_json_file_value_overrides_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _write(tmp_path, monkeypatch, '{"AWS_ACCESS_KEY_ID": "from-secman-json", "AWS_SECRET_ACCESS_KEY": 42}')
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "from-configmap")

    local_settings = Settings()

    assert local_settings.AWS_ACCESS_KEY_ID == "from-secman-json"
    assert local_settings.AWS_SECRET_ACCESS_KEY == "42"


def test_json_empty_and_null_fall_back_to_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _write(tmp_path, monkeypatch, '{"AWS_ACCESS_KEY_ID": "", "AWS_SECRET_ACCESS_KEY": null}')
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "from-configmap")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "from-configmap-too")

    local_settings = Settings()

    assert local_settings.AWS_ACCESS_KEY_ID == "from-configmap"
    assert local_settings.AWS_SECRET_ACCESS_KEY == "from-configmap-too"


def test_json_non_object_is_ignored(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _write(tmp_path, monkeypatch, '["AWS_ACCESS_KEY_ID"]')
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "from-configmap")

    assert secrets.secman_secrets() == {}
    assert Settings().AWS_ACCESS_KEY_ID == "from-configmap"


def test_second_call_is_noop(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    secret_file = _write(tmp_path, monkeypatch, "AWS_ACCESS_KEY_ID=first\n")

    secrets.secman_secrets()
    secret_file.write_text("AWS_ACCESS_KEY_ID=second\n")

    assert secrets.secman_secrets() == {"AWS_ACCESS_KEY_ID": "first"}


def test_values_never_logged(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    _write(tmp_path, monkeypatch, "AWS_SECRET_ACCESS_KEY=hunter2-super-secret\n")

    with caplog.at_level(logging.DEBUG):
        secrets.secman_secrets()

    assert "AWS_SECRET_ACCESS_KEY" in caplog.text
    assert "hunter2-super-secret" not in caplog.text
