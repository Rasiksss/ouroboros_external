# user-settings-cache Specification

## Purpose

A cache in front of the per-owner settings store. Each entry is one key of one owner-and-agent
scope, so a miss reads exactly the key that missed and a write drops exactly the entry it changed.
The absence of an override is itself a cached answer, every entry expires, and a cache that cannot
be reached is an absence rather than a failure — the store remains the source of truth and holds all
the authority. The boundary is a generic key/value one in the shared infrastructure package, on two
call styles: synchronous for the reader and asynchronous for the HTTP writer, held beside the store
rather than behind it. The backend is chosen by one configuration key — no cache, a cache held in
the process, or a shared one — and the combination that would serve values changed in another
process is refused at start-up rather than started. Reads are specified by `user-settings`, and the
invalidation the writer owes on every mutation by `user-settings-surface`.

## Requirements

### Requirement: A cache answers settings reads, one key at a time

The system SHALL consult a cache before the store when resolving a settable key, and SHALL read the
store only when the cache holds nothing for that owner, agent and key.

A miss SHALL be filled by reading exactly the key that missed, in one statement, and the entry
filled SHALL hold exactly that key. The cache therefore removes repeated reads of one key, not reads
of the keys a task did not ask for: a key resolved many times within a window costs one statement,
and a scope resolving the whole declaration from cold costs one statement per key. Adding a key to
the declaration adds a statement, which is the accepted price of a store that answers exactly what
it was asked.

The cache SHALL be keyed on the owner and the agent together with the key, matching how the rows
themselves are keyed. No entry SHALL be reachable under another owner's id.

#### Scenario: A key is read once and answered from its entry after

- **WHEN** a key with no cached entry is resolved repeatedly
- **THEN** the store is read once
- **AND** every subsequent resolution is answered from the cache

#### Scenario: A hit does not read the store

- **WHEN** a key's entry is cached and it is resolved
- **THEN** the store is not read

#### Scenario: One key's entry does not answer for another

- **WHEN** a scope resolves one key and then a second
- **THEN** the second reads the store rather than the first key's entry

#### Scenario: One owner's entry does not answer for another

- **WHEN** two owners hold overrides under the same agent id and both resolve a key
- **THEN** each is answered from its own entry
- **AND** neither reads the other's

#### Scenario: Two agents of one owner hold independent entries

- **WHEN** an owner's two agents resolve the same key and only one has an override
- **THEN** that agent resolves the override and the other resolves the instance value

### Requirement: A cache that cannot be reached is an absence, for a bounded window

A failure to reach the cache SHALL resolve the key from the store rather than fail the caller: the
store is the source of truth and a cache holds no authority, so a task must not fail because a cache
is down.

It SHALL further be treated as absent for a bounded window rather than attempted again on the next
key. Each attempt costs a connection timeout on the caller's own thread, and both halves of a miss —
the read and the fill that follows it — would otherwise pay one per key, so an unreachable cache
would cost far more than the reads it was composed to save.

A failure to drop an entry SHALL NOT be degraded this way: it propagates to the caller
(`user-settings-surface`).

#### Scenario: A read falls through to the store

- **WHEN** the cache cannot be reached and a key is resolved
- **THEN** the key resolves to the same value it would with no cache composed

#### Scenario: An unreachable cache is asked once per window

- **WHEN** the cache cannot be reached and a scope resolves several keys in turn
- **THEN** the cache is attempted once
- **AND** every key is answered from the store

### Requirement: The absence of an override is a cached answer

The system SHALL cache the result that a key has no stored override, and SHALL treat that cached
result as a hit rather than as a missing entry. An owner who has overridden nothing is the ordinary
case, and a cache that could not hold that answer would read the store on every access for the
majority of owners — which is the whole of what this cache is for.

A cached absence SHALL resolve the key to its instance value, and SHALL be distinguishable from a
key that has never been read. The entry SHALL therefore carry the value rather than be it, since the
boundary answers a miss with the same nothing an absent override would be.

#### Scenario: A key with no override is read once

- **WHEN** a key with no stored override is resolved repeatedly
- **THEN** the store is read once
- **AND** every resolution returns the instance value

#### Scenario: An absence is not a miss

- **WHEN** a key's cached entry states that it has no override
- **THEN** resolving it does not read the store

