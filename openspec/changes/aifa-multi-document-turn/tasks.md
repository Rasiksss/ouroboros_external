# Tasks

Paths are relative to `aifa/finengine/src/finengine/` unless they start with `aifa/`, `skills/` or
`aifa-symbiosis/`. Service tests are in `aifa/finengine/tests/`.

Prerequisite: `aifa-workspaces` and `aifa-document-search` merged into `dev`.

Two step branches. Each carries its own ADR, because F13 counts each branch from `merge-base dev`:
- **step A** = groups 1–6: the multi-document turn;
- **step B** = groups 7–9: presets.

**RFA tests (applies to every task).**
- A task that ports RFA behaviour lists the RFA tests (`AIFA_RFA/tests/…`) it carries over.
- A test bound to RFA infrastructure (their DB fixtures `session`/`workspace`/`owner`, OCR JSON,
  workers, AnalysisRun) is rewritten on our fixtures, not dropped.
- A test is skipped only when its behaviour is outside this plan, and the skip is written down
  with its reason.
- Such a task is done when the ported tests are green, and the step report has a table
  «тест RFA → наш тест | пропущен: причина».

The tests of RFA's AnalysisRun, resolver, cancellation and worker (`test_analysis_runs.py`,
`test_analysis_cancel.py`, `test_analysis_context.py`, `test_analysis_scope_wait.py` and the
like) are skipped: those parts are not ported (non-goals in design).

## 1. Step A decision record

- [ ] 1.1 Write ADR A (next free number, status «предложено»). It records D1–D8, the non-goals,
      and these changes:
      - `scope` in `POST /chat/turns`;
      - the `doc_id`/`offset` arguments;
      - one finish per turn and the event `turn_documents`;
      - the refusal code `лимит_циклов`;
      - the frontend additive change (relative to ADR-0015);
      - one queue per turn, which replaces the "per `LoopSession`" wording of ADR-0043 p. 4 and
        ADR-0092, with report-loop opening inside the queue (D3);
      - the departure from RFA: `all_workspace` lists never-parsed documents;
      - the `text_pages`/`profile_id` columns.

      In the same branch, update:
      - `CONTRACTS.md` §1 (`scope`, `turn_documents`);
      - `CONTRACTS.md` §3.1: the queue paragraph, so `aifa_open` of a report loop is no longer
        listed as out of queue;
      - `CONTRACTS.md` §3: tool rows for `aifa_open`, `aifa_doc`, `aifa_move`, `aifa_state`,
        `aifa_atoms`, `aifa_register_atom`, `aifa_report`, `aifa_wait`, `aifa_finish`;
        `лимит_циклов` in §3.0; checks numbered from the next free C3 number;
      - `ARCHITECTURE.md` §1 p. 5 and §7 «Цикл конечен» (≤ 6 report loops per turn).

      Verify: `uv run pytest aifa/tests/architecture/test_adr_discipline.py` green. Gate: owner
      «принято».

## 2. Turn scope and workspace

- [ ] 2.1 `QueryRequest.scope` (`models/query.py`). New test `tests/test_turn_scope.py`:
      `POST /chat/turns` with `scope="everything"` → 422, with no `TurnRecord` remembered. Fails
      before: 200.
      - Producer/consumer: the route `chat_turn_start` → `turn_record.recall`.
      - Allowed fake: `composition.turn_gateway()`.
- [ ] 2.2 Workspace derivation and the `all_workspace` branch in `_chat_turn_docs` (D7), plus
      `scope` and `workspace_id` in `TurnRecord`, with a tolerant `recall`. Assertions in
      `tests/test_turn_scope.py`:
      - `project_id=F1` (own folder) with `all_workspace`, where F1 holds ready A and B and a
        never-parsed C and F2 holds D → `record.doc_ids == (A, B, C)`. Fails before: `()`.
      - `project_id=F9` of another user → workspace `owner:<caller>`, with no foreign documents;
      - `project_id=P`, a workspace of the caller that is not a folder in the stored dialogs →
        the default workspace;
      - a chat outside folders → the default workspace's documents;
      - `doc_ids=[B]` narrows to `(B,)`;
      - an `active_doc_id` outside the workspace is not added;
      - no `scope` gives today's set, and an empty chat gives `()`;
      - a `pinned_tool` turn ignores `scope`;
      - `recall` of a record without the new fields works.

      RFA tests, from `test_conversation_scope.py` (9):
      - rewrite on `_chat_turn_docs`:
        - `test_another_owners_document_cannot_be_attached` (a foreign document is dropped);
        - `test_two_chats_of_one_owner_do_not_see_each_other` (as two folders);
        - `test_an_empty_key_never_becomes_a_wildcard` (an empty chat stays empty);
        - `test_a_document_being_deleted_is_not_in_scope` (as "deleted").
      - skip:
        - `test_an_admin_cannot_attach_someone_elses_document_either`: AIFA admins see every
          document (`_user_can_see`), and this plan keeps that.
        - `test_ready_only_scope_excludes_unfinished_documents`: deliberate deviation, ADR A.
        - attach/detach and "one document in several chats": chat membership rows are not in
          this plan.

