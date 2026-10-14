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
so that changing it takes effect on the next start rather than mid-flight.

An entry SHALL share the egress entry shape of an MCP server entry: an `id`, a `name`, `enabled`, a required
`url` whose scheme is `http` or `https`, and exactly one authorization in `auth` — of the kinds `none`,
`user_headers`, `user_token` and `mtls`, defaulting to `none` — declared and validated as
`mcp-user-authorization` states for a server, including its rules for forwarded names, whose protected
prefixes include `a2a-` and `x-a2a-`, and excluding its transport rules, since an A2A entry declares no
transport. An entry SHALL also carry a poll interval, a result cap, retrieval
aliases, and the skills to feature in the tool description, and SHALL refuse any other key: nothing but `auth`
authorizes, so a top-level `auth_header` or `auth_token` SHALL be refused as an unknown key. An entry of any
`auth` kind MAY use an `http://` URL, carrying credentials over it being the responsibility of whoever
declares the entry, and `mtls` SHALL mean that the URL addresses the deployment's egress gateway, so the connection adds no
credential and holds no client certificate. A validation error SHALL NOT render the input it rejects. Entries
that fail validation SHALL be dropped and named rather than failing the load, the drop SHALL render no value
the entry carries, and the agent list SHALL NOT be serialised into a subprocess environment.

The agents the manager connects SHALL be the connectable entries of the list — the enabled entries with a
non-empty id, the first entry of each id, each later duplicate logged — computed by one rule that the owner
settings listing of A2A agents reads too. With the flag on, the single-process server SHALL refuse to start,
before the A2A manager connects, when an enabled `user_headers` entry forwards a header or cookie that the
header collector declaration does not declare, naming `security_config/a2a.py` and each undeclared name.

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

#### Scenario: An entry without auth is unauthenticated

- **WHEN** an entry with only `id` and `url` is constructed
- **THEN** it is accepted and its `auth` kind is `none`

#### Scenario: The earlier flat credential fields are refused without their values

- **WHEN** the DEV `A2A_REMOTE_AGENTS` list holds an entry with a top-level `auth_header` and `auth_token`
- **THEN** the entry is dropped and the log line names its id and the unknown key
- **AND** the log line contains no part of the token

#### Scenario: User credentials over plaintext are accepted

- **WHEN** an entry declares `user_headers` or `user_token` with an `http://` URL
- **THEN** it is accepted with that authorization

#### Scenario: A gateway-fronted agent declares mtls

- **WHEN** an entry declares `auth` of kind `mtls`, or of kind `none`, with an `http://` URL
- **THEN** it is accepted
- **AND** an `mtls` entry's client and requests are built exactly as for a `none` entry at that URL

#### Scenario: A URL is required and addresses HTTP

- **WHEN** an entry has no `url`, or a `url` whose scheme is neither `http` nor `https`
- **THEN** construction fails

#### Scenario: An A2A protocol header cannot be forwarded

- **WHEN** an entry declares `user_headers` with header `A2A-Version` or `X-A2A-Extensions`, or `user_token`
  with `auth_header` `A2A-Extensions`
- **THEN** construction fails naming the protected header

#### Scenario: Only the first entry of an id connects

- **WHEN** the list holds a disabled entry `hr`, then two enabled entries with id `crm`
- **THEN** the manager connects only the first `crm` entry
- **AND** the duplicate is logged

#### Scenario: An undeclared forwarded name stops the server

- **WHEN** `A2A_EGRESS_ENABLED` is on and an enabled entry `hr` declares `user_headers` with header `X-Other`,
  which the header collector declaration does not declare
- **THEN** the server refuses to start before the A2A manager connects, naming `security_config/a2a.py` and
  `hr header:x-other`
- **AND** with `A2A_EGRESS_ENABLED` off, or the entry disabled, the server starts

### Requirement: One tool per remote agent, not one per skill

Each enabled remote agent SHALL be exposed as exactly one tool named `a2a__{agent_id}`. The A2A
protocol offers no way to address a published skill — a send request carries a message and a skill
declaration carries no input schema — so skills SHALL be described rather than dispatched. The
tool's parameters SHALL be `message` (the request text), an optional `wait` (default true), and an
optional `task_id` that continues an existing remote task and takes the task handle an earlier result
reported, which the parameter's description SHALL ask to be passed exactly as reported. The description SHALL
name the agent, summarise its published skills, and SHALL be truncated to 2000 characters.

