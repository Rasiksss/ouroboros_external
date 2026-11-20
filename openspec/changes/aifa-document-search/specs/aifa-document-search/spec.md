# Spec Delta

## Purpose

Defines the search projection of AIFA documents. It covers what is indexed and when, how the
projection stays consistent with the document registry, how documents and chunks are found
inside an allowed set, and the `aifa_search` tool.

## ADDED Requirements

### Requirement: An indexed document has one card and its chunks

For each indexed document, the projection SHALL hold exactly one `document` record and the
document's `chunk` records. Each SHALL carry `document_id` and `workspace_id`. Record ids SHALL be
deterministic. Re-indexing a document SHALL replace all of its previous chunks.

#### Scenario: Re-index replaces chunks

- **WHEN** document A is indexed, its text is re-published with fewer pages, and A is indexed again
- **THEN** the projection holds one card for A, and only chunks of the new text

### Requirement: Chunks follow RFA's rules and never split a table

Chunking SHALL follow RFA's rules:
- prose is cut by a sentence window of size 800 with overlap 100;
- no chunk crosses a page, a table or a Markdown section;
- prose repeating prose already emitted is kept once;
- a Markdown table is one chunk with its caption and is never split;
- a financial-statement page without tables is one chunk.

Each chunk SHALL carry its page number and section path.

#### Scenario: A large table stays whole

- **WHEN** a page holds a 300-row Markdown table
- **THEN** that table is exactly one chunk, which starts with its header row

#### Scenario: Prose window

- **WHEN** a page holds 2,000 characters of prose in one section
- **THEN** it becomes several prose chunks of at most 800 characters, and neighbouring chunks
  overlap

#### Scenario: Section boundary

- **WHEN** a Markdown heading starts a new section in the middle of a page
- **THEN** no prose chunk contains text from both sides of that heading

#### Scenario: Repeated prose

- **WHEN** the same paragraph appears on pages 3 and 40
- **THEN** it is emitted in one chunk only

### Requirement: The card describes the document and is never evidence

The card SHALL hold the title, company name, company key, document type, period, language, up to
20 top-level headings that are not table captions, and a short description built by code from
those fields. Company, type and period SHALL be taken from the document's meta card first, and
from its registry fields when the meta card lacks them. Card search SHALL return only document ids
and scores.

#### Scenario: Fields from the meta card

- **WHEN** the registry has an empty `company_name`, and the meta card says `issuer=«Сбербанк»`,
  `doc_kind=отчётность`, `period=2024`
- **THEN** the card's company is «Сбербанк», and its short description names the type, the company
  and 2024

### Requirement: Indexing follows ingestion and never fails it

After a document's text is published and classified, the service SHALL index it. A failed
indexing SHALL leave the document uploaded and usable, with the failure and its reason recorded
in the document's metadata. A successful one SHALL record the indexed state. Recording the state
SHALL NOT recreate a document deleted in the meantime. Later writes of the document's other
metadata SHALL NOT erase it.

#### Scenario: Search service down at upload

- **WHEN** a document is uploaded while the search service is unreachable
- **THEN** its text becomes ready as usual, and its metadata says indexing failed with the reason

#### Scenario: Deleted during indexing

- **WHEN** document A is deleted while its indexing runs
- **THEN** after indexing ends, A is still absent from the registry

#### Scenario: Metadata rewritten after indexing

- **WHEN** A is indexed and then its document type is changed by the user
- **THEN** A's recorded index state is still `indexed`

### Requirement: Deleting a document removes its records

Deleting a document SHALL remove its card and chunks from the projection. If the projection is
unreachable, the deletion of the document SHALL still complete, and the stale records SHALL never
be returned.

#### Scenario: Delete with search down

- **WHEN** document A is deleted while the search service is unreachable
- **THEN** A is gone from the registry, and later searches never return A

### Requirement: Search results are rechecked against the registry

