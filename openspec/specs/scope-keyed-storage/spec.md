# scope-keyed-storage Specification

## Purpose

How the runtime database records work on an owner's behalf: rows are keyed by the ownership axes of
`OwnerScope` rather than by a glued conversation key, axis columns are filled from a scope only,
scoped reads cannot be widened by omission, and the context summary is identified by its ownership
axes. A message and a summary carry the chat beside the owner and the agent, so two conversations of
one owner share no history. Migrations `0008` and `0016` backfill existing rows without losing
reachability, parking a row whose conversation cannot be read rather than deleting it.
## Requirements
### Requirement: Stored rows carry ownership axes, not a glued conversation key

Every runtime table that records work on an owner's behalf SHALL carry that owner's identity as
explicit columns taken from `OwnerScope`, and SHALL NOT carry a `conversation_id` column or any
other key that glues two axes into one string. `messages` and `context_summaries` SHALL carry
`owner_id`, `agent_id` and `chat_id`. `tasks` SHALL carry `owner_id` and `agent_id`.
`task_episodes` and `tool_invocations` SHALL carry `owner_id`, `agent_id`, `surface_id` and
`chat_id`. The `conversations` table SHALL NOT exist.

The chat is a key column on the two dialogue shapes because a conversation is the unit an owner
reads back and clears. While it lived inside `metadata_json` it reached no SQL predicate, and
`transport/dialogs/transport.py:456` mints the configured owner and agent for every peer of a
deployment — so peers differed only in an axis nothing read, and one person's messages were in
another person's `## Recent chat`. The same missing predicate is why one agent could not hold two
conversations at once.

`tasks` keeps the nullable `chat_id` it has had since `0001_baseline.py:66`, and that column SHALL
remain a routing field rather than an ownership predicate: the one-running-root rule counts an
owner's runs, not a conversation's, and concurrency stays at one root run per `(owner, agent)`.

`memory` and `scratchpad_blocks` SHALL stay keyed by the ownership pair. Durable memory and the
scratchpad are what an agent knows about a person across every conversation it has with them;
narrowing them by chat would make the agent forget the person on each new thread, which is the
opposite of what a conversation key is for.

The surface SHALL NOT join the chat in the message and summary key. Three axes are the whole
predicate, which puts one obligation on the chat id policy
(`ouroboros/contracts/chat_id_policy.py`): a chat id SHALL name at most one conversation within one
owner, whichever surface minted it. The policy already keeps the synthetic allocators apart — the
skill bridge's window against `A2A_CHAT_ID_CEILING` — and a surface that could mint an id another
surface already uses for a different conversation would merge two conversations under one owner,
which is the failure this change exists to remove.

#### Scenario: No table keeps the glued key

- **WHEN** the runtime schema is inspected after migration
- **THEN** no table has a `conversation_id` column
- **AND** no table named `conversations` exists

#### Scenario: Ownership axes are present where work is recorded

- **WHEN** a message, a context summary, a task, a task episode or a tool invocation is stored
- **THEN** the stored row carries `owner_id` and `agent_id` equal to the scope the work ran under
- **AND** a message or a summary additionally carries that scope's `chat_id`
- **AND** an episode or an invocation additionally carries that scope's `surface_id` and `chat_id`

#### Scenario: Two conversations of one owner do not share a history

- **WHEN** two chats under one owner and agent each store messages, and each commits a summary
- **THEN** a read under one chat's scope returns that chat's messages and that chat's summary only
- **AND** neither chat's rows appear in the other's history, tail, or `chat_history` output

#### Scenario: Durable memory keeps the wider key

- **WHEN** a `memory` record or a `scratchpad_blocks` row is written under one chat's scope and read
  back under another chat's scope of the same owner and agent
- **THEN** the row is returned
- **AND** neither table carries a `chat_id` column

### Requirement: Axis columns are filled from a scope and from nothing else

