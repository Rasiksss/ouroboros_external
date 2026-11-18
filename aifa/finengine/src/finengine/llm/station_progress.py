"""Progress writer for the SEALED Workstation station (#83, Фаза 3).

The sealed station is socketless — it cannot push progress to the backend over
HTTP. Mirroring the model file-RPC (``station_bridge.py``), the station instead
APPENDS structured progress events as NDJSON lines to a per-nonce file in
``FE_RPC_DIR``; a gateway-side daemon (``deploy/station-bridge/bridge.py``) tails
it and relays each line to the backend, which routes it by nonce to the right
user's SSE stream (see ``PHASE2_PROGRESS_DESIGN.md`` — correlation = nonce).

Gated on ``FE_PROGRESS_NONCE``: unset → ``emit`` is a no-op, so the inline/
non-station path (and any script run without a nonce) behaves exactly as before.
Event dicts keep the SAME shape the frontend already renders (plan / plan_step /
metric_row / extract_start / extract_done / …) — the transport changes, not the
protocol.
"""
from __future__ import annotations

import json
import os
import threading

_lock = threading.Lock()


def _nonce() -> str | None:
    n = os.environ.get("FE_PROGRESS_NONCE", "").strip()
    return n or None


def _path() -> str | None:
    n = _nonce()
    if not n:
        return None
    d = os.environ.get("FE_RPC_DIR", "/workspace/.model-rpc")
    return os.path.join(d, f"progress-{n}.ndjson")


def emit(event: dict) -> None:
    """Append one progress event as an NDJSON line. No-op unless FE_PROGRESS_NONCE
    is set. Best-effort: a write failure must never break the engine — progress
    is cosmetic, the final result is returned separately."""
    path = _path()
    if not path or not isinstance(event, dict):
        return
    try:
        d = os.path.dirname(path)
        if d:
            os.makedirs(d, exist_ok=True)
        line = json.dumps(event, ensure_ascii=False, default=str) + "\n"
        # Append under a process lock; O_APPEND keeps concurrent asyncio tasks'
        # lines whole. The daemon reads only complete newline-terminated lines.
        with _lock, open(path, "a", encoding="utf-8") as f:
            f.write(line)
            f.flush()
    except Exception:  # noqa: BLE001 — progress is best-effort, never fatal
        pass


def enabled() -> bool:
    return _nonce() is not None