The set SHALL be the deployment's connectable list for every owner alike: the provider narrows its
descriptors by no scope, and there is no per-owner selection of remote agents as
`OUROBOROS_USER_ENABLED_MCP_IDS` selects MCP servers — the owner settings surface adds no such key
(`user-settings-surface`). An owner holding no personal token for a `user_token` agent is therefore
refused when the tool is called, not left unaware that it exists.

#### Scenario: The offered agents do not depend on who is asking

- **WHEN** the catalog is read for two different owners
- **THEN** both are offered the same agent tools
- **AND** an owner with no personal token for a `user_token` agent is offered it too, and refused at
  the call

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

#### Scenario: The continuation parameter asks for the reported handle

- **WHEN** the schema of an agent tool is built
- **THEN** its `task_id` description says to pass the `task_id` exactly as an earlier result reported it

### Requirement: A delegated call does not have to hold the agent

A call with `wait` false SHALL submit the remote task and return immediately with its task handle and
status, leaving the agent free to continue. A call with `wait` true SHALL poll the remote task at
the configured interval until it reaches a terminal or interrupted state, or until
`A2A_TOOL_TIMEOUT_SEC` elapses. On timeout the client SHALL ask the remote agent to cancel the task, with the
credentials the call already resolved, and SHALL return an error that still carries the task handle, so the
result can be retrieved later.

The deadlines around one call SHALL be strictly ordered, innermost first: the operation's own
deadline (`A2A_TOOL_TIMEOUT_SEC`), which also bounds resolving the call's credentials, the cancel that follows
it, the cross-loop bridge's wait, and the ceiling the agent loop abandons the tool at. Only the innermost
deadline knows the task handle, so an outer wait that expired first would report a timeout carrying nothing
and leave the remote task running with no handle on it.

#### Scenario: A slow cancel still reports the task id

- **WHEN** a call overruns its deadline and the remote agent's cancel takes several seconds
- **THEN** the cancel is allowed to finish
- **AND** the returned timeout still carries the task handle

#### Scenario: A non-blocking call returns before the remote work finishes

- **WHEN** a tool call is made with `wait` false against an agent whose work takes minutes
- **THEN** the call returns within seconds
- **AND** the result carries the task handle of the remote task

#### Scenario: A blocking call that overruns cancels the remote task

- **WHEN** a call with `wait` true exceeds `A2A_TOOL_TIMEOUT_SEC`
- **THEN** a cancel request for the remote task id is sent to the remote agent
- **AND** the returned error carries the task handle

#### Scenario: Polling stops as soon as the remote task stops

- **WHEN** a polled remote task reaches a terminal state before the timeout
- **THEN** polling stops at that point
- **AND** the task's artifact text is returned

### Requirement: Task follow-up tools are global, not per agent

Retrieving and cancelling remote tasks SHALL be served by exactly two process-wide tools,
`a2a_task_status` and `a2a_task_cancel`, each taking the agent id and a task handle as `task_id`.
`a2a_task_status` SHALL accept an optional wait duration, so that a single tool serves both a one-shot status
read and a bounded wait. Configuring N agents SHALL therefore add N+2 tool names.

Each tool SHALL act for the run that calls it, not for the run that started the task: it SHALL open the handle
for the calling run's owner and agent before anything else, as "A remote task is followed only by the owner
and agent that started it" states, and its requests SHALL carry the calling run's credentials. Each tool's
`task_id` description SHALL ask for the value exactly as the result that reported it.

#### Scenario: Adding an agent adds exactly one name

- **WHEN** a third remote agent is configured alongside two existing ones
- **THEN** five A2A tool names are offered in total

#### Scenario: One tool serves both status and wait

- **WHEN** `a2a_task_status` is called with a wait duration of zero
- **THEN** the current status is returned without polling

#### Scenario: A follow-up authorizes as the caller

- **WHEN** a browser run of owner `o` and agent `a` whose context holds `x-sudir-token: t1` starts a task on a
  `user_headers` agent, and a later browser run of `o` and `a` whose context holds `x-sudir-token: t2` calls
  `a2a_task_status` with the reported handle
- **THEN** the task read carries `X-Sudir-Token: t2`

#### Scenario: The follow-up tools ask for the reported handle

- **WHEN** the schemas of `a2a_task_status` and `a2a_task_cancel` are read
- **THEN** each `task_id` description says to pass the value exactly as the result that reported it