Code that writes an axis column SHALL take an `OwnerScope` and read the value from it. It SHALL
NOT derive an axis from an adjacent field that carries a differently typed or differently
normalized spelling of the same idea — in particular not from a task dict's `chat_id`, whose type
is `int | str | None`, and not from a task's `source`, which is a normalized task source whose
dictionary is not the surface dictionary.

This SHALL hold for the ownership columns of a task row on **every** path that can create one,
including the path that writes a placeholder row before the work is enqueued. Reading the configured
owner and agent directly, rather than from the scope of the work being recorded, SHALL NOT be
permitted on any such path — the two agree only while both are instance constants, and the row
outlives that.

A re-queue that updates an existing row SHALL carry the ownership columns in its update. A row whose
ownership columns are written by one path while its stored scope is written by another SHALL be
treated as a defect, because the two can then disagree without any statement failing.

The chat column of a `messages` or a `context_summaries` row SHALL come from `scope.chat_id` and
from nothing else — in particular not from `append_chat_message`'s sibling `chat_id` parameter,
which is typed `Any` and exists because it is written into `metadata`. Where both survive, the
sibling SHALL be derived from the same scope at the call site, as `supervisor/message_bus.py:475`
already does with `routed_chat_id(scope)`. A call that passes a chat disagreeing with its scope is a
defect of the same kind as the row above: the row would be filed under one conversation and rendered
as another, and no statement would fail.

The column SHALL be the one value anything decides on. `metadata_json`'s copy of the chat id SHALL
NOT become a second source of truth: the progress filter at `sql_runtime_store.py:1479`, the dedup
guard at `chat_store.py:101`, and any later reader SHALL read the column. The metadata copy survives
as what rows written before the column carry, and as display provenance, never as a predicate.

#### Scenario: The storage write surface accepts a scope

- **WHEN** a chat message, an episode or an invocation is persisted
- **THEN** the call carries an `OwnerScope`
- **AND** every value written to an axis column comes from that scope, not from another parameter
  that happens to name the same idea

#### Scenario: A normalized task source cannot reach the surface column

- **WHEN** work runs on the skill-bridge surface, whose normalized task source is `internal`
- **THEN** the stored `surface_id` is `skill-bridge`

#### Scenario: A task row created before enqueue carries the work's ownership

- **WHEN** a placeholder task row is created for work that has not been enqueued yet
- **THEN** its ownership columns come from the scope of the work it records

#### Scenario: A re-queued row keeps its ownership columns in step with its scope

- **WHEN** an existing task row is re-queued rather than created
- **THEN** the ownership columns are part of the update
- **AND** they agree with the scope stored on the same row

#### Scenario: A message's chat column comes from its scope

- **WHEN** a chat message is persisted
- **THEN** the row's `chat_id` column equals `scope.chat_id`
- **AND** the chat its `metadata` names is derived from the same scope rather than passed
  independently

#### Scenario: Nothing decides on the metadata copy of the chat

- **WHEN** the paths that filter, group, or compare a message by its conversation are inspected
- **THEN** each reads the `chat_id` column
- **AND** none reads `metadata_json`'s copy to make the decision

### Requirement: A scoped read cannot be widened by omission

The store SHALL express each ownership predicate as one named constant, so that no statement can
filter on one axis and forget another. A read that is scoped SHALL require a complete `OwnerScope`
and SHALL NOT accept an axis as an optional or defaulted string. An empty or missing value SHALL
NOT cause a scoped read to return rows outside the scope.

Three widths now exist and each SHALL be exactly one named constant beside `_OWNED` and `_SCOPED`
(`ouroboros/storage/sql_runtime_store.py:51-54`): the ownership pair, the pair with the chat, and
all four axes. `_SCOPED` SHALL NOT be reused for `messages`, which has no `surface_id` column;
adding one to reuse a constant would key the row by an axis nothing needs. Every `messages` and
`context_summaries` statement — `list_messages` (1407), `list_chat_history_messages` (1501),
`get_message_by_dedup_key` (1727), the summary read (1908), upsert (1859) and compare-and-swap
(2192) — SHALL carry the pair-with-chat constant unless it is a named exception below.

