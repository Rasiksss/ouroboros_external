# memory-consolidation Specification

## Purpose

The consolidation lifecycle end to end: the supervisor publishes one raw trigger event after
persisting any chat message and decides nothing; the handler decides thresholds, single-flight,
deferral, and the failure cooldown; one run advances by follow-up delivery with its state in memory;
the summary is committed under compare-and-swap; and the retired consolidation task type is never
executed.
## Requirements
### Requirement: The supervisor publishes a raw trigger and decides nothing

After persisting a chat message, whatever actor wrote it, the supervisor SHALL publish one
persisted-message event naming the scope the row was written under and the message. The scope SHALL
be the same value the write used, passed through rather than minted at the announcement: the writer
holds it, and a second mint at the same seam is a second answer to the same question. Every persisted
row counts toward the backlog, so every one SHALL announce itself: restricting the trigger to agent
replies leaves a failed run with no way back once a session ends on one. The supervisor SHALL NOT
read consolidation thresholds, read the committed summary, count messages or tokens, dedupe against
work in flight, or write a task row. A message that failed to persist SHALL produce no event.

#### Scenario: A persisted message is reported whoever wrote it

- **WHEN** a message from any actor is persisted to chat history
- **THEN** exactly one persisted-message event is published, carrying the scope the row was written
  under and the message id

#### Scenario: The announcement mints no identity of its own

- **WHEN** every publish site of the persisted-message event is inspected
- **THEN** each passes the scope it already holds, and none constructs one

#### Scenario: An unpersisted message is not reported

- **WHEN** persistence returns nothing
- **THEN** no event is published

#### Scenario: The trigger reaches no scheduler

- **WHEN** the chat-persistence path's imports and calls are inspected
- **THEN** it neither imports the durable task queue nor reads settings for thresholds

### Requirement: Thresholds and single-flight are decided by the handler

The consolidation handler SHALL decide whether a trigger starts a run. It SHALL address the store
through the scope the trigger carries and SHALL NOT mint one of its own.

Thresholds and single-flight SHALL be evaluated per ownership pair — the `owner_id` and `agent_id`
of that scope — because that pair is what the backlog read and the committed summary are keyed by.

Single-flight SHALL hold across every instance running the handler, not only within one process. A
guard kept in process memory answers for one instance's runs and says nothing about a peer's, so with
several instances consuming one event stream a trigger can be evaluated for a pair another instance
is already summarizing. The guard SHALL therefore be a lease in shared storage, keyed by the same
ownership pair.

Single-flight SHALL also hold **within** one process, and SHALL NOT rely on the lease to provide it.
The lease is deliberately acquirable by the holder that already holds it — a holder names a process,
not a run — so two deliveries of one process for one pair both acquire it and neither learns of the
other. Inside a process the guarantee is therefore the loop's serialization per ownership pair, and
the handler SHALL be entitled to assume that no second delivery for its pair is running beside it.

The handler SHALL start a run only when no run is active for that pair anywhere in the deployment and
the pair's backlog after its committed cursor reaches the configured message threshold or token
threshold, or when the committed cursor names a message that no longer exists. A trigger arriving
while a run is active for its pair SHALL be dropped without reading thresholds; a trigger for a pair
with no active run SHALL be evaluated normally, whatever other pairs are doing. A trigger that finds
the pair free, passes the thresholds, and then loses the race to take the lease SHALL be dropped
without calling the model. A threshold configured as zero or negative SHALL be rejected as an error.

Surface and chat SHALL NOT enter the decision. Two chats of one owner share one backlog and one
summary row, so treating them as two runs would be two model calls competing for one commit.

#### Scenario: A backlog below both thresholds starts nothing

- **WHEN** a trigger arrives and the backlog is under the message threshold and under the token
  threshold
- **THEN** no run starts and no model is called

#### Scenario: Either threshold reached starts a run

- **WHEN** a trigger arrives and the backlog reaches the message threshold, or reaches the token
  threshold
- **THEN** a run starts from the committed cursor

#### Scenario: A missing anchor starts a run and recovers

