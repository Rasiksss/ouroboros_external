# a2a-egress Specification

## Purpose

Outbound A2A (agent-to-agent) delegation. Remote agents are configured, never discovered, and
reachable through per-agent tools and two process-wide task follow-up tools. The transport speaks
protocol version 1.0, treats an interrupted task as a result rather than a failure, and degrades
gracefully when an agent is unreachable. Delegation behaviour is taught by an instruction pack that
is offered exactly while the feature is enabled.

## Requirements

### Requirement: Remote agents are configured, never discovered

Outbound A2A SHALL be driven entirely by configuration: an `A2A_EGRESS_ENABLED` flag, a remote-agent
list, and an `A2A_TOOL_TIMEOUT_SEC` call timeout. The runtime SHALL source the remote-agent list from
`security_config/a2a.py` (`a2a_remote_agents`), imported once at process start; for the `DEV` polygon
the module MAY delegate to the legacy `A2A_REMOTE_AGENTS` settings key, which SHALL remain in
`AppSettings` unchanged and SHALL be read only inside `security_config/` and `schema.py`. The flag
SHALL default to off and SHALL be read once at startup and passed to the outbound manager, which
SHALL NOT read settings itself; changing the flag SHALL therefore take effect on the next start.
The call timeout SHALL be bound once when the tool source is built,
so that changing it takes effect on the next start rather than mid-flight. An entry SHALL carry at
least an id, a base URL, optional auth
header and token, a poll interval, a result cap, retrieval aliases, and the skills to feature in the
tool description. Entries that fail validation SHALL be dropped rather than failing the load, and
the agent list SHALL NOT be serialised into a subprocess environment.

#### Scenario: The flag is bound at startup, not read at dispatch time

- **WHEN** the process starts with `A2A_EGRESS_ENABLED` off
- **THEN** the outbound manager is never initialized and the provider offers no remote-agent tools
- **AND** turning the flag on afterwards takes effect on the next start

#### Scenario: The agent list comes from the security config module

- **WHEN** the process starts with `A2A_EGRESS_ENABLED` on and `security_config/a2a.py` exports
  `a2a_remote_agents` for the current polygon
- **THEN** the outbound manager is initialized with exactly those entries

#### Scenario: An empty agent list skips initialization

- **WHEN** `security_config/a2a.py` exports an empty list and `A2A_EGRESS_ENABLED` is on
- **THEN** the outbound manager is not initialized
- **AND** the process logs an informational message instead

#### Scenario: A malformed entry does not take the list down

- **WHEN** the configured list contains one entry that fails validation among valid ones
- **THEN** the valid entries are loaded
- **AND** the malformed entry is discarded

#### Scenario: Agent credentials never reach a subprocess environment

- **WHEN** a subprocess environment is built from settings
- **THEN** it contains no serialised remote-agent list

### Requirement: One tool per remote agent, not one per skill

Each enabled remote agent SHALL be exposed as exactly one tool named `a2a__{agent_id}`. The A2A
protocol offers no way to address a published skill — a send request carries a message and a skill
declaration carries no input schema — so skills SHALL be described rather than dispatched. The
tool's parameters SHALL be `message` (the request text), an optional `wait` (default true), and an
optional `task_id` that continues an existing remote task. The description SHALL name the agent,
summarise its published skills, and SHALL be truncated to 2000 characters.

#### Scenario: An agent publishing many skills yields one tool

- **WHEN** discovery finds a remote agent that publishes twenty-seven skills
- **THEN** exactly one tool named for that agent is offered
- **AND** the skills appear in that tool's description

#### Scenario: A long skill list cannot flood the model request

- **WHEN** an agent's name, description, and skill summaries exceed 2000 characters
- **THEN** the description placed in the model request is truncated to 2000 characters

#### Scenario: Two agents configured under one id do not both claim the name

- **WHEN** two configured entries resolve to the same tool name
- **THEN** the first one wins the name
- **AND** the collision is logged

### Requirement: A delegated call does not have to hold the agent

A call with `wait` false SHALL submit the remote task and return immediately with its `task_id` and
status, leaving the agent free to continue. A call with `wait` true SHALL poll the remote task at
the configured interval until it reaches a terminal or interrupted state, or until
`A2A_TOOL_TIMEOUT_SEC` elapses. On timeout the client SHALL ask the remote agent to cancel the task
and SHALL return an error that still carries the `task_id`, so the result can be retrieved later.