### Requirement: An interrupted remote task is a result, not a failure

A remote task that stops in `input-required` SHALL end polling immediately and SHALL be returned as
an ordinary tool result carrying the remote agent's question and the task handle — not as an error,
and without cancelling the task. Answering SHALL be the same agent tool called again with that
handle, which the client opens and sends as a message against the existing remote task, carrying the answering
run's credentials. A task that stops in `auth-required` SHALL be returned as `⚠️ A2A_ERROR` naming the agent,
stating that the remote agent requires credentials (`auth-required`), and carrying the task handle and the
hint that "Remote failures surface as tool failures" addresses to the entry's authorization kind. It SHALL
NOT be cancelled or answered: the task stays alive, so that a call with corrected credentials can still reach
it by that handle. The result SHALL carry no credential value.

#### Scenario: A question from a remote agent reaches the model

- **WHEN** a remote agent pauses a task in `input-required` to ask for a missing detail
- **THEN** the tool result contains that question and the task handle
- **AND** the result is not classified as a tool failure
- **AND** the remote task is left alive

#### Scenario: Answering continues the existing remote task

- **WHEN** the agent tool is called again with the task handle of a task that is in `input-required`
- **THEN** the message is delivered against that existing remote task by its remote task id
- **AND** no new remote task is created

#### Scenario: Answering a task that has already ended is explained

- **WHEN** the agent tool is called with the task handle of a task that has reached a terminal state
- **THEN** the result explains that the task has ended
- **AND** no raw protocol error is surfaced

#### Scenario: Missing credentials are addressed to the operator

- **WHEN** a remote task of an agent whose `auth` kind is `none` or `mtls` stops in `auth-required`
- **THEN** the tool returns `⚠️ A2A_ERROR` naming the agent, `auth-required` and the task handle, with the
  hint that an operator must change `security_config/a2a.py`
- **AND** the task is not cancelled

#### Scenario: Missing user credentials are addressed by authorization kind

- **WHEN** a remote task of an agent whose `auth` kind is `user_headers` or `user_token` stops in
  `auth-required`
- **THEN** the tool returns `⚠️ A2A_ERROR` naming the agent, `auth-required`, the hint for that kind and the
  task handle
- **AND** the task is not cancelled

### Requirement: An unanswered send is unknown, not refused

A send that is written but never answered SHALL remain unknown when the call's own deadline expires
or the transport's read timeout fires. It SHALL NOT be reported as a message the remote agent did not accept: it
may have accepted it and be running the work. It SHALL be returned as a distinct result that
forbids a blind resend, carrying the task handle it was sent against when the message continued an existing
task and saying plainly that there is none when it did not. That result SHALL be classified as a tool
failure, SHALL be distinguishable from a timeout, and an identical repeat of the same call SHALL be
blocked rather than sent again. A deadline that expires before the call's credentials are resolved SHALL NOT be
reported this way: no send was written, and the call returns the error that "A call that needs user
credentials and has none sends nothing" states.

#### Scenario: A send that gets no answer does not claim refusal

- **WHEN** a call's deadline expires while the send is still unanswered
- **THEN** the result says the outcome of the submission is unknown
- **AND** it does not say the remote agent declined the message

#### Scenario: An unanswered continuation names the task it was sent against

- **WHEN** the unanswered send was a message against an existing remote task
- **THEN** the result carries that task handle and points at the status tool with it

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

One HTTP client per agent serves the runs of every owner, so for every authorization kind it SHALL hold no
credential in its own headers, credentials riding only on the requests of one operation, and SHALL retain no
cookie a response sets, so that nothing one request was answered with reaches another owner's request or the
connection's own traffic. A request hook SHALL remove every name the entry's authenticator forwards — `Cookie`
and a `user_token` entry's `auth_header` included — from any request whose scheme, host or port differs from
the configured URL's.

The agent card SHALL be narrowed at connect, for every authorization kind, to the interfaces whose scheme, host
and port equal those of the configured URL, a default port being equal to its omission, so that no request of
the connection addresses an origin the operator did not configure. A card left with no JSON-RPC interface SHALL
fail the connection. A card that could not be fetched SHALL be replaced by one addressing the configured URL.

#### Scenario: A redirect is not followed

- **WHEN** a remote agent answers a request with a redirect
- **THEN** no request is made to the target it names
- **AND** the redirect is surfaced as the response to be dealt with

#### Scenario: The shared client holds no credential

