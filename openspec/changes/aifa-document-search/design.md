## Context

See `proposal.md` (Why). Observed state:

- **Ingestion is a FastAPI background task.**
  - `_parse_doc_in_background` (`api/main.py:482`) publishes the text with `save_document_text`
    (pages marked `===PAGE N===`).
  - It then detects the company and builds the meta card: `doc_kind` ∈ отчётность | пресс-релиз
    | презентация | транскрипт | таблица | прочее, plus `issuer`, `issuer_aliases` and `period`
    (`skills/meta_card.py`).
  - The v2 upload sends an empty company and period and no doc type (`use-doc-upload.ts:339-347`),
    so the registry fields are mostly empty, and the meta card is the real source.
  - There is no worker, lease or retry.
- **Deletion** is `delete_document` (`api/main.py:1394`): catalog/blob first, then the registry
  row.
- **Registry writes replace the whole `meta` JSON.**
  - `add_document` is an upsert of the whole JSON, and `update_document_meta` is an UPDATE of the
    whole JSON (`store/documents.py:115-121, 169-189`).
  - The ingestion task upserts its in-memory `meta` three times after the text is published
    (`api/main.py:580-606`).
  - The doc-type patch reads and rewrites the whole JSON (`api/main.py:1390`).

  So any new key kept in `meta` can be overwritten by a concurrent or later writer.
- **Access** is `_user_can_see` (owner or admin).
- **No embedding code** exists, although the `gigachat` and `openai` SDKs are dependencies.
- **Schema budget F4.**
  - `skills/aifa-engine/tests/test_schema_budget.py` has `TARGET_CHARS = 7000` and an exact
    `EXPECTED_TOOLS`.
  - It asserts `limit == 8000` (the default of `OUROBOROS_INITIAL_EXTENSION_SCHEMA_BUDGET`) and
    `total <= TARGET_CHARS < limit`.
  - The snapshot uses 6,127 (`aifa/contracts/tools.snapshot.json`).
  - Ouroboros skips any extension schema that would exceed the budget, with only a log warning
    (`ouroboros/agent/task_graph.py:179-187`). The budget is an env setting, not a frozen core
    constant.
- **Refusal codes.** `СБОЙ_СЛУЖБЫ` means "the service is unreachable, written by the plugin"
  (`CONTRACTS.md` §3.0). `ouro_view.service_fault` appends "повтори вызов".
- **The RFA reference** is `src/finengine/retrieval/`:
  - `index_schema.py`: strict mapping, `knn_vector` with HNSW/lucene/cosine;
  - `chunker.py` (`chunk_document`):
    - `chunk_size=800`, `overlap=100`, with a sentence-window policy for prose;
    - "tables are never split";
    - prose never crosses a page, a table envelope or a Markdown section;
    - repeated prose is kept once (`_without_repeated_prose`);
    - a financial-statement page without tables is one chunk (`is_financial_statement_page`);
    - tables are located through OCR JSON (`_locate_tables`), and spreadsheets have their own
      path (`_spreadsheet_chunks`);
  - `search.py`: RRF k=60 and `pool_size` growing with the number of documents;
  - `embeddings.py`, a bridge to `llm/embeddings.py` with providers `gigachat`/`openrouter`, where
    `local` was removed by rule T20;
  - `analytics/company_key.py`;
  - infra `opensearchproject/opensearch:2`, single node, security off, 1 GB heap.

## Goals / Non-Goals

**Goals:** a projection that can be rebuilt; selection and retrieval inside an allowed set;
nothing in the projection grants access; ingestion never depends on it.

**Non-Goals:**
- **No ingestion worker, leases or document state machine.** Indexing stays in the background
  task, and reconcile covers lost work. Revisit when indexing time or failures make uploads
  noticeably slower or lossy.
- **No neighbour-chunk expansion** (RFA §6.3). Revisit if answers lose definitions or units at
  chunk edges.