- **WHEN** the committed cursor names a message that is no longer stored
- **THEN** a run starts, and the backlog is listed from the beginning of history instead of from the
  missing anchor

#### Scenario: A trigger during an active run is dropped

- **WHEN** a trigger arrives whose ownership pair already has a run active
- **THEN** it is dropped, no thresholds are read, and no additional model call is made

#### Scenario: One owner's run does not block another owner's trigger

- **WHEN** a run is active for one ownership pair and a trigger arrives carrying a different pair
- **THEN** the trigger is evaluated against the thresholds and may start its own run

#### Scenario: Two chats of one owner produce one run, not two

- **WHEN** triggers arrive from two different chats whose scopes share an owner and an agent
- **THEN** at most one run is active for that pair, and only one commit is attempted

#### Scenario: The handler mints no scope

- **WHEN** the consolidation handler's module is inspected
- **THEN** it constructs no scope and imports no scope mint

#### Scenario: A trigger during another instance's run is dropped

- **WHEN** a trigger arrives at one instance for a pair a different instance is already consolidating
- **THEN** it is dropped, no thresholds are read, and no additional model call is made

#### Scenario: Two instances past the thresholds start one run

- **WHEN** two instances evaluate triggers for the same pair at the same time and both find the
  backlog past a threshold
- **THEN** exactly one run starts and exactly one model call is made

#### Scenario: Two triggers for one pair in one process do not run together

- **WHEN** two triggers for the same ownership pair are delivered to one process while that process
  runs work concurrently
- **THEN** the second is not handled while the first is still being handled
- **AND** the lease being acquirable by its own holder is not what prevented it

### Requirement: A run advances by follow-up delivery, and its state is in memory

A run's state — expected cursor, pending message and offset, pending summary, tail watermark, and
start time — SHALL live on the handler, not in the event, the transport, or the task store. That
state SHALL be held per ownership pair rather than as one slot, so that runs for two pairs neither
share a cursor nor evict one another. Only the pair's lease and its failure throttle are shared with
other instances; the run itself stays where it is executed.

Because that state is held per pair and written across several deliveries, it SHALL be written by one
delivery at a time. The loop's serialization per ownership pair is what provides that; the handler
SHALL NOT add a lock of its own, and SHALL NOT be written so that it would be correct without it.

The run SHALL also carry the scope it belongs to, and every follow-up it produces SHALL name that
scope. This is identity, not state: it is how the loop routes a re-delivery and how the handler finds
the run being resumed, rather than assuming there is only one. A run with work remaining SHALL
request immediate re-delivery; a run yielding to foreground work SHALL request delayed re-delivery.
Re-delivery SHALL use a distinct runtime-internal event kind, so that a continuation is never
mistaken for a new trigger. A continuation whose ownership pair has no active run SHALL be ignored
without failing.

A run SHALL be recorded in that state only after every step that can raise has completed, and a run
that fails SHALL be released before the handler returns. The loop does not reschedule a follow-up
whose dispatch failed, so a run left recorded with none outstanding would be blocked by its own
single-flight guard for the life of the process. Releasing a run SHALL release its lease in the same
step: a run released only locally would leave its pair blocked for every instance until the lease
expired.

A request to stop SHALL release the run and its lease rather than request a re-delivery. The loop
re-checks the stop signal before delivering a follow-up, so a run that merely defers on shutdown is
abandoned holding its lease, and an ordinary redeploy would park every active pair until its lease
expired.

Releasing SHALL survive the cancellation of the delivery that is releasing. Where a delivery is
cancelled — at shutdown, or because it outlasted the drain bound — the release SHALL still run to
completion. A release that reaches the database is an awaited operation, and one cancelled at its
first suspension point leaves exactly the held lease this rule exists to prevent.

The stop signal SHALL be re-read at the moment a re-delivery is requested, and not only before the
model call. It arrives while the run is executing — a model call under its own deadline is the
longest such window — so a run that read it only on entry would go on to request a re-delivery that
the loop will not deliver, which is the abandonment this rule exists to prevent.

#### Scenario: A continued run advances to the next batch

- **WHEN** a batch is summarized and the backlog still holds messages
- **THEN** the handler requests immediate re-delivery
- **AND** the next delivery summarizes the following batch rather than restarting the run

