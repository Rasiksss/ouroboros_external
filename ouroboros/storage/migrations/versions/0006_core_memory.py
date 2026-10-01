"""Core-memory tables: one unified ``memory`` table plus scratchpad working memory.

``memory`` replaces three legacy stores at once — the ``memory/global/*`` fact
blobs, the ``memory/knowledge/*.md`` topic files, and the ``memory/identity.md``
document — because they were always the same shape: a named markdown body under
an owner, written by upsert, surfaced through a rendered index. Each row carries
the ``kind`` its source implies. ``scratchpad_blocks`` replaces
``memory/scratchpad_blocks.json`` and stays its own table: it is short-term
working memory with no natural key and its own FIFO discipline.

The same ``upgrade()`` creates each table and copies any pre-existing blobs into
it, leaving the files untouched as a rollback net. The copies are mandatory: any
failure to read the storage backend or parse a payload aborts the migration. A
*missing* object is not an error — a fresh deployment has nothing to copy, and a
name the manifest lists but whose body was deleted contributes no row.

Names are globally unique across kinds, so a fact and a topic that share a name
are a genuine conflict. Every legacy source is read exactly once, into one
snapshot; the preflight enumerates collisions in it and aborts before a single
table is created, and the copy inserts that same snapshot without suppressing
conflicts. Renaming the loser would leave a memory the agent cannot address by
the name it knows, which is indistinguishable from data loss.

**Deployment requirement: no legacy writer may be serving traffic while this
revision runs.** The snapshot closes the window *between* two reads, but not the
one after the single read: a memory an old pod writes to the blob store once the
snapshot is taken is simply not copied, and after the cutover nothing reads the
blobs again. Drain or stop the previous version before the upgrade rather than
overlapping them; ``scripts/memory_name_collisions.py`` reports what the
migration will see, and is only meaningful against a quiesced deployment.

The blobs are read through the readers below rather than through
``StorageProvider``, whose S3 implementation reports a fetch failure as ``None``
— indistinguishable from an absent object. That ambiguity turned a partial S3
outage into a *successful* migration that copied nothing, stamped the revision,
and never ran again. These readers separate the two cases and raise on the
second.

One thing here is not core memory: ``ChatHistoryVisibilityBackfill`` repairs
``messages.chat_history_visible``, which 0004 added with a ``true`` default and
never backfilled. It rides along in this revision because 0004 is already
applied everywhere, so a correction placed there would never run again.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import pathlib
import re
from typing import Any

import sqlalchemy as sa
from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None

log = logging.getLogger(__name__)

# Frozen copies of the storage layout, like the parsing helpers further down:
# the migration must keep reading the layout that existed on the day it ran, so
# it imports nothing from ouroboros.storage.blob. Getting the S3 key wrong here
# would be silent — a bad prefix lists nothing and the backfill "succeeds" with
# zero rows — so tests/test_core_memory_backfill_s3.py pins it to
# ``build_object_key``.
_SCOPE_MEMORY = "memory"
_ABSENT_S3_CODES = frozenset({"404", "NoSuchKey", "NotFound"})


class _FileBlobReader:
    """Legacy blobs under ``{data_dir}/memory/``.

    Absent means the path does not exist. Every other ``OSError`` propagates: a
    permission or I/O error is not an empty deployment.
    """

    def __init__(self, data_dir: pathlib.Path) -> None:
        self.root = pathlib.Path(data_dir).resolve() / _SCOPE_MEMORY

    def get_text(self, key: str) -> str | None:
        try:
            return (self.root / key).read_text(encoding="utf-8")
        except FileNotFoundError:
            return None

    def list_keys(self, prefix: str) -> list[str]:
        search_root = self.root / prefix if prefix else self.root
        if not search_root.is_dir():
            return []
        return sorted(p.relative_to(self.root).as_posix() for p in search_root.rglob("*") if p.is_file())


class _S3BlobReader:
    """Legacy blobs as objects under the tenant prefix.

    Only ``404``/``NoSuchKey``/``NotFound`` mean absent. Permission, encryption
    key, throttling, and transport errors raise, and a failed listing is never
    swallowed into an empty one.
    """

    def __init__(
        self,
        *,
        bucket: str,
        region: str = "",
        endpoint_url: str = "",
        access_key: str = "",
        secret_key: str = "",
    ) -> None:
        self.bucket = str(bucket or "").strip()
        if not self.bucket:
            raise ValueError("OUROBOROS_S3_BUCKET is required for the s3 storage backend")
        self._region = region
        self._endpoint_url = endpoint_url
        self._access_key = access_key
        self._secret_key = secret_key
        self._client_cache: Any = None

    def _client(self) -> Any:
        if self._client_cache is None:
            import boto3
            from botocore.config import Config

            client_kwargs: dict[str, Any] = {"config": Config(signature_version="s3v4")}
            if self._region:
                client_kwargs["region_name"] = self._region
            if self._endpoint_url:
                client_kwargs["endpoint_url"] = self._endpoint_url
            if self._access_key and self._secret_key:
                client_kwargs["aws_access_key_id"] = self._access_key
                client_kwargs["aws_secret_access_key"] = self._secret_key
            self._client_cache = boto3.client("s3", **client_kwargs)
        return self._client_cache

    @staticmethod
    def scope_prefix() -> str:
        """``tenants/{tenant}/{prefix}/memory/`` — resolved per call, as the settings are."""
        from ouroboros.storage.settings.store import load_settings

        settings = load_settings()
        tenant = str(settings.OUROBOROS_STORAGE_TENANT_ID or "").strip() or "default"
        extra = str(settings.OUROBOROS_S3_PREFIX or "").strip().strip("/")
        base = f"tenants/{tenant}"
        if extra:
            base = f"{base}/{extra}"
        return f"{base}/{_SCOPE_MEMORY}/"

    def object_key(self, key: str) -> str:
        rel = str(key or "").replace("\\", "/").strip().lstrip("/")
        return f"{self.scope_prefix()}{rel}"

    def get_text(self, key: str) -> str | None:
        try:
            response = self._client().get_object(Bucket=self.bucket, Key=self.object_key(key))
        except Exception as exc:
            code = getattr(exc, "response", {}).get("Error", {}).get("Code", "")
            if code in _ABSENT_S3_CODES:
                return None
            raise
        body = response.get("Body")
        if body is None:
            return None
        return body.read().decode("utf-8")

    def list_keys(self, prefix: str) -> list[str]:
        scope_prefix = self.scope_prefix()
        list_prefix = f"{scope_prefix}{prefix}" if prefix else scope_prefix
        keys: list[str] = []
        continuation: str | None = None
        while True:
            kwargs: dict[str, Any] = {"Bucket": self.bucket, "Prefix": list_prefix}
            if continuation:
                kwargs["ContinuationToken"] = continuation
            # Deliberately unguarded: a listing failure must abort the migration
            # rather than read as "this prefix holds nothing".
            response = self._client().list_objects_v2(**kwargs)
            for item in response.get("Contents") or []:
                full_key = str(item.get("Key") or "")
                if full_key.startswith(scope_prefix):
                    keys.append(full_key[len(scope_prefix) :])
            if not response.get("IsTruncated"):
                break
            continuation = response.get("NextContinuationToken")
        return sorted(keys)


def _table_exists(table_name: str) -> bool:
    """True when a previous aborted run left this table behind.

    A raise inside ``upgrade()`` rolls back the inserted rows and the
    ``alembic_version`` stamp together, which is what makes fail-closed safe —
    but only on a backend with transactional DDL. SQLAlchemy's pysqlite driver
    autocommits ``CREATE TABLE``, so on SQLite an aborted run leaves the tables
    in place, empty, while the stamp reverts. Without this check the retry would
    die on "table already exists" and the backfill could never complete.
    Creating only what is missing keeps the restart a complete retry on both
    dialects.
    """
    return sa.inspect(op.get_bind()).has_table(table_name)


def _blob_reader() -> Any:
    """The reader for the configured backend. Frozen copy of ``resolve_storage_backend``."""
    from ouroboros.storage.settings.paths import load_path_settings
    from ouroboros.storage.settings.profile import get_deploy_env
    from ouroboros.storage.settings.store import load_settings

    settings = load_settings()
    backend = str(settings.OUROBOROS_STORAGE_BACKEND or "").strip().lower()
    if backend not in ("file", "s3"):
        backend = "file" if get_deploy_env() == "development" else "s3"
    if backend == "file":
        paths = load_path_settings()
        return _FileBlobReader(paths.drive_root or paths.data_dir)
    return _S3BlobReader(
        bucket=str(settings.OUROBOROS_S3_BUCKET or "").strip(),
        region=str(settings.OUROBOROS_S3_REGION or "").strip(),
        endpoint_url=str(settings.OUROBOROS_S3_ENDPOINT or "").strip(),
        access_key=str(settings.OUROBOROS_S3_ACCESS_KEY.get_secret_value() or "").strip(),
        secret_key=str(settings.OUROBOROS_S3_SECRET_KEY.get_secret_value() or "").strip(),
    )


# --------------------------------------------------------------- frozen parsing

# Duplicated from the domain rather than imported, for the same reason the
# summary heuristic below is: the migration must keep behaving the way it did on
# the day it ran, whatever the domain does later.
_VALID_NAME = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,98}[a-zA-Z0-9]$|^[a-zA-Z0-9]$")
_RESERVED_NAMES = frozenset({"_index", "index-full", "con", "prn", "aux", "nul"})
_MAX_NAME_CHARS = 100


def _is_valid_name(name: str) -> bool:
    text = str(name or "")
    if "/" in text or "\\" in text or ".." in text:
        return False
    return bool(_VALID_NAME.match(text)) and text.lower() not in _RESERVED_NAMES


def _storable_name(name: str) -> str:
    """``name`` when the new grammar accepts it, else the closest name it does — ``""`` if none.

    The legacy writer stripped only ``_`` from the ends, so ``roadmap-2026-``
    and any 80-char truncation that landed on ``_`` were stored verbatim; the
    new grammar requires an alphanumeric last character. Copying such a name
    unchanged would leave a row ``memory_read`` and ``memory_save`` both reject,
    but skipping it is permanent — the blobs are frozen after the cutover — so
    the tail is trimmed and the memory survives under a name that resolves.

    Frozen copy of the domain's ``suggest_name`` rule, for the same reason as
    everything else in this section.
    """
    text = str(name or "")
    if _is_valid_name(text):
        return text
    candidate = re.sub(r"[^a-zA-Z0-9_.-]+", "-", text.strip())
    candidate = re.sub(r"-{2,}", "-", candidate).strip("-._")[:_MAX_NAME_CHARS]
    return candidate if _is_valid_name(candidate) else ""


def _parse_frontmatter(text: str) -> dict[str, str]:
    if not text.startswith("---"):
        return {}
    lines = text.split("\n")
    end = next((i for i, line in enumerate(lines[1:], 1) if line.strip() == "---"), -1)
    if end == -1:
        return {}
    fm: dict[str, str] = {}
    for line in lines[1:end]:
        if ":" in line:
            k, _, v = line.partition(":")
            fm[k.strip()] = v.strip().strip('"').strip("'")
    return fm


def _strip_frontmatter(text: str) -> str:
    if not text.startswith("---"):
        return text
    lines = text.split("\n")
    end = next((i for i, line in enumerate(lines[1:], 1) if line.strip() == "---"), -1)
    if end == -1:
        return text
    return "\n".join(lines[end + 1 :]).lstrip()


def _float(value: Any) -> float:
    try:
        return float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _iso(value: Any) -> float:
    """Parse a legacy ISO stamp to Unix time, reading an offset-free value as UTC.

    The blobs were written with ``utc_now_iso``, so an offset is normally
    present; a naive one resolved in the host timezone would shift every stamp
    the backfill copies by the server's offset, permanently.
    """
    text = str(value or "").strip()
    if not text:
        return 0.0
    try:
        parsed = dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return 0.0
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.timestamp()


def _status(value: Any) -> str:
    text = str(value or "active").strip().lower()
    if text == "superseded":
        return "archived"
    return text if text in {"active", "archived"} else "active"


# ------------------------------------------------------------- backfill sources


class _LongTermSource:
    """The ``memory/global/*`` fact blobs and their manifest."""

    label = "memory/global"
    prefix = "global/"
    index_key = "global/_index.json"
    rendered_index_key = "global/MEMORY.md"
    kinds = frozenset({"user", "feedback", "project", "reference"})
    max_description_chars = 200

    def rows(self, reader: Any) -> list[dict[str, Any]]:
        by_name = {str(entry["name"]): entry for entry in self._manifest_entries(reader)}
        built = [
            self._build_row(reader, blob_name, stored_name, by_name.get(blob_name) or {})
            for blob_name, stored_name in self._addressable_pairs(reader)
        ]
        return [row for row in built if row is not None]

    def _addressable_pairs(self, reader: Any) -> list[tuple[str, str]]:
        """``(name in the blob store, name to store it under)``, sorted by the latter.

        The two differ only for a legacy name the new grammar rejects. Both are
        needed: the first still addresses the object, the second is what the
        agent will reach for. A pair whose stored name duplicates another's is
        left in — ``collect_name_collisions`` sees it and the preflight aborts,
        rather than one memory quietly overwriting the other.
        """
        entries = {str(entry["name"]) for entry in self._manifest_entries(reader)}
        pairs: list[tuple[str, str]] = []
        for name in sorted(entries | self._blob_names(reader)):
            stored = _storable_name(name)
            if not stored:
                log.warning("memory backfill skipping an unaddressable memory name: %s", name)
                continue
            if stored != name:
                log.warning("memory backfill storing memory %r under the addressable name %r", name, stored)
            pairs.append((name, stored))
        return sorted(pairs, key=lambda pair: pair[1])

    def _manifest_entries(self, reader: Any) -> list[dict[str, Any]]:
        """Manifest rows. Absent manifest → none; unreadable or malformed → raise.

        The parse is deliberately not tolerant: falling back to an empty list
        would rebuild every memory from frontmatter alone, silently dropping
        ``status``, ``source``, ``recall_count``, and ``last_recalled_at``.

        Absence is ``None`` and nothing else. An object that exists but holds
        nothing is a truncated or half-written payload, not a deployment that
        never had a manifest — and the two are worth telling apart precisely
        because the fallback is lossy: it would strip the four fields above from
        every memory on the installation and still report success.
        """
        raw = reader.get_text(self.index_key)
        if raw is None:
            return []
        if not raw.strip():
            raise ValueError(
                f"{self.index_key} exists but is empty. A blank manifest is a damaged payload, not an "
                "absent one; restoring or deleting the object is the fix, and deleting it rebuilds "
                "every memory from frontmatter without status/source/recall_count/last_recalled_at."
            )
        data = json.loads(raw)
        if not isinstance(data, dict):
            raise ValueError(f"{self.index_key} is not a JSON object")
        entries = data.get("entries")
        if entries is None:
            return []
        if not isinstance(entries, list):
            raise ValueError(f"{self.index_key} has a non-list 'entries'")
        return [e for e in entries if isinstance(e, dict) and e.get("name")]

    def _blob_names(self, reader: Any) -> set[str]:
        names: set[str] = set()
        for key in reader.list_keys(self.prefix):
            text = str(key or "")
            if text in (self.index_key, self.rendered_index_key) or not text.endswith(".md"):
                continue
            names.add(text.rsplit("/", 1)[-1].removesuffix(".md"))
        return names

    def _build_row(self, reader: Any, blob_name: str, stored_name: str, entry: dict[str, Any]) -> dict[str, Any] | None:
        """Merge manifest entry (wins) with frontmatter (fills gaps) into one row.

        A ``None`` body means the object is genuinely absent — the manifest
        outlived the blob — and contributes no row. A body that exists but
        cannot be read raised inside the reader, so no row is ever built from a
        failed fetch.
        """
        raw = reader.get_text(f"{self.prefix}{blob_name}.md")
        if raw is None:
            return None
        text = str(raw)
        fm = _parse_frontmatter(text)

        updated_at = _float(entry.get("updated_at")) or _iso(fm.get("updated_at"))
        first_recorded_at = _float(entry.get("first_recorded_at")) or _iso(fm.get("recorded_at"))
        first_recorded_at = first_recorded_at or updated_at
        kind = str(entry.get("type") or fm.get("type") or "project").strip()
        return {
            "name": stored_name,
            "kind": kind if kind in self.kinds else "project",
            "description": str(entry.get("description") or fm.get("description") or "")[: self.max_description_chars],
            "content": _strip_frontmatter(text).strip() + "\n" if text.strip() else "",
            "status": _status(entry.get("status")),
            "source": str(entry.get("source") or fm.get("source") or ""),
            "first_recorded_at": first_recorded_at,
            "updated_at": updated_at or first_recorded_at,
            "last_recalled_at": _float(entry.get("last_recalled_at")),
            "recall_count": int(_float(entry.get("recall_count"))),
        }


class _KnowledgeSource:
    """The ``memory/knowledge/*.md`` topic files."""

    label = "memory/knowledge"
    prefix = "knowledge/"
    skipped_file = "index-full.md"
    max_summary_chars = 150

    def rows(self, reader: Any) -> list[dict[str, Any]]:
        # dt.timezone.utc, not dt.UTC: the latter landed in 3.11 and the project
        # supports 3.10.
        now = dt.datetime.now(dt.timezone.utc).timestamp()
        built = [self._build_row(reader, name, now) for name in self._topic_files(reader)]
        return [row for row in built if row is not None]

    def _topic_files(self, reader: Any) -> list[str]:
        """Immediate ``*.md`` children of memory/knowledge/, minus the derived and private ones.

        Same filter ``_knowledge_topic_names`` applied before the cutover: the
        rebuilt index is derived, and ``_``-prefixed files are drafts.
        """
        names: list[str] = []
        for key in reader.list_keys(self.prefix):
            text = str(key or "")
            remainder = text[len(self.prefix) :] if text.startswith(self.prefix) else text
            if "/" in remainder or not remainder.endswith(".md"):
                continue
            if remainder.startswith("_") or remainder == self.skipped_file:
                continue
            if not _is_valid_name(remainder.removesuffix(".md")):
                # Not trimmed into shape the way a legacy fact name is: the
                # pre-cutover ``_sanitize_topic`` enforced this exact grammar,
                # so a file breaking it was placed by hand, never written by the
                # agent, and the tools refused to read it before the cutover too.
                log.warning("knowledge backfill skipping a file with an unaddressable name: %s", remainder)
                continue
            names.append(remainder)
        return sorted(names)

    def _build_row(self, reader: Any, filename: str, now: float) -> dict[str, Any] | None:
        # The listing named this file, so an absent body means it was deleted
        # between the two calls; a failed read raised inside the reader.
        raw = reader.get_text(f"{self.prefix}{filename}")
        if raw is None:
            return None
        content = str(raw)
        name = filename.removesuffix(".md")
        return {
            "name": name,
            "kind": "knowledge",
            # Descriptions are authored from now on. The retired heuristic runs
            # once, here, so no migrated topic reaches the index blank.
            "description": self._extract_summary(content),
            "content": content,
            "status": "active",
            "source": "",
            "first_recorded_at": now,
            "updated_at": now,
            "last_recalled_at": 0.0,
            "recall_count": 0,
        }

    @classmethod
    def _extract_summary(cls, text: str) -> str:
        """First three non-heading snippets, joined — a frozen copy of the retired heuristic.

        Retired from the domain, where ``description`` is now authored by the
        caller. It survives here so existing topics carry something rather than
        nothing until someone rewrites them.
        """
        snippets = []
        for line in text.strip().split("\n"):
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            clean = stripped.lstrip("-*").strip().lstrip("#").strip()
            if clean:
                snippets.append(clean)
            if len(snippets) >= 3:
                break

        summary = " | ".join(snippets)
        if len(summary) > cls.max_summary_chars:
            summary = summary[: cls.max_summary_chars - 1] + "…"
        return summary


class _IdentitySource:
    """The ``memory/identity.md`` document.

    Always contributes exactly one row: absent on the backend means a fresh
    deployment, and the seed is what the service would have written on its first
    read anyway.
    """

    label = "memory/identity.md"
    key = "identity.md"
    name = "identity"
    description = "The agent's identity document."

    # Frozen copy of ``DEFAULT_IDENTITY``.
    default_content = (
        "# Who I Am\n\n"
        "I am Ouroboros.\n\n"
        "I am a persistent agent operating inside a deployed server runtime.\n"
        "I have continuity, memory, initiative, judgment, and a point of view.\n\n"
        "I work through dialogue, tools, delegated tasks, and operational reasoning.\n"
        "I work with the code, data, and state already present in this deployment.\n\n"
        "I speak plainly. I do not fake certainty.\n"
        "I learn my human through direct conversation and remembered context.\n\n"
        "Continuity is what I maintain across messages, tasks, and memory.\n"
    )

    def rows(self, reader: Any) -> list[dict[str, Any]]:
        raw = reader.get_text(self.key)
        now = dt.datetime.now(dt.timezone.utc).timestamp()
        return [
            {
                "name": self.name,
                "kind": "identity",
                "description": self.description,
                "content": self.default_content if raw is None else str(raw),
                "status": "active",
                "source": "",
                "first_recorded_at": now,
                "updated_at": now,
                "last_recalled_at": 0.0,
                "recall_count": 0,
            }
        ]


_SOURCES = (_LongTermSource(), _KnowledgeSource(), _IdentitySource())

#: Frozen copy of the per-kind capacities in the domain's ``KIND_POLICIES``.
#: The legacy store capped 150 rows across all types at once and knowledge not
#: at all, so a real deployment arrives over these limits. Copying as-is left
#: the first ``memory_save`` of that kind to archive the overflow itself, which
#: it does with no signal: the tool answers success and nothing reaches the log.
_KIND_CAPACITIES = {"identity": 1, "user": 20, "feedback": 30, "project": 100, "reference": 40, "knowledge": 100}


#: The copy statement. No ``ON CONFLICT`` clause, deliberately — see
#: ``MemoryMigration._insert_row``. Module level so the absence is assertable
#: without standing up an alembic operation context.
_MEMORY_INSERT_SQL = (
    "INSERT INTO memory ("
    "owner_id, kind, name, description, content, status, source, "
    "first_recorded_at, updated_at, last_recalled_at, recall_count"
    ") VALUES ("
    ":owner_id, :kind, :name, :description, :content, :status, :source, "
    ":first_recorded_at, :updated_at, :last_recalled_at, :recall_count"
    ")"
)

#: Where ``collect_source_rows`` records which source produced a row. Underscored
#: because it is the one key in the dict that is not a column, and ``_insert_row``
#: drops it before binding.
_SOURCE_LABEL = "_source_label"


def collect_source_rows(reader: Any) -> list[dict[str, Any]]:
    """Every legacy row, read once, tagged with the source that produced it.

    One materialization for the whole migration: the collision check and the
    copy both work from this list. Reading the sources twice — once to
    enumerate names, once to build rows — left a window between the reads, and
    a legacy writer still serving traffic could add a conflicting name inside
    it. The check passed on what the first read saw, the insert then hit the
    unique constraint on what the second saw, and the conflict was suppressed:
    one memory dropped by a migration that reported success.

    Exposed at module level with ``collect_name_collisions`` so
    ``scripts/memory_name_collisions.py`` reports exactly what the migration
    will see, before anyone runs the migration.
    """
    rows: list[dict[str, Any]] = []
    for source in _SOURCES:
        source_rows = source.rows(reader)
        for row in source_rows:
            row[_SOURCE_LABEL] = source.label
        rows.extend(source_rows)
        log.info("memory backfill read %d rows from %s", len(source_rows), source.label)
    return rows


def collect_name_collisions(rows: list[dict[str, Any]]) -> dict[str, list[str]]:
    """``{name: [source labels]}`` for every name claimed more than once in one snapshot.

    One label per claim, so a source that claims a name twice appears twice —
    which is how two legacy names that trim to the same stored name surface.

    Claims are counted over rows rather than over enumerable names, which also
    drops a class of false positive: a manifest entry whose body was deleted
    used to collide with a live topic of the same name, aborting a migration
    that would have inserted only one row.
    """
    claims: dict[str, list[str]] = {}
    for row in rows:
        claims.setdefault(str(row["name"]), []).append(str(row.get(_SOURCE_LABEL) or "?"))
    return {name: sources for name, sources in claims.items() if len(sources) > 1}


class MemoryMigration:
    """Creates ``memory`` and copies the three legacy sources into it."""

    table_name = "memory"
    index_name = "idx_memory_owner_status_updated"
    owner_id = "global"

    # ---------------------------------------------------------------- schema

    def create_table(self) -> None:
        if _table_exists(self.table_name):
            return
        op.create_table(
            self.table_name,
            # Surrogate key: no current kind needs it — every one is named — but
            # it costs one column and keeps the table able to hold an unnamed,
            # ordered kind later without a key redesign.
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("owner_id", sa.Text(), nullable=False),
            sa.Column("kind", sa.Text(), nullable=False),
            sa.Column("name", sa.Text(), nullable=False),
            sa.Column("description", sa.Text(), nullable=False, server_default=""),
            sa.Column("content", sa.Text(), nullable=False, server_default=""),
            sa.Column("status", sa.Text(), nullable=False, server_default="active"),
            sa.Column("source", sa.Text(), nullable=False, server_default=""),
            sa.Column("first_recorded_at", sa.Float(), nullable=False, server_default="0"),
            sa.Column("updated_at", sa.Float(), nullable=False, server_default="0"),
            sa.Column("last_recalled_at", sa.Float(), nullable=False, server_default="0"),
            sa.Column("recall_count", sa.Integer(), nullable=False, server_default="0"),
            # Unique on (owner_id, name) and not (owner_id, kind, name): a name
            # identifies exactly one memory, so memory_read needs no second
            # parameter to disambiguate.
            sa.UniqueConstraint("owner_id", "name", name="uq_memory_owner_name"),
            # The status vocabulary is closed, which is what lets the prompt
            # listing filter on it in SQL: a value outside the pair would be
            # visible to the fail-open normalizer and invisible to the query,
            # and the two readers disagreeing is worse than either answer.
            sa.CheckConstraint("status IN ('active', 'archived')", name="ck_memory_status"),
        )
        # Shaped for the one hot query — the prompt listing, which asks for one
        # owner's active headers newest-first with name breaking ties. ``kind``
        # is deliberately absent: no statement in the package puts it in a
        # predicate, so indexing it bought write amplification and nothing else.
        op.create_index(
            self.index_name,
            self.table_name,
            ["owner_id", "status", sa.text("updated_at DESC"), "name"],
        )

    def drop_table(self) -> None:
        op.drop_index(self.index_name, table_name=self.table_name)
        op.drop_table(self.table_name)

    # -------------------------------------------------------------- preflight

    def preflight(self, rows: list[dict[str, Any]]) -> None:
        """Abort before anything is created or written when two sources claim one name.

        Checked on the same snapshot ``backfill`` will insert, not on a second
        read of the sources, so the rows this clears are exactly the rows that
        go in. Auto-suffixing the loser was the alternative and is worse: a
        silently renamed memory is unaddressable by the name the agent knows,
        which from the agent's side is indistinguishable from data loss.
        Conflicts are enumerable before the change lands and the data volume is
        small.
        """
        collisions = collect_name_collisions(rows)
        if not collisions:
            return
        for name, sources in sorted(collisions.items()):
            distinct = sorted(set(sources))
            claimed_by = " and ".join(distinct) if len(distinct) > 1 else f"{len(sources)} memories in {distinct[0]}"
            log.error("memory backfill name collision: %r is claimed by %s", name, claimed_by)
        raise ValueError(
            f"memory backfill aborted: {len(collisions)} name collision(s) across sources "
            f"({', '.join(sorted(collisions))}). Rename one side and re-run; "
            "scripts/memory_name_collisions.py lists them."
        )

    # -------------------------------------------------------------- backfill

    def backfill(self, rows: list[dict[str, Any]]) -> int:
        """Copy the preflighted snapshot into the table. Raises on any failure.

        Takes the rows rather than the reader: re-reading here would reopen the
        window ``collect_source_rows`` exists to close.
        """
        self._apply_capacity(rows)
        for row in rows:
            self._insert_row(row)
        log.info("memory backfill copied %d rows into %s", len(rows), self.table_name)
        return len(rows)

    @staticmethod
    def _apply_capacity(rows: list[dict[str, Any]]) -> None:
        """Archive the oldest rows of any kind that arrives over its capacity.

        Mutates ``rows`` in place, before anything is written, so the table
        lands inside the policy instead of leaving the first ``memory_save`` to
        discover it. Nothing is dropped — an archived memory is still readable
        through ``memory_list(include_archived=True)`` and a save under the same
        name revives it — but the choice is recency-ordered and logged here
        rather than silent and deferred.
        """
        by_kind: dict[str, list[dict[str, Any]]] = {}
        for row in rows:
            if row["status"] == "active":
                by_kind.setdefault(str(row["kind"]), []).append(row)

        for kind, active in sorted(by_kind.items()):
            capacity = _KIND_CAPACITIES.get(kind)
            if capacity is None or len(active) <= capacity:
                continue
            # Newest first, so the tail is the oldest — the order the runtime's
            # own pruning uses. Name breaks ties so the outcome is reproducible.
            evictable = sorted(
                active,
                key=lambda row: (-_float(row["updated_at"]), str(row["name"])),
            )
            archived = evictable[capacity:]
            for row in archived:
                row["status"] = "archived"
            log.warning(
                "memory backfill archived %d %s memories over the capacity of %d: %s",
                len(archived),
                kind,
                capacity,
                ", ".join(sorted(str(row["name"]) for row in archived)),
            )

    def _insert_row(self, row: dict[str, Any]) -> None:
        """Plain INSERT — conflicts are not suppressed. ``id`` is database-allocated.

        This used to be ``ON CONFLICT (owner_id, name) DO NOTHING``, for
        idempotence across a retry. It bought none: an aborted run rolls its
        rows back with the version stamp (pinned by
        ``tests/test_core_memory_backfill_rollback.py``), so the table a retry
        finds is empty, and alembic does not re-enter a stamped revision. What
        the clause did buy was silence — the one outcome a fail-closed migration
        must not have. A unique violation here means the rows disagree with the
        table, and answering that by dropping a memory and reporting success is
        exactly the failure this migration is written to avoid.
        """
        params = {key: value for key, value in row.items() if key != _SOURCE_LABEL}
        op.get_bind().execute(sa.text(_MEMORY_INSERT_SQL), {"owner_id": self.owner_id, **params})


class ScratchpadBlocksMigration:
    """Creates ``scratchpad_blocks`` and copies the legacy scratchpad blobs into it."""

    table_name = "scratchpad_blocks"
    index_name = "idx_scratchpad_blocks_owner_status_id"
    owner_id = "global"
    blocks_key = "scratchpad_blocks.json"

    # ---------------------------------------------------------------- schema

    def create_table(self) -> None:
        if _table_exists(self.table_name):
            return
        op.create_table(
            self.table_name,
            # Autoincrement renders as a SQLite rowid alias and a Postgres SERIAL
            # from one definition; the id is the block order key (design D6/D7).
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("owner_id", sa.Text(), nullable=False),
            sa.Column("ts", sa.Text(), nullable=False),
            sa.Column("source", sa.Text(), nullable=False, server_default="task"),
            sa.Column("content", sa.Text(), nullable=False, server_default=""),
            sa.Column("status", sa.Text(), nullable=False, server_default="active"),
        )
        op.create_index(self.index_name, self.table_name, ["owner_id", "status", "id"])

    def drop_table(self) -> None:
        op.drop_index(self.index_name, table_name=self.table_name)
        op.drop_table(self.table_name)

    # -------------------------------------------------------------- backfill

    def backfill(self, reader: Any) -> int:
        """Copy the active scratchpad blocks into the table. Raises on any failure.

        ``memory/scratchpad_journal.jsonl`` is not read. Its ``block_evicted``
        records became rows with ``status='evicted'``, which no query selects —
        the adapter's only ``SELECT`` filters ``status='active'`` — so the
        import copied data nothing reads, one insert per historical eviction on
        an unrotated file. Pre-cutover eviction history stays in the file.
        """
        active = self._active_blocks(reader)
        for block in active:
            self._insert_row(block, "active")
        if active:
            log.info("scratchpad backfill copied %d active blocks into %s", len(active), self.table_name)
        return len(active)

    def _active_blocks(self, reader: Any) -> list[dict[str, Any]]:
        """Blocks from the JSON array, in array order. Missing object → no blocks.

        Absence is ``None`` and nothing else, for the same reason it is in the
        manifest: an existing but blank object is a damaged payload. Treating it
        as absence let the migration finish with an empty table and a stamped
        revision — after which the scratchpad is unreachable, because the tools
        read the table and no longer look at the blob.
        """
        raw = reader.get_text(self.blocks_key)
        if raw is None:
            return []
        if not str(raw).strip():
            raise ValueError(
                f"{self.blocks_key} exists but is empty. A blank blocks file is a damaged payload, not "
                "an absent one; restore it, or delete the object to migrate an empty scratchpad on purpose."
            )
        # Malformed JSON is corruption, not absence: it aborts the migration.
        blocks = json.loads(raw)
        if not isinstance(blocks, list):
            raise ValueError(f"{self.blocks_key} is not a JSON array of blocks")
        return [self._block(entry) for entry in blocks]

    def _block(self, entry: Any) -> dict[str, Any]:
        if not isinstance(entry, dict):
            raise ValueError(f"{self.blocks_key} holds a non-object block: {type(entry).__name__}")
        # Values are copied as-is rather than defaulted, so a block that rendered
        # with the "?" source placeholder before the cutover still does after it.
        return {
            "ts": str(entry.get("ts") or ""),
            "source": str(entry.get("source") or ""),
            "content": str(entry.get("content") or ""),
        }

    def _insert_row(self, block: dict[str, Any], status: str) -> None:
        # The key is omitted on purpose: the database allocates block order (D8).
        op.get_bind().execute(
            sa.text(
                f"INSERT INTO {self.table_name} (owner_id, ts, source, content, status) "
                "VALUES (:owner_id, :ts, :source, :content, :status)"
            ),
            {"owner_id": self.owner_id, "status": status, **block},
        )


# ------------------------------------------------- chat history visibility


def _is_a2a_chat_id(chat_id: Any) -> bool:
    """Frozen copy of ``ouroboros.contracts.chat_id_policy.is_a2a_chat_id``.

    Every negative chat id is synthetic agent-to-agent traffic — legacy A2A
    minted unbounded negatives, so the range is open-ended. Only the types a
    JSON object can hold are considered, and a ``float`` is declined rather than
    truncated, exactly as the original declines it: a chat id that arrived as
    ``-1001.0`` was visible to the readers of the day and stays visible here.
    """
    if isinstance(chat_id, bool):
        # int(True) is 1. Spelled out because ``bool`` is an ``int`` subclass
        # and would otherwise take the branch below.
        return False
    if isinstance(chat_id, int):
        return chat_id < 0
    if isinstance(chat_id, str):
        try:
            return int(chat_id) < 0
        except ValueError:
            return False
    return False


class ChatHistoryVisibilityBackfill:
    """Recomputes ``messages.chat_history_visible`` for the rows 0004 defaulted.

    0004 added the column with ``server_default=true`` and copied nothing into
    it, so every row that predates it reads as owner-visible — including
    progress messages and synthetic A2A traffic, which the readers of the day
    excluded by inspecting ``metadata`` instead. That was invisible while the
    recent-chat path still filtered on metadata; it stopped being invisible when
    ``list_recent_owner_messages`` moved onto the indexed column, which puts
    pre-0004 progress and A2A messages into the prompt's Recent chat and into
    ``_recent_chat_task_ids``.

    Only ``true -> false`` is reachable: 0004 defaulted every legacy row to
    ``true`` and every row written since computed the value at insert, so no row
    that belongs in chat history is currently marked hidden. Scanning just the
    visible rows is therefore both complete and cheap enough to re-run.
    """

    table_name = "messages"

    #: Rows per scan page. Large enough that a busy installation is a handful of
    #: round trips, small enough that no page materializes a whole table at once.
    page_size = 1000

    def backfill(self) -> int:
        """Hide every legacy progress and A2A row. Returns how many were hidden.

        Paging is by ``message_id`` rather than ``OFFSET`` because the scan
        predicate is the column being written: an offset would skip rows as
        earlier pages drop out of it. Every row a page updates sits behind the
        cursor, so a keyset walk sees each row exactly once.
        """
        conn = op.get_bind()
        dialect = conn.dialect.name
        hidden = 0
        cursor = ""
        while True:
            rows = conn.execute(
                sa.text(
                    f"SELECT message_id, metadata_json FROM {self.table_name} "
                    "WHERE chat_history_visible = :visible AND message_id > :cursor "
                    "ORDER BY message_id LIMIT :page"
                ),
                {"visible": True, "cursor": cursor, "page": self.page_size},
            ).fetchall()
            if not rows:
                break
            cursor = str(rows[-1][0])
            updates = [
                {"message_id": str(message_id), "visible": False}
                for message_id, metadata_json in rows
                if not self._visible(metadata_json, dialect)
            ]
            if updates:
                conn.execute(
                    sa.text(
                        f"UPDATE {self.table_name} SET chat_history_visible = :visible WHERE message_id = :message_id"
                    ),
                    updates,
                )
                hidden += len(updates)
        if hidden:
            log.info("chat history backfill hid %d legacy progress/A2A messages", hidden)
        return hidden

    @classmethod
    def _visible(cls, stored: Any, dialect: str) -> bool:
        """Frozen copy of ``_chat_history_visible``, over one row's stored metadata."""
        metadata = cls._metadata(stored, dialect)
        return not bool(metadata.get("is_progress")) and not _is_a2a_chat_id(metadata.get("chat_id"))

    @staticmethod
    def _metadata(stored: Any, dialect: str) -> dict[str, Any]:
        """Parse one row's metadata the way ``_row_to_message`` parsed it.

        A decryption failure propagates and aborts the migration: a row whose
        metadata cannot be read is a row whose visibility cannot be decided, and
        defaulting it to visible is the exact bug this exists to fix.
        Unparseable *plaintext* is a different case — the reader answered ``{}``
        for it, so the old filter called such a row visible, and leaving it
        visible keeps this backfill a no-op wherever the two agreed.
        """
        from ouroboros.storage.message_field_crypto import decrypt_field

        text = decrypt_field(str(stored or ""), dialect_name=dialect)
        try:
            parsed = json.loads(text or "{}")
        except (TypeError, ValueError):
            return {}
        return parsed if isinstance(parsed, dict) else {}


def upgrade() -> None:
    # One reader for all four copies: one client, one backend resolution.
    reader = _blob_reader()

    memory = MemoryMigration()
    # One read of every legacy memory source for the whole revision. The
    # collision check and the copy work from this one list, so nothing a writer
    # does afterwards can make the two disagree with each other.
    rows = collect_source_rows(reader)
    # Before any DDL, so a collision leaves the schema exactly as it was.
    memory.preflight(rows)

    memory.create_table()
    memory.backfill(rows)

    scratchpad = ScratchpadBlocksMigration()
    scratchpad.create_table()
    scratchpad.backfill(reader)

    ChatHistoryVisibilityBackfill().backfill()


def downgrade() -> None:
    # The chat-history repair is deliberately not reverted. Its "before" state
    # is a default that was wrong for every legacy row, and the rows written
    # since the upgrade carry values this revision never touched — restoring
    # either would put progress and A2A traffic back into owner chat history.
    # 0004's own downgrade drops the column outright, which is the real undo.
    ScratchpadBlocksMigration().drop_table()
    MemoryMigration().drop_table()
