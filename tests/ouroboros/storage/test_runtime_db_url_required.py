from __future__ import annotations

import os
import pathlib
import subprocess
import sys

import pytest

from ouroboros.storage.db_url import RUNTIME_DB_URL_KEY
from ouroboros.storage.engine_setup import runtime_engine_urls
from ouroboros.storage.runtime_persistence import create_runtime_persistence
from tests.helpers.composition_roots import START_UP_ROOTS, called_names_in_order
from tests.helpers.parametrize import parametrize_from_dict

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[3]
_NOTHING_CONFIGURED = "no runtime database configured"


def _alembic_ini_in(directory: pathlib.Path) -> pathlib.Path:
    ini = directory / "alembic.ini"
    ini.write_text(
        (_REPO_ROOT / "alembic.ini")
        .read_text()
        .replace(
            "script_location = infra/migrations",
            f"script_location = {_REPO_ROOT}/infra/migrations",
        )
        .replace("prepend_sys_path = .", f"prepend_sys_path = {_REPO_ROOT}")
    )
    return ini


def _env_without_a_configured_url() -> dict[str, str]:
    return {key: value for key, value in os.environ.items() if not key.startswith(RUNTIME_DB_URL_KEY)}


def test_an_unconfigured_runtime_database_is_refused_by_the_setting_it_names() -> None:
    with pytest.raises(ValueError) as raised:
        runtime_engine_urls([])

    message = str(raised.value)
    assert _NOTHING_CONFIGURED in message
    assert RUNTIME_DB_URL_KEY in message
    assert f"{RUNTIME_DB_URL_KEY}_1..N" in message
    assert "://" not in message


def test_composing_a_store_without_a_configured_url_refuses_by_name() -> None:
    with pytest.raises(ValueError) as raised:
        create_runtime_persistence(configured_urls=[])

    assert _NOTHING_CONFIGURED in str(raised.value)
    assert RUNTIME_DB_URL_KEY in str(raised.value)


def test_the_schema_tool_refuses_instead_of_creating_a_database(tmp_path: pathlib.Path) -> None:
    working_directory = tmp_path / "cwd"
    working_directory.mkdir()
    ini = _alembic_ini_in(working_directory)

    finished = subprocess.run(
        [sys.executable, "-m", "alembic", "-c", str(ini), "upgrade", "head"],
        cwd=str(working_directory),
        env=_env_without_a_configured_url(),
        capture_output=True,
        text=True,
        timeout=180,
    )

    reported = finished.stdout + finished.stderr
    assert finished.returncode != 0, reported
    assert _NOTHING_CONFIGURED in reported
    assert RUNTIME_DB_URL_KEY in reported
    assert sorted(path.name for path in working_directory.iterdir()) == ["alembic.ini"], reported


@parametrize_from_dict({root: {"root": root} for root in START_UP_ROOTS})
def test_the_database_refusal_is_made_before_the_mode_is_consulted(root: str) -> None:
    calls = called_names_in_order(root)

    assert "refuse_without_runtime_database" in calls, root
    assert "validate_multiuser_configuration" in calls, root
    assert calls.index("refuse_without_runtime_database") < calls.index("validate_multiuser_configuration"), root
    assert calls.index("refuse_without_runtime_database") < calls.index("validate_production_storage"), root
    assert calls.index("validate_production_storage") < calls.index("validate_multiuser_configuration"), root


def test_a_root_refuses_the_missing_database_rather_than_the_mode_it_would_have_checked(
    tmp_path: pathlib.Path,
) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    environment = _env_without_a_configured_url()
    environment.pop("OUROBOROS_S3_BUCKET", None)
    environment["OUROBOROS_MULTIUSER"] = "true"
    environment["OUROBOROS_ENV"] = "production"
    environment["OUROBOROS_DATA_DIR"] = str(data_dir)

    finished = subprocess.run(
        [
            sys.executable,
            "-c",
            "from multiuser_config.config_registry.app import build_registry_app; build_registry_app()",
        ],
        cwd=str(_REPO_ROOT),
        env=environment,
        capture_output=True,
        text=True,
        timeout=180,
    )

    reported = finished.stdout + finished.stderr
    assert finished.returncode != 0, reported
    assert _NOTHING_CONFIGURED in reported
    assert RUNTIME_DB_URL_KEY in reported
    assert "OUROBOROS_S3_BUCKET" not in reported
    assert "://" not in reported.splitlines()[-1]
    assert list(data_dir.rglob("*")) == [], reported


def test_the_configured_url_and_not_the_data_directory_decides_which_database_answers(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    postgres_task_scope,
) -> None:
    import uuid

    from ouroboros.storage.runtime_models import TaskRequest

    urls, scope = postgres_task_scope
    monkeypatch.setenv("OUROBOROS_DATA_DIR", str(tmp_path / "first"))
    written = create_runtime_persistence(configured_urls=urls)
    task_id = f"data-dir-{uuid.uuid4().hex}"
    written.tasks_repo.create_task(TaskRequest(id=task_id, text="work", scope=scope.to_dict()))

    monkeypatch.setenv("OUROBOROS_DATA_DIR", str(tmp_path / "second"))
    answering = create_runtime_persistence(configured_urls=urls)

    assert answering.tasks_repo.get_task(task_id) is not None
    assert answering.db_urls == written.db_urls
    assert list(tmp_path.rglob("*.sqlite3")) == []
