import importlib.util
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from ouroboros.storage.db_url import collect_runtime_db_urls, postgres_connect_kwargs
from ouroboros.storage.settings.bootstrap import bootstrap_config, reset_config_for_tests
from ouroboros.storage.settings.secman import load_secman_file


@pytest.fixture(autouse=True)
def reset_config():
    reset_config_for_tests()
    yield
    reset_config_for_tests()


@pytest.mark.parametrize("as_json", [False, True])
def test_secman_configmap_precedence_and_provider_keys(tmp_path, as_json):
    secrets = {
        "WORKSTATION_CREATE_SECRET": "station-secret",
        "AIFA_ENGINE_TOKEN": "engine-secret",
        "CLOUDRU_FOUNDATION_MODELS_API_KEY": "provider-secret",
        "OUROBOROS_RUNTIME_DB_URL": "postgresql+psycopg://u:p@db/agent",
        "AIFA_ENGINE_URL": "",  # leave endpoint from ConfigMap
    }
    path = tmp_path / "secrets"
    path.write_text(json.dumps(secrets) if as_json else "\n".join(k + "=" + v for k, v in secrets.items()))
    before = dict(os.environ)
    cfg = bootstrap_config(
        environ={
            "OUROBOROS_ENV": "production",
            "SECMAN_SECRETS_FILE": str(path),
            "AIFA_ENGINE_URL": "http://aifa:8000",
            "AIFA_ENGINE_TOKEN": "old-env-token",
        }
    ).settings
    assert cfg.WORKSTATION_CREATE_SECRET.get_secret_value() == "station-secret"
    assert cfg.AIFA_ENGINE_TOKEN.get_secret_value() == "engine-secret"
    assert cfg.CLOUDRU_FOUNDATION_MODELS_API_KEY.get_secret_value() == "provider-secret"
    assert cfg.AIFA_ENGINE_URL == "http://aifa:8000"
    assert "engine-secret" not in repr(cfg)
    assert os.environ == before


def test_platform_toml_provides_runtime_db_url(tmp_path):
    path = tmp_path / "databases-gigaagent-v1"
    path.write_text(
        '[connections.POSTGRES-DB]\n'
        'dialect = "postgresql+asyncpg"\n'
        'host = "db.example"\n'
        'port = "8000"\n'
        'database = "test_db"\n'
        'username = "test-user"\n'
        'password = "p%@:/?#"\n'
        'defaultSchemaName = "Dev-Agent"\n'
        '[connections.object-store]\n'
        'accessKey = "test-access"\n'
        'secretKey = "test-secret"\n',
        encoding="utf-8",
    )
    settings = bootstrap_config(environ={"SECMAN_SECRETS_FILE": str(path)}).settings
    assert settings.OUROBOROS_S3_ACCESS_KEY.get_secret_value() == "test-access"
    assert settings.OUROBOROS_S3_SECRET_KEY.get_secret_value() == "test-secret"
    (url,) = collect_runtime_db_urls(settings)
    assert postgres_connect_kwargs(url) == {
        "host": "db.example",
        "port": 8000,
        "dbname": "test_db",
        "user": "test-user",
        "password": "p%@:/?#",
        "options": '-csearch_path="Dev-Agent"',
    }


def test_explicit_missing_file_fails_closed(tmp_path):
    with pytest.raises(RuntimeError, match="missing or empty"):
        load_secman_file({"SECMAN_SECRETS_FILE": str(tmp_path / "absent"), "SECMAN_WAIT_SEC": "0"})


@pytest.mark.asyncio
async def test_aifa_plugin_uses_secman_token_without_environment(monkeypatch, tmp_path):
    path = tmp_path / "secrets"
    path.write_text("AIFA_ENGINE_TOKEN=only-in-secman")
    bootstrap_config(environ={"SECMAN_SECRETS_FILE": str(path), "AIFA_ENGINE_URL": "http://aifa:8000"})
    monkeypatch.delenv("AIFA_ENGINE_TOKEN", raising=False)
    plugin_path = Path(__file__).resolve().parents[1] / "skills/aifa-engine/plugin.py"
    spec = importlib.util.spec_from_file_location("secman_engine_plugin", plugin_path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(200, text="OK")

    ctx = SimpleNamespace(
        scope=SimpleNamespace(owner_id="owner", agent_id="agent", chat_id="chat"),
        task_id="task",
        root_task_id="task",
        task_depth=0,
        transport={},
        trace_id="trace",
        stop_requested=lambda: False,
    )
    result = await module.EngineCall("aifa_state", transport=httpx.MockTransport(respond))(ctx)
    assert result == "OK"
    assert requests[0].headers["Authorization"] == "Bearer only-in-secman"
    assert requests[0].url == "http://aifa:8000/internal/ouro/v1/aifa_state"


@pytest.mark.parametrize("encoding", ["raw", "url"])
def test_platform_password_encoding_is_explicit_and_decoded_once(tmp_path, encoding):
    from urllib.parse import quote
    from sqlalchemy.engine import make_url

    original = "test@Pass+word/{~%40"
    stored = quote(original, safe="") if encoding == "url" else original
    path = tmp_path / "databases-test"
    fields = {
        "dialect": "postgresql", "host": "db", "port": 8000,
        "database": "app", "username": "test", "password": stored,
        "defaultSchemaName": "app",
    }
    path.write_text("[connections.POSTGRES-DB]\n" + "\n".join(
        f"{k} = {json.dumps(v)}" for k, v in fields.items()
    ), encoding="utf-8")
    loaded = load_secman_file({
        "SECMAN_SECRETS_FILE": str(path),
        "SECMAN_POSTGRES_PASSWORD_ENCODING": encoding,
    })
    assert make_url(loaded["OUROBOROS_RUNTIME_DB_URL"]).password == original
