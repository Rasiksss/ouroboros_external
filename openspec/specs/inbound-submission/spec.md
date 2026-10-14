# inbound-submission Specification

## Purpose

How a user message travels from a transport to the supervisor: local persistence and dedup by
`client_message_id`, at-least-once submission with retry, and a single generic supervisor-side path
that adopts the owner, logs to chat history and submits to the orchestrator. Slash commands are
decided by the supervisor, never by a transport; a transport answers only its own refusals,
synchronously.
## Requirements
### Requirement: External transports reject oversized messages at ingress

Dialogs, Browser, A2A and Skill Bridge SHALL enforce `OUROBOROS_MAX_INBOUND_MESSAGE_CHARS`
(positive integer, default 50000) before persistence, attachment processing or queue submission.
The limit includes slash-command arguments and attachment captions. A rejected message SHALL
receive a channel-native refusal and SHALL NOT be persisted. Unaddressed Dialogs messages and
externally imported history SHALL be dropped silently when oversized. Already persisted history,
replay and internal supervisor messages SHALL NOT be rechecked.

#### Scenario: The message exceeds the configured limit

- **WHEN** incoming text contains more characters than the configured limit
- **THEN** the transport rejects it without further processing

#### Scenario: The message is exactly at the limit

- **WHEN** incoming text contains exactly the configured number of characters
- **THEN** it continues through the ordinary inbound path

### Requirement: Every inbound message reaches the supervisor through one path

A transport SHALL deliver every user message — chat text, slash command, message with attachments —
by calling `inbox.submit`. No transport SHALL call the orchestrator, the
task queue, or a runtime dispatch function directly. The supervisor loop SHALL be the only consumer
of the inbox.

#### Scenario: A slash command travels as an ordinary message

- **WHEN** a user types `/status` in any transport
- **THEN** the transport submits it as a message and does not itself recognize it as a command

#### Scenario: No transport reaches the orchestrator

- **WHEN** the transports are inspected for calls to `orchestrator.submit` or a runtime inbound
  dispatch
- **THEN** none is found

### Requirement: A transport persists an inbound message before submitting it

A transport SHALL persist the message to chat storage marked unprocessed, SHALL then submit it, and
SHALL mark it processed only after the submission returns. A message persisted but not marked
processed SHALL be resubmitted later. A transport SHALL NOT delete a persisted message to compensate
for a failed submission.

The resubmission SHALL NOT be limited to the owner an instance is configured for. A stranded row
belongs to whoever the transport minted its scope for when the row was written, which in a
deployment serving many owners is nobody's configured constant, so a sweep that reads under the
instance's own owner recovers nothing for anyone. Any instance of a transport SHALL be able to
replay any stranded row of that transport, regardless of which instance persisted it.

#### Scenario: A failed submission leaves a retryable record

- **WHEN** submission raises after the message was persisted
- **THEN** the record remains, unprocessed, and no deletion is attempted

#### Scenario: A retry submits the message and completes it

- **WHEN** an unprocessed record is swept after an earlier submission failure
- **THEN** it is submitted again and marked processed on success

#### Scenario: A processed record is not submitted twice

- **WHEN** a message that is already marked processed is offered again
- **THEN** it is not resubmitted

#### Scenario: A stranded row of a non-configured owner is replayed

- **WHEN** a row persisted under an owner other than the instance's configured owner is left
  unprocessed, and that instance sweeps
- **THEN** the row is submitted and marked processed
- **AND** the envelope carries the owner and agent stored on the row, not the instance's configured
  ones

### Requirement: Every submitted message carries a client message id

A transport SHALL set `client_message_id` on every envelope it submits, and the value SHALL be stable
across retries of the same user message. A transport that has no natural client-supplied id SHALL
derive one from its own durable identifier for the request.

A `client_message_id` is unique within the owner that sent it and SHALL NOT be treated as unique
across owners. It is chosen by the client, and the other values a stored identity folds in are
owner-relative too — a chat id is unique within an owner and not across owners. Any identity derived
from a client message id SHALL therefore also carry the ownership the row is recorded under, so that
two owners submitting the same id are two messages rather than one. A transport SHALL NOT be able to
address, adopt, or acknowledge another owner's stored message by choosing what it puts in this field.