The deadlines around one call SHALL be strictly ordered, innermost first: the operation's own
deadline (`A2A_TOOL_TIMEOUT_SEC`), the cancel that follows it, the cross-loop bridge's wait, and the
ceiling the agent loop abandons the tool at. Only the innermost deadline knows the `task_id`, so an
outer wait that expired first would report a timeout carrying nothing and leave the remote task
running with no handle on it.

#### Scenario: A slow cancel still reports the task id

- **WHEN** a call overruns its deadline and the remote agent's cancel takes several seconds
- **THEN** the cancel is allowed to finish
- **AND** the returned timeout still carries the `task_id`

#### Scenario: A non-blocking call returns before the remote work finishes

- **WHEN** a tool call is made with `wait` false against an agent whose work takes minutes
- **THEN** the call returns within seconds
- **AND** the result carries the remote `task_id`

#### Scenario: A blocking call that overruns cancels the remote task

- **WHEN** a call with `wait` true exceeds `A2A_TOOL_TIMEOUT_SEC`
- **THEN** a cancel request is sent to the remote agent
- **AND** the returned error carries the `task_id`

#### Scenario: Polling stops as soon as the remote task stops

- **WHEN** a polled remote task reaches a terminal state before the timeout
- **THEN** polling stops at that point
- **AND** the task's artifact text is returned

### Requirement: Task follow-up tools are global, not per agent

Retrieving and cancelling remote tasks SHALL be served by exactly two process-wide tools,
`a2a_task_status` and `a2a_task_cancel`, each taking the agent id and the task id. `a2a_task_status`
SHALL accept an optional wait duration, so that a single tool serves both a one-shot status read and
a bounded wait. Configuring N agents SHALL therefore add N+2 tool names.

#### Scenario: Adding an agent adds exactly one name

- **WHEN** a third remote agent is configured alongside two existing ones
- **THEN** five A2A tool names are offered in total

#### Scenario: One tool serves both status and wait

- **WHEN** `a2a_task_status` is called with a wait duration of zero
- **THEN** the current status is returned without polling

### Requirement: An interrupted remote task is a result, not a failure

A remote task that stops in `input-required` SHALL end polling immediately and SHALL be returned as
an ordinary tool result carrying the remote agent's question and the `task_id` — not as an error,
and without cancelling the task. Answering SHALL be the same agent tool called again with that
`task_id`, which the client sends as a message against the existing remote task. A task that stops
in `auth-required` SHALL be returned as a tool error naming the agent whose credentials need
attention, and SHALL NOT be cancelled or answered.

#### Scenario: A question from a remote agent reaches the model

- **WHEN** a remote agent pauses a task in `input-required` to ask for a missing detail
- **THEN** the tool result contains that question and the `task_id`
- **AND** the result is not classified as a tool failure
- **AND** the remote task is left alive

#### Scenario: Answering continues the existing remote task

- **WHEN** the agent tool is called again with a `task_id` that is in `input-required`
- **THEN** the message is delivered against that existing remote task
- **AND** no new remote task is created

#### Scenario: Answering a task that has already ended is explained

- **WHEN** the agent tool is called with a `task_id` that has reached a terminal state
- **THEN** the result explains that the task has ended
- **AND** no raw protocol error is surfaced

#### Scenario: Missing credentials are addressed to the operator

- **WHEN** a remote task stops in `auth-required`
- **THEN** the tool returns an error naming the agent whose token needs attention
- **AND** the task is not cancelled

### Requirement: An unanswered send is unknown, not refused

A send that is written but never answered — the call's own deadline expiring on it, or the
transport's read timeout — SHALL NOT be reported as a message the remote agent did not accept: it
may have accepted it and be running the work. It SHALL be returned as a distinct result that
forbids a blind resend, carrying the `task_id` when the message continued an existing task and
saying plainly that there is none when it did not. That result SHALL be classified as a tool
failure, SHALL be distinguishable from a timeout, and an identical repeat of the same call SHALL be
blocked rather than sent again.

#### Scenario: A send that gets no answer does not claim refusal

- **WHEN** a call's deadline expires while the send is still unanswered
- **THEN** the result says the outcome of the submission is unknown
- **AND** it does not say the remote agent declined the message

#### Scenario: An unanswered continuation names the task it was sent against

- **WHEN** the unanswered send was a message against an existing remote task
- **THEN** the result carries that `task_id` and points at the status tool

#### Scenario: The same request is not sent twice on its own

- **WHEN** the model repeats an identical call after an unknown submission
- **THEN** the repeat is blocked rather than delivered

#### Scenario: A connection that never opened is an ordinary failure

- **WHEN** the send fails because the connection could not be established
- **THEN** the result is an ordinary remote failure, because nothing was submitted