A read surface that accepts a scope SHALL be called with one wherever the caller holds one. A caller
that omits an optional scope reads across every owner, and the widening is invisible at the call
site — which is how one owner's task descriptions and results reach another owner's model prompt.
Every caller that deliberately reads across owners SHALL be a named exception with its reason
recorded, and SHALL NOT be a path that feeds a prompt or an owner-facing surface.

Cross-chat is not cross-owner, so the rule above does not cover it and it needs its own. A read that
spans every chat of one pair SHALL be a surface of its own, with the widening in its name and its
reason recorded, and SHALL NOT be a scoped read that happens to be handed a scope whose chat is not
a conversation. There is no such thing as an accidental exception here: the crash-replay sweep at
`transport/journal.py:85` reads messages under `stored_pair_identity(...).as_scope()`, whose chat
axis is the literal `ServiceJob` name, so the first narrowed read matches zero rows for every owner
forever while still logging a healthy "resubmitted 0" — a silently dropped inbound message on every
crash. `new_core/identity/service_scope.py:66` SHALL stop promising that "nothing reads either" for
its surface and chat axes, because after this change one of them is read.

A cross-chat read MAY serve maintenance — the sweep, a pair-wide delete, a per-pair export — and
SHALL NOT feed a prompt, a history surface, or the `chat_history` tool, which is where one
conversation showing another begins.

#### Scenario: An incomplete scope cannot be expressed

- **WHEN** a scoped read surface is called
- **THEN** it takes an `OwnerScope` whose axes are all non-empty by construction
- **AND** there is no parameter whose empty value removes a predicate

#### Scenario: One constant carries the ownership predicate

- **WHEN** the SQL of the scoped reads is inspected
- **THEN** the owner-and-agent predicate appears as a single shared constant rather than as
  repeated inline text
- **AND** the pair-with-chat predicate the message and summary statements carry is one further
  constant, not the pair constant with a clause appended per statement

#### Scenario: No prompt is built from an unscoped read

- **WHEN** the paths that assemble prompt content or an owner-facing report are inspected
- **THEN** each read they perform carries the owner's scope
- **AND** any deliberate cross-owner read is named, justified, and feeds neither

#### Scenario: The journal sweep reads across chats by name

- **WHEN** the crash-replay sweep looks for rows a transport persisted but never handed over
- **THEN** it calls a read surface whose name says it spans the pair's conversations, passing the
  owner and agent rather than a scope whose chat is a job name
- **AND** it finds an unprocessed message stored under any chat of that pair

#### Scenario: A service identity cannot reach a chat-scoped statement

- **WHEN** a widened `ServiceScope` is passed to a statement that filters on the chat
- **THEN** it matches nothing, and the widening conversion no longer claims that its chat axis is
  unread

### Requirement: The summary of an instance is keyed by its ownership axes

The committed context summary SHALL be identified by `owner_id`, `agent_id` and `chat_id`. No
constant row id SHALL identify it: the row's own id SHALL be a surrogate, and uniqueness SHALL be
carried by the ownership axes. Reading the latest summary SHALL filter on those axes; committing one
SHALL match on them and SHALL resolve its conflict on them — `upsert_context_summary`'s conflict
target at `sql_runtime_store.py:1882` and the compare-and-swap at `2192` — so that work under one
agent, or under one conversation, can neither read nor overwrite the summary of another, and so that
a second agent, and a second chat, can commit at all.

The consolidation lease SHALL stay keyed by the ownership pair. It is deliberately wider than the
row it guards: a lease that covers more than it must serializes more, never less, so single-flight
per pair still admits one committing run at a time across that pair's conversations and
`upsert_context_summary` keeps the single-writer premise its comment states. Narrowing the lease
with the summary would also mean one conversation's clear deleting the lease of a run summarizing
another — which is why a chat-scoped deletion stops deleting `consolidation_leases`
(`sql_runtime_store.py:1851`) at all.

#### Scenario: Two agents keep separate summaries

- **WHEN** summaries are committed under two different `agent_id` values for one owner
- **THEN** both commits succeed
- **AND** each read returns the summary committed under its own axes
- **AND** neither commit overwrites the other's row