#### Scenario: A continuation is not confused with a trigger

- **WHEN** a run is active and a continuation is delivered
- **THEN** the run advances rather than being dropped by the single-flight rule

#### Scenario: A continuation resumes its own run

- **WHEN** runs are active for two ownership pairs and a continuation naming one of them is delivered
- **THEN** that pair's run advances
- **AND** the other pair's run is untouched

#### Scenario: A stale continuation is harmless

- **WHEN** a continuation is delivered whose ownership pair has no active run
- **THEN** it is ignored, no model is called, and the loop continues

#### Scenario: A failure while starting leaves no run recorded

- **WHEN** deciding whether to start a run raises
- **THEN** no run is recorded for that ownership pair
- **AND** the next trigger for it is evaluated against the thresholds as usual

#### Scenario: A failing run releases itself

- **WHEN** a run fails at any point after it started
- **THEN** it is no longer recorded as active before the handler returns
- **AND** its lease is released, so a later trigger for the same pair — on this instance or another —
  is not dropped by single-flight

#### Scenario: A restart forgets pending work without losing content

- **WHEN** the process restarts while a run has a pending fragment
- **THEN** the next run starts from the last committed cursor and re-summarizes the backlog

#### Scenario: A stop arriving mid-run still frees the pair

- **WHEN** the stop signal is set while a run is calling the model, and the run would otherwise ask
  to be delivered again
- **THEN** the run ends and releases its lease instead of requesting re-delivery
- **AND** the loop stops with no re-delivery outstanding for that pair

#### Scenario: A stop request frees the pair

- **WHEN** the stop signal is set while a run is active
- **THEN** the run ends and its lease is released rather than a re-delivery being requested
- **AND** an instance that starts afterwards may consolidate that pair without waiting for an expiry

#### Scenario: A cancelled delivery still releases the lease

- **WHEN** a delivery holding a run is cancelled for outlasting the drain bound
- **THEN** the lease release completes rather than being cancelled part-way
- **AND** the pair is free without waiting for the expiry

#### Scenario: The run state needs no lock of its own

- **WHEN** the handler's module is inspected
- **THEN** it holds no lock over its per-pair run state
- **AND** the serialization it relies on is the loop's, per ownership pair

### Requirement: A run yields to foreground work, with an age override

While foreground work is active, a run SHALL defer instead of calling the model, and SHALL request
re-delivery so it resumes once the foreground is idle even if no new message arrives. A run older
than the maximum deferral SHALL proceed despite active foreground work. A request to stop SHALL
always end the run before the model call, whatever the run's age.

A stop SHALL end the run rather than defer it. The loop re-checks the stop signal before delivering a
follow-up, so a re-delivery requested on shutdown never arrives and the run is abandoned holding its
pair; an ordinary redeploy would then park every active pair until its lease expired. Ending on a stop
SHALL release the lease and SHALL leave the pair's consecutive-failure count untouched: a redeploy is
not a failed consolidation, and a shared count would carry the throttle into the fleet that comes back.
A run deferring to foreground work SHALL keep its lease, because it intends to resume.

Whether foreground work is active SHALL reach the handler through an injected interface, not a query
the handler makes itself, so that a multi-instance scheduler replaces the implementation without
touching the handler. The single-process implementation SHALL read the supervisor's task table, and
a table it cannot read SHALL count as pressure: a run that cannot see the foreground must not assume
it has the machine to itself.

#### Scenario: Active foreground work defers the run

- **WHEN** a run reaches a model call while any task is queued, scheduled, interrupted, or running
- **THEN** no model is called and the run requests delayed re-delivery
- **AND** it keeps its lease on the pair while it waits

#### Scenario: The run yields to the interface, not a query of its own

- **WHEN** the injected foreground pressure reports work pending and no task row exists
- **THEN** the run still defers, and it proceeds once that same interface reports none

#### Scenario: A deferred run resumes without a new trigger

- **WHEN** the foreground becomes idle after a deferral
- **THEN** the run proceeds on a re-delivery, with no new persisted message required

#### Scenario: An overdue run stops yielding

