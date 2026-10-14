"""Emergency kill-switch for the agent loop.

Two independent off-switches, either trips the agent:

1. Env var `FINENGINE_AGENT_KILL_SWITCH=1` — set at deploy/restart time.
2. Sentinel file `<data_dir>/KILL_SWITCH` — flippable at runtime without
   a container restart. Reason is whatever's in the file body.

Both are checked on every agent-loop entry (`query_stream`) AND inside
`tools.execute()` — so even if a stream is mid-flight when ops trips the
switch, the next tool call is refused.

Per УКИИ-АУА-N (Sber regulatory framework): an autonomous agent must
have a centrally-controlled stop. This is that stop.
"""
from __future__ import annotations

import os
from finengine.config import settings


def _sentinel_path():
    return settings.data_dir / "KILL_SWITCH"


def is_killed() -> tuple[bool, str | None]:
    """Returns (killed, reason). Reason is best-effort human text."""
    if os.environ.get("FINENGINE_AGENT_KILL_SWITCH"):
        return True, "agent disabled via env var FINENGINE_AGENT_KILL_SWITCH"
    p = _sentinel_path()
    if p.exists():
        try:
            body = p.read_text(encoding="utf-8").strip()
        except OSError:
            body = ""
        return True, body or f"agent disabled via {p}"
    return False, None