#### Scenario: Two conversations keep separate summaries

- **WHEN** summaries are committed under two different `chat_id` values for one owner and agent
- **THEN** both commits succeed, and each read returns the summary committed under its own chat
- **AND** the second commit is neither absorbed by the first's conflict target nor refused by it

#### Scenario: Deleting one owner's history spares another agent's summary

- **WHEN** conversation history is cleared under one owner and agent
- **THEN** a summary committed under a different agent is still present

#### Scenario: Clearing one conversation spares the other's summary and the pair's lease

- **WHEN** history is cleared under one chat of an owner and agent
- **THEN** the summary committed under another chat of that pair is still present
- **AND** the pair's `consolidation_leases` row is untouched, so a run summarizing the other
  conversation is not cancelled by the clear

### Requirement: The migration is one revision, and it refuses a schema it does not recognise

The rekey SHALL be delivered as revision `0008`, the same revision that carries the core-memory
agent axis, because no environment has applied it. Before any DDL the revision SHALL verify that
the schema it is about to change is the one `0007` leaves behind, and SHALL fail loudly rather
than apply part of itself.

#### Scenario: Head stays at 0008

- **WHEN** migrations are run to head on an empty database
- **THEN** the recorded revision is `0008`
- **AND** the resulting schema carries the ownership axes and no `conversation_id` column

#### Scenario: An unrecognised schema stops the migration

- **WHEN** the revision runs against a database whose tables do not match what `0007` produces
- **THEN** it raises before issuing any DDL

### Requirement: Backfilled rows keep their reachability, and none is deleted

The backfill SHALL set `owner_id` and `agent_id` to the instance's configured owner and agent. For
`task_episodes` and `tool_invocations` it SHALL derive `surface_id` and `chat_id` by splitting the
stored key on its first separator, accepting the left part only when it names a known surface,
including the read-only legacy spellings. A row whose key does not name a known surface SHALL be
parked — surface set to the service surface, chat set to empty — so that no minted scope can match
it, and SHALL NOT be deleted. The number of parked rows SHALL be reported.

#### Scenario: A parseable legacy key becomes axes

- **WHEN** a row holds the key `dialogs:123`
- **THEN** its `surface_id` becomes `dialogs` and its `chat_id` becomes `123`

#### Scenario: An unparseable legacy key is parked, not dropped

- **WHEN** a row holds a key that names no known surface, such as a bare session id
- **THEN** the row still exists after migration
- **AND** its axes cannot match any scope a transport can mint

### Requirement: A dropped field does not resurrect from a stored payload

Row mappers SHALL NOT reconstruct `conversation_id` onto a model from a stored payload written
before this change. Where a model permits extra fields and is rebuilt from a payload, the retired
key SHALL be removed explicitly.

#### Scenario: A legacy payload yields a model without the retired key

- **WHEN** a row written before the migration is read back
- **THEN** the resulting model has no `conversation_id` attribute

### Requirement: A stored message's dedup identity is a column that carries its owner

A `messages` row SHALL carry its dedup identity in its own non-empty `dedup_key` column, and that
identity SHALL be derived from the row's `owner_id`, `agent_id` and `chat_id` together with the
values that distinguish one client message from another. A unique index on that column SHALL be what
refuses a duplicate.

The row's primary key SHALL NOT be the identity. It SHALL be a surrogate that no code derives from
the message's content or scope, so that the identity can be re-scoped later while the row keeps its
address — the shape the committed context summary already has, and for the same reason.

The identity SHALL be exactly as wide as the row's key: `messages` carries `owner_id`, `agent_id`
and `chat_id`, so an identity narrower than that triple lets two owners — or two conversations of
one owner — share a row, while one wider than it names an axis the row does not store.

This settles a width the code already had and the previous wording denied. `_inner_message_key`
(`ouroboros/storage/chat_store.py:25`) has always folded the chat into the inner value, so two
conversations of one owner already receive different `dedup_key` values for one client message id;
what changes is the statement, and the docstring rule at `chat_store.py:58` SHALL be restated at the
settled width rather than at the pair.