A hit SHALL be returned only if its document still exists, the caller may read it (owner or
admin, as `_user_can_see`), its text is ready, and it is in the search's allowed set of document
ids. A record in the projection SHALL NOT by itself grant access.

#### Scenario: Orphan record

- **WHEN** the projection still holds chunks of a deleted document A
- **THEN** no search returns them

#### Scenario: Outside the allowed set

- **WHEN** a search is limited to documents [B, C] and A's chunks match best
- **THEN** no hit from A is returned

### Requirement: Document selection fuses three rankings inside an allowed set

Selecting documents SHALL run over an allowed set of document ids, optionally filtered to one
workspace. It SHALL fuse, by reciprocal rank fusion with k = 60:
- BM25 over card fields;
- kNN over card vectors;
- the documents owning the top chunk hits.

The candidate pool SHALL grow with the number of allowed documents, as in RFA. Results SHALL be
document ids with scores, highest first.

#### Scenario: A weak card does not hide a strong document

- **WHEN** document A's card is sparse, but its chunks match the query best
- **THEN** A appears among the selected documents

#### Scenario: Documents from two workspaces

- **WHEN** the allowed set holds documents of folders F1 and F2 and no workspace filter is given
- **THEN** documents of both folders can be selected

### Requirement: Chunk retrieval is hybrid inside the allowed documents

Retrieving chunks SHALL run BM25 and kNN over chunks of the allowed documents only, and fuse them
by reciprocal rank fusion with k = 60. Each hit SHALL carry `document_id`, `chunk_id`, page,
section path and content.

#### Scenario: Paraphrased question

- **WHEN** a chunk says «риск ликвидности» and the query is «проблемы с ликвидностью»
- **THEN** the chunk is among the hits

### Requirement: Embeddings come from one configured API provider per index

Vectors SHALL be computed by the configured API provider, GigaChat or OpenRouter. The index SHALL
record the model and dimension. Writing vectors from a different model or dimension into an
existing index SHALL be refused, with indexing marked failed and the reason "нужна пересборка".

#### Scenario: Model changed without rebuild

- **WHEN** the embedding model setting changes and a document is indexed into the old index
- **THEN** indexing is refused and marked failed with reason "нужна пересборка"

### Requirement: The projection can be rebuilt and reconciled against the registry

Reconcile SHALL compare the registry's ready documents with the cards present in the index:
- ready documents with no card → indexed;
- cards of documents that no longer exist → removed.

Rebuild SHALL build a new index, switch the alias, then run reconcile. Both SHALL be safe to
repeat.

#### Scenario: Reconcile after outage

- **WHEN** three documents failed indexing during an outage and reconcile runs
- **THEN** all three are indexed, and a second run changes nothing

#### Scenario: Indexed into the old index during a rebuild

- **WHEN** document A is indexed into the old index while a rebuild runs
- **THEN** after the rebuild completes, A has a card in the new index

#### Scenario: Volume lost

- **WHEN** the index volume is lost and reconcile runs on a fresh index
- **THEN** every ready document is indexed again, whatever its recorded state

### Requirement: aifa_search returns chunks from the turn's documents

`aifa_search` SHALL take `query`, optional `doc_ids` (a subset of the turn's documents) and an
optional `limit` from 1 to 10 (default 5). It SHALL return chunks with doc id, page, section and
text, within the result limit. It SHALL be read-only, answer without waiting for changing calls,
and be available after `aifa_open` exactly when `aifa_doc` is.

#### Scenario: Foreign doc_id

- **WHEN** the planner calls `aifa_search(query="риски", doc_ids=["Z"])` and Z is not a turn document
- **THEN** the answer is `ОТКАЗ нет_документа`

#### Scenario: Search down during a turn

- **WHEN** the search service is unreachable and the planner calls `aifa_search`
- **THEN** the answer is `ОТКАЗ поиск_недоступен`, saying that `aifa_doc` still reads pages, with
  no advice to repeat the call
