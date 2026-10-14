---
paths:
  - "config_registry/agent_registry/**"
---

# The agent registry — read before editing

Which agents an owner has (`docs/ARCHITECTURE.md` §Data layout, `agents`).

| Concern | Owner |
|---|---|
| The record: `AgentEntity` | `config_registry/agent_registry/domain.py` |
| Boundary an owner's agents are asked through | `config_registry/agent_registry/interfaces.py` |
| Statements over the `agents` table | `config_registry/agent_registry/infra/repositories/repo.py` |
| HTTP router and its routes | `config_registry/agent_registry/api/http/endpoint.py` |
| Owner resolver and trace middleware | `infra/utils/http/fastapi.py` |
| Table and re-keyed `user_settings` | `infra/migrations/` |
| Tests | `tests/agent_registry/` |

**Rules:**

- This package imports no transport, no `supervisor`, no `multiuser_config` (test parses import graph). The owner header name is a constant in `ouroboros/contracts/owner_scope.py`.
- The owner resolver reads the mode itself: header when present; configured owner while `OUROBOROS_MULTIUSER` is off; 403 otherwise. No composition root hands a fallback.
- An agent has no kind. `SINGLE_AGENT_ID` (`"main"`) is the reserved id an absent header resolves to — a name, not a rank. Do not reintroduce a type column or a branch on the id.
- The default agent is an upsert, never a read then a conditional write.
- Absence is a return value. Deactivation refuses nothing — 404 for an id the owner holds no row for, 204 for every id it does. The 404 comes from the statement itself, not from a read placed in front of it.
- Every statement is keyed on `(owner_id, agent_id)`, written inside the method.
- The repository takes a composed engine, not the holder itself.
- Deactivation is a flag flip. The agent axis keys stored rows that outlive the agent.