The two-stage fold SHALL stay exactly where it is. `message_dedup_key` SHALL keep folding the
ownership pair alone over the inner value: the chat is already inside that inner value, and folding
it a second time at the outer stage would change every key the application derives while the rows
`0010` lifted keep the keys it computed — the derivation and the stored values would part, and every
redelivered message would land twice. An implementer reaching for the outer fold to "add the chat"
is undoing the property that makes the migration cheap.

The bare client id candidate at `chat_store.py:140` folds no chat by construction: it addresses rows
whose primary key was the raw value, written before any axis existed, and no chat can be recomputed
into a key already stored. It SHALL therefore remain safe by the scoped read and the in-Python guard
rather than by its derivation — both of which compare the chat — and SHALL match only where the
revision was able to give the old row its real chat.

A message with no client-supplied id SHALL still receive a unique `dedup_key`, so that the column is
non-empty on every row and the index needs no partial predicate to behave the same on every
supported dialect.

#### Scenario: Two owners with the same client message id get two rows

- **WHEN** two owners persist a message whose actor, source, chat id, user id, sender session and
  client message id are identical
- **THEN** both inserts land
- **AND** the two rows carry different `dedup_key` values

#### Scenario: Two conversations of one owner get two rows

- **WHEN** one owner persists the same client message id under one agent in two different chats
- **THEN** both inserts land, each under its own `chat_id`
- **AND** neither insert is absorbed by `ON CONFLICT (dedup_key) DO NOTHING`

#### Scenario: The same owner writing twice gets one row

- **WHEN** one owner persists the same client message id twice under the same agent, chat, user,
  session and actor
- **THEN** the second insert reports that nothing landed
- **AND** one row exists

#### Scenario: The primary key carries no meaning

- **WHEN** a newly persisted message row is inspected
- **THEN** its primary key is not derived from the message's client id, scope, or content
- **AND** nothing reconstructs the primary key in order to find the row again

#### Scenario: A message without a client id is still uniquely keyed

- **WHEN** two messages are persisted with no client message id under one owner
- **THEN** both rows exist with different `dedup_key` values

#### Scenario: A row written before the chat column is still found by the derivation

- **WHEN** a message stored before this change, whose chat the revision recovered, is submitted
  again with the same client message id, owner, agent, chat, user, session and actor
- **THEN** the lookup finds the existing row
- **AND** no second row is created

### Requirement: Finding a message by its dedup identity is a scoped read

The store's lookup by dedup identity SHALL take an `OwnerScope` and SHALL filter on the ownership
predicate as well as on the key, using the same shared constant the other scoped reads use — which
for `messages` is now the pair-with-chat constant, so `get_message_by_dedup_key`
(`sql_runtime_store.py:1727`) narrows on the conversation as well. It SHALL NOT return a row whose
ownership columns differ from the scope it was called with, whatever key it was asked for.

The derivation and the predicate are two enforcement points on purpose. The derivation makes a
cross-owner key impossible; the predicate makes a cross-owner *result* impossible even if a later
change to the derivation regresses. Any in-Python comparison that guards a legacy candidate SHALL
compare the ownership columns too, rather than only the values the old identity folded in — the chat
included, and read from the `chat_id` column rather than from `metadata_json`'s copy, so that a
parked row cannot be matched by a conversation that merely names the same chat in its blob.

#### Scenario: A collision cannot return another owner's row

- **WHEN** the lookup is called with one owner's scope and a key that a row under another owner
  carries
- **THEN** it returns nothing

#### Scenario: A collision cannot return another conversation's row

- **WHEN** the lookup is called with one chat's scope and a key that a row under another chat of the
  same owner and agent carries
- **THEN** it returns nothing

#### Scenario: A legacy candidate is checked for ownership

- **WHEN** a row addressable only through the legacy candidate is a candidate match for a different
  owner
- **THEN** the comparison rejects it on ownership before any other field is considered