- **WHEN** a connection of each authorization kind has connected
- **THEN** its HTTP client's own headers hold no `auth_header`, no declared forwarded header and no `Cookie`

#### Scenario: A cookie a response sets is not kept

- **WHEN** a remote agent of any authorization kind answers owner A's send with `Set-Cookie: session=s1`
- **THEN** owner B's next request to that agent carries no `session` cookie
- **AND** no later request of owner A carries it either

#### Scenario: Off-origin interfaces are dropped from the card

- **WHEN** the configured URL is `https://agent.example/a2a` and the card lists JSON-RPC interfaces at
  `https://agent.example/rpc` and `https://other.example/rpc`
- **THEN** every request of the connection is sent to `https://agent.example`

#### Scenario: A default port is the same origin

- **WHEN** the configured URL is `https://agent.example/a2a` and the card's JSON-RPC interface is
  `https://agent.example:443/rpc`
- **THEN** the interface is kept and the agent connects

#### Scenario: The fallback card addresses the configured URL

- **WHEN** the agent card cannot be fetched
- **THEN** the agent's calls are sent to the configured URL

#### Scenario: Credentials are stripped from a request to another origin

- **WHEN** a request carrying a `user_token` entry's `auth_header` and a `Cookie`, or a `user_headers` entry's
  declared header, is sent through the agent's HTTP client to a URL whose scheme, host or port differs from the
  configured URL's
- **THEN** that request carries none of those headers

### Requirement: Remote failures surface as tool failures

Errors from a remote agent — unreachable host, protocol error, timeout, refused task — SHALL be
returned as formatted tool-result strings that the agent loop classifies as tool failures, in the
same way MCP tool errors are. A timeout SHALL be distinguishable from other failures in the
classified result. An interrupted task SHALL NOT be classified as a failure.

When a send, a poll, a status read or the cancel tool's cancel is answered with HTTP 401 or 403, found anywhere
in the cause chain of the error raised, the operation SHALL return `⚠️ A2A_ERROR` naming the agent with a hint
addressed by the entry's authorization kind, in place of the error's text: for `none` and `mtls`, that this
deployment sends the agent no credential, so an operator must change `security_config/a2a.py`; for
`user_headers`, that the remote agent rejected the credentials of the user's browser session and the user must
sign in again; for `user_token`, that the remote agent rejected the token sent for this owner and agent, and
the owner can store a new personal token for that A2A agent in owner settings or an operator must fix the
static token. The result SHALL carry no SDK or HTTP error text and no credential value.

When the cancel sent after the deadline is answered with HTTP 401 or 403, the call SHALL still return
`⚠️ A2A_TIMEOUT` with its `task_id`, and SHALL report that the remote agent did not authorize the cancel with
the same hint in place of the error's text, carrying no SDK or HTTP error text and no credential value.

#### Scenario: An unreachable agent is a tool failure, not a crash

- **WHEN** a configured agent cannot be reached during a call
- **THEN** the tool returns a formatted error
- **AND** the agent loop records the call as a tool failure

#### Scenario: A timeout is classified as a timeout

- **WHEN** a call exceeds its timeout
- **THEN** the classified outcome is a timeout rather than a generic error

#### Scenario: A rejected credential is addressed by authorization kind

- **WHEN** the send to an agent is answered with HTTP 401 or 403, for an entry of each authorization kind
- **THEN** the result is `⚠️ A2A_ERROR` naming the agent with that kind's hint
- **AND** it contains neither the SDK's error text nor any credential value
- **AND** the agent loop records the call as a tool failure

#### Scenario: A rejected follow-up is addressed the same way

- **WHEN** `a2a_task_status` or `a2a_task_cancel` for a `user_token` agent is answered with HTTP 403
- **THEN** the result names owner settings and the personal token for that A2A agent
- **AND** it contains no SDK error text

#### Scenario: A rejected cancel after the deadline stays a timeout

- **WHEN** a call with `wait=true` outlives its timeout and the cancel sent after the deadline is answered with
  HTTP 401 or 403, for an entry of each authorization kind
- **THEN** the result is `⚠️ A2A_TIMEOUT` naming the agent with the task's `task_id`
- **AND** it says the remote agent did not authorize the cancel, with that kind's hint
- **AND** it contains neither the SDK's error text nor any credential value

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