- **No local embedding model** (RFA rule T20).
- **No OpenSearch security plugin.** The service sits on the internal compose network only.
- **No OCR JSON.** Table detection runs on our text (S1); OCR coordinates are not stored.
- **No spreadsheet-specific chunking** (RFA `_spreadsheet_chunks`). A spreadsheet's recognised
  text is chunked by the same text rules. Revisit when spreadsheet answers lose sheet or cell
  context.

## Decisions

### S1. A port of RFA `retrieval/` into `finengine/search/`, adapted to text-only input

- **Module names stay RFA's:** `client.py`, `index_schema.py`, `card.py`, `chunker.py`,
  `embeddings.py`, `indexing.py`, `search.py`, `reindex.py` (CLI).
- **Chunk rules are RFA's** (user decision): the sentence window 800/100, no crossing of a page,
  table or Markdown section, repeated prose kept once, tables never split, and a financial page
  without tables kept whole.
- **The one adaptation is table and page detection.** RFA reads layout from OCR JSON. We detect:
  - Markdown tables (`|…|` rows with a `|---|` separator) in the page text from `doc_pages.pages_of`;
  - financial-statement pages with a port of `financial_pages.py`, run on the page text.

  The locator is `{page, section_path}`.
- **Card fields** come first from the meta card (`issuer`, `doc_kind`, `period`), then from
  `DocumentMeta`. `company_key` uses a port of RFA `analytics/company_key.py` over `issuer` and
  `issuer_aliases`.
- **RFA fields with no source here** (`processing_mode`, `reporting_year`) are left out of the
  mapping. `standard` is added later by the turn change from the loop passport (not here).

**Alternative: our own chunk rules** (merge to the embedding limit, split tables by rows). Rejected
by the user in favour of RFA's rules.

### S2. Embeddings through the existing provider SDKs, with one model per index

`search/embeddings.py`:
- `FINENGINE_EMBEDDING_PROVIDER=gigachat|openrouter`, plus a model and a batch size;
- calls are traced as the chat calls are.

The index `_meta` stores the provider, model and dimension. The writer compares them before every
bulk write and refuses on mismatch ("нужна пересборка").

### S3. Indexing in the existing background task; the state is its own column

- At the end of `_parse_doc_in_background`, after its last `add_document`,
  `indexing.index_document(doc_id)` runs in `try`.
- Its outcome (`indexed` | `failed:<reason>`) goes to a new column `document.index_state` through
  `SqlDocumentStore.set_index_state(doc_id, state)`, a single-column
  `UPDATE … WHERE doc_id = :id`.
  - It touches no row of a deleted document (zero rows affected).
  - The whole-`meta` writers never overwrite it, because they do not write that column.
  - Keeping the state inside `meta` would lose it to the doc-type patch or a re-parse.
- The column is added by the store's start-up DDL (`ADD COLUMN IF NOT EXISTS`, the mechanism from
  `aifa-workspaces` W3).

**Alternative: an indexing queue with leases**, as in RFA's worker. Recorded as a non-goal.

### S4. Consistency by recheck, and by reconcile against the index itself

- Every hit passes `get_document` + `_user_can_see` + `document_text_exists` + the allowed set.
- `delete_document` calls `indexing.delete_document` best-effort, after the registry row.
- Reconcile never trusts `index_state` alone. It lists the card ids in the index (a scroll over
  `record_kind=document`) and compares them with the registry's ready documents. That covers
  three cases: a document indexed into an old index during a rebuild, a lost volume, and a
  stale `indexed` state.

### S5. Search is keyed by allowed document ids; the workspace is an optional filter

- `select_documents(allowed_ids, query, workspace_id=None)` and
  `retrieve_chunks(allowed_ids, query, k, workspace_id=None)`.
- A `conversation` turn can hold documents of several folders (owner access,
  `aifa-workspaces`), so `aifa_search` passes no workspace.
- `aifa-multi-document-turn` passes the turn's workspace for `all_workspace` selection.
- The pool size follows RFA `pool_size(candidate_size, documents=len(allowed_ids))`.

### S6. `aifa_search` as a read-only tool, with its own outage code and the raised F4 target

