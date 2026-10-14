# tool-confirmation Specification

## Purpose

When a tool call must not run until the owner allows it: which calls are gated and where that list
lives, how the request is addressed, how the run that asked is parked and resumed, how the decision
is taken exactly once across instances, and what the model is told about each outcome. The reply path
that resolves a request is specified in `inbound-submission`; the round that parks is organized as
`tool-execution` describes.

## Requirements

### Requirement: A tool the security config names does not run until the owner allows it

`authorize_tool` SHALL return `confirm_pending` for a tool whose name, or whose origin prefix, the
security config names, and the round SHALL obtain the owner's decision before the tool body runs. The
gate SHALL sit ahead of the body's dispatch, beside the other checks that return without running one,
so that no part of the effect — and no share of the process's tool-body budget — is spent on a call
that may be refused.

A call SHALL run only on a tool of the origin it was authorized on. Dispatch looks the name up again,
and a name that resolves to another origin there — a remote agent connected after the round looked it
up — was never put to that decision; such a call SHALL be refused rather than run.

The list SHALL live in `security_config/` beside the MCP and A2A allowlists, and SHALL have no second
definition: what needs a human is a security decision that changes through review, CI and image
signing rather than through a setting. A setting SHALL decide only whether the gate is active at all,
and while it is inactive every call SHALL authorize exactly as it did before this change.

Whether the gate is active SHALL be the owner's setting, resolved for the run's owner and agent each
time the run starts: at its first claim and again when a parked run resumes. The policy SHALL decide
from the value the run was given and SHALL read no setting of its own. An owner who stored nothing
SHALL get the instance value.

A tool that only delivers the run's output into the run's own conversation SHALL NOT be listed: the
owner would be asked whether to receive what they asked for.

#### Scenario: A named tool waits before its body runs

- **WHEN** the model calls a tool the security config names and the gate is active
- **THEN** the policy outcome is `confirm_pending`
- **AND** the tool body does not run before a decision is recorded

#### Scenario: An origin prefix covers a tool with a generated name

- **WHEN** the model calls a tool whose descriptor carries an origin the security config names
- **THEN** the call is gated, although the tool's name appears in no list

#### Scenario: A name that resolves to another origin at dispatch does not run

- **WHEN** a call was authorized while its name resolved to no gated origin, and dispatch resolves it to one
- **THEN** the tool body does not run, and the model is told to call it again

#### Scenario: A tool outside the list is unaffected

- **WHEN** the model calls a tool the security config does not name
- **THEN** the policy outcome is `allow` and the body runs as before

#### Scenario: Delivering a file to the run's own conversation is not gated

- **WHEN** the model calls `send_file` and the gate is active
- **THEN** the policy outcome is `allow`

#### Scenario: The inactive gate changes nothing

- **WHEN** neither the instance nor the owner has turned the gate on
- **THEN** every call authorizes exactly as it did before this change, and no request is recorded

#### Scenario: An owner who turned the gate off is not asked

- **WHEN** the instance keeps the gate on, the owner's override turns it off, and the model calls a
  tool the security config names
- **THEN** the tool body runs and no request is recorded

#### Scenario: An owner turns the gate on under an instance that keeps it off

- **WHEN** the instance keeps the gate off, the owner's override turns it on, and the model calls a
  tool the security config names
- **THEN** the policy outcome is `confirm_pending` and the run parks for the owner's decision

#### Scenario: A change applies at the next start

- **WHEN** the owner turns the gate off while a run of theirs waits for their decision, then approves
- **THEN** the approved call runs, and the next listed call of the resumed run runs without a question

### Requirement: One open request per scope

At most one confirmation request SHALL be open for a scope at a time. A gated call made while another
request is open for the same scope SHALL be refused without opening a second one.

A bare yes or no remains a valid answer, and it names no request: two open requests would make it
ambiguous, so a second request would be resolved by an answer given to the first.

#### Scenario: A second request while one is open is refused

- **WHEN** a gated call is made for a scope that already has an open request
- **THEN** no second request is recorded
- **AND** the call is refused and its body does not run

### Requirement: A request carries an id, and an answer may name it

Each request SHALL have an id, minted where the question is composed so that the text the owner sees
and the record the reply resolves name the same request. The question SHALL show the head of that id.