#### Scenario: A2A uses its task id

- **WHEN** an A2A request is submitted
- **THEN** its `client_message_id` equals the task id, so a client retry with the same task id
  resolves to the same stored message

#### Scenario: A repeated submission does not create a second record

- **WHEN** the same message is submitted twice with the same `client_message_id`
- **THEN** chat storage holds one record for it

#### Scenario: Two owners share a client message id and get two records

- **WHEN** two owners submit messages carrying the same `client_message_id`, in chats that carry the
  same id under each owner, with the same sender session and actor
- **THEN** chat storage holds one record for each owner
- **AND** neither submission is refused as a conflicting duplicate of the other

#### Scenario: A chosen client message id cannot name another owner's row

- **WHEN** an owner submits a message whose `client_message_id` is the stored id of another owner's
  message
- **THEN** the submission persists and submits that owner's own message
- **AND** the other owner's record is neither returned, adopted, nor marked processed

### Requirement: Losing an insert race is not a failure

When persisting an inbound message reports that nothing was inserted, the transport SHALL re-read the
record by `client_message_id` and continue with it. Only when the re-read finds nothing SHALL the
transport treat the outcome as a failure.

The re-read SHALL be scoped to the owner the message was submitted under and SHALL NOT return a
record stored under another owner. A record the transport did not write is not evidence that its own
message was persisted: continuing with one submits the message while acknowledging a row that
belongs to somebody else, which loses that owner's message from the replay sweep without any
statement failing.

#### Scenario: A concurrent writer wins the insert

- **WHEN** two writers persist the same `client_message_id` and one reports no insert
- **THEN** that writer re-reads the existing record and proceeds with it, reporting no error to the
  user

#### Scenario: A genuine persistence failure is reported

- **WHEN** nothing was inserted and the re-read finds no record
- **THEN** the transport reports a failure

#### Scenario: The re-read stays inside the submitting owner

- **WHEN** the re-read runs for one owner while a record stored under a different owner would match
  every other value the identity folds in
- **THEN** the re-read returns nothing
- **AND** the transport treats the outcome as its own persistence failure rather than proceeding
  with the other owner's record

### Requirement: Attachments are staged once and reused on retry

A transport SHALL check for attachments already stored against the message before staging them again,
and SHALL reuse what it finds. Temporary files staged for an inbound message SHALL be deleted only
after the message has been acknowledged as handled.

#### Scenario: A replayed message does not re-upload its files

- **WHEN** a message with attachments is retried after its first attempt failed downstream
- **THEN** the stored attachments are reused and no second upload occurs

#### Scenario: Temporary files survive an unhandled message

- **WHEN** handling fails before acknowledgement
- **THEN** the staged temporary files still exist for the retry

### Requirement: The supervisor's inbound path is generic

On receiving a message the supervisor SHALL log the message to chat history, pass the envelope's
minted scope through unchanged, and submit it to the orchestrator. It SHALL NOT re-derive the
scope, adopt an owner or agent from the message, or reconstruct identity from the payload. It
SHALL NOT branch on which transport the message came from, and SHALL NOT hold transport-specific
state such as an SDK cursor, a provider's session, or a transport's tracing spans.

The queue SHALL be the only way in. The supervisor SHALL NOT hold a reference to a run in flight, ask
one whether it is busy, or add a message to one that has already started. A message that arrives
while its owner has a run in flight SHALL be queued and SHALL run after it, which is what preserves
the order the owner sees.

The reason is not only that a reference cannot cross a process boundary. A run that can absorb a
message it did not start with has two entry points, and only one of them is durable: a message added
to a run that is already ending is dropped without a record, while a queued one survives the process
that was going to handle it.

#### Scenario: Two transports take the same path

- **WHEN** equivalent messages arrive from two different transports
- **THEN** the supervisor performs the same steps for both

#### Scenario: The supervisor holds no transport state

- **WHEN** the supervisor's inbound path is inspected
- **THEN** it references no transport cursor, provider state, or transport-specific tracing

