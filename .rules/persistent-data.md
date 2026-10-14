---
paths:
  - "infra/migrations/**"
---

# Persistent data and migrations — read before editing

Alembic lives in `infra/migrations/` (`alembic.ini` sets `script_location`).

**Rule:** a persistent data shape change is never local. Grep every reader *and* every writer of the
shape, then update all of them, their tests, and `docs/ARCHITECTURE.md` §Data layout in the same
change. A migration that lands without its readers updated is a broken deploy, not a partial one.

**Rule:** a revision is written against PostgreSQL and nothing else — the suite runs the dialect the
fleet runs. Where a column has a server default that says the same thing, name fewer columns rather
than more.
