---
paths:
  - "ouroboros/observability/**"
  - "ouroboros/storage/probe_database.py"
  - "supervisor/events.py"
---

# Logging, events, health, traces — read before editing

| Concern | Owner |
|---|---|
| Logging, events, health, pricing, traces | `ouroboros/observability/` |
| Supervisor-emitted events | `supervisor/events.py` |

**Rules:**

- Use project logging, never `print` or a second logger. Never log secrets or raw auth payloads. Mark unavoidable truncation explicitly.
- An event shape change updates every producer and consumer together.
- The probes' one asymmetry is deliberate: `readiness` is a coroutine (dials the runtime DB on its own connection); `startup` and `liveness` are synchronous (read memory, may never wait). Never `asyncio.wait_for` — it awaits the cancellation it requested (hang).
- Every exit hands the dial to `_close_behind` in one `finally`. Closing the connection is what ends the wait — a cancel does not (psycopg sends a cancel request and waits it out).
- The order matters: `cancel()`, one `sleep(0)`, `invalidate()`, then `cancel()` again.
- A peer holding the socket open and answering nothing keeps one connection per probe; `/ready` is polled for as long as the outage lasts.
- `dispose()` cancels nothing still in flight — the worker tiers dispose from a fresh loop after the probe server's own loop is closed.
- A readiness failure answers with `DATABASE_UNAVAILABLE`; the driver's message (host, port, user) goes to the log — this probe is unauthenticated.
- Redaction is a debt ratchet owned by `docs/DEVELOPMENT.md` §Known Debt Ratchets. Short form: add no new redaction site.
