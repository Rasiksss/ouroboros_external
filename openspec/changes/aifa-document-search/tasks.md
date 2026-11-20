# Tasks

Paths are relative to `aifa/finengine/src/finengine/` unless they start with `aifa/`, `skills/` or
`aifa-symbiosis/`. Service tests are in `aifa/finengine/tests/`. Tests that need OpenSearch run
against a disposable `opensearchproject/opensearch` container (the same pattern as PostgreSQL 16,
`aifa/AGENTS.md` §12) and are skipped without its address. Requires `aifa-workspaces` merged.

**RFA tests (applies to every task).**
- A task that ports RFA behaviour lists the RFA tests (`AIFA_RFA/tests/…`) it carries over.
- A test bound to RFA infrastructure (their DB fixtures `session`/`workspace`/`owner`, OCR JSON,
  workers, AnalysisRun) is rewritten on our fixtures, not dropped.
- A test is skipped only when its behaviour is outside this plan, and the skip is written down
  with its reason.
- Such a task is done when the ported tests are green, and the step report has a table
  «тест RFA → наш тест | пропущен: причина».

All `test_spreadsheet_*` and `test_worker_*` files are skipped: spreadsheet chunking and the RFA
worker are outside this plan.

This is one step branch (`sym/NN-document-search`). F13 counts its range from `merge-base dev`, so
a second branch would need its own ADR. If the session budget runs out, the rest is recorded in
`PROGRESS.md` §4 and continued on the same branch.

## 1. Decision record and infrastructure

- [ ] 1.1 Write the ADR (next free number, status «предложено»). It records:
      - S1–S6 and the non-goals;
      - the `opensearch` service and the dependency `opensearch-py`;
      - the tool `aifa_search` and the refusal code `поиск_недоступен`;
      - `OUROBOROS_INITIAL_EXTENSION_SCHEMA_BUDGET=10000` and the F4 target of 9,000, with the
        1,000 margin kept (S6);
      - the F9 changes;
      - the `index_state` column.

      Change ADR-0022's status line to say that p. 5 is replaced by this ADR.

      In the same branch, update:
      - `ARCHITECTURE.md` §4.2/§4.3;
      - `CONTRACTS.md` §3: the `aifa_search` row, `поиск_недоступен` in §3.0, and the next free
        C3 number for its check.

      Verify: F13 green. Gate: owner «принято».
- [ ] 1.2 Ask the user to confirm `opensearch-py`, then
      `uv add --project aifa/finengine opensearch-py==<pinned>`. Verify:
      `uv sync --frozen --project aifa/finengine` succeeds, and `uv.lock` adds only that package
      and its dependencies.
- [ ] 1.3 Add the `opensearch` service to `aifa/deploy/dev/compose.yml` (an exact 2.x tag, volume
      `os-data`, 1 GB heap). Add the engine env, with no `depends_on`. Add the settings to
      `config.py`. Set `OUROBOROS_INITIAL_EXTENSION_SCHEMA_BUDGET=10000` on the `ouroboros` service
      and in `aifa/contracts/dev_config.snapshot.json`, with an F9 violator test. Verify:
      - the compose contract test and `test_dev_images.py` are green;
      - `docker compose -p aifa-sym -f aifa/deploy/dev/compose.yml config` parses;
      - a settings test shows the defaults with the variables unset.

## 2. Index, embeddings, chunks, card

- [ ] 2.1 `search/client.py` and `search/index_schema.py`, ported from RFA, with fields adapted
      (S1). Tests:
      - the mapping is strict, carries `workspace_id`/`document_id` as keyword, and puts the
        model and dimension in `_meta`;
      - `index_body(dimension=0)` raises.

      RFA tests: port `unit/test_index_strategy.py` (16) and the mapping and alias part of
      `test_opensearch_projection.py`.
