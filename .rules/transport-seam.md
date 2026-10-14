---
paths:
  - "transport/**"
---

# Transport seam and user-facing transports — read before editing

`transport/interfaces.py` is the boundary between transports and the supervisor (`docs/ARCHITECTURE.md` §Transport seam, §Ingress). Browser payload shapes are a separate owner — see `.rules/http-contracts.md`.

| Concern | Owner |
|---|---|
| The seam: `IInbox`, `IOutbox`/`IOutboxReader`, `IMessageGateway`, `ITaskStopNotifier`, `IEnvelopeBuilder` | `transport/interfaces.py` |
| Wire format: encode/decode through constructors, `ensure_wire_safe` at every backend | `transport/codec.py` |
| In-process backend | `transport/infra/inmemory.py` |
| Redis Streams backend (spans processes) | `transport/infra/redis_gateway.py` |
| Kafka backend (BROADCAST-only outbox) | `transport/infra/kafka_gateway.py` |
| Crash replay of unprocessed inbound rows | `transport/journal.py` |
| Dialogs SDK lifecycle and peer routing | `transport/dialogs/provider.py` |
| Bounded inbound ingress and DLP work | `transport/dialogs/inbound.py` |
| Text, presence, and media delivery | `transport/dialogs/outbound.py` |
| A2A ingress and JSON-RPC server | `transport/a2a/executor.py`, `transport/a2a/server.py`, `transport/a2a/task_store.py` |
| In-process skill bridge | `transport/skill_bridge.py` |
| Backend conformance contract | `tests/helpers/transport_conformance.py` |

**Rules:**

- A transport imports the seam and shared infrastructure, **never `supervisor`**.
- `InboundEnvelope.scope` is a required `OwnerScope` the transport mints from its own `TRANSPORT_NAME`. Outbound carries scope *and* delivery `source` as separate fields.
- Filtering is on the transport side, never in the queue. A non-matching message is `ack` + drop, never `nack`.
- Persist the user's message with `inbound_processed: False` before submitting; mark processed after. A failed submit leaves the row for the journal sweep.
- The crash-replay sweep reads through `IConversationRepository.list_pair_messages(owner_id=…, agent_id=…)`, never through a widened `ServiceScope`.
- The `inbound_processed` marker is the derived column `messages.inbound_unprocessed` — filter in the statement (`unprocessed_only=True`), never in Python after the read.
- Pairs are a page, not a ceiling: the sweep drains every page through the keyset cursor; the pair axis is unbounded.
- The row budget bounds the pass's work (attempts), not its submissions.
- The composition root constructs the dialogs transport before the supervisor thread starts and stops every transport it started when `run_supervisor` returns.
