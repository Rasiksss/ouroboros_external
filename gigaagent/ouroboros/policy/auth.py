"""Network bind helpers.

Primary user authentication is delegated to the ingress front (SberChat /
approved identity gateway); this module does not implement credentials.
"""

from __future__ import annotations

import ipaddress


def is_loopback_host(host: str | None) -> bool:
    text = (host or "").strip().lower()
    if not text:
        return False
    # Normalize bracketed IPv6 literals (e.g. "[::1]" → "::1") so ipaddress can parse them.
    if text.startswith("[") and text.endswith("]"):
        text = text[1:-1]
    if text in {"localhost", "127.0.0.1", "::1"}:
        return True
    try:
        return ipaddress.ip_address(text.split("%", 1)[0]).is_loopback
    except ValueError:
        return False