## 3. Listing and ranked selection

- [ ] 3.1 New column `document.text_pages` (start-up DDL), set right after `save_document_text`
      by a single-column `set_text_pages`. The listing in
      `read_tools._listing`/`ouro_view.documents` reads:
      - registry and meta-card fields;
      - `text_pages`, computed once and stored if missing;
      - readiness.

      It is paged by `offset` in entries (D6). Add `offset` to the `aifa_doc` schema. Tests in
      `tests/loop_tools/test_read_tools.py` and `tests/test_documents_store.py`:
      - after a full parse, including the upserts at `api/main.py:580-606`, `text_pages` still
        equals `len(pages_of(text))`. Fails if it lived in `meta`.
      - with `document_text` patched to raise, a listing of documents that have `text_pages` still
        answers. Fails before: full texts are read.
      - the single-document page count equals `len(pages_of(text))`, the same number as before;
      - 30 documents → a next offset, then null;
      - a never-parsed document shows not ready.
- [ ] 3.2 Ranked `aifa_doc(query=…)` (D6). Tests, with a fake `select_documents` at the search
      boundary:
      - ranked documents come first in the fake's order, followed by the rest in set order;
      - an id the fake returns outside `record.doc_ids` is dropped;
      - the workspace is passed only for `all_workspace`;
      - the fake raising → the plain listing plus the "ранжирование недоступно" note.

      Fails before: refused with "pages и query нужны вместе с doc_id".

## 4. Loops per document

- [ ] 4.1 **Refactor commit (compatible, separate).** Move `OpenLoop.queue` to
      `LoopDesk.queue_of(run_id)`, and give `LoopDesk` a loop key helper (D2, D3). Behaviour is
      unchanged. Composition check, green with no test edits:
      - `tests/loop_tools/test_open_and_restore.py`
      - `tests/loop_tools/test_cycle_and_queue.py`
      - `tests/loop_tools/test_equivalence.py`
      - `aifa/tests/contract/test_loop_end_to_end.py`
- [ ] 4.2 `aifa_open(doc_id)` (`goal_tools.py`, `loop_opening.py`, `loop_desk.py`): resolve the
      document first, then key `run_id/doc_id`, with claim and job kind per document, open inside
      the turn queue, and `LOOP_INDEX`. Change the "after aifa_open" check in `read_tools.py` to
      the kind family (D2). Write the new column `document.profile_id` at loop open with a
      single-column UPDATE (`production_runtime.py`; start-up DDL). New tests in
      `tests/loop_tools/test_multi_document_loops.py`:
      - open A then B → both live, both versions active in the Case, the index is `[A, B]`.
        Fails before: the second call returns A's loop.
      - `aifa_open()` then `aifa_open(doc_id=A)` with set `[A]` → the same session object;
      - concurrent opens of A and B → both in the index;
      - an open of B while a move on A runs starts after the move;
      - `aifa_doc` while `aifa_open(doc_id=A)` is still running is not refused as "before open";
      - Z outside the set → `нет_документа`, and C without text → `нет_документа` (text not
        ready);
      - the ambiguity text contains `doc_id`;
      - `profile_id` is set for A after the open, and is still set after a later doc-type patch or
        ingestion upsert.
- [ ] 4.3 Loop limit 6 plus `Refusal.LOOPS_LIMIT = "лимит_циклов"`. Tests:
      - the 7th open → `ОТКАЗ лимит_циклов`, and the six still move;
      - market after report → `неверные_аргументы`, and report after market → the same.