On a surface where the owner answers by typing — Dialogs — an answer SHALL be accepted in three
phrasings and no others: a bare yes or no, the same word followed by the id, and
`/confirm <id> <yes|no>`. On any other surface no typed text SHALL be an answer, in any phrasing: the
browser shows the question as a card and decides by its click alone, so a word typed beside the card
is addressed to the agent and is ordinary work. What counts as an id SHALL be no shorter than the
head the question shows: a shorter number is one the owner wrote for the agent, and reading it as an
id would take the message off the agent's path entirely. An answer naming an id SHALL be applied only to a
request whose id it names, and SHALL otherwise decide nothing: the run that asked has moved on, and
deciding whatever is open instead would allow a call the owner never saw.

An answer naming an id SHALL reach the reply path whether or not a request is open, because only that
path can tell the owner their request is gone; a bare yes or no SHALL be ordinary work when nothing is
open. A message that begins with `/confirm` and is none of the three phrasings SHALL reach the reply
path as well, which SHALL tell the owner the phrasing: queued as work, it would be answered by an agent
that cannot decide the request. On a surface without typed answers every message that begins with
`/confirm` SHALL reach the reply path for the same reason, which SHALL decide nothing and SHALL tell
the owner to answer on the question's card. Where no person is on the other end, no question is ever
asked, so the reply path SHALL say that nothing is waiting instead. A message carrying an attachment SHALL be ordinary work in every
phrasing: a decision never arrives with a file, and the reply path stores none.

A bare yes or no SHALL be an answer only while the question is the last thing said before it. Once
anything has been said in between — by the owner, by the agent on another task, or by a schedule — the
same word may be answering whatever was said last, and only an answer naming the id decides. The
supervisor reads that from the persisted conversation: the question's row carries its request beside
it, and the reply is written before it is read, so the row before the reply's own row is what the word
answers. What was said SHALL be counted back from that row and never from the newest one: a reply
stored before the question was published answers nothing the question asks, however late it is read,
and a message stored after the reply does not change what the reply answered. A reply whose own row is
not found SHALL be ordinary work.

The reply path SHALL decide only the request the reply was read as answering. If that request was
settled meanwhile and the run has asked again, the answer SHALL decide nothing: it was given before the
second question existed.

#### Scenario: An answer naming another request decides nothing

- **WHEN** the owner answers naming an id that is not the open request's
- **THEN** the open request stays undecided, and the owner is told that request is no longer waiting

#### Scenario: An answer naming a request is never queued as work

- **WHEN** a Dialogs message naming a request arrives while nothing is open for that scope
- **THEN** it is handled as a reply rather than submitted to the agent

#### Scenario: A confirm command the vocabulary does not accept is told the phrasing

- **WHEN** the owner sends `/confirm` in Dialogs with an id or a decision the vocabulary does not accept
- **THEN** it is not submitted to the agent, and the owner is told how an answer is phrased

#### Scenario: A typed answer in the browser is ordinary work

- **WHEN** a browser message right after the question is a bare yes or no, or that word followed by the
  request's id
- **THEN** it is submitted to the agent and the request stays open

#### Scenario: A confirm command in the browser is pointed at the card

- **WHEN** a browser message begins with `/confirm`, in any phrasing
- **THEN** it is not submitted to the agent, nothing is decided, and the owner is told to answer on the
  question's card

#### Scenario: A confirm command from a peer is told nothing is waiting

- **WHEN** a message beginning with `/confirm` arrives on a surface with no person on the other end
- **THEN** it is not submitted to the agent, nothing is decided, and the sender is told nothing is waiting

#### Scenario: A bare answer after the conversation moved on decides nothing

- **WHEN** a message was exchanged after the question, and a bare yes or no then arrives
- **THEN** it is ordinary work and the request stays open, while an answer naming the id still decides it

#### Scenario: A bare answer sent before the question decides nothing

- **WHEN** a bare yes or no is stored before the question is published, and is read after it
- **THEN** it is ordinary work and the request stays open

#### Scenario: A message after the answer does not undo it

- **WHEN** a bare yes or no follows the question, and another message is stored before the answer is read
- **THEN** the answer still decides the request

#### Scenario: An answer does not carry to the next question

- **WHEN** a bare answer was read as answering one request, and by the time it is recorded that request is
  settled and another is open
- **THEN** the open request stays undecided, and the owner is told the request is no longer waiting

### Requirement: A decision may arrive as a click

