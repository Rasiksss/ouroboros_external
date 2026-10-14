---
paths:
  - "ouroboros/llm/**"
---

# LLM provider and model routing — read before editing

| Concern | Owner |
|---|---|
| Client entrypoint used by all runtime LLM calls | `ouroboros/llm/client.py` |
| Model shapes | `ouroboros/llm/models.py` |
| Provider/model lookup | `ouroboros/llm/registry.py` |
| Per-provider adapters | `ouroboros/llm/providers/` |
| Model catalog per provider | `ouroboros/llm/provider_models.py` |
| Preview-version canary: share, degradation, naming rule (TN09) | `ouroboros/llm/preview_canary.py` |

**Rule:** runtime code calls `LLMClient`, never a provider SDK directly. Adding a provider means an
adapter under `ouroboros/llm/providers/` plus a `ouroboros/llm/provider_models.py` entry — not a branch in calling code.