### Requirement: One revision lifts existing rows into the new identity, computing it from columns alone

The `dedup_key` backfill SHALL be computed from `owner_id`, `agent_id` and the row's existing primary
key, and from nothing else. It SHALL NOT read `metadata_json`, `payload_json`, or `content`: those
fields are encrypted at rest, so a backfill that depends on them fails or half-runs on a deployment
whose chat encryption key is not present to the migration.

The revision SHALL NOT rewrite any row's primary key. A stored payload carries the row's primary key
and is re-read in preference to the column, and no migration can re-encrypt it; a cursor stored
elsewhere addresses the row by that key as well.

Before any DDL the revision SHALL verify that the schema is the one the previous revision leaves
behind and that the column it adds does not already exist, and SHALL fail loudly rather than apply
part of itself. Before creating the unique index it SHALL verify that the backfilled values are
unique and SHALL name the colliding rows if they are not. It SHALL report how many rows it lifted.

The revision SHALL declare that it is not safe for a rolling deploy, because an instance running the
previous code writes rows without the new column's value.

#### Scenario: The backfill runs without the encryption key

- **WHEN** the revision runs against a database whose message fields are encrypted and whose key is
  not configured for the migration process
- **THEN** every row receives a `dedup_key`
- **AND** no field of any row is decrypted

#### Scenario: A pre-existing row is found by the derivation the application computes

- **WHEN** a message persisted before the revision is submitted again afterwards, with the same
  client message id, owner, agent, chat, user, session and actor
- **THEN** the lookup finds the existing row
- **AND** no second row is created

#### Scenario: Primary keys survive the revision

- **WHEN** the schema is compared before and after the revision
- **THEN** every row's primary key is unchanged
- **AND** a stored cursor naming a message by that key still resolves

#### Scenario: An unrecognised schema stops the revision

- **WHEN** the revision runs against a database that is not at the previous revision, or that
  already carries the column
- **THEN** it raises before issuing any DDL

### Requirement: One revision adds the chat axis, and reads every row's conversation or refuses

The chat axis SHALL be added to `messages` and `context_summaries` by the same Alembic revision
`0016` on head `0015` that materialises the journal marker. The column SHALL be `NOT NULL` with an
empty default, so the constraint holds while the backfill runs and no reader has to answer for a
null forever.

A message's chat lives only in `metadata_json`, which is AES-GCM encrypted at rest on PostgreSQL
when chat encryption is enabled (`ouroboros/storage/message_field_crypto.py:61`). The revision SHALL
decrypt it, importing `decrypt_field` the way `0006_core_memory.py:982-999` already does and for the
reason that revision states: a row whose metadata cannot be read is a row whose answer cannot be
decided. A blob that **parses and names a chat** SHALL take that chat through `str()`, so the column
carries the same spelling a minted scope's string axis does.

A blob that **cannot be decrypted** SHALL abort the revision, in a read-only pass that
completes before any DDL is issued. A deployment whose encryption key is not configured for the
migration process therefore gets a loud failure and an untouched schema, and retries with the key
present. This is the one place the change refuses to guess: parking on an unreadable blob would make
every message of an encrypted deployment vanish from every conversation at once, on the surface
where the pair-wide read was doing real work, and "I could not read it" SHALL NOT resolve to "so it
is gone".

A **plaintext** blob that names no chat, or that will not parse, SHALL be parked rather than abort
the revision — the shape `0006_core_memory.py` already uses for the same input, and for its stated
reason: the row reader answers `{}` for such a blob too, so the row already behaved as one that
records no conversation, and refusing to migrate over it would make an operator repair corruption
that predates this change on a row nothing has ever been able to read. Parking SHALL set `chat_id` to the empty string, which `OwnerScope`'s
non-empty axis validation makes unmatchable by any scope a transport can mint — the same parking
`0008` uses for a key that names no surface. A named sentinel SHALL NOT be used: once a chat id is
any non-empty string a client may present (`accept-opaque-browser-chat-ids`), a spelled sentinel is
a value a caller can mint and land on. No row SHALL be deleted, and the number parked SHALL be
reported.

