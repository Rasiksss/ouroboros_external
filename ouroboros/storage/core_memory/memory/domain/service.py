"""Unified memory domain service — the public durable-memory API.

All business rules live here: name validation, the kind policy, description
requirements, the append separator rule, timestamp bookkeeping, per-kind
capacity pruning, identity seeding, frontmatter rebuild on read, and index
rendering. Persistence is delegated to an ``IMemory`` port so the same rules run
over SQLite, PostgreSQL, or a test fake.
"""

from __future__ import annotations

import time

from ouroboros.storage.core_memory.memory.domain.errors import MemoryNotFoundError
from ouroboros.storage.core_memory.memory.domain.models import (
    IDENTITY_DESCRIPTION,
    IDENTITY_MAX_CHARS,
    IDENTITY_NAME,
    KIND_OWNED_NAMES,
    KIND_POLICIES,
    MAX_DESCRIPTION_CHARS,
    PROMPT_MEMORY_STATUSES,
    WRITE_MODES,
    MemoryHeader,
    effective_injection,
    effective_policy,
    policy_for,
)
from ouroboros.storage.core_memory.memory.domain.render import (
    IndexSection,
    build_frontmatter,
    is_rendered_memory_header,
    render_index,
    sanitize_owner,
    validate_name,
)
from ouroboros.storage.core_memory.memory.interfaces.memory import IMemory, MemoryRow


