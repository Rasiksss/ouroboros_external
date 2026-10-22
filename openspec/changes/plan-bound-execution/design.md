# Design

An optional reserved `_plan` object is present in tool schemas: revision, step_id, attempt_id.
For a root with an accepted plan, executor calls require it. Dispatch validates scope, revision,
the named attempt and accepted dependencies, records the call before the provider runs, and starts that
specific attempt. Read/control calls and tasks without a plan keep their current behavior.
The reserved argument never reaches the provider. Calls retain tool_call_id, child task_id and
result digest; no semantic success is inferred from a returned tool string. The root still
submits observations and reviews. A running call or live bound child blocks step completion,
replanning and plan closure. Executing and terminal plan states cannot bypass these checks.

Cancellation uses the existing AgentEvents transport immediately while the run is alive, with
an explicit abstract ToolEvents capability and a null implementation outside a runtime. This
parallels persisted subtask dispatch and preserves fallback delivery on transport failure.

AIFA receives no plan validator. aifa_state gains a paged artifact index, type filtering and a
compact view of requested values. Tool results list final artifact identities before long graph
details. Root and author instructions use these instead of repeating completed work or loading
all provenance. File authoring and file creation are one step because registration executes code.

Dev budgets: service loop1200→2400, subtask600→1200, graph900→1800, paired hard timeouts2700→5400.
The loop budget becomes an environment setting; production defaults remain unchanged.

Lifecycle close reserves exclusion under the same root row lock and releases it only after
its provider actually returns. Cancellation of a wait leaves the call running: it cannot prove
that a worker body stopped. Recovery of uncertain bodies remains outside this change.
The AIFA adapter retains bound job calls through existing aifa_wait polling; HTTP errors or
unavailable jobs leave execution unconfirmed. Move/finish handler deadlines are 5400 seconds,
while each HTTP request retains its 300-second deadline. Internal receipts serialize under
the root lock; public plan writes continue to require optimistic expected_version.