An agent card that, narrowed to the configured origin, offers no JSON-RPC interface SHALL be such a failure:
the connection SHALL be closed, no tool SHALL be offered for that agent, and the log line SHALL name the
configured origin and the origins the card offered as `scheme://host[:port]` only, with no path, query or
userinfo.

#### Scenario: One dead agent does not hide the live ones

- **WHEN** one of three configured agents is unreachable at startup
- **THEN** the other two are connected and their tools offered
- **AND** the server continues starting

#### Scenario: Disabled means invisible, not broken

- **WHEN** outbound A2A is disabled
- **THEN** the provider returns no descriptors
- **AND** no error is raised

#### Scenario: An agent whose card points elsewhere is skipped

- **WHEN** one of two configured agents has the URL `https://agent.example/a2a` and its card offers a JSON-RPC
  interface only at `https://user:pw@other.example:8443/rpc?k=v`
- **THEN** that agent is not connected and offers no tool
- **AND** the other agent is connected and the server continues starting
- **AND** the log line names `https://agent.example` and `https://other.example:8443` and contains no path,
  query or userinfo

### Requirement: Delegation behaviour is taught by an instruction pack

A skill SHALL carry the behavioural protocol for delegation, separately from the transport. It
SHALL state when to delegate rather than act, how to phrase a request, the required order for long
work (submit without waiting, record the `task_id`, continue, then retrieve), and a bound on how
many clarification rounds to accept before escalating to a human.

It SHALL state that a `task_id` is passed to the agent tool and to the follow-up tools exactly as the result
that reported it, never shortened, edited or composed from another value, and that a `task_id` refused as not
started by this owner and agent is not retried. For an `⚠️ A2A_ERROR` about credentials it SHALL state what to
tell the user by what the error says — that the owner can store a new personal token for that A2A agent in
owner settings, that the user must sign in again, or that an operator must fix the configuration — and SHALL
forbid repeating the call until that is done.

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

#### Scenario: The pack teaches handles and credential hints

- **WHEN** the pack's instructions are read
- **THEN** they say to pass `task_id` exactly as a result reported it
- **AND** for a credential error they say to tell the user about a personal token in owner settings, about
  signing in again, or about the operator, and not to repeat the call

### Requirement: Interrupted initialization releases every connection

The outbound A2A manager SHALL own every connection object created by an initialization batch until
the batch either commits or rolls back. If the batch is cancelled before it commits, it SHALL close
both connections still opening and connections that already opened, SHALL publish none of them, and
SHALL remain available for a later initialization attempt. Normal completion SHALL continue to
publish successful connections in configuration order.

#### Scenario: A completed peer is not leaked by a slower peer

- **WHEN** one connection completes and the initialization batch is cancelled while another remains pending
- **THEN** both connection clients are closed
- **AND** the manager exposes no tools from the interrupted batch

#### Scenario: Normal completion preserves configured order

- **WHEN** several peers complete discovery in an order different from their configuration order
- **THEN** the manager publishes them in configuration order

### Requirement: Every request of one delegated operation carries the calling run's credentials

Every request of an A2A operation SHALL carry the credentials of the run that made the operation, where an
operation is one call of an `a2a__{agent_id}` tool, of `a2a_task_status` or of `a2a_task_cancel`. The tool
provider SHALL pass the run's `OwnerScope` from the tool context to the manager, and the manager to the
connection, for every operation. Each connection SHALL build its authenticator once, when it is created, from
its entry and the egress authorization dependencies — the security context store and the user token store —
the manager was initialized with, with the A2A agent as the user token target.

The credentials SHALL be resolved once per operation, for the calling run's scope, on the manager's loop,
inside the operation's own deadline and before its first request. They SHALL ride as one per-request call
context on every request of that operation: the send, every poll that follows it, the cancel sent after the
deadline, the task read that explains a failed continuation, the status tool's read and its polls, and the
cancel tool's cancel. The cancel sent after the deadline SHALL NOT read a store again. No request SHALL carry
credentials through the headers of the shared HTTP client, and no credential SHALL be carried by ambient state
such as a context variable. The trace identifier header, the agent identifier header and the protocol version
header SHALL ride beside the credentials. Concurrent operations of runs of different owners on one connection
SHALL each carry only their own run's credentials.

#### Scenario: A send carries the calling owner's personal token

- **WHEN** a browser-originated run of owner `o` and agent `a` calls the tool of A2A agent `r`, which declares
  `user_token` with `auth_header` `X-API-Key` and a static token, and the store holds a personal token for
  `r`, `o` and `a`
