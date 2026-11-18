"""Read SecMan settings without exporting secrets to child processes."""

from __future__ import annotations

import io
import json
import os
import re
import time
import tomllib
from collections.abc import Mapping
from pathlib import Path
from urllib.parse import unquote

from dotenv import dotenv_values
from sqlalchemy.engine import URL

from ouroboros.storage.db_url import RUNTIME_DB_URL_KEY

_POSTGRES_TABLE = re.compile(r"^\s*\[\s*connections\s*\.\s*POSTGRES-DB\s*\]", re.MULTILINE)


def _s3_pairs(table: Mapping):
    """Find explicit S3 credential pairs in the platform TOML tables."""
    access = table.get("access_key", table.get("accessKey"))
    secret = table.get("secret_key", table.get("secretKey"))
    if isinstance(access, str) and access.strip() and isinstance(secret, str) and secret.strip():
        yield access, secret
    for child in table.values():
        if isinstance(child, dict):
            yield from _s3_pairs(child)


def _postgres_settings(raw: str, path: Path, *, password_encoding: str = "raw") -> dict[str, str]:
    if password_encoding not in {"raw", "url"}:
        raise ValueError("SECMAN_POSTGRES_PASSWORD_ENCODING must be raw or url")
    try:
        values = tomllib.loads(raw)
    except tomllib.TOMLDecodeError:
        raise ValueError(f"Invalid PostgreSQL TOML in SecMan file: {path}") from None

    connection = values["connections"]["POSTGRES-DB"]
    for field in ("dialect", "host", "database", "username", "password"):
        value = connection.get(field)
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"Missing or invalid {field} in SecMan connections.POSTGRES-DB")
    if connection["dialect"] not in {"postgresql", "postgresql+asyncpg", "postgresql+psycopg"}:
        raise ValueError("Unsupported dialect in SecMan connections.POSTGRES-DB")
    port = connection.get("port")
    if not re.fullmatch(r"[0-9]{1,5}", str(port)) or not 1 <= int(port) <= 65535:
        raise ValueError("Invalid port in SecMan connections.POSTGRES-DB")
    schema = connection.get("defaultSchemaName", "")
    if not isinstance(schema, str) or (schema and not re.fullmatch(r"[A-Za-z0-9_-]{1,63}", schema)):
        raise ValueError("Invalid defaultSchemaName in SecMan connections.POSTGRES-DB")

    # Runtime uses psycopg, including for async runtime engines.
    url = URL.create(
        "postgresql+psycopg",
        username=connection["username"],
        password=(unquote(connection["password"], errors="strict")
                      if password_encoding == "url" else connection["password"]),
        host=connection["host"],
        port=int(port),
        database=connection["database"],
        query={"options": f'-csearch_path="{schema}"'} if schema else {},
    )
    settings = {RUNTIME_DB_URL_KEY: url.render_as_string(hide_password=False)}
    pairs = set(_s3_pairs(values))
    if len(pairs) > 1:
        raise ValueError("Multiple distinct S3 credential pairs in SecMan TOML")
    if pairs:
        access, secret = pairs.pop()
        settings["OUROBOROS_S3_ACCESS_KEY"] = access
        settings["OUROBOROS_S3_SECRET_KEY"] = secret
    return settings


def load_secman_file(environ: Mapping[str, str] | None = None) -> dict[str, str]:
    source = os.environ if environ is None else environ
    filename = source.get("SECMAN_SECRETS_FILE", "").strip()
    if not filename:
        return {}
    path = Path(filename)
    try:
        wait_sec = float(source.get("SECMAN_WAIT_SEC", "30"))
        if not 0 <= wait_sec <= 300:
            raise ValueError
    except ValueError:
        raise ValueError("SECMAN_WAIT_SEC must be between 0 and 300") from None
    deadline = time.monotonic() + wait_sec
    while not path.is_file() or path.stat().st_size == 0:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise RuntimeError(f"SecMan file is missing or empty: {path}")
        time.sleep(min(0.5, remaining))
    try:
        raw = path.read_text(encoding="utf-8-sig")
    except OSError:
        raise RuntimeError(f"Cannot read SecMan file: {path}") from None
    if _POSTGRES_TABLE.search(raw):
        return _postgres_settings(
            raw, path, password_encoding=source.get("SECMAN_POSTGRES_PASSWORD_ENCODING", "raw")
        )
    if raw.lstrip().startswith(("{", "[")):
        try:
            values = json.loads(raw)
        except ValueError:
            raise ValueError(f"Invalid JSON in SecMan file: {path}") from None
        if not isinstance(values, dict):
            raise ValueError("SecMan JSON must be a flat object")
    else:
        # Never expand ${ENV} inside a secret. The supplied bytes are the value.
        values = dotenv_values(stream=io.StringIO(raw), interpolate=False)
    result: dict[str, str] = {}
    for key, value in values.items():
        if value is None:
            continue
        if isinstance(value, (dict, list)):
            raise ValueError("SecMan JSON must contain scalar values")
        text = value if isinstance(value, str) else json.dumps(value)
        if text.strip():
            result[str(key)] = text
    return result
