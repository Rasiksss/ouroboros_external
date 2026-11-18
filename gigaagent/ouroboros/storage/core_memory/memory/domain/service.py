"""Unified memory domain service — the public durable-memory API.

All business rules live here: name validation, the kind policy, description
requirements, the append separator rule, timestamp bookkeeping, per-kind
capacity pruning, frontmatter rebuild on read, and index
rendering. Persistence is delegated to an ``IMemory`` port so the same rules run
over PostgreSQL or a test fake.
"""

from __future__ import annotations

import math
import time

from new_core.identity import OwnerScope
from ouroboros.storage.core_memory.memory.domain.errors import MemoryConflictError, MemoryNotFoundError
from ouroboros.storage.core_memory.memory.domain.models import (
    MAX_DESCRIPTION_CHARS,
    PROMPT_MEMORY_STATUSES,
    WRITE_MODES,
    MemoryHeader,
    policy_for,
)
from ouroboros.storage.core_memory.memory.domain.render import (
    IndexSection,
    build_frontmatter,
    render_index,
    validate_name,
)
from ouroboros.storage.core_memory.memory.interfaces.memory import IMemory, MemoryRow


class Memory:
    """Durable named memory of every kind.

    Stateless with respect to identity: every method takes the ``OwnerScope`` of the
    run asking, and hands it to the port. Binding an owner to the service instead
    would let one cached service answer for whoever built it first.
    """

    def __init__(self, repo: IMemory):
        self._repo = repo

    # ----------------------------------------------------------------- writing

    def save(
        self,
        scope: OwnerScope,
        name: str,
        kind: str,
        description: str,
        content: str,
        *,
        mode: str = "overwrite",
        source: str = "",
        recorded_at: float | None = None,
        expected_updated_at: float | None = None,
        task_id: str = "",
        claim_token: str = "",
    ) -> str:
        """Write and prune atomically; append merges the body, overwrite replaces it."""
        safe = validate_name(name)
        policy = policy_for(kind)
        if mode not in WRITE_MODES:
            raise ValueError(f"Invalid mode {mode!r}. Use 'overwrite' or 'append'.")

        desc = " ".join(str(description or "").split())[:MAX_DESCRIPTION_CHARS]
        if not desc:
            raise ValueError("A one-line description is required for every memory.")

        def _row(current: MemoryRow | None, content_text: str) -> MemoryRow:
            now = time.time()
            if current is not None:
                now = max(now, math.nextafter(float(current.updated_at), math.inf))
            return self._merge(
                current,
                name=safe,
                kind=policy.kind,
                description=desc,
                content=content_text,
                now=now,
                source=source,
                recorded_at=recorded_at,
            )

        suffix = str(content or "")

        def _check(current: MemoryRow | None) -> None:
            if expected_updated_at is not None:
                expected = float(expected_updated_at)
                actual = float(current.updated_at) if current is not None else None
                if actual != expected:
                    raise MemoryConflictError(safe, expected, actual)

        if mode == "append":

            def _write(current: MemoryRow | None) -> MemoryRow:
                _check(current)
                base = str(current.content if current else "")
                # One newline, and only when the body does not already end with one.
                if base and not base.endswith("\n"):
                    base += "\n"
                return _row(current, base + suffix)
        else:

            def _write(current: MemoryRow | None) -> MemoryRow:
                _check(current)
                return _row(current, suffix.strip() + "\n")

        # The callback merges under the same transaction as its capacity prune.
        self._repo.write_and_prune(
            scope,
            policy.kind,
            safe,
            _write,
            lambda headers: self._select_overflow(policy.kind, headers),
            statuses=PROMPT_MEMORY_STATUSES,
            task_id=task_id,
            claim_token=claim_token,
        )
        return safe

    def _merge(
        self,
        existing: MemoryRow | None,
        *,
        name: str,
        kind: str,
        description: str,
        content: str,
        now: float,
        source: str,
        recorded_at: float | None,
    ) -> MemoryRow:
        """Build the row to persist, carrying provenance forward from ``existing``."""
        if existing is not None:
            first_recorded_at = float(existing.first_recorded_at or existing.updated_at or now)
        else:
            first_recorded_at = float(recorded_at if recorded_at is not None else now)
        return MemoryRow(
            name=name,
            kind=kind,
            description=description,
            content=content,
            status="active",
            source=source,
            first_recorded_at=first_recorded_at,
            updated_at=now,
            last_recalled_at=float(existing.last_recalled_at if existing else 0.0),
            recall_count=int(existing.recall_count if existing else 0),
        )

    def delete(self, scope: OwnerScope, name: str, *, task_id: str = "", claim_token: str = "") -> bool:
        """Delete one memory. Returns True if it existed."""
        safe = validate_name(name)
        return self._repo.delete(scope, safe, task_id=task_id, claim_token=claim_token)

    # ----------------------------------------------------------------- reading

    def read(self, scope: OwnerScope, name: str) -> tuple[str, float, float]:
        """Return (content, first_recorded_at, updated_at). Raises MemoryNotFoundError if missing.

        Content is returned with YAML frontmatter rebuilt from the row, so
        tool-visible memory text keeps the shape callers already parse.
        """
        safe = validate_name(name)
        row = self._repo.fetch(scope, safe)
        if row is None:
            raise MemoryNotFoundError(safe)
        first_recorded_at = float(row.first_recorded_at or row.updated_at or 0.0)
        updated_at = float(row.updated_at or first_recorded_at)
        text = (
            build_frontmatter(
                row.name,
                row.kind,
                row.description,
                source=row.source,
                recorded_at=first_recorded_at,
                updated_at=updated_at,
            )
            + str(row.content or "").strip()
            + "\n"
        )
        return text, first_recorded_at, updated_at

    def fetch(self, scope: OwnerScope, name: str) -> MemoryRow | None:
        """Return the raw row, or None."""
        return self._repo.fetch(scope, validate_name(name))

    def list_headers(self, scope: OwnerScope) -> list[MemoryHeader]:
        """Return every header including archived, newest first. Never loads bodies.

        This is the audit listing. Everything that feeds the prompt wants
        :meth:`list_prompt_headers` instead.
        """
        return self._repo.list_headers(scope)

    def list_prompt_headers(self, scope: OwnerScope) -> list[MemoryHeader]:
        """Return the headers eligible to reach a prompt, newest first.

        The status predicate lives here and only here — recall, the index
        sections and capacity pruning all read the same
        listing rather than each re-deciding what "active" means. Archived rows
        are excluded by the store, so a table that has been pruned for a year
        does not make every prompt build scan a year of tombstones.
        """
        return self._repo.list_headers(scope, statuses=PROMPT_MEMORY_STATUSES)

    def mark_recalled(self, scope: OwnerScope, names: list[str]) -> None:
        """Update recall counters for the given memories."""
        wanted = sorted({str(name).strip() for name in names if str(name or "").strip()})
        if not wanted:
            return
        self._repo.bump_recall(scope, wanted, time.time())

    # ------------------------------------------------------------------- index

    def render_index(self, scope: OwnerScope, section: IndexSection) -> tuple[str, bool]:
        """Render one index section over the current prompt-eligible headers."""
        return render_index(self.list_prompt_headers(scope), section)

    # --------------------------------------------------------------- lifecycle

    def _select_overflow(self, kind: str, headers: list[MemoryHeader]) -> list[str]:
        """Archive the oldest active rows exceeding this kind's capacity."""
        capacity = policy_for(kind).capacity
        active = [header for header in headers if header.kind == kind]
        overflow = len(active) - capacity
        if overflow <= 0:
            return []
        return [header.name for header in active[-overflow:]]


__all__ = ["Memory", "MemoryNotFoundError"]