- **WHEN** a run older than the maximum deferral reaches a model call while foreground work is
  active
- **THEN** the run proceeds

#### Scenario: A stop request ends even an overdue run

- **WHEN** the stop signal is set while a run older than the maximum deferral reaches a model call
- **THEN** no model is called
- **AND** the run ends and releases its lease rather than requesting re-delivery
- **AND** the pair's consecutive-failure count is unchanged

### Requirement: The summary is committed under compare-and-swap on the cursor

A completed run SHALL commit its summary only where the stored cursor still equals the cursor the run
started from, in a single transaction, and SHALL write the model's text verbatim. The row it matches
SHALL be identified by the ownership axes of the scope the run belongs to — `owner_id`, `agent_id`
and `chat_id` — not by a constant row id, so that a run under one agent can neither read nor
overwrite the summary of another, and neither can a run under one chat of the same pair. A failed
swap SHALL drop the run and leave the stored summary untouched, and the run SHALL be recorded as
stale. The commit SHALL NOT create, claim, or close a task row.

The chat axis is what makes the swap a guard rather than a race between conversations. A run reads
one chat's backlog; committed one axis short, it writes that reading over a summary of a different
conversation and moves the shared cursor to a message id from a history the displaced run never saw.
That cursor is not compared for identity on the next read: `list_chat_history_messages` resolves the
anchor through an unscoped `get_message` and turns it into a `(created_at, message_id)` ordering
boundary (`ouroboros/storage/sql_runtime_store.py`), so the displaced chat does not even
raise `MessageAnchorMissingError` — its next backlog read simply starts after the other chat's
timestamp, and everything it had said before that instant is never summarized by any later run. Two
active chats of one owner would therefore alternately erase each other's summary and step each other
past their own unsummarized messages, and every step of it is silent.

The committed summary SHALL carry the run's `chat_id` from `run.scope.chat_id`.

The insert the commit falls through to when no cursor is expected — the first summary a chat ever
gets (`ouroboros/storage/sql_runtime_store.py`) — SHALL name the chat in its conflict
target, and the uniqueness it names SHALL be the same width (`uq_context_summaries_owner_agent`,
`infra/migrations/versions/0008_owner_scope_rekey.py`). Left at the pair, the first
commit of a second chat conflicts with the first chat's row, `DO NOTHING` reports no rows, and the
run is dropped as stale — not once, but for every run that chat will ever start, since it never gets
a row of its own to swap against. The symptom is a permanently unsummarized chat behind a log line
that reads like an ordinary lost race.

#### Scenario: A summary is committed against its expected cursor

- **WHEN** a run finishes its backlog
- **THEN** the stored summary and cursor are replaced in one transaction, matched on the expected
  cursor and on the owner, the agent and the chat the run belongs to

#### Scenario: A conflicting write is refused

- **WHEN** the stored summary already advanced to a different cursor
- **THEN** the commit fails, the stored summary is unchanged, and the run is dropped and recorded as
  stale

#### Scenario: A second agent does not overwrite the first one's summary

- **WHEN** a run commits a summary under a different `agent_id` for the same owner
- **THEN** the first agent's stored summary is unchanged
- **AND** each agent reads back its own summary

#### Scenario: A second chat does not overwrite the first one's summary

- **WHEN** a run commits a summary under a different `chat_id` for the same owner and agent
- **THEN** the first chat's stored summary and cursor are unchanged
- **AND** each chat reads back the summary committed under its own chat

#### Scenario: A first commit for a second chat is not refused as stale

- **WHEN** a chat with no stored summary commits its first one while another chat of the same owner
  and agent already has one
- **THEN** the insert creates a row for that chat rather than conflicting away
- **AND** the run is recorded as completed rather than dropped as stale

#### Scenario: One chat's commit never moves another chat's backlog window

- **WHEN** one chat commits a summary and a second chat of the same owner and agent then starts a run
- **THEN** the second run's expected cursor names a message of its own chat
- **AND** its backlog begins at that message, with no message of its own skipped for having been
  written before the other chat's cursor

#### Scenario: A truncated or unsuccessful response is never committed

