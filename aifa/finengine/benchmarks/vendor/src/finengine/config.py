from pathlib import Path
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_prefix="FINENGINE_", extra="ignore")

    # No prefix — reads ANTHROPIC_API_KEY directly
    anthropic_api_key: str = Field("", alias="ANTHROPIC_API_KEY", validation_alias="ANTHROPIC_API_KEY")
    data_dir: Path = Path("./data")
    index_dir: Path = Path("./data/index")
    docs_dir: Path = Path("./data/documents")
    host: str = "0.0.0.0"
    port: int = 8000

    embedding_model: str = "intfloat/multilingual-e5-base"
    embedding_model_family: str = "e5"  # "" or "e5" — e5 family needs "passage:"/"query:" prefix
    top_k: int = 8
    chunk_size: int = 800
    chunk_overlap: int = 100

    claude_model: str = "claude-opus-4-6"

    # OpenRouter settings (for alternative model backends)
    openrouter_api_key: str = Field("", alias="OPENROUTER_API_KEY", validation_alias="OPENROUTER_API_KEY")
    openrouter_base_url: str = "https://openrouter.ai/api/v1"
    openrouter_model: str = Field("qwen/qwen3.6-plus-04-02", alias="FINENGINE_OPENROUTER_MODEL", validation_alias="FINENGINE_OPENROUTER_MODEL")
    classifier_model: str = Field(
        "google/gemma-4-26b-a4b-it",
        alias="FINENGINE_CLASSIFIER_MODEL",
        validation_alias="FINENGINE_CLASSIFIER_MODEL",
    )
    classifier_timeout_seconds: float = Field(
        20.0,
        alias="FINENGINE_CLASSIFIER_TIMEOUT_SECONDS",
        validation_alias="FINENGINE_CLASSIFIER_TIMEOUT_SECONDS",
    )
    # Concurrent extraction/classification requests allowed in flight against
    # the provider. Until 2026-07 an authenticated LLM proxy fronted the
    # provider and queued per user; calling the provider directly means this
    # cap is the only thing keeping a multi-document batch (N documents ×
    # per-document parallelism) from fanning out into rate-limit errors.
    llm_max_in_flight: int = Field(
        8,
        alias="FINENGINE_LLM_MAX_IN_FLIGHT",
        validation_alias="FINENGINE_LLM_MAX_IN_FLIGHT",
    )
    # Pin OpenRouter to a single provider (e.g. "DashScope" for Qwen) so
    # the same call always hits the same backend. Without this, OpenRouter
    # routes across multiple providers and we observed micro-variance in
    # outputs even at temperature=0. Empty = no pinning (default routing).
    openrouter_provider: str = Field("", alias="FINENGINE_OPENROUTER_PROVIDER", validation_alias="FINENGINE_OPENROUTER_PROVIDER")

    # GigaChat — Sber's LLM via the official `gigachat` SDK. Authenticates
    # with a base64-encoded `client_id:secret` issued by Sber's developer
    # portal. Same value as `authorization_key` in AIFA's llm_config.json.
    # `verify_ssl_certs=False` is hardcoded in the wrapper because Sber's
    # endpoint uses their corporate CA chain.
    gigachat_credentials: str = Field("", alias="GIGACHAT_CREDENTIALS", validation_alias="GIGACHAT_CREDENTIALS")
    gigachat_model: str = Field("GigaChat-Max", alias="GIGACHAT_MODEL", validation_alias="GIGACHAT_MODEL")
    # OAuth scope for the GigaChat token. Sber issues different keys per
    # tier — default `GIGACHAT_API_PERS` is for individual sandbox use,
    # `GIGACHAT_API_CORP` for corporate, `GIGACHAT_API_B2B` for legacy
    # legal-entity contracts. AIFA's keys are corporate, so we default
    # to CORP. Override via env if you swap to a personal sandbox key.
    gigachat_scope: str = Field("GIGACHAT_API_CORP", alias="GIGACHAT_SCOPE", validation_alias="GIGACHAT_SCOPE")

    # AIFA baseline calls the IDP-OCR cloud service to convert PDF → markdown
    # tables. Default is the IDP team's TLS endpoint; override per environment
    # via FINENGINE_IDP_OCR_URL if the team rotates the host.
    idp_ocr_url: str = Field(
        "https://idp-ocr.ds.idp-team.ru/predict",
        alias="FINENGINE_IDP_OCR_URL",
        validation_alias="FINENGINE_IDP_OCR_URL",
    )

    # Concurrent per-page POSTs to the IDP-OCR service during ingestion.
    # Bounded by the service's capacity, not local CPU (the calls are I/O,
    # not compute). Default 10; tune via FINENGINE_IDP_OCR_PARALLEL.
    idp_ocr_parallel: int = Field(
        10,
        alias="FINENGINE_IDP_OCR_PARALLEL",
        validation_alias="FINENGINE_IDP_OCR_PARALLEL",
    )

    # Path to Расула's AutoML CatBoost reranker binary used by AIFA baseline
    # to pick top-N candidate tables per metric. Empty = auto-resolve to
    # `Репа в репе/financier-extractor-dev/.../binaries/evolution_no_embs_extra_deep_classifier_evolute_features_automl.cbm`.
    # If the file is missing the reranker silently falls back to the
    # heuristic n-gram + fuzzy scorer.
    aifa_reranker_binary_path: str = Field(
        "",
        alias="FINENGINE_AIFA_RERANKER_BINARY",
        validation_alias="FINENGINE_AIFA_RERANKER_BINARY",
    )

    # Sentry — error tracking. Empty DSN disables the SDK.
    sentry_dsn_backend: str = Field("", alias="SENTRY_DSN_BACKEND", validation_alias="SENTRY_DSN_BACKEND")
    sentry_environment: str = Field("production", alias="SENTRY_ENVIRONMENT", validation_alias="SENTRY_ENVIRONMENT")
    sentry_traces_sample_rate: float = Field(0.1, alias="SENTRY_TRACES_SAMPLE_RATE", validation_alias="SENTRY_TRACES_SAMPLE_RATE")

    # Browser origins allowed to call the FastAPI app with credentials.
    # Local Next.js uses direct localhost:8000 for SSE/status/dialog sync; prod
    # normally goes through /api on the same origin and does not need CORS.
    cors_origins: str = Field(
        "http://localhost:3000,http://127.0.0.1:3000",
        alias="FINENGINE_CORS_ORIGINS",
        validation_alias="FINENGINE_CORS_ORIGINS",
    )

    # Auth — invite-only signup, JWT in HTTP-only cookie.
    # Generate the secret with `python -c "import secrets; print(secrets.token_urlsafe(64))"`.
    auth_secret_key: str = Field("CHANGE-ME-DEV-ONLY-DO-NOT-SHIP", alias="FINENGINE_AUTH_SECRET", validation_alias="FINENGINE_AUTH_SECRET")
    auth_token_ttl_days: int = 30
    auth_cookie_name: str = "finengine_session"
    auth_cookie_secure: bool = Field(True, alias="FINENGINE_AUTH_COOKIE_SECURE", validation_alias="FINENGINE_AUTH_COOKIE_SECURE")
    auth_db_path: Path = Path("./data/users.db")
    # If the users table is empty at startup, create this admin user.
    # Empty FINENGINE_ADMIN_PASSWORD = generate a random one and print to stdout
    # (visible in Dozzle on the very first run).
    admin_username: str = Field("admin", alias="FINENGINE_ADMIN_USERNAME", validation_alias="FINENGINE_ADMIN_USERNAME")
    admin_password: str = Field("", alias="FINENGINE_ADMIN_PASSWORD", validation_alias="FINENGINE_ADMIN_PASSWORD")

    # Current Finengine extraction path. Set false only as an emergency
    # rollback to the pre-matrix bulk_extractor routing.
    extractor_matrix_enabled: bool = Field(
        True,
        alias="FINENGINE_EXTRACTOR_MATRIX",
        validation_alias="FINENGINE_EXTRACTOR_MATRIX",
    )
    # Client role formatters and role-scoped extraction for finengine_dev.
    # `other` intentionally maps to the current generic Finengine DEV behavior.
    role_formatters_enabled: bool = Field(
        True,
        alias="FINENGINE_ROLE_FORMATTERS",
        validation_alias="FINENGINE_ROLE_FORMATTERS",
    )
    # Hidden IFRS-bank/DGR mini-extraction: how many largest borrower economy
    # sectors to keep from concentration tables.
    top_borrowers_count: int = Field(
        5,
        alias="FINENGINE_TOP_BORROWERS_COUNT",
        validation_alias="FINENGINE_TOP_BORROWERS_COUNT",
    )
    # Dev/test bootstrap accounts for role QA. Keep disabled unless an
    # environment explicitly opts in.
    seed_role_test_users: bool = Field(
        False,
        alias="FINENGINE_SEED_ROLE_TEST_USERS",
        validation_alias="FINENGINE_SEED_ROLE_TEST_USERS",
    )
    seed_role_test_password: str = Field(
        "ChangeMe123!",
        alias="FINENGINE_SEED_ROLE_TEST_PASSWORD",
        validation_alias="FINENGINE_SEED_ROLE_TEST_PASSWORD",
    )

    # ── Email file intake ─────────────────────────────────────────────────
    # Users on locked-down work machines can't upload files via the browser;
    # instead they email a PDF to the intake mailbox and the agent picks it up
    # in chat. We poll that mailbox over IMAP — it is the same beget mailbox
    # that already sends access-request notifications over SMTP, just read
    # from instead of written to. Empty user/password = feature disabled (the
    # poller no-ops), which is the default until creds are provided.
    imap_host: str = Field("imap.beget.com", alias="FINENGINE_IMAP_HOST", validation_alias="FINENGINE_IMAP_HOST")
    imap_port: int = Field(993, alias="FINENGINE_IMAP_PORT", validation_alias="FINENGINE_IMAP_PORT")
    imap_user: str = Field("", alias="FINENGINE_IMAP_USER", validation_alias="FINENGINE_IMAP_USER")
    imap_password: str = Field("", alias="FINENGINE_IMAP_PASSWORD", validation_alias="FINENGINE_IMAP_PASSWORD")
    # Address shown to the user to send files to. Falls back to imap_user when
    # empty. May be an IDN (e.g. "infoaifa@аифа.рф") — the API also returns an
    # ASCII/punycode variant for locked-down clients that choke on IDN.
    email_intake_address: str = Field("", alias="FINENGINE_EMAIL_INTAKE_ADDRESS", validation_alias="FINENGINE_EMAIL_INTAKE_ADDRESS")

    def model_post_init(self, __context):
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.index_dir.mkdir(parents=True, exist_ok=True)
        self.docs_dir.mkdir(parents=True, exist_ok=True)


settings = Settings()