#### Scenario: Identity is passed through, not rebuilt

- **WHEN** the supervisor queues a task for an inbound envelope
- **THEN** the task's scope equals the envelope's scope
- **AND** no supervisor code reconstructs an owner, agent, surface, or chat from the message payload

#### Scenario: A message arriving during a run is queued

- **WHEN** a message arrives for an owner whose task is already running
- **THEN** it is queued and runs after that task
- **AND** nothing is added to the run in flight

#### Scenario: The inbound path holds no reference to a run

- **WHEN** the supervisor's inbound path is inspected
- **THEN** it obtains no agent object, reads no busy flag, and calls no accessor reporting a run's
  current scope

### Requirement: Duplicate chat logging is harmless

Chat logging performed by the supervisor for a message a transport has already persisted SHALL leave
storage unchanged, because the stored identity is derived from `client_message_id` together with the
owner and agent the row is recorded under and the actor, source, chat, user and session.

Storage SHALL enforce that identity itself rather than relying on each writer to derive it
correctly, and the enforcing constraint SHALL be the one that carries the ownership, so that a
second write for the same owner is refused while a write for a different owner is not.

#### Scenario: The same message is written twice

- **WHEN** a transport persists a message and the supervisor then logs the same message
- **THEN** chat storage holds one record and the second write changes nothing

#### Scenario: The database refuses the duplicate

- **WHEN** two writes carrying the same stored identity reach storage without either reading first
- **THEN** exactly one row exists afterwards
- **AND** the second write reports that nothing was inserted rather than raising

### Requirement: The supervisor decides slash commands

The supervisor SHALL recognize slash commands from the submitted message and SHALL execute them
with its own context. No transport SHALL supply a command context, recognize a command, or execute
one.

A message naming a command the runtime once owned and has since retired SHALL be answered by the
runtime as unsupported, and SHALL NOT be queued as work. Queued instead, it reaches the agent, which
answers a command it cannot run as though it had run.

A message naming no command of the runtime SHALL be queued as work, whatever it says — a leading `/`
included, which the browser operator console uses to send the agent work.

The supervisor SHALL hand the handler the submitted message, so a command that takes an argument
keeps it. Reducing the message to its first word makes such a command indistinguishable from itself.

#### Scenario: A slash command from any transport runs with the supervisor's context

- **WHEN** `/status` is submitted from the browser and from dialogs
- **THEN** both execute in the supervisor with the same context and the answer returns over the
  outbound path

#### Scenario: A command keeps the argument it was given

- **WHEN** a command that takes an argument is submitted
- **THEN** the handler receives the message with that argument

#### Scenario: A retired command is refused rather than worked on

- **WHEN** a message naming a retired command is submitted, whatever its casing or argument
- **THEN** the runtime answers that the command is unsupported
- **AND** nothing is queued for it

#### Scenario: A command that never existed is work

- **WHEN** a message opening with a slash that names no owned and no retired command is submitted
- **THEN** it is queued for the agent

#### Scenario: A request to clear the history in plain words is ordinary work

- **WHEN** a message asking for the chat history to be cleared, phrased without a command, is
  submitted
- **THEN** it is queued for the agent and the runtime answers nothing itself

### Requirement: A transport answers its own refusals synchronously

A transport SHALL answer the client directly, and SHALL NOT submit the message, where it refuses
that message on its own grounds — an operator command policy, a payload conflict, a malformed
request.

A message the transport could not check SHALL be refused on the same grounds. A check that is
unavailable, that raises, or that does not return within the transport's bound SHALL produce a
refusal carrying a reason the caller can read; the message SHALL NOT be forwarded on the strength of
a check that did not happen.

Only an unqualified allowance SHALL count as a pass: allowed, with no accompanying message. An
allowance that carries a message — a backend reporting it was not configured, a verdict flagged
rather than clean — SHALL be refused with that message as the reason. The reason this rule sits at
the transport rather than in the checker is that an operator command has no agent downstream to read
a warning and act on it: the caller is the only reader there is, so a warning the transport drops is
a warning nobody ever sees. It is also the only rule decidable from the verdict a transport actually
receives, which makes it hold for whatever backend the deployment configures.