- [ ] 4.4 `doc_id` routing for `aifa_move`, `aifa_register_atom`, `aifa_report`, `aifa_atoms` and
      `aifa_state` without artifact arguments, plus the plugin schemas. `aifa_wait(task_id)` uses
      the latest deadline. Each call checks the document still exists (D4, D8). Tests:
      - two loops and no `doc_id` → `неверные_аргументы` listing A and B. Neither loop's `turns`
        nor refusal count changes. Fails before: the call goes to the first loop.
      - one loop and no `doc_id` → works;
      - `aifa_register_atom(doc_id=B)` registers only into B;
      - `aifa_atoms(doc_id=B)` shows B's registry;
      - `aifa_state(artifact_ids=[e1])` returns A's artifact;
      - `aifa_wait` with loops ending 12:00 and 12:10 waits until 12:10;
      - A deleted → `aifa_move(doc_id=A)` gives `нет_документа`, with no new Case document version;
      - concurrent moves on A and B serialise (gated fake executor), which guards the 4.1 queue;
      - alternating moves A → B → A succeed, and both loops' artifacts stay readable.

      Verify: `test_schema_budget.py` is green (total ≤ 9,000 < limit 10,000).
- [ ] 4.5 Turn-level `aifa_finish` and the `turn_documents` event (D5), in `loop_tools.py` and
      `production_runtime.publish_result`.
      - Producer/consumer: `FinishTool` → `publish_result` → SSE `turn_documents`, consumed by
        `aifa/web/src/lib/api.ts`.
      - Allowed fake: the event sink.

      Tests:
      - evidence `e1` (A) and `e2` (B) are accepted;
      - exactly one `turn_documents` with `[A, B]` before the `typed_result` events;
      - `aifa_move(doc_id=A)` afterwards → `цикл_закрыт`.

      Update the shared SSE fixtures (C1.2) and the golden normaliser in `aifa/tests/golden/`.
      Verify: `aifa/tests` green.
- [ ] 4.6 Restore and sweep (D2, D8). Tests in `tests/loop_tools/test_open_and_restore.py`:
      - two loops saved, a fresh `LoopDesk()`, then `aifa_state(doc_id=B)` → B's saved state;
      - `aifa_finish` closes both restored loops;
      - a loop whose document text is gone is not restored;
      - `_sweep(run_id)` deletes the index and both `run_id/<doc>` states.
- [ ] 4.7 Single-document path. Run `uv run pytest aifa/tests/contract` and
      `uv run --project aifa/finengine pytest aifa/finengine/tests/loop_tools
      aifa/finengine/tests/dynamic_run`. Verify: green without edits to existing assertions, except
      the listing fields and the added `turn_documents` event in golden traces.

## 5. Skill texts

- [ ] 5.1 Update three skill texts. Run `scripts/resolve_rules.py` on each file first and read the
      rules it names.
      - `skills/aifa-planning/SKILL.md`, section «Несколько документов»: the listing and
        `aifa_doc(query)`; open only what the question needs; `doc_id` on calls; one move per
        document, up to three goals in it (ADR-0089); one `aifa_finish`; put `doc_id` into
        subtask task texts.
      - `skills/aifa-atom-author/SKILL.md`: pass the task's `doc_id` to `aifa_register_atom` and
        `aifa_report`.
      - `skills/aifa-plan-author/SKILL.md`: the plan names the documents it works on.

      Verify: new assertions in `skills/aifa-engine/tests/test_planning_text.py` and
      `test_atom_author_text.py` fail before and pass after.

## 6. Frontend and step A handover

- [ ] 6.1 Add a toggle «Все документы папки» in `aifa/web/src/components/workspace/`. When on, the
      body carries `scope: "all_workspace"` and `doc_ids: []`; when off, the body is unchanged. The
      answer shows the document names from `turn_documents`. Verify with vitest:
      - the request body in both states;
      - the names render from a fixture event.

      `npm test` green.
- [ ] 6.2 Run `aifa/scripts/check.sh` and paste the result. Get an independent `/code-review` in a
      clean context (`docs/INVARIANTS.md` for `skills/aifa-*`, `REVIEW_GUIDE.md`). Add
      `PROGRESS.md` entries. Verify: no open blockers.

## 7. Step B decision record

- [ ] 7.1 Write ADR B (next free number). It records D9, the tool `aifa_compare`, RFA's
      `comparison_policy_v1` as ported, the attribute sources and the F9 change. Update
      `CONTRACTS.md` §3 (the `aifa_compare` row and the next free C3 number). Verify: F13 green
      on the step B branch. Gate: owner «принято».

## 8. Presets

