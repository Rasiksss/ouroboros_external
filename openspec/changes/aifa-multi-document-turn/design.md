## Context

See `proposal.md` (Why) and the two delta specs for required behaviour. Observed state of the
service (`aifa/finengine/src/finengine/`):

- **The turn already carries a set.**
  - The owner check is done for the whole set at the door (`api/main.py` `_docs_of_the_turn`,
    `_chat_turn_docs`).
  - `_chat_turn_docs` appends `active_doc_id` to the candidates (`api/main.py:1286-1287`).
  - `TurnRecord.doc_ids` is frozen in the coordination cache, and `aifa_doc` lists and reads every
    document in it (`assistant/read_tools.py:146-163`). The listing reads every full text.
- **Case and workspace come from `project_id`.**
  - A chat in a folder sends `project_id = folder_id` (`aifa/web/.../use-chat-run.ts:950`), and
    `case_id = project_id or session_id` (`api/main.py:1564`). So a folder's chats share a Case,
    and the request never carries the chat's own id.
  - The frontend always sends all chat `doc_ids` (`use-chat-run.ts:840-842, 947`).
- **The Case is multi-document.**
  - Several documents can be active in one Case (`store/cases.py:369-411`).
  - Evidence ids resolve against the whole Case (`skills/spec/dynamic_run.py:1199-1237`), and so
    does `aifa_state(artifact_ids)`.
  - `_bind_target_to_case` accepts two loops, because each target has its own
    `document_version`.
  - `closed` is persisted (`skills/spec/loop_state.py:59, 78`).
- **The loop is single-document.** `prepare_production_context` classifies one text and freezes
  one passport and registry; `profile_id` → (standard, company_type, language) is in
  `skills/atoms/run_passport.py:36-50`.
- **Loop identity is the run.**
  - Keys: `LoopDesk._open[run_id]`, state `LOOP_SESSION key=run_id`, the claim
    `loop_opening key=run_id`, the job kind `OPEN_KIND`.
  - The "after aifa_open" check in `read_tools.py:83, 141` matches the job kind exactly
    (`jobs.py:53-58`).
  - `OpenLoop.queue` is per loop.
  - `_sweep` deletes only `key=run_id` (`loop_desk.py:129-134`), and the state store has no TTL
    (`store/state.py:76-79`).
  - `CaseRuntime.register_document` takes no `case_lock` (`skills/case_runtime.py:204-217`).
