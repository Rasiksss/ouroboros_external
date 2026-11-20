# Spec Delta

## Purpose

Defines the AIFA workspace: the server-side group of documents that a folder's chats share. It
covers how documents belong to one, how folders become workspaces, and how existing documents are
assigned to one.

## ADDED Requirements

### Requirement: Every document belongs to exactly one workspace of its owner

Each document SHALL have one `workspace_id` naming a workspace with the same owner. A folder SHALL
be a workspace whose id is the folder's id. Each owner SHALL have one default workspace,
`owner:<owner_id>`, created on first use. A document written without a workspace, by any path,
SHALL get its owner's default workspace.

#### Scenario: Upload into the default workspace

- **WHEN** user 7 uploads a document without `workspace_id`
- **THEN** the document's `workspace_id` is `owner:7` and that workspace exists

#### Scenario: Upload into a folder

- **WHEN** user 7 uploads a document with `workspace_id=F1`, where folder F1 is theirs
- **THEN** the document's `workspace_id` is `F1`

#### Scenario: Document acquired by web search

- **WHEN** a web-search acquisition registers a document for user 7 without naming a workspace
- **THEN** the document's `workspace_id` is `owner:7`

#### Scenario: Metadata rewrite keeps the workspace

- **WHEN** document A belongs to F1, and its metadata is written again from an object that has no
  workspace (the ingestion upserts, a doc-type change)
- **THEN** A still belongs to F1

### Requirement: Upload accepts the caller's workspace and refuses another owner's

`POST /documents/upload` with a `workspace_id` owned by another user SHALL answer 404 and store
nothing. An id that no workspace has yet SHALL be created for the caller, if it passes the id
rules. An id with the reserved `owner:` prefix other than the caller's own default SHALL answer
404.

#### Scenario: Foreign folder

- **WHEN** user 7 uploads with `workspace_id=F9`, a folder of user 8
- **THEN** the answer is 404, and no document row or stored file is created

#### Scenario: Folder not yet pushed

- **WHEN** user 7 uploads with `workspace_id=F3`, and no workspace F3 exists yet
- **THEN** workspace F3 owned by user 7 is created, and the document belongs to it

#### Scenario: Another user's default

- **WHEN** user 7 uploads with `workspace_id=owner:8`
- **THEN** the answer is 404

### Requirement: Workspace ids follow fixed rules

A folder workspace id SHALL be 1 to 64 characters from letters, digits, `-` and `_`. Any other id,
including one starting with `owner:`, SHALL NOT become a folder workspace from a push or an
upload.

#### Scenario: Reserved prefix in a push

- **WHEN** user 7 pushes dialogs containing a folder with id `owner:8`
- **THEN** no workspace is created or changed for it, and the push itself succeeds

### Requirement: Folders become workspaces from the dialogs push

A `/dialogs` push SHALL create a workspace for every valid folder in it that has none yet, and
SHALL rename an existing one when the pushed folder is newer. It SHALL NOT change a workspace
owned by another user. A folder missing from a push SHALL NOT delete its workspace. A failure
while writing workspaces SHALL NOT fail the push.

#### Scenario: New folder

- **WHEN** user 7 pushes dialogs containing a new folder F2 named «Банки»
- **THEN** workspace F2 owned by user 7 and named «Банки» exists

#### Scenario: Colliding folder id

- **WHEN** user 7 pushes a folder whose id is already a workspace of user 8
- **THEN** that workspace is unchanged, and user 7 gets no workspace with that id

### Requirement: Existing documents are assigned once by backfill

A one-shot backfill SHALL give every document without `workspace_id` a workspace. It SHALL trust a
folder or chat list only for documents whose owner is the dialog state's owner:
- a document listed by exactly one folder of its owner, directly or by a chat in it → that
  folder;
- a document listed by several such folders → the oldest of them, logging the others;
- any other document → its owner's default workspace.

A second run SHALL change nothing.

#### Scenario: Document in one folder

- **WHEN** document A of user 7 is listed in user 7's folder F1 and the backfill runs
- **THEN** A's `workspace_id` is `F1`

#### Scenario: Document in two folders

- **WHEN** document A is listed in folders F1 (older) and F2, both of its owner
- **THEN** A's `workspace_id` is `F1` and the conflict with F2 is logged

#### Scenario: Foreign document in a folder list

- **WHEN** user 7's folder F1 lists document X owned by user 8
- **THEN** X is not assigned to F1, and gets `owner:8`

#### Scenario: Idempotent run

- **WHEN** the backfill runs a second time
- **THEN** no document's `workspace_id` changes

### Requirement: Workspace does not change who may read a document

Access checks SHALL stay owner-based: a document SHALL be readable by its owner, and by an admin,
exactly as before, whatever workspace it belongs to.

#### Scenario: Document of another folder in a chat

- **WHEN** a chat in folder F2 names document A of the same owner, and A belongs to F1
- **THEN** the turn may read A, as before this change