#### Scenario: An operator command policy refusal reaches the client immediately

- **WHEN** a browser operator command is refused by policy
- **THEN** the client receives the refusal on its own connection and nothing is submitted

#### Scenario: A conflicting duplicate is refused

- **WHEN** a message arrives with a `client_message_id` already stored against a different payload
- **THEN** the transport answers the client with a conflict and does not submit

#### Scenario: A command that could not be checked is refused

- **WHEN** an operator command is checked in a process where no safety backend is configured, and the
  check returns an allowance qualified by a warning that it did not run
- **THEN** the transport refuses the command, the client is told the check could not be performed,
  and nothing is submitted

#### Scenario: A flagged allowance is not a pass

- **WHEN** the check allows the command but returns a message flagging it
- **THEN** the transport refuses it and the message is the reason shown to the client

#### Scenario: A check that raises answers the client rather than dropping the connection

- **WHEN** the check raises instead of returning a verdict
- **THEN** the client receives a refusal on its still-open connection and nothing is submitted

### Requirement: The task dict carries scope, not conversation_id or session_id

The task dict handed to the agent SHALL carry the minted `scope` (a serializable dict) and SHALL
NOT carry `conversation_id` or `session_id`. Supervisor-side storage writes SHALL pass that same
scope to the store rather than deriving a conversation key from its axes; no supervisor code SHALL
construct a storage key from `surface_id` and `chat_id`.

#### Scenario: A queued task exposes scope only

- **WHEN** a worker reads a queued task's input dict
- **THEN** it contains `scope` with `owner_id`, `agent_id`, `surface_id`, and `chat_id`
- **AND** it contains neither `conversation_id` nor `session_id`

#### Scenario: Storage writes carry the scope itself

- **WHEN** the supervisor writes a message or a task row
- **THEN** it passes the scope to the store
- **AND** the stored row's ownership columns equal that scope's axes

### Requirement: An originating transport establishes the operation where the update arrives

A transport that originates an operation SHALL establish it at the frame where its protocol hands the
update over — the handler the client library calls — and not at the later frame where the message is
submitted to the supervisor. A transport originates when its protocol carries no identifier for the
caller to supply.

Everything the transport does on the update's behalf before submitting it — downloading an
attachment, checking it against policy, rejecting it — SHALL run inside that operation. This is the
work most likely to fail and the work an operator most needs to correlate, and a scope opened at
submission time excludes all of it.

Exactly one identifier SHALL be established per inbound update. A later frame handling the same
update SHALL accept the identifier the ingress established rather than originate a second one for the
same message; it SHALL originate only when none was supplied, so a message reaching it by another
path — a replayed backlog, a caller that did not go through the ingress — still runs under one.

Where the work crosses a thread — a worker pool that processes attachments, a queue consumed by
another thread — the identifier SHALL travel with the work item as data. A context variable SHALL NOT
be relied on to cross a thread, and a message carrier SHALL declare a field for the identifier rather
than the receiving side re-deriving or re-originating it.

#### Scenario: Pre-submission work is inside the operation

- **WHEN** an update carrying an attachment arrives and its file is downloaded and validated before
  submission
- **THEN** the download and the validation run under the same operation identifier as the submission
- **AND** the log records they emit carry that identifier

#### Scenario: One update establishes exactly one identifier

- **WHEN** an update is accepted at the ingress and later submitted
- **THEN** the submitted message carries the identifier the ingress established
- **AND** no second identifier is originated for that update

#### Scenario: A message arriving without one still runs under an identifier

- **WHEN** a message reaches the submitting frame with no operation identifier
- **THEN** that frame originates one
- **AND** the submission carries it

#### Scenario: The identifier crosses a worker thread as data

- **WHEN** an update is handed to a worker thread for processing before submission
- **THEN** the work item carries the operation identifier
- **AND** the worker runs the processing under it

### Requirement: A message polled but not handled is handed over again

