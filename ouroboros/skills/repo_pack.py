"""Shared path policy for skill payload hashing and repo pack collection."""

from __future__ import annotations

SENSITIVE_EXTENSIONS = frozenset(
    {
        ".env",
        ".pem",
        ".key",
        ".p12",
        ".pfx",
        ".jks",
        ".keystore",
        ".kdbx",
        ".gpg",
        ".asc",
    }
)

SENSITIVE_NAMES = frozenset(
    {
        ".env",
        ".env.local",
        ".env.production",
        ".env.staging",
        ".env.development",
        ".env.dev",
        ".env.test",
        ".env.example",
        "credentials.json",
        "service-account.json",
        "secrets.yaml",
        "secrets.json",
        "secrets.toml",
        "secrets.ini",
        "aws-credentials.json",
        "gcp-service-account.json",
        "id_rsa",
        "id_ed25519",
        "id_ecdsa",
        "id_dsa",
        ".git-credentials",
        ".netrc",
        ".npmrc",
        ".pypirc",
    }
)


def is_sensitive_path(path_name: str) -> bool:
    lowered = str(path_name or "").lower()
    if lowered in SENSITIVE_NAMES:
        return True
    return any(lowered.endswith(ext) for ext in SENSITIVE_EXTENSIONS)
