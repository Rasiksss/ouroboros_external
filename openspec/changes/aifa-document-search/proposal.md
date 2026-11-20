## Why

Two things a multi-document analysis needs cannot be done by reading whole texts or by substring
search (`aifa_doc query`):
- picking the relevant documents from a workspace with dozens of reports;
- answering a qualitative question across several of them ("что пишут о рисках ликвидности").

RFA solves both with a rebuildable OpenSearch projection: document cards and chunks with
BM25 + kNN + RRF (`AIFA_RFA/docs/MULTIDOCUMENT_TECHNICAL_SPEC.md` §6, code in
`src/finengine/retrieval/`). This change ports it.

## What Changes

- **Infrastructure.** A new single-node OpenSearch 2.x service in the aifa-sym compose (security
  plugin off on the internal network, 1 GB heap, as in RFA infra). The engine reaches it only
  through `FINENGINE_OPENSEARCH_URL`. The engine starts and ingests without it.
- **Index.** One index behind an alias, with two record kinds:
  - `document` (card): title, company, company key, type, period, language, a code-built short
    description, up to 20 headings, and a document vector;
  - `chunk`: content, page/section locator, chunk type and a content vector.

  Every record carries `document_id` and `workspace_id`.
- **Chunks follow RFA's rules:**
  - prose goes by a sentence window of 800 characters with an overlap of 100;
  - a chunk never crosses a page, a table or a Markdown section;
  - repeated prose is kept once;
  - tables are never split;
  - a financial-statement page without tables is kept whole.

  Tables are detected from our text (Markdown table syntax), since we have no OCR JSON. RFA's
  separate spreadsheet chunking is not ported.
- **Embeddings.** An API provider, as RFA code does after rule T20 (no model weights in the
  image): GigaChat `/embeddings` by default, OpenRouter `/embeddings` as the alternative. The
  existing SDKs are used. The model and dimension are fixed in the index metadata.
- **Indexing** runs after a document's text is published and classified, in the same background
  task. A failure never fails the upload: the state goes into the document's metadata.
  - Deleting a document removes its records.
  - Rebuild and reconcile commands restore the projection from Postgres and S3, checking the
    registry against the index itself.
- **Search functions** run over an allowed set of document ids, with an optional workspace
  filter:
  - document selection (card BM25 + card kNN + owners of top chunks, fused by RRF k=60);
  - chunk retrieval (BM25 + kNN, RRF k=60).

  Every hit is rechecked against the document registry.
- **New tool `aifa_search(query, doc_ids?, limit?)`.** Read-only. It returns chunks with
  `doc_id`, page, section and text, only from the turn's documents. If search is down, it refuses
  with a new code `поиск_недоступен`.
- **Schema budget.** The current schemas use 6,127 of F4's target of 7,000. This change and
  `aifa-multi-document-turn` add tools and arguments that do not fit.
  - The ouroboros setting `OUROBOROS_INITIAL_EXTENSION_SCHEMA_BUDGET` (default 8,000) rises to
    10,000 in the aifa-sym environment (F9).
  - F4's target rises to 9,000, which keeps the 1,000 margin of ADR-0022 p. 5.
  - The margin matters: ouroboros silently drops an extension schema that does not fit
    (`ouroboros/agent/task_graph.py:185-187`).
- **New dependency** `opensearch-py` in `aifa/finengine`. The user chose OpenSearch on
  2026-11-19; the dependency itself is confirmed at apply time.

## Capabilities

### New Capabilities

- `aifa-document-search`: the search projection of AIFA documents. Covers what is indexed and
  when, how it stays consistent with Postgres, how documents and chunks are found inside an
  allowed set, and the `aifa_search` tool.

### Modified Capabilities

None.

## Impact

- Service: a new package `aifa/finengine/src/finengine/search/` and `config.py` settings.
  - Hooks go into `api/main.py` (`_parse_doc_in_background` and `delete_document`).
  - The tool needs `assistant/search_tool.py`, plus `ouro_view` (new refusal code), `tool_api`,
    `tool_names` and the plugin.
- Plugin `skills/aifa-engine/plugin.py` (new tool) and
  `skills/aifa-engine/tests/test_schema_budget.py` (target 9,000; the limit read from the deployed
  env snapshot; expected tools). Also the `aifa-planning` skill text.
- Deploy: `aifa/deploy/dev/compose.yml`, with:
  - the `opensearch` service and volume, and the engine env;
  - `OUROBOROS_ENABLED_TOOLS` +1 and `OUROBOROS_INITIAL_EXTENSION_SCHEMA_BUDGET=10000`, both under
    F9.

  Contract snapshots in `aifa/contracts/`.
- Storage: a new `index_state` column on `document`, written by a single-column UPDATE.
  Whole-`meta` writes (the ingestion upserts, the doc-type patch) would otherwise overwrite it.
- Dev host: about 1 GB more RAM and an index volume. The disk was full recently (`e518098`), so
  disk is checked before deploy.
- Data: document text leaves the stand to the embeddings provider. It is the same class of data
  already sent to the LLM provider.
- Records carry the `workspace_id` from `aifa-workspaces`, so that change goes first. Access never
  depends on that field. Used by `aifa-multi-document-turn`.