The transport's journal covers one half of the hand-over: the row is written unprocessed, submitted,
and marked processed only once `submit` returns, so a crash before `submit` leaves a row the sweep
replays. The other half SHALL be covered by the seam's settlement rule, because by the time `submit`
returns the transport has given up ownership and its row already says processed — a message the
supervisor polled and never handled is invisible to every sweep, stays in the user's own history, and
never becomes a task.

The supervisor SHALL handle a polled envelope before it polls again, since the next poll is what
settles the previous envelope. It SHALL NOT poll a batch ahead of handling, hand polled envelopes to
a pool, or otherwise separate taking a message from finishing with it.

A supervisor that stops before its next poll — crash, restart, deployment roll — SHALL leave that
envelope unsettled, and the supervisor that replaces it SHALL receive the same envelope. Handling
that raised SHALL still be settled by the next poll: the supervisor answers the originating chat and
does not requeue, so a message that fails deterministically is not redelivered forever.

#### Scenario: The drain handles each envelope before polling again

- **WHEN** the supervisor drains the inbox
- **THEN** each envelope's handling has returned before the next poll is issued
- **AND** no polled envelope is queued for later handling by another worker

#### Scenario: A supervisor that dies mid-handling receives the message again

- **WHEN** the supervisor polls an envelope and the process ends before its next poll
- **THEN** the replacement supervisor's first poll returns that same envelope
- **AND** the transport's journal row, already marked processed, is not what recovers it

#### Scenario: A handling failure is answered, not requeued

- **WHEN** handling a polled envelope raises
- **THEN** the supervisor answers the originating chat and continues its drain
- **AND** the envelope is settled by the next poll rather than delivered again

### Requirement: A redelivered message resolves to the same row and the same task

Redelivery SHALL be safe because inbound identity is derived, never minted at the point of handling.
The stored chat row is keyed by `client_message_id` together with the actor and the scope, so a second
pass writes the same row; the queued task id is derived from the same material, and a submission whose
task already exists SHALL return that task rather than queue a second one. A user SHALL therefore see
one message and one answer no matter how many times the envelope was delivered.

This SHALL hold only for envelopes carrying a `client_message_id`. An envelope without one takes a
freshly generated task id, so redelivery would run it twice; the requirement that every submitted
message carries a stable client message id is what makes at-least-once safe, and the seam SHALL NOT
add a second defence behind it.

#### Scenario: The same envelope delivered twice queues one task

- **WHEN** an envelope with a `client_message_id` is handled and then delivered again after the
  consumer stopped
- **THEN** the second pass resolves to the task the first pass queued
- **AND** no second task is created

#### Scenario: The second pass leaves chat history unchanged

- **WHEN** a redelivered envelope is logged to chat history again
- **THEN** storage holds one record for it and the second write changes nothing

#### Scenario: An envelope without a client message id has no protection

- **WHEN** an envelope carrying no `client_message_id` is delivered twice
- **THEN** each delivery takes a fresh task id and the message runs twice
- **AND** this is why a transport SHALL set a stable `client_message_id` on every envelope it submits

### Requirement: A transport's policy check runs off the connection loop

A transport SHALL NOT run a policy check on the event loop that serves its connections. It SHALL run
the check on a worker of its own and await the outcome, so that frames belonging to other owners on
the same instance keep being received and delivered while a check is in flight.

The bound on the check SHALL be the transport's own, chosen for an interactive command, and SHALL NOT
be inherited from the checking backend's timeout for agent-side calls. Expiry SHALL be a refusal, as
above.

An abandoned check SHALL hold neither the loop, the connection, nor capacity shared with the
transport's other blocking work: the worker SHALL belong to the call and SHALL be released without
waiting for a check that has already been given up on. A check parked on a shared pool is the same
outage one step removed — the transport's storage and delivery hops queue behind it.

This matters because an instance serves many owners. A check that blocks the loop stalls every
connection on that instance, including the heartbeat, on behalf of one command that none of those
owners sent.

#### Scenario: Other owners are served while a check is in flight

- **WHEN** one owner's operator command is being checked and the check has not returned
- **THEN** another owner's frame on the same instance is still received and answered

