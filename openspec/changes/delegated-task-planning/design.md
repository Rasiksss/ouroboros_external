# Design

`task_plan` operates on the current root, never a caller-supplied owner or root ID. Accept reads
the complete JSON result of a completed direct child in the same scope. Proposal revisions and
mutable execution states are separate. Updates require the last state version; PostgreSQL locks
the root row through commit. Payloads are sealed with the required credentials key and associated
data containing the root and complete owner scope, independently of optional chat encryption.

The root records observations and semantic verdicts. Deterministic checks cover schema, graph
references and consistency of recorded reviews, not financial or editorial correctness. The
planning skill does not execute work or report a truncated terminal note to AIFA.