- [ ] 8.1 New pure module `skills/cross_document.py`, porting RFA `analysis/policy.py` and the
      preset rules (D9).

      RFA tests: **rewrite** `test_analysis_presets.py` (40). All of them use RFA's
      `session`/`workspace` fixtures, so they are rebuilt on Case-fact fixtures with the same
      inputs and assertions.
      - Rewrite:
        - retrospective: YoY and CAGR in Python, chronological order, the quarter-and-year chain,
          a change of scale, CAGR by calendar, one point for agreeing documents, two documents of
          one period;
        - cross-source: both numbers kept, agreement not reported as a disagreement, a real
          conflict reads as one, two scales are not two answers, another unit not compared;
        - peer: periods a peer is missing, no common period;
        - shared: a facts set outside the frozen scope is not read (as "outside the Case"), an
          empty scope, every cell names its source, missing is not zero, the company is named not
          slugged, trailing zeros, coverage names a document that gave nothing.
      - Skip, as not in this plan:
        - "which extraction run wins": `*_does_not_win`, the partial/role/rolled-back runs, and
          `test_only_a_cut_off_current_run_counts_as_incomplete`;
        - the period-schema versions v1/v2;
        - the prompt-projection and frozen-column-order tests tied to RFA's matrix renderer.

      From `test_multidoc_isolation.py`, port the `comparison_policy` case here, and the scenario
      read isolation in 8.2.

      Add tests:
      - 100 vs 106 with `TOTAL_ASSETS` = 500 → material;
      - 100 vs 102 → minor;
      - 100 vs 110 with no benchmark → minor plus an "unknown threshold" note;
      - sources 100 / 103 / 107 → one spread 100↔107, with no source marked correct;
      - two documents with an identical value and basis → `agreement`;
      - percent metrics: the p.p. rule;
      - reordered ids → an identical result;
      - RUB, RUB, USD → USD excluded;
      - IFRS vs RSBU in peer → excluded as "разные стандарты";
      - two companies in retrospective → two series;
      - FY 2023+2024 against FY 2024 only → rows for both periods plus a gap naming the peer;
      - no common period → two rows and two gaps, not an empty table;
      - a rank without evaluative words.

      Fails before: the module does not exist.
- [ ] 8.2 `assistant/compare_tool.py` and `ouro_view.compared`, wired into `ToolApi`,
      `tool_names.py`, plugin `TOOLS`, compose `OUROBOROS_ENABLED_TOOLS`, `test_schema_budget.py`
      `EXPECTED_TOOLS` and the snapshots (`tools.snapshot.json`, `tool_names.json`,
      `dev_config.snapshot.json`). Tests in `tests/loop_tools/test_compare_tool.py`:
      - an unknown preset → `неверные_аргументы`;
      - ids from another Case → missing, then `нет_входов`;
      - a non-metric artifact id → missing;
      - it answers while the queue is held;
      - the company comes from the meta card, and the standard from the `profile_id` column, with no
        arguments involved;
      - every value carries its id;
      - an over-limit table is cut by rows with a note.

      RFA tests: from `test_multidoc_isolation.py` (14), rewrite the read-isolation cases as "a
      preset reads only this turn's Case and active documents". Skip the registry-claim cases
      tied to RFA's scenario registry, which is not in this plan.

      Verify: `test_dev_config.py` and `test_schema_budget.py` are green (total ≤ 9,000 < 10,000).
- [ ] 8.3 Add a presets section to `skills/aifa-planning/SKILL.md`: when to use each preset;
      include `TOTAL_ASSETS`/`SALES_REVENUE` ids for materiality; comment only on the shown
      numbers. Verify: new assertions in `test_planning_text.py` fail before and pass after.

## 9. Verification and handover

- [ ] 9.1 Run `aifa/scripts/check.sh` and paste the result, comparing with the baseline.
- [ ] 9.2 Get an independent `/code-review` of the step B diff in a clean context, citing
      file:line. Verify: no open blockers in the review file.
- [ ] 9.3 Live check on aifa-sym (only on the owner's «выкатывай»; prod untouched). Verify each
      outcome and record it in `PROGRESS.md` §3:
      - two Сбербанк reports, «чистая прибыль 2023 и 2024» → two loops, and one `turn_documents`
        naming both;
      - Сбербанк and ВТБ 2024 → `aifa_compare(peer_comparison)` in the trace, and the answer's
        numbers match the table;
      - «Все документы папки» over a folder of 10+ reports → `aifa_doc(query)` in the trace, and
        only the needed loops open;
      - a five-bank peer turn finishes within `OUROBOROS_HARD_TIMEOUT_SEC` (1800 s). Record its
        wall time; if it does not finish, record the parallel-loops revisit (D3) in `PROGRESS.md`
        §4;
      - a single-report turn → the same as before.
- [ ] 9.4 Add `PROGRESS.md` §1/§3/§4 entries and run `graphify update .`. Verify: the entries
      exist, and the rebuild exits 0.