Every existing `context_summaries` row SHALL be parked unconditionally: it summarizes a backlog
drawn from every conversation of its pair, so no single chat can truthfully claim it. The consequence
is stated rather than discovered — the first consolidation of each conversation after the upgrade
starts from that conversation's oldest reachable message and pays for it once in tokens. No content
is lost: raw rows are retained and the cursor advances only on a committed summary.

A parked row is owner data no scope a transport can mint reaches. Nothing deletes stored history
in this distribution, so the revision leaves it where it is and reports the count rather than
deciding for an operator.

Row mappers SHALL read the chat from the column, never from `metadata_json` or from `payload_json`,
so that a parked row reads back as a row with no reachable conversation rather than one that appears
to have a chat the predicate cannot see.

The indexes SHALL move with the predicate: `idx_messages_owner_agent_visible_created` and
`idx_messages_owner_agent_created` (both from `0008`) SHALL be replaced by forms carrying the chat
between the pair and `created_at`, and `uq_context_summaries_owner_agent`
(`0008_owner_scope_rekey.py:89`) by a unique constraint over the three axes, which is what
`ON CONFLICT` then names. A narrowed read left on a pair-wide index scans every conversation of the
pair to return one.

Before any DDL the revision SHALL verify that the schema is the one `0015` leaves behind and that
neither column already exists, and SHALL fail loudly rather than apply part of itself. It SHALL
declare that it is not safe for a rolling deploy: an instance on the previous code inserts messages
with no chat value, which the default parks — its rows would be invisible to every reader the moment
they are written, and the write would look successful.

#### Scenario: The backfill runs without the encryption key

- **WHEN** the revision runs against a database whose message metadata is encrypted and whose key is
  not configured for the migration process
- **THEN** it raises before issuing any DDL, and the schema is untouched
- **AND** no row is parked, so the upgrade can be retried once the key is configured

#### Scenario: A plaintext blob that will not parse is parked, not fatal

- **WHEN** the revision meets a row whose plaintext `metadata_json` is not valid JSON
- **THEN** the row is parked on the empty chat and retained
- **AND** the revision completes and reports the parked count

#### Scenario: A readable conversation survives the upgrade

- **WHEN** the revision runs against a database whose `metadata_json` is plaintext
- **THEN** each message's `chat_id` column holds the string spelling of the chat its metadata named
- **AND** a read under that conversation's scope returns it afterwards

#### Scenario: A parked row is retained and unreachable

- **WHEN** a row was parked because its conversation could not be read
- **THEN** the row still exists, and no minted scope's chat-scoped read matches it
- **AND** the revision reports how many rows it parked

#### Scenario: Head is 0016 and the keyspace carries the chat

- **WHEN** migrations are run to head on an empty database
- **THEN** the recorded revision is `0016`
- **AND** `messages` and `context_summaries` carry `chat_id`, the summary uniqueness spans the three
  axes, and the message indexes carry the chat between the pair and `created_at`

#### Scenario: An unrecognised schema stops the revision

- **WHEN** the revision runs against a database that is not at `0015`, or one that already carries
  either `chat_id` column
- **THEN** it raises before issuing any DDL

### Requirement: One revision materialises the inbound journal marker, or refuses

The marker the crash-replay sweep filters on SHALL be added to `messages` as a single Alembic
revision `0016` on head `0015`. The column SHALL be `BOOLEAN NOT NULL` defaulting to false, so the
constraint holds while the backfill runs and no reader has to answer for a null.

The column SHALL carry the sweep's whole predicate rather than the metadata key alone: it is true
for an owner-authored row carrying a non-empty `chat_id` whose metadata says the journal never
handed it over and does not mark it `history_only`, and false for every other row. The backfill
SHALL read under that same narrowing, so the upgrade marks exactly the rows the running code would
mark and no reader has to re-test what the column already decided.