- **THEN** the send request carries `X-API-Key` with the personal token as its only value

#### Scenario: Recorded browser credentials ride the send

- **WHEN** a browser-originated run whose security context holds `x-sudir-token: t1` and cookie `sudir=abc`
  calls the tool of an agent declaring `user_headers` with header `X-Sudir-Token` and cookie `sudir`
- **THEN** the send request carries `X-Sudir-Token: t1` and `Cookie: sudir=abc`

#### Scenario: Every poll carries the credentials of the send

- **WHEN** a call with `wait` true polls a running remote task several times
- **THEN** every task read carries the credentials the send carried
- **AND** the credentials were resolved once for the whole call

#### Scenario: The cancel after the deadline carries the caller's credentials and reads no store

- **WHEN** a call with `wait` true on a `user_token` agent overruns `A2A_TOOL_TIMEOUT_SEC`
- **THEN** the cancel request carries the personal token the send carried
- **AND** the user token store was read exactly once for the call

#### Scenario: A continuation probe carries the credentials of the send

- **WHEN** the send of a continuation fails and the client reads the task to explain the failure
- **THEN** that task read carries the credentials the send carried

#### Scenario: The follow-up tools carry the calling run's credentials

- **WHEN** a run calls `a2a_task_status` with a wait duration and then `a2a_task_cancel` for a task handle it
  holds
- **THEN** every task read and the cancel request carry the credentials resolved for that run

#### Scenario: Concurrent owners each carry their own credentials

- **WHEN** runs of two owners, one holding a personal token and one not, call the same `user_token` agent
  concurrently
- **THEN** one send carries the first owner's personal token and the other carries the static token

#### Scenario: Trace headers ride beside the credentials

- **WHEN** a send carries user credentials
- **THEN** it also carries the active trace identifier, the agent identifier and the protocol version header

### Requirement: A call that needs user credentials and has none sends nothing

An operation on an agent declaring `user_headers` or `user_token` SHALL send no request when its
authenticator refuses it — in the refusals `mcp-user-authorization` defines for that kind and those of
"Personal tokens and browser credentials are resolved only on the surfaces that own them": no recorded
context, a declared header or cookie with no non-empty unexpired value, a value a request header cannot hold,
a store that raises, or neither a personal nor a static token. The operation SHALL return `⚠️ A2A_ERROR`
naming the agent with the detail `cannot be authorized: <reason>`, SHALL name a missing header or cookie by its
name only, and SHALL be classified as a tool failure. No credential other than the one the kind's rules select
SHALL be substituted: a store that raises SHALL NOT be answered with the static token.

When the credentials are not resolved within the operation's deadline, the operation SHALL send nothing and
SHALL return `⚠️ A2A_ERROR` naming the agent and stating that the credentials were not resolved within the
timeout and nothing was sent. It SHALL NOT be reported as `⚠️ A2A_TIMEOUT` or `⚠️ A2A_SUBMISSION_UNKNOWN`: no
message was written and no task exists.

An agent declaring `none` or `mtls` SHALL cause neither store to be read and SHALL add no credential to any
request.

#### Scenario: No recorded browser credentials means no request

- **WHEN** a browser-originated run whose security token has nothing saved calls the tool of a `user_headers`
  agent
- **THEN** no request reaches the agent
- **AND** the result is `⚠️ A2A_ERROR` naming the agent, stating that the call cannot be authorized, and
  naming a declared header or cookie the credentials lack

#### Scenario: Neither a personal nor a static token means no request

- **WHEN** a dialogs run calls the tool of a `user_token` agent declaring no static token and the store holds
  no personal token for that agent and the run's owner and agent
- **THEN** no request reaches the agent
- **AND** the error states that no user token is stored for this owner and agent

#### Scenario: A store failure does not fall back to the static token

- **WHEN** the user token store raises while an operation resolves its token and the agent declares a static
  token
- **THEN** no request reaches the agent
- **AND** the error states that the user token store is unavailable

#### Scenario: A hanging store fails inside the call's deadline and sends nothing

- **WHEN** the user token store does not answer before `A2A_TOOL_TIMEOUT_SEC` elapses, including a store
  that goes on waiting after it is cancelled
- **THEN** the call returns, within its timeout, `⚠️ A2A_ERROR` stating that nothing was sent
- **AND** the result is neither `⚠️ A2A_TIMEOUT` nor `⚠️ A2A_SUBMISSION_UNKNOWN`
- **AND** no request reaches the agent

