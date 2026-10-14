"""Dozor Mailfilter DLP verdict for outbound search queries."""

import base64
from collections.abc import Callable
from datetime import datetime
from typing import Any
from uuid import uuid4

import httpx

from ouroboros.observability.aef_tracing import with_agent_header, with_trace_header


def _encode_subject(subject: str) -> str:
    return "=?UTF-8?B?" + base64.b64encode(subject.encode()).decode() + "?="


class DlpError(Exception):
    """Blocked verdict or a failed DLP exchange; the search call must not happen."""


class DlpClient:
    def __init__(
        self,
        base_url: str,
        mapping_schema: str,
        sender_email: str,
        recipient_email: str,
        server_ip: str,
        client_ip: str,
        subject: str,
        content_type: str,
        allow: str,
        timeout: float,
        client_factory: Callable[..., httpx.Client] | None = None,
    ) -> None:
        self._base_url = base_url
        self._mapping_schema = mapping_schema
        self._sender_email = sender_email
        self._recipient_email = recipient_email
        self._server_ip = server_ip
        self._client_ip = client_ip
        self._subject = subject
        self._content_type = content_type
        self._allow = allow
        self._timeout = timeout
        self._client_factory = client_factory or httpx.Client

    def check(self, query: str) -> None:
        """Raise DlpError unless Mailfilter returns the "ok" verdict for the query."""
        with self._client_factory(timeout=self._timeout, headers=self._headers()) as client:
            response = client.post(f"{self._base_url}/filter", content=query.encode())
        # With the Allow: 204 header Dozor replies without a body: 204 for the "ok"
        # verdict, 403 for a blocked query.
        if response.status_code == httpx.codes.NO_CONTENT:
            return
        if response.status_code == httpx.codes.FORBIDDEN:
            raise DlpError(self._block_reason({}))
        if response.status_code != httpx.codes.OK:
            raise DlpError(f"DLP check failed: HTTP {response.status_code}")
        try:
            verdict = response.json()
        except ValueError as exc:
            raise DlpError("invalid JSON response from DLP") from exc

        result = verdict.get("result") if isinstance(verdict, dict) else None
        if result == "ok":
            return
        if result == "block":
            raise DlpError(self._block_reason(verdict))
        raise DlpError(f"unexpected DLP verdict: {result!r}")

    def _headers(self) -> dict[str, str]:
        # `with_trace_header` omits the header when no identifier is active, which is also
        # Dozor's rule: it rejects a request carrying an empty header value.
        headers = with_agent_header(
            with_trace_header(
                {
                    "X-Message-Date": datetime.now().astimezone().isoformat(timespec="seconds"),
                    "X-Message-ID": uuid4().hex,
                }
            )
        )
        configured = {
            "Allow": self._allow,
            "Content-Type": self._content_type,
            "X-Subject": _encode_subject(self._subject) if self._subject else "",
            "X-Dozor-Mapping-Schema": self._mapping_schema,
            "X-Application": self._mapping_schema,
            "X-Sender-User-Email": self._sender_email,
            "X-Recipient-User-Email": self._recipient_email,
            "X-Server-IP": self._server_ip,
            "X-Client-IP": self._client_ip,
        }
        # Dozor rejects requests with empty header values, so unset settings are omitted.
        headers.update({name: value for name, value in configured.items() if value})
        return headers

    @staticmethod
    def _block_reason(verdict: dict[str, Any]) -> str:
        rule_names = [name for match in verdict.get("matches") or [] if (name := (match.get("rule") or {}).get("name"))]
        reason = "search query blocked by DLP policy"
        if rule_names:
            reason += f" (rules: {', '.join(rule_names)})"
        return (
            f"{reason}; the verdict is final: do not retry, rephrase or split the query, "
            "tell the user this cannot be searched"
        )
