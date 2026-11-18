"""Secrets from the secman-delivered file.

secman mounts a secrets file into the pod — either dotenv-formatted
(`KEY=value` lines) or a flat JSON object; the format is auto-detected. Its
non-empty values are kept in process memory and handed to the `Settings`
classes as init kwargs, which outrank env/`.env` in pydantic-settings. Nothing
is written to `os.environ`: secrets there would show up in
`/proc/<pid>/environ` and be inherited by every child process, including the
sandboxed session commands this service spawns.

Consumers that read credentials from the environment themselves (boto3) must be
handed the values explicitly — see `app/services/workspace_persistence.py`.

Resulting priority: secman file (non-empty values) > configmap env vars >
CONFIG/.env file > defaults in code.
"""

from __future__ import annotations

import io
import json
import logging
import os
from functools import lru_cache
from pathlib import Path

from dotenv import dotenv_values

DEFAULT_SECRETS_FILE = "/vault/secrets/secret-files/workstation-secrets"

logger = logging.getLogger(__name__)


@lru_cache(maxsize=1)
def secman_secrets() -> dict[str, str]:
    """Return the non-empty values of the secman file, kept in memory only.

    Empty values (`KEY=`) are skipped so the configmap value stays in effect; a
    missing file yields an empty mapping (local dev). Cached: the file is read
    once per process.
    """
    path = Path(os.environ.get("SECMAN_SECRETS_FILE", DEFAULT_SECRETS_FILE))
    if not path.is_file():
        return {}
    try:
        raw = path.read_text()
    except OSError:
        logger.warning("secman secrets file %s exists but could not be read", path, exc_info=True)
        return {}

    try:
        data = json.loads(raw)
    except ValueError:
        values = dotenv_values(stream=io.StringIO(raw))
    else:
        if not isinstance(data, dict):
            logger.warning("secman secrets file %s is JSON but not an object; ignoring", path)
            return {}
        values = {
            key: value if isinstance(value, str) else json.dumps(value)
            for key, value in data.items()
            if value is not None
        }

    applied = {key: value for key, value in values.items() if value and value.strip()}
    if applied:
        logger.info("loaded %d secret(s) from %s: %s", len(applied), path, ", ".join(sorted(applied)))
    return applied
