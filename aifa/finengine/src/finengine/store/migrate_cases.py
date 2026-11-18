"""Idempotent Slice-10 schema migration for Case/PlanRun persistence."""
from __future__ import annotations

from finengine.config import settings
from finengine.store.cases import ensure_case_schema


def main() -> int:
    url = (settings.state_database_url or "").strip()
    if not url:
        print("База не настроена: пуст FINENGINE_STATE_DATABASE_URL. Case требует общий Postgres.")
        return 2
    ensure_case_schema(url)
    print("Case/PlanRun schema is ready (idempotent create_all migration).")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
