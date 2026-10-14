"""Compatibility import for Gateway Boundary HTTP + WebSocket contracts.

The canonical SSOT moved to ``transport.browser.contracts`` in Gateway
Boundary v1. This module remains so older skills/tests importing
``ouroboros.contracts.api_v1`` keep working while new code imports from the
gateway package directly.
"""

from __future__ import annotations

from transport.browser.contracts import *  # noqa: F403
from transport.browser.contracts import __all__  # noqa: F401
