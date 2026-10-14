# skill-activation Specification

## Purpose
How active skill instructions are injected into the prompt and what happens when they overflow the budget.
## Requirements
### Requirement: Accepted manifests retain exactly the read ranges
The system SHALL retain only accepted successful pages of a canonical `SKILL.md` eligible skill within the current invocation. References, refusals, errors, and late results after timeout/cancellation SHALL NOT be activated. Discovery, eligibility, confinement, the schema, and read limits are preserved. Merging of line/character pages SHALL preserve the original body, whitespace, and line breaks in document order without repeats; gaps are marked, not automatically read through. A conflicting version SHALL be rejected without changing the accepted version, ranges, or recency.
#### Scenario: Partial and abandoned reads
- **WHEN** pages are read out of order, repeated, overlap, or require `start_char`, then a reference, refusal, new version, or late result arrives
- **THEN** only the exact union of accepted ranges of the original version is retained; completeness means covering the entire document.

### Requirement: Foreground requests preserve selected instructions verbatim
Until `RunConfig.emergency_threshold` is reached, all activations SHALL be present verbatim in every request. Normal rounds, retry, fallback, round-limit finalization, language repair, and LLM-compaction SHALL preserve selected instructions in a single protected block. The summarizer SHALL receive them separately from the history for retelling; summary and local compaction SHALL NOT change their text. A tool message SHALL contain a receipt with `tool_call_id`, logical identity, range, and continuation cursors; low-level reading preserves the body. Language repair SHALL consider selected instructions as language evidence and SHALL use the route of the last successful response.
#### Scenario: Compaction and repaired fallback answer
- **WHEN** after reading the history is compressed, primary fails and fallback answers, or primary later recovers
- **THEN** selected instructions are preserved verbatim, and repair uses the model of the corresponding successful response.

### Requirement: Emergency pressure reduces only the prompt projection
The system SHALL use the existing `RunConfig.emergency_threshold` (75% of the window in token mode), first performing microcompaction and LLM-compaction of the history, then repeating the approximate estimate. If the context is still above the target, the system SHALL select whole skills in descending order of recency. A candidate that does not fit SHALL NOT prevent consideration of the next smaller skills. The full registry of read ranges SHALL remain in `ToolContext`; excluded names SHALL be listed deterministically in the protected block with an instruction to re-read the needed skill via `skill_read`. Exclusion SHALL NOT truncate the section or end the task by itself.
#### Scenario: History first and then old skills
- **WHEN** history and active instructions reach the emergency threshold
- **THEN** history is compressed first; if that is sufficient, all instructions remain, otherwise older skills are temporarily absent from the prompt, retaining their ranges in memory.
#### Scenario: Restore an excluded skill
- **WHEN** an excluded skill is successfully re-read
- **THEN** its recency is updated and it returns to the priority projection, displacing older skills wholesale if necessary.

### Requirement: Recency and rendering order are independent
Recency SHALL be determined by the last accepted successful read. The rendering of selected skills SHALL follow the order of first activation, stored separately from recency. A re-read with sufficient budget SHALL leave the system prompt completely unchanged and SHALL NOT add a copy. Two assemblies without new events SHALL be identical.
#### Scenario: Reread changes selection but not the cache prefix
- **WHEN** A, B, C are read, then A
- **THEN** the selection priority is A, C, B; the full prompt remains A, B, C, and a projection of two fitting skills yields A, C.

### Requirement: Actual context exhaustion is bounded and explicit
The loop's approximate estimate SHALL serve compaction only; message/tool transformations and actual overflow SHALL belong to `LLMClient`/provider. `call_llm_with_retry` SHALL return `LLMFailure` without the usual retries for an overflowed request. TPM rate limits / HTTP 429 SHALL retain normal retry with backoff and SHALL NOT trigger skill eviction. The loop SHALL perform bounded compaction and one retry, including overflow first encountered on fallback and round-limit finalization. After actual overflow, compaction SHALL reduce `keep_recent` to one tool-round with the history protections in effect, allowing even a short voluminous history to be compressed; the retry SHALL exclude optional skills, preserving the most recent not-yet-shown successful read. The emergency threshold SHALL be a target, not a basis to hide that read until the next successful LLM request. An unavailable summarizer SHALL use the existing local fallback. If its failure was context overflow and subsequent eviction changed the projection, the compactor SHALL retry summarization once with the updated protected instructions before retrying the foreground request. If the projection is unchanged or the error is of a different type, no additional call SHALL be made; a repeated error SHALL retain the local fallback.

`SKILL_CONTEXT_OVERFLOW` SHALL be admitted only if after history compaction and exclusion of older skills the provider still rejects the mandatory base request with the most recent not-yet-shown read. Success SHALL clear the failure; an error of a different type SHALL replace the previous reason. `LLMProvider.call` SHALL return `LLMMessage | LLMFailure` directly, and `RunState` SHALL atomically replace the response/failure of each call. The provider SHALL NOT require a mutable `last_failure`; middleware MAY receive `None` on error.
#### Scenario: Recovery fits or reports the latest failure
- **WHEN** primary returned overflow, and after compaction the retry or fallback returned a network failure or success
- **THEN** the result carries the current network failure without the old overflow, or a response without failure.
#### Scenario: Old instructions initially prevent history summarization
- **WHEN** the primary request and the summarizer both receive overflow, then older skills are excluded and the history with the latest read becomes available for summarization
- **THEN** the existing summarization is retried once with the new projection, the primary retry receives the compressed history and the latest read verbatim, and the task continues without a false `SKILL_CONTEXT_OVERFLOW`.
#### Scenario: Latest skill exceeds the emergency target but fits the provider
- **WHEN** the latest read does not fit the emergency target, but the mandatory request with it is physically feasible
- **THEN** older skills are excluded first, the latest read is sent in full, and the task continues.

### Requirement: Activations belong to one invocation

State SHALL be isolated between simultaneous and sequential runs sharing an agent and catalog, including repeated task ID and scope, and SHALL be released on success, error, or cancellation. Persistence, recovery on a different pod/after restart, and cross-task inheritance SHALL NOT be added; the `skill_activations` table is not used.

In multi-user mode, a run SHALL resolve the skills permitted by the owner and agent once before prompt assembly and SHALL use an immutable task surface until the invocation completes. Changing or removing `OUROBOROS_USER_ENABLED_SKILL_IDS` SHALL NOT remove already accepted pages and SHALL NOT change skill availability for this invocation; the next run SHALL resolve the new value. This rule SHALL NOT be interpreted as persisting activations between runs.

#### Scenario: Overlapping and repeated runs

- **WHEN** one run activates a skill and completes in any manner
- **THEN** the adjacent and subsequent runs start with an empty registry and without its instructions

#### Scenario: Selection changes after a page was accepted

- **WHEN** a run has successfully read a page of an available skill and the owner then changes or removes
  the allowlist setting
- **THEN** the current run retains the accepted page and the prior task surface until completion
- **AND** the next run uses the new setting value and does not inherit the accepted page