#### Scenario: An agent without user authorization reads no store

- **WHEN** a run with no security context calls the tool of an agent whose `auth` kind is `none` or `mtls`
- **THEN** the send is made
- **AND** neither store is read

#### Scenario: A refused authorization is a tool failure

- **WHEN** an operation is refused authorization
- **THEN** the agent loop records the call as a tool failure and the task continues

### Requirement: A remote task is followed only by the owner and agent that started it

Every `task_id` an A2A result reports SHALL be a task handle, `<remote task id>~<mac>`, where `mac` is the
first 32 lowercase hexadecimal characters of HMAC-SHA256 over the compact JSON array
`["a2a-task", <remote agent id>, <remote task id>, <owner id>, <agent id>]` under a key derived, with the domain
label `ouroboros:a2a-task-handle:v1`, from `OUROBOROS_CHAT_ENCRYPTION_KEY`. A handle SHALL be bound to the
owner, the agent and the remote agent, and SHALL NOT be bound to the chat or the surface: a task started in
one chat SHALL be followed from any chat or surface of the same owner and agent.

`a2a_task_status`, `a2a_task_cancel` and a call of an agent tool with `task_id` SHALL open the handle for the
calling run's owner and agent and the addressed remote agent first — before either store is read, before
credentials are resolved and before any request is sent. A value that does not open — a handle sealed for
another owner, agent or remote agent, a bare remote task id, or a handle whose `mac` was altered — SHALL be
refused with `⚠️ A2A_ERROR` naming the agent and the value as given, stating that it was not started by this
owner and agent with that agent, and saying to pass `task_id` exactly as the result that started the task
reported it. A request that names a task SHALL name it by its remote task id; the remote agent SHALL never
receive a handle.

The key SHALL be validated when the task handles are composed, so that a key that is not 32 bytes of base64
refuses the start rather than a call. A task started before handles existed SHALL NOT be followable, and a
changed key SHALL invalidate every handle sealed under the previous one.

#### Scenario: Every result naming a task names its handle

- **WHEN** results report a remote task as submitted, running, completed with no output, ended in a failed
  state, `input-required`, `auth-required`, timed out, cancelled, or unanswered on a continuation
- **THEN** each names the task by the remote task id followed by `~` and 32 lowercase hexadecimal characters
- **AND** a call answered with a message rather than a task reports no handle

#### Scenario: Another owner's handle is refused before any store read or request

- **WHEN** a run of owner `o2` and agent `a` passes a handle sealed for owner `o1` and agent `a` to
  `a2a_task_status`, to `a2a_task_cancel`, or as the `task_id` of the agent tool
- **THEN** the result is `⚠️ A2A_ERROR` naming the handle as given and stating that it was not started by this
  owner and agent with that agent
- **AND** neither store is read
- **AND** no request reaches the remote agent

#### Scenario: A handle of another agent or another remote agent is refused

- **WHEN** a run of owner `o` and agent `a2` passes a handle sealed for owner `o` and agent `a1`, or passes a
  handle sealed for remote agent `r1` with agent id `r2`
- **THEN** the call is refused and no request is sent

#### Scenario: A bare or forged task id is refused

- **WHEN** a run passes a remote task id without a `mac`, or its own handle with one character of the `mac`
  changed
- **THEN** the call is refused and no request is sent

#### Scenario: A handle opens from another chat and surface of the same owner and agent

- **WHEN** a browser run of owner `o` and agent `a` in chat `c1` starts a task, and a dialogs run of `o` and
  `a` in chat `c2` reads it with the reported handle
- **THEN** the task read is sent for the remote task id

#### Scenario: The remote agent never receives a handle

- **WHEN** a run continues, reads and cancels a task by its handle
- **THEN** the send, the task reads and the cancel name the remote task id
- **AND** no request body or header carries the handle's `mac`

#### Scenario: A changed key invalidates open handles

- **WHEN** a handle sealed under one key is opened by task handles composed with another key
- **THEN** it does not open

#### Scenario: An invalid key refuses the start

- **WHEN** task handles are composed with an `OUROBOROS_CHAT_ENCRYPTION_KEY` that is not 32 bytes of base64
- **THEN** composition fails naming `OUROBOROS_CHAT_ENCRYPTION_KEY`
- **AND** the failure renders no part of the key

### Requirement: Card discovery carries only the static credential

