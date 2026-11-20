# Tasks

Paths are relative to `aifa/finengine/src/finengine/` unless they start with `aifa/` or
`aifa-symbiosis/`. Service tests are in `aifa/finengine/tests/`. Database tests use the existing
PostgreSQL 16 container pattern. One step branch (`sym/NN-workspaces`).

**RFA tests (applies to every task).**
- A task that ports RFA behaviour lists the RFA tests (`AIFA_RFA/tests/…`) it carries over.
- A test bound to RFA infrastructure (their DB fixtures `session`/`workspace`/`owner`, OCR JSON,
  workers, AnalysisRun) is rewritten on our fixtures, not dropped.
- A test is skipped only when its behaviour is outside this plan, and the skip is written down
  with its reason.
- Such a task is done when the ported tests are green, and the step report has a table
  «тест RFA → наш тест | пропущен: причина».

## 1. Decision record

- [ ] 1.1 Write the ADR (next free number, status «предложено»). It records W1–W4, the non-goals,
      and the role of this change as a prerequisite for the multi-document turn. In the same
      branch, update:
      - `ARCHITECTURE.md` §4.3 (service storage);
      - `CONTRACTS.md` §1: the `workspace_id` upload field, and the frontend edit as an exception
        to "no frontend edits" (ADR-0015).

      Verify: `uv run pytest aifa/tests/architecture/test_adr_discipline.py` green. Gate: the
      owner sets «принято» before merging into `dev`.

## 2. Storage

- [ ] 2.1 New `store/workspaces.py` with the `workspace` table (`create_all` on first access) and
      `upsert_workspace`/`get_workspace`, applying the id rule from W1. New test
      `tests/test_workspaces_store.py`:
      - `upsert("owner:7")` is idempotent;
      - an upsert of an id owned by user 8 leaves the row unchanged;
      - the ids `owner:8`, an empty string and a 65-character id are rejected as folder ids.

      Each fails before because the module does not exist.

      RFA tests:
      - port `test_intake.py::test_one_workspace_per_owner_created_on_demand` (as the default
        workspace) and `::test_deduplication_does_not_cross_workspaces`, the latter only as "a
        workspace never lists another owner's document";
      - skip the rest of `test_intake.py` (byte deduplication, chat linking on upload, the email
        path): not in this plan.
- [ ] 2.2 `workspace_id` on `DocumentMeta` and on the `document` column, added in
      `SqlDocumentStore.__init__` (`ADD COLUMN IF NOT EXISTS` plus an index). `add_document`
      assigns the default, and `list_documents(workspace_id=…)` is added (W3). Tests on
      PostgreSQL:
      - a pre-existing `document` table without the column → the store starts, the column
        exists, and a second start is a no-op;
      - `add_document` without a workspace → `owner:<owner_id>`, with that workspace created;
      - `list_documents(workspace_id="F1")` returns only F1's rows;
      - the web-search path (`skills/document_acquisition/registration.py`) yields `owner:<id>`;
      - a document in F1 re-upserted from a `DocumentMeta` without a workspace stays in F1, and so
        does one rewritten by `update_document_meta`;
      - `get_document` reports the column's workspace even if the `meta` JSON says otherwise.

      Fails before: the column does not exist.
- [ ] 2.3 One-shot `store/migrate_workspaces.py`: dry run, then `--apply` (W4). New test
      `tests/test_migrate_workspaces.py`, with fixture dialog states:
      - a document in one folder → that folder;
      - in two folders → the oldest, with the conflict logged;
      - in none → `owner:<id>`;
      - via `projects[].doc_ids` of a chat in a folder → that folder;
      - a foreign document in a folder list → its own owner's default;
      - `owner_id=0` → `owner:0`;
      - a second `--apply` changes nothing.

## 3. API

- [ ] 3.1 Add `workspace_id` to `POST /documents/upload` (`api/main.py`). Tests in
      `tests/test_workspaces_api.py`:
      - no field → `owner:<id>`;
      - own folder → that folder;
      - an unknown valid id → created for the caller;
      - foreign → 404, and `owner:8` → 404, both with no registry row and no `catalog().upload`
        call (assert on a fake catalog).

      Producer/consumer: the real route → `SqlDocumentStore`. Allowed fake: `catalog()`, the
      ouroboros boundary.
- [ ] 3.2 Call the workspace upsert from the `/dialogs` push after `_merge_dialog_states`, in
      `try` (W2). Tests:
      - a new folder → a workspace with its name;
      - a newer rename → the name changes, and an older one does not;
      - a foreign id → unchanged plus a log line;
      - a folder with id `owner:8` → ignored and the push returns 200;
      - a raising upsert → the push still returns 200;
      - a folder missing from a push → the workspace remains.

## 4. Frontend

- [ ] 4.1 `use-doc-upload.ts`: an upload from a chat with `folder_id` sends
      `workspace_id=folder_id`; a chat outside folders sends nothing. Verify with vitest:
      - the form body carries the folder id in a folder chat;
      - there is no field outside folders.

      `npm test` in `aifa/web` green.

## 5. Verification and handover

- [ ] 5.1 Run `aifa/scripts/check.sh` and paste the result, comparing with the session-start
      baseline.
- [ ] 5.2 Get an independent `/code-review` of the diff in a clean context, citing file:line, per
      `aifa-symbiosis/plan/REVIEW_GUIDE.md`. Verify: no open blockers in the review file.
- [ ] 5.3 On aifa-sym, at the owner's signal, after the engine deploy: run the migration dry run,
      review the conflict report with the owner, then run `--apply`. Verify:
      - `SELECT count(*) FROM document WHERE workspace_id IS NULL` = 0;
      - a new upload in a folder chat gets the folder's workspace.
- [ ] 5.4 Add `aifa-symbiosis/plan/PROGRESS.md` §1/§3/§4 entries and run `graphify update .`.
      Verify: the entries exist, and the rebuild exits 0.