- [ ] 2.2 `search/embeddings.py` with `gigachat` and `openrouter` (S2). Tests:
      - batching honours the batch size;
      - an unknown provider raises with the setting's name;
      - a dimension mismatch against index `_meta` → "нужна пересборка".

      Allowed fake: the provider HTTP boundary.

      RFA tests: port `unit/test_embed_providers.py` (9), `unit/test_embed_bridge.py` (3) and
      `unit/test_vector_store_dimension.py` (14). Skip `unit/test_openrouter_identity_headers.py`:
      it covers RFA's identity headers, and our provider headers are not changed by this plan.
- [ ] 2.3a `search/chunker.py`, a port of RFA `chunk_document`, covering:
      - the sentence window 800/100;
      - no crossing of a page, table or Markdown section;
      - `_without_repeated_prose`;
      - tables whole;
      - a financial page without tables whole (a port of `financial_pages.py` on page text).

      RFA tests:
      - port `test_chunker.py` (12) and `unit/test_financial_pages.py` (8);
      - rewrite a test that feeds OCR JSON on a `===PAGE N===` text fixture carrying the same
        content;
      - port the chunk-identity parts of `test_text_identity.py` and `test_citation_place.py`;
      - skip `test_spreadsheet_chunks.py` (not in this plan).

      Verify: the ported tests pass, plus new ones:
      - no chunk spans `===PAGE N===`;
      - no prose chunk spans a Markdown heading;
      - a paragraph repeated on pages 3 and 40 is emitted once;
      - no prose chunk exceeds 800 characters.
- [ ] 2.3b Markdown-table detection without OCR JSON (S1). Tests on fixtures cut from real
      corpus pages:
      - a 300-row table → one chunk starting with its header;
      - a table inside prose → prose chunks around one table chunk;
      - pipe characters in prose are not mistaken for a table.
- [ ] 2.4 `search/card.py`, plus a port of RFA `analytics/company_key.py`. Tests:
      - an empty registry `company_name` with meta card `issuer=«Сбербанк»` → card company
        «Сбербанк»;
      - the short description holds only type, company and period;
      - at most 20 headings, with no table captions;
      - equal `company_key` for «ПАО Сбербанк» and «Сбербанк».

      RFA tests: port `test_document_card.py` (7) and `test_company_key.py` (17). Rewrite a card
      test that reads RFA DB columns on `DocumentMeta` plus meta-card fixtures.

## 3. Indexing lifecycle

- [ ] 3.1 `search/indexing.py` with `index_document` and `delete_document`. Hook it after the
      last `add_document` of `_parse_doc_in_background` and after the registry removal in
      `delete_document`. The state goes to the new `document.index_state` column (start-up
      `ADD COLUMN IF NOT EXISTS`) through a single-column `set_index_state` (S3). Tests, with
      OpenSearch:
      - after indexing, a doc-type patch (`api/main.py:1390`) leaves `index_state == "indexed"`.
        Fails if the state lived in `meta`.
      - an existing `document` table without the column → the store starts and the column exists;
      - a re-index with fewer pages leaves only the new chunks;
      - search unreachable at upload → text ready and `index_state` starts with `failed:`;
      - delete with search unreachable → the row is gone and the route returns 200;
      - a document deleted between index start and state write stays absent from the registry.

      Producer/consumer: the real background task → the real indexer. Allowed fake: the
      embeddings provider.

      RFA tests:
      - port `test_search_indexing.py` (9) and the write/delete part of
        `test_opensearch_projection.py` (23 in total, split between 2.1 and 3.1);
      - rewrite the worker/lease-driven ones on the background task;
      - skip the parts about RFA's document state machine (`DELETING`, leases): not in this plan.
- [ ] 3.2 `search/reindex.py` with `--init`, `--rebuild` and `--reconcile`. Reconcile compares
      registry ready documents with the card ids in the index (S4), and rebuild ends with
      reconcile. Tests:
      - after an outage of 3 documents, reconcile indexes all 3, and a second run makes 0 writes;
      - an orphan card is removed;
      - a document indexed into the old index during a rebuild has a card in the new one;
      - a fresh empty index with every state `indexed` → all re-indexed.

      RFA tests: port `test_reindex_opensearch.py` (14).

