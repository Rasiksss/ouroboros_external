"""GigaSearch v3 external search transport."""

import json
import uuid
from typing import Any

import httpx

MAX_ERROR_BODY_CHARS = 2_000


class GigaSearchError(Exception):
    """Invalid search input, TLS configuration, or HTTP response."""


class GigaSearch:
    def __init__(
        self,
        base_url: str,
        timeout: float,
        max_query_len: int,
        max_limit: int,
        surface_id: str,
        user_id: str,
        cert_path: str = "",
        key_path: str = "",
        ca_path: str = "",
        verify_ssl: bool = True,
    ) -> None:
        self._base_url = base_url
        self._timeout = timeout
        self._max_query_len = max_query_len
        self._max_limit = max_limit
        self._surface_id = surface_id
        self._user_id = user_id
        self._cert_path = cert_path
        self._key_path = key_path
        self._ca_path = ca_path
        self._verify_ssl = verify_ssl

    def execute(self, query: str, limit: int, search_mode: str, scopes: list[str] | None = None) -> str:
        self._validate_query_len(query)
        self._validate_limit(limit)

        body: dict[str, Any] = {
            "query": query,
            "limit": limit,
            "search_mode": search_mode,
        }
        if scopes:
            body["scopes"] = scopes

        headers = {
            "X-Request-ID": str(uuid.uuid4()),
            "X-Surface-ID": self._surface_id,
            "X-User-ID": self._user_id,
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        client_kwargs: dict[str, Any] = {
            "timeout": self._timeout,
            "headers": headers,
            "verify": False if not self._verify_ssl else (self._ca_path or True),
        }
        if bool(self._cert_path) != bool(self._key_path):
            raise GigaSearchError("GIGASEARCH_CERT_PATH and GIGASEARCH_KEY_PATH must be set together")
        if self._cert_path and self._key_path:
            client_kwargs["cert"] = (self._cert_path, self._key_path)

        with httpx.Client(**client_kwargs) as client:
            response = client.post(f"{self._base_url}/v3/search", json=body)
            payload = self._response_json(response)
            if not response.is_success:
                detail = self._bounded_detail(payload, response.text)
                raise GigaSearchError(f"HTTP {response.status_code}: {detail}")
        return json.dumps(payload, ensure_ascii=False)

    @staticmethod
    def _response_json(response: httpx.Response) -> Any:
        try:
            return response.json()
        except ValueError as exc:
            if response.is_success:
                raise GigaSearchError("invalid JSON response from GigaSearch") from exc
            return None

    @staticmethod
    def _bounded_detail(payload: Any, response_text: str) -> str:
        if payload is not None:
            detail = json.dumps(payload, ensure_ascii=False)
        else:
            detail = str(response_text or "").strip() or "empty response body"
        if len(detail) > MAX_ERROR_BODY_CHARS:
            return detail[:MAX_ERROR_BODY_CHARS] + "... (truncated)"
        return detail

    def _validate_query_len(self, query: str) -> None:
        if len(query) == 0:
            raise GigaSearchError("query must not be empty")
        if len(query) > self._max_query_len:
            raise GigaSearchError(f"query exceeds maximum length of {self._max_query_len} characters")

    def _validate_limit(self, limit: int) -> None:
        if limit < 1 or limit > self._max_limit:
            raise GigaSearchError(f"limit must be between 1 and {self._max_limit}")