### Requirement: Every entry expires

Every cache entry SHALL carry an expiry, after which it is no longer served. The expiry window SHALL
be configuration.

The store is written by paths that do not pass through the HTTP surface — the revision that carries
rows over, and an operator acting directly — and an entry that no invalidation reaches would
otherwise be served indefinitely with nothing able to detect it. The expiry bounds how long such a
write stays invisible; it is not the mechanism by which an owner's own change takes effect, which is
invalidation.

#### Scenario: An entry is not served after its window

- **WHEN** an entry's expiry window has passed
- **THEN** the next resolution reads the store

#### Scenario: A row written outside the surface becomes visible

- **WHEN** a row is written directly to the store and no invalidation is issued
- **THEN** the value is resolved after the expiry window at the latest

### Requirement: Writing an override invalidates the entry it changes

The system SHALL drop the cached entry for an owner, agent and key whenever that override is stored
or cleared, so that a reader resolving that key afterwards resolves the written value rather than
the entry the write made wrong. Every mutation route names the key it changes, so the drop SHALL be
exactly that entry and SHALL NOT need to reach the scope's other keys.

Invalidation SHALL happen on the shared cache, not on a copy local to the writing process, so that
readers in other processes are reached.

#### Scenario: A stored override reaches a reader that had cached the scope

- **WHEN** a scope's entry is cached and an owner stores an override for one of its keys
- **THEN** a reader resolving that key resolves the stored value

#### Scenario: A cleared override reaches a reader that had cached the scope

- **WHEN** a scope's entry is cached and an owner clears an override
- **THEN** a reader resolving that key resolves the instance value

#### Scenario: Clearing what is not there still leaves the reader correct

- **WHEN** an owner clears a key that has no stored override
- **THEN** that key does not resolve to a stale value afterwards

#### Scenario: A drop leaves the scope's other keys cached

- **WHEN** several keys of one scope are cached and an owner writes one of them
- **THEN** the written key is read from the store on the next resolution
- **AND** the others are still answered from their entries

#### Scenario: Invalidation is not confined to the writing process

- **WHEN** the writer and a reader run as different processes over a shared cache
- **AND** the writer stores an override
- **THEN** the reader resolves the stored value

### Requirement: The backend is selected by configuration, and the combination that would serve stale values is refused

The system SHALL choose the cache backend from one configuration key, whose values are: no cache, a
cache held in the process, and a shared cache. It SHALL be possible to run with no cache at all.

A cache held in the process is correct only where the writer is that same process. The predicate that
decides this is **whether the process is a component tier**, not whether the deployment is declared
multi-user: the single-process runtime serves several owners in one process legitimately, and its
writer and its readers are that one process. Every component tier SHALL therefore refuse to start on
the in-process cache, and the single-process runtime SHALL NOT make that refusal, rather than either
of them starting and serving values changed arbitrarily long ago. The refusal SHALL name what is
misconfigured and SHALL NOT name any connection URL.

The system SHALL NOT fall back from one backend to another. A backend that cannot be composed SHALL
stop start-up, because a fallback is indistinguishable from success at start-up and surfaces later
as settings that do not take effect.

#### Scenario: No cache is a valid configuration

- **WHEN** the selector names no cache
- **THEN** every accessor reads the store, as before this change

#### Scenario: A component tier refuses an in-process cache

- **WHEN** a component tier starts and the selector names the in-process cache
- **THEN** start-up is refused

#### Scenario: The single-process runtime accepts an in-process cache

- **WHEN** the single-process runtime starts and the selector names the in-process cache
- **THEN** it starts, whether or not the deployment is declared multi-user
- **AND** its writer and its readers share that one cache

#### Scenario: A shared cache that cannot be composed stops start-up

- **WHEN** the selector names the shared cache and it cannot be composed
- **THEN** start-up is refused
- **AND** no other backend is used in its place

#### Scenario: The refusal names no connection URL

- **WHEN** any of these refusals is raised
- **THEN** its message contains no connection URL

### Requirement: The cache is a generic boundary on two call styles, held beside the store rather than behind it