- **WHEN** the model's response has empty content, or lacks a successful finish reason
- **THEN** it is rejected as invalid rather than committed

### Requirement: Consolidation behavior is preserved from the durable implementation

A run SHALL summarize at most 100 messages per batch. A message too large for the input budget SHALL
be split into fragments on UTF-8 boundaries, bounded by both a byte cap and a token budget, and
carried across deliveries by a pending offset. Listing messages SHALL recover from a missing anchor
by listing from the beginning. The model call SHALL use the light model with one fallback, sharing a
deadline of the background read timeout plus a fixed margin.

Every step of a run that performs input or output SHALL be awaited rather than blocking: reading the
backlog, calling the model, renewing the lease, committing the summary and releasing the lease. The
run SHALL block the event loop it runs on at no point.

Making those steps asynchronous SHALL change no value and no outcome. The batch size, the byte cap,
the token budget, the fragmenting rule, the anchor recovery, the model pair and the shared deadline
SHALL be exactly what they are, and the deadline SHALL still be shared with the fallback so the
fallback is attempted within what remains of it.

#### Scenario: An oversized message is summarized in fragments

- **WHEN** a single message exceeds the input budget
- **THEN** it is summarized across successive deliveries in fragments split on UTF-8 boundaries

#### Scenario: A pending offset off a UTF-8 boundary is rejected

- **WHEN** a run's pending offset does not fall on a UTF-8 boundary, or falls outside the message
- **THEN** the run fails with an error rather than producing a corrupt fragment

#### Scenario: The fallback model carries the remaining deadline

- **WHEN** the primary model times out or returns an invalid response
- **THEN** the fallback is attempted within what remains of the shared deadline

#### Scenario: A run in a model call does not stall its neighbours

- **WHEN** a run is inside its model call and a delivery for a different ownership pair is scheduled
  on the same event loop
- **THEN** that delivery runs

#### Scenario: The preserved values are unchanged

- **WHEN** the batch size, byte cap, token budget, model pair and shared deadline are compared with
  the synchronous implementation's
- **THEN** each is the same value

### Requirement: Consolidation reports only through its observability event

A run that finishes SHALL record one observability event under the memory stream carrying at least
its status, the ownership pair it ran for, the resulting cursor, the tail watermark, the batch count,
and token totals. It SHALL NOT carry the surface or the chat of the trigger that started it: the run
is keyed by the pair and summarizes every chat of that owner, so naming one chat would read as a
claim about what was summarized. It SHALL NOT emit a usage event, a task-done event, or anything else
onto the supervisor's event stream. Deferral and continuation are not outcomes and SHALL be logged
only.

#### Scenario: A completed run is observable

- **WHEN** a run commits a summary
- **THEN** an observability event records status, the ownership pair, cursor, tail watermark,
  batches, and tokens

#### Scenario: The outcome names no chat

- **WHEN** a run's observability event is inspected
- **THEN** it carries no surface and no chat id

#### Scenario: A failed run is observable

- **WHEN** a run raises
- **THEN** an observability event records the failure and the loop continues

#### Scenario: A run emits nothing onto the supervisor's stream

- **WHEN** a run finishes, successfully or not
- **THEN** no usage event and no task-done event is emitted

### Requirement: Only a completed run chains into the next one

After a run commits, the handler SHALL re-check thresholds and start a new run if they are met. After
a run fails, it SHALL start nothing at all, whether or not the chat tail moved while it ran, so a
failure cannot drive a retry loop; the messages that arrived meanwhile trigger on their own, at the
rate they actually arrive. A run that finds nothing to summarize SHALL end quietly.

#### Scenario: A committed run chains into the next backlog

- **WHEN** a run commits and the remaining backlog still meets a threshold
- **THEN** a new run starts without waiting for a new message

#### Scenario: A failed run in an active chat starts nothing

- **WHEN** a run fails
- **AND** new chat messages arrived while it was running
- **THEN** no new run is started by the failing run's own path

#### Scenario: An empty backlog ends the run

- **WHEN** a run finds no messages after the committed cursor
- **THEN** the run ends without a commit and without an error