The `history_only` term is the one the backfill cannot lose. A row a transport stored as context and
never meant to submit — recovered history, and every group message nobody addressed to the agent —
was written with the unprocessed marker and retired by a second write, so the stored corpus holds
that whole population with the marker still on it wherever the second write did not land. Without
the term the revision marks all of it, and the first sweep after the upgrade submits it as live user
messages: the agent answers, at once, messages that were never addressed to it.

The marker lives only in `metadata_json`, which is AES-GCM encrypted at rest on PostgreSQL when
chat encryption is enabled. The revision SHALL decrypt it in the same paged, read-only pass that
reads each row's conversation, completing before any DDL is issued — the shape
`0006_core_memory.py` already uses. A blob that
**cannot be decrypted** SHALL abort the revision against an untouched schema: a row whose metadata
cannot be read is a row whose answer cannot be decided, and answering "handed over" for it would
retire from the sweep the exact rows the sweep exists for. A **plaintext** blob that will not parse,
or that names no marker, SHALL take false — the same answer the row reader already gives such a
blob, so the column states what the Python predicate already stated.

The revision SHALL create a **partial** index over the marker, keyed on the owner, the agent, the
source and `created_at` — the shape of the one read that uses it. A partial index is what keeps the
sweep's cost proportional to the stranded rows rather than to the stored history, and it is what
makes walking every ownership pair affordable.

Before any DDL the revision SHALL verify that the schema is the one `0015` leaves behind and that
neither the column nor the index already exists, and SHALL fail loudly rather than apply part of
itself. It SHALL declare that it is not safe for a rolling deploy: an instance on the previous code
inserts message rows through a statement that names no marker column, so its rows take the false
default and no sweep will ever see them, while the write looks successful.

The revision SHALL report how many rows it marked, including zero: a backfill that found nothing
and a backfill that did not run are the two states that line exists to tell apart, and the count is
what an operator reads before starting the runtimes that will replay them.

The downgrade SHALL drop the index and the column before it unwinds the revision's other parts, and
SHALL be re-entrant over both drops. The column is derived from metadata every row retains, so
nothing is lost and a re-upgrade recomputes it.

A downgrade that drops the column and then refuses leaves a version record still reading this
revision over a `messages` table that has no marker column, since DDL commits outside the
transaction on SQLite, and every message insert on such a schema fails on a column the code names
and the database does not have. The recovery SHALL be to run the same downgrade again once the
condition it named is resolved, and SHALL NOT be to stamp the revision below and upgrade: this
revision's preflight refuses a schema that already carries the chat axis it adds.

#### Scenario: Head is 0016 and messages carry the marker

- **WHEN** migrations are run to head on an empty database
- **THEN** the recorded revision is `0016`
- **AND** `messages` carries `inbound_unprocessed` and the partial index over it, keyed on the owner,
  the agent, the source and `created_at`

#### Scenario: The backfill runs without the encryption key

- **WHEN** the revision runs against a database whose message metadata is encrypted and whose key is
  not configured for the migration process
- **THEN** it raises before issuing any DDL, and the schema is untouched

#### Scenario: A row the journal never handed over keeps its marker

- **WHEN** the revision runs against a database holding one row whose metadata says the journal never
  handed it over and others that say it did
- **THEN** only that row's column is true afterwards
- **AND** the revision reports the number of rows it marked

#### Scenario: A row no sweep would replay is not marked

- **WHEN** the revision meets a row whose metadata says the journal never handed it over but which
  is not owner-authored, or which carries no conversation
- **THEN** its column is false afterwards

#### Scenario: History the transport only stored is not backfilled

- **WHEN** the revision meets a row whose metadata says the journal never handed it over and also
  marks it `history_only`
- **THEN** its column is false afterwards, so no sweep replays a message nobody addressed to the
  agent

#### Scenario: An unrecognised schema stops the revision

- **WHEN** the revision runs against a database that is not at `0015`, or one that already carries
  the column or the index
- **THEN** it raises before issuing any DDL

#### Scenario: The downgrade runs again over a schema that already lost the column

- **WHEN** the downgrade dropped the marker and its index and then refused, and is run again
- **THEN** it passes over the absent column and index rather than failing on them
