---
paths:
  - "config_registry/chat_registry/**"
  - "tests/chat_registry/**"
---

# The chat registry — read before editing

Which conversations an owner has for one agent (`docs/ARCHITECTURE.md` §Data layout, `chats`).

| Concern | Owner |
|---|---|
| The record: `ChatEntity`, `TitleSource` | `config_registry/chat_registry/domain.py` |
| Sync and async faces: sync store (transport/dispatch) and async repo (HTTP surface/memory tier) | `config_registry/chat_registry/interfaces.py` |
| Statements over the `chats` table | `config_registry/chat_registry/infra/repositories/repo.py` |
| HTTP router and its routes | `config_registry/chat_registry/api/http/endpoint.py` |
| Table, CHECK constraints, backfill | `infra/migrations/` |
| Provisional title and retired-chat refusal in the inbound transaction | `ouroboros/storage/chat_store.py` |
| Titling step and composite dispatch value | `background_memory/handlers/titles.py` |
| Owner resolver and trace middleware | `infra/utils/http/fastapi.py` |
| Tests | `tests/chat_registry/`, `tests/background_memory/test_title_handler.py` |

**Rules:**

- Sync and async faces over one table because callers differ in call style (sync thread vs asyncio). Neither face takes a surface. The titling pair (`claim_title_attempt`, `store_generated_title`) is on both faces — the memory tier became a loop, the transport did not.
- Creation is an upsert plus a read in one transaction, never a read then a conditional write (several instances race).
- The claim and CAS are statements, not handler logic. Row count of `UPDATE … title_attempts + 1 WHERE title_source = 'provisional'` is the permission to call the model, taken before the call.
- `chat_title` is the one normalizer for all writers (transport, model, owner rename). First line, control chars to space, whitespace collapsed, cut to `TITLE_MAX_LENGTH`.
- The titling step's wall-clock budget (`_TITLE_BUDGET_SEC`) is small — it runs ahead of consolidation inside one delivery, which is a task of its own and holds only the run it precedes.
- The listing is unbounded and decrypts on the loop. Do not "fix" with a bare `LIMIT` — a silent cap reads as the whole list.
- The title is encrypted at rest; no statement may sort, filter or search on `title`.
- No foreign key to `agents` — the registry row is written by the transport while the `agents` row is created on the HTTP path.
- Browser only, including the backfill. Dialogs, A2A and the skill bridge write no record.
- This package imports no transport, no `supervisor`, no `multiuser_config`, names no model client (`tests/chat_registry/test_import_boundary.py`).
- Every statement is keyed on `(owner_id, agent_id)` — and, except a listing, the chat — inside the method.
- Absence is a return value; a refusal is not an absence. `None` means the table holds nothing; the router turns it into 404.
- Retirement is a flag flip, never a delete — the chat axis keys rows that outlive the conversation.