### Requirement: Consecutive failures throttle the start of the next run

The handler SHALL keep a count of consecutive failed runs alongside the time of the most recent
failure, per ownership pair. The count SHALL be incremented on a `failed` outcome and reset to zero
on a `completed` one; a run dropped as stale, ended because it lost its lease, ended by a stop request,
or ended because it found nothing to summarize is none of the two and SHALL leave the count alone. While a cooldown derived from that count has not elapsed since that pair's last
failure, a trigger for that pair SHALL NOT start a run, and the thresholds SHALL NOT be read. One pair's
failures SHALL NOT throttle another's. The cooldown SHALL grow with the failure count and SHALL be
capped, so a persistently failing consolidation settles into occasional retries rather than either a
loop or a permanent stall.

That count and its timestamp SHALL be held in storage shared by every instance, keyed by the ownership
pair, and SHALL be read in the same read that answers whether the pair is leased. Held per instance
they would bound model calls per instance rather than per pair, and the intended rate would be
multiplied by the number of instances — worst under exactly the provider outage the cooldown exists
for, which fails every instance at once. Being shared, the timestamp SHALL be a wall-clock time rather
than a process-local monotonic reading, which has no meaning in another process.

The throttle state SHALL be written by the same operation that releases the pair's lease, so that a
release cannot land without its outcome: a pair handed to the next instance with the gate wrongly open
is the retry loop the cooldown exists to close.

A pair's throttle state SHALL be discarded when its count resets, so the state that outlives every
run prunes itself on the healthy path. It SHALL NOT be capped or evicted on any other rule: an
eviction that can fire while a cooldown is still running would reopen the retry loop the cooldown
exists to close, and that is exactly what a provider outage — which fails every pair at once — would
trigger. The base and the cap SHALL be constants rather than settings: this is a safety valve on a
loop, not a tuning surface. A pair for which no failure has been recorded SHALL not be throttled.

#### Scenario: A failing consolidation with an active chat starts one run, not a stream

- **WHEN** consolidation fails on every attempt and chat messages keep arriving
- **THEN** each trigger inside the cooldown is declined
- **AND** the number of model calls stays bounded rather than growing with message volume

#### Scenario: The cooldown grows with consecutive failures

- **WHEN** consolidation fails repeatedly
- **THEN** each successive cooldown is longer than the previous one, up to the cap

#### Scenario: A success reopens the gate immediately and forgets the pair

- **WHEN** a run completes after one or more failures
- **THEN** the consecutive-failure count is reset to zero and the next trigger is not throttled
- **AND** no throttle state is retained for that ownership pair

#### Scenario: The cooldown expires and one attempt is allowed

- **WHEN** the cooldown for the current failure count has elapsed
- **AND** the thresholds and the single-flight guard allow it
- **THEN** a run starts

#### Scenario: One owner's failures do not throttle another

- **WHEN** runs fail repeatedly for one ownership pair and a trigger arrives for a different pair
- **THEN** the second pair is evaluated against the thresholds alone

#### Scenario: A cooldown at the cap is not cut short

- **WHEN** a pair's failure count has driven its cooldown to the cap
- **THEN** its count survives until the cooldown elapses and a run either completes or fails again
- **AND** no rule discards it while the cooldown is still running

#### Scenario: A fresh handler is not throttled

- **WHEN** a handler has recorded no failure
- **THEN** a trigger is evaluated against the thresholds alone

#### Scenario: A declined trigger says why

- **WHEN** a trigger is declined because the cooldown has not elapsed
- **THEN** an info line is logged naming the consecutive-failure count and the remaining cooldown
- **AND** the level is info rather than warning, because every persisted message asks and a warning
  would repeat for the whole cooldown over one unchanged fact

#### Scenario: A cooldown declines a trigger on every instance

- **WHEN** a run fails on one instance and a trigger for the same pair reaches another instance inside
  the cooldown
- **THEN** the second instance declines it too, and no model is called

#### Scenario: A restart does not reopen the gate

- **WHEN** the instance that recorded a pair's failures restarts inside that pair's cooldown
- **THEN** the next trigger for that pair is still declined until the cooldown elapses