#### Scenario: A check that never returns is given up on

- **WHEN** a check exceeds the transport's bound
- **THEN** the caller receives a refusal at that bound
- **AND** the connection stays open and the instance keeps serving other connections

#### Scenario: An abandoned check holds no shared capacity

- **WHEN** a check has been abandoned and is still running
- **THEN** the transport's other blocking work proceeds without waiting for it

### Requirement: A refusal caused by a missing check names itself as one

When a command is refused because the check could not be performed, the answer SHALL be
distinguishable on the wire from a refusal produced by a verdict — a distinct machine-readable code —
and the transport SHALL log a warning naming why the check was unavailable.

The two refusals have different fixes: one is the deployment's configuration, the other is the
command that was sent. A single opaque "not allowed" makes a misconfigured instance look like a
correctly working one that dislikes every command.

#### Scenario: A missing backend and a blocked command are told apart

- **WHEN** one command is refused because no check could run and another is refused by a verdict
- **THEN** the two answers carry different codes

#### Scenario: The operator can see why nothing can be checked

- **WHEN** a command is refused because the check was unavailable
- **THEN** a warning is logged naming the reason the check could not run

### Requirement: The journal sweep addresses every stored owner and agent

The sweep SHALL enumerate the owner/agent pairs that exist in message storage and SHALL read the
unprocessed rows of each pair under an identity built from that pair. The pair SHALL come from the
stored rows; the sweep SHALL NOT derive whose rows to replay from the instance's configuration and
SHALL NOT derive it from any message payload. The identity SHALL be author-less work naming the
sweep as its job, because a replay has no conversation behind it.

The sweep SHALL live in one place shared by every transport that keeps an inbound journal, so a fix
cannot land for one transport and leave another silently unrecovered.

#### Scenario: Every owner in storage is swept

- **WHEN** unprocessed rows exist for three different owners and none of them is the instance's
  configured owner
- **THEN** all three are submitted

#### Scenario: The sweep reads no owner from configuration

- **WHEN** the sweep's read path is inspected
- **THEN** it obtains owner and agent from the enumerated stored pairs
- **AND** it calls no function that returns the instance's configured owner

#### Scenario: All transports inherit the fix

- **WHEN** the browser, dialogs, and A2A sweeps are inspected
- **THEN** each reaches storage through the shared replay, and none carries its own owner-selection
  logic

### Requirement: A journal sweep is bounded and reports what it left

A sweep SHALL bound the rows it replays in one pass: a per-pair window over the newest rows the
journal never handed over, and a bound on the rows replayed across the whole pass. When either bound
truncates the pass, the sweep SHALL report the truncation at warning level with the count it stopped
at. The per-pair window's truncation SHALL be reported with how many pairs filled their window, and
SHALL NOT stop the pass: one busy pair must not withhold the sweep from the pairs behind it.

A sweep SHALL NOT bound the number of ownership pairs it enumerates. Pairs SHALL be enumerated in
pages through a keyset cursor over the enumeration's own order, and every page SHALL be drained
within the one pass.

Truncation SHALL leave the remaining rows unprocessed so a later sweep replays them; the sweep SHALL
NOT mark, delete, or otherwise consume a row it did not submit.

#### Scenario: A backlog larger than the budget stops and says so

- **WHEN** the unprocessed rows across all pairs exceed the pass budget
- **THEN** the sweep replays up to the budget, logs a warning naming how many it replayed and that it
  stopped early
- **AND** the rows it did not reach are still unprocessed afterwards

#### Scenario: A truncated pass drains on the next sweep

- **WHEN** a sweep runs again after a truncated pass with no new rows written
- **THEN** the rows the previous pass did not reach are submitted

#### Scenario: A pair with more stranded rows than its window says so, and the pass goes on

- **WHEN** one pair holds more rows the journal never handed over than the per-pair window admits
- **THEN** the sweep logs a warning naming the window and how many pairs filled it
- **AND** it still sweeps the pairs enumerated after it in the same pass

#### Scenario: More pairs than one page holds are all reached in one pass

