import json
import os

import pytest

from finengine.config import Settings
from finengine.secman import load_secman_file


@pytest.mark.parametrize("as_json", [False, True])
def test_secman_aliases_override_env_without_export(monkeypatch, tmp_path, as_json):
    secrets = {
        "FINENGINE_AUTH_SECRET": "auth-from-secman",
        "OPENROUTER_API_KEY": "model-from-secman",
        "AIFA_ENGINE_TOKEN": "engine-from-secman",
        "FINENGINE_WORKSTATION_CREATE_SECRET": "station-from-secman",
        "FINENGINE_AUTH_DATABASE_URL": "postgresql+psycopg://u:p@db/aifa",
        "FINENGINE_ADMIN_USERNAME": "",  # blank leaves ConfigMap in effect
    }
    path = tmp_path / "secrets"
    path.write_text(
        json.dumps(secrets) if as_json else "\n".join(k + "=" + v for k, v in secrets.items())
    )
    monkeypatch.setenv("SECMAN_SECRETS_FILE", str(path))
    monkeypatch.setenv("FINENGINE_AUTH_SECRET", "env-value")
    monkeypatch.setenv("FINENGINE_ADMIN_USERNAME", "configmap-admin")
    monkeypatch.delenv("AIFA_ENGINE_TOKEN", raising=False)
    cfg = Settings(_env_file=None, data_dir=tmp_path, docs_dir=tmp_path)
    assert cfg.auth_secret_key == "auth-from-secman"
    assert cfg.openrouter_api_key == "model-from-secman"
    assert cfg.aifa_engine_token == "engine-from-secman"
    assert cfg.workstation_create_secret == "station-from-secman"
    assert cfg.auth_database_url == secrets["FINENGINE_AUTH_DATABASE_URL"]
    assert cfg.admin_username == "configmap-admin"
    assert "AIFA_ENGINE_TOKEN" not in os.environ
    assert os.environ["FINENGINE_AUTH_SECRET"] == "env-value"


def test_secret_dollar_sequences_are_literal(monkeypatch, tmp_path):
    path = tmp_path / "secrets"
    path.write_text('OPENROUTER_API_KEY="literal-${HOME}"')
    assert (
        load_secman_file({"SECMAN_SECRETS_FILE": str(path)})["OPENROUTER_API_KEY"]
        == "literal-${HOME}"
    )


def test_explicit_missing_file_fails_closed(tmp_path):
    with pytest.raises(RuntimeError, match="missing or empty"):
        load_secman_file({"SECMAN_SECRETS_FILE": str(tmp_path / "absent"), "SECMAN_WAIT_SEC": "0"})


@pytest.mark.parametrize("content", ['{"bad":', '["bad"]', '{"TOKEN":{"nested":"secret"}}'])
def test_invalid_secret_shape_does_not_echo_values(tmp_path, content):
    path = tmp_path / "secrets"
    path.write_text(content)
    with pytest.raises(ValueError) as error:
        load_secman_file({"SECMAN_SECRETS_FILE": str(path)})
    assert "nested" not in str(error.value)

def test_platform_toml_configures_all_postgres_stores(monkeypatch, tmp_path):
    from sqlalchemy.engine import make_url

    path = tmp_path / "postgres"
    path.write_text("""
[connections.POSTGRES-DB]
dialect = "postgresql"
host = "db"
port = 8000
database = "shared"
username = "aifa"
password = "p@ss:/?#"
defaultSchemaName = "shared-schema"

[connections.object-store]
access_key = "test-access"
secret_key = "test-secret"
""", encoding="utf-8")
    monkeypatch.setenv("SECMAN_SECRETS_FILE", str(path))
    monkeypatch.setenv("FINENGINE_PG_SCHEMA", "old-schema")
    settings = Settings(_env_file=None, data_dir=tmp_path, docs_dir=tmp_path)

    assert settings.pg_schema == "shared-schema"
    assert settings.s3_access_key == "test-access"
    assert settings.s3_secret_key == "test-secret"
    dsn = make_url(settings.auth_database_url)
    assert dsn.drivername == "postgresql+psycopg"
    assert dsn.password == "p@ss:/?#"
    assert dsn.database == "shared"
    assert dsn.port == 8000
    assert dsn.query["options"] == '-csearch_path="shared-schema"'
    for store in ("dialogs", "runs", "documents", "scenarios", "news", "state", "journals", "artifacts"):
        assert getattr(settings, f"{store}_database_url") == settings.auth_database_url