#### Scenario: The release and the outcome are one write

- **WHEN** a run ends and its lease is released
- **THEN** the outcome's effect on the consecutive-failure count is applied by the same operation
- **AND** no state exists in which the pair is free but its outcome went unrecorded

#### Scenario: A pair with no recorded failure is not throttled

- **WHEN** no failure has been recorded for an ownership pair
- **THEN** a trigger is evaluated against the thresholds alone

### Requirement: The retired consolidation task type is never executed

The consolidation task type SHALL be retired: rows of that type left in a runtime database SHALL be
excluded from every worker's claim and swept to cancelled at executor startup. No component SHALL
create such a row, and the supervisor SHALL hold no memory lane, no consolidation scheduling, and no
consolidation timeout handling.

#### Scenario: A leftover row is retired at startup

- **WHEN** the executor starts with a consolidation row in a non-terminal status
- **THEN** the row is cancelled with a reason naming the retired type

#### Scenario: A leftover row is never claimed

- **WHEN** a consolidation row exists and any worker claims work
- **THEN** the row is not claimed, and no payload of it reaches the agent loop

#### Scenario: Persisting a message creates no row

- **WHEN** agent messages are persisted past the consolidation thresholds
- **THEN** no consolidation task row is created

### Requirement: A consolidation run holds a lease on its ownership pair

A run SHALL hold a lease keyed by its ownership pair for as long as it exists, and SHALL be the only
run for that pair anywhere in the deployment while it holds it. The lease SHALL live in the runtime
database: it is the substrate every instance of the deployment provably shares, and it is where the
committed summary the lease protects already lives, so the guard and the thing it guards cannot
outlive one another. The lease SHALL NOT live in an existing row belonging to another component — a
guard another tier may rewrite as a side effect of its own work is not a guard.

The lease SHALL stay keyed by the ownership pair even though the summary it protects is now keyed by
the pair and the chat. The two are deliberately different widths: the lease is what holds the
deployment to one background consolidation per `(owner_id, agent_id)`, which this change does not
widen, so an owner with ten open chats still costs at most one background model call at a time.
Because the guard is wider than the row, a trigger for chat B that arrives while chat A's run holds
the pair SHALL be dropped by single-flight as any other trigger for a leased pair is, thresholds
unread. Nothing is lost by that drop, only delayed: a run reads and commits its own chat alone, so
B's backlog is not consumed by A's run and stays past its threshold, and B's next persisted message
triggers it again. The chained re-check after a commit evaluates the finishing run's own scope
(`background_memory/handlers/consolidation.py`), so it is B's next message and not A's completion
that starts B — a chat that falls silent immediately after its dropped trigger keeps an unsummarized
backlog until it speaks again, which costs context length and no content.

Both chats of a pair SHALL likewise share one consecutive-failure count and one cooldown, because
that state lives on the lease row and the lease is the pair's. A run failing on chat A therefore
throttles chat B's first attempt. This is accepted rather than worked around: the throttle bounds
model calls during a failure that is almost never chat-specific — a provider outage fails every chat
of every pair at once — and the alternative is a counter per chat that multiplies the intended retry
rate by the number of conversations an owner keeps open.

Acquiring the lease SHALL be decided inside the write, not by a read followed by a write. Two
instances that both find the pair unleased SHALL leave exactly one holder; the loser SHALL drop its
trigger without calling the model.

The lease SHALL cover the run's whole follow-up chain rather than one delivery. A run advances by
re-delivery, and a lease released between deliveries would let a second instance take the pair
mid-run and start a duplicate summary of the same backlog. The holder SHALL renew the lease at the
start of every delivery, before the run performs any read or model call, and a renewal that finds the
lease is no longer this run's SHALL end the run before the model is called.

The lease SHALL carry an expiry. A lease with no expiry turns an instance that stopped while holding
one into a permanent stop for that pair, which is a worse outcome than the duplicate work the lease
prevents. The expiry SHALL exceed the longest step a run can take between two renewals — one model
call under its own deadline — and SHALL be derived from that deadline rather than declared beside it,
so that it stays correct when the deadline is tuned. It SHALL NOT be a configuration setting: it
bounds a failure this stage does not otherwise handle, and is not a tuning surface.

