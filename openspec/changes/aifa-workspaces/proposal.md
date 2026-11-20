## Why

The RFA multi-document model (`AIFA_RFA/docs/MULTIDOCUMENT_TECHNICAL_SPEC.md` §2.1) makes the
Workspace the boundary of available documents. In AIFA, a folder (`Folder` in
`aifa/web/src/lib/projects.ts`) groups chats, and a chat inside a folder already sends the
folder's id as its `project_id` (`use-chat-run.ts:950`), so its Case is the folder's.

The folder's document list, however, exists only as `folders[].doc_ids` inside one JSON blob per
user (`store/dialogs.py`):
- the browser pushes the blob, and the server merges it;
- folders merge by last write;
- chat `doc_ids` only ever grow;
- nothing ties a document row to a folder or checks the list against the owner.

The "all documents of this folder" scope (`aifa-multi-document-turn`) needs that membership to be
server truth.

## What Changes

- New table `workspace` with `workspace_id`, `owner_id`, `name`, `created_at` and `updated_at`.
  - A folder is a workspace with the folder's id.
  - Every owner also has one default workspace, `owner:<owner_id>`, for chats outside folders.
  - The `owner:` prefix is reserved for these defaults.
- The `document` table gets `workspace_id` (indexed). The document store assigns it on every
  write, so every creation path gets a workspace: upload, web-search acquisition, demo. An empty
  value means the owner's default.
- `POST /documents/upload` accepts `workspace_id`:
  - empty means the default workspace;
  - an unknown id is created for the caller (a folder that has not been pushed yet);
  - an id owned by someone else gives 404.

  The frontend sends the folder id when uploading in a folder chat.
- Folder rows are created and renamed from the existing `/dialogs` push, since the folder's id and
  name already arrive there. Folders are not deleted by this change.
- A one-shot backfill assigns existing documents a workspace from the stored dialog states. It
  trusts only documents the state's owner actually owns, so no client action is needed:
  - a document listed in a folder → that folder;
  - in several folders → the oldest, with the others logged;
  - otherwise → the owner's default workspace.
- Access stays owner-based. The workspace groups documents and does not widen or narrow who may
  read them.

## Capabilities

### New Capabilities

- `aifa-workspaces`: what a workspace is in AIFA, how documents belong to one, how folders become
  workspaces, and how existing documents are assigned.

### Modified Capabilities

None.

## Impact

- Service (`aifa/finengine/src/finengine/`):
  - `store/documents.py` (column, its addition at store start, default assignment,
    `list_documents(workspace_id=…)`)
  - new `store/workspaces.py`
  - new one-shot `store/migrate_workspaces.py`
  - `api/main.py` (upload field, `/dialogs` merge hook)
  - `models/document.py` (`workspace_id`)
- Frontend `aifa/web/src/components/workspace/use-doc-upload.ts`: send the folder id on upload.
  No folder document pane exists in v2, so none is added.
- Governance: a new symbiosis ADR (next free number).
  - `ARCHITECTURE.md` §4.3 (service storage).
  - `CONTRACTS.md` §1, because C1 says there are no frontend edits and lists the routes, and this
    adds an upload field and a frontend edit (ADR-0015).
  - F13 applies.
- No new dependency. The ouroboros core is untouched.
- Prerequisite for `aifa-multi-document-turn`. `aifa-document-search` stores `workspace_id` on
  records but does not require it for access.