def test_aef_database_filename_and_shared_s3_files(monkeypatch, tmp_path):
    from sqlalchemy.engine import make_url

    path = tmp_path / "databases-aifa-v1"
    path.write_text("""
[connections.POSTGRES-DB]
dialect = "postgresql"
host = "db"
port = 8000
database = "shared"
username = "test-user"
password = "test-password"
defaultSchemaName = "shared-schema"
""", encoding="utf-8")
    (tmp_path / "ouroboros_s3_access_key").write_text("shared-access", encoding="utf-8")
    (tmp_path / "ouroboros_s3_secret_key").write_text("shared-secret", encoding="utf-8")
    monkeypatch.setenv("SECMAN_SECRETS_FILE", str(path))
    settings = Settings(_env_file=None, _secrets_dir=tmp_path, data_dir=tmp_path, docs_dir=tmp_path)
    assert make_url(settings.auth_database_url).host == "db"
    assert settings.pg_schema == "shared-schema"
    assert settings.s3_access_key == "shared-access"
    assert settings.s3_secret_key == "shared-secret"

    (tmp_path / "FINENGINE_S3_ACCESS_KEY").write_text("aifa-access", encoding="utf-8")
    (tmp_path / "FINENGINE_S3_SECRET_KEY").write_text("aifa-secret", encoding="utf-8")
    settings = Settings(_env_file=None, _secrets_dir=tmp_path, data_dir=tmp_path, docs_dir=tmp_path)
    assert settings.s3_access_key == "aifa-access"
    assert settings.s3_secret_key == "aifa-secret"


@pytest.mark.parametrize("use_url", [False, True])
def test_platform_toml_preserves_connection_parameters(tmp_path, use_url):
    from sqlalchemy.engine import make_url

    connection = {
        "dialect": "postgresql+asyncpg", "host": "db", "port": 8000,
        "database": "shared", "username": "app", "password": "p@ss:/?#",
        "defaultSchemaName": "shared-schema",
        "query": "sslmode=verify-full&sslrootcert=%2Fvault%2Fca.pem&connect_timeout=7&options=-cstatement_timeout%3D5000",
    }
    if use_url:
        connection = {
            "url": "postgresql://url-user:p%40ss%3A%2F%3F%23@url-db:8000/url-db?sslmode=verify-full&sslrootcert=%2Fvault%2Fca.pem&connect_timeout=7&options=-cstatement_timeout%3D5000",
            "defaultSchemaName": "shared-schema",
            "query": "sslmode=disable",  # URL wins, as in demo-fastapi-db
        }
    raw = "[connections.POSTGRES-DB]\n" + "\n".join(
        f"{key} = {json.dumps(value)}" for key, value in connection.items()
    )
    path = tmp_path / "databases-aifa-v1"
    path.write_text(raw, encoding="utf-8")
    loaded = load_secman_file({"SECMAN_SECRETS_FILE": str(path)})
    url = make_url(loaded["FINENGINE_AUTH_DATABASE_URL"])
    assert url.drivername == "postgresql+psycopg"
    assert url.password == "p@ss:/?#"
    assert url.host == ("url-db" if use_url else "db")
    assert url.query["sslmode"] == "verify-full"
    assert url.query["sslrootcert"] == "/vault/ca.pem"
    assert url.query["connect_timeout"] == "7"
    assert url.query["options"] == '-cstatement_timeout=5000 -csearch_path="shared-schema"'
    assert len({v for k, v in loaded.items() if k.endswith("_DATABASE_URL")}) == 1


@pytest.mark.parametrize("bad_url", [
    "postgresql://user:private-password@db:invalid/shared",
    "mysql://user:private-password@db/shared",
])
def test_invalid_platform_url_does_not_expose_password(tmp_path, bad_url):
    path = tmp_path / "databases-aifa-v1"
    path.write_text('[connections.POSTGRES-DB]\ndefaultSchemaName = "app"\nurl = ' + json.dumps(bad_url))
    with pytest.raises(ValueError) as error:
        load_secman_file({"SECMAN_SECRETS_FILE": str(path)})
    assert "private-password" not in str(error.value)


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
    assert make_url(loaded["FINENGINE_AUTH_DATABASE_URL"]).password == original
