## Why

A chat in AIFA already holds several reports. The frontend sends all of them with every turn, and
`aifa_doc` can list and read each one. The analysis loop still runs on exactly one: `gate_document`
(`aifa/finengine/src/finengine/assistant/loop_opening.py:67`) refuses a turn with several documents
and no active one. As a result the questions the RFA multi-document contour answers get no answer
here: "Sberbank 2023 vs 2024", "Sberbank vs VTB", "press release vs IFRS report".

This change ports the behaviour of `AIFA_RFA/docs/MULTIDOCUMENT_TECHNICAL_SPEC.md` that fits the
symbiosis split: ouroboros plans and resolves dialogue context, and the service holds documents
and Case and runs the code. It is the last of three changes. It builds on `aifa-workspaces`
(server-side folder membership) and `aifa-document-search` (OpenSearch projection, `aifa_search`).

## What Changes

- **Turn scope.** `POST /chat/turns` takes `scope: "conversation" | "all_workspace"`:
  - The default is `conversation`, today's behaviour.
  - `all_workspace` is an explicit user choice. It freezes every document of the turn's workspace
    as the candidate set; documents whose text is not ready are listed but cannot be opened.
  - The turn's workspace is the request's `project_id` when it names a workspace of the caller
    that the caller's stored dialogs list as a folder (a chat in a folder already sends the
    folder id). Otherwise it is the owner's default workspace.
  - The system never switches to `all_workspace` on its own.
  - The set is frozen in `TurnRecord` when the turn is created.
- **Document listing and selection.**
  - `aifa_doc` without arguments returns registry metadata for each turn document (company,
    period, type, language, pages, readiness) without loading texts, paged.
  - `aifa_doc(query=…)` ranks the turn's documents with the selection of `aifa-document-search`:
    ranked ones first, then the rest. When search is down, it falls back to the plain listing.
- **One loop per document, in the turn's Case.**
  - `aifa_open` takes an optional `doc_id`, and every report loop is keyed by its resolved
    document.
  - A turn holds at most six loops, and `aifa_open` runs in the turn's queue.
  - Every loop-bound tool takes `doc_id` when more than one loop is open: `aifa_move`,
    `aifa_state`, `aifa_atoms`, `aifa_register_atom`, `aifa_report`.
  - All loops write to the turn's Case (`case_id = project_id or session_id`, so a folder's chats
    share one), which already supports several active documents.
- **Turn-level finish.**
  - `aifa_finish` closes every loop and accepts evidence from any of them.
  - It publishes one result.
  - A new additive event `turn_documents {document_ids}` tells the screen which documents were
    used.
- **Cross-document presets.**
  - A new read-only tool `aifa_compare(preset, artifact_ids)` computes `retrospective`,
    `peer_comparison` or `cross_source_verification` in code over metric facts already in the
    Case.
  - The rules are RFA's, ported as they are:
    - the cross-source spread runs between the smallest and largest value, with no winning
      source;
    - `agreement` means identical value and basis in at least two documents;
    - the materiality threshold comes from the company's `TOTAL_ASSETS`/`SALES_REVENUE`, and an
      unknown threshold gives `minor_difference`;
    - peers are checked by standard, currency, scale and basis.
  - The model only comments on the numbers shown.
- **Frontend.**
  - A chat-level toggle «Все документы папки» sends `scope=all_workspace` and an empty `doc_ids`.
  - The answer shows the names from `turn_documents`.
- **Not ported from RFA** (non-goals, each recorded in design): the document lease and worker
  state machine, the service-side context resolver with slot deltas, `AnalysisRun` persistence
  and idempotency, and llm_balancer. The Workspace table and OpenSearch are ported by the two
  sibling changes.

## Capabilities

### New Capabilities

- `aifa-turn-documents`: which documents an AIFA turn may use, and how. Covers scope
  (`conversation`/`all_workspace`), the frozen set, owner checks, the listing and ranked
  selection, loops opened per document, and one turn-level finish.
- `aifa-cross-document-presets`: deterministic cross-document tables over Case facts
  (retrospective, peer comparison, cross-source verification) and their comparability and
  materiality rules.

### Modified Capabilities

None. Existing `openspec/specs/` cover the ouroboros core, and this change does not touch it.

## Impact

- Service (`aifa/finengine/src/finengine/`):
  - `api/main.py` (`QueryRequest.scope`, `_chat_turn_docs`, `_start_ouroboros_turn`,
    `text_pages` at text publish)
  - `models/query.py`
  - `assistant/turn_record.py`, `assistant/loop_opening.py`, `assistant/loop_desk.py`
  - `assistant/goal_tools.py`, `assistant/loop_tools.py`, `assistant/read_tools.py`,
    `assistant/atoms_tool.py`, `assistant/atom_tools.py`
  - `assistant/ouro_view.py`
  - `skills/production_runtime.py` (`publish_result`; the profile written at loop open)
  - `store/documents.py`: new columns `text_pages` and `profile_id`, written by single-column
    UPDATEs, so the whole-`meta` writers cannot erase them
  - a new `assistant/compare_tool.py` and `skills/cross_document.py` (a port of RFA
    `analysis/policy.py` and the preset rules)
  - `assistant/composition.py` / `tool_api.py`
- Plugin `skills/aifa-engine/plugin.py`: the `doc_id`/`offset` arguments and the `aifa_compare`
  tool. They must fit the F4 target of 9,000 under the 10,000 budget set by
  `aifa-document-search`, keeping the 1,000 margin.
- Skill texts `skills/aifa-planning/`, `skills/aifa-atom-author/` and `skills/aifa-plan-author/`.
- Frontend `aifa/web/src/components/workspace/`: the toggle and the `turn_documents` event.
- Symbiosis governance: two ADRs, one per step branch (F13), each with the next free number.
  - `CONTRACTS.md` §1 (`scope`, the new event), §3 (tool table, the next free C3 numbers) and the
    shared SSE fixtures (C1.2, ADR-0045).
  - `ARCHITECTURE.md`.
- Ouroboros core: untouched (F1 stays green).
- Requires `aifa-workspaces` and `aifa-document-search` to be merged first. It adds no dependency
  or infrastructure beyond theirs.