class Memory:
    """Durable named memory of every kind.

    The production path uses ``owner_id="global"``; the parameter remains for
    explicit reads in tests and maintenance tools.
    """

    def __init__(self, repo: IMemory, owner_id: str = "global"):
        self._repo = repo
        self._owner = sanitize_owner(owner_id)

    # ----------------------------------------------------------------- writing

    def save(
        self,
        name: str,
        kind: str,
        description: str,
        content: str,
        *,
        mode: str = "overwrite",
        source: str = "",
        recorded_at: float | None = None,
    ) -> str:
        """Write one memory and prune its kind back to capacity. Returns the name.

        ``mode`` is ``overwrite`` or ``append``; append joins the new content to
        the existing body inside one transaction, so concurrent appenders
        serialize. ``source`` marks how the memory was created ("distilled" for
        auto-extracted entries). ``recorded_at`` sets the first-recorded stamp
        for new memories only.
        """
        safe = validate_name(name)
        # The effective policy, not the static one: it is the single place the
        # identity self-edit setting is read, so the tool enum, the prompt, and
        # this refusal can never disagree about what may be written.
        policy = effective_policy(kind)
        if not policy.writable:
            raise ValueError(f"Memory kind {policy.kind!r} cannot be written through the memory tools.")
        if mode not in WRITE_MODES:
            raise ValueError(f"Invalid mode {mode!r}. Use 'overwrite' or 'append'.")

        if policy.kind == "identity" and safe != IDENTITY_NAME:
            raise ValueError(
                f"Memory kind 'identity' is stored under the single name {IDENTITY_NAME!r}; {safe!r} cannot be used."
            )

        desc = " ".join(str(description or "").split())[:MAX_DESCRIPTION_CHARS]
        if policy.kind == "identity":
            if mode != "overwrite":
                raise ValueError("The identity document is replaced whole. Use mode='overwrite'.")
            # ``memory_read('identity')`` returns the body under a rebuilt
            # header. Refuse to store that rendering as the document: stripping
            # it would silently truncate a body whose own frontmatter used the
            # same shape, and there is no previous version to fall back on.
            body = str(content or "").strip()
            if is_rendered_memory_header(body):
                raise ValueError(
                    "The content starts with a rendered memory header. "
                    "Send the document body only, without the `---` block."
                )
            if not body:
                raise ValueError("The identity document cannot be empty.")
            if len(body) > IDENTITY_MAX_CHARS:
                raise ValueError(
                    f"The identity document is limited to {IDENTITY_MAX_CHARS} characters; got {len(body)}."
                )
            content = body
            desc = IDENTITY_DESCRIPTION
        if not desc:
            raise ValueError("A one-line description is required for every memory.")

        now = time.time()
        # The up-front read gates the protected-name refusal only. Provenance is
        # read again under the write lock, inside the callback below, so an
        # overwrite carries forward the row as it stands when the write commits —
        # not the row as it looked before any concurrent save.
        existing = self._repo.fetch(self._owner, safe)
        _refuse_protected(existing, safe, "written", requested_kind=policy.kind)

        def _row(current: MemoryRow | None, content_text: str) -> MemoryRow:
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
        if mode == "append":

            def _write(current: MemoryRow | None) -> MemoryRow:
                base = str(current.content if current else "")
                # One newline, and only when the body does not already end with one.
                if base and not base.endswith("\n"):
                    base += "\n"
                return _row(current, base + suffix)
        else:

            def _write(current: MemoryRow | None) -> MemoryRow:
                return _row(current, suffix.strip() + "\n")

        # Write and prune in one (owner, kind)-serialized transaction. The
        # template is the row a PostgreSQL adapter materializes when the name is
        # absent: the same row an empty-body create would write, so writing over
        # it and writing over nothing agree (see IMemory.write_and_prune). The
        # prune callback selects the eviction; the port lists and archives.
        self._repo.write_and_prune(
            self._owner,
            policy.kind,
            safe,
            _write,
            _row(None, ""),
            lambda headers: self._select_overflow(policy.kind, headers),
            statuses=PROMPT_MEMORY_STATUSES,
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
            owner_id=self._owner,
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

    def delete(self, name: str) -> bool:
        """Delete one memory. Returns True if it existed."""
        safe = validate_name(name)
        _refuse_protected(self._repo.fetch(self._owner, safe), safe, "deleted")
        return self._repo.delete(self._owner, safe)

    # ----------------------------------------------------------------- reading

    def read(self, name: str) -> tuple[str, float, float]:
        """Return (content, first_recorded_at, updated_at). Raises MemoryNotFoundError if missing.

        Content is returned with YAML frontmatter rebuilt from the row, so
        tool-visible memory text keeps the shape callers already parse.
        """
        safe = validate_name(name)
        row = self._repo.fetch(self._owner, safe)
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

    def fetch(self, name: str) -> MemoryRow | None:
        """Return the raw row, or None."""
        return self._repo.fetch(self._owner, validate_name(name))

    def list_headers(self) -> list[MemoryHeader]:
        """Return every header including archived, newest first. Never loads bodies.

        This is the audit listing. Everything that feeds the prompt wants
        :meth:`list_prompt_headers` instead.
        """
        return self._repo.list_headers(self._owner)

    def list_prompt_headers(self) -> list[MemoryHeader]:
        """Return the headers eligible to reach a prompt, newest first.

        The status predicate lives here and only here — recall, the index
        sections, the body sections, and capacity pruning all read the same
        listing rather than each re-deciding what "active" means. Archived rows
        are excluded by the store, so a table that has been pruned for a year
        does not make every prompt build scan a year of tombstones.
        """
        return self._repo.list_headers(self._owner, statuses=PROMPT_MEMORY_STATUSES)

    def mark_recalled(self, names: list[str]) -> None:
        """Update recall counters for the given memories."""
        wanted = sorted({str(name).strip() for name in names if str(name or "").strip()})
        if not wanted:
            return
        self._repo.bump_recall(self._owner, wanted, time.time())

    # ---------------------------------------------------------------- identity

    def load_identity(self) -> str:
        """Return the identity content, seeding the default when the row is absent.

        A present memory is returned verbatim — including an empty one, which
        stays empty. Only absence triggers the write, and that write goes
        through the transactional path, so two processes seeding at once
        converge on one row and both see the same content.
        """
        existing = self._repo.fetch(self._owner, IDENTITY_NAME)
        if existing is not None:
            return str(existing.content or "")

        policy = policy_for("identity")
        now = time.time()
        seed_row = MemoryRow(
            owner_id=self._owner,
            name=IDENTITY_NAME,
            kind=policy.kind,
            description=IDENTITY_DESCRIPTION,
            content=policy.seed,
            status="active",
            first_recorded_at=now,
            updated_at=now,
        )
        observed: list[str] = []

        def _seed(current: MemoryRow | None) -> MemoryRow | None:
            if current is not None:
                # Another process won the race; its content is what both return.
                observed.append(str(current.content or ""))
                return None
            observed.append(policy.seed)
            return seed_row

        self._repo.revise(self._owner, IDENTITY_NAME, _seed, seed_row)
        return observed[0] if observed else policy.seed

    # ------------------------------------------------------------------- index

    def render_index(self, section: IndexSection) -> tuple[str, bool]:
        """Render one index section over the current prompt-eligible headers."""
        return render_index(self.list_prompt_headers(), section)

    # --------------------------------------------------------------- lifecycle

    def _select_overflow(self, kind: str, headers: list[MemoryHeader]) -> list[str]:
        """Return the names to archive to bring ``kind`` back to its capacity.

        This is the prune policy, and the whole of it: the port hands it the
        prompt-eligible headers — newest-first, the port's order — from inside the
        write transaction, and it names the victims. The listing and the archival
        happen in the port (``write_and_prune``), so the eviction rides the same
        transaction as the write it followed.

        The limit comes from the kind's policy, so kinds cannot crowd each other
        out and adding a kind does not mean finding this function. Body-injected
        rows (identity) carry no index line and are excluded from eviction here;
        their kind's capacity is 1 and never overflows anyway. The backfill
        applies the same rule (``0006._apply_capacity``).
        """
        capacity = policy_for(kind).capacity
        active = [header for header in headers if header.kind == kind]
        overflow = len(active) - capacity
        if overflow <= 0:
            return []
        # The listing is newest-first, so the tail is the oldest.
        evictable = [header for header in active if effective_injection(header.kind) != "body"]
        return [header.name for header in evictable[-overflow:]]


def _refuse_protected(
    existing: MemoryRow | None,
    name: str,
    verb: str,
    *,
    requested_kind: str = "",
) -> None:
    """Refuse to touch a protected memory — by reserved name first, by row second.

    Names are globally unique, so without this ``memory_save(name='identity',
    kind='project')`` would replace the identity row through the front door.

    The reserved-name check deliberately does not consult ``existing``: a name
    in ``KIND_OWNED_NAMES`` belongs to its kind whether or not the row is there.
    Deciding from the fetch alone left the reservation conditional on the row —
    with identity missing, a save under ``project`` was accepted, and
    ``load_identity`` then returned that body as the identity and let it be
    deleted again. Startup seeds identity before any tool runs, so reaching that
    state took a recovery or a restore; the invariant should not depend on which
    of the two ran first.

    The writability question goes to :func:`effective_policy`, so the setting
    that permits an identity rewrite is not then refused here. That opens a hole
    the cross-kind guard closes: with the setting on, ``memory_save(
    name='identity', memory_type='project')`` would otherwise pass the writable
    check on the *identity* policy and land the identity row under ``project`` —
    deletable, evictable, and injected by that kind's discipline instead. The
    row would still be the one ``load_identity`` reads.

    That guard keys on the reservation, not on the row it found, for the reason
    the kind lookup above does: a name owned by a kind may be written only as
    that kind, at any setting value, row or no row.

    Only *owned* names are locked this way. An ordinary memory may still change
    its kind by being saved again under a different one.
    """
    owned_kind = KIND_OWNED_NAMES.get(name, "")
    kind = owned_kind or (existing.kind if existing is not None else "")
    if kind not in KIND_POLICIES:
        return
    policy = effective_policy(kind)
    if verb == "written" and owned_kind and requested_kind and requested_kind != owned_kind:
        raise ValueError(f"Memory {name!r} is of kind {owned_kind!r} and cannot be written as {requested_kind!r}.")
    if not (policy.writable if verb == "written" else policy.deletable):
        raise ValueError(f"Memory {name!r} is of kind {policy.kind!r} and cannot be {verb} through the tools.")


__all__ = ["Memory", "MemoryNotFoundError"]
