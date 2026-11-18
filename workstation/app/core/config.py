"""Application settings for the workstation service."""

from __future__ import annotations

from enum import StrEnum
from functools import lru_cache
from os import environ
from typing import Annotated, Any

from pydantic import AliasChoices, AnyHttpUrl, Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.core.secrets import secman_secrets


class RuntimeMode(StrEnum):
    BWRAP = "bwrap"
    LOCAL = "local"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=[path for path in [environ.get("CONFIG"), ".env"] if path],
        env_file_encoding="utf-8",
        case_sensitive=True,
        extra="ignore",
    )

    def __init__(self, **overrides: Any) -> None:
        # secman secrets stay in RAM: passed as init kwargs, which outrank
        # env/.env in pydantic-settings, so they never reach os.environ.
        # Explicit kwargs still win over the secrets file.
        super().__init__(**{**secman_secrets(), **overrides})

    SERVICE_NAME: str = "workstation"
    APP_TITLE: str = "AEF Workstation"
    APP_DESCRIPTION: str = "Сервис изолированных рабочих сессий для выполнения команд и работы с файлами."
    APP_VERSION: str = "0.1.0"
    APP_HOST: str = "0.0.0.0"
    APP_PORT: int = 8080
    APP_WORKERS: int = 1
    APP_IS_RELOAD: bool = False
    APP_LOG_LEVEL: str = "INFO"
    APP_LOG_LEVEL_CORE: str | None = None
    APP_LOG_DIR: str = "/var/log/app"

    AGENT_ID: str | None = None
    AGENT_INSTANCE_ID: str | None = None
    DISTRIBUTIVE: str | None = None
    AI_HUB_ID: str | None = None
    HUB_NAME: str | None = None
    CLUSTER_ID: str | None = None
    POD_NAMESPACE: str | None = None
    POD_NAME: str | None = None
    NAMESPACE: str | None = None
    NODE_NAME: str | None = None

    APP_ROOT_PATH: str = ""
    NODOC: bool = False
    IS_ERR_DETAIL_HIDDEN: bool = False
    BACKEND_CORS_ORIGINS: list[AnyHttpUrl] = Field(default_factory=list)
    TIMEZONE_NAME: str = "UTC"

    DATABASE_URL: str = "sqlite+aiosqlite:////tmp/workstation.db"
    DATABASE_SCHEMA: str = "public"
    DATABASE_ECHO: bool = False
    DATABASE_POOL_SIZE: int = 5
    DATABASE_MAX_OVERFLOW: int = 10

    ROOTFS_TEMPLATE_DIR: str = "/opt/workstation/template"
    ROOTFS_ROOT: str = "/tmp/workstation/sessions"
    SHARED_BIN_DIR: str = "/opt/workstation/bin"
    SANDBOX_BINARY: str = "/usr/local/bin/ws-sandbox"
    NETWORK_ENABLED: bool = False
    BWRAP_BINARY: str = "bwrap"
    SYSTEMD_RUN_BINARY: str = "systemd-run"
    RUNTIME_MODE: RuntimeMode = RuntimeMode.LOCAL
    MAX_FILE_SIZE: int = 20 * 1024 * 1024
    MAX_TOTAL_BYTES: int = 200 * 1024 * 1024
    MEMORY_LIMIT_BYTES: int = 512 * 1024 * 1024
    CPU_QUOTA_PCT: int = 100
    MAX_TASKS: int = 256
    SESSION_MEMORY_MAX: int = 512 * 1024 * 1024
    SESSION_PIDS_MAX: int = 64
    SESSION_CPU_QUOTA_PCT: int = 50
    SESSION_RLIMIT_AS: int = 1 * 1024 * 1024 * 1024
    SESSION_RLIMIT_FSIZE: int = 20 * 1024 * 1024
    SESSION_RLIMIT_CPU_SEC: int = 60
    SESSION_RLIMIT_NOFILE: int = 256
    # UID-wide ceiling on processes. All sessions share the pod UID, so this is
    # the cross-session safety net; per-session enforcement runs in the pgid
    # watchdog.
    SESSION_RLIMIT_NPROC: int = 1024
    SESSION_FILE_WATCHDOG_INTERVAL_SEC: float = 2.0
    EXEC_ALLOWLIST: list[str] = Field(
        default_factory=lambda: [
            # /bin/sh is required because workstation clients wrap commands as
            # ["/bin/sh", "-lc", <user command>]. Without it every exec falls
            # back to a policy denial before the wrapper runs.
            "/bin/sh",
            "/bin/bash",
            "/bin/cat",
            "/bin/ls",
            "/bin/pwd",
            "/bin/grep",
            "/bin/mkdir",
            "/bin/touch",
            "/bin/echo",
            "/bin/python3",
            "/bin/python",
            "/bin/unrar",
            "/bin/7za",
        ]
    )
    EXEC_TIMEOUT_SEC: int = 300
    EXEC_OUTPUT_LIMIT_BYTES: int = 1 * 1024 * 1024
    SIGTERM_GRACE_SEC: int = 3
    SESSION_IDLE_TIMEOUT_SEC: int = 900
    SESSION_ABSOLUTE_TTL_SEC: int = 3600
    SESSION_TOUCH_DEBOUNCE_SEC: int = 5
    # Session create is the only route reachable without a session token, so it is the
    # only one a shared secret can guard. Off by default: deployments that never set
    # the secret keep working unchanged.
    IS_CREATE_SESSION_SECRET: bool = False
    SESSION_CREATE_SECRET: str | None = None
    AUDIT_ENABLED: bool = False
    CLEANUP_INTERVAL_SEC: int = 60
    WORKSPACE_STORE_ENABLED: bool = False
    WORKSPACE_STORE_BACKEND: str = "s3"
    WORKSPACE_STORE_S3_BUCKET: str | None = None
    WORKSPACE_STORE_S3_PREFIX: str = "workstation/workspaces"
    WORKSPACE_STORE_S3_ENDPOINT_URL: str | None = None
    WORKSPACE_STORE_RESTORE_TIMEOUT_SEC: int = 300
    WORKSPACE_STORE_SNAPSHOT_TIMEOUT_SEC: int = 300
    # S3 credentials arrive in the secman file and are handed to boto3
    # explicitly: it would otherwise look them up in os.environ, where secrets
    # deliberately no longer are. Unset means "use boto3's own chain"
    # (instance role, ~/.aws), so credential-less deployments keep working.
    AWS_ACCESS_KEY_ID: str | None = Field(default=None, validation_alias=AliasChoices("AWS_ACCESS_KEY_ID", "access_key"))
    AWS_SECRET_ACCESS_KEY: str | None = Field(default=None, validation_alias=AliasChoices("AWS_SECRET_ACCESS_KEY", "secret_key"))
    AWS_SESSION_TOKEN: str | None = None
    AWS_REGION: str | None = None
    ENV_ALLOWLIST: list[str] = Field(
        # Real clients pass HOME, PWD, TERM, USER and an OPENCLAW_* marker so
        # the spawned shell behaves predictably. Rejecting them surfaces as
        # `environment keys are not allowed` and forces every caller to
        # re-tune workstation's policy, so accept them by default.
        default_factory=lambda: [
            "PATH",
            "LANG",
            "LC_ALL",
            "HOME",
            "PWD",
            "TERM",
            "USER",
            "OPENCLAW_SHELL",
        ]
    )
    DENY_COMMAND_TOKENS: list[str] = Field(default_factory=lambda: ["sudo", "mount", "iptables", "docker", "podman"])
    METRICS_ENABLED: bool = True

    @field_validator("BACKEND_CORS_ORIGINS", mode="before")
    @classmethod
    def _split_csv_origins(cls, v: str | list[str]) -> list[str] | str:
        if isinstance(v, str) and not v.startswith("["):
            return [origin.strip() for origin in v.split(",") if origin.strip()]
        return v

    @model_validator(mode="after")
    def _require_secret_when_enforced(self) -> Settings:
        """Refuse to start enforcing a secret that was never delivered.

        A missing key in the secman file would otherwise leave session create wide open
        while the config still claims it is guarded, which is indistinguishable from
        working protection until someone probes the route.
        """
        if self.IS_CREATE_SESSION_SECRET and not (self.SESSION_CREATE_SECRET or "").strip():
            raise ValueError("IS_CREATE_SESSION_SECRET=true requires a non-empty SESSION_CREATE_SECRET")
        return self

    @field_validator("ENV_ALLOWLIST", "DENY_COMMAND_TOKENS", "EXEC_ALLOWLIST", mode="before")
    @classmethod
    def _split_csv_values(cls, v: str | list[str]) -> list[str] | str:
        if isinstance(v, str) and not v.startswith("["):
            return [item.strip() for item in v.split(",") if item.strip()]
        return v


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


settings: Annotated[Settings, "global singleton"] = get_settings()