The browser transport SHALL expose a route by which a client submits one decision for the request it
names, and that route SHALL be the only way a decision is made on the browser surface. The route SHALL
resolve the caller's identity the way its other ingresses do, and SHALL refuse a caller it cannot
attribute.

The route SHALL NOT record the decision. It SHALL submit it on the ordinary inbound path, so that the
supervisor records it through the same reply path a typed answer takes: a transport that wrote the
decision itself would be a second place that authorizes tool calls, and transports may not import the
supervisor at all. The decision SHALL travel as the request id and the decision in fields of the
submitted message, not as text: on the browser surface no typed text is an answer, and a click that had
to pass as one would reopen exactly that path. No other ingress SHALL set those fields.

The submitted decision SHALL NOT be logged as a line in the conversation: what belongs there is the
question the owner answered and the runtime's reply to it.

#### Scenario: A click reaches the supervisor as the owner's answer

- **WHEN** a client posts a decision for a request
- **THEN** an inbound message carrying that request's id and that decision in its fields, with no text,
  is submitted under the caller's own scope, and nothing is written to the confirmation record by the
  transport

#### Scenario: The supervisor records a click

- **WHEN** such a message arrives while the request it names is open
- **THEN** the decision is recorded and the parked run is queued, as for a typed answer naming that id

#### Scenario: A decision the vocabulary does not accept submits nothing

- **WHEN** a client posts an unusable id or an unknown decision
- **THEN** the request is refused and no message is submitted

### Requirement: Only a human answers

A decision SHALL come from a message the owner sent. A path by which the runtime submits messages on
a skill's behalf SHALL refuse the confirmation vocabulary the way it already refuses commands: such a
message is addressed under a scope indistinguishable from the owner's own, so nothing downstream can
tell the two apart, and the gate exists precisely to keep the runtime from authorizing itself.

Which human is the conversation's question and not the gate's: an answer SHALL be accepted from any
message on the scope the request was opened for, and SHALL NOT be held to an authorization its own
request was never held to. Anyone who may write there may already put the same call to the agent as
ordinary work, so binding the answer to one participant would refuse a decision that same person
could make by asking again.

#### Scenario: A participant who did not start the task answers

- **WHEN** a request is open for a conversation and a message on that scope answers it
- **THEN** the decision is recorded whoever in that conversation sent it

#### Scenario: An injected reply is refused at the boundary

- **WHEN** a skill submits a bare yes or no through the message-injection path
- **THEN** the submission is refused and no decision is recorded

#### Scenario: A message carrying an attachment is not an answer

- **WHEN** a message whose text matches the vocabulary also carries an attachment
- **THEN** it is treated as ordinary work, so the attachment is stored rather than dropped

### Requirement: A run parked on a decision holds nothing

A round that reaches a gated call with no decision for it SHALL store the run's state on the
confirmation record and leave the graph, and the task record SHALL move to `waiting_confirmation` in
the same transaction that opens the request. A record parked with no request open would wait for an
answer nobody was asked for; a request opened without parking would hold the worker the parking is
for.

The stored state SHALL be everything the round needs to be replayed — the transcript, the names of
the active tools, the active model, effort and locality, the accumulated usage and trace, the round
index and the model response being acted on — and SHALL NOT include the task, its tool context, or
anything that is an object rather than data. Tool schemas SHALL be stored as the names they belong
to and asked for again on resume: a schema stored as data is the string an encoder falls back to,
and the round after the answer hands it to a provider.

No call of the parked round SHALL run before the decision. The assistant turn naming the round's
tool calls is written before any of them executes, and a transcript whose calls have no results is
one providers reject, so the round is parked whole.

What is given back is the worker, not the owner's lane. A parked record SHALL still count as work in
flight wherever the runtime asks that question, because the run is going to be queued again: a
durable job parked on a question keeps its owner's ceiling in force.

#### Scenario: A gated call with no decision parks the run

- **WHEN** a round reaches a gated call the owner has not decided
- **THEN** the task record reads `waiting_confirmation`, the request is open, and the run has left
  the graph without publishing a result

#### Scenario: Parking frees the owner's worker

- **WHEN** a run parks on a question
- **THEN** the instance claims the owner's next task while the question is still open

#### Scenario: A parked job still counts against its owner's ceiling

- **WHEN** a durable job is parked on a question and its owner's running roots have reached the
  ceiling
- **THEN** a further root of that owner is not claimed

#### Scenario: No call of a parked round runs