### Requirement: A delegated call is not a parallel-safe read

An `a2a__*` tool SHALL NOT be marked parallel-safe. What the remote agent does with the message is
outside this process and may be mutative, and one batch can carry two calls to the same agent or two
continuations of one task. Only reading a remote task's state SHALL be parallel-safe.

#### Scenario: Delegation is not admitted to a read-only batch

- **WHEN** the tool metadata for a remote agent tool is resolved
- **THEN** it is not parallel-safe
- **AND** the task status tool is

### Requirement: The configured credential goes only where it was configured to go

The per-agent HTTP client SHALL NOT follow redirects. An entry may name any credential header, and
the HTTP client strips only `Authorization` when a redirect changes origin, so any other configured
header — and the request body with it — would otherwise reach the redirect target. A remote agent's
URL is configured rather than discovered, so a redirect SHALL surface as a failure against that
configuration rather than being followed.

#### Scenario: A redirect is not followed

- **WHEN** a remote agent answers a request with a redirect
- **THEN** no request is made to the target it names
- **AND** the redirect is surfaced as the response to be dealt with

### Requirement: Remote failures surface as tool failures

Errors from a remote agent — unreachable host, protocol error, timeout, refused task — SHALL be
returned as formatted tool-result strings that the agent loop classifies as tool failures, in the
same way MCP tool errors are. A timeout SHALL be distinguishable from other failures in the
classified result. An interrupted task SHALL NOT be classified as a failure.

#### Scenario: An unreachable agent is a tool failure, not a crash

- **WHEN** a configured agent cannot be reached during a call
- **THEN** the tool returns a formatted error
- **AND** the agent loop records the call as a tool failure

#### Scenario: A timeout is classified as a timeout

- **WHEN** a call exceeds its timeout
- **THEN** the classified outcome is a timeout rather than a generic error

### Requirement: The client speaks protocol version 1.0

The client SHALL use the 1.0 protocol methods for sending, retrieving, and cancelling tasks, and
SHALL send the protocol version header on every request, because an absent header means version 0.3
to a compliant server. The client SHALL accept both response shapes: a task, and a direct message
returned without a task.

#### Scenario: A request is not silently downgraded

- **WHEN** the client issues any request to a remote agent
- **THEN** the protocol version header is present

#### Scenario: An agent that answers without creating a task is handled

- **WHEN** a remote agent replies with a message rather than a task
- **THEN** the reply text is returned as the tool result
- **AND** no task id is invented

### Requirement: A failing agent degrades, it does not break startup

Connecting to remote agents SHALL be attempted per agent, and a failure SHALL be logged and skipped
rather than aborting initialisation of the others or of the server. When the feature is disabled or
no agent is configured, the provider SHALL offer no tools and SHALL NOT error.

#### Scenario: One dead agent does not hide the live ones

- **WHEN** one of three configured agents is unreachable at startup
- **THEN** the other two are connected and their tools offered
- **AND** the server continues starting

#### Scenario: Disabled means invisible, not broken

- **WHEN** outbound A2A is disabled
- **THEN** the provider returns no descriptors
- **AND** no error is raised

### Requirement: Delegation behaviour is taught by an instruction pack

A skill SHALL carry the behavioural protocol for delegation, separately from the transport. It
SHALL state when to delegate rather than act, how to phrase a request, the required order for long
work (submit without waiting, record the `task_id`, continue, then retrieve), a bound on how many
clarification rounds to accept before escalating to a human, and how it orders against the existing
assignment-delegation skill, which routes work to people rather than agents.

The pack SHALL be offered exactly while outbound A2A is enabled, and SHALL be located relative to
the code that ships it rather than to the configured skills directory, so that a deployment whose
repository path points elsewhere still has it. Only its manifest metadata SHALL reach the prompt
surface; the instructions themselves SHALL be read on demand.

#### Scenario: The pack follows the feature, not the path variable

- **WHEN** outbound A2A is enabled and the configured skills directory does not contain the pack
- **THEN** the pack is still discovered

#### Scenario: Disabling outbound A2A withdraws the pack

- **WHEN** outbound A2A is disabled
- **THEN** the pack is absent from the skill catalogue
- **AND** it is absent even if the configured skills directory does contain it

#### Scenario: The instructions are not injected into every prompt

- **WHEN** the pack is offered
- **THEN** only its name and description reach the prompt surface
- **AND** its instructions are loaded only when the agent reads the skill

#### Scenario: Agent delegation and human assignment are ordered explicitly

- **WHEN** a request could be delegated to a remote agent or sent to a person as an assignment
- **THEN** the instructions state which takes precedence