- **Loop-bound tools** (all via `open_loop_of`):
  - `aifa_move`, `aifa_finish`, `aifa_register_atom` (`ExclusiveTool`);
  - `aifa_report` (`atom_tools.py:86`), `aifa_atoms` (`atoms_tool.py:45`, the registry is per
    report);
  - `aifa_state`, `aifa_doc`;
  - `aifa_wait(task_id)` (`goal_tools.py:156-157`, the loop's deadline).

  The subtask skill `aifa-atom-author` calls `aifa_register_atom`/`aifa_report`.
- **Result publication.** `publish_result` emits only `typed_result` and `file` events
  (`skills/production_runtime.py:878-905`), and no `analysis_card` exists on this path.
- **Document metadata.**
  - v2 uploads leave `company_name`/`period`/`doc_type` empty (`use-doc-upload.ts:339-347`). The
    meta card holds `doc_kind`/`issuer`/`period`, and `doc_kind` does not distinguish IFRS from
    RSBU.
  - `total_pages` is 0 for txt/md, and the physical page count after cloud OCR.
  - Every write of `meta` replaces the whole JSON: the ingestion upserts at `api/main.py:580-606`
    run after the text is already published at line 575; `update_document_meta`; and the
    doc-type patch at `api/main.py:1390`.
- **F4.** After `aifa-document-search`, the ouroboros budget is 10,000 and the target is 9,000.
  Ouroboros drops a schema that does not fit, without an error (`ouroboros/agent/task_graph.py:185`).
- **The queue is part of the contract.** CONTRACTS §3.1 lists `aifa_open` as out of queue, and
  ADR-0092 says "очередь `aifa_move` по одному на `LoopSession`" (ADR-0043 p. 4).
- **RFA's universe for `all_workspace` is READY documents only** (RFA §7).
- **Governance.**
  - The ouroboros core is frozen (F1).
  - The tool set, the refusal list and the ouroboros env change only by ADR.
  - F13 counts each step branch from `merge-base dev`, so each branch that touches snapshots or
    governed docs carries its own ADR.

## Goals / Non-Goals

**Goals:**
- Several documents in one turn, with the loop machinery reused per document unchanged.
- The single-document path keeps its calls and loop answers (contract tests C3.x stay green
  without edits).
- Presets follow RFA's policy and cite Case facts.

**Non-Goals** (RFA parts deliberately not ported):
- **No Document state machine, lease or worker pools.** "Ready" means `document_text_exists`.
- **No Workspace table or search projection here.** They come from `aifa-workspaces` and
  `aifa-document-search`; this change only consumes them.
- **No automatic narrowing of the set** (RFA §7 resolver). The planner ranks with `aifa_doc(query)`
  and chooses which loops to open.
- **No service-side context resolver or slot deltas.** Ouroboros holds the dialogue (user
  decision; ADR-0060/0061).
- **No AnalysisRun table, idempotency key or clarification run.** The `run_orchestrator` run and
  the tool calls' `operation_id` already cover this.
- **No llm_balancer, and no currency conversion.**
- **No parsing triggered by `all_workspace`.** Never-parsed documents are listed as not ready
  (user decision). This deliberately departs from RFA's READY-only universe, and ADR A records it:
  the planner sees that a folder document exists but cannot be read, and can tell the user.
- **No cross-loop use of the `set.assemble`/`calc.series` atoms.**

## Decisions

### D1. One loop per document, not one loop over many documents

A loop is keyed by its document. Everything inside a loop stays as it is: passport, registry, node
keys, gates.

- **Alternative: one loop holding several texts.** The passport and registry are per report, so
  merging them rewrites the engine.
- **Alternative: one active loop, switched by `doc_id`.** It hides which loop is live, and D3
  makes parallel loops cost no more.

### D2. Keys by the resolved document

`aifa_open` resolves the document first (`doc_id` → only document → active → refuse), then:
- the key is `f"{run_id}/{doc_id}"` for every report loop, whichever way it was resolved;
- the market loop keeps `run_id`;
- the opening claim key and the job kind (`aifa_open:<doc_id>`) follow the same rule;
- the "after aifa_open" check in `read_tools.py` (and `aifa_search`) changes from an exact job kind
  to "any job of kind `aifa_open` or `aifa_open:*` exists, or a loop of this turn is open";
- `state.LOOP_INDEX key=run_id → [doc_id, …]` lists the opened report loops in order;
- `_sweep` deletes the index and every `run_id/<doc_id>` loop state with `run_id`'s own key. The
  store has no TTL, so without this the state would pile up.

**Alternative: keep `run_id` for the implicit open.** Then `aifa_open()` followed by
`aifa_open(doc_id=A)` opens two loops on A, and A drops out of the index (the review's B4).

### D3. One queue per turn, and report opening runs in it

- `OpenLoop.queue` moves to `LoopDesk.queue_of(run_id)` (one `asyncio.Lock` per run, in-process as
  now).
- Changing calls and report-loop opening take it. Opening keeps its job and `ЖДИ` semantics: the
  job waits for the queue inside its 240 s budget.
- This serialises the `LOOP_INDEX` read-modify-write and the six-loop count. It also keeps
  `register_document` (no `case_lock`) from bumping the Case revision during another loop's move
  (`update_case_head(expected_revision=…)` would raise `CaseConflictError`).
- **This changes the contract and is recorded as such.**
  - CONTRACTS §3.1 lists `aifa_open` as out of queue, and ADR-0092 / ADR-0043 p. 4 say one queue
    per `LoopSession`.
  - ADR A replaces both wordings with "одна очередь на ход: `aifa_move`, `aifa_register_atom`,
    `aifa_finish` и открытие отчётного цикла".
  - The `aifa_open` line of CONTRACTS §3.1 is edited in the same branch.
  - The single-loop behaviour is unchanged, because there one loop is one turn.
- **Consequence: moves on different documents run one after another.**
  - Peer comparison of five banks is at least five sequential moves.
  - Parallelism stays inside a move (ADR-0088/0092), but one move works on one document.
  - The risk to `OUROBOROS_HARD_TIMEOUT_SEC` (1800 s) is in Risks, and the live check measures it.
  - Revisit: parallel loops once Case head writes tolerate concurrent loops (per-loop queues plus
    `case_lock` on `register_document` and `update_case_head`).
- **Alternative: `case_lock` around `register_document` with per-loop queues.** It covers the
  Case race at registration but not the index race, nor concurrent `update_case_head` of two
  moves.

### D4. Six report loops per turn; refusals before a loop is chosen touch no loop

- Six is a constant in `loop_desk.py`. A breach is refused with the new code `лимит_циклов`.
- A refusal for a missing `doc_id` is returned before any loop is selected, so it calls no
  `session.reject`: it spends no move and counts in no streak.
- `aifa_wait(task_id)` takes `max(opened_at + max_wall_seconds)` over the open loops.

### D5. Finish runs on the first loop, closes the rest, and announces the documents

- `aifa_finish` (no `doc_id`) runs today's `check`/`finish` on the first loop in `LOOP_INDEX`.
  Evidence resolves Case-wide.
- It then marks every other loop closed (persisted `closed`) without publishing.
- `publish_result` gets `document_ids` and emits one new additive event
  `{"type": "turn_documents", "document_ids": [...]}` before the `typed_result` events. The
  shared SSE fixtures (C1.2, ADR-0045) and `CONTRACTS.md` §1 are updated.
- `last_word` uses the first loop's limits.

**Alternative: a field on `typed_result`.** There are N of those per answer and none per turn.

### D6. Listing from the registry, with stored page counts and ranked order

- The listing reads `get_document` metadata (the meta card before registry fields for company,
  type and period) and `document_text_exists`. It does not load texts.
- Page count: a new column `document.text_pages`.
  - Right after `save_document_text` in `_parse_doc_in_background`,
    `set_text_pages(doc_id, len(pages_of(text)))` writes it with a single-column UPDATE.
  - The column is added by the start-up DDL from `aifa-workspaces` W3. It is not a `meta` key,
    because the upserts at lines 580-606 that follow would overwrite it.
  - For documents published earlier, the listing computes it once from the text and stores it,
    so the single-document answer keeps today's number.
- Paging: `offset` counts entries (unlike `registry_offset`, which counts characters).
- `aifa_doc(query=…)` without `doc_id` calls
  `search.select_documents(record.doc_ids, query, workspace_id=…)`, passing the workspace only
  for `all_workspace`. It renders ranked documents first, then the rest in set order. On a search
  failure it renders the plain listing with a note.

### D7. Scope in the request; workspace derived from `project_id`; frozen in `TurnRecord`

- `QueryRequest.scope: Literal["conversation", "all_workspace"] = "conversation"`. Pydantic gives
  the 422.
- Workspace: the id is `project_id` only if both hold, else `owner:<id>`:
  - `get_workspace(project_id)` is owned by the caller;
  - the caller's stored dialogs list a folder with that id.

  The second check keeps a stray workspace created by an upload with a chat's id from changing
  that chat's scope. The request has no field that picks the workspace.
- `_chat_turn_docs` for `all_workspace`: `list_documents(workspace_id=…)` (every document, ready
  or not), narrowed by a non-empty `doc_ids`, then `_docs_of_the_turn`. `active_doc_id` is kept
  only if it is already in that set.
- `TurnRecord` gains `scope` and `workspace_id`, and `recall` tolerates records without them.
- The `pinned_tool` path ignores `scope`.
- Frontend: with the toggle on, the body carries `scope: "all_workspace"` and `doc_ids: []`.

### D8. Deleted documents and restore

- Each loop-bound call checks `get_document(doc_id)` before running. If the document is gone, it
  answers `нет_документа`.
- `LoopDesk._restore` refuses to restore a loop whose document text is missing, instead of
  opening it with empty text and registering a new version.

### D9. `aifa_compare`: a read-only tool, with RFA's policy ported

- `assistant/compare_tool.py` handles arguments, Case rows and fact → document lookup by
  `document_version` against `active_documents`.
- `skills/cross_document.py` holds pure functions: a port of RFA `analysis/policy.py`
  (`comparison_kind`, `spread`, `material`, thresholds 5% / 1% / 1 p.p.) and of the preset rules
  in `analysis/presets.py`:
  - retrospective comparisons: one series per company and metric, with the period shape
    following the latest point, the previous/year-ago windows 45–120 / 320–410 days, and CAGR
    where allowed;
  - peer comparisons: rows per period, gaps grouped by company (missing ≠ zero; no refusal for
    different periods), and the standard, currency, scale and basis check;
  - `agreements` (an identical value and basis in at least two documents) and `disagreements`
    (one `spread` between the smallest and largest value per company, metric, period and basis,
    with the benchmark for the largest row). Verification statuses follow; there is no winner and
    no pairwise matrix;
  - the materiality benchmark from `TOTAL_ASSETS`/`SALES_REVENUE` of the same company, basis and
    period. The concept ids exist in our registry.
- Attributes:
  - company and source type come from the meta card, then registry fields;
  - `company_key` comes from `search.card.company_key`, so «ПАО Сбербанк» = «Сбербанк»;
  - standard and company type come from a new column `document.profile_id`, which
    `open_agent_loop` writes with a single-column UPDATE when a loop opens. Every fact comes from
    a loop, so every fact's document has it. It is not a `meta` key: a loop can open between the
    text publish (line 575) and the ingestion upserts (580-606), which would erase it.
- Order independence: groups are keyed by (unit, scale, basis), the largest group wins, and ties
  are broken by unit name.
- It runs out of queue, like `aifa_state`.
- **Alternative: run presets as atoms inside a loop.** Their inputs would have to cross loops.
- **Alternative: port `presets.py` whole.** It is bound to RFA's `facts` rows. Only its rules are
  ported, with RFA's tests where they apply.

### D10. Startup side-effects touched (rule R2.1)

| Side-effect at start | Change | Preserved by |
|---|---|---|
| `composition.tool_api()` builds `ToolApi` once (`@cache`) | adds `CompareTool` | `tools.snapshot.json` test |
| Plugin `register(api)` | `doc_id`/`offset` arguments; `aifa_compare` | `test_schema_budget.py` (target 9,000 < limit 10,000 from the snapshot, exact set) |
| `SqlDocumentStore.__init__` start-up DDL | `ADD COLUMN IF NOT EXISTS text_pages`, `profile_id` | test: existing table without the columns → the store starts and both exist |
| Ouroboros reads `OUROBOROS_ENABLED_TOOLS` | `+ aifa-engine.aifa_compare` | F9 snapshot + `test_dev_config.py` |
| `tool_names.py` map | `aifa_compare → read_source` | `tool_names.json` test |
| `LoopDesk()` created once; lazy restore | new keys, `LOOP_INDEX`, extended `_sweep` | restore and sweep tests |
| DB schema (tables) | none; state docs via `store/state.py` | — |

## Risks / Trade-offs

- **[Planner picks the wrong documents]** → Ranked selection includes chunk owners. The listing
  shows the file name, and the user can narrow with `doc_ids`.
- **[Six loops cost six budgets]** → `OUROBOROS_HARD_TIMEOUT_SEC` caps the turn, and the skill text
  says to open only what the question needs.
- **[Sequential moves across documents can hit the hard timeout]** Five documents need at least
  five moves in a row (D3). → Mitigations:
  - the skill text: one move per document, up to three goals in it (ADR-0089), and presets over
    facts already in the Case instead of re-extracting;
  - the live check measures a five-document turn against 1800 s;
  - if it is too slow, the recorded revisit is parallel loops (D3).
- **[Schema budget]** The new arguments and tool must stay under 9,000. → `test_schema_budget.py`
  fails before ouroboros would silently drop a tool.
- **[Opening waits for a running move]** → Opening keeps its `ЖДИ` job semantics, and the planner
  polls with `aifa_wait`, as today.
- **[No size benchmark among the named facts]** → The status falls to `minor_difference` with a
  note (RFA behaviour), and the skill text tells the planner to include
  `TOTAL_ASSETS`/`SALES_REVENUE`.
- **[Meta card empty for some documents]** → Those facts go without a company and are excluded from
  peer and retrospective grouping, with a reason. They are never mixed in.
- **[Frontend change vs ADR-0015]** → The ADR records the toggle and `turn_documents` as additive.
- **[Rollback with new `TurnRecord` fields in the cache]** The old `recall` raises on unknown
  fields. → The rollback step deletes `ouro_turn:*` keys, which ends in-flight turns, as a restart
  does.

## Migration Plan

1. Deploy `engine`, `ouroboros` (plugin schema, enabled tools) and `frontend` on aifa-sym at the
   owner's signal. Report loops in flight lose their loop (new keys); the Case artifacts survive.
2. Rollback: redeploy the previous images, then delete the coordination-cache keys `ouro_turn:*`.
   Leftover `run_id/<doc>` state documents are inert, and the old `_sweep` ignores them. A
   one-line cleanup query is in the ADR.