- **WHEN** a round contains a gated call with no decision beside calls that are not gated
- **THEN** no body of that round runs

### Requirement: A resumed run acts on the call it was parked on

A task queued again after its decision SHALL restore the stored state into the run it is starting,
and SHALL execute the round it was parked on rather than a new one. Restoring SHALL write into the
objects the run has already handed out — the transcript the tool context holds, the usage totals the
agent accumulates into, the active-tool set the tool surface reads — rather than replacing them.

The resumed run SHALL rebuild the round's tool surface from the stored names, so that the model is
handed the same surface it called from, including tools a skill widened it with before the run
parked, and the tools the round runs read that surface. A name the catalog no longer carries SHALL
yield no schema, and the call is then uncallable rather than callable against a schema that no
longer exists. The resumed run SHALL NOT assemble an opening transcript or recall memory for one:
the stored transcript is the run's, and the assembly would be discarded.

A decision SHALL be keyed by the round it was asked in together with the call it named: the call's
position in the round, the tool name and a hash of the arguments. Providers repeat tool-call ids
across rounds and leave them empty within one, so an id would carry an answer onto a call the owner
never saw, and two identical calls in one round would both run on the one answer; the position names
one call and no other.

A decision SHALL allow one call of one round of one attempt at the task. The round that acts on a
decision SHALL mark it spent, and a decision already spent SHALL read as no decision at all. A task
whose resumed run died is reclaimed and started over from its transcript; it reaches the same round
index and may repeat the same call, and an answer still standing there would run that call without
asking anyone. A round that cannot mark its decisions spent, or finds them already spent by another
attempt, SHALL NOT make the calls they allow. A round that cannot read its decisions SHALL NOT make
its gated calls and SHALL NOT put them to the owner again: a second question would store a second
answer to the same call beside the one that could not be read, and a later read could act on either.

A task that parked more than once SHALL be restored from the state of its **latest round**, decided
by the round index rather than by when the record was written: the write stamp is the clock of
whichever instance parked the run, and two instances whose clocks disagree would otherwise resume
the run at a round it has already left.

Restoring SHALL take whatever the stored state carries and keep the starting value of anything it
does not, rather than requiring the shape the current version writes. A rollout runs two versions at
once, so a run may be resumed from state written before a field of it existed; failing there would
end the task rather than the round.

Stored state that exists but cannot be read back SHALL end the run rather than start the task over.
Only the absence of a record SHALL mean the task never parked: a run that started the task over would
repeat every action of the rounds before the one it parked in.

Stored state SHALL be handed to a run exactly once, and SHALL be dropped in the same transaction that
reads it, after it has been read back successfully. A resumed run is reclaimable like any other, at
any round after the one it was parked on; restoring it a second time would rewind it into a round it
has left, run the call the owner approved for that round again without asking, and discard every
round since. Once the state is gone, such a reclaim is an ordinary interrupted run, and a gated call
the model makes again is put to the owner again.

#### Scenario: A reclaimed run is not rewound into a round it has left

- **WHEN** stored state has already been handed to a run
- **THEN** a later run of the same task is not given it, and starts from the transcript instead

#### Scenario: The resumed round runs the approved call

- **WHEN** the owner approves and the task is queued again
- **THEN** the run resumes at the parked round and the approved call's body runs

#### Scenario: The resumed round is handed its tool surface back

- **WHEN** a run is restored from stored state
- **THEN** the round's tools are the schemas the stored names stand for, taken from the catalog
- **AND** a tool that reads the surface sees those names as active

#### Scenario: A resumed run builds no opening transcript

- **WHEN** a parked run is resumed
- **THEN** the context assembler is asked for the tool context and not for a transcript

#### Scenario: An answer does not carry to a later attempt at the same round

- **WHEN** a run acts on a decision and its task is later started over and reaches that round again
- **THEN** the decision is spent, so the repeated call is put to the owner again

#### Scenario: An answer the round did not spend is not acted on

- **WHEN** the round cannot mark its decision spent, or another attempt spent it first
- **THEN** the call is not made, since the answer would still stand for a later attempt

#### Scenario: An answer the round could not read is not asked for again

- **WHEN** the round cannot read the decisions stored for it
- **THEN** its gated calls are not made
- **AND** the owner is not asked about them again

#### Scenario: An answer does not carry to another round

- **WHEN** a later round makes a call carrying the same tool-call id
- **THEN** that call has no decision and the run parks again

