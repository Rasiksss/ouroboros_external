## Context

See `proposal.md` (Why). Observed state:

- **Folders and chats.**
  - `Folder {id, name, instructions, doc_ids, …}` and `Project` (one chat, `folder_id?`, `doc_ids`)
    live in the browser.
  - A chat in a folder sends `project_id = folder_id` (`use-chat-run.ts:950`), so a folder's
    chats share one Case (`case_id = project_id`, `api/main.py:1564`).
  - The server knows this: `_upsert_dialog_stub` skips creating a chat card for a folder id
    (`api/main.py:2245-2250`).
- **The dialogs mirror.** `store/dialogs.py` keeps one JSON state per user, file-backed by
  default and SQL when configured.
  - `/dialogs` merges a push with `_merge_dialog_states` (`api/main.py:2140`): folders by
    `updated_at`, chat `doc_ids` by union.
  - `_upsert_dialog_stub` writes `request.doc_ids` there unfiltered (`api/main.py:1555`), so the
    lists may name other users' documents.
- **The v2 screen has no folder document pane.** `createFolder`, `saveFolders` and
  `uploadProjectSource` have no callers.
- **Document registry.**
  - It is `document(doc_id, owner_id, meta JSON)`, SQL-only.
  - `SqlDocumentStore.__init__` runs `_metadata.create_all` ("Alembic в репозитории нет — таблицы
    заводятся при первом обращении", `store/documents.py:89-91`).
  - `ensure_schema` only creates the schema (`CREATE SCHEMA`).
  - `add_document` is an upsert of the whole `meta` JSON, and `update_document_meta` is an
    UPDATE of the whole JSON (`store/documents.py:115-121, 169-189`).
  - The ingestion task upserts its in-memory `meta` three times (`api/main.py:580-606`), and the
    doc-type patch rewrites the whole JSON (`api/main.py:1390`). A value kept only in `meta` can
    therefore be overwritten by a stale object.
- **Document creation paths** without a workspace today:
  - `upload_document`;
  - `skills/document_acquisition/registration.py:131-135` (web search);
  - `demo/routes.py:89`.
- **Upload storage.** Upload stores the original in the ouroboros catalog (`catalog().upload`,
  `api/main.py:694-697`) before the registry row.
- **The RFA reference** keeps one workspace per owner, created on demand
  (`ingestion/workspaces.py`, "transitional"). Its spec allows many per owner. We take the spec's
  shape plus RFA's "default per owner".

## Goals / Non-Goals

**Goals:** every document belongs to a server-side workspace; a folder is a workspace; existing
data is assigned with no user action; no creation path can produce a document without one.

**Non-Goals:**
- **No shared workspaces or members.** Access stays owner-based.
- **No moving documents between workspaces.** Revisit when users ask to move a file between
  folders.
- **No deleting workspaces with folders.** There is no folder tombstone in the dialogs merge.
  Revisit together with folder tombstones.
- **No `GET /workspaces/{id}/documents` and no folder pane.** Nothing on the v2 screen would call
  it. `aifa-multi-document-turn` reads `list_documents(workspace_id=…)` inside the service.
  Revisit when a folder pane is designed.
- **No `state` jsonb.** Messages and UI state stay in dialogs and sessions.
- **No `NOT NULL` constraint.** The store guarantees the value (W3), and a constraint would break
  rollback to an image that writes rows without the column.

## Decisions

### W1. Workspace id = folder id, plus `owner:<id>` for chats outside folders

- Folder ids are the ids the chats already send as `project_id`, so the turn's workspace is
  `project_id` itself when it names a workspace of the caller (used by `aifa-multi-document-turn`).
- The default id is deterministic, so creating it is an idempotent upsert.
- Folder ids must match `[A-Za-z0-9_-]{1,64}`. That excludes the reserved `owner:` prefix, so a
  client cannot claim another user's default.

**Alternative: a server-generated UUID with a folder → workspace map.** It adds a second id the
frontend and the turn would have to translate.

### W2. Folder rows are written from the `/dialogs` push and from upload

- After `_merge_dialog_states`, one upsert runs per valid pushed folder:
  - insert if the id is unknown;
  - rename if the pushed folder is newer;
  - skip and log if the id is foreign.
- The upsert runs in `try`, as `_upsert_dialog_stub` does: a workspace write never fails the push.
- Upload creates an unknown valid id for the caller, because the push of a new folder can lag
  behind the first upload in it.

**Alternative: `POST /workspaces` endpoints.** There is no v2 screen to call them.

### W3. A column assigned in the store, and added at store start

- `document.workspace_id` is a column with an index, for the same reason `owner_id` is one: a list
  by workspace is a SQL condition. **The column is the truth.**
  - `DocumentMeta.workspace_id` exists for callers. Reads (`_to_meta` callers: `get_document`,
    `list_documents`) overlay it from the column.
  - Any value inside the `meta` JSON is ignored.
- `SqlDocumentStore.add_document`:
  - on insert, `workspace_id = meta.workspace_id or f"owner:{owner_id}"`, upserting that default
    workspace;
  - on conflict, `workspace_id = COALESCE(NULLIF(excluded.workspace_id, ''),
    document.workspace_id)`, so an object without a workspace never resets it.

  That covers every creation path in one place, legacy `owner_id=0` included (`owner:0`).
  `update_document_meta` does not write the column.
- The column is added where the store already manages its schema: in `SqlDocumentStore.__init__`,
  right after `create_all`, run `ALTER TABLE document ADD COLUMN IF NOT EXISTS workspace_id …`
  plus the index. This is a startup side-effect (W5). It makes the "deploy engine first" order
  safe: the first store access adds the column before any insert uses it.
- The `workspace` table is created by `create_all` in `store/workspaces.py` on its first access,
  the same pattern.

**Alternative: column only by the migration script, run before deploy.** It ties every deploy of
this change to a manual step, and an INSERT into a missing column breaks upload in between.

### W4. Backfill from dialog states, trusting only owned documents

`store/migrate_workspaces.py [--apply]`, with a dry run by default.
- Owners are enumerated from `SELECT DISTINCT owner_id FROM document`, because the dialogs store
  may be file-backed and has no user list. Each owner's state is read through the dialogs store
  API.
- Pairs document → folders come from `folders[].doc_ids`, and from `projects[].doc_ids` of chats
  with `folder_id`. A pair is kept only when `document.owner_id` equals the state's owner.
- Each document gets one workspace:
  - one folder → that folder;
  - several → the oldest by `created_at`, with the others logged;
  - none → `owner:<owner_id>`.
- Folder workspaces are upserted first with the W2 rules.
- Only rows with `workspace_id IS NULL` are touched, so a second run is a no-op. W3 already fills
  every new row.

### W5. Startup side-effects touched (rule R2.1)

| Side-effect | Change | Preserved by |
|---|---|---|
| `SqlDocumentStore.__init__` runs `create_all` on first access | adds `ALTER TABLE … ADD COLUMN IF NOT EXISTS workspace_id` and its index | test on PostgreSQL 16: an existing `document` table without the column → the store starts, upload works, and the column exists; a second start is a no-op |
| `store/workspaces.py` first access | `create_all` creates `workspace` | test: an empty database → the table exists after the first call |
| `ensure_schema --apply` (manual) | none | — |
| Engine import | none | existing tests |

## Risks / Trade-offs

- **[A document in two folders lands in only one]** → The oldest wins and the conflict is
  logged. The document stays readable from chats of the other folder (owner access), but is
  absent from that folder's `all_workspace` scope. Moving documents is a recorded revisit.
- **[Folder id collision across owners]** → Ids are never taken over, and upload checks the
  owner. The `owner:` prefix is unreachable through the id rule.
- **[DDL at store start on a large table]** `ADD COLUMN` without a default is a metadata-only
  change in PostgreSQL. Creating the index on a small table is quick. → Acceptable here, and the
  `IF NOT EXISTS` guards make restarts no-ops.
- **[Old frontend tab uploads without `workspace_id`]** → The document goes to the default
  workspace (W3). The new bundle ships in the same rollout.

## Migration Plan

1. Deploy the engine. The first store access adds the column, and new documents get a workspace.
2. Run `python -m finengine.store.migrate_workspaces` (dry run), review the conflict report with
   the owner, then run it with `--apply`.
3. Deploy the frontend.

Rollback: the old engine ignores the nullable column and the table. Rows written meanwhile keep
their workspace.