The system SHALL express the cache as a key/value boundary in the shared infrastructure package,
knowing nothing of owners, agents or settings keys, so that a second caller can compose the same
backends without inheriting this one's shapes. Every backend — including the one that holds nothing
— SHALL implement it, so that no cache is a composed object rather than a branch.

The boundary SHALL exist on both call styles: a synchronous one for the reader and an asynchronous
one for the HTTP writer. The writer runs on an event loop that serves every other request of its
tier, so its call SHALL be genuinely asynchronous and SHALL NOT be a synchronous client call moved
to a thread. Both styles SHALL carry the same failure policy, the same serialization and the same
keyspace, and the in-process backend's two styles SHALL address one set of entries, since the
process that composes it is the process where reader and writer are the same.

The reader SHALL hold that boundary beside the store, not behind it. The reuse SHALL be one method
the accessors share: it consults the cache, reads the key it was given from the store on a miss, and
fills that key's entry. Each accessor SHALL name and parse its own key — there is no key map, and
nothing above the reader hands an accessor a value read elsewhere.

The boundary SHALL return a value as it was stored, imposing no shape. A caller that expects a
particular shape SHALL check for it, and an entry of another shape SHALL be treated as a miss.

#### Scenario: Each accessor still owns its key

- **WHEN** the reader resolves a key with a cache composed
- **THEN** that accessor names and parses its own key, as it does with no cache composed

#### Scenario: The store boundary describes one read

- **WHEN** the store is asked for a value
- **THEN** it is asked for one key of one owner and agent, whatever cache is composed

#### Scenario: The writer reaches the cache without the store

- **WHEN** the HTTP writer invalidates
- **THEN** it drops the entry through the asynchronous cache boundary and touches no store

#### Scenario: The in-process backend's two styles are one set of entries

- **WHEN** a value is written through one call style and read through the other
- **THEN** the value written is the value read

#### Scenario: An entry of an unexpected shape is a miss

- **WHEN** the cache holds an entry for a key that is not one of these envelopes
- **THEN** the store is read and the value it returns is what resolves

### Requirement: The package composes a shared cache without reaching the broker's composition

The cache SHALL obtain its clients from factories in the shared infrastructure package, not from the
broker composition, which the settings package may not import. Each external service's connection
options SHALL live in one place in that package, so a deployment never holds two descriptions of how
to reach the same service. Every Redis client, whether the broker composition's or one of the cache's
two call styles, SHALL be built from the one Redis description, `infra/redis_client.py`. Every Kafka
client of the message broker SHALL be built from
the one Kafka description, `infra/kafka_client.py`. The start-up announcement and AEF tracing
producers reach a cluster of their own through their own settings and are recorded debt, not a second
description of the broker's connection. A second call style is a second binding of one description,
and SHALL differ only where the client library forces it.

The shared cache is a Redis cache whatever the broker is, because Kafka offers no key-value store with
expiry and deletion. When the broker is Kafka, the cache's Redis description therefore stands alone,
with no broker client beside it. The cache SHALL NOT build a client from the Kafka settings, and the
Kafka description SHALL NOT read the Redis settings.

The settings package SHALL continue to import no transport, no supervisor, no multi-user
composition and nothing that executes work, statically or lazily.

#### Scenario: The import boundary still holds

- **WHEN** the settings package's modules are parsed, and the package is imported in a fresh process
- **THEN** none of the forbidden packages is imported or loaded

#### Scenario: The connection options have one owner

- **WHEN** the broker is Redis and the client the cache composes is compared with the one the broker composes
- **THEN** both are built by the same factory

#### Scenario: A Kafka broker leaves the cache's Redis description standing alone

- **WHEN** the cache's modules and the Kafka description are inspected
- **THEN** the cache names only the Redis description and never the Kafka one
- **AND** the Kafka description names no Redis module and reads no `OUROBOROS_REDIS_*` setting

### Requirement: A composed cache does not leak between tests

The system SHALL make it possible to compose the settings object with no cache, and the test helper
that rebuilds that object SHALL leave no cache behind from a previous test.

The settings object is assembled once when its module is imported and readers import it, so a cache
reachable through it outlives any one test unless it is replaced along with the store.

#### Scenario: Rebuilding the settings object clears any cached values

- **WHEN** the helper that rebuilds the settings object runs between two tests
- **THEN** no value cached by the first is served to the second