#### Scenario: An answer does not carry to another call

- **WHEN** the resumed round calls the same tool with different arguments
- **THEN** the stored decision does not apply to it

#### Scenario: An answer does not carry to a twin of the call

- **WHEN** the round makes the same call twice, with the same arguments and the same or an empty tool-call id
- **THEN** the answer allows the call it was given for, and the other is put to the owner on its own

#### Scenario: The later round is restored although its record was written with a lagging clock

- **WHEN** a task parked twice and the second record carries the earlier write stamp
- **THEN** the run is restored from the later round's state

#### Scenario: State written by another version resumes the run

- **WHEN** the stored state carries no value for a field the current version writes
- **THEN** the run resumes with the starting value of that field and the rest restored

#### Scenario: Unreadable state ends the run rather than repeating it

- **WHEN** a parked task's stored state is present but cannot be read back
- **THEN** the run ends with that failure, and the task is not started over

### Requirement: What the parked round reached outside its own state comes back with it

A parked round may have been about to act on things that hang off the run rather than on its state,
and those SHALL be put back before the decision is acted on.

The task's workspace SHALL outlive a parked run and SHALL be handed to the run that resumes it: the
call the owner is being asked about may name a file written there, and a workspace removed with the
parked run would make the approval act on nothing. The path SHALL travel on the task record rather
than inside the stored state, because the resumed run needs the directory before it builds anything
that could read that state. It SHALL be used only while it is still a directory under this instance's
task root: the path crosses a database, another instance's directory is simply absent here, and the
resumed run then gets a fresh workspace as it did before the path was kept at all.

A sandbox session the parked run held SHALL be reattached to the resumed run's tool context, so that
an approved call is not refused for the absence of a session the owner never dismissed.
Reattachment is best effort: a session reaped while the owner was deciding SHALL leave the round to
tell the model, which reconnects, rather than end the task. The session put back SHALL be the one the
run held: its identity is stored with the state, and a reattachment that answers with another session
— an instance that never held it is given a fresh sandbox, without the files the call is about — SHALL
be refused the same way, so that the approved call is not made in a sandbox the model never wrote to.

What the run's earlier calls came to SHALL be stored with its state and restored with it: the outcome
ledger the model is shown and the failure streaks the repeat guard refuses a call on. The resumed
round SHALL be preflighted as it was when it parked — a fresh guard would put to the owner a call the
parked round refused without asking — and the model SHALL keep seeing outcomes whose results
compaction has already cut from the transcript.

The files a detached run has stored for delivery with its final answer SHALL be stored with its state
and restored with it: they are delivered once, at the end, and a park between the storing and the end
would otherwise lose every file stored before it.

The skill instructions in force — the pages the run has read, their recency, and which of them are
projected into the prompt — SHALL be stored with its state and restored with it: the next skill read
rebuilds the prompt section from them, and an empty set would drop every skill read before the park.
The checks that read those instructions inside the round — the final-response language check and
compaction — SHALL see them from the resumed round on, not only after the next skill read.

#### Scenario: The approved call finds the file it was approved to act on

- **WHEN** a run parks on a call naming a file in its task workspace and the owner approves
- **THEN** the resumed run is given the same workspace and the call reads that file

#### Scenario: A workspace left by another instance is not reused

- **WHEN** a parked record names a workspace that is not under this instance's task root
- **THEN** the resumed run builds a fresh workspace instead

#### Scenario: The sandbox session comes back with the run

- **WHEN** a run that held a sandbox session is resumed
- **THEN** the session is reattached to the resumed run's tool context before the call runs

#### Scenario: Another instance's sandbox is not handed to the approved call

- **WHEN** a run that held a sandbox session is resumed where reattachment answers with a different session
- **THEN** the resumed run holds no session, and the round tells the model to reconnect

#### Scenario: Files stored for the final delivery survive a park

- **WHEN** a detached run stored a file for delivery with its answer, parked afterwards, and was resumed
- **THEN** the resumed run still delivers that file with its answer

#### Scenario: An earlier skill's instructions survive a park

- **WHEN** a run read skill A, parked, was resumed and then read skill B
- **THEN** the prompt carries the instructions of both

#### Scenario: The resumed round's checks read the restored instructions

- **WHEN** a run that had read a skill parks and is resumed
- **THEN** the language check and compaction read that skill's instructions before any further skill read