Losing a lease SHALL be neither a completed run nor a failed one. It SHALL end the run, SHALL be
recorded as its own outcome, and SHALL leave the pair's consecutive-failure count untouched: the run
that took the pair over did nothing wrong, and a shared count would otherwise throttle it for the run
it displaced.

A run that ends for any reason SHALL release its lease before the handler returns, so the pair is
free for the next trigger rather than for the expiry to free.

A lease SHALL be acquirable by the holder that already holds it. A holder names a process, not a
run, so a process that finds its own holder on a pair it is running nothing for is looking at a
lease its own crashed loop left behind — and it is the only party that can know that. A lease held
by a different holder SHALL NOT be acquirable before it expires.

A run whose backlog is deleted under it SHALL end without committing, and SHALL NOT be recorded as a
failure. It SHALL detect the deletion itself, from the **oldest** message it set out to summarize —
the head it stamped at its start, not its tail: a deletion that stops short of the newest row leaves
the tail in place, while the head is older by construction and goes first. Without the check a run
whose head is gone would summarize whatever remains and commit, handing the owner back a summary of
rows that no longer exist. The ending SHALL be its own outcome and SHALL leave the pair's
consecutive-failure count untouched: losing a backlog is not the run's fault, and a `failed` outcome
would throttle every other conversation of that pair, whose lease carries the count. The same check
is what stops a mid-batch run from reporting its now-unreachable pending fragment as a failure.

#### Scenario: A deleted backlog drops the run that set out from it

- **WHEN** a run is in flight for one conversation and the messages it set out to summarize are
  deleted
- **THEN** the run ends without calling the model and commits no summary
- **AND** its outcome is recorded as its own kind, and the pair's consecutive-failure count is
  unchanged, so the pair's other conversations are not throttled

#### Scenario: An instance takes back a lease its own crashed loop left

- **WHEN** an instance restarts its memory loop and a trigger arrives for a pair its previous loop
  held
- **THEN** it may acquire that pair without waiting for the expiry
- **AND** a lease held by a different instance is still not acquirable

#### Scenario: A second chat's trigger is delayed by the pair's lease, not lost

- **WHEN** a trigger arrives for one chat while a run for a different chat of the same pair holds the
  lease
- **THEN** the trigger is dropped without reading thresholds and without a model call
- **AND** the second chat's backlog is unchanged, so a later trigger for it starts its own run from
  its own committed cursor

#### Scenario: Only one of two instances starts a run

- **WHEN** two instances of the handler each receive a trigger for the same ownership pair whose
  backlog is past a threshold
- **THEN** exactly one acquires the lease and calls the model
- **AND** the other drops its trigger without calling the model

#### Scenario: A run keeps its pair across its follow-up chain

- **WHEN** a run has summarized one batch and requested re-delivery, and a trigger for the same pair
  reaches a second instance before the next delivery
- **THEN** the second instance finds the pair leased and starts nothing
- **AND** the original run resumes on its own re-delivery

#### Scenario: A lost lease stops the run before the model call

- **WHEN** a delivery's renewal finds the lease is no longer held by this run
- **THEN** the run ends without calling the model and without requesting re-delivery

#### Scenario: An expired lease is reclaimable

- **WHEN** a lease's expiry has passed and a trigger for that pair arrives
- **THEN** the trigger may acquire the lease and start a run

#### Scenario: The expiry outlives one model call

- **WHEN** the lease expiry is compared with the deadline a single model call may occupy
- **THEN** the expiry is the longer of the two, and is derived from that deadline rather than stated
  independently of it

#### Scenario: A lease loss is not counted as a failure

- **WHEN** a run ends because it lost its lease
- **THEN** its outcome is recorded as a lease loss
- **AND** the pair's consecutive-failure count is unchanged

#### Scenario: A finished run frees the pair immediately

- **WHEN** a run commits, fails, is dropped as stale, is stopped, or finds nothing to summarize
- **THEN** its lease is released before the handler returns, without waiting for the expiry
- **AND** an ending that reports no outcome still releases the lease