## 4. Search

- [ ] 4.1 `search/search.py` with `select_documents(allowed_ids, query, workspace_id=None)` and
      `retrieve_chunks(allowed_ids, query, k, workspace_id=None)`: RRF k=60, RFA `pool_size`, and
      a registry recheck through `_user_can_see` (S4, S5). Tests, with OpenSearch:
      - a sparse card whose chunks match best is selected;
      - a hit outside `allowed_ids` is never returned;
      - an orphan chunk is never returned;
      - documents of two workspaces are both selectable with no filter, and only one with a
        filter;
      - live test (skipped without a key): «проблемы с ликвидностью» finds the «риск
        ликвидности» chunk.

      RFA tests:
      - port `test_hybrid_search.py` (12) and `unit/test_computed_chunk_search.py` (3);
      - from `test_analysis_retrieval.py` (11), port the hybrid-ranking cases;
      - skip its per-document top-up of crowded-out documents ("добор внутри вытесненных"): that
        retrieval policy is not in this plan. Revisit if answers over many documents keep missing
        one source.

## 5. Tool

- [ ] 5.1 `assistant/search_tool.py`, `ouro_view.search_hits` and `Refusal.SEARCH_DOWN`, wired as
      follows:
      - `ToolApi` tuple;
      - `tool_names.py`;
      - plugin `TOOLS`;
      - compose `OUROBOROS_ENABLED_TOOLS`;
      - `test_schema_budget.py` (`TARGET_CHARS = 9000`, the limit read from
        `dev_config.snapshot.json`, `assert total <= TARGET_CHARS < limit` kept, the core default
        still asserted 8,000, `EXPECTED_TOOLS`);
      - snapshots `tools.snapshot.json`, `tool_names.json` and `dev_config.snapshot.json`.

      Tests in `tests/loop_tools/test_search_tool.py`:
      - a foreign `doc_id` → `ОТКАЗ нет_документа`;
      - before `aifa_open` → the same refusal as `aifa_doc`;
      - `limit` outside 1–10 → `неверные_аргументы`;
      - search down → `ОТКАЗ поиск_недоступен` naming `aifa_doc`, with no "повтори";
      - it answers while a move holds the queue.

      Verify: `test_dev_config.py` and `test_schema_budget.py` are green, and the measured
      `budget_chars` is recorded in the ADR.
- [ ] 5.2 In `skills/aifa-planning/SKILL.md`, say when to use `aifa_search` (qualitative and
      cross-document passages) and when to use `aifa_doc` (exact pages). Run
      `scripts/resolve_rules.py` on the file first. Verify: new assertions in
      `skills/aifa-engine/tests/test_planning_text.py` fail before and pass after.

## 6. Verification and handover

- [ ] 6.1 Run `aifa/scripts/check.sh` and paste the result, comparing with the baseline.
- [ ] 6.2 Get an independent `/code-review` in a clean context, citing file:line. It checks
      `docs/INVARIANTS.md` for `skills/aifa-*`. Verify: no open blockers.
- [ ] 6.3 On aifa-sym, at the owner's signal: check free disk and memory first, then deploy, run
      `reindex --init` and `--reconcile`, and enable the tool. Verify:
      - cards in the index = ready documents in the registry;
      - RFA `test_benchmark/long_table_recall.py`, run against our index on the same corpus
        documents, with the recall recorded in `PROGRESS.md` §3 next to RFA's figure;
      - a live question across two reports gets an answer citing pages from `aifa_search`.
- [ ] 6.4 Add `PROGRESS.md` §1/§3/§4 entries, including the image tag and its digest (ADR-0035),
      and run `graphify update .`.