#### Scenario: Earlier failures still count in the resumed round

- **WHEN** a round parks while the repeat guard refuses one of its calls, and the owner approves another
- **THEN** the resumed round refuses that call again without asking, and the ledger lists the earlier
  outcomes

### Requirement: A decision is taken once, by the first reply

The decision SHALL be written by a conditional statement that applies only while the request is open
**and the run it belongs to is neither stopping nor over**, and the caller SHALL learn from that write
whether it applied. A second reply SHALL find the request already decided and SHALL NOT change it.

The decision SHALL NOT be inferred from a record read beforehand: several instances may read the same
open request before any of them writes, and a stop may land between such a read and the write. A
request closed because its run is gone SHALL be closed rather than decided — expiry is not a decision,
and the statement that records decisions refuses to write one for such a run at all.

A decision recorded while a run was withdrawing its question SHALL be honoured rather than discarded:
the withdrawal applies to no row, and the record stays parked for that decision to be read.

#### Scenario: Two replies decide once

- **WHEN** two replies to the same open request are processed concurrently
- **THEN** the write applies for exactly one of them
- **AND** the other learns that its write applied to no row, and the recorded decision is unchanged

#### Scenario: A reply after expiry does not decide

- **WHEN** a reply arrives for a request already closed unanswered
- **THEN** no decision is recorded, and the message is treated as ordinary work like any other text
  arriving with nothing open

#### Scenario: A reply whose run will not act on it records no agreement

- **WHEN** a reply arrives for a request whose task has ended, is gone, or has a stop recorded
- **THEN** the request is closed without a decision, and the owner is told the action did not happen

#### Scenario: An answer that lands as the waiter gives up still decides

- **WHEN** a decision is recorded between a run's last read and its own withdrawing write
- **THEN** the withdrawal applies to no row and the record stays parked on that decision

#### Scenario: The waiting run reads the decision from the record

- **WHEN** a decision is recorded by another process
- **THEN** the run started for that task observes it and proceeds accordingly, holding no reference
  to the component that recorded it

### Requirement: A question is open exactly while its run is parked

A recorded decision SHALL queue the task it belongs to. Where that write does not apply — the
instance died, or the record was moved by something else — one supervisor sweep SHALL hold both
halves of that correspondence, so that neither half depends on every ending of a run remembering it.

The sweep SHALL queue any parked record that has a stored run state and no open request, so that a
parked record is never waiting on an instance that is gone. A parked record with an open request
SHALL NOT be queued: the owner may still answer it. A record in `waiting_confirmation` with no
stored run state SHALL NOT be queued either — the a2a task lifecycle writes that status for its own
input_required, and it is not this sweep's to move.

The sweep SHALL close any open request whose task is no longer parked on it, because an open request
refuses every later gated call for its scope. Only a run that reaches the confirmation step withdraws
its own question, and a run ended by a stop, a cancel or a restart never reaches it; a question left
behind by one of those would end confirmations for that chat until someone noticed. A request whose
task is parked and not stopping SHALL be left open, whether or not an instance is running it.

The same sweep SHALL drop the stored run state of a request whose task has reached a terminal status,
because that state is a whole round's transcript and nothing will resume it. The request itself SHALL
be kept: what the owner was asked, and what they answered, is the record of the decision.

#### Scenario: The sweep queues a record whose question was withdrawn

- **WHEN** a parked record's request is closed with no decision recorded
- **THEN** the sweep queues that task

#### Scenario: The sweep passes over an open question

- **WHEN** a parked record's request is still open
- **THEN** the sweep leaves it parked

#### Scenario: The sweep passes over a record that never parked here

- **WHEN** a record reads `waiting_confirmation` and carries no stored run state
- **THEN** the sweep leaves it alone

#### Scenario: The sweep closes a question nobody is parked on

- **WHEN** a request is open and its task is no longer parked on it
- **THEN** the sweep closes the request, and the next gated call for that scope may open its own

#### Scenario: A stopping run's question is closed

- **WHEN** a request is open for a task that has a stop recorded
- **THEN** the sweep closes the request rather than leaving it for an answer nothing would act on

#### Scenario: The stored run state of a finished task is dropped

- **WHEN** a request belongs to a task that has reached a terminal status
- **THEN** the sweep clears its stored run state and leaves the request row itself in place

### Requirement: The wait is unbounded, and a stop ends it