- **WHEN** unprocessed rows exist for more ownership pairs than one enumeration page carries
- **THEN** one pass submits the rows of every pair, including those past the first page
- **AND** the pass reports no pair-axis truncation

### Requirement: A journal sweep reports the ground it covered

A sweep SHALL report how many pairs it walked and how many rows it replayed on every pass, including
a pass that replayed nothing. A sweep that recovered nothing and a sweep that addressed no owner at
all SHALL NOT be indistinguishable in the log, because the second is a misconfiguration that
otherwise surfaces only as messages that never became tasks.

#### Scenario: A sweep that replays nothing still reports

- **WHEN** a sweep finds no unprocessed row
- **THEN** it reports the number of pairs it walked and zero rows replayed

#### Scenario: A sweep that finds no pairs is visible

- **WHEN** message storage holds no owner/agent pair
- **THEN** the sweep reports zero pairs walked

### Requirement: Concurrent sweeps may replay a row more than once

Several instances of the same transport MAY sweep the same rows at the same time. The sweep SHALL NOT
claim or lease a row before submitting it, and SHALL mark a row processed only after its submission
returns, so an instance that dies mid-sweep leaves the row for another.

The duplicate SHALL be harmless rather than merely rare: repeated submissions of one stranded row
SHALL resolve to a single task, because the envelope carries the row's stored `client_message_id` and
the inbound task id is derived from it together with the envelope's scope. Acknowledging an already
acknowledged row SHALL change nothing.

The cost SHALL be bounded and stated: the submissions one boot can produce is the number of
instances times the pass budget, and the inbox behind the seam SHALL be sized for that burst.

Each backend SHALL state what "sized" means for it:

- For a queue held in memory, whether in the process or in a Redis stream, it is memory.
- For a broker that bounds its log by retention, it means two things. Retention never evicts an
  envelope no consumer has settled. Every envelope a transport submits fits under the broker's
  message-size ceiling.

An envelope over that ceiling SHALL be refused at `submit`, before anything is appended, so its row
stays unprocessed and the failure is logged, rather than the envelope being accepted and lost.

#### Scenario: Two instances replay the same stranded row

- **WHEN** two instances sweep the same unprocessed row and both submit it
- **THEN** one task exists for it
- **AND** chat storage still holds one record for it

#### Scenario: An instance dies mid-sweep

- **WHEN** an instance submits a stranded row and stops before marking it processed
- **THEN** another instance's sweep finds the row still unprocessed and replays it

#### Scenario: A retention-bounded inbox keeps every envelope nobody settled

- **WHEN** a burst of replayed envelopes is submitted to an inbox on a broker that bounds its log by
  retention, and no consumer settles them for longer than any retention period
- **THEN** none of them is evicted before a consumer settles it

#### Scenario: An envelope over the size ceiling is refused at submit

- **WHEN** a transport submits an envelope larger than the broker's message-size ceiling
- **THEN** `submit` raises before anything is appended
- **AND** the transport's row stays unprocessed

### Requirement: A reply to an open confirmation resolves it instead of queueing a task

Where a confirmation request is open for the submitted message's scope and the message is one of the
recognized replies, the supervisor SHALL resolve the request and SHALL NOT enqueue a task for that
message. The supervisor SHALL answer the owner either way, as it does for a command.

Where the decision applied, the supervisor SHALL queue the task the request belongs to, under its own
id: the run gave its worker back to wait, and the answer is what puts it back in line. A queueing that
follows a write which did not apply would start a round with no decision to read, so the write's own
outcome SHALL decide whether the task is queued.

The reply SHALL be recognized in the supervisor, from the same vocabulary contract the command
recognition uses, and SHALL NOT be recognized in a transport: a transport may not import the
supervisor, and a second recognizer is a second source of truth for what a reply means.

The message SHALL still be persisted to the conversation before it is recognized, so the reply reads
back in the history beside the question it answers, and so that a bare word is read against what
preceded it: it is a reply only while the question is the row before it (`tool-confirmation`).

Where no request is open for that scope, the same text SHALL be submitted as an ordinary message. A
recognized word is a reply only while something is waiting for it.

