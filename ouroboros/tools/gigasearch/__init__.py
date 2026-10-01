import httpx

from new_core.tools.context import ToolContext
from new_core.tools.entry import ToolEntry
from new_core.tools.schema import ToolSchema
from ouroboros.storage.settings.schema import AppSettings
from ouroboros.storage.settings.store import load_settings
from ouroboros.tools.gigasearch.dlp import DlpClient, DlpError
from ouroboros.tools.gigasearch.gigasearch import GigaSearch, GigaSearchError

DEFAULT_BASE_URL = "https://gigachat.devices.sberbank.ru"
DEFAULT_TIMEOUT_SEC = 30.0
DEFAULT_TOTAL_TIMEOUT_SEC = 180
DEFAULT_LIMIT = 10
MAX_LIMIT = 100
MAX_QUERY_LENGTH = 2048


def _resolve_settings(settings: AppSettings) -> tuple[str, str, str, float, str, str, str, bool]:
    base_url = (settings.GIGASEARCH_BASE_URL or DEFAULT_BASE_URL).rstrip("/")
    surface_id = (settings.GIGASEARCH_SURFACE_ID or "").strip()
    user_id = (settings.GIGASEARCH_USER_ID or "").strip()
    try:
        timeout = float(settings.GIGASEARCH_TIMEOUT_SEC or DEFAULT_TIMEOUT_SEC)
    except (TypeError, ValueError):
        timeout = DEFAULT_TIMEOUT_SEC
    return (
        base_url,
        surface_id,
        user_id,
        timeout,
        (settings.GIGASEARCH_CERT_PATH or "").strip(),
        (settings.GIGASEARCH_KEY_PATH or "").strip(),
        (settings.GIGASEARCH_CA_PATH or "").strip(),
        bool(settings.GIGASEARCH_VERIFY_SSL),
    )


def _resolve_dlp(settings: AppSettings) -> DlpClient:
    """Build the DLP gate from settings; without DLP_BASE_URL the search must not happen."""
    base_url = settings.DLP_BASE_URL.strip().rstrip("/")
    if not base_url:
        raise DlpError("DLP is not configured; set DLP_BASE_URL before using gigasearch")
    return DlpClient(
        base_url=base_url,
        mapping_schema=settings.DLP_MAPPING_SCHEMA.strip(),
        sender_email=settings.DLP_SENDER_EMAIL.strip(),
        recipient_email=settings.DLP_RECIPIENT_EMAIL.strip(),
        server_ip=settings.DLP_SERVER_IP.strip(),
        client_ip=settings.DLP_CLIENT_IP.strip(),
        subject=settings.DLP_SUBJECT.strip(),
        content_type=settings.DLP_CONTENT_TYPE.strip(),
        allow=settings.DLP_ALLOW.strip(),
        timeout=settings.DLP_TIMEOUT_SEC,
    )


def _gigasearch_handler(ctx: ToolContext, query: str, limit: int = DEFAULT_LIMIT) -> str:
    del ctx
    settings = load_settings()
    base_url, surface_id, user_id, timeout, cert_path, key_path, ca_path, verify_ssl = _resolve_settings(settings)

    try:
        limit = int(limit)
    except (TypeError, ValueError):
        limit = DEFAULT_LIMIT

    try:
        _resolve_dlp(settings).check(query)
        giga_search = GigaSearch(
            base_url=base_url,
            timeout=timeout,
            max_query_len=MAX_QUERY_LENGTH,
            max_limit=MAX_LIMIT,
            surface_id=surface_id,
            user_id=user_id,
            cert_path=cert_path,
            key_path=key_path,
            ca_path=ca_path,
            verify_ssl=verify_ssl,
        )
        # Search mode and scopes are fixed per current task scope: always advanced, all sources.
        return giga_search.execute(query, limit, search_mode="advanced", scopes=None)
    except (DlpError, GigaSearchError, httpx.RequestError) as exc:
        return f"⚠️ TOOL_ERROR (gigasearch): {exc}"


def get_tools() -> list[ToolEntry]:
    return [
        ToolEntry(
            name="gigasearch",
            schema=ToolSchema(
                name="gigasearch",
                description=("Search the web via GigaSearch using advanced mode and all available sources."),
                parameters={
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": f"Search query text. Length from 1 to {MAX_QUERY_LENGTH} characters.",
                        },
                        "limit": {
                            "type": "integer",
                            "description": f"Number of documents. Default {DEFAULT_LIMIT}, range 1-{MAX_LIMIT}.",
                        },
                    },
                    "required": ["query"],
                },
            ),
            handler=_gigasearch_handler,
            timeout_sec=DEFAULT_TOTAL_TIMEOUT_SEC,
        ),
    ]