A question SHALL have no deadline: the run costs nothing while it waits, and an owner who answers
tomorrow gets the call they asked for. Expiry SHALL remain only as the outcome of a question closed
without an answer.

A run that could not deliver its question SHALL close the request it opened and put its record back
under itself, so the round proceeds rather than parking on a question nobody was shown. Where either
write does not apply or fails — an answer landed first, a stop reached the record, the owner's worker
slot has since been taken, or the store did not answer — the round SHALL NOT run, and the record SHALL
be left to what moves it: the queued answer, the stop, or the sweep. A close that failed leaves the
question open on a parked record, which is the question an instance that died between parking and
publishing leaves behind, and the status report reaches it the same way. Putting the record back SHALL
hand the round back to the run in the same write: the round's stored state SHALL be dropped and its
decisions spent, because the run goes on to act on them, and a reclaim would otherwise rewind into that
round or run a call it approved again. The run SHALL keep the clock of its attempt, which a record
queued for another attempt does not.

A stop requested for a parked task SHALL be recorded and the record SHALL be queued, so that the run
started for it reads the stop and ends without executing the call. A restart SHALL cancel a parked
record as it cancels a queued one: neither has a run of its own to observe the stop.

Because the wait has no deadline, the owner SHALL be able to see the open request without having
received its question: the status report SHALL name the request open for the asking scope — its
tool, the shown head of its id, and the task it belongs to. A parked record appears in neither the
pending nor the running list, so an instance that died between parking and publishing would
otherwise leave a question nobody can answer and a scope that refuses every later gated call.

#### Scenario: An unanswered question stays open

- **WHEN** a question is published and no answer arrives
- **THEN** the request stays open and the record stays parked

#### Scenario: An undelivered question is withdrawn

- **WHEN** publishing the question fails
- **THEN** the request is closed, the record is running again, and the round continues without the
  gated call

#### Scenario: A question that could not be withdrawn keeps the round waiting

- **WHEN** publishing the question fails and closing its request fails too
- **THEN** the round does not run, and the request stays open on the parked record

#### Scenario: A reclaim does not replay a round whose question was withdrawn

- **WHEN** a round with an approved call withdraws its question about another, and its run later dies
- **THEN** the task is started over from its transcript rather than that round, and the approved call
  is put to the owner again if the model repeats it

#### Scenario: A stop reaches a parked run

- **WHEN** the owner stops a task that is waiting for their answer, or the job it belongs to
- **THEN** the stop is recorded, the record is queued, and the run that takes it ends without
  running the call
- **AND** the workspace the parked run kept is removed with that run

#### Scenario: The status report names the question the owner has not answered

- **WHEN** the owner asks for status while a request is open for their scope
- **THEN** the report names its tool, the shown head of its id, and its task

### Requirement: A surface with nobody behind it is not asked

A gated call SHALL open no request when the run's scope is on a surface no person answers on — a
peer agent's, or a skill's. Parking such a run would wait on a reply that will never be sent, and
the wait has no deadline to end it. The round SHALL proceed instead, and the gated call SHALL come
back as a call nobody was asked about, exactly as it does when a question cannot be delivered.

#### Scenario: A peer agent's task is refused rather than parked

- **WHEN** a gated call is reached by a run whose scope is on the a2a surface
- **THEN** no request is opened, the record stays running, and the call is refused as unasked

### Requirement: The question reaches the owner as a message, not as progress

The question SHALL be published as an owner-visible message rather than as a progress line: progress
may be suppressed on the way to the user and is not kept in the conversation, and a question the
owner never sees is one nothing will ever answer.

The question SHALL name the tool and SHALL describe the call through the same redaction the logs use,
truncated. Raw arguments SHALL NOT be placed in the message or in the audit record.

The question's text SHALL tell the owner how to answer by typing only on a surface where typing
answers. Elsewhere the text SHALL carry no such instruction: a word typed there reaches the agent, which
cannot decide the request.

The same question SHALL travel in fields beside its text: the request id, the task, the tool, and the
truncated argument preview with whether it was truncated. A transport that renders only text SHALL be
unaffected by their presence, and a client that draws the call as something answerable SHALL NOT have
to parse the sentence to find what it is answering. The fields SHALL carry the same preview the text
does, never the raw call.

#### Scenario: The question is delivered as an owner-visible message

- **WHEN** a gated call opens a request
- **THEN** the question is published as a non-progress message addressed to the request's scope