Recognition SHALL NOT consult a model. A reply decides whether an effect the owner cannot recall is
allowed to happen, and that decision is not a place for an inference that can be wrong.

#### Scenario: A reply while a request is open resolves it

- **WHEN** the owner sends a recognized reply and a confirmation request is open for that scope
- **THEN** the request is resolved with that decision
- **AND** the task that was parked on it is queued under its own id
- **AND** no new task is enqueued for the message
- **AND** the owner is told the decision was recorded

#### Scenario: A decision that did not apply queues nothing

- **WHEN** the conditional write finds the request already settled
- **THEN** no task is queued and the owner is told what settled it

#### Scenario: The same word with nothing open is an ordinary message

- **WHEN** the owner sends the same text with no open request for that scope
- **THEN** it is submitted as an ordinary message and a task is enqueued as before

#### Scenario: A reply is kept in the conversation

- **WHEN** a reply is recognized and resolves a request
- **THEN** the message is persisted to the conversation history like any other owner message

#### Scenario: A reply addressed to another scope does not resolve this one

- **WHEN** a recognized reply arrives under a scope other than the open request's
- **THEN** that request is unchanged and the message is submitted as an ordinary message

### Requirement: The journal marker is a stored column, and the window is taken over it

The marker a transport writes before it hands a message to the seam SHALL be materialised in an
indexed `messages.inbound_unprocessed` column at write time, and the crash-replay sweep's per-pair
window SHALL be taken over that column rather than over the pair's whole history.

The column SHALL be true for exactly the rows a sweep replays: an inbound owner-authored row, in a
conversation a scope can reach, whose metadata says the journal never handed it over and does not
mark it as history the transport merely stored. A transport that persists a row it will never
submit — replayed history and the unaddressed group message are the cases that exist — SHALL write
it already processed, so it never becomes a journal row.

The predicate SHALL keep excluding a row whose metadata carries `history_only`, even though such a
row is now written already processed at insert time. That term is what the *stored* corpus needs:
rows written before this change took `inbound_processed: False` and were retired by a second write
that a crash — or a swallowed failure on the marking path — could leave undone, and every group
message nobody addressed to the agent is kept as history that way. Dropping the term makes the
backfill mark that whole population, and the first sweep after the upgrade delivers it as live user
messages: the agent answers messages that were never addressed to it.

The column SHALL be derived and never a second declaration. `inbound_processed` inside the message
metadata stays the one thing a transport writes and the one thing every reader that holds a row
reads; the column SHALL be recomputed by one predicate wherever that metadata is written, so the two
cannot disagree. The acknowledgement path SHALL recompute it, because a metadata write that leaves
the column behind makes the sweep replay handed-over rows on every boot.

The sweep SHALL apply the marker in the statement rather than in Python after it. A predicate a
reader applies after the read cannot run before the `LIMIT`, so the newest rows of a pair crowd out
the older stranded ones — and because a pass marks processed exactly the rows it read, each pass
pushes the stranded row one position further out. The column SHALL be appended to the statement as a
bare clause and never as a comparison against a bound parameter, so a partial index over it is
provably implied at plan time. Every other predicate the sweep needs, the conversation among them,
SHALL be in that statement too: a row the read returns is a row the sweep submits, so the window it
was given is the work it found.

#### Scenario: A stranded row older than the window is still found

- **WHEN** a pair holds one row the journal never handed over and more newer handed-over rows than
  the per-pair window admits
- **THEN** the sweep returns the stranded row

#### Scenario: A repeated sweep does not bury what it left

- **WHEN** a sweep runs twice over a pair whose stranded rows exceed the budget
- **THEN** the second pass returns the rows the first did not reach

#### Scenario: Acknowledging a row clears the column

- **WHEN** a row is marked processed through the acknowledgement path
- **THEN** a later sweep of that pair does not return it

#### Scenario: A row the transport never submits is not a journal row

- **WHEN** a transport persists a row as replayed history rather than as a message it is about to
  hand over
- **THEN** the column is false on it and no sweep returns it
- **AND** a stored row carrying the unprocessed marker together with `history_only` is not returned
  either