- `assistant/search_tool.py` follows `DocTool`:
  - out of queue;
  - available under the same "after aifa_open" check as `aifa_doc`;
  - `doc_ids` ⊆ `record.doc_ids`, defaulting to all of them;
  - hits rendered by `ouro_view.search_hits` within the result limit.
- On outage it refuses with a new code `Refusal.SEARCH_DOWN = "поиск_недоступен"` and the text
  "поиск недоступен; страницы читает aifa_doc". `СБОЙ_СЛУЖБЫ` belongs to the plugin and tells the
  planner to retry, which is wrong here.
- The new code needs the ADR (closed list).
- F4: the margin of ADR-0022 p. 5 (1,000) is kept, because a schema that does not fit is dropped
  silently.
  - The ADR raises `OUROBOROS_INITIAL_EXTENSION_SCHEMA_BUDGET` to 10,000 in the aifa-sym
    environment (F9, ADR-0027 path) and `TARGET_CHARS` to 9,000.
  - ADR-0022's status line notes that p. 5 is replaced.
  - `test_schema_budget.py` reads the limit from `aifa/contracts/dev_config.snapshot.json`, not
    from the `AppSettings` default. It still asserts `total <= TARGET_CHARS < limit`, and still
    asserts the core default is 8,000, so a core change is noticed.
  - The ADR records the measured size after this change.
  - **Alternative: target 8,000 with the default budget.** This was rejected after checking
    `task_graph.py`: there is no margin, and one extra character drops a tool without an error.

### S7. Startup side-effects touched (rule R2.1)

| Side-effect | Change | Preserved by |
|---|---|---|
| Engine import and start (`api/main.py`) | none: no index work at start; the search client is created lazily | test: the app starts with `FINENGINE_OPENSEARCH_URL` unset, and upload/parse work |
| `config.py` settings load | new `FINENGINE_OPENSEARCH_URL`, `FINENGINE_EMBEDDING_*`, all optional with defaults | settings test: defaults with the variables unset |
| compose `aifa-sym` services | a new `opensearch` service and volume `os-data`; the engine env; no `depends_on` | `aifa/tests/architecture/test_dev_images.py` and the compose contract test |
| Ouroboros reads `OUROBOROS_ENABLED_TOOLS` and `OUROBOROS_INITIAL_EXTENSION_SCHEMA_BUDGET` | adds `aifa-engine.aifa_search`; budget 10,000 | F9: `dev_config.snapshot.json`, `test_dev_config.py` |
| Plugin `register(api)` | adds `aifa_search` | `test_schema_budget.py` (target 9,000 < limit from snapshot, exact set) |
| `SqlDocumentStore.__init__` start-up DDL | `ADD COLUMN IF NOT EXISTS index_state` | test: existing table without the column → the store starts and the column exists |
| `composition.tool_api()` cached registry | adds `SearchTool` | `tools.snapshot.json` test |
| Index creation | a deploy step (`reindex --init`) | the runbook in `aifa/deploy/dev/README.md` |

## Risks / Trade-offs

- **[Dev host resources]** 1 GB heap, and the disk was full recently. → Check `df -h` and free
  memory before deploy.
- **[Embedding cost and egress]** → Batching, and a re-index only on new text or a rebuild. The
  text has the same sensitivity as the chat LLM calls.
- **[Lost indexing on restart]** → Reconcile against the index (S4). An unindexed document is
  still listed and readable.
- **[Table detection without OCR layout]** A scanned table that OCR flattened into prose is not
  seen as a table, so it is chunked as prose at 800 characters. → Port RFA's chunker tests that
  apply to text, and add tests on real `===PAGE N===` fixtures from our corpus.
- **[Card quality]** The meta card can be empty. → Chunk-owner aggregation in selection keeps a
  weak card from hiding a document.

## Migration Plan

1. Deploy the compose change and the engine.
2. Run `reindex --init`, then `reindex --reconcile`.
3. Enable `aifa_search` in ouroboros.

Rollback: disable the tool in the env and stop the `opensearch` service. Ingestion is unaffected.
