# Spec Delta

## Purpose

Defines which documents an AIFA chat turn may use and how its analysis spans several of them.
Covers the turn's scope, the frozen document set, the listing and ranked selection the planner
uses, one analysis loop per document over the turn's Case, and one finish for the whole turn.

## ADDED Requirements

### Requirement: A turn uses the chat's documents unless the user chooses all documents

A chat turn SHALL have scope `conversation` unless its request explicitly sets `scope` to
`all_workspace`. Under `conversation`, the turn's candidate set SHALL be the documents named by the
request (or, for a follow-up without `doc_ids`, the chat session's documents) plus the active
document. Documents the caller may not see SHALL be dropped without failing the turn.

#### Scenario: A request without scope keeps today's set

- **WHEN** a user starts a turn with `doc_ids=[A, B]` and no `scope`
- **THEN** the turn's set is `[A, B]`

#### Scenario: A foreign document is dropped silently

- **WHEN** a user starts a turn with `doc_ids=[A, X]` and document X belongs to another user
- **THEN** the turn starts with the set `[A]`

#### Scenario: An empty chat stays empty

- **WHEN** a user starts a turn in a chat without documents and without `scope`
- **THEN** the turn's set is empty and the system does not widen it to the user's other documents

### Requirement: The turn's workspace is derived on the server

The turn's workspace SHALL be the request's `project_id` when that names a workspace owned by the
caller and listed as a folder in the caller's stored dialogs. Otherwise it SHALL be the caller's
default workspace. No other request field SHALL choose it.

#### Scenario: Chat in a folder

- **WHEN** a chat in folder F1 starts a turn (its `project_id` is F1)
- **THEN** the turn's workspace is F1

#### Scenario: Another user's folder id

- **WHEN** user 7 starts a turn with `project_id=F9`, and F9 is a folder of user 8
- **THEN** the turn's workspace is `owner:7`, and no document of user 8 is in its set

#### Scenario: Stray workspace with a chat's id

- **WHEN** a chat outside folders has id P, and a workspace P of the same user exists (created by
  an upload) but no folder P is in the user's stored dialogs
- **THEN** the turn's workspace is the user's default workspace

### Requirement: The all-documents scope is an explicit choice over the workspace

With `scope=all_workspace`, the turn's candidate set SHALL be every document of the turn's
workspace, including documents whose text is not ready. An active document outside that set SHALL
NOT be added. Any `scope` other than `conversation` and `all_workspace` SHALL be rejected with HTTP
422.

#### Scenario: All documents of the folder

- **WHEN** a chat in folder F1 starts a turn with `scope=all_workspace`, F1 holds ready documents
  A and B and a never-parsed document C, and folder F2 of the same owner holds D
- **THEN** the turn's set is `[A, B, C]`, and D is not in it

#### Scenario: Unknown scope is rejected

- **WHEN** a request sets `scope="everything"`
- **THEN** the service answers 422 and no turn starts

### Requirement: An explicit document list narrows the scope

A non-empty `doc_ids` together with `scope=all_workspace` SHALL restrict the set to those listed
documents that also belong to the all-documents candidate set.

#### Scenario: Narrowing inside all documents

- **WHEN** a chat whose folder holds documents A, B and C starts a turn with `scope=all_workspace`
  and `doc_ids=[B]`
- **THEN** the turn's set is `[B]`

### Requirement: The turn's document set is frozen at creation

The set computed when the turn is created SHALL be the only set any tool of that turn reads or
opens. A document added to the chat while the turn runs SHALL NOT join that turn. A call naming a
document outside the set, or one deleted since the turn began, SHALL be refused with
`нет_документа`.

#### Scenario: Reading outside the set is refused

- **WHEN** the planner calls `aifa_doc(doc_id=Z)` and Z is not in the turn's set
- **THEN** the answer is `ОТКАЗ нет_документа`

#### Scenario: Document deleted mid-turn

- **WHEN** a loop on A is open, A is deleted, and the planner calls `aifa_move(doc_id=A)`
- **THEN** the answer is `ОТКАЗ нет_документа`, and no new document version is registered in the
  Case

### Requirement: The document listing shows metadata without reading texts

`aifa_doc` without arguments SHALL list the turn's documents, one entry per document, with
`doc_id`, file name, company, period, document type, language, page count and whether its text is
ready. It SHALL NOT load document texts. The page count SHALL equal the number of pages in the
recognised text. The listing SHALL be paged by an `offset` in entries, with a next offset; a null
next offset means the end.

#### Scenario: Listing a large set

- **WHEN** a turn's set holds more documents than fit in one result
- **THEN** `aifa_doc` returns the first page with a next offset
- **AND** `aifa_doc(offset=<next offset>)` returns the following documents

#### Scenario: Never-parsed document

- **WHEN** an `all_workspace` turn holds document C that was uploaded but never recognised
- **THEN** its listing entry says the text is not ready

### Requirement: The listing can be ranked by a query

`aifa_doc(query=…)` without `doc_id` SHALL list the turn's documents in this order: first the
documents the search projection selects for the query, highest first; then all the remaining
documents of the set in set order. When search is unavailable, it SHALL return the plain listing
with a note that ranking is unavailable. Documents outside the set SHALL never appear.

#### Scenario: Ranked selection in a folder

- **WHEN** an `all_workspace` turn holds 40 documents and the planner calls
  `aifa_doc(query="ВТБ МСФО 2024")`
- **THEN** the first entries are the documents the search ranks highest
- **AND** documents the search did not return still follow in the listing

#### Scenario: Search unavailable

- **WHEN** search is unreachable and the planner calls `aifa_doc(query="ВТБ")`
- **THEN** the answer is the plain listing with a note that ranking is unavailable

### Requirement: A report loop is identified by its document

`aifa_open` SHALL accept an optional `doc_id`. It SHALL first resolve the document: the given
`doc_id`, else the only document, else the active one. It SHALL then open, or return the already
open, loop on it. Calls resolving to the same document SHALL reach the same loop. With several
documents, no active one and no `doc_id`, it SHALL refuse with `нет_документа` naming `doc_id`.

#### Scenario: Two loops in one turn

- **WHEN** the planner calls `aifa_open(doc_id=A)` and then `aifa_open(doc_id=B)` in one turn
- **THEN** two loops are open, one on A and one on B, both writing to the turn's Case

#### Scenario: Implicit then explicit open of the same document

- **WHEN** the turn's set is `[A]`, and the planner calls `aifa_open()` and then
  `aifa_open(doc_id=A)`
- **THEN** both calls answer with the state of one and the same loop

#### Scenario: Ambiguous open asks for doc_id

- **WHEN** a turn's set is `[A, B]`, no active document is set and the planner calls `aifa_open()`
- **THEN** the answer is `ОТКАЗ нет_документа` naming `doc_id` as required

#### Scenario: Document without text

- **WHEN** the planner calls `aifa_open(doc_id=C)` and C's text is not ready
- **THEN** the answer is `ОТКАЗ нет_документа` saying that the text is not ready

### Requirement: Opening loops is serialised with the turn's changing calls

Opening a report loop SHALL run one at a time per turn, in the same queue as the turn's changing
calls. The turn's record of open loops SHALL list every loop opened, whatever the concurrency.

#### Scenario: Concurrent opens

- **WHEN** the planner sends `aifa_open(doc_id=A)` and `aifa_open(doc_id=B)` concurrently
- **THEN** both loops open one after the other, and the turn lists both

#### Scenario: Open during a move

- **WHEN** `aifa_move(doc_id=A)` is executing and the planner calls `aifa_open(doc_id=B)`
- **THEN** the loop on B starts opening only after the move has returned

### Requirement: A turn holds a bounded number of loops

A turn SHALL hold at most six open report loops. A call that would open a seventh SHALL be refused
with the new code `лимит_циклов` and open nothing; the open loops keep working. The market loop
(`mode=market`) SHALL stay one per turn, and a turn SHALL NOT mix it with report loops.

#### Scenario: The seventh loop is refused

- **WHEN** six report loops are open and the planner calls `aifa_open(doc_id=G)`
- **THEN** the answer is `ОТКАЗ лимит_циклов` and no loop opens on G

#### Scenario: Market and report loops do not mix

- **WHEN** a report loop on A is open and the planner calls `aifa_open(mode="market")`
- **THEN** the answer is `ОТКАЗ неверные_аргументы` and no market loop opens

### Requirement: Loop-bound calls name their loop when several are open

The following calls SHALL accept `doc_id`: `aifa_move`, `aifa_register_atom`, `aifa_report`,
`aifa_atoms`, and `aifa_state` without artifact arguments. With exactly one open loop, `doc_id`
MAY be omitted. With several open loops, a call without `doc_id` SHALL be refused with
`неверные_аргументы` listing the open documents. Such a refusal SHALL spend no move and count in
no loop's refusal streak.

#### Scenario: Move without doc_id among two loops

- **WHEN** loops on A and B are open and the planner calls `aifa_move` without `doc_id`
- **THEN** the call is refused with `неверные_аргументы` listing A and B, and neither loop's moves
  or refusal count change

#### Scenario: Artifacts of another document are readable

- **WHEN** the loop on A produced artifact `e1` and the planner calls `aifa_state(artifact_ids=["e1"])`
  while the loop on B is the one it last moved
- **THEN** the answer contains `e1`

#### Scenario: Subtask registers into the named loop

- **WHEN** loops on A and B are open and a subtask calls `aifa_register_atom(doc_id=B, …)`
- **THEN** the atom is registered into the loop on B only

### Requirement: Waiting for a subtask uses the turn's latest deadline

`aifa_wait(task_id=…)` SHALL bound its wait by the latest wall-time deadline among the turn's open
loops, without needing `doc_id`.

#### Scenario: Two loops with different deadlines

- **WHEN** the loop on A ends at 12:00 and the loop on B ends at 12:10, and the planner waits for a
  subtask at 11:55
- **THEN** the wait may last until 12:10

### Requirement: Changing calls of a turn run one at a time across its loops

Calls that change a loop (`aifa_move`, `aifa_finish`, `aifa_register_atom`) SHALL be serialised per
turn, not per loop. A second changing call of the same turn SHALL wait for the first, even when it
targets another document.

#### Scenario: Moves on two documents do not overlap

- **WHEN** the planner sends `aifa_move(doc_id=A)` and `aifa_move(doc_id=B)` concurrently
- **THEN** the second starts only after the first has returned

#### Scenario: Alternating moves keep both loops valid

- **WHEN** the planner moves A, then B, then A again
- **THEN** each move succeeds, and the artifacts of both loops stay readable

### Requirement: One finish closes the whole turn

`aifa_finish` SHALL close every open loop of the turn and accept evidence ids from any of them. It
SHALL publish a single result, preceded by one `turn_documents` event listing every document whose
loop was opened. After it, every loop-changing call of that turn SHALL be refused with
`цикл_закрыт`.

#### Scenario: Finish with evidence from two documents

- **WHEN** loops on A and B are open and the planner calls `aifa_finish` citing `e1` from A and
  `e2` from B
- **THEN** the finish is accepted, one `turn_documents` event lists A and B, and one result is
  published

#### Scenario: Move after finish

- **WHEN** `aifa_finish` was accepted and the planner calls `aifa_move(doc_id=A)`
- **THEN** the answer is `ОТКАЗ цикл_закрыт`

### Requirement: Open loops survive a service restart

Each open loop of a turn SHALL be saved and restored separately, by its turn and document. After a
restart, a call naming `doc_id` SHALL reach the restored loop of that document with its moves and
artifacts intact. `aifa_finish` after a restart SHALL close all loops listed for the turn.

#### Scenario: Restore two loops

- **WHEN** loops on A and B are open, the service restarts, and the planner calls `aifa_state(doc_id=B)`
- **THEN** the answer is the state of the loop on B as saved before the restart

### Requirement: A single-document turn behaves as before

A turn whose set holds one document, or several with an active one, SHALL work with the same calls,
arguments and loop answers as before this change; `doc_id` SHALL never be required in it. Only
the `aifa_doc` listing gains fields, and the finish adds the `turn_documents` event.

#### Scenario: Unchanged single-document path

- **WHEN** a turn's set is `[A]` and the planner calls `aifa_open()`, `aifa_move(...)` and
  `aifa_finish(...)` without `doc_id`
- **THEN** each call behaves as it did before this change