#### Scenario: A client is given the call in fields

- **WHEN** a gated call opens a request
- **THEN** the published message carries the request id, tool and argument preview beside the text,
  and a text-only transport delivers the question unchanged

#### Scenario: Only a surface answered by typing is told what to type

- **WHEN** a gated call opens a request on the browser surface
- **THEN** the question's text names the tool, the head of the id and the preview, and carries no
  instruction to type an answer, while the same call in Dialogs is told how to answer

#### Scenario: Redacted arguments describe the call

- **WHEN** a gated call carries an argument the tool's metadata declares redacted
- **THEN** its value appears neither in the question nor in the audit record

### Requirement: Every request records a paired outcome

A request SHALL record that it was opened and, exactly once, how it ended — allowed, denied, or
closed unanswered. A request recorded as opened with no recorded outcome SHALL NOT be a reachable
state, so that a stuck decision is visible in the audit trail rather than inferred from its absence.

A gated call refused because the scope already had an open request opens nothing, so it has no paired
record; it is the earlier request that carries the pair.

A decided request SHALL record its outcome where the decision is recorded, by the write that applied
and naming the request. The run that asked is not the place: it reads the decision only when it
resumes, may never reach that round again — stopped after one of two questions was answered — and
would then leave the first request opened with no outcome.

Every record of a request — its opening and its outcome — SHALL be written in the transaction that
makes the change it records, and a change whose record cannot be written SHALL NOT apply. A record
written after the change fails on its own and would leave an approval the run acts on with no outcome
anyone can see; a refused change is one the owner or the sweep simply makes again.

A question closed by the sweep rather than by its own run SHALL record its outcome there. That is the
ending of every question an owner abandons — a stop, a cancel or a restart — and the run that opened
it is gone by then, so the sweep is the last place able to record it. Closing a question anywhere
else, without that record, SHALL NOT be a second way to end one.

#### Scenario: A question closed by the sweep records its outcome

- **WHEN** the sweep closes a question because its run is no longer parked
- **THEN** the audit trail holds an outcome record naming that request

#### Scenario: A resolved request records its outcome

- **WHEN** a request is allowed or denied
- **THEN** the audit trail holds both the opening record and one outcome record naming that request
  and the decision, written as the decision is recorded

#### Scenario: A decision the run never acts on still has its outcome

- **WHEN** the owner answers the first of a round's two questions and the task is stopped on the second
- **THEN** the first request's outcome is in the audit trail, and the second's is the closure the sweep records

#### Scenario: An expired request records its expiry

- **WHEN** a request is closed with no decision
- **THEN** the audit trail holds an outcome record naming it

#### Scenario: A change whose record cannot be written does not apply

- **WHEN** a request is opened, decided, withdrawn or closed by the sweep while its record cannot be written
- **THEN** the request is left as it was, and the store failure reaches the caller

### Requirement: A refusal is an outcome, not a tool failure

A call the owner denied, or that expired undecided, SHALL be reported to the model as an outcome
carrying its own marker, and SHALL NOT be classified as a tool execution failure. It SHALL NOT feed
the repeat guard's failure streaks and SHALL NOT be counted in the round's error count.

Nothing failed: the tool was not run. Counting a refusal as a failure would block the tool for the
rest of the task after two refusals, which turns the owner's decision into a malfunction.

#### Scenario: A denied call is not a tool failure

- **WHEN** the owner denies a gated call
- **THEN** the model receives a result naming the denial
- **AND** the round's error count is unchanged and the repeat guard records no failure

#### Scenario: The model can act after a denial

- **WHEN** a gated call is denied
- **THEN** the run continues with the remaining rounds rather than ending on the refusal

### Requirement: A client that returns finds the open request in the history

The browser history SHALL carry, on the question's row, the request that row published —
`confirmation_id`, `task_id`, `tool`, `arguments_preview`, `arguments_truncated` — and `open`, which
SHALL be true exactly while that request is the scope's pending one. A client SHALL NOT be left to
parse the question's text, and a row whose request is decided, withdrawn or closed SHALL say so.

#### Scenario: The question is still open when the client comes back

- **WHEN** a client reads the history of a conversation whose run is parked on a request
- **THEN** the question's row carries the request with `open` true

#### Scenario: The question was answered while the client was away

- **WHEN** a client reads the history after the request was decided
- **THEN** the question's row carries the request with `open` false