Fetching an agent card SHALL carry, as headers of that fetch alone, only the entry's static credential: the
`auth_header` with the static `auth_token` of a `user_token` entry that declares one. Discovery runs for no
run, so it SHALL read neither store and SHALL carry no personal token and no forwarded header or cookie. An
entry of any other kind, and a `user_token` entry without a static token, SHALL fetch its card with no
credential. The static credential SHALL NOT reach the card fetch through the shared client's headers.

#### Scenario: Discovery carries the static token

- **WHEN** a `user_token` entry declaring a static token connects
- **THEN** the card request carries `auth_header` with the static token
- **AND** neither store is read

#### Scenario: Otherwise discovery is anonymous

- **WHEN** a `user_token` entry without a static token, or an entry of kind `none`, `mtls` or `user_headers`,
  connects
- **THEN** the card request carries no `auth_header`, no declared header and no `Cookie`

### Requirement: Personal tokens and browser credentials are resolved only on the surfaces that own them

An A2A operation SHALL apply the surface gates of the shared egress authorization unchanged. For
`user_headers`, credentials SHALL be resolved only for a run on the browser surface, the one surface that
records a security context; an operation of a run on any other surface — dialogs, skill-bridge or the A2A
ingress surface — SHALL be refused naming the surface, without reading the security context store and without
sending a request. For `user_token`, a personal token SHALL be looked up only for a run on the browser,
dialogs or skill-bridge surface, under the agent's id as an A2A agent target and the run's owner and agent. A
run on the A2A ingress surface SHALL NOT read the user token store, SHALL send the static token when the entry
declares one, and SHALL otherwise be refused naming surface `a2a`. A personal token stored for an MCP server
SHALL NOT be sent to an A2A agent with the same id.

#### Scenario: A run from A2A ingress sends the static token and reads no store

- **WHEN** a run that arrived through A2A ingress calls the tool of a `user_token` agent declaring a static
  token
- **THEN** the send carries the static token
- **AND** the user token store is not read

#### Scenario: A run from A2A ingress without a static token is refused

- **WHEN** a run on the A2A ingress surface calls the tool of a `user_token` agent without a static token
- **THEN** no request reaches the agent and the error names surface `a2a`

#### Scenario: Browser credentials are not resolved for another surface

- **WHEN** a run on the dialogs, skill-bridge or A2A ingress surface calls the tool of a `user_headers` agent
- **THEN** no request reaches the agent and the error names that surface
- **AND** the security context store is not read

#### Scenario: Dialogs and skill-bridge runs look up their own personal token

- **WHEN** a run of owner `o` and agent `a` on the dialogs or the skill-bridge surface calls the tool of
  `user_token` agent `r`
- **THEN** the user token store is asked for the token of A2A agent `r`, owner `o` and agent `a`

#### Scenario: An MCP token with the same id is not sent

- **WHEN** the store holds a personal token for MCP server `hr`, owner `o` and agent `a` and none for A2A agent
  `hr`, and a browser run of `o` and `a` calls A2A agent `hr` declaring a static token
- **THEN** the send carries the static token

### Requirement: No credential value appears in a result, a log record or an error

Outbound A2A SHALL render no credential value — a static token, a personal token, or a forwarded header or
cookie value — in any tool result, log record, raised error or startup refusal. An authorization refusal SHALL
name a header or cookie by its name only; a store failure SHALL log only the error's type; the result of a
rejected credential SHALL carry no SDK or HTTP error text; the drop of an entry that fails validation SHALL
render no value it carries; and the entry's `repr` SHALL NOT contain its static token. A log line that names
an agent's address because of its card's origin SHALL render only `scheme://host[:port]`.

#### Scenario: No log record contains a credential

- **WHEN** runs calling `user_headers` and `user_token` agents send, poll, time out, are refused authorization,
  and are answered with HTTP 401, with every log record captured
- **THEN** no captured record's rendered message or exception text contains a static token, a personal token,
  or a forwarded header or cookie value

#### Scenario: An unforwardable personal token is not rendered

- **WHEN** the store returns a personal token containing CR LF and the agent declares a static token
- **THEN** no request reaches the agent
- **AND** the tool error contains no part of either token

#### Scenario: Neither the entry nor its drop renders the token

- **WHEN** an entry declaring a static `auth_token` is rendered with `repr`, and an entry of the DEV list
  carrying a top-level `auth_token` is dropped
- **THEN** neither the output nor the log line of the drop contains the token

